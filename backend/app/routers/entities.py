from fastapi import APIRouter, HTTPException, Depends
from app.models.entity import EntityCreate, EntityResponse
from app.auth.dependencies import require_contributor
from app.database import db
import uuid

router = APIRouter(prefix="/entities", tags=["Entities"])


@router.post("/", response_model=EntityResponse)
def create_entity(entity: EntityCreate, _: dict = Depends(require_contributor)):
    entity_id = str(uuid.uuid4())

    query = """
        CREATE (e:Entity {
            id: $id,
            name: $name,
            type: $type,
            country: $country,
            founded: $founded,
            revenue: $revenue,
            description: $description,
            verified: false
        })
        RETURN e
    """

    with db.get_session() as session:
        result = session.run(query,
            id=entity_id,
            **entity.model_dump()
        )
        record = result.single()
        if not record:
            raise HTTPException(status_code=500, detail="Failed to create entity")
        return {**dict(record["e"]), "id": entity_id}


@router.get("/countries")
def list_countries():
    """Return distinct country names with entity counts, sorted by count."""
    query = """
        MATCH (e:Entity)
        WHERE e.country IS NOT NULL AND e.country <> ''
        RETURN e.country AS country, count(e) AS cnt
        ORDER BY cnt DESC
    """
    with db.get_session() as session:
        result = session.run(query)
        return [{"country": r["country"], "count": r["cnt"]} for r in result]


@router.get("/by-country")
def get_entities_by_country():
    """Return entity counts per country. Entity lists are fetched per-country on demand."""
    query = """
        MATCH (e:Entity)
        WHERE e.country IS NOT NULL AND e.country <> ''
        RETURN e.country AS country, count(e) AS cnt
        ORDER BY cnt DESC
    """
    with db.get_session() as session:
        result = session.run(query)
        return [{"country": rec["country"], "count": rec["cnt"]} for rec in result]


@router.get("/by-country/{country}")
def get_entities_for_country(country: str, limit: int = 200):
    """Return up to `limit` entities for a specific country, ordered by name."""
    query = """
        MATCH (e:Entity)
        WHERE e.country = $country
        RETURN e.id AS id, e.name AS name, e.type AS type
        ORDER BY e.name
        LIMIT $limit
    """
    with db.get_session() as session:
        result = session.run(query, country=country, limit=limit)
        return [{"id": r["id"], "name": r["name"], "type": r["type"]} for r in result]


@router.get("/{entity_id}", response_model=EntityResponse)
def get_entity(entity_id: str):
    query = """
        MATCH (e:Entity {id: $id})
        RETURN e
    """
    with db.get_session() as session:
        result = session.run(query, id=entity_id)
        record = result.single()
        if not record:
            raise HTTPException(status_code=404, detail="Entity not found")
        return dict(record["e"])


@router.get("/")
def list_entities(skip: int = 0, limit: int = 20):
    query = """
        MATCH (e:Entity)
        RETURN e
        SKIP $skip LIMIT $limit
    """
    with db.get_session() as session:
        result = session.run(query, skip=skip, limit=limit)
        return [dict(record["e"]) for record in result]


@router.put("/{entity_id}", response_model=EntityResponse)
def update_entity(entity_id: str, entity: EntityCreate, _: dict = Depends(require_contributor)):
    query = """
        MATCH (e:Entity {id: $id})
        SET e += {
            name: $name,
            type: $type,
            country: $country,
            founded: $founded,
            revenue: $revenue,
            description: $description
        }
        RETURN e
    """
    with db.get_session() as session:
        result = session.run(query, id=entity_id, **entity.model_dump())
        record = result.single()
        if not record:
            raise HTTPException(status_code=404, detail="Entity not found")
        return dict(record["e"])


@router.delete("/{entity_id}")
def delete_entity(entity_id: str, _: dict = Depends(require_contributor)):
    query = """
        MATCH (e:Entity {id: $id})
        DETACH DELETE e
    """
    with db.get_session() as session:
        session.run(query, id=entity_id)
        return {"message": "Entity deleted"}
