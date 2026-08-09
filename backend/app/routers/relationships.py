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
from app.suppressions import load_keys, is_suppressed, load_suppressed_nodes
from app.pins import load_pins, apply_pin

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
    # Works for both Person->Entity and Entity->Entity
    query = """
        MATCH (owner {id: $owner_id})
        MATCH (owned:Entity {id: $owned_id})
        CREATE (owner)-[r:OWNS {
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
        }]->(owned)
        RETURN r
    """

    with db.get_session() as session:
        result = session.run(query, last_scraped_at=_now_iso(), **data.model_dump())
        if not result.single():
            raise HTTPException(status_code=404, detail="Owner or Entity not found")
        return {"message": "Ownership relationship created"}


@router.post("/owns/close")
def close_owns_relationship(owner_id: str, owned_id: str, until: str, _: dict = Depends(require_contributor)):
    # When ownership ends, set the until date (becomes historical)
    query = """
        MATCH (owner {id: $owner_id})-[r:OWNS]->(owned:Entity {id: $owned_id})
        WHERE r.until IS NULL
        SET r.until = $until
        RETURN r
    """

    with db.get_session() as session:
        result = session.run(query,
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
    edge_filter = "" if include_indirect else (
        "WHERE ALL(e IN r WHERE e.direct_or_indirect IS NULL "
        "OR e.direct_or_indirect <> 'indirect')"
    )
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


@router.get("/ownership-tree/{entity_id}")
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


def owners_of(entity_id: str, limit: int = OWNERS_DEFAULT_LIMIT) -> tuple[list[dict], bool]:
    """Who owns this entity right now. Returns (owners, truncated).

    `limit` bounds the rows read from the database. Suppressed owners and nodes
    are filtered out afterwards, in Python, so a truncated result can contain
    *fewer* than `limit` entries — the flag, not the length, tells you whether
    anything was cut.
    """
    # Anchor on the indexed Entity and follow the edge inward — the unanchored
    # (owner)-[:OWNS]->(e {id}) form makes ArcadeDB scan every node at scale.
    query = f"""
        MATCH (e:Entity {{id: $entity_id}})<-[r:OWNS]-(owner)
        WHERE r.until IS NULL
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


@router.get("/owners/{entity_id}")
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


@router.get("/history/{entity_id}")
def get_ownership_history(
    entity_id: str,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=HISTORY_MAX_LIMIT,
                                description="Max events per category (owners in, owned out, roles).")] = HISTORY_DEFAULT_LIMIT,
):
    events, truncated = ownership_history_of(entity_id, limit)
    _mark_truncated(response, truncated)
    return events
