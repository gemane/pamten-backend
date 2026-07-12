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


# Per-entry provenance for an entity comes from the source reference on each
# relationship that touches it (and on the entity itself). We run one simple
# MATCH/RETURN per source — the same query shape as get_owners — and merge in
# Python, rather than one big Cypher with list literals / UNWIND / COALESCE,
# which ArcadeDB's Cypher engine does not support.
_PROVENANCE_QUERIES = (
    # Owners of this entity
    """
    MATCH (a)-[r:OWNS]->(e:Entity {id: $entity_id})
    WHERE r.source_id IS NOT NULL
    MATCH (s:Source {id: r.source_id})
    RETURN s.id AS id, s.name AS name, s.type AS type,
           s.credibility_score AS credibility_score, s.url AS source_home_url,
           r.source_url AS source_url, r.source_date AS source_date,
           r.last_scraped_at AS last_scraped_at
    """,
    # Things this entity owns
    """
    MATCH (e:Entity {id: $entity_id})-[r:OWNS]->(b)
    WHERE r.source_id IS NOT NULL
    MATCH (s:Source {id: r.source_id})
    RETURN s.id AS id, s.name AS name, s.type AS type,
           s.credibility_score AS credibility_score, s.url AS source_home_url,
           r.source_url AS source_url, r.source_date AS source_date,
           r.last_scraped_at AS last_scraped_at
    """,
    # Roles at this entity
    """
    MATCH (p)-[r:HAS_ROLE]->(e:Entity {id: $entity_id})
    WHERE r.source_id IS NOT NULL
    MATCH (s:Source {id: r.source_id})
    RETURN s.id AS id, s.name AS name, s.type AS type,
           s.credibility_score AS credibility_score, s.url AS source_home_url,
           r.source_url AS source_url, r.source_date AS source_date,
           r.last_scraped_at AS last_scraped_at
    """,
    # Provenance stamped directly on the entity
    """
    MATCH (e:Entity {id: $entity_id})
    WHERE e.source_id IS NOT NULL
    MATCH (s:Source {id: e.source_id})
    RETURN s.id AS id, s.name AS name, s.type AS type,
           s.credibility_score AS credibility_score, s.url AS source_home_url,
           e.source_url AS source_url, e.source_date AS source_date,
           e.last_scraped_at AS last_scraped_at
    """,
)


@router.get("/entity/{entity_id}")
def get_sources_for_entity(entity_id: str):
    """
    Return per-entry provenance for this entity: one row per source reference
    found on its ownership/role relationships (and on the entity itself),
    joined to the Source node for display metadata.

    Each row carries the specific record URL (falling back to the source's home
    URL), the date the fact was recorded in the source, and when we last scraped
    it — so a reader (e.g. a journalist) can verify the exact record. Shaped to
    stay backward-compatible with the old Source response
    (id/name/type/credibility_score/url) plus source_date + last_scraped_at.
    """
    # Read columns explicitly with rec.get(): the ArcadeDB result-record type
    # supports __getitem__/get but not dict(rec) on a whole multi-column row.
    _COLS = ("id", "name", "type", "credibility_score", "source_home_url",
             "source_url", "source_date", "last_scraped_at")
    rows: list[dict] = []
    with db.get_session() as session:
        for query in _PROVENANCE_QUERIES:
            for rec in session.run(query, entity_id=entity_id):
                rows.append({c: rec.get(c) for c in _COLS})

    # Merge + dedupe in Python: the specific record URL wins over the source home
    # URL; a source can appear once per distinct (url, source_date) pair.
    seen: set = set()
    out: list[dict] = []
    for r in rows:
        url = r.get("source_url") or r.get("source_home_url")
        key = (r.get("id"), url, r.get("source_date"))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id":                r.get("id"),
            "name":              r.get("name"),
            "type":              r.get("type"),
            "credibility_score": r.get("credibility_score"),
            "url":               url,
            "source_date":       r.get("source_date"),
            "last_scraped_at":   r.get("last_scraped_at"),
        })

    out.sort(key=lambda x: -(x["credibility_score"] or 0))
    return out


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
