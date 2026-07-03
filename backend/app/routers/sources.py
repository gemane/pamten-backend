from fastapi import APIRouter, HTTPException
from app.models.source import SourceCreate, SourceResponse
from app.database import db
import uuid

router = APIRouter(prefix="/sources", tags=["Sources"])


@router.post("/", response_model=SourceResponse)
def create_source(source: SourceCreate):
    source_id = str(uuid.uuid4())

    query = """
        CREATE (s:Source {
            id: $id,
            name: $name,
            url: $url,
            credibility_score: $credibility_score,
            type: $type
        })
        RETURN s
    """

    with db.get_session() as session:
        result = session.run(query,
            id=source_id,
            **source.model_dump()
        )
        record = result.single()
        if not record:
            raise HTTPException(status_code=500, detail="Failed to create source")
        return {**dict(record["s"]), "id": source_id}


@router.get("/{source_id}", response_model=SourceResponse)
def get_source(source_id: str):
    query = """
        MATCH (s:Source {id: $id})
        RETURN s
    """
    with db.get_session() as session:
        result = session.run(query, id=source_id)
        record = result.single()
        if not record:
            raise HTTPException(status_code=404, detail="Source not found")
        return dict(record["s"])


@router.get("/entity/{entity_id}")
def get_sources_for_entity(entity_id: str):
    """Return all unique Source nodes referenced by this entity's ownership and role relationships."""
    query = """
        MATCH (e:Entity {id: $entity_id})
        OPTIONAL MATCH ()-[r1:OWNS]->(e) WHERE r1.source_id IS NOT NULL
        OPTIONAL MATCH (e)-[r2:OWNS]->() WHERE r2.source_id IS NOT NULL
        OPTIONAL MATCH ()-[r3:HAS_ROLE]->(e) WHERE r3.source_id IS NOT NULL
        WITH collect(r1.source_id) + collect(r2.source_id) + collect(r3.source_id) AS ids
        UNWIND ids AS sid
        MATCH (s:Source {id: sid})
        RETURN DISTINCT s
        ORDER BY s.credibility_score DESC
    """
    with db.get_session() as session:
        result = session.run(query, entity_id=entity_id)
        return [dict(rec["s"]) for rec in result]


@router.get("/")
def list_sources(skip: int = 0, limit: int = 20):
    query = """
        MATCH (s:Source)
        RETURN s
        ORDER BY s.credibility_score DESC
        SKIP $skip LIMIT $limit
    """
    with db.get_session() as session:
        result = session.run(query, skip=skip, limit=limit)
        return [dict(record["s"]) for record in result]
