from fastapi import APIRouter, Query, HTTPException
from app.database import db
from app.suppressions import load_keys, is_suppressed
from app.pins import load_pins, apply_pin

router = APIRouter(prefix="/search", tags=["Search"])


def _rank(node: dict, q: str) -> tuple:
    """
    Sort key: (match_tier ASC, name_length ASC).
    Tier 0 = exact, 1 = starts-with, 2 = contains name, 3 = alias/description only.
    Lower tier and shorter name float to the top.
    """
    name = (node.get("name") or "").lower()
    if name == q:
        return (0, len(name))
    if name.startswith(q):
        return (1, len(name))
    if q in name:
        return (2, len(name))
    return (3, len(name))


@router.get("/")
def search(q: str = Query(..., min_length=2), country: str | None = Query(default=None)):
    """
    Search for entities and persons by name, alias, or description.

    Matches against:
    - `name` (primary field)
    - `aliases` — Wikidata alternate labels (e.g. "AB InBev" finds "Anheuser-Busch InBev")
    - `description` — short Wikidata description

    Results are ranked by match quality:
    1. **Exact name match** — query equals the full name
    2. **Starts-with** — name begins with the query; shorter names rank higher within this tier
    3. **Contains** — query appears anywhere in the name
    4. **Alias / description match** — query only matched a secondary field

    Up to 20 entities and 10 persons are returned (30 total, trimmed to 20 after ranking).
    """
    q_lower = q.lower()

    # Run Entity and Person queries separately — ArcadeDB UNION + LIMIT is unreliable.
    if country:
        entity_cypher = """
            MATCH (n:Entity)
            WHERE toLower(n.name) CONTAINS $q AND n.country = $country
            RETURN n AS node, 1.0 AS score, 'Entity' AS type
            LIMIT 20
        """
        entity_params: dict = {"q": q_lower, "country": country}
    else:
        entity_cypher = """
            MATCH (n:Entity)
            WHERE toLower(n.name) CONTAINS $q
               OR toLower(coalesce(n.description, '')) CONTAINS $q
               OR any(alias IN coalesce(n.aliases, []) WHERE toLower(alias) CONTAINS $q)
            RETURN n AS node, 1.0 AS score, 'Entity' AS type
            LIMIT 20
        """
        entity_params = {"q": q_lower}

    person_cypher = """
        MATCH (n:Person)
        WHERE toLower(n.full_name) CONTAINS $q
           OR any(a IN coalesce(n.alias, []) WHERE toLower(a) CONTAINS $q)
        RETURN n AS node, 1.0 AS score, 'Person' AS type
        LIMIT 10
    """

    results = []
    with db.get_session() as session:
        for record in session.run(entity_cypher, **entity_params):
            results.append({
                "node":  dict(record["node"]),
                "score": record["score"],
                "type":  record["type"],
            })
        if not country:
            for record in session.run(person_cypher, q=q_lower):
                results.append({
                    "node":  dict(record["node"]),
                    "score": record["score"],
                    "type":  record["type"],
                })

    results.sort(key=lambda r: _rank(r["node"], q_lower))
    return results[:20]


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

        # Drop suppressed edges and apply pinned corrections (both read-time overlays).
        sup = load_keys(session)
        pins = load_pins(session)

        owners = []
        for o in record["owners"]:
            if not o["owner"]:
                continue
            owner = dict(o["owner"])
            if is_suppressed(sup, "owns", owner.get("id"), entity_id):
                continue
            rel = apply_pin(pins, owner.get("id"), entity_id, dict(o["rel"]))
            owners.append({"owner": owner, "relationship": rel})

        subsidiaries = []
        for s in record["subsidiaries"]:
            if not s["entity"]:
                continue
            sub = dict(s["entity"])
            if is_suppressed(sup, "owns", entity_id, sub.get("id")):
                continue
            rel = apply_pin(pins, entity_id, sub.get("id"), dict(s["rel"]))
            subsidiaries.append({"entity": sub, "relationship": rel})

        executives = []
        for ex in record["executives"]:
            if not ex["person"]:
                continue
            person, role = dict(ex["person"]), dict(ex["role"])
            if is_suppressed(sup, "role", person.get("id"), entity_id, role.get("role")):
                continue
            executives.append({"person": person, "role": role})

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

        # Drop moderator-suppressed edges before collapsing.
        sup = load_keys(session)
        positions = [x for x in record["positions"] if x["entity"] and not is_suppressed(
            sup, "role", person_id, dict(x["entity"]).get("id"), dict(x["rel"]).get("role"))]
        holdings = [x for x in record["holdings"] if x["entity"] and not is_suppressed(
            sup, "owns", person_id, dict(x["entity"]).get("id"))]

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
