from fastapi import APIRouter, HTTPException
from app.models.entity import EntityCreate, EntityResponse
from app.database import db
import uuid

router = APIRouter(prefix="/entities", tags=["Entities"])


@router.post("/", response_model=EntityResponse)
def create_entity(entity: EntityCreate):
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
def update_entity(entity_id: str, entity: EntityCreate):
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
def delete_entity(entity_id: str):
    query = """
        MATCH (e:Entity {id: $id})
        DETACH DELETE e
    """
    with db.get_session() as session:
        session.run(query, id=entity_id)
        return {"message": "Entity deleted"}
