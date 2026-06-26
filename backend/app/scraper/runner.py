"""
Scraper runner — orchestrates Wikidata and SEC EDGAR fetching and ArcadeDB writes.

Entry points:
  run_scrape(query, depth)          – Wikidata scrape
  run_scrape_sec_edgar(company)     – SEC EDGAR scrape
  run_scrape_all(query, depth)      – both scrapers in sequence

All entry points:
- Check SCRAPER_ENABLED before doing anything.
- Check the per-source flag before running that source.
- Write using MERGE so repeated runs are safe (no duplicates).
"""

import uuid
import logging
from app.config import settings
from app.database import db
from app.scraper.wikidata import search_entity, fetch_company_data
from app.scraper.mapper import infer_entity_type, parse_full_name, is_person_name, normalize_entity_name
from app.scraper.sources import get_source_enabled

log = logging.getLogger(__name__)

WIKIDATA_SOURCE_NAME  = "Wikidata"
WIKIDATA_SOURCE_URL   = "https://www.wikidata.org"
WIKIDATA_CREDIBILITY  = 80
MAX_SUBSIDIARIES      = 15   # per entity, to avoid runaway scrapes
MAX_CEOS              = 3

SEC_EDGAR_SOURCE_NAME = "SEC EDGAR"
SEC_EDGAR_SOURCE_URL  = "https://www.sec.gov/edgar"
SEC_EDGAR_CREDIBILITY = 98   # legally mandated filings

OPENCORPORATES_SOURCE_NAME = "OpenCorporates"
OPENCORPORATES_SOURCE_URL  = "https://opencorporates.com"
OPENCORPORATES_CREDIBILITY = 85


# ── Database helpers ──────────────────────────────────────────────────────────

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
    name_norm = normalize_entity_name(name)
    with db.get_session() as session:
        rec = session.run(
            """
            MATCH (e:Entity)
            WHERE ($wid IS NOT NULL AND e.wikidata_id = $wid)
               OR e.name = $name
               OR e.name_normalized = $name_norm
            RETURN e.id AS id LIMIT 1
            """,
            wid=wikidata_id,
            name=name,
            name_norm=name_norm,
        ).single()

        if rec:
            entity_id = rec["id"]
            session.run(
                """
                MATCH (e:Entity {id: $id})
                SET e.wikidata_id     = $wid,
                    e.type            = COALESCE($type, e.type),
                    e.country         = COALESCE($country, e.country),
                    e.founded         = COALESCE($founded, e.founded),
                    e.revenue         = COALESCE($revenue, e.revenue),
                    e.description     = COALESCE($desc, e.description),
                    e.name_normalized = $name_norm,
                    e.name            = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $name ELSE e.name END,
                    e.name_credibility = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $cred ELSE e.name_credibility END
                """,
                id=entity_id,
                name=name,
                wid=wikidata_id,
                type=entity_type,
                country=country,
                founded=founded,
                revenue=revenue,
                desc=description,
                name_norm=name_norm,
                cred=WIKIDATA_CREDIBILITY,
            )
            return entity_id

        entity_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (e:Entity {
                id: $id, name: $name, name_normalized: $name_norm,
                name_credibility: $cred,
                type: $type, country: $country, founded: $founded,
                revenue: $revenue, description: $desc,
                wikidata_id: $wid, verified: false
            })
            """,
            id=entity_id,
            name=name,
            name_norm=name_norm,
            cred=WIKIDATA_CREDIBILITY,
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
                stake_percent: null, ownership_type: null,
                since: null, until: null,
                source_id: $sid, credibility_score: $score
            }]->(b)
            """,
            oid=owner_id,
            nid=owned_id,
            sid=source_id,
            score=WIKIDATA_CREDIBILITY,
        )


def _upsert_role(person_id: str, entity_id: str, role: str, source_id: str,
                 since: str | None = None, until: str | None = None):
    """Create a HAS_ROLE edge if one doesn't already exist (matched on role+since)."""
    with db.get_session() as session:
        exists = session.run(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
            WHERE r.role = $role
              AND (r.since = $since OR (r.since IS NULL AND $since IS NULL))
            RETURN r LIMIT 1
            """,
            pid=person_id,
            eid=entity_id,
            role=role,
            since=since,
        ).single()
        if exists:
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: $since, until: $until,
                source_id: $sid, credibility_score: $score
            }]->(e)
            """,
            pid=person_id,
            eid=entity_id,
            role=role,
            since=since,
            until=until,
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
        if depth > 1:
            _scrape_node(sub["qid"], depth - 1, visited, scraped, source_id,
                         parent_entity_id=entity_id)
        elif sub["qid"] not in {s["qid"] for s in scraped}:
            scraped.append({
                "qid":  sub["qid"],
                "id":   sub_id,
                "name": sub_name,
                "type": sub_type,
            })

    # CEOs — sort current first (no until), then most recent since, before capping
    sorted_ceos = sorted(
        data.get("ceos", []),
        key=lambda c: (1 if c.get("until") else 0, c.get("since") or "0000"),
        reverse=True,
    )
    for ceo in sorted_ceos[:MAX_CEOS]:
        if not ceo.get("label"):
            continue
        person_id = _upsert_person(
            full_name=ceo["label"],
            nationality=ceo.get("nationality"),
            description=ceo.get("description"),
            wikidata_id=ceo["qid"],
        )
        _upsert_role(person_id, entity_id, "CEO", source_id,
                     since=ceo.get("since"), until=ceo.get("until"))


# ── Wikidata public entry point ───────────────────────────────────────────────

def run_scrape(query: str, depth: int = 2) -> dict:
    """
    Trigger a Wikidata scrape for a company name.
    Raises PermissionError if SCRAPER_ENABLED is not true.
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )

    if not get_source_enabled("wikidata"):
        raise PermissionError("Wikidata source is disabled. Enable it in the Scraper panel.")

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


# ── SEC EDGAR helpers ─────────────────────────────────────────────────────────

def _ensure_sec_edgar_source() -> str:
    """Get or create the SEC EDGAR source node, return its id."""
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {name: $name}) RETURN s.id AS id",
            name=SEC_EDGAR_SOURCE_NAME,
        ).single()
        if rec:
            return rec["id"]

        source_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (s:Source {
                id: $id, name: $name, url: $url,
                credibility_score: $score, type: 'register'
            })
            """,
            id=source_id,
            name=SEC_EDGAR_SOURCE_NAME,
            url=SEC_EDGAR_SOURCE_URL,
            score=SEC_EDGAR_CREDIBILITY,
        )
        return source_id


def _upsert_entity_by_name(name: str, entity_type: str = "company",
                            cik: str | None = None) -> str:
    """Find or create an Entity node matched by CIK, exact name, or normalized name."""
    name_norm = normalize_entity_name(name)
    with db.get_session() as session:
        rec = session.run(
            """
            MATCH (e:Entity)
            WHERE ($cik IS NOT NULL AND e.sec_cik = $cik)
               OR e.name = $name
               OR e.name_normalized = $name_norm
            RETURN e.id AS id LIMIT 1
            """,
            cik=cik,
            name=name,
            name_norm=name_norm,
        ).single()

        if rec:
            entity_id = rec["id"]
            session.run(
                """
                MATCH (e:Entity {id: $id})
                SET e.name_normalized  = $name_norm,
                    e.sec_cik          = COALESCE($cik, e.sec_cik),
                    e.name             = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $name ELSE e.name END,
                    e.name_credibility = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $cred ELSE e.name_credibility END
                """,
                id=entity_id, name_norm=name_norm, cik=cik, name=name,
                cred=SEC_EDGAR_CREDIBILITY,
            )
            return entity_id

        entity_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (e:Entity {
                id: $id, name: $name, name_normalized: $name_norm,
                name_credibility: $cred,
                type: $type, sec_cik: $cik, verified: false,
                country: null, founded: null, revenue: null,
                description: null, wikidata_id: null
            })
            """,
            id=entity_id, name=name, name_norm=name_norm,
            cred=SEC_EDGAR_CREDIBILITY, type=entity_type, cik=cik,
        )
        return entity_id


def _upsert_person_by_name(full_name: str) -> str:
    """Find or create a Person node matched by full_name."""
    first_name, last_name = parse_full_name(full_name)
    with db.get_session() as session:
        rec = session.run(
            "MATCH (p:Person {full_name: $name}) RETURN p.id AS id LIMIT 1",
            name=full_name,
        ).single()
        if rec:
            return rec["id"]

        person_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (p:Person {
                id: $id, first_name: $first, last_name: $last,
                full_name: $full, nationality: '', description: '',
                wikidata_id: null, verified: false,
                alias: [], nationalities: []
            })
            """,
            id=person_id, first=first_name, last=last_name, full=full_name,
        )
        return person_id


def _upsert_owns_sec(owner_id: str, owned_id: str, source_id: str,
                     ownership_type: str, file_date: str | None,
                     stake_percent: float | None):
    """Create or update an OWNS edge with SEC EDGAR attribution."""
    with db.get_session() as session:
        existing = session.run(
            """
            MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid})
            WHERE r.source_id = $sid AND r.until IS NULL
            RETURN r LIMIT 1
            """,
            oid=owner_id, nid=owned_id, sid=source_id,
        ).single()
        if existing:
            return
        session.run(
            """
            MATCH (a {id: $oid}), (b {id: $nid})
            CREATE (a)-[:OWNS {
                stake_percent:    $stake,
                ownership_type:   $otype,
                since:            $since,
                until:            null,
                source_id:        $sid,
                credibility_score: $score
            }]->(b)
            """,
            oid=owner_id, nid=owned_id,
            stake=stake_percent, otype=ownership_type,
            since=file_date, sid=source_id, score=SEC_EDGAR_CREDIBILITY,
        )


def _upsert_role_sec(person_id: str, entity_id: str, role: str,
                     source_id: str):
    """Create a HAS_ROLE edge attributed to SEC EDGAR if not already present."""
    with db.get_session() as session:
        existing = session.run(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
            WHERE r.role = $role AND r.until IS NULL
            RETURN r LIMIT 1
            """,
            pid=person_id, eid=entity_id, role=role,
        ).single()
        if existing:
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: null, until: null,
                source_id: $sid, credibility_score: $score
            }]->(e)
            """,
            pid=person_id, eid=entity_id, role=role,
            sid=source_id, score=SEC_EDGAR_CREDIBILITY,
        )


# ── SEC EDGAR public entry point ──────────────────────────────────────────────

def run_scrape_sec_edgar(company_name: str) -> dict:
    """
    Scrape SEC EDGAR for ownership and executive data about one company.
    Requires SCRAPER_ENABLED=true AND SCRAPER_SEC_EDGAR_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )
    if not settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError(
            "SEC EDGAR scraper is disabled. "
            "Set SCRAPER_SEC_EDGAR_ENABLED=true in the environment to enable."
        )
    if not get_source_enabled("sec_edgar"):
        raise PermissionError(
            "SEC EDGAR source is disabled. Enable it in the Scraper panel."
        )

    # Import here to avoid circular imports and to keep the cold-start fast
    from app.scraper.sec_edgar import scrape_company

    log.info("SEC EDGAR runner: starting scrape for %r", company_name)
    data = scrape_company(company_name)

    if not data:
        return {
            "status":  "no_results",
            "company": company_name,
            "total":   0,
            "scraped": [],
        }

    source_id = _ensure_sec_edgar_source()
    scraped: list[dict] = []

    # Upsert the target company
    target_id = _upsert_entity_by_name(
        name=data["name"],
        entity_type="company",
        cik=data.get("cik"),
    )
    scraped.append({"type": "entity", "name": data["name"], "role": "target"})

    # Ownership filings → investor nodes + OWNS edges
    for filing in data.get("ownership_filings", []):
        investor_name = filing.get("investor_name", "").strip()
        if not investor_name:
            continue

        if is_person_name(investor_name):
            investor_node_id = _upsert_person_by_name(investor_name)
            scraped.append({"type": "person", "name": investor_name, "role": "investor"})
        else:
            investor_node_id = _upsert_entity_by_name(
                name=investor_name,
                entity_type="company",
                cik=filing.get("investor_cik"),
            )
            scraped.append({"type": "entity", "name": investor_name, "role": "investor"})

        _upsert_owns_sec(
            owner_id=investor_node_id,
            owned_id=target_id,
            source_id=source_id,
            ownership_type=filing.get("ownership_type", "unknown"),
            file_date=filing.get("file_date"),
            stake_percent=filing.get("stake_percent"),
        )
        log.info(
            "SEC EDGAR: wrote OWNS %r → %r (%s)",
            investor_name, data["name"], filing.get("form_type"),
        )

    # Executives → Person nodes + HAS_ROLE edges
    for exec_rec in data.get("executives", []):
        name = exec_rec.get("name", "").strip()
        role = exec_rec.get("role", "Executive")
        if not name:
            continue

        person_id = _upsert_person_by_name(name)
        _upsert_role_sec(person_id, target_id, role, source_id)
        scraped.append({"type": "person", "name": name, "role": role})
        log.info("SEC EDGAR: wrote HAS_ROLE %r → %r (%s)", name, data["name"], role)

    log.info(
        "SEC EDGAR runner: finished %r — %d nodes written",
        company_name, len(scraped),
    )
    return {
        "status":  "ok",
        "company": company_name,
        "cik":     data.get("cik"),
        "total":   len(scraped),
        "scraped": scraped,
    }


# ── Run-all entry point ───────────────────────────────────────────────────────

def run_scrape_all(query: str, depth: int = 2) -> dict:
    """
    Run all enabled scrapers for a given company name.
    Each scraper that is disabled is skipped silently; its key in the result
    will have status 'disabled'.
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )

    results: dict[str, dict] = {}

    # Wikidata
    if get_source_enabled("wikidata"):
        try:
            results["wikidata"] = run_scrape(query, depth)
        except PermissionError as exc:
            results["wikidata"] = {"status": "disabled", "detail": str(exc)}
        except Exception as exc:
            log.error("Wikidata scrape failed for %r: %s", query, exc)
            results["wikidata"] = {"status": "error", "detail": str(exc)}
    else:
        results["wikidata"] = {"status": "disabled"}

    # SEC EDGAR
    if settings.SCRAPER_SEC_EDGAR_ENABLED and get_source_enabled("sec_edgar"):
        try:
            results["sec_edgar"] = run_scrape_sec_edgar(query)
        except PermissionError as exc:
            results["sec_edgar"] = {"status": "disabled", "detail": str(exc)}
        except Exception as exc:
            log.error("SEC EDGAR scrape failed for %r: %s", query, exc)
            results["sec_edgar"] = {"status": "error", "detail": str(exc)}
    else:
        results["sec_edgar"] = {"status": "disabled"}

    # OpenCorporates
    if settings.SCRAPER_OPENCORPORATES_ENABLED and get_source_enabled("open_corporates"):
        try:
            results["open_corporates"] = run_scrape_open_corporates(query)
        except PermissionError as exc:
            results["open_corporates"] = {"status": "disabled", "detail": str(exc)}
        except Exception as exc:
            log.error("OpenCorporates scrape failed for %r: %s", query, exc)
            results["open_corporates"] = {"status": "error", "detail": str(exc)}
    else:
        results["open_corporates"] = {"status": "disabled"}

    return {"status": "ok", "query": query, "results": results}


# ── OpenCorporates helpers ────────────────────────────────────────────────────

def _ensure_open_corporates_source() -> str:
    """Get or create the OpenCorporates source node, return its id."""
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {name: $name}) RETURN s.id AS id",
            name=OPENCORPORATES_SOURCE_NAME,
        ).single()
        if rec:
            return rec["id"]

        source_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (s:Source {
                id: $id, name: $name, url: $url,
                credibility_score: $score, type: 'register'
            })
            """,
            id=source_id,
            name=OPENCORPORATES_SOURCE_NAME,
            url=OPENCORPORATES_SOURCE_URL,
            score=OPENCORPORATES_CREDIBILITY,
        )
        return source_id


def _upsert_location_oc(address: dict) -> str | None:
    """
    Find or create a Location node from a registered address dict.
    Returns the Location's id, or None if the address is empty.
    """
    city    = (address.get("city")    or "").strip()
    country = (address.get("country") or "").strip()
    street  = (address.get("street")  or "").strip()
    zip_    = (address.get("zip")     or "").strip()

    if not (city or country):
        return None

    with db.get_session() as session:
        rec = session.run(
            """
            MATCH (l:Location)
            WHERE l.city = $city AND l.country = $country
              AND COALESCE(l.street, '') = $street
            RETURN l.id AS id LIMIT 1
            """,
            city=city, country=country, street=street,
        ).single()
        if rec:
            return rec["id"]

        location_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (l:Location {
                id: $id, street: $street, city: $city,
                country: $country, zip: $zip
            })
            """,
            id=location_id, street=street, city=city,
            country=country, zip=zip_,
        )
        return location_id


def _upsert_role_oc(person_id: str, entity_id: str, role: str,
                    start_date: str | None, end_date: str | None,
                    source_id: str):
    """Create a HAS_ROLE edge attributed to OpenCorporates if not already present."""
    with db.get_session() as session:
        existing = session.run(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
            WHERE r.role = $role AND r.until IS NULL
            RETURN r LIMIT 1
            """,
            pid=person_id, eid=entity_id, role=role,
        ).single()
        if existing:
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: $since, until: $until,
                source_id: $sid, credibility_score: $score
            }]->(e)
            """,
            pid=person_id, eid=entity_id, role=role,
            since=start_date, until=end_date,
            sid=source_id, score=OPENCORPORATES_CREDIBILITY,
        )


# ── OpenCorporates public entry point ─────────────────────────────────────────

def run_scrape_open_corporates(company_name: str) -> dict:
    """
    Scrape OpenCorporates for registration details and officers for one company.
    Requires SCRAPER_ENABLED=true AND SCRAPER_OPENCORPORATES_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )
    if not settings.SCRAPER_OPENCORPORATES_ENABLED:
        raise PermissionError(
            "OpenCorporates scraper is disabled. "
            "Set SCRAPER_OPENCORPORATES_ENABLED=true in the environment to enable."
        )
    if not get_source_enabled("open_corporates"):
        raise PermissionError(
            "OpenCorporates source is disabled. Enable it in the Scraper panel."
        )

    from app.scraper.open_corporates import scrape_company

    log.info("OpenCorporates runner: starting scrape for %r", company_name)
    data = scrape_company(company_name)

    if not data:
        return {
            "status":  "no_results",
            "company": company_name,
            "total":   0,
            "scraped": [],
        }

    source_id = _ensure_open_corporates_source()
    scraped: list[dict] = []

    # Upsert the target company
    target_id = _upsert_entity_by_name(
        name=data["name"],
        entity_type="company",
    )
    scraped.append({"type": "entity", "name": data["name"], "role": "target"})

    # Registered address → Location node linked with REGISTERED_IN
    address = data.get("registered_address") or {}
    location_id = _upsert_location_oc(address)
    if location_id:
        with db.get_session() as session:
            session.run(
                """
                MATCH (e:Entity {id: $eid}), (l:Location {id: $lid})
                MERGE (e)-[:REGISTERED_IN {source_id: $sid}]->(l)
                """,
                eid=target_id, lid=location_id, sid=source_id,
            )
        city    = address.get("city", "")
        country = address.get("country", "")
        scraped.append({"type": "location", "city": city, "country": country,
                        "role": "registered_address"})

    # Officers → Person or Entity nodes + HAS_ROLE edges
    for officer in data.get("officers", []):
        name = officer.get("name", "").strip()
        role = officer.get("role", "Officer")
        if not name:
            continue

        if is_person_name(name):
            person_id = _upsert_person_by_name(name)
            _upsert_role_oc(
                person_id, target_id, role,
                officer.get("start_date"), officer.get("end_date"),
                source_id,
            )
            scraped.append({"type": "person", "name": name, "role": role})
        else:
            entity_id = _upsert_entity_by_name(name=name, entity_type="company")
            scraped.append({"type": "entity", "name": name, "role": role})

        log.info("OpenCorporates: wrote %r → %r (%s)", name, data["name"], role)

    log.info(
        "OpenCorporates runner: finished %r — %d nodes written",
        company_name, len(scraped),
    )
    return {
        "status":             "ok",
        "company":            company_name,
        "jurisdiction_code":  data.get("jurisdiction_code"),
        "company_number":     data.get("company_number"),
        "total":              len(scraped),
        "scraped":            scraped,
    }
