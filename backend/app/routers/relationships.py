from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Depends, Query, Response
from app.auth.dependencies import require_contributor
from app.models.relationship import (
    OwnsRelationshipCreate,
    RoleRelationshipCreate,
    RelatedToCreate,
    DualListedCreate,
)
from app.database import db
from app.db.anchors import node_label
from app.suppressions import load_keys, is_suppressed, load_suppressed_nodes
from app.pins import load_pins, apply_pin
from app.claims import KIND_OWNS, record_claim
from app.routers.search import _NOT_A_SHORTCUT

router = APIRouter(prefix="/relationships", tags=["Relationships"])

# These three read endpoints walk the graph and previously returned every row the
# query produced. On a hub node — a nominee custodian, a large holding — that is
# tens of thousands of rows, which is a slow query, a multi-megabyte response and
# an unusable payload on a phone. Each now has a bounded default that a caller can
# raise to a hard ceiling.
#
# Truncation is reported in the `X-Result-Truncated` response header rather than by
# changing the response body: these endpoints return bare JSON arrays, and wrapping
# them in an envelope would break every already-released client (the unversioned
# mount is still serving them — see main.py). The header is listed in the CORS
# expose_headers, or browsers wouldn't be allowed to read it.
TRUNCATED_HEADER = "X-Result-Truncated"

TREE_DEFAULT_LIMIT, TREE_MAX_LIMIT = 500, 5_000
OWNERS_DEFAULT_LIMIT, OWNERS_MAX_LIMIT = 200, 1_000
HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT = 500, 2_000


def _mark_truncated(response: Response, truncated: bool) -> None:
    response.headers[TRUNCATED_HEADER] = "true" if truncated else "false"


def _strip_meta(doc) -> dict:
    """Drop ArcadeDB's @rid/@type/@cat metadata keys from a returned document."""
    if not isinstance(doc, dict):
        return doc
    return {k: v for k, v in doc.items() if not k.startswith("@")}


def _now_iso() -> str:
    """UTC timestamp for last_scraped_at / last-recorded provenance."""
    return datetime.now(timezone.utc).isoformat()


@router.post("/owns")
def create_owns_relationship(data: OwnsRelationshipCreate, _: dict = Depends(require_contributor)):
    # Works for both Person->Entity and Entity->Entity — which is why the owner's
    # label is looked up first: an unlabelled anchor scans every vertex type
    # (minutes on the full import; see app/db/anchors.py).
    query = """
        MATCH (owner:{label} {{id: $owner_id}})
        MATCH (owned:Entity {{id: $owned_id}})
        CREATE (owner)-[r:OWNS {{
            stake_percent: $stake_percent,
            ownership_type: $ownership_type,
            since: $since,
            until: $until,
            value_usd: $value_usd,
            source_id: $source_id,
            credibility_score: $credibility_score,
            source_url: $source_url,
            source_date: $source_date,
            last_scraped_at: $last_scraped_at
        }}]->(owned)
        RETURN r
    """

    with db.get_session() as session:
        label = node_label(data.owner_id, session)
        if not label:
            raise HTTPException(status_code=404, detail="Owner or Entity not found")
        result = session.run(query.format(label=label),
                             last_scraped_at=_now_iso(), **data.model_dump())
        if not result.single():
            raise HTTPException(status_code=404, detail="Owner or Entity not found")
    # A manual assertion is still an assertion: without the claim, an edge
    # created here could never corroborate (or be contradicted by) a scraper's.
    # A claim is one source's statement, so it needs a source to speak for —
    # no source_id, no claim (the edge itself is still created above).
    if data.source_id:
        record_claim(kind=KIND_OWNS, from_id=data.owner_id, to_id=data.owned_id,
                     source_id=data.source_id, stake_percent=data.stake_percent,
                     ownership_type=data.ownership_type.value, since=data.since,
                     source_url=data.source_url, source_date=data.source_date,
                     credibility_score=data.credibility_score or 80)
    return {"message": "Ownership relationship created"}


@router.post("/owns/close")
def close_owns_relationship(owner_id: str, owned_id: str, until: str, _: dict = Depends(require_contributor)):
    # When ownership ends, set the until date (becomes historical)
    query = """
        MATCH (owner:{label} {{id: $owner_id}})-[r:OWNS]->(owned:Entity {{id: $owned_id}})
        WHERE r.until IS NULL
        SET r.until = $until
        RETURN r
    """

    with db.get_session() as session:
        label = node_label(owner_id, session)
        if not label:
            raise HTTPException(status_code=404, detail="Active relationship not found")
        result = session.run(query.format(label=label),
            owner_id=owner_id,
            owned_id=owned_id,
            until=until
        )
        if not result.single():
            raise HTTPException(status_code=404, detail="Active relationship not found")
        return {"message": "Ownership relationship closed"}


@router.post("/roles")
def create_role_relationship(data: RoleRelationshipCreate, _: dict = Depends(require_contributor)):
    query = """
        MATCH (p:Person {id: $person_id})
        MATCH (e:Entity {id: $entity_id})
        CREATE (p)-[r:HAS_ROLE {
            role: $role,
            since: $since,
            until: $until,
            source_id: $source_id,
            credibility_score: $credibility_score,
            source_url: $source_url,
            source_date: $source_date,
            last_scraped_at: $last_scraped_at
        }]->(e)
        RETURN r
    """

    with db.get_session() as session:
        result = session.run(query, last_scraped_at=_now_iso(), **data.model_dump())
        if not result.single():
            raise HTTPException(status_code=404, detail="Person or Entity not found")
        return {"message": "Role relationship created"}


@router.post("/roles/close")
def close_role_relationship(person_id: str, entity_id: str, until: str, _: dict = Depends(require_contributor)):
    query = """
        MATCH (p:Person {id: $person_id})-[r:HAS_ROLE]->(e:Entity {id: $entity_id})
        WHERE r.until IS NULL
        SET r.until = $until
        RETURN r
    """

    with db.get_session() as session:
        result = session.run(query,
            person_id=person_id,
            entity_id=entity_id,
            until=until
        )
        if not result.single():
            raise HTTPException(status_code=404, detail="Active role not found")
        return {"message": "Role relationship closed"}


@router.post("/related-to")
def create_related_to(data: RelatedToCreate, _: dict = Depends(require_contributor)):
    query = """
        MATCH (a:Person {id: $person_a_id})
        MATCH (b:Person {id: $person_b_id})
        MERGE (a)-[r:RELATED_TO {relation: $relation}]->(b)
        RETURN r
    """

    with db.get_session() as session:
        result = session.run(query, **data.model_dump())
        if not result.single():
            raise HTTPException(status_code=404, detail="One or both persons not found")
        return {"message": "Relationship created"}


@router.post("/dual-listed")
def create_dual_listed(data: DualListedCreate, _: dict = Depends(require_contributor)):
    """
    Link two entities as a dual-listed company (symmetric, non-ownership).
    MERGE so re-adding is idempotent; provenance is stamped on the edge.
    """
    # Store both directions so the relationship is symmetric and can be found
    # with a plain directed match (an undirected match returns a path that the
    # result layer can't iterate).
    query = """
        MATCH (a:Entity {id: $entity_a_id})
        MATCH (b:Entity {id: $entity_b_id})
        MERGE (a)-[r1:DUAL_LISTED_WITH]->(b)
        MERGE (b)-[r2:DUAL_LISTED_WITH]->(a)
        SET r1.source_id = $source_id, r1.source_url = $source_url,
            r1.source_date = $source_date, r1.last_scraped_at = $last_scraped_at,
            r2.source_id = $source_id, r2.source_url = $source_url,
            r2.source_date = $source_date, r2.last_scraped_at = $last_scraped_at
        RETURN r1
    """
    with db.get_session() as session:
        result = session.run(query, last_scraped_at=_now_iso(), **data.model_dump())
        if not result.single():
            raise HTTPException(status_code=404, detail="One or both entities not found")
        return {"message": "Dual-listed relationship created"}


def ownership_tree_of(
    entity_id: str, depth: int = 3, limit: int = TREE_DEFAULT_LIMIT,
    include_indirect: bool = True,
) -> tuple[list[dict], bool]:
    """Everything an entity owns, up to `depth` levels deep. Returns (paths, truncated).

    Path count grows exponentially with depth, so `limit` bounds it. Which paths
    survive the cut is the database's order, not a ranking — a truncated tree is a
    sample of the ownership graph, not its most important part. Callers that need
    completeness should narrow the depth rather than raise the limit.

    ``include_indirect`` defaults to **True**. It briefly defaulted to False, to
    drop GLEIF's "ultimate parent" shortcut edges on the grounds that they
    duplicate a path the tree already contains. That is true of most of them but
    not all: where GLEIF recorded the top of a chain and not its steps, the
    shortcut is the only ownership there is, and excluding it made 58 of 484
    owned entities unreachable. Whether a given shortcut is redundant is a global
    property, computed by ``maintenance.mark_ownership_shortcuts`` and stamped on
    the edge as ``shortcut`` — filter on that, not on the kind.

    Passing False still filters by kind. Useful for a caller that genuinely wants
    only directly-held subsidiaries, but it will omit companies whose sole link is
    an ultimate-parent edge.

    Edges with no ``direct_or_indirect`` at all (Wikidata, SEC — sources that
    never state the distinction) are always kept: absent is not the same as
    indirect, and dropping them would silently lose the only ownership those
    sources record.

    Kept separate from the route because the route takes a `Response` to set the
    truncation header, and FastAPI only injects that over HTTP — an in-process
    caller would have to invent one.
    """
    # depth must be interpolated as a literal — Cypher doesn't accept a parameter
    # for variable-length path bounds. limit is an int from a validated Query, so
    # it is safe to interpolate the same way.
    safe_depth = max(1, min(int(depth), 10))
    # `RETURN path` is NOT usable here: ArcadeDB hands a path back as its string
    # form — "(#1:3)-[#37:20725]->(#1:120)" — not an object with .nodes/.relationships,
    # so unpacking it raised AttributeError for every entity that actually had a
    # subsidiary. nodes()/relationships() return the real documents instead.
    # Fetch one extra row: if it comes back, there was more than `limit`.
    # When filtering is asked for, it must hold for EVERY hop — hence ALL() over
    # the bound edge list rather than a plain WHERE, which would test only the
    # last edge and let a shortcut back in halfway down a chain. Verified against
    # a real ArcadeDB: ALL() over a variable-length binding is supported.
    # Proven-redundant shortcuts are always dropped — the docstring above has
    # promised "filter on that, not on the kind" since the include_indirect
    # episode, and search.py's graph endpoints already do; a tree keeping them
    # repeats a company at every level of its group.
    #
    # COALESCE instead of `x IS NULL OR x <> v`: ArcadeDB's Cypher rejects any
    # PARENTHESIZED predicate inside ALL(...) with "Variable 'e' not defined"
    # (verified on 26.7.3), so _NOT_A_SHORTCUT — fine in a plain WHERE, used in
    # owners_of below — cannot be composed here. Same null semantics: an
    # unstamped edge is kept.
    only_direct = ("" if include_indirect else
                   " AND COALESCE(e.direct_or_indirect, 'direct') <> 'indirect'")
    edge_filter = ("WHERE ALL(e IN r WHERE "
                   f"COALESCE(e.shortcut, false) <> true{only_direct})")
    query = f"""
        MATCH path = (:Entity {{id: $entity_id}})-[r:OWNS*1..{safe_depth}]->(subsidiary)
        {edge_filter}
        RETURN nodes(path) AS path_nodes, relationships(path) AS path_rels
        LIMIT {limit + 1}
    """

    with db.get_session() as session:
        result = session.run(query, entity_id=entity_id, depth=depth)
        paths = []
        for record in result:
            paths.append({
                "nodes": [_strip_meta(n) for n in (record["path_nodes"] or [])],
                "relationships": [_strip_meta(r) for r in (record["path_rels"] or [])],
            })

    return paths[:limit], len(paths) > limit


@router.get("/ownership-tree/{entity_id:path}")
def get_ownership_tree(
    entity_id: str,
    response: Response,
    depth: int = 3,
    limit: Annotated[int, Query(ge=1, le=TREE_MAX_LIMIT,
                                description="Max paths. X-Result-Truncated says whether more exist.")] = TREE_DEFAULT_LIMIT,
    include_indirect: Annotated[bool, Query(
        description="Include GLEIF 'ultimate parent' edges. On by default — most duplicate a "
                    "path the tree already contains, but some are the only link to a company, "
                    "so excluding them by kind loses entities.")] = True,
):
    paths, truncated = ownership_tree_of(entity_id, depth, limit, include_indirect)
    _mark_truncated(response, truncated)
    return paths


#: The whole tree below one company, as distinct nodes rather than paths. Tenet
#: Healthcare has 1,161 (two levels), Chubb 256 over nine; the cap is on NODES.
SUBTREE_DEFAULT_NODES, SUBTREE_MAX_NODES, SUBTREE_MAX_DEPTH = 2_000, 5_000, 12


def subsidiary_tree_of(entity_id: str, max_nodes: int = SUBTREE_DEFAULT_NODES) -> dict | None:
    """Every company below this one, level by level — the tree the graph draws
    and the panel indents. None when the entity does not exist.

    `{root_id, nodes: [{entity, parent_id, depth}], edges: [{from_id, to_id,
    depth, relationship}], truncated}`. `nodes` names ONE parent per company
    so a list can indent it and a graph can place it: its DEEPEST holder in
    the tree between holders of equal stake, the largest holder otherwise
    (`_placing_parents`). `edges`
    carries every holding, so the graph can still draw a co-holder's line.

    A walk, not a path query: `ownership_tree_of` returns PATHS, whose number
    grows exponentially with depth and repeats every shared prefix. Here each
    level is one adjacency expansion from the previous level's vertices by rid
    (the shape the OWNS dedup uses — never a scan), so nine levels of Chubb
    are nine round-trips. Current edges only, proven shortcuts left out — the
    same rule as the profile, so the tree and the panel agree; suppressed
    nodes and edges are dropped and not walked through; pins apply. A company
    reached twice (two holders, or a cycle) is a node once.
    """
    from app.db.arcadedb import run_sql
    root = run_sql("SELECT @rid AS rid FROM Entity WHERE id = :id", {"id": entity_id})
    if not root:
        return None
    with db.get_session() as session:
        sup = load_keys(session)
        hidden = load_suppressed_nodes(session)
        pins = load_pins(session)
    seen: dict[str, str] = {root[0]["rid"]: entity_id}      # vertex rid -> id
    frontier = [root[0]["rid"]]
    nodes: list[dict] = []
    edges: list[dict] = []
    truncated = False
    for depth in range(1, SUBTREE_MAX_DEPTH + 1):
        if not frontier:
            break
        rows = run_sql(
            "SELECT *, @out AS o, @in AS i FROM "
            f"(SELECT expand(outE('OWNS')) FROM [{', '.join(frontier)}]) "
            "WHERE until IS NULL AND (shortcut IS NULL OR shortcut <> true)")
        new_rids = sorted({r["i"] for r in rows} - set(seen))
        children = {r["@rid"]: r for r in run_sql(
            f"SELECT FROM [{', '.join(new_rids)}]")} if new_rids else {}
        # the largest stake first, so the parent a list shows is the main holder
        rows.sort(key=lambda r: -(r.get("stake_percent") or -1))
        frontier = []
        for r in rows:
            parent_id = seen.get(r["o"])
            child = children.get(r["i"])
            child_id = (child or {}).get("id") or seen.get(r["i"])
            if not parent_id or not child_id or child_id == parent_id:
                continue
            if child_id in hidden or is_suppressed(sup, "owns", parent_id, child_id):
                continue
            if r["i"] not in seen:
                if len(nodes) >= max_nodes:
                    truncated = True
                    continue
                seen[r["i"]] = child_id
                frontier.append(r["i"])
                nodes.append({"entity": _strip_meta(child), "parent_id": parent_id, "depth": depth})
            edges.append({"from_id": parent_id, "to_id": child_id, "depth": depth,
                          "relationship": apply_pin(pins, parent_id, child_id, _strip_meta(
                              {k: v for k, v in r.items() if k not in ("o", "i")}))})
    else:
        truncated = truncated or bool(frontier)
    _placing_parents(entity_id, nodes, edges)
    return {"root_id": entity_id, "nodes": nodes, "edges": edges, "truncated": truncated}


def _placing_parents(root_id: str, nodes: list[dict], edges: list[dict]) -> None:
    """Name the ONE holder that places each company: its largest and, between
    equals, its deepest. In place.

    The walk finds a company at its SHALLOWEST level. But a filer's flat
    subsidiary list names everything it controls at any depth: Microsoft's
    Exhibit 21 lists Activision's subsidiaries beside Activision, while GLEIF
    says they sit under Activision. Shallowest-first hung them off Microsoft,
    and Activision's own lines to them ran across the whole picture. The more
    specific statement is the deeper one, so a company goes under the holder
    furthest from the root — its depth is the longest path to it.

    That holds between EQUALS — neither of those two states a stake. Where
    stakes are stated the largest holder places the company: Chubb's 99.9 %
    holding company, not the affiliate with 0.1 % that happens to sit deeper
    (which left the company without a visible line under the ≥1 % filter).
    A holder is never chosen if it is the company's own descendant, so a
    cross-holding cannot detach a branch from the root. Edge depths follow
    their holder.
    (The frontend's tree layout applies the same rule: `utils/treeLayout.ts`.)
    """
    parent = {n["entity"]["id"]: n["parent_id"] for n in nodes}
    depth = {root_id: 0, **{n["entity"]["id"]: n["depth"] for n in nodes}}

    def pct(e: dict) -> float:
        # No stated percentage (a consolidation parent, a filer's subsidiary
        # list) counts as control when holders compete to place a company.
        v = (e.get("relationship") or {}).get("stake_percent")
        return float(v) if isinstance(v, (int, float)) else 50.0

    stake = {e["to_id"]: pct(e) for e in edges if parent.get(e["to_id"]) == e["from_id"]}

    def is_ancestor(candidate: str, of: str) -> bool:
        seen = set()
        while of in parent and of not in seen:
            seen.add(of)
            of = parent[of]
            if of == candidate:
                return True
        return False

    changed, rounds = True, 0
    while changed and rounds <= len(nodes) + 1:
        changed, rounds = False, rounds + 1
        for e in edges:
            holder, held = e["from_id"], e["to_id"]
            if held == root_id or held not in parent or holder not in depth:
                continue
            d = depth[holder] + 1
            if parent[held] == holder:          # its parent moved down: it follows
                if d != depth[held]:
                    depth[held], changed = d, True
                continue
            cur = stake.get(held, 50.0)
            better = pct(e) > cur or (pct(e) == cur and d > depth[held])
            if better and not is_ancestor(held, holder):
                parent[held], depth[held], stake[held] = holder, d, pct(e)
                changed = True
    for n in nodes:
        n["parent_id"], n["depth"] = parent[n["entity"]["id"]], depth[n["entity"]["id"]]
    for e in edges:
        e["depth"] = depth.get(e["from_id"], 0) + 1


@router.get("/subsidiary-tree/{entity_id:path}")
def get_subsidiary_tree(
    entity_id: str,
    response: Response,
    max_nodes: Annotated[int, Query(ge=1, le=SUBTREE_MAX_NODES,
                                    description="Max companies in the tree. X-Result-Truncated "
                                                "says whether more exist.")] = SUBTREE_DEFAULT_NODES,
):
    """Every company below this one, all levels: distinct nodes (each with one
    parent and its depth) and every holding between them."""
    tree = subsidiary_tree_of(entity_id, max_nodes)
    if tree is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    _mark_truncated(response, tree["truncated"])
    return tree


def owners_of(entity_id: str, limit: int = OWNERS_DEFAULT_LIMIT) -> tuple[list[dict], bool]:
    """Who owns this entity right now. Returns (owners, truncated).

    `limit` bounds the rows read from the database. Suppressed owners and nodes
    are filtered out afterwards, in Python, so a truncated result can contain
    *fewer* than `limit` entries — the flag, not the length, tells you whether
    anything was cut.
    """
    # Anchor on the indexed Entity and follow the edge inward — the unanchored
    # (owner)-[:OWNS]->(e {id}) form makes ArcadeDB scan every node at scale.
    # Same shortcut rule as search.py's node sections: a redundant
    # ultimate-parent edge would list the same owner twice here while the
    # graph view (which filters) shows it once.
    query = f"""
        MATCH (e:Entity {{id: $entity_id}})<-[r:OWNS]-(owner)
        WHERE r.until IS NULL AND {_NOT_A_SHORTCUT.format(rel="r")}
        RETURN owner, r
        LIMIT {limit + 1}
    """

    with db.get_session() as session:
        rows = list(session.run(query, entity_id=entity_id))
        truncated = len(rows) > limit
        rows = rows[:limit]
        sup = load_keys(session)                  # suppressed owner edges
        hidden = load_suppressed_nodes(session)   # suppressed owner nodes
        pins = load_pins(session)                 # pinned corrections
        out = []
        for record in rows:
            owner = dict(record["owner"])
            if owner.get("id") in hidden or is_suppressed(sup, "owns", owner.get("id"), entity_id):
                continue
            rel = apply_pin(pins, owner.get("id"), entity_id, dict(record["r"]))
            out.append({"owner": owner, "relationship": rel})
        return out, truncated


@router.get("/owners/{entity_id:path}")
def get_owners(
    entity_id: str,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=OWNERS_MAX_LIMIT,
                                description="Max owner rows read. X-Result-Truncated says whether more exist.")] = OWNERS_DEFAULT_LIMIT,
):
    owners, truncated = owners_of(entity_id, limit)
    _mark_truncated(response, truncated)
    return owners


def ownership_history_of(
    entity_id: str, limit: int = HISTORY_DEFAULT_LIMIT,
) -> tuple[list[dict], bool]:
    """The full ownership + role timeline for an entity. Returns (events, truncated).

    Unlike the other two, `limit` applies **per category** — inbound ownership,
    outbound ownership and roles are three separate queries — so the result can
    hold up to 3 × `limit` events. Limiting the merged total would mean one noisy
    category could crowd the others out of the timeline entirely.
    """
    events = []
    truncated = False

    with db.get_session() as session:
        # Who owns / owned this entity
        rows = list(session.run(
            f"""
            MATCH (e:Entity {{id: $id}})<-[r:OWNS]-(owner)
            RETURN owner, r, 'ownership_in' AS kind
            LIMIT {limit + 1}
            """,
            id=entity_id,
        ))
        truncated = truncated or len(rows) > limit
        for rec in rows[:limit]:
            events.append({
                "kind":          "ownership_in",
                "party":         dict(rec["owner"]),
                "since":         rec["r"].get("since"),
                # "first_listed": since is a LOWER bound (oldest annual subsidiary
                # list naming it), not the start of the holding
                "since_basis":   rec["r"].get("since_basis"),
                "until":         rec["r"].get("until"),
                "active":        rec["r"].get("until") is None,
                "stake_percent": rec["r"].get("stake_percent"),
                "ownership_type": rec["r"].get("ownership_type"),
            })

        # What this entity owns / owned
        rows = list(session.run(
            f"""
            MATCH (e:Entity {{id: $id}})-[r:OWNS]->(owned)
            RETURN owned, r, 'ownership_out' AS kind
            LIMIT {limit + 1}
            """,
            id=entity_id,
        ))
        truncated = truncated or len(rows) > limit
        for rec in rows[:limit]:
            events.append({
                "kind":          "ownership_out",
                "party":         dict(rec["owned"]),
                "since":         rec["r"].get("since"),
                # "first_listed": since is a LOWER bound (oldest annual subsidiary
                # list naming it), not the start of the holding
                "since_basis":   rec["r"].get("since_basis"),
                "until":         rec["r"].get("until"),
                "active":        rec["r"].get("until") is None,
                "stake_percent": rec["r"].get("stake_percent"),
                "ownership_type": rec["r"].get("ownership_type"),
            })

        # Executive roles at this entity
        rows = list(session.run(
            f"""
            MATCH (e:Entity {{id: $id}})<-[r:HAS_ROLE]-(p:Person)
            RETURN p, r, 'role' AS kind
            LIMIT {limit + 1}
            """,
            id=entity_id,
        ))
        truncated = truncated or len(rows) > limit
        for rec in rows[:limit]:
            events.append({
                "kind":   "role",
                "party":  dict(rec["p"]),
                "since":  rec["r"].get("since"),
                "until":  rec["r"].get("until"),
                "active": rec["r"].get("until") is None,
                "role":   rec["r"].get("role"),
            })

    # Dated events first (desc), undated at bottom
    def sort_key(e):
        return e["since"] or ""

    return sorted(events, key=sort_key, reverse=True), truncated


@router.get("/history/{entity_id:path}")
def get_ownership_history(
    entity_id: str,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=HISTORY_MAX_LIMIT,
                                description="Max events per category (owners in, owned out, roles).")] = HISTORY_DEFAULT_LIMIT,
):
    events, truncated = ownership_history_of(entity_id, limit)
    _mark_truncated(response, truncated)
    return events
