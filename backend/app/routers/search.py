from fastapi import APIRouter, Query, HTTPException
from app.database import db
from app.db.arcadedb import run_sql
from app.suppressions import load_keys, is_suppressed, load_suppressed_nodes
from app.pins import load_pins, apply_pin

router = APIRouter(prefix="/search", tags=["Search"])


def _clean(row: dict) -> dict:
    """Drop ArcadeDB's @rid/@type/@cat metadata keys from a raw SQL row."""
    return {k: v for k, v in row.items() if not k.startswith("@")}


def _rank(node: dict, q: str, tokens: list[str] | None = None, idx: int = 0) -> tuple:
    """
    Sort key (all ascending): (-name_token_matches, match_tier, db_index).

    - name_token_matches: how many query words appear in the NAME. Entities whose
      name contains more of the query ("Axel Springer SE" for "axel springer",
      "Carlsberg Group" for "carlsberg group") float above bare single-word hits.
    - match_tier: 0 exact, 1 starts-with, 2 contains full query, 3 otherwise.
    - db_index: preserves ArcadeDB's FULL_TEXT relevance order for ties, so a
      rare-token match (Dangote…) stays above a common-token one (…GROUP) — we
      deliberately do NOT tiebreak on name length (which floated "BLG GROUP" up).
    """
    name = (node.get("name") or "").lower()
    toks = tokens if tokens is not None else q.split()
    matches = sum(1 for t in toks if t and t in name)
    if name == q:
        tier = 0
    elif name.startswith(q):
        tier = 1
    elif q in name:
        tier = 2
    else:
        tier = 3
    # Shorter name wins only when the name actually matches the full query
    # (tiers 0-2) — e.g. "Apple Inc." over "Apple Sales International". For
    # weaker matches, keep the DB's relevance order instead of name length.
    name_len = len(name) if tier <= 2 else 0
    return (-matches, tier, name_len, idx)


@router.get("/")
def search(q: str = Query(..., min_length=2), country: str | None = Query(default=None)):
    """
    Full-text search for entities and persons.

    Backed by a FULL_TEXT index on `search_text` (Entity: name + description +
    aliases; Person: full_name + aliases), queried with `CONTAINSTEXT`. Matching
    is by whole word/token and position-independent — "busch" finds "Anheuser-
    Busch InBev", and an alias finds the node it was merged into — but it does
    NOT match arbitrary mid-word substrings ("ovarti" won't find "Novartis").
    Multi-word queries require ALL words (AND), so "carlsberg group" won't match
    every unrelated "* GROUP" company. Populate `search_text` with
    `manage.py backfill-search`.

    Results are ranked by match quality against the name:
    1. **Exact name match** — query equals the full name
    2. **Starts-with** — name begins with the query; shorter names rank higher within this tier
    3. **Contains** — query appears anywhere in the name

    Up to 20 entities and 10 persons are returned (30 total, trimmed to 20 after ranking).
    """
    q_lower = q.lower()
    tokens = q_lower.split()

    # Index-backed full-text search via the FULL_TEXT index on `search_text`
    # (see db/schema.py). `CONTAINSTEXT` uses the index — an un-indexable
    # `toLower(name) CONTAINS` scan of every Entity took ~12s on 3M rows.
    # ArcadeDB's FULL_TEXT is OR-only (no AND/phrase/`+` operators) but returns
    # rows already ranked by relevance (rows matching more/rarer tokens first),
    # so we keep that order and only re-rank by NAME token coverage below.
    # Entity and Person run separately — ArcadeDB UNION + LIMIT is unreliable.
    if country:
        entity_sql = ("SELECT FROM Entity WHERE search_text CONTAINSTEXT :q "
                      "AND country = :country LIMIT 30")
        entity_params: dict = {"q": q_lower, "country": country}
    else:
        entity_sql = "SELECT FROM Entity WHERE search_text CONTAINSTEXT :q LIMIT 30"
        entity_params = {"q": q_lower}

    person_sql = "SELECT FROM Person WHERE search_text CONTAINSTEXT :q LIMIT 15"

    results = []
    for row in run_sql(entity_sql, entity_params):
        results.append({"node": _clean(row), "score": 1.0, "type": "Entity"})
    if not country:
        for row in run_sql(person_sql, {"q": q_lower}):
            results.append({"node": _clean(row), "score": 1.0, "type": "Person"})

    # Hide moderator-suppressed nodes from search.
    with db.get_session() as session:
        hidden = load_suppressed_nodes(session)

    results = [r for r in results if r["node"].get("id") not in hidden]
    # Rank: entities whose NAME contains more of the query words first (so
    # "carlsberg group" beats a bare "* GROUP"), then exact/starts-with, then the
    # DB's own relevance order (index position) — never name length, which used
    # to float short unrelated names like "BLG GROUP" to the top.
    results = [{**r, "_i": i} for i, r in enumerate(results)]
    results.sort(key=lambda r: _rank(r["node"], q_lower, tokens, r["_i"]))
    # De-dupe by node id (CONTAINSTEXT can return a row per index bucket), keeping
    # the highest-ranked instance, and cap at 20.
    out: list[dict] = []
    seen: set = set()
    for r in results:
        nid = r["node"].get("id")
        if nid in seen:
            continue
        seen.add(nid)
        out.append({k: v for k, v in r.items() if k != "_i"})
        if len(out) >= 20:
            break
    return out


@router.get("/entity/{entity_id}/full-profile")
def get_full_profile(entity_id: str):
    # Everything about an entity in one call
    query = """
        MATCH (e:Entity {id: $id})
        OPTIONAL MATCH (e)-[:HEADQUARTERED_IN]->(hq:Location)
        OPTIONAL MATCH (e)-[:OPERATES_IN]->(ops:Location)
        OPTIONAL MATCH (owner)-[owns_r:OWNS]->(e) WHERE owns_r.until IS NULL
        OPTIONAL MATCH (e)-[sub_r:OWNS]->(subsidiary) WHERE sub_r.until IS NULL
        OPTIONAL MATCH (p:Person)-[role_r:HAS_ROLE]->(e) WHERE role_r.until IS NULL
        OPTIONAL MATCH (e)-[:DUAL_LISTED_WITH]->(dlc:Entity)
        RETURN e,
               hq,
               collect(DISTINCT ops) as operations,
               collect(DISTINCT {owner: owner, rel: owns_r}) as owners,
               collect(DISTINCT {entity: subsidiary, rel: sub_r}) as subsidiaries,
               collect(DISTINCT {person: p, role: role_r}) as executives,
               collect(DISTINCT dlc) as dual_listed
    """

    with db.get_session() as session:
        result = session.run(query, id=entity_id)
        record = result.single()
        if not record:
            raise HTTPException(status_code=404, detail="Entity not found")

        # Read-time overlays: suppressed edges/nodes dropped, pinned values applied.
        sup = load_keys(session)
        hidden = load_suppressed_nodes(session)
        pins = load_pins(session)

        # A suppressed entity is hidden entirely.
        if entity_id in hidden:
            raise HTTPException(status_code=404, detail="Entity not found")

        # Collapse duplicate OWNS/HAS_ROLE edges (a re-imported BODS dump can
        # create a second identical edge to the same node — CREATE EDGE isn't
        # idempotent), keeping the largest stake so the row isn't shown twice.
        owners_by: dict[str, dict] = {}
        for o in record["owners"]:
            if not o["owner"]:
                continue
            owner = dict(o["owner"])
            oid = owner.get("id")
            if oid in hidden or is_suppressed(sup, "owns", oid, entity_id):
                continue
            rel = apply_pin(pins, oid, entity_id, dict(o["rel"]))
            cur = owners_by.get(oid)
            if cur is None or (rel.get("stake_percent") or -1) > (cur["relationship"].get("stake_percent") or -1):
                owners_by[oid] = {"owner": owner, "relationship": rel}
        owners = list(owners_by.values())

        subs_by: dict[str, dict] = {}
        for s in record["subsidiaries"]:
            if not s["entity"]:
                continue
            sub = dict(s["entity"])
            sid = sub.get("id")
            if sid in hidden or is_suppressed(sup, "owns", entity_id, sid):
                continue
            rel = apply_pin(pins, entity_id, sid, dict(s["rel"]))
            cur = subs_by.get(sid)
            if cur is None or (rel.get("stake_percent") or -1) > (cur["relationship"].get("stake_percent") or -1):
                subs_by[sid] = {"entity": sub, "relationship": rel}
        subsidiaries = list(subs_by.values())

        execs_by: dict[tuple, dict] = {}
        for ex in record["executives"]:
            if not ex["person"]:
                continue
            person, role = dict(ex["person"]), dict(ex["role"])
            if person.get("id") in hidden or is_suppressed(sup, "role", person.get("id"), entity_id, role.get("role")):
                continue
            execs_by.setdefault((person.get("id"), role.get("role")), {"person": person, "role": role})
        executives = list(execs_by.values())

        return {
            "entity": dict(record["e"]),
            "headquarters": dict(record["hq"]) if record["hq"] else None,
            "operations": [dict(loc) for loc in record["operations"] if loc],
            "owners": owners,
            "subsidiaries": subsidiaries,
            "executives": executives,
            "dual_listed": [dict(d) for d in record["dual_listed"] if d],
        }


def _dedupe_positions(rows: list) -> list:
    """
    Collapse to one entry per (entity, role). A person can hold several HAS_ROLE
    edges for the same role at the same company — e.g. two CEO tenures with
    different `since` dates — which are distinct in the graph but duplicate noise
    in a current-positions view. Keep the most recent tenure. Sorted for a stable
    display order.
    """
    best: dict[tuple, dict] = {}
    for x in rows:
        if not x["entity"]:
            continue
        entity, role = dict(x["entity"]), dict(x["rel"])
        key = (entity["id"], role.get("role"))
        cur = best.get(key)
        if cur is None or (role.get("since") or "") > (cur["role"].get("since") or ""):
            best[key] = {"entity": entity, "role": role}
    return sorted(best.values(),
                  key=lambda e: ((e["entity"].get("name") or "").lower(), e["role"].get("role") or ""))


def _dedupe_holdings(rows: list) -> list:
    """One entry per owned entity — keep the largest stake if it appears twice."""
    best: dict[str, dict] = {}
    for x in rows:
        if not x["entity"]:
            continue
        entity, rel = dict(x["entity"]), dict(x["rel"])
        key = entity["id"]
        cur = best.get(key)
        if cur is None or (rel.get("stake_percent") or -1) > (cur["relationship"].get("stake_percent") or -1):
            best[key] = {"entity": entity, "relationship": rel}
    return sorted(best.values(), key=lambda e: (e["entity"].get("name") or "").lower())


@router.get("/person/{person_id}/full-profile")
def get_person_profile(person_id: str):
    """
    Everything about a person in one call: the positions they hold (HAS_ROLE →
    entity) and the entities they own (OWNS → entity). Both already in the graph
    from scraping — the entity full-profile surfaces them from the company side;
    this surfaces them from the person side.
    """
    query = """
        MATCH (p:Person {id: $id})
        OPTIONAL MATCH (p)-[role_r:HAS_ROLE]->(org:Entity) WHERE role_r.until IS NULL
        OPTIONAL MATCH (p)-[owns_r:OWNS]->(owned:Entity)   WHERE owns_r.until IS NULL
        RETURN p,
               collect(DISTINCT {entity: org,   rel: role_r}) as positions,
               collect(DISTINCT {entity: owned, rel: owns_r}) as holdings
    """

    with db.get_session() as session:
        record = session.run(query, id=person_id).single()
        if not record:
            raise HTTPException(status_code=404, detail="Person not found")

        sup = load_keys(session)
        hidden = load_suppressed_nodes(session)
        if person_id in hidden:
            raise HTTPException(status_code=404, detail="Person not found")

        # Drop suppressed edges and edges to suppressed entities before collapsing.
        positions = [x for x in record["positions"] if x["entity"]
                     and dict(x["entity"]).get("id") not in hidden
                     and not is_suppressed(sup, "role", person_id, dict(x["entity"]).get("id"), dict(x["rel"]).get("role"))]
        holdings = [x for x in record["holdings"] if x["entity"]
                    and dict(x["entity"]).get("id") not in hidden
                    and not is_suppressed(sup, "owns", person_id, dict(x["entity"]).get("id"))]

        # Apply pinned OWNS corrections to the collapsed holdings.
        pins = load_pins(session)
        holdings_out = _dedupe_holdings(holdings)
        for h in holdings_out:
            h["relationship"] = apply_pin(pins, person_id, h["entity"].get("id"), h["relationship"])

        return {
            "person": dict(record["p"]),
            "positions": _dedupe_positions(positions),
            "holdings": holdings_out,
        }


@router.get("/geographic")
def search_by_country(country: str, region: str = None):
    # Find all entities in a country or region
    query = """
        MATCH (e:Entity)-[:HEADQUARTERED_IN]->(l:Location)
        WHERE l.country = $country
        RETURN e, l
        ORDER BY e.name
    """

    with db.get_session() as session:
        result = session.run(query, country=country)
        return [
            {
                "entity": dict(record["e"]),
                "location": dict(record["l"])
            }
            for record in result
        ]
