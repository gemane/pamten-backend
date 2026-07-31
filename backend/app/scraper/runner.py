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
from app.scraper.mapper import infer_entity_type, parse_full_name, is_person_name, normalize_entity_name, derive_ownership_type, is_nominee_name
from app.scraper.sources import get_source_enabled
from app.scraper.graph_writer import (
    _record_touched, _record_touched_entity, _with_autodedup,
)
from app.scraper.scraper_registry import ScraperSpec, register, registered
from app.scraper.geocode import geocode_address


def _now_iso() -> str:
    """UTC timestamp for last_scraped_at provenance."""
    return datetime.now(timezone.utc).isoformat()




def _duplicate_name_summary() -> dict:
    """Same-company-different-identifier duplicates surfaced right after an
    import (e.g. one company under two GLEIF LEIs). Best-effort — a failure here
    must never fail the import."""
    try:
        from app.scraper.maintenance import count_duplicate_entity_names
        return count_duplicate_entity_names()
    except Exception as exc:  # noqa: BLE001 - observability, never fatal
        log.warning("duplicate-name summary failed: %s", exc)
        return {"error": str(exc)}


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
BODS_GLEIF_CREDIBILITY   = 92   # authoritative LEI data, CC0 — corporate not beneficial ownership

UK_PSC_SOURCE_NAME       = "UK PSC"
UK_PSC_SOURCE_URL        = "https://www.gov.uk/government/publications/persons-with-significant-control-register"
BODS_UK_PSC_CREDIBILITY  = 97   # statutory UK legal register, CC0

# Companies House BasicCompanyData (the UK company register) — names/addresses for
# the number-keyed companies the PSC import creates. Enrichment only, no edges.
CH_REGISTER_CREDIBILITY  = 97   # statutory UK register, authoritative for the name


# ── Database helpers ──────────────────────────────────────────────────────────

def _ensure_source(name: str, url: str, credibility: int, type_: str = "register") -> str:
    """Get or create a Source node by name, return its id. One helper for every
    source (Wikidata / SEC / OpenCorporates / GLEIF / UK PSC) — pass its constants;
    Wikidata is the lone ``knowledge_base``, the rest are ``register``."""
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {name: $name}) RETURN s.id AS id", name=name,
        ).single()
        if rec:
            return rec["id"]

        source_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (s:Source {
                id: $id, name: $name, url: $url,
                credibility_score: $score, type: $type
            })
            """,
            id=source_id, name=name, url=url, score=credibility, type=type_,
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
    employees: int | None = None,
    employees_as_of: int | None = None,
    hq_lat: float | None = None,
    hq_lng: float | None = None,
    hq_city: str | None = None,
    hq_country: str | None = None,
    aliases: list[str] | None = None,
    countries: list[str] | None = None,      # all domiciles (dual-listed → >1)
    hq_locations: list[str] | None = None,   # all HQs as "City|CC" strings
    source_id: str | None = None,
    credibility_score: int = 80,
) -> str:
    """
    Find entity by wikidata_id or name, update it if found, create if not.
    Returns the entity's internal id.

    ``source_id`` (the Wikidata Source node) is stamped onto the entity so its
    own provenance shows in the node panel. It only *fills* a missing value
    (COALESCE) on update, so an entity first seen from a register (GLEIF/SEC)
    keeps that higher-credibility source when Wikidata later enriches it.
    """
    name_norm = normalize_entity_name(name)
    # FULL_TEXT search field (name + description + aliases), so scraped entities
    # are findable via /search — same content as manage.py backfill-search. The
    # BODS importer sets this inline too; the Wikidata scraper must as well or the
    # companies it adds never enter the search index.
    search_text = " ".join(p for p in (
        name, description or "", " ".join(aliases or [])) if p).strip()
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
                    e.employees       = COALESCE($employees, e.employees),
                    e.employees_as_of = COALESCE($employees_as_of, e.employees_as_of),
                    e.description     = COALESCE($desc, e.description),
                    e.search_text     = $search_text,
                    e.name_normalized = $name_norm,
                    e.aliases         = CASE WHEN size($aliases) > 0 THEN $aliases ELSE COALESCE(e.aliases, []) END,
                    e.countries       = CASE WHEN size($countries) > 0 THEN $countries ELSE COALESCE(e.countries, []) END,
                    e.hq_locations    = CASE WHEN size($hq_locations) > 0 THEN $hq_locations ELSE COALESCE(e.hq_locations, []) END,
                    e.hq_lat          = COALESCE(e.hq_lat, $hq_lat),
                    e.hq_lng          = COALESCE(e.hq_lng, $hq_lng),
                    e.hq_city         = COALESCE(e.hq_city, $hq_city),
                    e.hq_country      = COALESCE(e.hq_country, $hq_country),
                    e.name            = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $name ELSE e.name END,
                    e.name_credibility = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $cred ELSE e.name_credibility END,
                    e.source_id       = COALESCE(e.source_id, $source_id)
                """,
                id=entity_id,
                name=name,
                wid=wikidata_id,
                source_id=source_id,
                type=entity_type,
                country=country,
                founded=founded,
                revenue=revenue,
                desc=description,
                name_norm=name_norm,
                search_text=search_text,
                cred=credibility_score,
                employees=employees, employees_as_of=employees_as_of,
                aliases=aliases or [],
                countries=countries or [], hq_locations=hq_locations or [],
                hq_lat=hq_lat, hq_lng=hq_lng, hq_city=hq_city, hq_country=hq_country,
            )
            return _record_touched_entity(entity_id)

        entity_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (e:Entity {
                id: $id, name: $name, name_normalized: $name_norm,
                search_text: $search_text,
                name_credibility: $cred,
                type: $type, country: $country, founded: $founded,
                revenue: $revenue, employees: $employees, employees_as_of: $employees_as_of,
                description: $desc,
                wikidata_id: $wid, verified: false, source_id: $source_id,
                is_nominee: $is_nominee,
                aliases: $aliases, countries: $countries, hq_locations: $hq_locations,
                hq_lat: $hq_lat, hq_lng: $hq_lng,
                hq_city: $hq_city, hq_country: $hq_country
            })
            """,
            id=entity_id,
            name=name,
            name_norm=name_norm,
            search_text=search_text,
            cred=credibility_score,
            type=entity_type,
            country=country,
            founded=founded,
            revenue=revenue,
            employees=employees, employees_as_of=employees_as_of,
            desc=description,
            wid=wikidata_id,
            source_id=source_id,
            is_nominee=is_nominee_name(name),
            aliases=aliases or [],
            countries=countries or [], hq_locations=hq_locations or [],
            hq_lat=hq_lat, hq_lng=hq_lng, hq_city=hq_city, hq_country=hq_country,
        )
        return _record_touched_entity(entity_id)


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
    source_id: str | None = None,
) -> str:
    first_name, last_name = parse_full_name(full_name)
    aliases       = aliases or []
    nationalities = nationalities or []
    # Prefer an explicit single nationality; else the first of the list.
    nat = nationality or (nationalities[0] if nationalities else "")
    with db.get_session() as session:
        # Sequential indexed lookups (wikidata_id then full_name) — an OR across
        # them full-scans the Person type, which matters once UK PSC loads
        # millions of persons.
        rec = None
        if wikidata_id:
            rec = session.run(
                "MATCH (p:Person) WHERE p.wikidata_id = $wid RETURN p.id AS id LIMIT 1",
                wid=wikidata_id,
            ).single()
        if not rec:
            rec = session.run(
                "MATCH (p:Person) WHERE p.full_name = $name RETURN p.id AS id LIMIT 1",
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
                    p.nationalities = CASE WHEN size(COALESCE(p.nationalities, [])) > 0 THEN p.nationalities ELSE $nats END,
                    p.source_id     = COALESCE(p.source_id, $source_id)
                """,
                id=rec["id"], bdate=birth_date, ddate=death_date, bplace=birth_place,
                desc=description or "", nat=nat,
                aliases=aliases, nats=nationalities, source_id=source_id,
            )
            return _record_touched(rec["id"])

        person_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (p:Person {
                id: $id, first_name: $first, last_name: $last,
                full_name: $full, nationality: $nat,
                description: $desc, wikidata_id: $wid,
                birth_date: $bdate, death_date: $ddate, birth_place: $bplace,
                verified: false, alias: $aliases, nationalities: $nats,
                source_id: $source_id
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
            source_id=source_id,
        )
        return _record_touched(person_id)


def _upsert_owns(owner_id: str, owned_id: str, source_id: str,
                 source_url: str | None = None, source_date: str | None = None,
                 owner_label: str = "Entity", credibility_score: int = 80):
    """Create an active OWNS edge if one doesn't already exist.

    Stamps per-entry provenance (source_url/source_date/last_scraped_at). On a
    re-scrape of an existing edge, refresh last_scraped_at so the UI shows when
    the fact was last confirmed against the source.

    Both endpoints are labelled (owner is Entity or Person, owned is always
    Entity) so the id lookups use the per-type index — a label-less
    `MATCH (a {id}), (b {id})` full-scans every node (~14s on 3M) per edge.
    """
    owner_label = owner_label if owner_label in ("Entity", "Person") else "Entity"
    now = _now_iso()
    with db.get_session() as session:
        exists = session.run(
            f"""
            MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
            WHERE r.until IS NULL RETURN r LIMIT 1
            """,
            oid=owner_id,
            nid=owned_id,
        ).single()
        if exists:
            session.run(
                f"""
                MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
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
            f"""
            MATCH (a:{owner_label} {{id: $oid}}), (b:Entity {{id: $nid}})
            CREATE (a)-[:OWNS {{
                stake_percent: null, ownership_type: 'majority',
                since: null, until: null,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $sdate, last_scraped_at: $now
            }}]->(b)
            """,
            oid=owner_id,
            nid=owned_id,
            sid=source_id,
            score=credibility_score,
            surl=source_url, sdate=source_date, now=now,
        )


def _upsert_succession(predecessor_id: str, successor_id: str, source_id: str,
                       since: str | None = None,
                       source_url: str | None = None, source_date: str | None = None,
                       credibility_score: int = 80):
    """Create a SUCCEEDED_BY edge (predecessor → successor) if none exists.

    Models corporate succession/rename (e.g. Twitter → X Corp., from Wikidata
    P1366 'replaced by' / P1365 'replaces'). `since` is when the succession took
    effect (Wikidata P585 qualifier). Directed predecessor → successor; a
    re-scrape refreshes provenance instead of duplicating the edge. Both
    endpoints are Entity, labelled so the id lookups use the per-type index.
    """
    if predecessor_id == successor_id:
        return  # guard against a self-loop from bad data
    now = _now_iso()
    with db.get_session() as session:
        exists = session.run(
            """
            MATCH (a:Entity {id: $pid})-[r:SUCCEEDED_BY]->(b:Entity {id: $sid})
            RETURN r LIMIT 1
            """,
            pid=predecessor_id, sid=successor_id,
        ).single()
        if exists:
            session.run(
                """
                MATCH (a:Entity {id: $pid})-[r:SUCCEEDED_BY]->(b:Entity {id: $sid})
                SET r.last_scraped_at = $now,
                    r.since       = COALESCE($since, r.since),
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date)
                """,
                pid=predecessor_id, sid=successor_id, now=now,
                since=since, surl=source_url, sdate=source_date,
            )
            return
        session.run(
            """
            MATCH (a:Entity {id: $pid}), (b:Entity {id: $sid})
            CREATE (a)-[:SUCCEEDED_BY {
                since: $since, source_id: $srcid, credibility_score: $score,
                source_url: $surl, source_date: $sdate, last_scraped_at: $now
            }]->(b)
            """,
            pid=predecessor_id, sid=successor_id, srcid=source_id, since=since,
            score=credibility_score, surl=source_url, sdate=source_date, now=now,
        )


def _upsert_role(person_id: str, entity_id: str, role: str, source_id: str,
                 since: str | None = None, until: str | None = None,
                 source_url: str | None = None, credibility_score: int = 80):
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
            score=credibility_score,
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
        employees=data.get("employees"),
        employees_as_of=data.get("employees_as_of"),
        hq_lat=data.get("hq_lat"),
        hq_lng=data.get("hq_lng"),
        hq_city=data.get("hq_city"),
        hq_country=data.get("hq_country"),
        aliases=data.get("aliases", []),
        countries=data.get("countries", []),
        hq_locations=data.get("hq_locations", []),
        source_id=source_id,
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
            source_id=source_id,
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
            source_id=source_id,
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
                                   nationalities=off.get("nationalities"),
                                   source_id=source_id)
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
                                      nationalities=owner.get("nationalities"),
                                      source_id=source_id)
            owner_label = "Person"
        else:
            owner_id = _upsert_entity(
                name=owner["label"],
                entity_type=infer_entity_type(instances),
                country=None, founded=None, revenue=None, description=None,
                wikidata_id=owner["qid"],
                source_id=source_id,
            )
            owner_label = "Entity"
        _upsert_owns(owner_id, entity_id, source_id, source_url=_wikidata_url(qid),
                     owner_label=owner_label)

    # Succession (P1366 replaced-by / P1365 replaces) → SUCCEEDED_BY edge, always
    # directed predecessor → successor. Each side is a distinct entity (e.g.
    # Twitter → X Corp.), so upsert a minimal node for it like a subsidiary.
    for succ in data.get("successors", []):
        if not succ.get("name"):
            continue
        succ_id = _upsert_entity(
            name=succ["name"], entity_type="company",
            country=None, founded=None, revenue=None, description=None,
            wikidata_id=succ["qid"], source_id=source_id,
        )
        _upsert_succession(entity_id, succ_id, source_id, since=succ.get("date"),
                           source_url=_wikidata_url(qid))
    for pred in data.get("predecessors", []):
        if not pred.get("name"):
            continue
        pred_id = _upsert_entity(
            name=pred["name"], entity_type="company",
            country=None, founded=None, revenue=None, description=None,
            wikidata_id=pred["qid"], source_id=source_id,
        )
        _upsert_succession(pred_id, entity_id, source_id, since=pred.get("date"),
                           source_url=_wikidata_url(qid))


# ── Wikidata public entry point ───────────────────────────────────────────────

@_with_autodedup
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

    source_id = _ensure_source(WIKIDATA_SOURCE_NAME, WIKIDATA_SOURCE_URL, WIKIDATA_CREDIBILITY, "knowledge_base")
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

def _search_text(name: str, description: str | None, aliases: list[str] | None) -> str:
    """FULL_TEXT search field: name + description + aliases (same recipe as the
    Wikidata scraper and manage.py backfill-search)."""
    return " ".join(p for p in (
        name, description or "", " ".join(aliases or [])) if p).strip()


def _merge_aliases(existing: list[str] | None, new: list[str] | None,
                   current_name: str | None = None) -> list[str]:
    """Union of `existing` + `new` aliases, order-preserving, deduped
    case-insensitively, excluding the entity's current legal name."""
    out: list[str] = []
    seen: set[str] = set()
    skip = (current_name or "").strip().lower()
    for a in list(existing or []) + list(new or []):
        a = (a or "").strip()
        k = a.lower()
        if a and k != skip and k not in seen:
            seen.add(k)
            out.append(a)
    return out


def _upsert_entity_by_name(name: str, entity_type: str = "company",
                            cik: str | None = None,
                            source_id: str | None = None,
                            former_names: list[str] | None = None,
                            credibility_score: int = 98) -> str:
    """Find or create an Entity node matched by CIK, exact name, or normalized name.

    ``source_id`` (the calling scraper's Source node — SEC EDGAR or
    OpenCorporates) is stamped so the entity's own provenance shows in the node
    panel. On an existing node it only fills a missing value, so a register or
    Wikidata source already recorded isn't overwritten.

    ``former_names`` (SEC EDGAR ``formerNames`` — prior names of the *same* CIK)
    are folded into the entity's ``aliases`` + ``search_text`` so the old name is
    searchable (e.g. "Facebook" → Meta Platforms). Merged, never clobbered."""
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
            if former_names:
                # Fold former names into aliases + search_text (union, no clobber).
                rec = session.run(
                    "MATCH (e:Entity {id: $id}) RETURN e.name AS name, "
                    "e.aliases AS aliases, e.description AS descr",
                    id=entity_id,
                ).single()
                cur_name = (rec["name"] if rec else None) or name
                merged   = _merge_aliases(rec["aliases"] if rec else None, former_names, cur_name)
                session.run(
                    """
                    MATCH (e:Entity {id: $id})
                    SET e.sec_cik     = COALESCE($cik, e.sec_cik),
                        e.source_id   = COALESCE(e.source_id, $source_id),
                        e.aliases     = $aliases,
                        e.search_text = $search_text
                    """,
                    id=entity_id, cik=cik, source_id=source_id, aliases=merged,
                    search_text=_search_text(cur_name, rec["descr"] if rec else None, merged),
                )
            else:
                session.run(
                    """
                    MATCH (e:Entity {id: $id})
                    SET e.sec_cik   = COALESCE($cik, e.sec_cik),
                        e.source_id = COALESCE(e.source_id, $source_id)
                    """,
                    id=entity_id, cik=cik, source_id=source_id,
                )
            return _record_touched_entity(entity_id)

        entity_id = str(uuid.uuid4())
        aliases = _merge_aliases([], former_names, name)
        session.run(
            """
            CREATE (e:Entity {
                id: $id, name: $name, name_normalized: $name_norm,
                name_credibility: $cred, search_text: $search_text, aliases: $aliases,
                type: $type, sec_cik: $cik, verified: false, source_id: $source_id,
                is_nominee: $is_nominee,
                country: null, founded: null, revenue: null,
                description: null, wikidata_id: null
            })
            """,
            id=entity_id, name=name, name_norm=name_norm,
            cred=credibility_score, type=entity_type, cik=cik, source_id=source_id,
            search_text=_search_text(name, None, aliases), aliases=aliases,
            is_nominee=is_nominee_name(name),
        )
        return _record_touched_entity(entity_id)


def _upsert_person_by_name(full_name: str, source_id: str | None = None) -> str:
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
            return _record_touched(rec["id"])

        # 2. Reversed two-word form — catches "Brin Sergey" when "Sergey Brin"
        #    already exists (or vice-versa)
        if reversed_name:
            rec = session.run(
                "MATCH (p:Person {full_name: $name}) RETURN p.id AS id LIMIT 1",
                name=reversed_name,
            ).single()
            if rec:
                return _record_touched(rec["id"])

        person_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (p:Person {
                id: $id, first_name: $first, last_name: $last,
                full_name: $full, nationality: '', description: '',
                wikidata_id: null, verified: false, source_id: $source_id,
                alias: [], nationalities: []
            })
            """,
            id=person_id, first=first_name, last=last_name, full=full_name,
            source_id=source_id,
        )
        return _record_touched(person_id)


def _upsert_owns_sec(owner_id: str, owned_id: str, source_id: str,
                     ownership_type: str, file_date: str | None,
                     stake_percent: float | None, source_url: str | None = None,
                     owner_label: str = "Entity", credibility_score: int = 98):
    """Create or update an OWNS edge with SEC EDGAR attribution.

    Provenance stamped per-entry: source_url = the specific SEC filing document,
    source_date = the filing date, last_scraped_at = now. On a re-scrape of an
    existing edge we refresh last_scraped_at so the UI can show when we last
    confirmed the fact against the source.

    Endpoints are labelled (owner is Entity or Person, owned always Entity) so
    the id lookups use the index — a label-less two-node match full-scans every
    node (~14s on 3M) per edge.
    """
    owner_label = owner_label if owner_label in ("Entity", "Person") else "Entity"
    now = datetime.now(timezone.utc).isoformat()
    with db.get_session() as session:
        existing = session.run(
            f"""
            MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
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
                f"""
                MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
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
            f"""
            MATCH (a:{owner_label} {{id: $oid}}), (b:Entity {{id: $nid}})
            CREATE (a)-[:OWNS {{
                stake_percent:    $stake,
                ownership_type:   $otype,
                since:            $since,
                until:            null,
                source_id:        $sid,
                credibility_score: $score,
                source_url:       $surl,
                source_date:      $sdate,
                last_scraped_at:  $now
            }}]->(b)
            """,
            oid=owner_id, nid=owned_id,
            stake=stake_percent, otype=ownership_type,
            since=file_date, sid=source_id, score=credibility_score,
            surl=source_url, sdate=file_date, now=now,
        )


def _upsert_role_sec(person_id: str, entity_id: str, role: str,
                     source_id: str, source_url: str | None = None,
                     source_date: str | None = None, credibility_score: int = 98):
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
            sid=source_id, score=credibility_score,
            surl=source_url, sdate=source_date, now=now,
        )


# ── SEC EDGAR public entry point ──────────────────────────────────────────────

@_with_autodedup
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

    source_id = _ensure_source(SEC_EDGAR_SOURCE_NAME, SEC_EDGAR_SOURCE_URL, SEC_EDGAR_CREDIBILITY)
    scraped: list[dict] = []

    # Upsert the target company
    target_id = _upsert_entity_by_name(
        name=data["name"],
        entity_type="company",
        cik=data.get("cik"),
        source_id=source_id,
        former_names=data.get("former_names"),
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
            investor_node_id = _upsert_person_by_name(investor_name, source_id=source_id)
            scraped.append({"type": "person", "name": investor_name, "role": "investor"})
        else:
            investor_node_id = _upsert_entity_by_name(
                name=investor_name,
                entity_type="company",
                cik=filing.get("investor_cik"),
                source_id=source_id,
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
            owner_label="Person" if is_individual else "Entity",
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

        person_id = _upsert_person_by_name(name, source_id=source_id)
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
                owner_label="Person",
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
                owner_label="Person",
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

@_with_autodedup
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

    # Dispatch to every registered scraper — no hardcoded per-source chain, so a new
    # scraper just registers a ScraperSpec (see app/scraper/scraper_registry.py).
    results: dict[str, dict] = {}
    for spec in registered():
        if not spec.enabled():
            results[spec.name] = {"status": "disabled"}
            continue
        try:
            results[spec.name] = spec.run(query, depth)
        except PermissionError as exc:
            results[spec.name] = {"status": "disabled", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - one scraper failing mustn't sink the rest
            log.error("%s scrape failed for %r: %s", spec.name, query, exc)
            results[spec.name] = {"status": "error", "detail": str(exc)}

    return {"status": "ok", "query": query, "results": results}


# ── OpenCorporates helpers ────────────────────────────────────────────────────

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
                    source_id: str, source_url: str | None = None, credibility_score: int = 85):
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
            sid=source_id, score=credibility_score,
            surl=source_url, now=now,
        )


# ── OpenCorporates public entry point ─────────────────────────────────────────

@_with_autodedup
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

    source_id = _ensure_source(OPENCORPORATES_SOURCE_NAME, OPENCORPORATES_SOURCE_URL, OPENCORPORATES_CREDIBILITY)
    scraped: list[dict] = []

    # Verifiable per-record URL for this company on OpenCorporates
    company_url = _opencorporates_url(
        data.get("jurisdiction_code"), data.get("company_number"),
    )

    # Upsert the target company
    target_id = _upsert_entity_by_name(
        name=data["name"],
        entity_type="company",
        source_id=source_id,
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
            person_id = _upsert_person_by_name(name, source_id=source_id)
            _upsert_role_oc(
                person_id, target_id, role,
                officer.get("start_date"), officer.get("end_date"),
                source_id, source_url=company_url,
            )
            scraped.append({"type": "person", "name": name, "role": role})
        else:
            _upsert_entity_by_name(name=name, entity_type="company", source_id=source_id)
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

def _post_bods_import() -> dict:
    """Housekeeping every BODS import needs, so it isn't a separate manual step:
    flag nominee/custodian entities the load added, and collapse duplicate active
    OWNS edges (CREATE EDGE isn't idempotent, so a re-import doubles them).
    Best-effort — a failure here must not fail the import."""
    from app.scraper.maintenance import flag_nominee_entities, deduplicate_owns_edges
    out: dict = {}
    try:
        out["nominees"] = flag_nominee_entities()
    except Exception as exc:  # noqa: BLE001
        log.warning("post-import flag-nominees failed: %s", exc)
    try:
        out["edge_dedup"] = deduplicate_owns_edges()
    except Exception as exc:  # noqa: BLE001
        log.warning("post-import edge dedup failed: %s", exc)
    return out


# ── GLEIF public entry points (golden copy) ───────────────────────────────────
# The OpenOwnership GLEIF BODS import was retired — GLEIF entities/relationships/
# succession now come from the current golden copy (LEI-CDF + RR-CDF), below.

def run_import_ch_psc(local_file: str, limit: int | None = None,
                      bulk_load: bool = False, batch_size: int = 400) -> dict:
    """
    Import a Companies House PSC snapshot (current UK beneficial ownership, daily)
    — the replacement for the frozen OpenOwnership UK PSC BODS export. Reuses the
    UK PSC source + flag. Checks SCRAPER_ENABLED and SCRAPER_BODS_UK_PSC_ENABLED.
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

    from app.scraper.companies_house_psc import import_ch_psc

    source_id = _ensure_source(UK_PSC_SOURCE_NAME, UK_PSC_SOURCE_URL, BODS_UK_PSC_CREDIBILITY)
    log.info("CH PSC: importing from %s (limit=%s)", local_file, limit)
    counts = import_ch_psc(
        filepath=local_file,
        source_id=source_id,
        credibility_score=BODS_UK_PSC_CREDIBILITY,
        limit=limit,
        bulk_load=bulk_load,
        batch_size=batch_size,
    )
    return {"status": "ok", "source": UK_PSC_SOURCE_NAME, **counts,
            **_post_bods_import()}


def run_import_basic_company_data(local_file: str, limit: int | None = None,
                                  bulk_load: bool = False, batch_size: int = 400) -> dict:
    """
    Enrich number-keyed UK companies (gb-coh:{number}) with names/addresses/former
    names from a Companies House BasicCompanyData snapshot — the companion to the
    PSC import, which leaves controlled companies un-named. Enrichment only (no
    nodes/edges created), so no _post_bods_import housekeeping is needed. Gated on
    SCRAPER_ENABLED and SCRAPER_BODS_UK_PSC_ENABLED (same UK-data switch as PSC).
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

    from app.scraper.basic_company_data import import_basic_company_data

    log.info("CH BasicData: importing from %s (limit=%s)", local_file, limit)
    counts = import_basic_company_data(
        filepath=local_file,
        credibility_score=CH_REGISTER_CREDIBILITY,
        limit=limit,
        bulk_load=bulk_load,
        batch_size=batch_size,
    )
    return {"status": "ok", "source": "Companies House Register", **counts}


def run_import_gleif_succession(local_file: str, limit: int | None = None) -> dict:
    """
    Import GLEIF LEI-CDF succession (MERGED/DUPLICATE/RETIRED → SuccessorLEI) into
    SUCCEEDED_BY edges. Reuses the GLEIF source + flags. `local_file` is a
    pre-downloaded LEI-CDF golden-copy .json/.zip (multi-GB — a local batch job,
    not a URL fetch, so no download path here). Checks SCRAPER_ENABLED and
    SCRAPER_BODS_GLEIF_ENABLED.
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

    from app.scraper.gleif_succession import import_lei_cdf_succession

    source_id = _ensure_source(GLEIF_SOURCE_NAME, GLEIF_SOURCE_URL, BODS_GLEIF_CREDIBILITY)
    log.info("GLEIF succession: importing from %s (limit=%s)", local_file, limit)
    counts = import_lei_cdf_succession(
        filepath=local_file,
        source_id=source_id,
        credibility_score=BODS_GLEIF_CREDIBILITY,
        limit=limit,
    )
    return {"status": "ok", "source": GLEIF_SOURCE_NAME, **counts}


def run_import_gleif_lei_cdf(local_file: str, limit: int | None = None,
                             filter_jurisdiction: str | None = None,
                             bulk_load: bool = False) -> dict:
    """
    Import GLEIF entities from the LEI-CDF golden copy (current, authoritative) —
    the replacement for the frozen OpenOwnership GLEIF BODS entity data. Reuses
    the GLEIF source + flags. Checks SCRAPER_ENABLED and SCRAPER_BODS_GLEIF_ENABLED.
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

    from app.scraper.gleif_lei_cdf import import_lei_cdf_entities

    source_id = _ensure_source(GLEIF_SOURCE_NAME, GLEIF_SOURCE_URL, BODS_GLEIF_CREDIBILITY)
    log.info("GLEIF LEI-CDF entities: importing from %s (limit=%s, jur=%s)",
             local_file, limit, filter_jurisdiction)
    counts = import_lei_cdf_entities(
        filepath=local_file,
        source_id=source_id,
        credibility_score=BODS_GLEIF_CREDIBILITY,
        limit=limit,
        filter_jurisdiction=filter_jurisdiction,
        bulk_load=bulk_load,
    )
    # Stamp the baseline marker so the incremental `gleif-update` knows the full
    # load has run (it refuses to apply deltas onto an un-baselined graph).
    from app.scraper.gleif_incremental import mark_full_load_done
    mark_full_load_done()
    return {"status": "ok", "source": GLEIF_SOURCE_NAME, **counts,
            "duplicate_names": _duplicate_name_summary()}


def run_import_gleif_rr(local_file: str, limit: int | None = None) -> dict:
    """
    Import GLEIF RR-CDF (Level 2) direct/ultimate consolidation parents as
    direct/indirect OWNS edges. Reuses the GLEIF source + flags. `local_file` is a
    pre-downloaded RR-CDF golden-copy .json/.zip. Checks SCRAPER_ENABLED and
    SCRAPER_BODS_GLEIF_ENABLED.
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

    from app.scraper.gleif_rr import import_rr_cdf
    from app.scraper.maintenance import deduplicate_owns_edges

    source_id = _ensure_source(GLEIF_SOURCE_NAME, GLEIF_SOURCE_URL, BODS_GLEIF_CREDIBILITY)
    log.info("GLEIF RR-CDF: importing from %s (limit=%s)", local_file, limit)
    counts = import_rr_cdf(
        filepath=local_file,
        source_id=source_id,
        credibility_score=BODS_GLEIF_CREDIBILITY,
        limit=limit,
    )
    # RR consolidation edges overlap the GLEIF parent edges from the BODS import,
    # so collapse the duplicates automatically (keeps the direct/indirect-flagged
    # edge) — no separate "remember to dedup" step.
    log.info("GLEIF RR-CDF: deduplicating overlapping OWNS edges")
    dedup = deduplicate_owns_edges()
    return {"status": "ok", "source": GLEIF_SOURCE_NAME, **counts, "edge_dedup": dedup}


def run_gleif_update(interval: str = "auto", lei_file: str | None = None,
                     rr_file: str | None = None, limit: int | None = None) -> dict:
    """
    Apply a GLEIF **delta** update on top of the full golden-copy load — the
    retirement-aware daily refresh (see `app/scraper/gleif_incremental.py`). It
    idempotently upserts changed entities + succession edges and upserts/closes
    changed OWNS edges against the live-indexed DB (no `--bulk-load`, no whole-DB
    dedup), and is logged as a `gleif-update` ScrapeRun (visible in GET /scraper/runs).

    `interval="auto"` (the default) is **gap-aware**: it checkpoints the last GLEIF
    publish it applied (an `ImportState` node) and, on each run, picks the smallest
    delta window (LastDay/Week/Month) that still covers the gap since that
    checkpoint — so a few missed daily runs self-heal on the next one. A gap wider
    than ~30 days can't be covered by a delta and raises (run a full reload). Pass an
    explicit `interval` (IntraDay/LastDay/LastWeek/LastMonth) to override, or
    pre-downloaded `lei_file`/`rr_file` to skip the fetch.
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

    from app.scraper.gleif_incremental import (
        choose_catchup_interval,
        download_deltas,
        fetch_publish_metadata,
        full_load_present,
        import_lei_cdf_delta,
        import_rr_delta,
        read_last_publish,
        write_last_publish,
    )
    from app.db.schema import ensure_indexes
    from app.scraper.run_log import record_run

    # Idempotent, ~1s. Guarantees the checkpoint type (ImportState) + indexes exist
    # even when the cron runs before the API has ever started on a fresh DB — the
    # intended "full-import.sh, then let the daily delta take over" bootstrap.
    ensure_indexes()

    source_id = _ensure_source(GLEIF_SOURCE_NAME, GLEIF_SOURCE_URL, BODS_GLEIF_CREDIBILITY)
    with record_run("gleif-update", interval) as run:
        # The delta rides on top of the full golden copy — refuse to apply it onto a
        # graph that was never baselined (it would build a partial, wrong dataset).
        if not full_load_present():
            raise RuntimeError(
                "No GLEIF full load found — the incremental update rides on top of the "
                "full golden copy. Run the full load first (full-import.sh / "
                "`manage.py gleif-lei-cdf`), then re-run.")
        current_publish = None
        if lei_file and rr_file:
            resolved = "local"
            log.info("GLEIF update: using local delta files")
        else:
            publish = fetch_publish_metadata()
            current_publish = publish.get("publish_date")
            if (interval or "auto").lower() == "auto":
                last = read_last_publish()
                resolved = choose_catchup_interval(last, current_publish)
                if resolved is None:
                    raise RuntimeError(
                        f"GLEIF delta can't cover the gap since last applied publish "
                        f"{last!r} (current {current_publish!r}) — too stale for a delta. "
                        "Run a full reload (full-import.sh).")
                log.info("GLEIF update: auto interval=%s (last applied publish=%s, current=%s)",
                         resolved, last, current_publish)
            else:
                resolved = interval
                log.info("GLEIF update: fetching %s deltas", resolved)
            paths = download_deltas(publish, resolved)
            lei_file, rr_file = paths["lei2"], paths["rr"]

        log.info("GLEIF update: applying LEI-CDF delta %s", lei_file)
        lei = import_lei_cdf_delta(lei_file, source_id, BODS_GLEIF_CREDIBILITY, limit=limit)
        log.info("GLEIF update: applying RR delta %s", rr_file)
        rr = import_rr_delta(rr_file, source_id, BODS_GLEIF_CREDIBILITY, limit=limit)
        run["total"] = lei["updated"] + rr["created"] + rr["closed"]
        # Advance the checkpoint only after a clean apply, and only when we fetched a
        # published delta in full (not local files, not a --limit spot check).
        if current_publish and not limit:
            write_last_publish(current_publish)

    return {"status": "ok", "source": GLEIF_SOURCE_NAME, "interval": resolved,
            "publish_date": current_publish, "lei_cdf": lei, "rr": rr}


# ── Scraper registry ──────────────────────────────────────────────────────────
# Register the built-in scrapers so run_scrape_all (and, later, the router) can
# iterate them. A new scraper registers its own ScraperSpec — no dispatch edits.
register(ScraperSpec(
    "wikidata", lambda q, d: run_scrape(q, d),
    lambda: settings.SCRAPER_WIKIDATA_ENABLED and get_source_enabled("wikidata")))
register(ScraperSpec(
    "sec_edgar", lambda q, d: run_scrape_sec_edgar(q),
    lambda: settings.SCRAPER_SEC_EDGAR_ENABLED and get_source_enabled("sec_edgar")))
register(ScraperSpec(
    "open_corporates", lambda q, d: run_scrape_open_corporates(q),
    lambda: settings.SCRAPER_OPENCORPORATES_ENABLED and get_source_enabled("open_corporates")))
