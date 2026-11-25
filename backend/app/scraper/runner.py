"""
Scraper runner — orchestrates Wikidata and SEC EDGAR fetching and ArcadeDB writes.

Entry points here:
  run_scrape(query, depth)          – Wikidata scrape
  run_scrape_person(query)          – a person
  run_scrape_all(query, depth)      – every registered scraper in sequence
and, since the split of 2026-11-25, in the modules beside this one:
  runner_sec          – run_scrape_sec_edgar(company), the seat holders
  runner_sec_filings  – Exhibit 21, acquisitions, proxy, Form D, 13F, a filer's holdings
  runner_oc           – run_scrape_open_corporates(company)
  runner_imports      – the GLEIF and Companies House imports and deltas
This module keeps what they share: the entity and person upserts, the recursive
scrape, the scraper registry — and the imports of the database, the settings and
the write layer, which the other modules reach through it (`runner.db`).

All entry points:
- Check SCRAPER_ENABLED before doing anything.
- Check the per-source flag before running that source.
- Write using MERGE so repeated runs are safe (no duplicates).
"""

import logging
import time
from app.config import settings
from app.database import db
from app.entity_resolution import resolve_entity_id
from app.merged_ids import resolve_current_id
from app import industry  # noqa: F401
from app.claims import record_claim, KIND_ROLE  # noqa: F401
from app.stake_history import one_statement_per_run  # noqa: F401
from app.scraper.wikidata import (classify_candidates, search_by_statement, search_entity,
                                  search_entity_in_country, fetch_company_data, pick_candidate)
from app.scraper.sources import KNOWN_SOURCES
from app.scraper.mapper import infer_entity_type, parse_full_name, is_person_name, normalize_entity_name, derive_ownership_type, is_nominee_name, has_entity_suffix, normalize_cik, strip_edgar_tag  # noqa: F401
from app.scraper.sources import get_source_enabled
from app.scraper.graph_writer import (
    _record_edge_parties, _record_touched, _record_touched_entity, _stamp_person_cik, _with_autodedup,  # noqa: F401
    mark_scrape_incomplete,  # noqa: F401
    mark_source_lost,
    set_scrape_target,
    # The shared write layer lives in graph_writer now (the module split the
    # multi-scraper refactor deferred); re-exported here so existing imports and
    # test patch targets — `app.scraper.runner._upsert_owns` and friends — keep
    # working unchanged.
    _ensure_source, _merge_aliases, _now_iso, _same_seat,            # noqa: F401
    _person_search_text, _relabel_if_more_credible,                   # noqa: F401
    _upsert_entity_by_name, _upsert_person_by_name,                  # noqa: F401
    _upsert_owns, _upsert_role, _upsert_succession,                  # noqa: F401
)
from app.scraper.sec_writer import (
    VOTING_GROUP_TYPE,                                                           # noqa: F401
    _individual_by_code, _is_control_filing, _member_key,                        # noqa: F401
    _retire_superseded_bloc_edge, _roster_overlap, _rosters_match,               # noqa: F401
    _same_member, _split_member_key,                                             # noqa: F401
    _upsert_group_membership, _upsert_owns_sec,                                  # noqa: F401
    _upsert_role_sec, _upsert_voting_group, VOTING_PACT, JOINT_FILING, _prune_group_membership, _close_role_sec, form_label,                          # noqa: F401
    stated_by_13dg,                                                              # noqa: F401
    write_acquisition_sec,                                                       # noqa: F401
)
from app.scraper.scraper_registry import ScraperSpec, register, registered
from app.field_twins import merge_nationalities
from app.roles import combined_start, end_seats_at_death  # noqa: F401
from app.dissolution import end_at_dissolution
from app.node_ids import key_on_learning, mint_node_id
from app import addresses

from app.scraper.edge_schema import (READ_FIELD, READ_LAYOUT, READ_NARRATIVE, READ_TABLE,  # noqa: F401
                                     weakest_reading)  # noqa: F401
from app.scraper.country_match import matches_requested, country_mismatch  # noqa: F401
from app.scraper.geocode import geocode_address

#: a registered pin geocoded from a stated address: fill-only, its own parameter names
_REG_PIN_FILL = ",\n                ".join(
    f"e.{addresses.field(addresses.REGISTERED, p)} = COALESCE(e.{addresses.field(addresses.REGISTERED, p)}, ${v})"
    for p, v in (("lat", "lat"), ("lng", "lng"), ("precision", "prec")))


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
            f"""
            MATCH (e:Entity {{id: $id}})
            SET e.country    = COALESCE(e.country, $country),
                {_REG_PIN_FILL}
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
#: Subsidiaries scraped again one level down, per entity (depth 2–3): the
#: recursion is what grows ~exponentially. Every subsidiary is still READ and
#: written at its own level — until 2026-10-31 this capped the list itself, in
#: SPARQL row order, which is not fixed: Nestlé lists 23, and Alcon fell inside
#: the first 15 on one run and outside on the next (findings item 24).
MAX_SUBSIDIARIES_FOLLOWED = 15
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
    founded: int | None,
    revenue: float | None,
    description: str | None,
    wikidata_id: str,
    employees: int | None = None,
    employees_as_of: int | None = None,
    aliases: list[str] | None = None,
    source_id: str | None = None,
    credibility_score: int = 80,
    lei: str | list[str] | None = None,      # P1278 — a pointer, never written (register_pointer)
    sec_cik: str | list[str] | None = None,  # P5531 — a pointer, never written
    website: str | None = None,              # P856 — official site (display only)
    logo_url: str | None = None,             # P154/P18 → direct Commons thumb (display only)
    dissolved_on: str | None = None,         # P576 — the day the entity ceased (app.dissolution)
    register_required: bool = False,
    anchor: str | None = None,               # the node the query named (run_scrape; findings item 60)
) -> str | None:
    """
    Find entity by wikidata_id, the item's stated ids or name, update it if
    found, create if not. Returns the entity's internal id — or None when it
    writes nothing: the node found holds a register id the item contradicts,
    or (``register_required``, a related company) the item points at no
    register that knows it.

    ``anchor`` is the node the QUERY named — the searched company's, as the
    resolver found it before the scrape (`run_scrape`). An item that states
    no register id and whose own names find no node enriches the anchor when
    it bears one of the anchor's names, legal form kept (`_anchored`), instead
    of becoming a `wd:` node beside it: Shentel's Q23073960 states no id and
    is labelled "Shentel", the query was EDGAR's "SHENANDOAH TELECOMMUNICATIONS
    CO", and the item's alias is that company's legal name — the run wrote
    wd:Q23073960 five seconds before the SEC wrote sec:0000354963 (findings
    item 60, 2026-11-05 rebuild). The registers now run first, so the filer
    node exists when the item arrives.

    **Wikidata writes no hard id** (2026-10-29). ``lei`` / ``sec_cik`` (one
    value or every value the item states) find the node, and a register writes
    a node the graph lacks (`register_pointer.write_from_pointer`) — never this
    writer, which keyed it `lei:` from Wikidata's word and COALESCE-filled a
    node found by name with the item's ids. An item is an organisation, not a
    legal entity: BlackRock's Q219635 still states the pre-2024 parent's CIK,
    and the listed BlackRock carried it, was renamed after it and lost its own
    filings to a second node. A node whose register LEI or CIK is not among
    the item's is another legal entity than the item describes, and nothing
    is written onto it (`register_pointer.contradiction`; it is logged). Nor
    onto a node the item's ids name only under a name that had stopped
    applying AND that another registrant bears now (`register_pointer.moved_on`
    over `stale`): Q219635's CIK and LEI are those of the old parent,
    "BlackRock Finance, Inc." since 2024-09-26, and the item id with its
    aliases ("BlackRock, Inc.", "BLK") landed there, and won it every
    "BlackRock" lookup (2026-10-29 rebuild); a renamed registrant nobody else's
    name became keeps its item (Franklin Resources → Franklin Templeton, Inc.,
    findings item 66). An
    item without ids, or one no register knows, creates a `wd:` node — the
    searched company only; a related one is skipped.

    ``source_id`` (the Wikidata Source node) is stamped onto the entity so its
    own provenance shows in the node panel. It only *fills* a missing value
    (COALESCE) on update, so an entity first seen from a register (GLEIF/SEC)
    keeps that higher-credibility source when Wikidata later enriches it.

    Wikidata writes no address — no country, no list of countries, no
    headquarters city, coordinate or list — and no entity type, neither on
    create nor on update (2026-10-27). The registers state all of them, and
    Wikidata's are crowd-edited and often describe a group rather than the
    legal entity: Unilever's item Q157062 states both the United Kingdom and
    the Netherlands, its row order is arbitrary, and the update put NL over
    the GB that GLEIF and Companies House state for UNILEVER PLC. A node
    Wikidata creates carries the default type "company" and no country until
    a register fills them.
    """
    from app.scraper import register_pointer
    name_norm = normalize_entity_name(name)
    item_names = [name, *(aliases or [])]
    # Several stated LEIs are asked in the pointer's order (`ordered_leis`:
    # the parent per GLEIF first, the item's names as the tie-break) — in
    # the lookup below as much as in `write_from_pointer`. Asked in
    # Wikidata's order, Barclays' Q245343 took the Bank, the first of its
    # two LEIs, in two rebuilds running: the order #554 taught the pointer
    # was never reached, because this lookup found the node first (findings
    # item 61, 2026-11-06 rebuild). The CIK stays ahead of the LEI here: on
    # dev, two of the four items whose LEI and CIK name different nodes
    # state a SUBSIDIARY's LEI (Chevron U.S.A. Inc. for Chevron Corp,
    # WesBanco Bank for WesBanco Inc), and the filer the CIK names is the
    # organisation the item describes (findings item 66, guard 2026-11-06).
    leis = register_pointer.ordered_leis([x for x in ([lei] if isinstance(lei, str) else lei or []) if x], item_names)
    ciks = [c for c in (normalize_cik(x) for x in ([sec_cik] if isinstance(sec_cik, str) else sec_cik or [])) if c]
    # FULL_TEXT search field (name + description + aliases), so scraped entities
    # are findable via /search — same content as manage.py backfill-search. The
    # BODS importer sets this inline too; the Wikidata scraper must as well or the
    # companies it adds never enter the search index.
    search_text = " ".join(p for p in (
        name, description or "", " ".join(aliases or [])) if p).strip()
    with db.get_session() as session:
        # Sequential indexed lookups — an OR across these fields full-scans the
        # Entity type on ArcadeDB (see app.entity_resolution). The item's
        # stated ids come ahead of the name, so a company Wikidata knows the
        # LEI for attaches to its existing GLEIF node instead of becoming a
        # second copy of it — the LEIs in the order above.
        entity_id = (resolve_entity_id(session, wikidata_id=wikidata_id)
                     or next((i for i in (resolve_entity_id(session, sec_cik=c) for c in ciks) if i), None)
                     or next((i for i in (resolve_entity_id(session, lei_id=x) for x in leis) if i), None))
        other_of_name = None
        unconfirmed = None
        if not entity_id:
            entity_id = resolve_entity_id(session, name=name, name_normalized=name_norm)
            held_by_name = _held_ids(session, entity_id) if entity_id and (leis or ciks) else None
            # A node found by the NAME alone whose register id is not the
            # item's is another legal entity of that name, not a reason to
            # write nothing: J.P. Morgan Securities LLC and PLC share the key
            # "jp morgan securities", and whichever item came second was lost
            # (findings item 30). The item's ids then go to their register.
            if held_by_name and register_pointer.contradiction(held_by_name, leis, ciks):
                log.info("Wikidata: %s (%s) shares a name with %s, another legal entity; "
                         "asking the register", name, wikidata_id, entity_id)
                other_of_name, entity_id = entity_id, None
            # A node found by the NAME alone that holds a register id, none of
            # them the item's, is the item's legal entity only if that register
            # says so: the item's ids are asked first, with the node as the
            # candidate the register may join. Taken on the name, the item went
            # wherever a namesake happened to be written first — Q1516562
            # (Liberty Media, LEI 549300ZKUTPIBZLWLL89, the Nevada company)
            # landed on the Delaware filer CIK 0000869614 that a Berkshire 13G
            # had written an hour earlier; GLEIF refused that join and wrote
            # its own node without the item (findings item 50).
            elif held_by_name and register_pointer.unconfirmed(held_by_name, leis, ciks):
                log.info("Wikidata: %s (%s) found %s by name only; asking the register",
                         name, wikidata_id, entity_id)
                unconfirmed, entity_id = entity_id, None
    if not entity_id and (leis or ciks):
        # the item names break a tie among several stated LEIs (findings item 61)
        entity_id = (register_pointer.write_from_pointer(leis, ciks, candidates=[unconfirmed], names=item_names)
                     if unconfirmed else register_pointer.write_from_pointer(leis, ciks, names=item_names))
    # The item found for the company the query named, stating no register id
    # and found no node of its own names: it enriches the query node when
    # the item bears one of that node names (findings item 60).
    if not entity_id and anchor and not (leis or ciks) and not other_of_name and not unconfirmed:
        entity_id = _anchored(anchor, wikidata_id, [name, *(aliases or [])])
    # No register knows the item's ids: the name's node stands — enriched
    # when it was only unconfirmed, refused by the contradiction below when it
    # contradicts them — rather than a twin of that name.
    entity_id = entity_id or other_of_name or unconfirmed
    if not entity_id and register_required:
        log.info("Wikidata: %s (%s) points at no register that knows it — not written", name, wikidata_id)
        return None
    with db.get_session() as session:
        if entity_id:
            held = session.run("MATCH (e:Entity {id: $id}) RETURN e.lei_id AS lei_id, e.sec_cik AS sec_cik, "
                               "e.name AS name, e.other_names AS other_names, e.former_names AS former_names, "
                               "e.gleif_former_names AS gleif_former_names",
                               id=entity_id).single()
            held = {k: held.get(k) for k in ("lei_id", "sec_cik", "name", "other_names", "former_names",
                                             "gleif_former_names")} if held else {}
            # a register id the item's contradict, or ids that name the node
            # only under a name it no longer bears AND another registrant now
            # bears (`register_pointer.moved_on`; a bare rename of the same
            # registrant is not a move — Franklin Resources → Franklin
            # Templeton, Inc., findings item 66)
            why = (register_pointer.contradiction(held, leis, ciks)
                   or register_pointer.moved_on(held, item_names))
            if why:
                log.warning("Wikidata: %s (%s) is not the legal entity of %s — %s; nothing written",
                            name, wikidata_id, entity_id, why)
                return None
            # an LEI the item states and the graph lacks: GLEIF decides
            # whether it is this filer's (register_pointer.attach_stated_lei).
            # A join re-keys the filer `sec:` → `lei:` (app.node_ids), and the
            # writes below must follow it: on the old id they matched nothing,
            # and the 2026-10-31 rebuild left 17 companies without their item
            # id and their Wikidata board seats (findings item 22). A record
            # GLEIF writes on its own leaves the filer where it was.
            if leis and not held.get("lei_id") and held.get("sec_cik"):
                if register_pointer.attach_stated_lei(held, entity_id, leis, [name, *(aliases or [])]) not in (None, entity_id):
                    entity_id = resolve_current_id(session, entity_id) or entity_id
            # and a CIK the item states on a GLEIF node that lacks one: EDGAR
            # decides the same way (register_pointer.attach_stated_cik)
            if ciks and not held.get("sec_cik") and held.get("lei_id"):
                register_pointer.attach_stated_cik(held, entity_id, ciks)
            # search_text / name_normalized must derive from the name that WINS
            # the credibility contest below, not from the incoming one: Wikidata
            # enriching a GLEIF company must not evict the legal name from the
            # search index (SpaceX became findable only as "SpaceX" — the legal
            # name resolved to News Corp). The losing name still joins
            # search_text so both spellings stay searchable.
            rec = session.run(
                "MATCH (e:Entity {id: $id}) RETURN e.name AS name, "
                "COALESCE(e.name_credibility, 0) AS cred, e.aliases AS aliases, "
                "e.description AS descr",
                id=entity_id).single()
            if rec and rec.get("name") and rec["cred"] > credibility_score:
                kept_name = rec["name"]
                final_aliases = aliases or rec.get("aliases") or []
                search_text = " ".join(p for p in (
                    kept_name, name if name != kept_name else "",
                    description or rec.get("descr") or "",
                    " ".join(final_aliases)) if p).strip()
                name_norm = normalize_entity_name(kept_name)
            # founded only moves EARLIER: time travel hides a company before
            # it, and a later year from Wikidata replaced an earlier one.
            # No lei_id / sec_cik: a hard id comes from its register only
            # (see the docstring). NB: comments must stay OUT of the query
            # string; ArcadeDB's Cypher parser rejects `--` and fails the
            # whole statement.
            session.run(
                """
                MATCH (e:Entity {id: $id})
                SET e.wikidata_id     = $wid,
                    e.founded         = CASE WHEN $founded IS NOT NULL AND (e.founded IS NULL OR $founded < e.founded) THEN $founded ELSE e.founded END,
                    e.revenue         = COALESCE($revenue, e.revenue),
                    e.employees       = COALESCE($employees, e.employees),
                    e.employees_as_of = COALESCE($employees_as_of, e.employees_as_of),
                    e.description     = COALESCE($desc, e.description),
                    e.search_text     = $search_text,
                    e.name_normalized = $name_norm,
                    e.aliases         = CASE WHEN size($aliases) > 0 THEN $aliases ELSE COALESCE(e.aliases, []) END,
                    e.website         = COALESCE(e.website, $website),
                    e.logo_url        = COALESCE(e.logo_url, $logo_url),
                    e.dissolved_on    = COALESCE(e.dissolved_on, $dissolved_on),
                    e.active          = CASE WHEN $dissolved_on IS NOT NULL THEN false ELSE e.active END,
                    e.name            = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $name ELSE e.name END,
                    e.name_credibility = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred THEN $cred ELSE e.name_credibility END,
                    e.source_id       = COALESCE(e.source_id, $source_id)
                """,
                id=entity_id,
                name=name,
                wid=wikidata_id,
                source_id=source_id,
                founded=founded,
                revenue=revenue,
                desc=description,
                name_norm=name_norm,
                search_text=search_text,
                cred=credibility_score,
                employees=employees, employees_as_of=employees_as_of,
                aliases=aliases or [],
                website=website, logo_url=logo_url, dissolved_on=dissolved_on,
            )
            # a node created by name and now identified by Wikidata gets its keyed id
            entity_id = key_on_learning(session, "Entity", entity_id)
            if dissolved_on:
                end_at_dissolution(entity_id)      # nothing of a dissolved company stays open
            return _record_touched_entity(entity_id)

        # keyed by the Wikidata item (app.node_ids): the only id this writer
        # has of its own — no register knew the item's ids, or it states none
        entity_id = mint_node_id(wikidata_id=wikidata_id)
        session.run(
            """
            CREATE (e:Entity {
                id: $id, name: $name, name_normalized: $name_norm,
                search_text: $search_text,
                name_credibility: $cred,
                type: 'company', founded: $founded,
                revenue: $revenue, employees: $employees, employees_as_of: $employees_as_of,
                description: $desc,
                wikidata_id: $wid,
                source_id: $source_id,
                is_nominee: $is_nominee,
                aliases: $aliases,
                website: $website,
                logo_url: $logo_url, created_at: $created_at,
                dissolved_on: $dissolved_on, active: $active
            })
            """,
            id=entity_id, created_at=_now_iso(),
            name=name,
            name_norm=name_norm,
            search_text=search_text,
            cred=credibility_score,
            founded=founded,
            revenue=revenue,
            employees=employees, employees_as_of=employees_as_of,
            desc=description,
            wid=wikidata_id,
            source_id=source_id,
            is_nominee=is_nominee_name(name),
            aliases=aliases or [],
            website=website, logo_url=logo_url,
            # a dissolved company is not active; a living one is not asserted
            # active by a knowledge base (null = no register has said)
            dissolved_on=dissolved_on, active=False if dissolved_on else None,
        )
        if dissolved_on:
            end_at_dissolution(entity_id)          # the sibling path of the update above
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
    birth_country: str | None = None,
    nationalities: list[str] | None = None,
    source_id: str | None = None,
    image_url: str | None = None,
    wikipedia_url: str | None = None,
) -> str:
    first_name, last_name = parse_full_name(full_name)
    aliases       = aliases or []
    # One fact, one field: a caller's single `nationality` (Wikidata's CEO
    # row, a PSC record) joins the list — the scalar twin was dropped on
    # 2026-10-25 after 204 persons sat with a filled scalar and an empty list.
    # An empty list is written as no list: what no source stated is absent,
    # as a blank scalar is (196 dev persons carried `[]` on 2026-10-25).
    nationalities = merge_nationalities(nationalities, nationality)
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
            # Only fill blanks — never overwrite what's already there. The item
            # id is one of them: a person the SEC wrote first from a Form 4
            # (registers before knowledge bases, findings item 60) took
            # Wikidata's aliases and never its QID — 22 of them in the
            # 2026-11-06 rebuild, Sergey Brin and Sundar Pichai among them
            # (findings item 67).
            session.run(
                """
                MATCH (p:Person {id: $id})
                SET p.birth_date   = COALESCE(p.birth_date, $bdate),
                    p.wikidata_id  = COALESCE(p.wikidata_id, $wid),
                    p.death_date   = COALESCE(p.death_date, $ddate),
                    // The country belongs to the place it qualifies: filled
                    // only beside the same place (a place another source
                    // wrote first keeps no country it was not stated with).
                    // Before birth_place, so the comparison sees the old value.
                    p.birth_country = CASE WHEN p.birth_place IS NULL OR p.birth_place = $bplace
                                           THEN COALESCE(p.birth_country, $bcountry)
                                           ELSE p.birth_country END,
                    p.birth_place  = COALESCE(p.birth_place, $bplace),
                    p.image_url    = COALESCE(p.image_url, $image),
                    p.wikipedia_url = COALESCE(p.wikipedia_url, $wikipedia),
                    p.description   = CASE WHEN COALESCE(p.description, '') = '' THEN $desc ELSE p.description END,
                    p.aliases       = CASE WHEN size(COALESCE(p.aliases, [])) > 0 THEN p.aliases ELSE $aliases END,
                    p.nationalities = CASE WHEN size(COALESCE(p.nationalities, [])) > 0 THEN p.nationalities ELSE $nats END,
                    p.source_id     = COALESCE(p.source_id, $source_id),
                    // Derived, so it is refreshed rather than blank-filled: this
                    // pass may be the one that brought the aliases.
                    p.search_text   = $search_text,
                    // When the person was last written from a source — the
                    // company scrapes never stamped it, so no person on dev
                    // had one (954, 2026-10-25); only the person scrape did.
                    p.last_scraped_at = $now
                """,
                id=rec["id"], wid=wikidata_id, bdate=birth_date, ddate=death_date, bplace=birth_place,
                bcountry=birth_country if birth_place else None,
                now=_now_iso(), image=image_url, wikipedia=wikipedia_url, desc=description or None,
                search_text=_person_search_text(full_name, aliases),
                aliases=aliases, nats=nationalities or None, source_id=source_id,
            )
            if death_date:
                end_seats_at_death(rec["id"])      # a seat ends no later than its holder's death
            return _record_touched(rec["id"])

        person_id = mint_node_id(wikidata_id=wikidata_id)      # `wd:Q…`, a uuid without one
        session.run(
            """
            CREATE (p:Person {
                id: $id, first_name: $first, last_name: $last,
                full_name: $full,
                description: $desc, wikidata_id: $wid,
                birth_date: $bdate, death_date: $ddate, birth_place: $bplace,
                birth_country: $bcountry,
                image_url: $image, wikipedia_url: $wikipedia,
                aliases: $aliases, nationalities: $nats,
                source_id: $source_id, search_text: $search_text,
                created_at: $created_at, last_scraped_at: $created_at
            })
            """,
            id=person_id, created_at=_now_iso(),
            first=first_name,
            last=last_name,
            full=full_name,
            search_text=_person_search_text(full_name, aliases),
            desc=description or None,      # absent, never '' (2026-10-25)
            wid=wikidata_id,
            bdate=birth_date,
            ddate=death_date,
            bplace=birth_place,
            bcountry=birth_country if birth_place else None,
            image=image_url,
            wikipedia=wikipedia_url,
            aliases=aliases,
            nats=nationalities or None,
            source_id=source_id,
        )
        if death_date:
            end_seats_at_death(person_id)
        return _record_touched(person_id)


# ── Recursive scrape ──────────────────────────────────────────────────────────

def _anchored(anchor: str, wikidata_id: str, item_names: list[str]) -> str | None:
    """The node the query named, when the item bears one of its names — the
    node's own name or a register's other name, legal form kept
    (`sec_ex21.legal_name_key`) — and the node carries no item yet; else None
    (findings item 60). Not the node's aliases: a knowledge base's labels and
    other filers' spellings name brands and relatives (findings item 62). The
    item's label is often a brand ("Shentel"); its aliases hold the legal
    name ("Shenandoah Telecommunications Company"), which EDGAR's "SHENANDOAH
    TELECOMMUNICATIONS CO/VA/" keys alike."""
    from app.scraper.sec_ex21 import legal_name_key
    with db.get_session() as session:
        rec = session.run("MATCH (e:Entity {id: $id}) RETURN e.wikidata_id AS qid, e.name AS name, "
                          "e.other_names AS other", id=anchor).single()
    if not rec or rec.get("qid"):
        return None
    borne = {legal_name_key(n) for n in [rec.get("name"), *(rec.get("other") or [])] if n} - {""}
    if any(legal_name_key(n) in borne for n in item_names if n):
        log.info("Wikidata: %s (%s) enriches %s, the company the query named — the item bears its name",
                 item_names[0] if item_names else "", wikidata_id, anchor)
        return anchor
    log.info("Wikidata: %s (%s) bears none of %s's names — its own node", item_names[0] if item_names else "",
             wikidata_id, anchor)
    return None


def _resolve_related_company(rel: dict) -> str | None | bool:
    """Decide whether a Wikidata-related COMPANY may be written.

    Returns an existing node id that already carries the item (write the edge
    to it), None (proceed to `_upsert_entity` with ``register_required``: the
    item points at a register, which writes a node the graph lacks — or the
    node its ids find lacks the item, and the writer attaches the item and
    its stated ids to it), or False (SKIP — no existing node, no LEI/CIK, so
    creating it would mint an unidentifiable orphan that only clutters search
    and dedup, the litter the depth-2 pass produced for Samsung's
    subsidiaries; or the node found holds a register id the item contradicts,
    `register_pointer.contradiction`, or bears the item's name no longer and
    another registrant bears it now, `register_pointer.moved_on`).

    Until 2026-11-06 an existing node was returned as it stood, item or no
    item: the edge was drawn and nothing else. While Wikidata ran first that
    node was its own; with the registers first (findings item 60) the SEC
    part had written MUFG's filer sec:0000067088 from a 13D/A before Morgan
    Stanley's item named Q1542175 as an owner, and the filer kept neither the
    item nor the LEI the item states (findings item 66).

    People are never routed here — Wikidata's coverage of people is a strength
    and person dedup handles them; only company nodes are gated."""
    from app.database import db
    from app.scraper.register_pointer import contradiction, moved_on, ordered_leis
    # several stated LEIs in the pointer's order, as `_upsert_entity` asks
    # them (findings item 61)
    leis = ordered_leis(rel.get("leis") or [x for x in [rel.get("lei")] if x],
                        [n for n in [rel.get("name"), rel.get("label"), *(rel.get("aliases") or [])] if n])
    ciks = rel.get("sec_ciks") or [x for x in [rel.get("sec_cik")] if x]
    with db.get_session() as session:
        existing = (resolve_entity_id(session, wikidata_id=rel.get("qid"))
                    or next((i for i in (resolve_entity_id(session, sec_cik=c) for c in ciks) if i), None)
                    or next((i for i in (resolve_entity_id(session, lei_id=x) for x in leis) if i), None))
        if existing:
            held = session.run("MATCH (e:Entity {id: $id}) RETURN e.lei_id AS lei_id, e.sec_cik AS sec_cik, "
                               "e.name AS name, e.other_names AS other_names, e.former_names AS former_names, "
                               "e.gleif_former_names AS gleif_former_names, e.wikidata_id AS wikidata_id",
                               id=existing).single()
            held = {k: held.get(k) for k in ("lei_id", "sec_cik", "name", "other_names", "former_names",
                                             "gleif_former_names", "wikidata_id")} if held else {}
            why = (contradiction(held, leis, ciks)
                   or moved_on(held, [rel.get("name"), rel.get("label"), *(rel.get("aliases") or [])]))
            if why:
                log.warning("Wikidata: related %s (%s) is not the legal entity of %s — %s; skipped",
                            rel.get("name") or rel.get("label"), rel.get("qid"), existing, why)
                return False
            if not held.get("wikidata_id"):
                # the node its ids found carries no item yet: the writer attaches
                # this one and the ids it states (findings item 66)
                log.info("Wikidata: related %s (%s) enriches %s, which its ids found without an item",
                         rel.get("name") or rel.get("label"), rel.get("qid"), existing)
                return None
    if existing:
        return existing
    return None if (leis or ciks) else False


def _held_ids(session, entity_id: str) -> dict:
    """The register ids a node holds, as `register_pointer.contradiction` reads them."""
    rec = session.run("MATCH (e:Entity {id: $id}) RETURN e.lei_id AS lei_id, e.sec_cik AS sec_cik",
                      id=entity_id).single()
    return {"lei_id": rec.get("lei_id"), "sec_cik": rec.get("sec_cik")} if rec else {}


def _subsidiary_order(sub: dict) -> tuple:
    """A fixed order for an item's subsidiaries, which SPARQL returns in no
    particular one: those a register knows (an LEI or a CIK — the only ones
    written) first, current before ended, then by QID number."""
    identified = bool(sub.get("leis") or sub.get("lei") or sub.get("sec_ciks") or sub.get("sec_cik"))
    qid = str(sub.get("qid") or "")
    return (not identified, bool(sub.get("until")), int(qid[1:]) if qid[1:].isdigit() else 0, qid)


def _scrape_node(
    qid: str,
    depth: int,
    visited: set,
    scraped: list,
    source_id: str,
    parent_entity_id: str | None = None,
    counts: dict | None = None,
    anchor: str | None = None,
):
    # `anchor`: the node the query names (`run_scrape`), for the searched item
    # only — a related company never inherits it (findings item 60).
    # `counts` accumulates hygiene stats across the recursion: skipped_unidentified
    # (related companies with no existing node and no id a register knows, not
    # written) and conflicts (an item whose ids a node's register ids contradict,
    # register_pointer.contradiction). Created once by the top call, threaded down.
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
        founded=data.get("founded"),
        revenue=data.get("revenue"),
        description=data.get("description"),
        wikidata_id=qid,
        employees=data.get("employees"),
        website=data.get("website"),
        logo_url=data.get("logo_url"),
        dissolved_on=data.get("dissolved_on"),
        employees_as_of=data.get("employees_as_of"),
        aliases=data.get("aliases", []),
        lei=data.get("leis") or data.get("lei"),
        sec_cik=data.get("sec_ciks") or data.get("sec_cik"),
        source_id=source_id,
        anchor=anchor,
    )
    if entity_id is None:
        # the node found is another legal entity than the item's ids describe:
        # nothing of the item is written (register_pointer.contradiction)
        counts["conflicts"] = counts.get("conflicts", 0) + 1
        return
    scraped.append({
        "qid":  qid,
        "id":   entity_id,
        "name": data["name"],
        "type": entity_type,
    })

    # Wire up to parent if this node was reached via a subsidiary edge
    if parent_entity_id:
        _upsert_owns(parent_entity_id, entity_id, source_id,
                     source_url=_wikidata_url(qid), read_from=READ_FIELD)

    # Subsidiaries — all of them, in a fixed order (`_subsidiary_order`); only
    # the first MAX_SUBSIDIARIES_FOLLOWED written ones are followed down
    followed = 0
    for sub in sorted(data.get("subsidiaries", []), key=_subsidiary_order):
        sub_name = sub.get("name") or sub["qid"]
        sub_type = infer_entity_type(list(sub.get("instances", set())))
        gate = _resolve_related_company(sub)
        if gate is False:
            counts["skipped_unidentified"] = counts.get("skipped_unidentified", 0) + 1
            continue
        # No country: a node created here carries an LEI or a CIK
        # (_resolve_related_company), and that register states it.
        sub_id = gate or _upsert_entity(
            name=sub_name,
            lei=sub.get("leis") or sub.get("lei"), sec_cik=sub.get("sec_ciks") or sub.get("sec_cik"),
            founded=None,
            revenue=None,
            description=None,
            wikidata_id=sub["qid"],
            source_id=source_id,
            register_required=True,
        )
        if sub_id is None:
            counts["skipped_unidentified"] = counts.get("skipped_unidentified", 0) + 1
            continue
        _upsert_owns(entity_id, sub_id, source_id,
                     source_url=_wikidata_url(sub["qid"]),
                     since=sub.get("since"), until=sub.get("until"),
                     read_from=READ_FIELD)
        if depth > 1 and followed < MAX_SUBSIDIARIES_FOLLOWED:
            followed += 1
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
            birth_place=ceo.get("birth_place"), birth_country=ceo.get("birth_country"),
            image_url=ceo.get("image_url"),
            wikipedia_url=ceo.get("wikipedia_url"),
            aliases=ceo.get("aliases"),
            nationalities=ceo.get("nationalities"),
            source_id=source_id,
        )
        _upsert_role(person_id, entity_id, "CEO", source_id,
                     since=ceo.get("since"), until=ceo.get("until"),
                     source_url=_wikidata_url(qid), read_from=READ_FIELD)

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
                                   birth_place=off.get("birth_place"), birth_country=off.get("birth_country"),
                                   image_url=off.get("image_url"),
                                   wikipedia_url=off.get("wikipedia_url"),
                                   aliases=off.get("aliases"),
                                   nationalities=off.get("nationalities"),
                                   source_id=source_id)
        _upsert_role(person_id, entity_id, off["role"], source_id,
                     since=off.get("since"), until=off.get("until"),
                     source_url=_wikidata_url(qid), read_from=READ_FIELD)

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
                                      birth_place=owner.get("birth_place"), birth_country=owner.get("birth_country"),
                                      image_url=owner.get("image_url"),
                                      wikipedia_url=owner.get("wikipedia_url"),
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
                lei=owner.get("leis") or owner.get("lei"), sec_cik=owner.get("sec_ciks") or owner.get("sec_cik"),
                founded=None, revenue=None, description=None,
                wikidata_id=owner["qid"],
                source_id=source_id,
                register_required=True,
            )
            if owner_id is None:
                counts["skipped_unidentified"] = counts.get("skipped_unidentified", 0) + 1
                continue
            owner_label = "Entity"
        _upsert_owns(owner_id, entity_id, source_id, source_url=_wikidata_url(qid),
                     owner_label=owner_label,
                     since=owner.get("since"), until=owner.get("until"),
                     read_from=READ_FIELD)

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
            name=succ["name"], founded=None, revenue=None, description=None,
            wikidata_id=succ["qid"], lei=succ.get("leis") or succ.get("lei"),
            sec_cik=succ.get("sec_ciks") or succ.get("sec_cik"), source_id=source_id, register_required=True,
        )
        if succ_id is None:
            counts["skipped_unidentified"] = counts.get("skipped_unidentified", 0) + 1
            continue
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
            name=pred["name"], founded=None, revenue=None, description=None,
            wikidata_id=pred["qid"], lei=pred.get("leis") or pred.get("lei"),
            sec_cik=pred.get("sec_ciks") or pred.get("sec_cik"), source_id=source_id, register_required=True,
        )
        if pred_id is None:
            counts["skipped_unidentified"] = counts.get("skipped_unidentified", 0) + 1
            continue
        _upsert_succession(pred_id, entity_id, source_id, since=pred.get("date"),
                           source_url=_wikidata_url(qid))


# ── Wikidata public entry point ───────────────────────────────────────────────

#: The Wikidata properties of the hard ids a node can hold, in the order they
#: are asked: SEC CIK, then LEI.
_ITEM_BY_ID = (("sec_cik", "P5531"), ("lei_id", "P1278"))


def _stored_wikidata_item(query: str, country: str | None = None) -> str | None:
    """The Wikidata item for the node the database already holds for the
    query, read by id before any name search: the node's own QID, else the
    item stating its CIK (P5531), else its LEI (P1278). None means the name
    search, as before.

    A label search takes the name as written, and the names a scrape is asked
    for are often EDGAR's: "Wells Fargo & Company/Mn", "ONEOK INC /NEW/",
    "Trade Desk, Inc." found nothing, and 14 companies Wikidata files under
    the very CIK the node holds came back no_results on every rebuild
    (findings item 7, 2026-10-31). By id there is no namesake to pick wrong:
    "Toast Inc" by name is two unrelated "Toast" items, by CIK Toast, Inc.

    An id-found item must still look like a company (`pick_candidate`), and a
    CIK or LEI must name exactly one: GitLab's CIK sits on the software item,
    which stays refused."""
    stored = runner_sec._stored_entity(query, country)
    if not stored:
        return None
    if stored.get("wikidata_id"):
        return pick_candidate([{"id": stored["wikidata_id"]}], "company")
    for field, prop in _ITEM_BY_ID:
        value = stored.get(field)
        if not value:
            continue
        try:
            hits = search_by_statement(prop, value)
            if not hits and prop == "P5531" and value.lstrip("0") != value:
                hits = search_by_statement(prop, value.lstrip("0"))     # a few items drop the padding
        except Exception as exc:  # noqa: BLE001 — the lookup only saves a search
            log.warning("Wikidata: %s=%s lookup failed for %r: %s", prop, value, query, exc)
            return None
        if len(hits) == 1:
            return pick_candidate(hits, "company")
        if hits:
            log.info("Wikidata: %d items state %s=%s; left to the name search", len(hits), prop, value)
    return None


def _item_by_plain_name(query: str, country: str | None = None) -> str | None:
    """The item a name names once its legal form and EDGAR tag are dropped —
    "Lone Pine Capital Llc" is Lone Pine Capital, "Lord Abbett & Co" Lord
    Abbett — asked only when the name as written found no company.

    A name, not an id, so it is held to a name's standard: the item's own
    LABEL (not an alias) must reduce to the same words, the words must be at
    least two, and exactly one company-shaped item may qualify. "Inter & Co,
    Inc." reduces to "inter", which Inter Milan answers; "Sof Ltd" to "sof",
    a file format; "SmartStop Self Storage REIT, Inc." is not the label
    "SmartStop Self Storage". The writer still refuses an item whose LEI or
    CIK contradicts the node's (findings item 7, 2026-10-31)."""
    key = normalize_entity_name(strip_edgar_tag(query))
    if len(key.split()) < 2 or key == query.strip().lower():
        return None
    hits = (search_entity_in_country(key, country) if country else search_entity(key, limit=5))
    same = [h["id"] for h in hits or []
            if h.get("label") and normalize_entity_name(h["label"]) == key]
    if not same:
        return None
    facts = classify_candidates(same)
    companies = [q for q in same if facts.get(q, {}).get("is_company")]
    if len(companies) == 1:
        return companies[0]
    if companies:
        log.info("Wikidata: %d companies are labelled %r; none taken", len(companies), key)
    return None


def _stored_person_item(query: str) -> str | None:
    """The QID a Person node already holds for the query, read before any
    name search — only when exactly one person in the graph has the query's
    name (either word order: EDGAR writes 'MUSK ELON'), so a namesake never
    hands over another's item. Wikidata hardly states CIKs on people (none for
    55 dev persons sampled, 2026-10-31), so the QID is the only id worth
    asking by. Best-effort: a failed lookup means the name search."""
    words = sorted(normalize_entity_name(query).split())
    if len(words) < 2:
        return None
    try:
        from app.routers.search import _clean
        from app.db.arcadedb import run_sql
        rows = [_clean(r) for r in run_sql(
            "SELECT id, full_name, wikidata_id FROM Person WHERE search_text CONTAINSTEXT :q LIMIT 25",
            {"q": query.lower()})]
    except Exception as exc:  # noqa: BLE001 — the lookup only saves a search
        log.warning("Wikidata: stored-person lookup failed for %r: %s", query, exc)
        return None
    same = [r for r in rows if sorted(normalize_entity_name(r.get("full_name") or "").split()) == words]
    if len(same) == 1 and same[0].get("wikidata_id"):
        return pick_candidate([{"id": same[0]["wikidata_id"]}], "person")
    return None


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

    qid = _stored_wikidata_item(query, country)
    if qid is None:
        results = (search_entity_in_country(query, country) if country
                   else search_entity(query, limit=3))

        # A name is not a kind. Searching "Steve Jobs" returns the 2015 film first,
        # the book second and the man third — and taking the top hit wrote a Danny
        # Boyle picture into the graph as a company, because `infer_entity_type`
        # falls back to "company" for any P31 it does not recognise. Pick the hit
        # that actually looks like a company, or say there is not one.
        qid = pick_candidate(results, "company") if results else None
        if qid is None:
            qid = _item_by_plain_name(query, country)
        if qid is None and not results:
            return {"status": "no_results", "query": query, "total": 0, "scraped": [],
                    "requested_country": country}
    if qid is None:
        log.info("Wikidata: nothing company-shaped among the hits for %r", query)
        return {"status": "not_a_company", "query": query, "total": 0, "scraped": [],
                "requested_country": country}

    source_id = _ensure_source(WIKIDATA_SOURCE_NAME, WIKIDATA_SOURCE_URL, WIKIDATA_CREDIBILITY, "knowledge_base")
    scraped: list = []
    visited: set  = set()
    counts: dict  = {}

    # The node the QUERY names, for the item found by searching that name: an
    # item stating no register id enriches it rather than becoming a `wd:`
    # twin beside it, when the item bears one of its names (`_anchored`,
    # findings item 60). A node that already carries an item is no anchor.
    stored = runner_sec._stored_entity(query, country)
    anchor = stored["id"] if stored and stored.get("id") and not stored.get("wikidata_id") else None
    _scrape_node(qid, depth, visited, scraped, source_id, counts=counts, anchor=anchor)

    # Mark the TARGET (the searched company) as on-demand scraped at this depth, so the
    # freshness gate + the depth-2 "deepen" pass can decide correctly next time. Only the
    # target — not the subsidiaries this pass touched (they stay independently scrapable).
    target_id = next((s["id"] for s in scraped if s.get("qid") == qid), None)
    if target_id:
        set_scrape_target(target_id, depth)

    return {
        # `conflict`: the searched item's ids contradict the node it found, and
        # nothing of it was written (the target is the first node visited)
        "status":      "conflict" if target_id is None and counts.get("conflicts") else "ok",
        "query":       query,
        "wikidata_id": qid,
        "entity_id":   target_id,
        "total":       len(scraped),
        "scraped":     scraped,
        "skipped_unidentified": counts.get("skipped_unidentified", 0),
        "conflicts":   counts.get("conflicts", 0),
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

    qid = _stored_person_item(query)
    results = [] if qid else (search_entity_in_country(query, country) if country
                              else search_entity(query, limit=3))
    if not qid and not results:
        return {"status": "no_results", "query": query, "total": 0, "scraped": [],
                "requested_country": country}

    # The same reason in the other direction: the man is the third hit for his
    # own name, so the first is not the one to ask about.
    qid = qid or pick_candidate(results, "person")
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
        birth_place=detail.get("birth_place"), birth_country=detail.get("birth_country"), image_url=detail.get("image_url"),
        wikipedia_url=detail.get("wikipedia_url"), aliases=detail.get("aliases"),
        nationalities=detail.get("nationalities"), source_id=source_id,
    )

    scraped: list[dict] = [{"type": "person", "name": detail.get("full_name"), "role": "target"}]
    for link in fetch_person_companies(qid):
        if not link["is_company"]:
            log.info("Wikidata person scrape: skipping %r — not a company", link["name"])
            continue
        entity_id = _upsert_entity(
            name=link["name"], founded=None, revenue=None, description=None,
            wikidata_id=link["qid"], source_id=source_id,
        )
        for role in link["roles"]:
            if role == OWNER_ROLE:
                _upsert_owns(owner_id=person_id, owned_id=entity_id, source_id=source_id,
                             owner_label="Person", credibility_score=WIKIDATA_CREDIBILITY,
                             read_from=READ_FIELD)
            else:
                _upsert_role(person_id, entity_id, role, source_id,
                             credibility_score=WIKIDATA_CREDIBILITY, read_from=READ_FIELD)
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
    specs = registered()
    ids_before = (runner_sec.stored_hard_ids(query, country)
                  if any(s.reads_stored_ids and s.enabled() for s in specs) else None)

    def run_one(spec) -> dict:
        # how long the source took, for the rebuild's timings (2026-11-11)
        started = time.monotonic()
        try:
            out = spec.run(query, depth, country)
        except PermissionError as exc:
            out = {"status": "disabled", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - one scraper failing mustn't sink the rest
            log.error("%s scrape failed for %r: %s", spec.name, query, exc)
            # nothing of it was written, and the sources after it run without what
            # it would have written: the target is stamped incomplete (item 54)
            mark_source_lost(spec.name)
            out = {"status": "error", "detail": str(exc)}
        return {**out, "seconds": round(time.monotonic() - started, 1)} if isinstance(out, dict) else out

    for spec in specs:
        if not spec.enabled():
            results[spec.name] = {"status": "disabled"}
            continue
        results[spec.name] = run_one(spec)
    # once more, by the id a later source wrote (findings item 27)
    for spec in specs:
        if spec.enabled() and runner_sec.rerun_for_new_ids(spec, results.get(spec.name), ids_before, query, country):
            again = run_one(spec)
            results[spec.name] = {**again, "rerun": "new_ids",
                                  "seconds": round((results[spec.name].get("seconds") or 0)
                                                   + (again.get("seconds") or 0), 1)}
    # and the SEC's seat-holders pass once more, for the officers a later source
    # wrote: their Form 4s and 8-K departures by name (findings item 67). The
    # registers run before the knowledge bases (item 60), so the SEC part met
    # none of Wikidata's officers on its own run.
    sec = results.get("sec_edgar")
    filer_id = runner_sec.seat_rerun_target(sec)
    if filer_id:
        started = time.monotonic()
        try:
            results["sec_edgar"] = {**sec, "seats_rerun": runner_sec.run_sec_seat_holders(
                filer_id, sec["cik"], sec.get("company") or query)}
        except Exception as exc:  # noqa: BLE001 - an extra pass; its loss is logged, not fatal
            log.error("sec_edgar seat-holders rerun failed for %r: %s", query, exc)
            results["sec_edgar"] = {**sec, "seats_rerun": {"status": "error", "detail": str(exc)}}
        # the seats pass is the SEC's time too
        results["sec_edgar"]["seconds"] = round((sec.get("seconds") or 0) + time.monotonic() - started, 1)

    return {"status": "ok", "query": query, "results": results}


# The pipelines that moved out of this file (2026-11-25). Imported here and not
# at the top: they reach back into this module for the write layer and the
# database, so it has to be whole first; the registry below calls them.
from app.scraper import runner_oc, runner_sec  # noqa: E402

# ── Scraper registry ──────────────────────────────────────────────────────────
# Register the built-in scrapers so run_scrape_all (and, later, the router) can
# iterate them. A new scraper registers its own ScraperSpec — no dispatch edits.
# One declaration per source. enabled() derives from the settings flag + the
# per-source DB toggle; label/credibility/url come from the KNOWN_SOURCES entry
# via spec.meta; register() validates all of it loudly at import time.
register(ScraperSpec("wikidata", lambda q, d, c=None: run_scrape(q, d, c),
                     depth_aware=True, reads_stored_ids=True))
register(ScraperSpec("sec_edgar", lambda q, d, c=None: runner_sec.run_scrape_sec_edgar(q, c)))
register(ScraperSpec("open_corporates", lambda q, d, c=None: runner_oc.run_scrape_open_corporates(q, c),
                     settings_flag="SCRAPER_OPENCORPORATES_ENABLED"))
