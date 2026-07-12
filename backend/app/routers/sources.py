from fastapi import APIRouter, HTTPException, Depends, Query
from app.models.source import SourceCreate, SourceResponse
from app.auth.dependencies import require_contributor
from app.database import db
import uuid

router = APIRouter(prefix="/sources", tags=["Sources"])


@router.post("/", response_model=SourceResponse)
def create_source(source: SourceCreate, _: dict = Depends(require_contributor)):
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
    """
    Return per-entry provenance for this entity: one row per source reference
    found on its ownership/role relationships (and on the entity itself),
    joined to the Source node for display metadata.

    Each row carries the specific record URL, the date the fact was recorded in
    the source, and when we last scraped it — so a reader (e.g. a journalist)
    can verify the exact record. Rows are shaped to stay backward-compatible
    with the old Source response (id/name/type/credibility_score/url) and add
    source_date + last_scraped_at.

    Provenance tuples are collected as [source_id, source_url, source_date,
    last_scraped_at] lists (not map literals) for broad ArcadeDB Cypher support.
    """
    query = """
        MATCH (e:Entity {id: $entity_id})
        OPTIONAL MATCH (a)-[r1:OWNS]->(e) WHERE r1.source_id IS NOT NULL
        OPTIONAL MATCH (e)-[r2:OWNS]->(b) WHERE r2.source_id IS NOT NULL
        OPTIONAL MATCH (c)-[r3:HAS_ROLE]->(e) WHERE r3.source_id IS NOT NULL
        WITH e,
            collect(DISTINCT [r1.source_id, r1.source_url, r1.source_date, r1.last_scraped_at]) +
            collect(DISTINCT [r2.source_id, r2.source_url, r2.source_date, r2.last_scraped_at]) +
            collect(DISTINCT [r3.source_id, r3.source_url, r3.source_date, r3.last_scraped_at]) AS rel_rows
        WITH CASE WHEN e.source_id IS NOT NULL
             THEN rel_rows + [[e.source_id, e.source_url, e.source_date, e.last_scraped_at]]
             ELSE rel_rows END AS rows
        UNWIND rows AS row
        WITH row WHERE row[0] IS NOT NULL
        MATCH (s:Source {id: row[0]})
        RETURN DISTINCT
            s.id AS id,
            s.name AS name,
            s.type AS type,
            s.credibility_score AS credibility_score,
            COALESCE(row[1], s.url) AS url,
            row[2] AS source_date,
            row[3] AS last_scraped_at
        ORDER BY credibility_score DESC
    """
    with db.get_session() as session:
        result = session.run(query, entity_id=entity_id)
        return [dict(rec) for rec in result]


@router.get("/")
def list_sources(skip: int = Query(0, ge=0, le=100_000), limit: int = Query(20, ge=1, le=100)):
    query = """
        MATCH (s:Source)
        RETURN s
        ORDER BY s.credibility_score DESC
        SKIP $skip LIMIT $limit
    """
    with db.get_session() as session:
        result = session.run(query, skip=skip, limit=limit)
        return [dict(record["s"]) for record in result]
