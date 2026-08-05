from datetime import datetime, timezone
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


@router.get("/ownership-tree/{entity_id}")
def get_ownership_tree(
    entity_id: str,
    response: Response,
    depth: int = 3,
    limit: int = Query(TREE_DEFAULT_LIMIT, ge=1, le=TREE_MAX_LIMIT,
                       description="Max paths to return. X-Result-Truncated says whether more exist."),
):
    """Everything an entity owns, up to `depth` levels deep.

    Path count grows exponentially with depth, so `limit` bounds it. Which paths
    survive the cut is the database's order, not a ranking — a truncated tree is a
    sample of the ownership graph, not its most important part. Callers that need
    completeness should narrow the depth rather than raise the limit.
    """
    # depth must be interpolated as a literal — Cypher doesn't accept a parameter
    # for variable-length path bounds. limit is an int from a validated Query, so
    # it is safe to interpolate the same way.
    safe_depth = max(1, min(int(depth), 10))
    # Fetch one extra row: if it comes back, there was more than `limit`.
    query = f"""
        MATCH path = (:Entity {{id: $entity_id}})-[:OWNS*1..{safe_depth}]->(subsidiary)
        RETURN path
        LIMIT {limit + 1}
    """

    with db.get_session() as session:
        result = session.run(query, entity_id=entity_id, depth=depth)
        paths = []
        for record in result:
            path = record["path"]
            paths.append({
                "nodes": [dict(node) for node in path.nodes],
                "relationships": [dict(rel) for rel in path.relationships]
            })

    truncated = len(paths) > limit
    _mark_truncated(response, truncated)
    return paths[:limit]


@router.get("/owners/{entity_id}")
def get_owners(
    entity_id: str,
    response: Response,
    limit: int = Query(OWNERS_DEFAULT_LIMIT, ge=1, le=OWNERS_MAX_LIMIT,
                       description="Max owner rows to read. X-Result-Truncated says whether more exist."),
):
    """Who owns this entity right now.

    `limit` bounds the rows read from the database. Suppressed owners and nodes
    are filtered out afterwards, in Python, so a truncated response can contain
    *fewer* than `limit` entries — the header, not the length, tells you whether
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
        _mark_truncated(response, truncated)
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
        return out


@router.get("/history/{entity_id}")
def get_ownership_history(
    entity_id: str,
    response: Response,
    limit: int = Query(HISTORY_DEFAULT_LIMIT, ge=1, le=HISTORY_MAX_LIMIT,
                       description="Max events per category (owners in, owned out, roles)."),
):
    """The full ownership + role timeline for an entity.

    Unlike the other two, `limit` applies **per category** — inbound ownership,
    outbound ownership and roles are three separate queries — so the response can
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

    _mark_truncated(response, truncated)
    return sorted(events, key=sort_key, reverse=True)
