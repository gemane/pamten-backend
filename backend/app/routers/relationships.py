from fastapi import APIRouter, HTTPException, Depends
from app.auth.dependencies import require_contributor
from app.models.relationship import (
    OwnsRelationshipCreate,
    RoleRelationshipCreate,
    RelatedToCreate
)
from app.database import db

router = APIRouter(prefix="/relationships", tags=["Relationships"])


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
            credibility_score: $credibility_score
        }]->(owned)
        RETURN r
    """

    with db.get_session() as session:
        result = session.run(query, **data.model_dump())
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
            credibility_score: $credibility_score
        }]->(e)
        RETURN r
    """

    with db.get_session() as session:
        result = session.run(query, **data.model_dump())
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


@router.get("/ownership-tree/{entity_id}")
def get_ownership_tree(entity_id: str, depth: int = 3):
    # Get everything an entity owns, up to N levels deep.
    # depth must be interpolated as a literal — Cypher doesn't accept a parameter
    # for variable-length path bounds.
    safe_depth = max(1, min(int(depth), 10))
    query = f"""
        MATCH path = (:Entity {{id: $entity_id}})-[:OWNS*1..{safe_depth}]->(subsidiary)
        RETURN path
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
        return paths


@router.get("/owners/{entity_id}")
def get_owners(entity_id: str):
    # Who owns this entity right now?
    query = """
        MATCH (owner)-[r:OWNS]->(e:Entity {id: $entity_id})
        WHERE r.until IS NULL
        RETURN owner, r
    """

    with db.get_session() as session:
        result = session.run(query, entity_id=entity_id)
        return [
            {
                "owner": dict(record["owner"]),
                "relationship": dict(record["r"])
            }
            for record in result
        ]


@router.get("/history/{entity_id}")
def get_ownership_history(entity_id: str):
    events = []

    with db.get_session() as session:
        # Who owns / owned this entity
        for rec in session.run(
            """
            MATCH (owner)-[r:OWNS]->(e:Entity {id: $id})
            RETURN owner, r, 'ownership_in' AS kind
            """,
            id=entity_id,
        ):
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
        for rec in session.run(
            """
            MATCH (e:Entity {id: $id})-[r:OWNS]->(owned)
            RETURN owned, r, 'ownership_out' AS kind
            """,
            id=entity_id,
        ):
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
        for rec in session.run(
            """
            MATCH (p:Person)-[r:HAS_ROLE]->(e:Entity {id: $id})
            RETURN p, r, 'role' AS kind
            """,
            id=entity_id,
        ):
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

    return sorted(events, key=sort_key, reverse=True)
