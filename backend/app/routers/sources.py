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


# Per-entry provenance for an entity is where *its own* information came from:
# who owns it, its executives, and the entity record itself. We deliberately do
# NOT include the entity's outbound ownership edges (its subsidiaries) — those
# would add one source row per subsidiary (each a distinct record URL), which
# floods the panel; a subsidiary's own source is shown when you select it.
#
# We run one simple MATCH/RETURN per source — the same query shape as get_owners
# — and merge in Python, rather than one big Cypher with list literals / UNWIND
# / COALESCE, which ArcadeDB's Cypher engine does not support.
_PROVENANCE_QUERIES = (
    # Owners of this entity. Anchor on the indexed Entity and follow the edge
    # *inward* — writing it as (a)-[:OWNS]->(e {id}) makes ArcadeDB scan every
    # node for `a` instead of resolving `e` by index (36s on a full-GLEIF DB).
    """
    MATCH (e:Entity {id: $entity_id})<-[r:OWNS]-(a)
    WHERE r.source_id IS NOT NULL
    MATCH (s:Source {id: r.source_id})
    RETURN s.id AS id, s.name AS name, s.type AS type,
           s.credibility_score AS credibility_score, s.url AS source_home_url,
           r.source_url AS source_url, r.source_date AS source_date,
           r.last_scraped_at AS last_scraped_at
    """,
    # Roles at this entity (same anchoring fix)
    """
    MATCH (e:Entity {id: $entity_id})<-[r:HAS_ROLE]-(p)
    WHERE r.source_id IS NOT NULL
    MATCH (s:Source {id: r.source_id})
    RETURN s.id AS id, s.name AS name, s.type AS type,
           s.credibility_score AS credibility_score, s.url AS source_home_url,
           r.source_url AS source_url, r.source_date AS source_date,
           r.last_scraped_at AS last_scraped_at
    """,
    # NOTE: the entity's OWN record provenance is derived from its hard identifiers
    # (_entity_own_source_rows), not the single `source_id` field — so a node carrying
    # several ids (e.g. a Wikidata QID *and* an SEC CIK after a cross-source merge) shows
    # ALL its sources, each deep-linked to the specific record page.
)

# Each hard identifier an entity carries → the source that assigns it + a deep link to the
# specific record (not the source's home page). Order = display priority within a source.
_ID_SOURCES = (
    ("wikidata_id",        "Wikidata",  "https://www.wikidata.org/wiki/{}"),
    ("sec_cik",            "SEC EDGAR", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={}"),
    ("lei_id",             "GLEIF",     "https://search.gleif.org/#/record/{}"),
    ("companies_house_id", "UK PSC",    "https://find-and-update.company-information.service.gov.uk/company/{}"),
)


def _entity_own_source_rows(session, entity_id: str) -> list[dict]:
    """The entity record's OWN provenance: one deep-linked row per hard identifier it
    carries (Wikidata QID / SEC CIK / LEI / Companies House number), joined to that
    source. A cross-source entity (both a QID and a CIK, e.g. after a merge) thus shows
    BOTH sources. Falls back to the single stamped `source_id` when the node has no such
    identifier (or its source has no Source node)."""
    rec = session.run(
        "MATCH (e:Entity {id: $id}) RETURN e.wikidata_id AS wikidata_id, e.sec_cik AS sec_cik, "
        "e.lei_id AS lei_id, e.companies_house_id AS companies_house_id, e.source_id AS source_id, "
        "e.source_url AS source_url, e.source_date AS source_date, e.last_scraped_at AS last_scraped_at",
        id=entity_id).single()
    if not rec:
        return []
    date, lsa = rec.get("source_date"), rec.get("last_scraped_at")

    rows: list[dict] = []
    for field, sname, tmpl in _ID_SOURCES:
        val = rec.get(field)
        if not val:
            continue
        s = session.run(
            "MATCH (s:Source {name: $n}) RETURN s.id AS id, s.type AS type, "
            "s.credibility_score AS cred, s.url AS home", n=sname).single()
        if not s:
            continue
        rows.append({"id": s.get("id"), "name": sname, "type": s.get("type"),
                     "credibility_score": s.get("cred"), "source_home_url": s.get("home"),
                     "source_url": tmpl.format(val), "source_date": date, "last_scraped_at": lsa})

    if rows:
        return rows
    # Fallback: the single stamped source_id (no hard identifier to deep-link from).
    if rec.get("source_id"):
        s = session.run(
            "MATCH (s:Source {id: $id}) RETURN s.id AS id, s.name AS name, s.type AS type, "
            "s.credibility_score AS cred, s.url AS home", id=rec.get("source_id")).single()
        if s:
            rows.append({"id": s.get("id"), "name": s.get("name"), "type": s.get("type"),
                         "credibility_score": s.get("cred"), "source_home_url": s.get("home"),
                         "source_url": rec.get("source_url"), "source_date": date, "last_scraped_at": lsa})
    return rows


@router.get("/entity/{entity_id}")
def get_sources_for_entity(entity_id: str):
    """
    Return per-entry provenance for this entity: one row per source reference
    behind who owns it, its executive roles, and the entity record itself
    (NOT its subsidiaries — see the query note above), joined to the Source node
    for display metadata.

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
        for query in _PROVENANCE_QUERIES:            # owners + roles (edge provenance)
            for rec in session.run(query, entity_id=entity_id):
                rows.append({c: rec.get(c) for c in _COLS})
        rows.extend(_entity_own_source_rows(session, entity_id))   # the record's own sources

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


# Per-entry provenance for a person: where their information came from — the
# entities they own (OWNS), the roles they hold (HAS_ROLE), and the person
# record itself.
_PERSON_PROVENANCE_QUERIES = (
    # Entities this person owns
    """
    MATCH (p:Person {id: $person_id})-[r:OWNS]->(x)
    WHERE r.source_id IS NOT NULL
    MATCH (s:Source {id: r.source_id})
    RETURN s.id AS id, s.name AS name, s.type AS type,
           s.credibility_score AS credibility_score, s.url AS source_home_url,
           r.source_url AS source_url, r.source_date AS source_date,
           r.last_scraped_at AS last_scraped_at
    """,
    # Roles this person holds
    """
    MATCH (p:Person {id: $person_id})-[r:HAS_ROLE]->(x)
    WHERE r.source_id IS NOT NULL
    MATCH (s:Source {id: r.source_id})
    RETURN s.id AS id, s.name AS name, s.type AS type,
           s.credibility_score AS credibility_score, s.url AS source_home_url,
           r.source_url AS source_url, r.source_date AS source_date,
           r.last_scraped_at AS last_scraped_at
    """,
    # Provenance stamped directly on the person record
    """
    MATCH (p:Person {id: $person_id})
    WHERE p.source_id IS NOT NULL
    MATCH (s:Source {id: p.source_id})
    RETURN s.id AS id, s.name AS name, s.type AS type,
           s.credibility_score AS credibility_score, s.url AS source_home_url,
           p.source_url AS source_url, p.source_date AS source_date,
           p.last_scraped_at AS last_scraped_at
    """,
)


@router.get("/person/{person_id}")
def get_sources_for_person(person_id: str):
    """
    Return per-entry provenance for this person: one source row per ownership /
    role fact and the person record itself. Same shape as /sources/entity.
    """
    _COLS = ("id", "name", "type", "credibility_score", "source_home_url",
             "source_url", "source_date", "last_scraped_at")
    rows: list[dict] = []
    with db.get_session() as session:
        for query in _PERSON_PROVENANCE_QUERIES:
            for rec in session.run(query, person_id=person_id):
                rows.append({c: rec.get(c) for c in _COLS})

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
