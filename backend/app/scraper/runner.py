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
from datetime import datetime, timezone
from app.config import settings
from app.database import db
from app.entity_resolution import resolve_entity_id
from app.scraper.wikidata import search_entity, fetch_company_data
from app.scraper.mapper import infer_entity_type, parse_full_name, is_person_name, normalize_entity_name, derive_ownership_type
from app.scraper.sources import get_source_enabled
from app.scraper.geocode import geocode_address


def _now_iso() -> str:
    """UTC timestamp for last_scraped_at provenance."""
    return datetime.now(timezone.utc).isoformat()


def _wikidata_url(qid: str | None) -> str | None:
    """Verifiable per-record URL for a Wikidata entity (QID page)."""
    return f"https://www.wikidata.org/wiki/{qid}" if qid else None


def _opencorporates_url(jurisdiction_code: str | None, company_number: str | None) -> str | None:
    """Verifiable per-record URL for an OpenCorporates company page."""
    if not jurisdiction_code or not company_number:
        return None
    return f"https://opencorporates.com/companies/{jurisdiction_code}/{company_number}"

log = logging.getLogger(__name__)


def _geocode_and_attach(entity_id: str, location_id: str, address: dict) -> None:
    """
    Best-effort: geocode an address, persist lat/lng on the Location node, and
    denormalize a primary location (coords + city/country) onto the Entity so
    the map can place a pin without traversing edges. Keeps any values already
    present (COALESCE(existing, new)) so richer data is never clobbered.
    """
    coord = geocode_address(address)
    lat, lng = coord if coord else (None, None)
    with db.get_session() as session:
        if coord:
            session.run(
                "MATCH (l:Location {id: $id}) SET l.latitude = $lat, l.longitude = $lng",
                id=location_id, lat=lat, lng=lng,
            )
        session.run(
            """
            MATCH (e:Entity {id: $id})
            SET e.hq_city    = COALESCE(e.hq_city, $city),
                e.hq_country = COALESCE(e.hq_country, $country),
                e.hq_lat     = COALESCE(e.hq_lat, $lat),
                e.hq_lng     = COALESCE(e.hq_lng, $lng)
            """,
            id=entity_id,
            city=address.get("city") or None,
            country=address.get("country") or None,
            lat=lat, lng=lng,
        )

WIKIDATA_SOURCE_NAME  = "Wikidata"
WIKIDATA_SOURCE_URL   = "https://www.wikidata.org"
WIKIDATA_CREDIBILITY  = 80
MAX_SUBSIDIARIES      = 15   # per entity, to avoid runaway scrapes
MAX_CEOS              = 3
MAX_OFFICERS          = 12   # founders + chairpersons + board members combined
MAX_OWNERS            = 10   # owned-by (P127) links
MAX_INSIDER_LOOKUPS   = 15   # known people to look up personal Form-4 holdings for

SEC_EDGAR_SOURCE_NAME = "SEC EDGAR"
SEC_EDGAR_SOURCE_URL  = "https://www.sec.gov/edgar"
SEC_EDGAR_CREDIBILITY = 98   # legally mandated filings

OPENCORPORATES_SOURCE_NAME = "OpenCorporates"
OPENCORPORATES_SOURCE_URL  = "https://opencorporates.com"
OPENCORPORATES_CREDIBILITY = 85

GLEIF_SOURCE_NAME        = "GLEIF"
GLEIF_SOURCE_URL         = "https://www.gleif.org"
GLEIF_BODS_URL           = "https://oo-bodsdata.s3.amazonaws.com/data/gleif_version_0_4/json.zip"
BODS_GLEIF_CREDIBILITY   = 92   # authoritative LEI data, CC0 — corporate not beneficial ownership

UK_PSC_SOURCE_NAME       = "UK PSC"
UK_PSC_SOURCE_URL        = "https://www.gov.uk/government/publications/persons-with-significant-control-register"
UK_PSC_BODS_URL          = "https://oo-bodsdata.s3.amazonaws.com/data/uk_version_0_4/json.zip"
BODS_UK_PSC_CREDIBILITY  = 97   # statutory UK legal register, CC0


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
    hq_lat: float | None = None,
    hq_lng: float | None = None,
    hq_city: str | None = None,
    hq_country: str | None = None,
    aliases: list[str] | None = None,
    countries: list[str] | None = None,      # all domiciles (dual-listed → >1)
    hq_locations: list[str] | None = None,   # all HQs as "City|CC" strings
) -> str:
    """
    Find entity by wikidata_id or name, update it if found, create if not.
    Returns the entity's internal id.
    """
    name_norm = normalize_entity_name(name)
    with db.get_session() as session:
        # Sequential indexed lookups — an OR across these fields full-scans the
        # Entity type on ArcadeDB (see app.entity_resolution).
        entity_id = resolve_entity_id(
            session, wikidata_id=wikidata_id, name=name, name_normalized=name_norm,
        )

        if entity_id:
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
                    e.aliases         = CASE WHEN size($aliases) > 0 THEN $aliases ELSE COALESCE(e.aliases, []) END,
                    e.countries       = CASE WHEN size($countries) > 0 THEN $countries ELSE COALESCE(e.countries, []) END,
                    e.hq_locations    = CASE WHEN size($hq_locations) > 0 THEN $hq_locations ELSE COALESCE(e.hq_locations, []) END,
                    e.hq_lat          = COALESCE(e.hq_lat, $hq_lat),
                    e.hq_lng          = COALESCE(e.hq_lng, $hq_lng),
                    e.hq_city         = COALESCE(e.hq_city, $hq_city),
                    e.hq_country      = COALESCE(e.hq_country, $hq_country),
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
                aliases=aliases or [],
                countries=countries or [], hq_locations=hq_locations or [],
                hq_lat=hq_lat, hq_lng=hq_lng, hq_city=hq_city, hq_country=hq_country,
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
                wikidata_id: $wid, verified: false,
                aliases: $aliases, countries: $countries, hq_locations: $hq_locations,
                hq_lat: $hq_lat, hq_lng: $hq_lng,
                hq_city: $hq_city, hq_country: $hq_country
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
            aliases=aliases or [],
            countries=countries or [], hq_locations=hq_locations or [],
            hq_lat=hq_lat, hq_lng=hq_lng, hq_city=hq_city, hq_country=hq_country,
        )
        return entity_id


def _upsert_person(
    full_name: str,
    nationality: str | None,
    description: str | None,
    wikidata_id: str,
    birth_date: str | None = None,
    death_date: str | None = None,
    birth_place: str | None = None,
    aliases: list[str] | None = None,
    nationalities: list[str] | None = None,
) -> str:
    first_name, last_name = parse_full_name(full_name)
    aliases       = aliases or []
    nationalities = nationalities or []
    # Prefer an explicit single nationality; else the first of the list.
    nat = nationality or (nationalities[0] if nationalities else "")
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
            # Backfill detail for a person first seen from a source that lacked it
            # (e.g. created as a bare founder name, later enriched on re-scrape).
            # Only fill blanks — never overwrite what's already there.
            session.run(
                """
                MATCH (p:Person {id: $id})
                SET p.birth_date   = COALESCE(p.birth_date, $bdate),
                    p.death_date   = COALESCE(p.death_date, $ddate),
                    p.birth_place  = COALESCE(p.birth_place, $bplace),
                    p.description   = CASE WHEN COALESCE(p.description, '') = '' THEN $desc ELSE p.description END,
                    p.nationality   = CASE WHEN COALESCE(p.nationality, '') = '' THEN $nat  ELSE p.nationality END,
                    p.alias         = CASE WHEN size(COALESCE(p.alias, [])) > 0 THEN p.alias ELSE $aliases END,
                    p.nationalities = CASE WHEN size(COALESCE(p.nationalities, [])) > 0 THEN p.nationalities ELSE $nats END
                """,
                id=rec["id"], bdate=birth_date, ddate=death_date, bplace=birth_place,
                desc=description or "", nat=nat,
                aliases=aliases, nats=nationalities,
            )
            return rec["id"]

        person_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (p:Person {
                id: $id, first_name: $first, last_name: $last,
                full_name: $full, nationality: $nat,
                description: $desc, wikidata_id: $wid,
                birth_date: $bdate, death_date: $ddate, birth_place: $bplace,
                verified: false, alias: $aliases, nationalities: $nats
            })
            """,
            id=person_id,
            first=first_name,
            last=last_name,
            full=full_name,
            nat=nat,
            desc=description or "",
            wid=wikidata_id,
            bdate=birth_date,
            ddate=death_date,
            bplace=birth_place,
            aliases=aliases,
            nats=nationalities,
        )
        return person_id


def _upsert_owns(owner_id: str, owned_id: str, source_id: str,
                 source_url: str | None = None, source_date: str | None = None):
    """Create an active OWNS edge if one doesn't already exist.

    Stamps per-entry provenance (source_url/source_date/last_scraped_at). On a
    re-scrape of an existing edge, refresh last_scraped_at so the UI shows when
    the fact was last confirmed against the source.
    """
    now = _now_iso()
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
            session.run(
                """
                MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid})
                WHERE r.until IS NULL
                SET r.last_scraped_at = $now,
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date)
                """,
                oid=owner_id, nid=owned_id, now=now,
                surl=source_url, sdate=source_date,
            )
            return
        session.run(
            """
            MATCH (a {id: $oid}), (b {id: $nid})
            CREATE (a)-[:OWNS {
                stake_percent: null, ownership_type: 'majority',
                since: null, until: null,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $sdate, last_scraped_at: $now
            }]->(b)
            """,
            oid=owner_id,
            nid=owned_id,
            sid=source_id,
            score=WIKIDATA_CREDIBILITY,
            surl=source_url, sdate=source_date, now=now,
        )


def _upsert_role(person_id: str, entity_id: str, role: str, source_id: str,
                 since: str | None = None, until: str | None = None,
                 source_url: str | None = None):
    """Create a HAS_ROLE edge if one doesn't already exist (matched on role+since)."""
    now = _now_iso()
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
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role
                  AND (r.since = $since OR (r.since IS NULL AND $since IS NULL))
                SET r.last_scraped_at = $now,
                    r.source_url = COALESCE($surl, r.source_url)
                """,
                pid=person_id, eid=entity_id, role=role, since=since, now=now,
                surl=source_url,
            )
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: $since, until: $until,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $since, last_scraped_at: $now
            }]->(e)
            """,
            pid=person_id,
            eid=entity_id,
            role=role,
            since=since,
            until=until,
            sid=source_id,
            score=WIKIDATA_CREDIBILITY,
            surl=source_url, now=now,
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
        hq_lat=data.get("hq_lat"),
        hq_lng=data.get("hq_lng"),
        hq_city=data.get("hq_city"),
        hq_country=data.get("hq_country"),
        aliases=data.get("aliases", []),
        countries=data.get("countries", []),
        hq_locations=data.get("hq_locations", []),
    )
    scraped.append({
        "qid":  qid,
        "id":   entity_id,
        "name": data["name"],
        "type": entity_type,
    })

    # Wire up to parent if this node was reached via a subsidiary edge
    if parent_entity_id:
        _upsert_owns(parent_entity_id, entity_id, source_id,
                     source_url=_wikidata_url(qid))

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
        _upsert_owns(entity_id, sub_id, source_id,
                     source_url=_wikidata_url(sub["qid"]))
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
        if ceo.get("is_human") is False:   # an org wrongly in a person slot — skip
            continue
        person_id = _upsert_person(
            full_name=ceo["label"],
            nationality=ceo.get("nationality"),
            description=ceo.get("description"),
            wikidata_id=ceo["qid"],
            birth_date=ceo.get("birth_date"),
            death_date=ceo.get("death_date"),
            birth_place=ceo.get("birth_place"),
            aliases=ceo.get("aliases"),
            nationalities=ceo.get("nationalities"),
        )
        _upsert_role(person_id, entity_id, "CEO", source_id,
                     since=ceo.get("since"), until=ceo.get("until"),
                     source_url=_wikidata_url(qid))

    # Founders / chairpersons / board members → Person + HAS_ROLE
    for off in data.get("officers", [])[:MAX_OFFICERS]:
        if not off.get("label"):
            continue
        if off.get("is_human") is False:   # a company listed as founder/board — skip
            continue
        person_id = _upsert_person(full_name=off["label"], nationality=None,
                                   description=None, wikidata_id=off["qid"],
                                   birth_date=off.get("birth_date"),
                                   death_date=off.get("death_date"),
                                   birth_place=off.get("birth_place"),
                                   aliases=off.get("aliases"),
                                   nationalities=off.get("nationalities"))
        _upsert_role(person_id, entity_id, off["role"], source_id,
                     source_url=_wikidata_url(qid))

    # Owned by (P127) → OWNS edge (owner → this company). The owner may be a
    # person (e.g. a founder-owner) or another entity (e.g. a holding company).
    for owner in data.get("owners", [])[:MAX_OWNERS]:
        if not owner.get("label"):
            continue
        instances = list(owner.get("instances", []))
        if "Q5" in instances:  # Q5 = human
            owner_id = _upsert_person(full_name=owner["label"], nationality=None,
                                      description=None, wikidata_id=owner["qid"],
                                      birth_date=owner.get("birth_date"),
                                      death_date=owner.get("death_date"),
                                      birth_place=owner.get("birth_place"),
                                      aliases=owner.get("aliases"),
                                      nationalities=owner.get("nationalities"))
        else:
            owner_id = _upsert_entity(
                name=owner["label"],
                entity_type=infer_entity_type(instances),
                country=None, founded=None, revenue=None, description=None,
                wikidata_id=owner["qid"],
            )
        _upsert_owns(owner_id, entity_id, source_id, source_url=_wikidata_url(qid))


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

    if not settings.SCRAPER_WIKIDATA_ENABLED:
        raise PermissionError(
            "Wikidata scraper is disabled. Set SCRAPER_WIKIDATA_ENABLED=true to enable."
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
        # Indexed lookups first (an OR full-scans the Entity type on ArcadeDB).
        entity_id = resolve_entity_id(
            session, sec_cik=cik, name=name, name_normalized=name_norm,
        )
        # Fuzzy CIK fallback: an EDGAR filer whose stored normalized name is a
        # prefix of this one. This can't use an index (variable-length prefix of
        # the *parameter*), so only run it as a last resort when a CIK is known
        # and the indexed lookups missed.
        if not entity_id and cik:
            rec = session.run(
                """
                MATCH (e:Entity)
                WHERE e.name_normalized IS NOT NULL
                  AND size(e.name_normalized) >= 4
                  AND $name_norm STARTS WITH e.name_normalized
                RETURN e.id AS id LIMIT 1
                """,
                name_norm=name_norm,
            ).single()
            entity_id = rec["id"] if rec else None

        if entity_id:
            # Only stamp the CIK onto the existing entity; preserve whatever
            # name and credibility the entity already has (Wikidata names are
            # human-readable; EDGAR registered names are all-caps legal strings).
            session.run(
                "MATCH (e:Entity {id: $id}) SET e.sec_cik = COALESCE($cik, e.sec_cik)",
                id=entity_id, cik=cik,
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
    """
    Find or create a Person node matched by full_name.

    SEC EDGAR investor filings use LAST FIRST word order, while Form 3/4
    executive filings use FIRST LAST order. For two-word names this causes
    duplicate nodes (e.g. "Brin Sergey" and "Sergey Brin").  We resolve
    this by also trying the reversed form before creating a new node, and
    storing whichever form already exists if found.
    """
    parts = full_name.strip().split()
    reversed_name = f"{parts[1]} {parts[0]}" if len(parts) == 2 else None

    first_name, last_name = parse_full_name(full_name)
    with db.get_session() as session:
        # 1. Exact match
        rec = session.run(
            "MATCH (p:Person {full_name: $name}) RETURN p.id AS id LIMIT 1",
            name=full_name,
        ).single()
        if rec:
            return rec["id"]

        # 2. Reversed two-word form — catches "Brin Sergey" when "Sergey Brin"
        #    already exists (or vice-versa)
        if reversed_name:
            rec = session.run(
                "MATCH (p:Person {full_name: $name}) RETURN p.id AS id LIMIT 1",
                name=reversed_name,
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
                     stake_percent: float | None, source_url: str | None = None):
    """Create or update an OWNS edge with SEC EDGAR attribution.

    Provenance stamped per-entry: source_url = the specific SEC filing document,
    source_date = the filing date, last_scraped_at = now. On a re-scrape of an
    existing edge we refresh last_scraped_at so the UI can show when we last
    confirmed the fact against the source.
    """
    now = datetime.now(timezone.utc).isoformat()
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
            # Refresh last_scraped_at and backfill the specific record URL/date
            # onto edges created before provenance (COALESCE keeps existing
            # values when this scrape didn't yield a URL).
            session.run(
                """
                MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid})
                WHERE r.source_id = $sid AND r.until IS NULL
                SET r.last_scraped_at = $now,
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date)
                """,
                oid=owner_id, nid=owned_id, sid=source_id, now=now,
                surl=source_url, sdate=file_date,
            )
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
                credibility_score: $score,
                source_url:       $surl,
                source_date:      $sdate,
                last_scraped_at:  $now
            }]->(b)
            """,
            oid=owner_id, nid=owned_id,
            stake=stake_percent, otype=ownership_type,
            since=file_date, sid=source_id, score=SEC_EDGAR_CREDIBILITY,
            surl=source_url, sdate=file_date, now=now,
        )


def _upsert_role_sec(person_id: str, entity_id: str, role: str,
                     source_id: str, source_url: str | None = None,
                     source_date: str | None = None):
    """Create a HAS_ROLE edge attributed to SEC EDGAR if not already present.

    Provenance: source_url = the specific Form 3/4 filing document,
    source_date = its filing date. On a re-scrape of an existing edge we refresh
    last_scraped_at and backfill the URL/date (COALESCE keeps existing values
    when this scrape didn't yield them).
    """
    now = datetime.now(timezone.utc).isoformat()
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
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role AND r.until IS NULL
                SET r.last_scraped_at = $now,
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date)
                """,
                pid=person_id, eid=entity_id, role=role, now=now,
                surl=source_url, sdate=source_date,
            )
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: null, until: null,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $sdate, last_scraped_at: $now
            }]->(e)
            """,
            pid=person_id, eid=entity_id, role=role,
            sid=source_id, score=SEC_EDGAR_CREDIBILITY,
            surl=source_url, sdate=source_date, now=now,
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

        # Prefer the explicit Item 8 "Type of Reporting Person" field parsed
        # from the SC 13D/13G filing (is_individual=True → IN code).
        # Fall back to the name heuristic only when the document wasn't fetched.
        is_individual = filing.get("is_individual")
        if is_individual is None:
            is_individual = is_person_name(investor_name)

        if is_individual:
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
            source_url=filing.get("source_url"),
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
        _upsert_role_sec(person_id, target_id, role, source_id,
                         source_url=exec_rec.get("source_url"),
                         source_date=exec_rec.get("source_date"))
        scraped.append({"type": "person", "name": name, "role": role})
        log.info("SEC EDGAR: wrote HAS_ROLE %r → %r (%s)", name, data["name"], role)

        # Insider (Form 4) holding → OWNS edge, so a founder/exec who holds
        # shares also shows as an owner. stake_percent is set when the issuer's
        # shares outstanding were readable; else it's a minority holding.
        shares = exec_rec.get("shares_owned")
        if shares and shares > 0:
            stake = exec_rec.get("stake_percent")
            _upsert_owns_sec(
                owner_id=person_id,
                owned_id=target_id,
                source_id=source_id,
                ownership_type=(derive_ownership_type(stake) if stake is not None else "minority"),
                file_date=exec_rec.get("source_date"),
                stake_percent=stake,
                source_url=exec_rec.get("source_url"),
            )
            scraped.append({"type": "owns", "name": name, "role": "insider owner"})
            log.info("SEC EDGAR: wrote insider OWNS %r → %r (%s shares)", name, data["name"], shares)

    # Person-centric insider ownership: for people already linked to this company
    # (founders/execs/directors — e.g. from the Wikidata pass) who don't yet have
    # an ownership edge, read THEIR own Form 4s. This reaches insiders the
    # issuer-side scan misses, e.g. a founder-CEO whose filings are flooded out of
    # the company's recent window (Larry Fink at BlackRock).
    from app.scraper.sec_edgar import fetch_insider_holding
    cik = data.get("cik")
    shares_out = data.get("shares_outstanding")
    if cik:
        with db.get_session() as session:
            known = [
                {"id": r.get("id"), "name": r.get("name")}
                for r in session.run(
                    """
                    MATCH (p:Person)-[:HAS_ROLE]->(e:Entity {id: $id})
                    WHERE NOT (p)-[:OWNS]->(e)
                    RETURN p.id AS id, p.full_name AS name LIMIT $cap
                    """,
                    id=target_id, cap=MAX_INSIDER_LOOKUPS,
                )
            ]
        for row in known:
            pname, pid = row["name"], row["id"]
            if not pname or not pid:
                continue
            holding = fetch_insider_holding(pname, cik, shares_out)
            if not holding:
                continue
            stake = holding.get("stake_percent")
            _upsert_owns_sec(
                owner_id=pid, owned_id=target_id, source_id=source_id,
                ownership_type=(derive_ownership_type(stake) if stake is not None else "minority"),
                file_date=holding.get("source_date"),
                stake_percent=stake,
                source_url=holding.get("source_url"),
            )
            scraped.append({"type": "owns", "name": pname, "role": "insider owner"})
            log.info("SEC EDGAR: person-centric insider OWNS %r → %r (%s shares)",
                     pname, data["name"], holding.get("shares_owned"))

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
    if settings.SCRAPER_WIKIDATA_ENABLED and get_source_enabled("wikidata"):
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

    out: dict = {"status": "ok", "query": query, "results": results}

    # Auto-merge high-confidence duplicate persons the sources spelled differently
    # (SEC "Page Lawrence" ↔ Wikidata "Larry Page"). Only safe, high-confidence
    # merges are applied; the rest surface in the review panel. Best-effort — a
    # dedup failure must never fail the scrape.
    if settings.SCRAPER_AUTODEDUP_ENABLED:
        try:
            from app.routers.persons import deduplicate_high_confidence
            dd = deduplicate_high_confidence(apply=True)
            out["deduplication"] = {
                "merged_count": dd["merged_count"], "review_count": dd["review_count"]}
        except Exception as exc:  # noqa: BLE001 - never fail a scrape on dedup
            log.error("Auto-dedup after scrape failed for %r: %s", query, exc)
            out["deduplication"] = {"status": "error", "detail": str(exc)}

    return out


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
                    source_id: str, source_url: str | None = None):
    """Create a HAS_ROLE edge attributed to OpenCorporates if not already present.

    Stamps per-entry provenance: source_url = the OpenCorporates company page,
    source_date = the officer's start date, last_scraped_at = now (refreshed on
    re-scrape).
    """
    now = _now_iso()
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
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role AND r.until IS NULL
                SET r.last_scraped_at = $now,
                    r.source_url = COALESCE($surl, r.source_url)
                """,
                pid=person_id, eid=entity_id, role=role, now=now,
                surl=source_url,
            )
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: $since, until: $until,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $since, last_scraped_at: $now
            }]->(e)
            """,
            pid=person_id, eid=entity_id, role=role,
            since=start_date, until=end_date,
            sid=source_id, score=OPENCORPORATES_CREDIBILITY,
            surl=source_url, now=now,
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

    # Verifiable per-record URL for this company on OpenCorporates
    company_url = _opencorporates_url(
        data.get("jurisdiction_code"), data.get("company_number"),
    )

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
        _geocode_and_attach(target_id, location_id, address)
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
                source_id, source_url=company_url,
            )
            scraped.append({"type": "person", "name": name, "role": role})
        else:
            _upsert_entity_by_name(name=name, entity_type="company")
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


# ── BODS (GLEIF / UK PSC) helpers ─────────────────────────────────────────────

def _ensure_bods_gleif_source() -> str:
    """Get or create Source node for GLEIF, return its id."""
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {name: $name}) RETURN s.id AS id",
            name=GLEIF_SOURCE_NAME,
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
            id=source_id, name=GLEIF_SOURCE_NAME,
            url=GLEIF_SOURCE_URL, score=BODS_GLEIF_CREDIBILITY,
        )
        return source_id


def _ensure_bods_uk_psc_source() -> str:
    """Get or create Source node for UK PSC, return its id."""
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {name: $name}) RETURN s.id AS id",
            name=UK_PSC_SOURCE_NAME,
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
            id=source_id, name=UK_PSC_SOURCE_NAME,
            url=UK_PSC_SOURCE_URL, score=BODS_UK_PSC_CREDIBILITY,
        )
        return source_id


# ── GLEIF public entry point ──────────────────────────────────────────────────

def run_import_bods_gleif(
    limit: int | None = None,
    filter_jurisdiction: str | None = None,
    local_file: str | None = None,
    bulk_load: bool = False,
) -> dict:
    """
    Import GLEIF dataset.
    Checks SCRAPER_ENABLED and SCRAPER_BODS_GLEIF_ENABLED.
    If local_file is given, import from file instead of URL.

    Args:
        limit:               Max entity statements to process (None = full dataset).
        filter_jurisdiction: ISO alpha-2 country code to restrict entity imports.
        local_file:          Path to a pre-downloaded .zip or .json file.
        bulk_load:           Drop secondary indexes for the load, rebuild after
                             (much faster on a full import; see bods._run_import).
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )
    if not settings.SCRAPER_BODS_GLEIF_ENABLED:
        raise PermissionError(
            "GLEIF scraper is disabled. "
            "Set SCRAPER_BODS_GLEIF_ENABLED=true in the environment to enable."
        )
    if not get_source_enabled("bods_gleif"):
        raise PermissionError("GLEIF source is disabled. Enable it in the Scraper panel.")

    from app.scraper.bods import import_bods_source, import_bods_file

    source_id = _ensure_bods_gleif_source()
    log.info("GLEIF runner: starting BODS import (limit=%s, jurisdiction=%s, local=%s)",
             limit, filter_jurisdiction, local_file)

    if local_file:
        counts = import_bods_file(
            filepath=local_file,
            source_id=source_id,
            credibility_score=BODS_GLEIF_CREDIBILITY,
            limit=limit,
            filter_jurisdiction=filter_jurisdiction,
            bulk_load=bulk_load,
        )
    else:
        counts = import_bods_source(
            source_name=GLEIF_SOURCE_NAME,
            url=GLEIF_BODS_URL,
            source_id=source_id,
            credibility_score=BODS_GLEIF_CREDIBILITY,
            limit=limit,
            filter_jurisdiction=filter_jurisdiction,
            bulk_load=bulk_load,
        )
    return {"status": "ok", "source": GLEIF_SOURCE_NAME, **counts}


# ── UK PSC public entry point ─────────────────────────────────────────────────

def run_import_bods_uk_psc(
    limit: int | None = None,
    filter_jurisdiction: str | None = None,
    local_file: str | None = None,
    bulk_load: bool = False,
) -> dict:
    """
    Import UK PSC dataset.
    Checks SCRAPER_ENABLED and SCRAPER_BODS_UK_PSC_ENABLED.
    If local_file is given, import from file instead of URL.

    Args:
        limit:               Max entity statements to process (None = full ~8 M-entity dataset).
        filter_jurisdiction: ISO alpha-2 country code (defaults to "GB" for UK PSC).
        local_file:          Path to a pre-downloaded .zip or .json file.
        bulk_load:           Drop secondary indexes for the load, rebuild after
                             (much faster on a full import; see bods._run_import).
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )
    if not settings.SCRAPER_BODS_UK_PSC_ENABLED:
        raise PermissionError(
            "UK PSC scraper is disabled. "
            "Set SCRAPER_BODS_UK_PSC_ENABLED=true in the environment to enable."
        )
    if not get_source_enabled("bods_uk_psc"):
        raise PermissionError("UK PSC source is disabled. Enable it in the Scraper panel.")

    from app.scraper.bods import import_bods_source, import_bods_file

    source_id = _ensure_bods_uk_psc_source()
    jur = filter_jurisdiction or "GB"
    log.info("UK PSC runner: starting BODS import (limit=%s, local=%s)", limit, local_file)

    if local_file:
        counts = import_bods_file(
            filepath=local_file,
            source_id=source_id,
            credibility_score=BODS_UK_PSC_CREDIBILITY,
            limit=limit,
            filter_jurisdiction=jur,
            bulk_load=bulk_load,
        )
    else:
        counts = import_bods_source(
            source_name=UK_PSC_SOURCE_NAME,
            url=UK_PSC_BODS_URL,
            source_id=source_id,
            credibility_score=BODS_UK_PSC_CREDIBILITY,
            limit=limit,
            filter_jurisdiction=jur,
            bulk_load=bulk_load,
        )
    return {"status": "ok", "source": UK_PSC_SOURCE_NAME, **counts}
