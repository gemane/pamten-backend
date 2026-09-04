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

import os
import uuid
import logging
import zipfile
from contextlib import ExitStack
from datetime import datetime, timezone
from app.config import settings
from app.database import db
from app.entity_resolution import resolve_entity_id
from app.claims import record_claim, KIND_ROLE
from app.scraper.wikidata import (search_entity, search_entity_in_country,
                                  fetch_company_data, pick_candidate)
from app.scraper.sources import KNOWN_SOURCES
from app.scraper.mapper import infer_entity_type, parse_full_name, is_person_name, normalize_entity_name, derive_ownership_type, is_nominee_name
from app.scraper.sources import get_source_enabled
from app.scraper.graph_writer import (
    _record_touched, _record_touched_entity, _with_autodedup, set_scrape_target,
    # The shared write layer lives in graph_writer now (the module split the
    # multi-scraper refactor deferred); re-exported here so existing imports and
    # test patch targets — `app.scraper.runner._upsert_owns` and friends — keep
    # working unchanged.
    _ensure_source, _merge_aliases, _now_iso, _person_search_text,   # noqa: F401
    _upsert_entity_by_name, _upsert_person_by_name,                  # noqa: F401
    _upsert_owns, _upsert_role, _upsert_succession,                  # noqa: F401
)
from app.scraper.sec_writer import (
    VOTING_GROUP_TYPE,                                                           # noqa: F401
    _INDIVIDUAL_CODES, _is_control_filing, _member_key,                          # noqa: F401
    _retire_superseded_bloc_edge, _roster_overlap, _rosters_match,               # noqa: F401
    _same_member, _split_member_key,                                             # noqa: F401
    _upsert_group_membership, _upsert_owns_sec,                                  # noqa: F401
    _upsert_role_sec, _upsert_voting_group,                                      # noqa: F401
)
from app.scraper.scraper_registry import ScraperSpec, register, registered
from app.scraper.country_match import matches_requested, country_mismatch
from app.scraper.geocode import geocode_address






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


def _stamp_registration(entity_id: str, jurisdiction_code: str | None,
                        company_number: str | None) -> None:
    """Record an OpenCorporates company number as identity, fill-if-missing.

    jurisdiction + number IS OpenCorporates' identity model, yet it used to be
    fetched and thrown away after building the source URL. `gb` becomes
    companies_house_id (the same convention GLEIF and PSC use); other national
    jurisdictions become register_id when the country maps to exactly one
    register, and sub-national codes ("us_de") through the curated place map —
    "us" alone names 64 registers, but the state names exactly one.

    COALESCE: this scraper resolves by name, so `entity_id` may be a node
    another source already stamped — a lookup must never overwrite a register's
    own statement of the same fact.
    """
    from app.scraper.gleif_reference import (make_register_id, register_for_place,
                                             sole_register_for_country)

    if not jurisdiction_code or not company_number:
        return
    number = company_number.strip()
    iso2 = jurisdiction_code[:2].upper() if len(jurisdiction_code) >= 2 else None
    sub_national = len(jurisdiction_code) > 2
    ch_id = number if iso2 == "GB" and not sub_national else None
    register_id = None
    if not ch_id and not sub_national:
        register_id = make_register_id(sole_register_for_country(iso2), number)
    elif not ch_id and sub_national:
        region = jurisdiction_code.split("_", 1)[1] if "_" in jurisdiction_code else ""
        register_id = make_register_id(register_for_place(iso2, region), number)
    with db.get_session() as session:
        session.run(
            "MATCH (e:Entity {id: $id}) "
            "SET e.registration_number = COALESCE(e.registration_number, $number), "
            "    e.companies_house_id = COALESCE(e.companies_house_id, $ch), "
            "    e.register_id = COALESCE(e.register_id, $rid)",
            id=entity_id, number=number, ch=ch_id, rid=register_id)

log = logging.getLogger(__name__)


def _geocode_registered_and_attach(entity_id: str, address: dict) -> None:
    """
    Best-effort: geocode a REGISTERED address and write it to the registered
    fields, so the map can place a pin without traversing edges. Keeps any values
    already present (COALESCE(existing, new)) so richer data is never clobbered.

    It used to write these coordinates into `hq_*`. That conflated two different
    places: a registered office is frequently an agent's door — 24 companies in
    the dev graph share one building in Wilmington — and calling it a headquarters
    put the company somewhere it has never traded. The map now draws one or the
    other from the Registered/Headquarters switch, so the distinction has to hold
    at the point of writing.
    """
    coord = geocode_address(address)
    lat, lng = coord if coord else (None, None)
    with db.get_session() as session:
        session.run(
            """
            MATCH (e:Entity {id: $id})
            SET e.country    = COALESCE(e.country, $country),
                e.reg_lat    = COALESCE(e.reg_lat, $lat),
                e.reg_lng    = COALESCE(e.reg_lng, $lng),
                e.reg_geo_precision = COALESCE(e.reg_geo_precision, $prec)
            """,
            id=entity_id,
            country=address.get("country") or None,
            lat=lat, lng=lng,
            # geocode_address works from city/country, so town level at best.
            prec="approx" if coord else None,
        )

# Source metadata comes from the catalogue in app/scraper/sources.py — one
# definition, so the provenance stamped onto scraped data and the public source
# list cannot drift apart. These aliases keep the call sites below readable.
_WD, _SEC = KNOWN_SOURCES["wikidata"], KNOWN_SOURCES["sec_edgar"]
_OC, _GLEIF, _PSC = (KNOWN_SOURCES["open_corporates"], KNOWN_SOURCES["bods_gleif"],
                     KNOWN_SOURCES["bods_uk_psc"])

WIKIDATA_SOURCE_NAME  = _WD["label"]
WIKIDATA_SOURCE_URL   = _WD["url"]
WIKIDATA_CREDIBILITY  = _WD["credibility"]
MAX_SUBSIDIARIES      = 15   # per entity, to avoid runaway scrapes
MAX_CEOS              = 3
MAX_OFFICERS          = 30   # founders + chairpersons + board members combined (large boards)
MAX_OWNERS            = 10   # owned-by (P127) links
MAX_INSIDER_LOOKUPS   = 15   # known people to look up personal Form-4 holdings for

SEC_EDGAR_SOURCE_NAME = _SEC["label"]
SEC_EDGAR_SOURCE_URL  = _SEC["url"]
SEC_EDGAR_CREDIBILITY = _SEC["credibility"]        # legally mandated filings

OPENCORPORATES_SOURCE_NAME = _OC["label"]
OPENCORPORATES_SOURCE_URL  = _OC["url"]
OPENCORPORATES_CREDIBILITY = _OC["credibility"]

GLEIF_SOURCE_NAME        = _GLEIF["label"]
GLEIF_SOURCE_URL         = _GLEIF["url"]
BODS_GLEIF_CREDIBILITY   = _GLEIF["credibility"]   # authoritative LEI data, CC0

UK_PSC_SOURCE_NAME       = _PSC["label"]
UK_PSC_SOURCE_URL        = _PSC["url"]
BODS_UK_PSC_CREDIBILITY  = _PSC["credibility"]     # statutory UK legal register, CC0

# Companies House BasicCompanyData (the UK company register) — names/addresses for
# the number-keyed companies the PSC import creates. Enrichment only, no edges, and
# no toggle of its own, so it is not in the catalogue.
CH_REGISTER_CREDIBILITY  = 97   # statutory UK register, authoritative for the name




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
    lei: str | None = None,                  # P1278 — bridge to a GLEIF node
    sec_cik: str | None = None,              # P5531 — bridge to a SEC EDGAR node
    website: str | None = None,              # P856 — official site (display only)
    logo_url: str | None = None,             # P154/P18 → direct Commons thumb (display only)
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
        # lei_id / sec_cik are checked ahead of the name (see _RESOLVE_FIELDS), so
        # a company Wikidata knows the LEI for attaches to its existing GLEIF node
        # instead of becoming a second copy of it. That is the duplicate this
        # bridge is meant to stop being created in the first place; already-split
        # pairs still need the dedup pass to merge them.
        entity_id = resolve_entity_id(
            session, wikidata_id=wikidata_id, sec_cik=sec_cik, lei_id=lei,
            name=name, name_normalized=name_norm,
        )

        if entity_id:
            # lei_id / sec_cik use COALESCE(existing, new): a register (GLEIF/SEC)
            # is authoritative for its own identifier and Wikidata is crowd-edited,
            # so only fill a gap — a clobbered lei_id would re-point a merge key at
            # the wrong company. NB: comments must stay OUT of the query string;
            # ArcadeDB's Cypher parser rejects `--` and fails the whole statement.
            session.run(
                """
                MATCH (e:Entity {id: $id})
                SET e.wikidata_id     = $wid,
                    e.lei_id          = COALESCE(e.lei_id, $lei),
                    e.sec_cik         = COALESCE(e.sec_cik, $sec_cik),
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
                    e.website         = COALESCE(e.website, $website),
                    e.logo_url        = COALESCE(e.logo_url, $logo_url),
                    e.name            = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $name ELSE e.name END,
                    e.name_credibility = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $cred ELSE e.name_credibility END,
                    e.source_id       = COALESCE(e.source_id, $source_id)
                """,
                id=entity_id,
                name=name,
                wid=wikidata_id,
                lei=lei, sec_cik=sec_cik,
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
                website=website, logo_url=logo_url,
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
                wikidata_id: $wid, lei_id: $lei, sec_cik: $sec_cik,
                verified: false, source_id: $source_id,
                is_nominee: $is_nominee,
                aliases: $aliases, countries: $countries, hq_locations: $hq_locations,
                hq_lat: $hq_lat, hq_lng: $hq_lng,
                hq_city: $hq_city, hq_country: $hq_country,
                website: $website,
                logo_url: $logo_url
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
            lei=lei, sec_cik=sec_cik,
            source_id=source_id,
            is_nominee=is_nominee_name(name),
            aliases=aliases or [],
            countries=countries or [], hq_locations=hq_locations or [],
            hq_lat=hq_lat, hq_lng=hq_lng, hq_city=hq_city, hq_country=hq_country,
            website=website, logo_url=logo_url,
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
                    p.source_id     = COALESCE(p.source_id, $source_id),
                    // Derived, so it is refreshed rather than blank-filled: this
                    // pass may be the one that brought the aliases.
                    p.search_text   = $search_text
                """,
                id=rec["id"], bdate=birth_date, ddate=death_date, bplace=birth_place,
                desc=description or "", nat=nat,
                search_text=_person_search_text(full_name, aliases),
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
                source_id: $source_id, search_text: $search_text
            })
            """,
            id=person_id,
            first=first_name,
            last=last_name,
            full=full_name,
            search_text=_person_search_text(full_name, aliases),
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




# ── Recursive scrape ──────────────────────────────────────────────────────────

def _resolve_related_company(rel: dict) -> str | None | bool:
    """Decide whether a Wikidata-related COMPANY may be written.

    Returns an existing node id (write the edge to it), None (proceed to
    create — it carries a hard id, so a future source can merge it), or
    False (SKIP — no existing node, no LEI/CIK, so creating it would mint an
    unidentifiable orphan that only clutters search and dedup, the litter the
    depth-2 pass produced for Samsung's subsidiaries).

    People are never routed here — Wikidata's coverage of people is a strength
    and person dedup handles them; only company nodes are gated."""
    from app.database import db
    lei, cik, wd = rel.get("lei"), rel.get("sec_cik"), rel.get("qid")
    with db.get_session() as session:
        existing = resolve_entity_id(session, lei_id=lei, sec_cik=cik,
                                     wikidata_id=wd)
    if existing:
        return existing
    return None if (lei or cik) else False


def _scrape_node(
    qid: str,
    depth: int,
    visited: set,
    scraped: list,
    source_id: str,
    parent_entity_id: str | None = None,
    counts: dict | None = None,
):
    # `counts` accumulates hygiene stats across the recursion (currently just
    # skipped_unidentified — related companies with no id and no existing node,
    # not written). Created once by the top call, threaded down.
    if counts is None:
        counts = {}
    if qid in visited:
        return
    visited.add(qid)

    data = fetch_company_data(qid)
    if not data or not data.get("name"):
        return

    # A person is not a company, and `infer_entity_type` cannot say so: it falls
    # back to "company" for any P31 it does not recognise, which includes Q5.
    # Searching "Larry Page" put the top Wikidata hit — the man — straight through
    # here and wrote him as a company alongside the Person node that already
    # existed for him. Owners and officers are checked for Q5 before they are
    # written; the search target never was.
    if "Q5" in (data.get("instances") or []):
        log.info("Wikidata: %s (%s) is a person, not a company — not writing an Entity",
                 data.get("name"), qid)
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
        website=data.get("website"),
        logo_url=data.get("logo_url"),
        employees_as_of=data.get("employees_as_of"),
        hq_lat=data.get("hq_lat"),
        hq_lng=data.get("hq_lng"),
        hq_city=data.get("hq_city"),
        hq_country=data.get("hq_country"),
        aliases=data.get("aliases", []),
        countries=data.get("countries", []),
        hq_locations=data.get("hq_locations", []),
        lei=data.get("lei"),
        sec_cik=data.get("sec_cik"),
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
        gate = _resolve_related_company(sub)
        if gate is False:
            counts["skipped_unidentified"] = counts.get("skipped_unidentified", 0) + 1
            continue
        sub_id = gate or _upsert_entity(
            name=sub_name,
            entity_type=sub_type,
            lei=sub.get("lei"), sec_cik=sub.get("sec_cik"),
            # Fetched in one batched query alongside the scrape. Passing None here
            # is what left owner-only companies — BlackRock among them — with no
            # country at all, and so absent from the map. Jurisdiction and
            # headquarters stay separate: the map's Registered/Headquarters switch
            # is meaningless if they are conflated on the way in.
            country=sub.get("country"),
            hq_country=sub.get("hq_country"),
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
                         parent_entity_id=entity_id, counts=counts)
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
                     since=off.get("since"), until=off.get("until"),
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
            gate = _resolve_related_company(owner)
            if gate is False:
                counts["skipped_unidentified"] = counts.get("skipped_unidentified", 0) + 1
                continue
            owner_id = gate or _upsert_entity(
                name=owner["label"],
                entity_type=infer_entity_type(instances),
                lei=owner.get("lei"), sec_cik=owner.get("sec_cik"),
                country=owner.get("country"), hq_country=owner.get("hq_country"),
                founded=None, revenue=None, description=None,
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
        gate = _resolve_related_company(succ)
        if gate is False:
            counts["skipped_unidentified"] = counts.get("skipped_unidentified", 0) + 1
            continue
        succ_id = gate or _upsert_entity(
            name=succ["name"], entity_type="company",
            country=None, founded=None, revenue=None, description=None,
            wikidata_id=succ["qid"], lei=succ.get("lei"),
            sec_cik=succ.get("sec_cik"), source_id=source_id,
        )
        _upsert_succession(entity_id, succ_id, source_id, since=succ.get("date"),
                           source_url=_wikidata_url(qid))
    for pred in data.get("predecessors", []):
        if not pred.get("name"):
            continue
        gate = _resolve_related_company(pred)
        if gate is False:
            counts["skipped_unidentified"] = counts.get("skipped_unidentified", 0) + 1
            continue
        pred_id = gate or _upsert_entity(
            name=pred["name"], entity_type="company",
            country=None, founded=None, revenue=None, description=None,
            wikidata_id=pred["qid"], lei=pred.get("lei"),
            sec_cik=pred.get("sec_cik"), source_id=source_id,
        )
        _upsert_succession(pred_id, entity_id, source_id, since=pred.get("date"),
                           source_url=_wikidata_url(qid))


# ── Wikidata public entry point ───────────────────────────────────────────────

@_with_autodedup
def run_scrape(query: str, depth: int = 2, country: str | None = None) -> dict:
    """
    Trigger a Wikidata scrape for a company name.
    Raises PermissionError if SCRAPER_ENABLED is not true.

    With `country` (ISO-2, from the search box) the search itself is restricted to
    that country at Wikidata — `haswbstatement:P17` — rather than the world's best
    "Alphabet" being fetched and then judged. The difference is not cosmetic:
    Alphabet Fuhrparkmanagement, the German company of that name, is nowhere near
    the global top hits, so no amount of filtering afterwards would ever find it.

    An item that states no country cannot be found this way, by design. Asked for
    a company in Germany, "we do not know where this is" is not an answer.
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

    results = (search_entity_in_country(query, country) if country
               else search_entity(query, limit=3))
    if not results:
        return {"status": "no_results", "query": query, "total": 0, "scraped": [],
                "requested_country": country}

    # A name is not a kind. Searching "Steve Jobs" returns the 2015 film first,
    # the book second and the man third — and taking the top hit wrote a Danny
    # Boyle picture into the graph as a company, because `infer_entity_type`
    # falls back to "company" for any P31 it does not recognise. Pick the hit
    # that actually looks like a company, or say there is not one.
    qid = pick_candidate(results, "company")
    if qid is None:
        log.info("Wikidata: nothing company-shaped among the hits for %r", query)
        return {"status": "not_a_company", "query": query, "total": 0, "scraped": [],
                "requested_country": country}

    source_id = _ensure_source(WIKIDATA_SOURCE_NAME, WIKIDATA_SOURCE_URL, WIKIDATA_CREDIBILITY, "knowledge_base")
    scraped: list = []
    visited: set  = set()
    counts: dict  = {}

    _scrape_node(qid, depth, visited, scraped, source_id, counts=counts)

    # Mark the TARGET (the searched company) as on-demand scraped at this depth, so the
    # freshness gate + the depth-2 "deepen" pass can decide correctly next time. Only the
    # target — not the subsidiaries this pass touched (they stay independently scrapable).
    target_id = next((s["id"] for s in scraped if s.get("qid") == qid), None)
    if target_id:
        set_scrape_target(target_id, depth)

    return {
        "status":      "ok",
        "query":       query,
        "wikidata_id": qid,
        "entity_id":   target_id,
        "total":       len(scraped),
        "scraped":     scraped,
        "skipped_unidentified": counts.get("skipped_unidentified", 0),
    }


# ── SEC EDGAR helpers ─────────────────────────────────────────────────────────





# ── SEC EDGAR public entry point ──────────────────────────────────────────────

@_with_autodedup
def _upsert_affiliate(filer_id: str, affiliate_id: str, source_id: str,
                      source_url: str | None = None, source_date: str | None = None):
    """Record that two entities belong to the same fund group.

    RELATED_TO carries a free-text `relation`, which is why it fits: the edge says
    exactly what the filing says and nothing more. Deliberately NOT an OWNS edge —
    a 13F cover page naming another manager establishes group membership, not
    ownership or control, and writing it as ownership would invent a fact.

    MERGE on (relation) so re-scraping refreshes provenance instead of stacking
    duplicate edges.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db.get_session() as session:
        session.run(
            """
            MATCH (a:Entity {id: $aid}), (b:Entity {id: $bid})
            MERGE (a)-[r:RELATED_TO {relation: 'affiliate'}]->(b)
            SET r.source_id       = COALESCE(r.source_id, $sid),
                r.source_url      = COALESCE($surl, r.source_url),
                r.source_date     = COALESCE($sdate, r.source_date),
                r.last_scraped_at = $now
            """,
            aid=filer_id, bid=affiliate_id, sid=source_id,
            surl=source_url, sdate=source_date, now=now,
        )


def _write_affiliates(filer_id: str, affiliates: list[dict], source_id: str) -> int:
    """Create the entity nodes for a filer's affiliated managers and link them."""
    written = 0
    for aff in affiliates or []:
        name = (aff.get("name") or "").strip()
        if not name:
            continue
        affiliate_id = _upsert_entity_by_name(name=name, entity_type="company",
                                              cik=aff.get("cik"), source_id=source_id)
        if affiliate_id == filer_id:
            continue                      # a filer listing itself
        _upsert_affiliate(filer_id, affiliate_id, source_id,
                          source_url=aff.get("source_url"), source_date=aff.get("source_date"))
        written += 1
    return written


@_with_autodedup
def run_sec_13f(company: str, limit: int = 100, window_days: int = 135,
                force: bool = False) -> dict:
    """Ingest one issuer's institutional holders from Form 13F info tables.

    The inverse question to `run_sec_holdings` (what a manager holds): who holds
    this company. 13F fills the sub-5% blind spot — 13D/G only exists above the
    threshold, so Nvidia's 0.9% of SpaceX can never appear there, but it is in
    every quarter's 13F.

    Enriches, does not discover: the company must already be in the graph (any
    source), because the write needs a node to point at and the matching needs
    its names. Percentages are computed, never transcribed — 13F reports counts
    and dollars, and stake = shares / shares outstanding via `_pct_of`, with its
    precision floor (a real but tiny position gets no percentage rather than a
    false 0.0%). votingAuthority is deliberately NOT written: it states the
    manager's authority over its own held shares, not a bloc's share of the
    issuer's votes, and mapping it onto `voting_power_pct` would mark every
    index fund as a voting bloc.

    The discovered CUSIP is stamped on the company (fill-if-missing), so the
    next run matches info-table rows exactly instead of by name.
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError("SEC EDGAR scraper is disabled. "
                              "Set SCRAPER_SEC_EDGAR_ENABLED=true to enable.")

    from app.routers.search import resolve_best_entity
    from app.scraper.run_log import record_run
    from app.scraper.sec_edgar import (_cik10, _pct_of, fetch_13f_holders,
                                       fetch_shares_outstanding,
                                       latest_13f_deadline, next_13f_deadline)
    from app.scraper.sec_writer import mark_13f_stale

    entity = resolve_best_entity(company, None)
    if not entity:
        return {"status": "no_results", "company": company, "total": 0, "scraped": []}
    company_id = entity["id"]
    names = [n for n in [entity.get("name"), *(entity.get("aliases") or [])] if n]

    # 13F comes AFTER the schedules scrape, by design: that scrape stamps the
    # CIK (the denominator lookup needs it) and harvests the CUSIP that lets
    # info-table rows match exactly instead of by name.
    if not entity.get("sec_cik"):
        return {"status": "needs_sec_scrape", "company": company,
                "entity_id": company_id, "total": 0,
                "detail": "The entity has no SEC CIK yet — run the SEC EDGAR "
                          "scrape (13D/G + Form 4) first."}

    with db.get_session() as session:
        row = session.run("MATCH (e:Entity {id: $id}) "
                          "RETURN e.sec_13f_scraped_at AS at",
                          id=company_id).single()
    last_run = row.get("at") if row else None

    # Quarterly by deadline, not by TTL: 13Fs are due 45 days after quarter
    # end, so a re-run before the next deadline reads the same filings. Fresh
    # means no new deadline has passed since the last run — the day after a
    # deadline the gate opens by itself, however recent the last run was.
    if not force and last_run:
        today = datetime.now(timezone.utc).date()
        if datetime.fromisoformat(last_run).date() >= latest_13f_deadline(today):
            return {"status": "fresh", "company": company,
                    "entity_id": company_id, "total": 0, "last_run": last_run,
                    "next_deadline": next_13f_deadline(today).isoformat()}

    # A refresh reads only what is NEW since the last completed run (plus a
    # week for stragglers), not the full discovery window: the older filings
    # were already ingested, and re-reading them cannot change an edge —
    # newest-per-filer wins regardless. min(), never max(): an explicit
    # wider window (or 0 = all time) stays what the caller asked for.
    if last_run:
        days_since = (datetime.now(timezone.utc)
                      - datetime.fromisoformat(last_run)).days
        window_days = min(window_days, days_since + 7)

    with record_run("sec-13f", company) as run:
        source_id = _ensure_source(SEC_EDGAR_SOURCE_NAME, SEC_EDGAR_SOURCE_URL,
                                   SEC_EDGAR_CREDIBILITY)
        data = fetch_13f_holders(entity.get("name") or company, known_names=names,
                                 cusip=entity.get("cusip"), limit=limit,
                                 window_days=window_days)

        # The denominator, once per run. Padded: the companyconcept endpoint
        # 404s an unpadded CIK. A company with no XBRL (SpaceX is private)
        # yields None and every stake stays a share count without a percentage
        # — which is honest, and the UI shows the counts.
        outstanding = None
        if entity.get("sec_cik"):
            outstanding = fetch_shares_outstanding(_cik10(entity["sec_cik"]))

        if data["cusip_seen"] and not entity.get("cusip"):
            with db.get_session() as session:
                session.run("MATCH (e:Entity {id: $id}) "
                            "SET e.cusip = COALESCE(e.cusip, $c)",
                            id=company_id, c=data["cusip_seen"])

        written = 0
        for h in data["holders"]:
            filer_id = _upsert_entity_by_name(
                name=h["filer_name"], entity_type="company",
                cik=h["filer_cik"], source_id=source_id)
            if not filer_id:
                continue
            pct = _pct_of(h["shares"], outstanding)
            _upsert_owns_sec(
                owner_id=filer_id, owned_id=company_id, source_id=source_id,
                ownership_type=(derive_ownership_type(pct) if pct is not None
                                else "minority"),
                file_date=h.get("period"),
                stake_percent=pct,
                shares=h["shares"], shares_outstanding=outstanding,
                share_class=h.get("share_class"), value_usd=h.get("value_usd"),
                filing_type="13F",
                source_url=h.get("source_url"))
            written += 1

        # Dim what this quarter did not confirm — only when the run actually
        # ingested a period, so an empty refresh window dims nothing.
        stale_marked = 0
        if written and data.get("period"):
            stale_marked = mark_13f_stale(company_id, data["period"])

        # The quarterly gate's date — stamped only on a completed run, so a
        # crashed run never counts as fresh.
        with db.get_session() as session:
            session.run("MATCH (e:Entity {id: $id}) SET e.sec_13f_scraped_at = $now",
                        id=company_id, now=datetime.now(timezone.utc).isoformat())

        run["total"] = written
        log.info("SEC 13F: %d holder edges for %r (%d/%d filings read)",
                 written, company, data["filings_fetched"], data["filings_total"])
        return {"status": "ok", "company": company, "entity_id": company_id,
                "total": written, "stale_marked": stale_marked,
                "cusip": data["cusip_seen"],
                "period": data["period"], "shares_outstanding": outstanding,
                "filings_fetched": data["filings_fetched"],
                "filings_total": data["filings_total"]}


def run_sec_holdings(cik: str, limit: int = 100, succeeds_cik: str | None = None) -> dict:
    """Ingest what one SEC filer owns — the 13D/13G stakes it discloses in others.

    Keyed on CIK rather than a name because that is what identifies a filer, and
    because the company this matters for is often not findable by name: Vanguard's
    live book sits under VANGUARD CAPITAL MANAGEMENT LLC (0002100119), not the
    VANGUARD GROUP INC (0000102909) most people would search for.

    ``succeeds_cik`` records that this filer took over from another — a
    SUCCEEDED_BY edge, predecessor → successor. Given explicitly rather than
    inferred: filing patterns can suggest a handover, but asserting a corporate
    relationship from them would be a guess written into the graph as fact.
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise PermissionError("SEC EDGAR scraper is disabled. "
                              "Set SCRAPER_SEC_EDGAR_ENABLED=true to enable.")

    from app.scraper.sec_edgar import (fetch_filer_country, fetch_filer_headquarters,
                                       fetch_filer_holdings, fetch_filer_name,
                                       fetch_filer_website)

    filer_name = fetch_filer_name(cik)
    if not filer_name:
        return {"status": "no_results", "cik": cik, "total": 0, "scraped": []}

    source_id = _ensure_source(SEC_EDGAR_SOURCE_NAME, SEC_EDGAR_SOURCE_URL, SEC_EDGAR_CREDIBILITY)
    # Free: the submissions document was already fetched for the name and is cached.
    filer_id = _upsert_entity_by_name(name=filer_name, entity_type="company",
                                      cik=cik, source_id=source_id,
                                      country=fetch_filer_country(cik),
                                      headquarters=fetch_filer_headquarters(cik),
                                      website=fetch_filer_website(cik))

    holdings = fetch_filer_holdings(cik, limit=limit)
    written = closed = 0
    for h in holdings:
        subject_name = (h.get("subject_name") or "").strip()
        if not subject_name:
            continue
        subject_id = _upsert_entity_by_name(
            name=subject_name, entity_type="company", cik=h.get("subject_cik"),
            source_id=source_id,
            country=fetch_filer_country(h["subject_cik"]) if h.get("subject_cik") else None,
            headquarters=fetch_filer_headquarters(h["subject_cik"]) if h.get("subject_cik") else None,
            website=fetch_filer_website(h["subject_cik"]) if h.get("subject_cik") else None)
        _upsert_owns_sec(
            owner_id=filer_id, owned_id=subject_id, source_id=source_id,
            ownership_type="minority", file_date=h.get("file_date"),
            stake_percent=h.get("stake_percent"), source_url=h.get("source_url"),
            voting_power_pct=h.get("voting_power_pct"), until=h.get("until"),
            filing_type=h.get("filing_type"),
        )
        written += 1
        if h.get("until"):
            closed += 1

    # Group structure from the 13F cover page — one extra request, and a far
    # better signal than name matching.
    from app.scraper.sec_edgar import fetch_affiliated_managers
    affiliates = _write_affiliates(filer_id, fetch_affiliated_managers(cik), source_id)

    succession = None
    if succeeds_cik:
        pred_name = fetch_filer_name(succeeds_cik)
        if pred_name:
            pred_id = _upsert_entity_by_name(name=pred_name, entity_type="company",
                                             cik=succeeds_cik, source_id=source_id)
            _upsert_succession(pred_id, filer_id, source_id,
                               credibility_score=SEC_EDGAR_CREDIBILITY)
            succession = {"predecessor": pred_name, "successor": filer_name}

    log.info("SEC EDGAR: %s — %d holdings written (%d already ended)",
             filer_name, written, closed)
    return {
        "status": "ok", "cik": cik, "filer": filer_name,
        "total": written, "ended": closed, "affiliates": affiliates,
        "succession": succession,
        "scraped": [{"type": "entity", "name": h["subject_name"], "role": "holding"}
                    for h in holdings],
    }


def run_scrape_sec_edgar(company_name: str, country: str | None = None) -> dict:
    """
    Scrape SEC EDGAR for ownership and executive data about one company.
    Requires SCRAPER_ENABLED=true AND SCRAPER_SEC_EDGAR_ENABLED=true.

    With `country`, a filer registered elsewhere is rejected before anything is
    written.

    Checked afterwards rather than asked for up front, unlike Wikidata and
    OpenCorporates, because EDGAR's search-side `State=` filter is the wrong
    field — it matches the *business address*, which for a foreign filer is
    usually its US filing office. Deutsche Bank AG lists New York and states no
    incorporation at all; Siemens AG states Germany and gives no address country.
    Filtering the search by that field would hide both from a German search. So
    the single match EDGAR returns is judged on `stateOfIncorporation`, which is
    the field that answers the question — and a filer that states none is not an
    answer to "in Germany".
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
    from app.scraper.sec_edgar import (fetch_filer_country, fetch_filer_headquarters,
                                       fetch_filer_website, scrape_company)

    log.info("SEC EDGAR runner: starting scrape for %r", company_name)
    data = scrape_company(company_name)

    if not data:
        return {
            "status":  "no_results",
            "company": company_name,
            "total":   0,
            "scraped": [],
        }

    # The submissions document was already fetched for former names and the LEI,
    # and is cached, so this costs nothing — and it settles the country question
    # before the first write.
    filer_country = fetch_filer_country(data["cik"]) if data.get("cik") else None
    if not matches_requested(filer_country, country):
        log.info("SEC EDGAR: %r is registered in %s, not %s — skipping",
                 data["name"], filer_country, country)
        return country_mismatch(company_name, filer_country, country)

    source_id = _ensure_source(SEC_EDGAR_SOURCE_NAME, SEC_EDGAR_SOURCE_URL, SEC_EDGAR_CREDIBILITY)
    scraped: list[dict] = []

    # Upsert the target company
    target_id = _upsert_entity_by_name(
        name=data["name"],
        entity_type="company",
        cik=data.get("cik"),
        source_id=source_id,
        former_names=data.get("former_names"),
        lei=data.get("lei"),
        country=filer_country,
        # Where it is RUN, from the same cached document — kept apart from
        # `country`, which is where it is registered.
        headquarters=fetch_filer_headquarters(data["cik"]) if data.get("cik") else None,
        website=fetch_filer_website(data["cik"]) if data.get("cik") else None,
    )
    scraped.append({"type": "entity", "name": data["name"], "role": "target"})

    # Ownership filings → investor nodes + OWNS edges
    # CUSIP from the first schedule that states one — fill-if-missing, so a
    # hand-set or 13F-adopted value is never clobbered. This is what lets
    # `sec-13f` match info-table rows exactly instead of by name.
    cusip = next((f.get("issuer_cusip") for f in data.get("ownership_filings", [])
                  if f.get("issuer_cusip")), None)
    if cusip:
        with db.get_session() as session:
            session.run("MATCH (e:Entity {id: $id}) SET e.cusip = COALESCE(e.cusip, $c)",
                        id=target_id, c=cusip)

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

        # A group of parties acting together, filed on one schedule. The bloc
        # belongs to the group, not to whoever submitted the form — hanging
        # 52.3% off BRC said BRC votes it, when in truth nine parties do and BRC
        # merely filed. Only Schedule 13D: a 13G reporting "shared voting power"
        # is an asset manager aggregating across its own subsidiaries (State
        # Street, Morgan Stanley), which is not a governance bloc at all.
        members = filing.get("group_members") or []
        if members and _is_control_filing(filing.get("form_type")):
            roster = [_member_key(investor_name, filing.get("investor_cik"))]
            roster += [_member_key(m["name"], m.get("cik")) for m in members]
            group_id = _upsert_voting_group(
                subject_id=target_id, subject_name=data["name"], roster=roster,
                source_id=source_id)

            # The filer joins as a member like everybody else; it gets no edge
            # of its own, or the company would list both it and the group.
            _upsert_group_membership(investor_node_id, group_id,
                                     "Person" if is_individual else "Entity", source_id)
            for m in members:
                m_individual = (m.get("type_code") in _INDIVIDUAL_CODES
                                if m.get("type_code") else is_person_name(m["name"]))
                if m_individual:
                    mid = _upsert_person_by_name(m["name"], source_id=source_id)
                    scraped.append({"type": "person", "name": m["name"], "role": "group member"})
                else:
                    mid = _upsert_entity_by_name(name=m["name"], entity_type="company",
                                                 cik=m.get("cik"), source_id=source_id)
                    scraped.append({"type": "entity", "name": m["name"], "role": "group member"})
                _upsert_group_membership(mid, group_id,
                                         "Person" if m_individual else "Entity", source_id)

            _upsert_owns_sec(
                owner_id=group_id, owned_id=target_id, source_id=source_id,
                ownership_type=filing.get("ownership_type", "unknown"),
                file_date=filing.get("file_date"),
                # No stake: the group's members hold the shares individually, and
                # a bloc percentage summed with theirs would exceed the company.
                stake_percent=None,
                voting_power_pct=filing.get("voting_power_pct") or filing.get("stake_percent"),
                # The bloc's count belongs on the group's edge above all: this
                # is the one place it is not a number repeated by nine parties.
                voting_shares=filing.get("voting_shares"),
                share_class=filing.get("share_class"),
                filing_type=filing.get("filing_type"),
                source_url=filing.get("source_url"),
            )
            # Retire the edge this filing used to produce. Before groups existed
            # the bloc was written straight onto the filer, and that row is not
            # merely stale — it is the same filing, now represented by the group,
            # so leaving it would show the company both its group and BRC each
            # voting 52.3%. Scoped hard: same source, same filer, no stake of its
            # own, so a member's genuine holding is untouched.
            _retire_superseded_bloc_edge(investor_node_id, target_id, source_id,
                                         "Person" if is_individual else "Entity")
            log.info("SEC EDGAR: wrote voting group of %d over %r (filed by %r)",
                     len(roster), data["name"], investor_name)
            continue

        _upsert_owns_sec(
            owner_id=investor_node_id,
            owned_id=target_id,
            source_id=source_id,
            ownership_type=filing.get("ownership_type", "unknown"),
            file_date=filing.get("file_date"),
            stake_percent=filing.get("stake_percent"),
            voting_power_pct=filing.get("voting_power_pct"),
            share_class=filing.get("share_class"),
            shares=filing.get("shares"),
            shares_outstanding=filing.get("shares_outstanding"),
            voting_shares=filing.get("voting_shares"),
            source_url=filing.get("source_url"),
            filing_type=filing.get("filing_type"),
            # A newer filing reported no position — the holding is history.
            # The holdings path below has always done this; this one did not.
            until=filing.get("until"),
            owner_label="Person" if is_individual else "Entity",
        )
        log.info(
            "SEC EDGAR: wrote OWNS %r → %r (%s)",
            investor_name, data["name"], filing.get("form_type"),
        )

    # Holdings → OWNS edges pointing OUT of this company.
    #
    # The mirror of the ownership_filings loop above. That one reads 13D/13G
    # filings naming this company as the subject (who owns it); this reads the
    # ones it FILES about others (what it owns). An asset manager has no filings
    # about itself — Vanguard is privately held and isn't a listed issuer — so
    # without this its node stays empty no matter how often it is scraped.
    for holding in data.get("holdings", []):
        subject_name = (holding.get("subject_name") or "").strip()
        if not subject_name:
            continue
        subject_id = _upsert_entity_by_name(
            name=subject_name,
            entity_type="company",
            cik=holding.get("subject_cik"),
            source_id=source_id,
        )
        scraped.append({"type": "entity", "name": subject_name, "role": "holding"})
        _upsert_owns_sec(
            owner_id=target_id,
            owned_id=subject_id,
            source_id=source_id,
            ownership_type=holding.get("ownership_type", "minority"),
            file_date=holding.get("file_date"),
            stake_percent=holding.get("stake_percent"),
            source_url=holding.get("source_url"),
            # Set when a later amendment reported 0% — the filer has dropped
            # below the 5% threshold, so this is history, not a live position.
            until=holding.get("until"),
        )
        log.info("SEC EDGAR: wrote OWNS %r → %r (%s%%%s)",
                 data["name"], subject_name, holding.get("stake_percent"),
                 f", ended {holding['until']}" if holding.get("until") else "")

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
                # Form 4 states the holding exactly; until now it decided
                # whether to write an edge and was then thrown away.
                shares=shares, filing_type="Form 4",
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
                shares=holding.get("shares_owned"),
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
    # Mark the target company as on-demand scraped. SEC is depth-blind, so stamp depth 0
    # (the freshness stamp uses max(), so this never lowers a deeper Wikidata pass).
    if target_id:
        set_scrape_target(target_id, 0)

    return {
        "status":    "ok",
        "company":   company_name,
        "cik":       data.get("cik"),
        "entity_id": target_id,
        "total":     len(scraped),
        "scraped":   scraped,
    }


# ── Scraping a person ─────────────────────────────────────────────────────────

def _person_freshness(person_id: str, session) -> None:
    """Stamp a scraped person the way `set_scrape_target` stamps a company, so the
    on-demand freshness gate can tell an enriched person from an untouched one."""
    session.run(
        "MATCH (p:Person {id: $id}) SET p.on_demand_scraped = true, "
        "p.last_scraped_at = $now, p.scrape_depth = 1",
        id=person_id, now=_now_iso(),
    )


@_with_autodedup
def run_scrape_person(query: str, country: str | None = None) -> dict:
    """Scrape a PERSON: who they are, and the companies they run, founded or own.

    The company scrape reads a company and finds its people. This is the other
    direction, and it exists because searching a person's name used to do
    something worse than nothing: the top Wikidata hit for "Larry Page" is the
    man, and he was written into the graph as a company.

    Wikidata records these links only from the company side, so the lookup is a
    reverse one, and its results need filtering — "founded by" is used loosely
    enough to include buildings, software and, in Elon Musk's case, a car and an
    aeroplane. `looks_like_a_company` decides; see it for why.
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true to enable.")
    if not settings.SCRAPER_WIKIDATA_ENABLED:
        raise PermissionError("Wikidata scraper is disabled.")
    if not get_source_enabled("wikidata"):
        raise PermissionError("Wikidata source is disabled. Enable it in the Scraper panel.")

    from app.scraper.wikidata import (OWNER_ROLE, fetch_person_companies,
                                      fetch_person_details_for)

    results = (search_entity_in_country(query, country) if country
               else search_entity(query, limit=3))
    if not results:
        return {"status": "no_results", "query": query, "total": 0, "scraped": [],
                "requested_country": country}

    # The same reason in the other direction: the man is the third hit for his
    # own name, so the first is not the one to ask about.
    qid = pick_candidate(results, "person")
    detail = fetch_person_details_for(qid) if qid else None
    if not detail or not detail.get("is_human"):
        # Not a person — the company path handles this, and guessing here would
        # write a company into the person shape.
        return {"status": "not_a_person", "query": query, "qid": qid, "total": 0, "scraped": []}

    source_id = _ensure_source(WIKIDATA_SOURCE_NAME, WIKIDATA_SOURCE_URL,
                               WIKIDATA_CREDIBILITY, "knowledge_base")
    chosen = next((r for r in results if r["id"] == qid), None) or {}
    person_id = _upsert_person(
        full_name=detail.get("full_name") or chosen.get("label") or query,
        nationality=None, description=detail.get("description"), wikidata_id=qid,
        birth_date=detail.get("birth_date"), death_date=detail.get("death_date"),
        birth_place=detail.get("birth_place"), aliases=detail.get("aliases"),
        nationalities=detail.get("nationalities"), source_id=source_id,
    )

    scraped: list[dict] = [{"type": "person", "name": detail.get("full_name"), "role": "target"}]
    for link in fetch_person_companies(qid):
        if not link["is_company"]:
            log.info("Wikidata person scrape: skipping %r — not a company", link["name"])
            continue
        entity_id = _upsert_entity(
            name=link["name"], entity_type=infer_entity_type(link["instances"]),
            country=link.get("country"), founded=None, revenue=None, description=None,
            wikidata_id=link["qid"], source_id=source_id,
        )
        for role in link["roles"]:
            if role == OWNER_ROLE:
                _upsert_owns(owner_id=person_id, owned_id=entity_id, source_id=source_id,
                             owner_label="Person", credibility_score=WIKIDATA_CREDIBILITY)
            else:
                _upsert_role(person_id, entity_id, role, source_id,
                             credibility_score=WIKIDATA_CREDIBILITY)
        scraped.append({"type": "entity", "name": link["name"], "role": ", ".join(link["roles"])})

    with db.get_session() as session:
        _person_freshness(person_id, session)

    return {"status": "ok", "query": query, "person_id": person_id, "qid": qid,
            "total": len(scraped), "scraped": scraped}


# ── Run-all entry point ───────────────────────────────────────────────────────

@_with_autodedup
def run_scrape_all(query: str, depth: int = 2, country: str | None = None) -> dict:
    """
    Run all enabled scrapers for a given company name.
    Each scraper that is disabled is skipped silently; its key in the result
    will have status 'disabled'. `country` (ISO-2 or None) is handed to every
    source, which rejects a match that is demonstrably somewhere else.
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
            results[spec.name] = spec.run(query, depth, country)
        except PermissionError as exc:
            results[spec.name] = {"status": "disabled", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - one scraper failing mustn't sink the rest
            log.error("%s scrape failed for %r: %s", spec.name, query, exc)
            results[spec.name] = {"status": "error", "detail": str(exc)}

    return {"status": "ok", "query": query, "results": results}


# ── OpenCorporates helpers ────────────────────────────────────────────────────

def _upsert_role_oc(person_id: str, entity_id: str, role: str,
                    start_date: str | None, end_date: str | None,
                    source_id: str, source_url: str | None = None, credibility_score: int = 85):
    """Create a HAS_ROLE edge attributed to OpenCorporates if not already present.

    Stamps per-entry provenance: source_url = the OpenCorporates company page,
    source_date = the officer's start date, last_scraped_at = now (refreshed on
    re-scrape).
    """
    record_claim(kind=KIND_ROLE, from_id=person_id, to_id=entity_id, source_id=source_id,
                 role=role, since=start_date, until=end_date, source_url=source_url,
                 credibility_score=credibility_score)
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
def run_scrape_open_corporates(company_name: str, country: str | None = None) -> dict:
    """
    Scrape OpenCorporates for registration details and officers for one company.
    Requires SCRAPER_ENABLED=true AND SCRAPER_OPENCORPORATES_ENABLED=true.

    `country` is passed to the API as `jurisdiction_code`, so the search runs
    inside that country. The returned code is still checked — one line, no extra
    call — because a filter that silently stops working is the kind of thing that
    is only noticed once wrong data is in the graph.
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
    data = scrape_company(company_name, country)

    if not data:
        return {
            "status":  "no_results",
            "company": company_name,
            "total":   0,
            "scraped": [],
        }

    oc_country = (data.get("jurisdiction_code") or "")[:2].upper() or None
    if not matches_requested(oc_country, country):
        log.info("OpenCorporates: %r is registered in %s, not %s — skipping",
                 data.get("name"), oc_country, country)
        return country_mismatch(company_name, oc_country, country)

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
    _stamp_registration(target_id, data.get("jurisdiction_code"), data.get("company_number"))
    scraped.append({"type": "entity", "name": data["name"], "role": "target"})

    # Registered address → geocoded onto the entity itself.
    address = data.get("registered_address") or {}
    if address.get("city") or address.get("country"):
        _geocode_registered_and_attach(target_id, address)
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
                      bulk_load: bool = False, batch_size: int = 400,
                      only_companies: set[str] | None = None,
                      digest_out: str | None = None) -> dict:
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
        only_companies=only_companies,
        digest_out=digest_out,
    )
    # Stamp the baseline marker the incremental refresh rides on. Narrowing by
    # --limit or --only means the graph holds part of the register, so a refresh
    # must run in only-existing mode; recording *which* lets the refusal say why
    # rather than claim PSC was never loaded.
    from app.scraper.ch_psc_incremental import (
        mark_psc_load_done, snapshot_date, snapshot_entry, write_last_snapshot,
    )
    mark_psc_load_done("subset" if (limit or only_companies) else "full")
    # A digest written here IS the baseline for this snapshot, so record which
    # snapshot that was. Without it the first refresh cannot tell how big a gap it
    # is covering — it assumed one day, so a month's legitimate changes would be
    # measured against a single day's allowance — and the staleness guard has
    # nothing to compare against, leaving an older snapshot applyable over a newer
    # baseline. Found by running the real thing: it reported gap_days 1 for 24.
    if digest_out:
        entry = (snapshot_entry(zipfile.ZipFile(local_file))
                 if local_file.lower().endswith(".zip") else os.path.basename(local_file))
        write_last_snapshot(snapshot_date(entry), counts.get("digest_records", 0))
    return {"status": "ok", "source": UK_PSC_SOURCE_NAME, **counts,
            **_post_bods_import()}


def run_ch_psc_update(local_file: str, digest: str | None = None,
                      limit: int | None = None, batch_size: int = 1000,
                      only_existing: bool | None = None, max_churn_pct: float = 5.0,
                      force: bool = False, dry_run: bool = False,
                      rebuild_digest: bool = False) -> dict:
    """
    Apply a Companies House PSC snapshot **incrementally**, by diffing it against
    the digest of the last one applied (see `app/scraper/ch_psc_incremental.py`).

    Companies House publishes no delta feed — only a full snapshot, overwritten
    daily — so the delta is computed locally. A snapshot is a complete state, so
    there is no catch-up window to miss: diffing against a week-old digest simply
    yields a week's changes.

    Deliberately **not scheduled**. It is driven by hand until it has proved itself
    over several real snapshots; `--dry-run` and the churn guard exist to make those
    runs legible.
    """
    if not settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable.")
    if not settings.SCRAPER_BODS_UK_PSC_ENABLED:
        raise PermissionError(
            "UK PSC scraper is disabled. "
            "Set SCRAPER_BODS_UK_PSC_ENABLED=true in the environment to enable.")

    from app.db.schema import ensure_indexes
    from app.scraper import ch_psc_incremental as inc
    from app.scraper.run_log import record_run

    # Idempotent, and it is what creates the OWNS.psc_self_link index the whole
    # write path matches on — a refresh against a database that never had it would
    # full-scan every edge, per batch.
    ensure_indexes()

    digest = digest or inc.default_digest_path(local_file)
    entry = inc.snapshot_entry(zipfile.ZipFile(local_file)) if local_file.lower().endswith(".zip") \
        else os.path.basename(local_file)
    snap_date = inc.snapshot_date(entry)

    if rebuild_digest:
        # Escape hatch: re-establish the baseline digest without touching the graph.
        # Loud, because it silently forfeits whatever changed since the last one.
        log.warning("CH PSC: rebuilding the digest from %s — this FORFEITS any changes "
                    "since the last applied snapshot", entry)
        counts = inc.write_digest(local_file, digest, limit=limit)
        inc.write_last_snapshot(snap_date, counts["records"])
        return {"status": "ok", "source": UK_PSC_SOURCE_NAME, "rebuilt": True, **counts}

    scope = inc.psc_load_scope()
    if scope is None:
        raise RuntimeError(
            "No Companies House PSC load found — the refresh rides on top of a full "
            "snapshot import. Run `manage.py ch-psc --file … --digest-out …` first.")
    if not os.path.exists(digest):
        raise RuntimeError(
            f"No baseline digest at {digest} — it is written by `ch-psc --digest-out`, "
            "or rebuild one with `ch-psc-update --rebuild-digest` (which forfeits a day).")

    source_id = _ensure_source(UK_PSC_SOURCE_NAME, UK_PSC_SOURCE_URL, BODS_UK_PSC_CREDIBILITY)
    with record_run("ch-psc-update", snap_date) as run:
        # A subset baseline holds part of the register, so refreshing it whole would
        # not update it — it would drag the rest of the UK in. Same policy as GLEIF.
        only_existing = scope != "full" if only_existing is None else only_existing
        if only_existing:
            run["note"] = ("only-existing mode: refreshing the companies this database "
                           "already holds, ignoring the rest of the register")

        last = inc.read_last_snapshot() or {}
        if last.get("projection_version") not in (None, inc.PROJECTION_VERSION) and not force:
            raise RuntimeError(
                f"the stored digest was built by projection v{last.get('projection_version')}, "
                f"this code is v{inc.PROJECTION_VERSION} — every digest is invalidated by "
                "that change. Rebuild with --rebuild-digest.")
        if last.get("snapshot_date") and snap_date <= last["snapshot_date"] and not force:
            return {"status": "skipped", "source": UK_PSC_SOURCE_NAME,
                    "reason": f"snapshot {snap_date} is not newer than the last applied "
                              f"{last['snapshot_date']}"}

        new_digest = inc.new_digest_tempfile(digest)
        log.info("CH PSC refresh: digesting %s", entry)
        dcounts = inc.write_digest(local_file, new_digest, limit=limit)
        diff = inc.diff_digests(digest, new_digest)
        gap = inc.days_since(last.get("snapshot_date"))
        ok, why = inc.churn_allowed(diff, max_churn_pct, gap)
        summary = {"snapshot_date": snap_date, "added": diff.added, "changed": diff.changed,
                   "vanished": len(diff.vanished), "churn_pct": round(inc.churn_pct(diff), 3),
                   "records": dcounts["records"], "gap_days": gap}
        if not ok and not force:
            os.unlink(new_digest)
            raise RuntimeError(f"CH PSC refresh refused: {why}")
        if dry_run:
            os.unlink(new_digest)
            run["total"] = diff.total
            return {"status": "dry-run", "source": UK_PSC_SOURCE_NAME, **summary}

        applied = inc.apply_diff(local_file, diff, source_id, BODS_UK_PSC_CREDIBILITY,
                                 until_date=snap_date, only_existing=only_existing,
                                 batch_size=batch_size)
        # Only now: a crash before this leaves tomorrow's diff a superset of today's,
        # which is redone idempotently. Rotating first would lose it outright.
        inc.rotate_digest(new_digest, digest)
        inc.write_last_snapshot(snap_date, dcounts["records"])
        run["total"] = diff.total

    return {"status": "ok", "source": UK_PSC_SOURCE_NAME, **summary, **applied}


def run_import_basic_company_data(local_file: str, limit: int | None = None,
                                  bulk_load: bool = False, batch_size: int = 400,
                                  only_companies: set[str] | None = None) -> dict:
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
        only_companies=only_companies,
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
                             bulk_load: bool = False,
                             only_leis: set[str] | None = None) -> dict:
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
        only_leis=only_leis,
    )
    # Stamp the baseline marker so the incremental `gleif-update` knows whether the
    # full load has run (it refuses to apply deltas onto an un-baselined graph).
    # Narrowing the import by ANY of these means the entity baseline is partial, and
    # a delta — which carries every record changed worldwide — would not refresh that
    # subset but bury it. This used to stamp "full" unconditionally, so the curated
    # test import re-enabled the nightly delta against a 488-entity database.
    from app.scraper.gleif_incremental import mark_full_load_done
    mark_full_load_done("subset" if (only_leis or limit or filter_jurisdiction) else "full")
    return {"status": "ok", "source": GLEIF_SOURCE_NAME, **counts,
            "duplicate_names": _duplicate_name_summary()}


def run_import_gleif_repex(local_file: str, limit: int | None = None) -> dict:
    """
    Import GLEIF reporting exceptions — the published reasons companies give for
    naming no parent (see `app/scraper/gleif_repex.py`). Writes onto entities this
    database already holds and creates none, so it is safe to run against a
    curated subset as well as a full load. Reuses the GLEIF source + flags.
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

    from app.scraper.gleif_repex import import_repex

    # The source node is ensured for consistency with the other GLEIF imports, even
    # though an exception is a property on an existing entity rather than a new
    # node or edge — the provenance is the entity's, already stamped by LEI-CDF.
    _ensure_source(GLEIF_SOURCE_NAME, GLEIF_SOURCE_URL, BODS_GLEIF_CREDIBILITY)
    log.info("GLEIF repex: importing from %s (limit=%s)", local_file, limit)
    counts = import_repex(filepath=local_file, limit=limit)
    return {"status": "ok", "source": GLEIF_SOURCE_NAME, **counts}


def run_import_gleif_rr(local_file: str, limit: int | None = None,
                        only_leis: set[str] | None = None,
                        emit_leis_path: str | None = None) -> dict:
    """
    Import GLEIF RR-CDF (Level 2) direct/ultimate consolidation parents as
    direct/indirect OWNS edges. Reuses the GLEIF source + flags. `local_file` is a
    pre-downloaded RR-CDF golden-copy .json/.zip. Checks SCRAPER_ENABLED and
    SCRAPER_BODS_GLEIF_ENABLED.

    `only_leis` restricts to the corporate family of those seed LEIs (test subset);
    `emit_leis_path` writes the family's LEIs for a follow-up entity-naming pass.
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
        only_leis=only_leis,
        emit_leis_path=emit_leis_path,
    )
    # RR consolidation edges overlap the GLEIF parent edges from the BODS import,
    # so collapse the duplicates automatically (keeps the direct/indirect-flagged
    # edge) — no separate "remember to dedup" step.
    log.info("GLEIF RR-CDF: deduplicating overlapping OWNS edges")
    dedup = deduplicate_owns_edges()
    return {"status": "ok", "source": GLEIF_SOURCE_NAME, **counts, "edge_dedup": dedup}


def run_gleif_update(interval: str = "auto", lei_file: str | None = None,
                     rr_file: str | None = None, limit: int | None = None,
                     only_existing: bool | None = None,
                     repex_file: str | None = None) -> dict:
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
    pre-downloaded `lei_file`/`rr_file`/`repex_file` to skip the fetch.
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

    from app.scraper.gleif_repex import import_repex
    from app.scraper.gleif_incremental import (
        choose_catchup_interval,
        downloaded_deltas,
        fetch_publish_metadata,
        load_scope,
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
    # ExitStack so the downloaded deltas (when we fetch any) are removed however
    # the run ends, without the happy path having to nest another `with`.
    with ExitStack() as stack, record_run("gleif-update", interval) as run:
        # The delta rides on top of the full golden copy. A delta carries every
        # record GLEIF changed worldwide, so applying it to anything less than that
        # baseline does not refresh the graph — it floods it.
        scope = load_scope()
        if scope is None:
            raise RuntimeError(
                "No GLEIF load found — the incremental update rides on top of the "
                "full golden copy. Run the full load first (full-import.sh / "
                "`manage.py gleif-lei-cdf`), then re-run.")
        # A subset baseline still gets its delta — that is how a curated database
        # stays current, and how the delta path itself gets exercised — but in
        # only-existing mode: refresh the companies that are here, ignore the rest
        # of the world. Applying a delta whole to a subset does not refresh it.
        only_existing = scope != "full" if only_existing is None else only_existing
        if only_existing:
            run["note"] = ("only-existing mode: refreshing the companies this database "
                           "already holds, ignoring records for the rest of the world")
            log.info("GLEIF update: %s", run["note"])
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
            # Held open across the whole apply below: the files have to survive
            # until they are read, and be gone once they are. `download_deltas`
            # alone leaks its temp directory, which is what it had been doing here
            # nightly for months.
            fetched = stack.enter_context(downloaded_deltas(publish, resolved))
            lei_file, rr_file = fetched["lei2"], fetched["rr"]
            repex_file = repex_file or fetched.get("repex")

        log.info("GLEIF update: applying LEI-CDF delta %s", lei_file)
        lei = import_lei_cdf_delta(lei_file, source_id, BODS_GLEIF_CREDIBILITY, limit=limit,
                                   only_existing=only_existing)
        log.info("GLEIF update: applying RR delta %s", rr_file)
        rr = import_rr_delta(rr_file, source_id, BODS_GLEIF_CREDIBILITY, limit=limit,
                             only_existing=only_existing)
        # Reporting exceptions last, so a company that gained a real parent in this
        # same delta is written before the reason it once had none. The importer
        # only ever updates entities that exist, so it needs no only_existing mode:
        # a statement about a company we do not carry lands nowhere by construction.
        repex = {}
        if repex_file:
            log.info("GLEIF update: applying repex delta %s", repex_file)
            repex = import_repex(repex_file, limit=limit)
        run["total"] = lei["updated"] + rr["created"] + rr["closed"]
        # Advance the checkpoint only after a clean apply, and only when we fetched a
        # published delta in full (not local files, not a --limit spot check).
        if current_publish and not limit:
            write_last_publish(current_publish)

    return {"status": "ok", "source": GLEIF_SOURCE_NAME, "interval": resolved,
            "publish_date": current_publish, "lei_cdf": lei, "rr": rr, "repex": repex}


# ── Scraper registry ──────────────────────────────────────────────────────────
# Register the built-in scrapers so run_scrape_all (and, later, the router) can
# iterate them. A new scraper registers its own ScraperSpec — no dispatch edits.
# One declaration per source. enabled() derives from the settings flag + the
# per-source DB toggle; label/credibility/url come from the KNOWN_SOURCES entry
# via spec.meta; register() validates all of it loudly at import time.
register(ScraperSpec("wikidata", lambda q, d, c=None: run_scrape(q, d, c),
                     depth_aware=True))
register(ScraperSpec("sec_edgar", lambda q, d, c=None: run_scrape_sec_edgar(q, c)))
register(ScraperSpec("open_corporates", lambda q, d, c=None: run_scrape_open_corporates(q, c),
                     settings_flag="SCRAPER_OPENCORPORATES_ENABLED"))
