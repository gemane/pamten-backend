from typing import Annotated

import re

from fastapi import APIRouter, Query, HTTPException
from app.config import settings
from app.database import db
from app.db.arcadedb import run_sql
from app.scraper.mapper import normalize_entity_name
from app.suppressions import load_keys, is_suppressed, load_suppressed_nodes
from app.pins import load_pins, apply_pin
from app.merged_ids import resolve_current_id

router = APIRouter(prefix="/search", tags=["Search"])


def _clean(row: dict) -> dict:
    """Drop ArcadeDB's @rid/@type/@cat metadata keys from a raw SQL row."""
    return {k: v for k, v in row.items() if not k.startswith("@")}


def _rank(node: dict, q: str, tokens: list[str] | None = None, idx: int = 0,
          nn: str | None = None) -> tuple:
    """
    Sort key (all ascending): (exact_norm, -name_token_matches, match_tier,
    name_len, db_index).

    - exact_norm: 0 when the node's name_normalized equals the normalized query
      (so "BlackRock, Inc." leads for that query, above the many "BLACKROCK …
      FUND, INC." variants that only share common tokens), else 1.
    - name_token_matches: how many query words appear in the NAME.
    - match_tier: 0 exact, 1 starts-with, 2 contains full query, 3 otherwise.
    - notable: 0 for a curated (wikidata_id) entity, else 1 — so within the same
      match quality the notable parent company ("Heineken Holding") floats above
      raw GLEIF registry entries ("HEINEKEN VIETNAM …"). Placed AFTER tokens+tier
      so a better name match still wins (searching "Heineken Vietnam" surfaces the
      subsidiary, not the parent).
    - name_len (tiers 0-2 only) then db_index: keep the FULL_TEXT relevance order
      for weak matches; never tiebreak weak matches on length ("BLG GROUP").
    """
    name = (node.get("name") or "").lower()
    toks = tokens if tokens is not None else q.split()
    matches = sum(1 for t in toks if t and t in name)
    exact_norm = 0 if (nn and node.get("name_normalized") == nn) else 1
    notable = 0 if node.get("wikidata_id") else 1
    if name == q:
        tier = 0
    elif name.startswith(q):
        tier = 1
    elif q in name:
        tier = 2
    else:
        tier = 3
    name_len = len(name) if tier <= 2 else 0
    return (exact_norm, -matches, tier, notable, name_len, idx)


def _entity_candidate_rows(q_lower: str, nn: str | None, country: str | None) -> list[dict]:
    """The Entity candidate set for a query, in candidate order (exact-name, then
    notable/wikidata-tagged, then FULL_TEXT relevance) — cleaned rows, not yet ranked or
    de-duped. Shared by `search()` (its Entity portion) and `resolve_best_entity()` so the
    ranking has a single source of truth. `_rank` orders whatever this returns."""
    if country:
        exact_sql = ("SELECT FROM Entity WHERE name_normalized = :nn "
                     "AND country = :country LIMIT 10")
        exact_params: dict = {"nn": nn, "country": country}
        notable_sql = ("SELECT FROM Entity WHERE search_text CONTAINSTEXT :q "
                       "AND wikidata_id IS NOT NULL AND country = :country LIMIT 15")
        notable_params: dict = {"q": q_lower, "country": country}
        entity_sql = ("SELECT FROM Entity WHERE search_text CONTAINSTEXT :q "
                      "AND country = :country LIMIT 30")
        entity_params: dict = {"q": q_lower, "country": country}
    else:
        exact_sql = "SELECT FROM Entity WHERE name_normalized = :nn LIMIT 10"
        exact_params = {"nn": nn}
        notable_sql = ("SELECT FROM Entity WHERE search_text CONTAINSTEXT :q "
                       "AND wikidata_id IS NOT NULL LIMIT 15")
        notable_params = {"q": q_lower}
        entity_sql = "SELECT FROM Entity WHERE search_text CONTAINSTEXT :q LIMIT 30"
        entity_params = {"q": q_lower}

    rows: list[dict] = []
    if nn:
        rows += [_clean(r) for r in run_sql(exact_sql, exact_params)]
    rows += [_clean(r) for r in run_sql(notable_sql, notable_params)]
    rows += [_clean(r) for r in run_sql(entity_sql, entity_params)]
    # Resilience fallback: if the FULL_TEXT index yields nothing, do a bounded
    # substring scan on the name. ArcadeDB doesn't maintain FULL_TEXT indexes
    # perfectly (an interrupted bulk-load can leave the Lucene index incomplete —
    # see db/schema.py `rebuild_fulltext_indexes(hard=True)`), which silently hides
    # companies that ARE in the DB. This un-indexed scan is the slow path the
    # FULL_TEXT index normally avoids, so it only runs when nothing was found; cap
    # it with LIMIT and gate it behind SEARCH_SUBSTRING_FALLBACK for very large DBs.
    if not rows and settings.SEARCH_SUBSTRING_FALLBACK and q_lower:
        # NB: the param must not be named `like` — ArcadeDB's parser reads `:like` as the
        # LIKE keyword and rejects the statement.
        pat = f"%{q_lower}%"
        if country:
            fb_sql = ("SELECT FROM Entity WHERE name.toLowerCase() LIKE :pat "
                      "AND country = :country LIMIT 20")
            fb_params: dict = {"pat": pat, "country": country}
        else:
            fb_sql = "SELECT FROM Entity WHERE name.toLowerCase() LIKE :pat LIMIT 20"
            fb_params = {"pat": pat}
        rows += [_clean(r) for r in run_sql(fb_sql, fb_params)]
    return rows


def resolve_best_entity(q: str, country: str | None = None) -> dict | None:
    """The single best-matching (non-suppressed) Entity for a query, using the same
    candidate set + `_rank` as `/search` — the shared 'which DB node does this query
    mean' resolver the on-demand scrape uses to decide freshness/target."""
    q_lower = q.lower()
    tokens = q_lower.split()
    nn = normalize_entity_name(q)
    rows = _entity_candidate_rows(q_lower, nn, country)
    if not rows:
        return None
    with db.get_session() as session:
        hidden = load_suppressed_nodes(session)
    rows = [r for r in rows if r.get("id") not in hidden]
    if not rows:
        return None
    ranked = sorted(enumerate(rows), key=lambda ir: _rank(ir[1], q_lower, tokens, ir[0], nn))
    return ranked[0][1]


def resolve_best_person(q: str) -> dict | None:
    """The best-matching (non-suppressed) Person for a query — the person-side twin
    of `resolve_best_entity`, used by the on-demand scrape to decide whether a name
    is somebody we already know.

    No country parameter: `Person` carries a nationality, not a country of
    registration, and filtering people by the country box would answer a question
    nobody asked ("Germans called Müller") with data that means something else.
    """
    q_lower = (q or "").lower()
    tokens = q_lower.split()
    rows = [_clean(r) for r in run_sql(
        "SELECT FROM Person WHERE search_text CONTAINSTEXT :q LIMIT 15", {"q": q_lower})]
    if not rows:
        return None
    with db.get_session() as session:
        hidden = load_suppressed_nodes(session)
    rows = [r for r in rows if r.get("id") not in hidden]
    if not rows:
        return None
    # `_rank` keys off `name`; a Person's is `full_name`.
    ranked = sorted(enumerate(rows),
                    key=lambda ir: _rank({**ir[1], "name": ir[1].get("full_name", "")},
                                         q_lower, tokens, ir[0], None))
    return ranked[0][1]


SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT = 20, 50


@router.get("/")
def search(
    q: Annotated[str, Query(min_length=2)],
    country: Annotated[str | None, Query()] = None,
    # Annotated form, so the default is a real int rather than a Query object.
    # This function is also called directly (integration tests, and any future
    # in-process caller), where FastAPI never resolves the default — with
    # `limit: int = Query(20)` that path got a Query instance and blew up on the
    # first comparison.
    limit: Annotated[int, Query(ge=1, le=SEARCH_MAX_LIMIT,
                                description="Max results after ranking (default 20).")] = SEARCH_DEFAULT_LIMIT,
):
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

    Candidates are gathered per type and then trimmed to `limit` after ranking, so
    a smaller limit still returns the *best* matches rather than the first ones the
    database happened to emit. There is no paging beyond the limit: this is a
    type-ahead, and anything past the top results is noise.
    """
    q_lower = q.lower()
    tokens = q_lower.split()
    nn = normalize_entity_name(q)   # for the exact-name lookup + ranking

    # Index-backed full-text search via the FULL_TEXT index on `search_text`
    # (see db/schema.py). `CONTAINSTEXT` uses the index — an un-indexable
    # `toLower(name) CONTAINS` scan of every Entity took ~12s on 3M rows.
    # ArcadeDB's FULL_TEXT is OR-only over tokens (no phrase/`+` operators) but
    # returns rows already ranked by relevance; we re-rank by NAME token coverage
    # below. Entity and Person run separately — ArcadeDB UNION + LIMIT is unreliable.
    # (CONTAINSTEXT composes with an extra AND filter on ArcadeDB 26.7.2+.)
    # Entity candidates (exact-name, notable/wikidata, FULL_TEXT) come from the shared
    # helper so /search and the on-demand resolver rank the same set.
    results = [{"node": node, "score": 1.0, "type": "Entity"}
               for node in _entity_candidate_rows(q_lower, nn, country)]

    person_sql = "SELECT FROM Person WHERE search_text CONTAINSTEXT :q LIMIT 15"
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
    results.sort(key=lambda r: _rank(r["node"], q_lower, tokens, r["_i"], nn))
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
        if len(out) >= limit:
            break
    return out


def _succession_rows(rows: list, hidden: set) -> list[dict]:
    """Flatten collected {entity, rel} SUCCEEDED_BY maps into entity dicts with the
    succession date (`since`) attached. Drops empty rows and suppressed nodes."""
    out: list[dict] = []
    for r in rows:
        ent = r["entity"]
        if not ent:
            continue
        ent = dict(ent)
        if ent.get("id") in hidden:
            continue
        rel = dict(r["rel"]) if r["rel"] else {}
        ent["since"] = rel.get("since")
        out.append(ent)
    return out


_FREE_FLOAT_MIN = 0.5    # don't surface a residual smaller than this (rounding noise)
_OVER_100_TOL   = 0.5    # tolerance before flagging disclosed ownership > 100%


def _corroborations_for(entity_id: str) -> dict[tuple, list[str]]:
    """Which sources assert each relationship touching this entity.

    Read from `Claim`, which has recorded every source's assertion since claims
    shipped — the writers emit one per edge write — but which nothing ever read
    back. Keyed (from, to, kind), the claim's own identity minus the source, so
    the caller can look up any row it is rendering.

    One query for the whole profile rather than one per row: a profile shows up
    to ~200 relationships and this is on the panel-open path.

    Returns source NAMES, resolved here, because the id is meaningless to the
    client and the edge's own source may not be in the node's source list at all
    (an edge is attributed to whoever created it — the #261 lesson).
    """
    from app.db.arcadedb import run_sql

    rows = run_sql(
        "SELECT from_id, to_id, kind, source_id FROM Claim "
        "WHERE from_id = :id OR to_id = :id", {"id": entity_id})
    if not rows:
        return {}
    names = {r["id"]: r["name"] for r in run_sql("SELECT id, name FROM Source")}
    out: dict[tuple, list[str]] = {}
    for r in rows:
        name = names.get(r["source_id"])
        if not name:
            continue
        key = (r["from_id"], r["to_id"], r["kind"])
        bucket = out.setdefault(key, [])
        if name not in bucket:      # one source asserting twice is one source
            bucket.append(name)
    return out


def _attach_corroboration(rel: dict, claims: dict[tuple, list[str]],
                          from_id: str, to_id: str, kind: str) -> dict:
    """Stamp the asserting sources onto a relationship dict, in place.

    `corroborations` counts distinct sources; `asserted_by` names them, sorted so
    the payload is stable. Rows with no claim rows (edges older than the claims
    table) get 0 and an empty list rather than nothing — absent and unknown are
    different things, and the UI should not have to guess which it is seeing.
    """
    sources = sorted(claims.get((from_id, to_id, kind), []))
    rel["corroborations"] = len(sources)
    rel["asserted_by"] = sources
    return rel


#: Words in a class title that describe rather than identify the security.
#: "Common Stock" and "Common Stock, $0.01 par value per share" are one class;
#: "Series A Shares" and "CPOs" are two.
_CLASS_NOISE = re.compile(
    r"(,?\s*(without|with)\s+(a\s+)?(nominal|par)\s+value[^,;]*)"
    r"|(,?\s*(\$|usd|eur)?\s*[\d.]+\s*(par\s+value|per\s+share)[^,;]*)"
    r"|(,?\s*par\s+value[^,;]*)",
    re.IGNORECASE,
)


def _class_key(title: str | None) -> str | None:
    """A class title reduced to its identity, or None when unstated.

    Two filings describing the same security rarely spell it identically —
    "Common Stock" against "Common Stock, par value $0.0001 per share" — so the
    descriptive tail is dropped before comparing. What survives still separates
    genuinely different instruments, which is the point: Grupo Televisa's
    "Series A Shares; Series B Shares; Dividend Preferred Shares" and
    "Certificados de Participacion Ordinarios (CPOs) and Global D Shares" are
    not the same denominator, and adding percentages of both gave the company
    115.9% of itself.
    """
    if not title or not title.strip():
        return None
    key = title
    key = re.sub(r"\([^)]*\)", " ", key)            # parenthetical glosses: ("A Shares")
    key = re.sub(r",\s*(which|each)\b.*$", "", key, flags=re.IGNORECASE)  # trailing prose
    key = _CLASS_NOISE.sub("", key)
    # A title is often a LIST of securities, and two filers list the same ones
    # in different order with different separators. Compare the set of parts,
    # not the sentence: "Series A Shares; Series B Shares" and "Series A Shares
    # ("A Shares"), Series B Shares ("B Shares")" name one denominator.
    parts = re.split(r"[;,]|\band\b", key, flags=re.IGNORECASE)
    cleaned = sorted({re.sub(r"[^a-z0-9]+", " ", p.lower()).strip() for p in parts} - {""})
    return " | ".join(cleaned) or None


def _ownership_summary(owners: list[dict]) -> dict:
    """Derive a free-float / data-quality summary from an entity's owners.

    `free_float_pct` = 100 − Σ(disclosed stakes), i.e. the widely-held remainder
    (Streubesitz) — but only when EVERY owner has a known stake (an owner with an
    unknown % means we can't tell what's left) and the disclosed total is under
    100%. `exceeds_100` flags aggregation conflicts (overlapping sources/dates)
    rather than silently capping. Percentages, not sourced — computed on read.
    """
    known = [o["relationship"].get("stake_percent") for o in owners
             if isinstance(o["relationship"].get("stake_percent"), (int, float))]
    unknown_owners = sum(
        1 for o in owners
        if not isinstance(o["relationship"].get("stake_percent"), (int, float))
    )

    # Group the stakes by the security they are percentages OF. A percentage is
    # only addable to another when both share a denominator, and a company with
    # several share classes has several denominators.
    by_class: dict = {}
    for o in owners:
        rel = o["relationship"]
        pct = rel.get("stake_percent")
        if not isinstance(pct, (int, float)):
            continue
        key = _class_key(rel.get("share_class"))
        bucket = by_class.setdefault(key, {"title": rel.get("share_class"),
                                           "disclosed_pct": 0.0, "owners": 0})
        bucket["disclosed_pct"] += pct
        bucket["owners"] += 1
    for b in by_class.values():
        b["disclosed_pct"] = round(b["disclosed_pct"], 4)

    # Filings that name their class and disagree about it. Unnamed ones (every
    # pre-2024 filing) can't contradict anybody, so they don't trigger this.
    named = {k for k in by_class if k is not None}
    multi_class = len(named) > 1

    disclosed = round(sum(known), 4) if known else None
    if multi_class:
        # No single number is true here: the parts are measured against
        # different wholes. Saying nothing beats saying 115.9%.
        disclosed = None
    exceeds = disclosed is not None and disclosed > 100.0 + _OVER_100_TOL
    free_float = None
    if disclosed is not None and unknown_owners == 0 and not exceeds:
        residual = round(100.0 - disclosed, 4)
        if residual >= _FREE_FLOAT_MIN:
            free_float = residual
    return {
        "disclosed_pct": disclosed,
        "free_float_pct": free_float,
        "unknown_owners": unknown_owners,
        "exceeds_100": exceeds,
        "multi_class": multi_class,
        # Per-security totals, largest first; the unnamed bucket last.
        "by_class": sorted(
            ({"share_class": b["title"], **{k: v for k, v in b.items() if k != "title"}}
             for b in by_class.values()),
            key=lambda b: (b["share_class"] is None, -b["disclosed_pct"]),
        ),
    }


# Per-section caps for the profile. Each section is its own query, so these bound
# the payload additively; before the split one entity's page could inline every
# subsidiary it had (236 of them, ~197 KB, measured on the dev database).
PROFILE_SECTION_LIMIT = 200
PROFILE_SECTION_MAX = 1_000

# One query per section, each anchored on the indexed Entity id.
#
# This replaced a single MATCH with seven OPTIONAL MATCHes. That form is a
# cartesian product: the engine materialises owners x subsidiaries x executives x
# … rows and collect(DISTINCT) then discards all but one. Measured on the dev
# database, Microsoft (24 x 15 x 33) produced 11,880 intermediate rows for a
# single page view. It is multiplicative, so it degrades non-linearly as scraper
# coverage fills more than one dimension on the same company — 100 owners x 236
# subsidiaries x 50 executives would be 1.18M rows. Separate queries make the
# cost additive instead, and give each section its own LIMIT.
# Every section anchors on the indexed Entity id and follows the edge outward or
# inward. Writing an inbound section the natural-reading way — (owner)-[:OWNS]->(e
# {id}) — makes ArcadeDB scan instead of using the index: measured standalone on
# the dev database, that form took 589 ms against 15 ms for the anchored one. It
# was harmless inside the old OPTIONAL MATCH chain because `e` was already bound;
# as separate queries it is not. Same trap documented in routers/relationships.py.
#
# `WITH <node>, collect(<edge>)` groups first, so LIMIT counts DISTINCT nodes
# rather than raw edges. That distinction is not cosmetic: a re-imported dump
# leaves duplicate OWNS edges (Johnson & Johnson has 236 edges to 160 distinct
# subsidiaries on the dev database), and limiting the edges let duplicates eat the
# budget — LIMIT 200 returned just 124 of the 160 companies. Grouping first makes
# the cap mean what a caller assumes it means.
# Redundant ultimate-parent edges are excluded from BOTH the lists and the counts,
# so the panel and the graph describe the same company the same way. Two views
# disagreeing about who owns what is worse than either answer alone.
#
# GLEIF often states a pair twice — "X is the direct parent of Y" AND "X is the
# ultimate parent of Y" — which showed the same owner twice in the owners list and
# repeated a company at every level of a group's tree. `shortcut` is set by
# maintenance.mark_ownership_shortcuts on edges proven to duplicate a path of
# direct edges; NULL means unproven and is always kept.
_NOT_A_SHORTCUT = "({rel}.shortcut IS NULL OR {rel}.shortcut <> true)"

_NODE_EDGE_SECTIONS = {
    "owners": (
        "MATCH (e:Entity {{id: $id}})<-[owns_r:OWNS]-(owner) WHERE owns_r.until IS NULL "
        "AND " + _NOT_A_SHORTCUT.format(rel="owns_r") + " "
        "WITH owner, collect(owns_r) AS rels RETURN owner AS node, rels LIMIT {limit}"),
    "subsidiaries": (
        "MATCH (e:Entity {{id: $id}})-[sub_r:OWNS]->(subsidiary) WHERE sub_r.until IS NULL "
        "AND " + _NOT_A_SHORTCUT.format(rel="sub_r") + " "
        "WITH subsidiary, collect(sub_r) AS rels RETURN subsidiary AS node, rels LIMIT {limit}"),
    "executives": (
        "MATCH (e:Entity {{id: $id}})<-[role_r:HAS_ROLE]-(p:Person) WHERE role_r.until IS NULL "
        "WITH p, collect(role_r) AS rels RETURN p AS node, rels LIMIT {limit}"),
    "succeeded_by": (
        "MATCH (e:Entity {{id: $id}})-[succ_r:SUCCEEDED_BY]->(succ:Entity) "
        "WITH succ, collect(succ_r) AS rels RETURN succ AS node, rels LIMIT {limit}"),
    "replaces": (
        "MATCH (e:Entity {{id: $id}})<-[pred_r:SUCCEEDED_BY]-(pred:Entity) "
        "WITH pred, collect(pred_r) AS rels RETURN pred AS node, rels LIMIT {limit}"),
}

# Real totals per section, independent of the row limit.
#
# The client cannot derive these: each section is capped at PROFILE_SECTION_LIMIT,
# so the length of a returned array is a lower bound, not a count. Barclays has 118
# subsidiaries and Unilever 112 in the *test subset* alone; a full import is larger
# still, and "Subsidiaries" with no number next to a truncated list tells the reader
# nothing.
#
# count(DISTINCT node), never count(edge): the section queries group with
# `WITH <node>, collect(<edge>)` precisely because duplicate edges exist — Johnson &
# Johnson has 236 edges to 160 distinct subsidiaries — so counting edges would print
# a number that disagrees with the list it labels.
#
# Same anchoring discipline as the sections: start from the indexed Entity id and
# follow the edge outward or inward. The unanchored inbound form measured 589 ms
# against 15 ms.
_SECTION_COUNTS = {
    "owners": ("MATCH (e:Entity {id: $id})<-[r:OWNS]-(owner) WHERE r.until IS NULL "
               "AND " + _NOT_A_SHORTCUT.format(rel="r") + " "
               "RETURN count(DISTINCT owner) AS n"),
    "subsidiaries": ("MATCH (e:Entity {id: $id})-[r:OWNS]->(sub) WHERE r.until IS NULL "
                     "AND " + _NOT_A_SHORTCUT.format(rel="r") + " "
                     "RETURN count(DISTINCT sub) AS n"),
    "executives": ("MATCH (e:Entity {id: $id})<-[r:HAS_ROLE]-(p:Person) WHERE r.until IS NULL "
                   "RETURN count(DISTINCT p) AS n"),
    "dual_listed": ("MATCH (e:Entity {id: $id})-[:DUAL_LISTED_WITH]->(d:Entity) "
                    "RETURN count(DISTINCT d) AS n"),
    "succeeded_by": ("MATCH (e:Entity {id: $id})-[:SUCCEEDED_BY]->(s:Entity) "
                     "RETURN count(DISTINCT s) AS n"),
    "replaces": ("MATCH (e:Entity {id: $id})<-[:SUCCEEDED_BY]-(pr:Entity) "
                 "RETURN count(DISTINCT pr) AS n"),
}


def _section_counts(session, entity_id: str) -> dict:
    """True size of each section, whatever the row limit returned."""
    out = {}
    for name, cypher in _SECTION_COUNTS.items():
        try:
            rec = session.run(cypher, id=entity_id).single()
            out[name] = int(rec["n"]) if rec and rec["n"] is not None else 0
        except Exception:  # noqa: BLE001 — a missing count must not lose the profile
            out[name] = None
    return out


# Sections that are bare nodes with no edge properties to carry.
_NODE_ONLY_SECTIONS = {
    "dual_listed": (
        "MATCH (e:Entity {{id: $id}})-[:DUAL_LISTED_WITH]->(dlc:Entity) "
        "WITH DISTINCT dlc RETURN dlc AS node LIMIT {limit}"),
}


def _pairs(rows, node_key: str, rel_key: str) -> list[dict]:
    """Expand grouped (node, [edges]) rows back into one dict per edge.

    The post-processing downstream already collapses duplicates — picking the
    largest stake, the most recent tenure — so it is handed the same shape it
    always got, and that logic stays untouched by the split.
    """
    out = []
    for row in rows:
        node = row["node"]
        for rel in (row["rels"] or []):
            out.append({node_key: node, rel_key: rel})
    return out


@router.get("/entity/{entity_id}/full-profile")
def get_full_profile(
    entity_id: str,
    limit: Annotated[int, Query(ge=1, le=PROFILE_SECTION_MAX,
                                description="Max rows per section (owners, subsidiaries, …).")] = PROFILE_SECTION_LIMIT,
):
    # HQ lives on the Entity itself (hq_locations / hq_city / hq_country /
    # hq_lat / hq_lng). The Location vertex it used to be read from was a
    # parallel representation of the same fact, and the client never used it.
    head_query = "MATCH (e:Entity {id: $id}) RETURN e LIMIT 1"
    with db.get_session() as session:
        head = session.run(head_query, id=entity_id).single()
        if not head:
            # A merge may have folded this id away — follow the forwarding
            # address so a shared link to the old node still opens the company.
            # Only on a miss; the section queries below then use the survivor's id.
            merged_into = resolve_current_id(session, entity_id)
            if merged_into:
                head = session.run(head_query, id=merged_into).single()
                if head:
                    entity_id = merged_into
        if not head:
            raise HTTPException(status_code=404, detail="Entity not found")

        grouped = {
            name: list(session.run(sql.format(limit=limit), id=entity_id))
            for name, sql in _NODE_EDGE_SECTIONS.items()
        }
        plain = {
            name: [r["node"] for r in session.run(sql.format(limit=limit), id=entity_id)]
            for name, sql in _NODE_ONLY_SECTIONS.items()
        }

        counts = _section_counts(session, entity_id)

        # Same keys the single-query version produced, so the post-processing
        # below is unchanged.
        record = {
            "e": head["e"],
            "dual_listed": plain["dual_listed"],
            "owners": _pairs(grouped["owners"], "owner", "rel"),
            "subsidiaries": _pairs(grouped["subsidiaries"], "entity", "rel"),
            "executives": _pairs(grouped["executives"], "person", "role"),
            "succeeded_by": _pairs(grouped["succeeded_by"], "entity", "rel"),
            "replaces": _pairs(grouped["replaces"], "entity", "rel"),
        }

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
            # Drop a self-loop (A owns A — treasury shares or a data error): it's
            # not a real owner and would inflate the disclosed % / free-float.
            if oid == entity_id or oid in hidden or is_suppressed(sup, "owns", oid, entity_id):
                continue
            rel = apply_pin(pins, oid, entity_id, dict(o["rel"]))
            cur = owners_by.get(oid)
            if cur is None or (rel.get("stake_percent") or -1) > (cur["relationship"].get("stake_percent") or -1):
                owners_by[oid] = {"owner": owner, "relationship": rel}
        owners = list(owners_by.values())
        claims = _corroborations_for(entity_id)
        for o in owners:
            _attach_corroboration(o["relationship"], claims,
                                  o["owner"].get("id"), entity_id, "owns")

        subs_by: dict[str, dict] = {}
        for s in record["subsidiaries"]:
            if not s["entity"]:
                continue
            sub = dict(s["entity"])
            sid = sub.get("id")
            if sid == entity_id or sid in hidden or is_suppressed(sup, "owns", entity_id, sid):
                continue
            rel = apply_pin(pins, entity_id, sid, dict(s["rel"]))
            cur = subs_by.get(sid)
            if cur is None or (rel.get("stake_percent") or -1) > (cur["relationship"].get("stake_percent") or -1):
                subs_by[sid] = {"entity": sub, "relationship": rel}
        subsidiaries = list(subs_by.values())
        for sub in subsidiaries:
            _attach_corroboration(sub["relationship"], claims,
                                  entity_id, sub["entity"].get("id"), "owns")

        execs_by: dict[tuple, dict] = {}
        for ex in record["executives"]:
            if not ex["person"]:
                continue
            person, role = dict(ex["person"]), dict(ex["role"])
            if person.get("id") in hidden or is_suppressed(sup, "role", person.get("id"), entity_id, role.get("role")):
                continue
            execs_by.setdefault((person.get("id"), role.get("role")), {"person": person, "role": role})
        executives = list(execs_by.values())
        for ex in executives:
            _attach_corroboration(ex["role"], claims,
                                  ex["person"].get("id"), entity_id, "role")

        # Circular ownership: an entity that is BOTH an owner and a subsidiary of
        # this one (A↔B reciprocal holding). Surface it as a data-quality signal.
        sub_ids = {s["entity"]["id"] for s in subsidiaries}
        cross_holdings = [o["owner"] for o in owners if o["owner"].get("id") in sub_ids]

        # The parties to a filing group. They join by RELATED_TO, not OWNS —
        # membership is not ownership — so nothing in the owners query above can
        # see them, and the group's panel would otherwise list nobody.
        group_members = []
        if (dict(record["e"]).get("type") or "") == "voting_group":
            for m in session.run(
                    """MATCH (m)-[r:RELATED_TO]->(g:Entity {id: $id})
                       WHERE r.relation = 'group_member'
                       RETURN m, labels(m)[0] AS label""", id=entity_id):
                node = dict(m["m"])
                if node.get("id") in hidden:
                    continue
                group_members.append({"party": node, "kind": m["label"].lower()})
            group_members.sort(key=lambda x: (x["party"].get("name")
                                              or x["party"].get("full_name") or ""))

        # …and the groups this entity is a party TO. The mirror of the above:
        # without it a member's profile has no idea it belongs to a bloc, so
        # centring Altria could never draw the agreement it votes in — the
        # payload simply lacked the fact.
        voting_groups = []
        for g in session.run(
                """MATCH (m {id: $id})-[r:RELATED_TO]->(g:Entity)
                   WHERE r.relation = 'group_member'
                   RETURN g""", id=entity_id):
            node = dict(g["g"])
            if node.get("id") not in hidden:
                voting_groups.append({"group": node})

        return {
            "entity": dict(record["e"]),
            "group_members": group_members,
            "voting_groups": voting_groups,
            # True totals, independent of the per-section row limit above.
            "counts": counts,
            "owners": owners,
            "ownership": _ownership_summary(owners),
            "cross_holdings": cross_holdings,
            "subsidiaries": subsidiaries,
            "executives": executives,
            "dual_listed": [dict(d) for d in record["dual_listed"] if d],
            "succeeded_by": _succession_rows(record["succeeded_by"], hidden),
            "replaces": _succession_rows(record["replaces"], hidden),
        }


def _dedupe_positions(rows: list) -> list:
    """
    Collapse to one entry per (entity, role, since) — one *spell*, not one job.

    Two sources describing the same appointment must not become two rows; two
    genuine spells of the same appointment must not become one. Where the same
    spell arrives twice, keep the entry with the later end date, since the one
    that knows the tenure ended later is the better-informed of the two. Sorted
    for a stable display order.
    """
    best: dict[tuple, dict] = {}
    for x in rows:
        if not x["entity"]:
            continue
        entity, role = dict(x["entity"]), dict(x["rel"])
        # Keyed on the start date as well as the company and the role, because
        # holding the same post twice is a real thing and the second spell is
        # often the interesting one: Steve Jobs sat on Apple's board from 1977,
        # left in 1985 and returned in 1997. Collapsing on (company, role) threw
        # one of those away — and with it the fact the timeline exists to show.
        key = (entity["id"], role.get("role"), role.get("since"))
        cur = best.get(key)
        if cur is None or (role.get("until") or "") > (cur["role"].get("until") or ""):
            best[key] = {"entity": entity, "role": role}
    return sorted(best.values(),
                  key=lambda e: ((e["entity"].get("name") or "").lower(),
                                 e["role"].get("role") or "",
                                 e["role"].get("since") or ""))


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
    Everything about a person in one call: the positions they have held (HAS_ROLE
    → entity) and the entities they own (OWNS → entity). Both already in the graph
    from scraping — the entity full-profile surfaces them from the company side;
    this surfaces them from the person side.

    **Ended roles are included.** They were filtered out here, which made the
    payload answer "what does this person do now" and nothing else: Steve Jobs
    came back with three positions out of six, missing both of his spells on
    Apple's board and his run as its CEO. A caller that wants only current
    positions has `until` to filter on; a caller that wants a career — the
    timeline — cannot invent what was never sent.
    """
    query = """
        MATCH (p:Person {id: $id})
        OPTIONAL MATCH (p)-[role_r:HAS_ROLE]->(org:Entity)
        OPTIONAL MATCH (p)-[owns_r:OWNS]->(owned:Entity)
        RETURN p,
               collect(DISTINCT {entity: org,   rel: role_r}) as positions,
               collect(DISTINCT {entity: owned, rel: owns_r}) as holdings
    """

    with db.get_session() as session:
        record = session.run(query, id=person_id).single()
        if not record:
            # Person dedup merges aggressively (auto-merge on scrape), so a
            # person id folded away is the common case, not a rare one.
            merged_into = resolve_current_id(session, person_id)
            if merged_into:
                record = session.run(query, id=merged_into).single()
                if record:
                    person_id = merged_into
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

        claims = _corroborations_for(person_id)
        positions_out = _dedupe_positions(positions)
        for pos in positions_out:
            _attach_corroboration(pos["role"], claims,
                                  person_id, pos["entity"].get("id"), "role")
        for h in holdings_out:
            _attach_corroboration(h["relationship"], claims,
                                  person_id, h["entity"].get("id"), "owns")

        return {
            "person": dict(record["p"]),
            "positions": positions_out,
            "holdings": holdings_out,
        }


@router.get("/geographic")
def search_by_country(country: str, region: str = None):
    # Find all entities in a country or region
    query = """
        MATCH (e:Entity)
        WHERE e.country = $country
        RETURN e
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
