"""
Scraper runner — orchestrates Wikidata fetching and Neo4j writes.

Entry point: run_scrape(query, depth)
- Checks SCRAPER_ENABLED before doing anything.
- Searches Wikidata for the query.
- Fetches entity data recursively up to `depth` levels.
- Writes entities, persons, and relationships to Neo4j using MERGE
  so repeated runs are safe (no duplicates).
"""

import uuid
from app.config import settings
from app.database import db
from app.scraper.wikidata import search_entity, fetch_company_data
from app.scraper.mapper import infer_entity_type, parse_full_name

WIKIDATA_SOURCE_NAME  = "Wikidata"
WIKIDATA_SOURCE_URL   = "https://www.wikidata.org"
WIKIDATA_CREDIBILITY  = 80
MAX_SUBSIDIARIES      = 15   # per entity, to avoid runaway scrapes
MAX_CEOS              = 3


# ── Neo4j helpers ─────────────────────────────────────────────────────────────

def _ensure_source() -> str:
    """Get or create the Wikidata source node, return its id."""
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {name: $name}) RETURN s.id AS id",
            name=WIKIDATA_SOURCE_NAME,
        ).single()
        if rec:
            return rec["id"]

        source_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (s:Source {
                id: $id, name: $name, url: $url,
                credibility_score: $score, type: 'knowledge_base'
            })
            """,
            id=source_id,
            name=WIKIDATA_SOURCE_NAME,
            url=WIKIDATA_SOURCE_URL,
            score=WIKIDATA_CREDIBILITY,
        )
        return source_id


def _upsert_entity(
    name: str,
    entity_type: str,
    country: str | None,
    founded: int | None,
    revenue: float | None,
    description: str | None,
    wikidata_id: str,
) -> str:
    """
    Find entity by wikidata_id or name, update it if found, create if not.
    Returns the entity's internal id.
    """
    with db.get_session() as session:
        rec = session.run(
            """
            MATCH (e:Entity)
            WHERE ($wid IS NOT NULL AND e.wikidata_id = $wid) OR e.name = $name
            RETURN e.id AS id LIMIT 1
            """,
            wid=wikidata_id,
            name=name,
        ).single()

        if rec:
            entity_id = rec["id"]
            session.run(
                """
                MATCH (e:Entity {id: $id})
                SET e.wikidata_id  = $wid,
                    e.type         = COALESCE($type, e.type),
                    e.country      = COALESCE($country, e.country),
                    e.founded      = COALESCE($founded, e.founded),
                    e.revenue      = COALESCE($revenue, e.revenue),
                    e.description  = COALESCE($desc, e.description)
                """,
                id=entity_id,
                wid=wikidata_id,
                type=entity_type,
                country=country,
                founded=founded,
                revenue=revenue,
                desc=description,
            )
            return entity_id

        entity_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (e:Entity {
                id: $id, name: $name, type: $type,
                country: $country, founded: $founded,
                revenue: $revenue, description: $desc,
                wikidata_id: $wid, verified: false
            })
            """,
            id=entity_id,
            name=name,
            type=entity_type,
            country=country,
            founded=founded,
            revenue=revenue,
            desc=description,
            wid=wikidata_id,
        )
        return entity_id


def _upsert_person(
    full_name: str,
    nationality: str | None,
    description: str | None,
    wikidata_id: str,
) -> str:
    first_name, last_name = parse_full_name(full_name)
    with db.get_session() as session:
        rec = session.run(
            """
            MATCH (p:Person)
            WHERE ($wid IS NOT NULL AND p.wikidata_id = $wid) OR p.full_name = $name
            RETURN p.id AS id LIMIT 1
            """,
            wid=wikidata_id,
            name=full_name,
        ).single()
        if rec:
            return rec["id"]

        person_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (p:Person {
                id: $id, first_name: $first, last_name: $last,
                full_name: $full, nationality: $nat,
                description: $desc, wikidata_id: $wid,
                verified: false, alias: [], nationalities: []
            })
            """,
            id=person_id,
            first=first_name,
            last=last_name,
            full=full_name,
            nat=nationality or "",
            desc=description or "",
            wid=wikidata_id,
        )
        return person_id


def _upsert_owns(owner_id: str, owned_id: str, source_id: str):
    """Create an active OWNS edge if one doesn't already exist."""
    with db.get_session() as session:
        exists = session.run(
            """
            MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid})
            WHERE r.until IS NULL RETURN r LIMIT 1
            """,
            oid=owner_id,
            nid=owned_id,
        ).single()
        if exists:
            return
        session.run(
            """
            MATCH (a {id: $oid}), (b {id: $nid})
            CREATE (a)-[:OWNS {
                stake_percent: null, ownership_type: 'unknown',
                since: null, until: null,
                source_id: $sid, credibility_score: $score
            }]->(b)
            """,
            oid=owner_id,
            nid=owned_id,
            sid=source_id,
            score=WIKIDATA_CREDIBILITY,
        )


def _upsert_role(person_id: str, entity_id: str, role: str, source_id: str):
    """Create a HAS_ROLE edge if one doesn't already exist."""
    with db.get_session() as session:
        exists = session.run(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
            WHERE r.role = $role AND r.until IS NULL RETURN r LIMIT 1
            """,
            pid=person_id,
            eid=entity_id,
            role=role,
        ).single()
        if exists:
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: null, until: null,
                source_id: $sid, credibility_score: $score
            }]->(e)
            """,
            pid=person_id,
            eid=entity_id,
            role=role,
            sid=source_id,
            score=WIKIDATA_CREDIBILITY,
        )


# ── Recursive scrape ──────────────────────────────────────────────────────────

def _scrape_node(
    qid: str,
    depth: int,
    visited: set,
    scraped: list,
    source_id: str,
    parent_entity_id: str | None = None,
):
    if qid in visited:
        return
    visited.add(qid)

    data = fetch_company_data(qid)
    if not data or not data.get("name"):
        return

    entity_type = infer_entity_type(data["instances"])
    entity_id = _upsert_entity(
        name=data["name"],
        entity_type=entity_type,
        country=data.get("country"),
        founded=data.get("founded"),
        revenue=data.get("revenue"),
        description=data.get("description"),
        wikidata_id=qid,
    )
    scraped.append({
        "qid":  qid,
        "id":   entity_id,
        "name": data["name"],
        "type": entity_type,
    })

    # Wire up to parent if this node was reached via a subsidiary edge
    if parent_entity_id:
        _upsert_owns(parent_entity_id, entity_id, source_id)

    # Subsidiaries
    for sub in data.get("subsidiaries", [])[:MAX_SUBSIDIARIES]:
        sub_name = sub.get("name") or sub["qid"]
        sub_type = infer_entity_type(list(sub.get("instances", set())))
        sub_id = _upsert_entity(
            name=sub_name,
            entity_type=sub_type,
            country=None,
            founded=None,
            revenue=None,
            description=None,
            wikidata_id=sub["qid"],
        )
        _upsert_owns(entity_id, sub_id, source_id)
        scraped.append({
            "qid":  sub["qid"],
            "id":   sub_id,
            "name": sub_name,
            "type": sub_type,
        })

        if depth > 1:
            _scrape_node(sub["qid"], depth - 1, visited, scraped, source_id,
                         parent_entity_id=entity_id)

    # CEOs
    for ceo in data.get("ceos", [])[:MAX_CEOS]:
        if not ceo.get("label"):
            continue
        person_id = _upsert_person(
            full_name=ceo["label"],
            nationality=ceo.get("nationality"),
            description=ceo.get("description"),
            wikidata_id=ceo["qid"],
        )
        _upsert_role(person_id, entity_id, "CEO", source_id)


# ── Public entry point ────────────────────────────────────────────────────────

def run_scrape(query: str, depth: int = 2) -> dict:
    """
    Trigger a scrape for a company name.
    Raises PermissionError if SCRAPER_ENABLED is not true.
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in .env to enable."
        )

    depth = max(0, min(int(depth), 3))  # hard cap at 3 levels

    results = search_entity(query, limit=3)
    if not results:
        return {"status": "no_results", "query": query, "total": 0, "scraped": []}

    top = results[0]
    qid = top["id"]

    source_id = _ensure_source()
    scraped: list = []
    visited: set  = set()

    _scrape_node(qid, depth, visited, scraped, source_id)

    return {
        "status":      "ok",
        "query":       query,
        "wikidata_id": qid,
        "total":       len(scraped),
        "scraped":     scraped,
    }
