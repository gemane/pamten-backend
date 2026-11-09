"""A register writes the company a knowledge base points at.

Wikidata states an LEI (P1278) and an SEC CIK (P5531) for many companies, and
until 2026-10-29 the Wikidata writer stored them on the node: a node it created
was keyed `lei:{LEI}` from Wikidata's word alone, and a node it found by name
was COALESCE-filled with the item's ids. A Wikidata item is an organisation,
not a legal entity, so its ids can belong to another legal entity than the
node: BlackRock's item Q219635 still states the pre-2024 parent's CIK and LEI
(the company renamed BlackRock Finance, Inc. when a new holding company took
the name), the writer joined it to the new parent's GLEIF node by name and
stamped the old CIK on it, and since the SEC name for a CIK wins, the listed
BlackRock was renamed "BlackRock Finance, Inc." while its own filings drew a
second node.

So a hard id on a node comes only from its register. Wikidata's ids are a
pointer: the writer looks the company up by them, and when the graph does not
hold it, the register is asked — GLEIF's API by LEI, EDGAR's submissions by
CIK — and writes the node itself, under its own name, id and source. Wikidata
then only adds what it adds anywhere: its item id, description, logo, edges.
A pointer the register does not know writes nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

GLEIF_RECORD_URL = "https://api.gleif.org/api/v1/lei-records/{lei}"


def _wrap(value) -> dict | None:
    """A scalar in the golden copy's `{"$": value}` shape (`gleif_succession._v`)."""
    return None if value in (None, "") else {"$": value}


def _api_day(stamp: str | None) -> str | None:
    """A GLEIF API date as the day the golden copy states.

    The API gives UTC instants: an entity created at midnight in Vienna reads
    "2006-02-12T23:00:00Z", where the golden copy says 2006-02-13. A local
    midnight east of Greenwich lands after noon of the day before in UTC, one
    west of it before noon of the same day, so an instant after noon is the
    next day's date."""
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:10] if len(stamp) >= 10 else None
    return (t + timedelta(days=1) if t.hour >= 12 else t).date().isoformat()


def _api_address(addr: dict | None) -> dict | None:
    if not addr:
        return None
    lines = [ln for ln in (addr.get("addressLines") or []) if ln]
    return {k: v for k, v in {
        "FirstAddressLine": _wrap(lines[0] if lines else None),
        "AdditionalAddressLine": [_wrap(ln) for ln in lines[1:]] or None,
        "City": _wrap(addr.get("city")),
        "Region": _wrap(addr.get("region")),
        "Country": _wrap(addr.get("country")),
        "PostalCode": _wrap(addr.get("postalCode")),
    }.items() if v}


def golden_copy_record(attributes: dict) -> dict:
    """A GLEIF API record's `attributes` in the golden copy's LEI-CDF shape,
    so the bulk importer's own reading (`gleif_lei_cdf._entity_props`) writes
    the node — one mapping of GLEIF's fields, not two."""
    entity = attributes.get("entity") or {}
    registration = attributes.get("registration") or {}

    def names(items):
        return [{"$": n["name"], "@type": n.get("type")} for n in items or [] if n.get("name")]

    return {
        "LEI": _wrap(attributes.get("lei")),
        "Entity": {
            "LegalName": _wrap((entity.get("legalName") or {}).get("name")),
            "OtherEntityNames": {"OtherEntityName": names(entity.get("otherNames"))},
            "TransliteratedOtherEntityNames": {
                "TransliteratedOtherEntityName": names(entity.get("transliteratedOtherNames"))},
            "LegalAddress": _api_address(entity.get("legalAddress")),
            "HeadquartersAddress": _api_address(entity.get("headquartersAddress")),
            "RegistrationAuthority": {
                "RegistrationAuthorityID": _wrap((entity.get("registeredAt") or {}).get("id")),
                "RegistrationAuthorityEntityID": _wrap(entity.get("registeredAs")),
            },
            "LegalJurisdiction": _wrap(entity.get("jurisdiction")),
            "LegalForm": {
                "EntityLegalFormCode": _wrap((entity.get("legalForm") or {}).get("id")),
                "OtherLegalForm": _wrap((entity.get("legalForm") or {}).get("other")),
            },
            "EntityStatus": _wrap(entity.get("status")),
            "EntityCreationDate": _wrap(_api_day(entity.get("creationDate"))),
            "EntityExpirationDate": _wrap(_api_day((entity.get("expiration") or {}).get("date"))),
            "LegalEntityEvents": {"LegalEntityEvent": [
                {"@event_status": ev.get("status"), "LegalEntityEventType": _wrap(ev.get("type")),
                 "LegalEntityEventEffectiveDate": _wrap(_api_day(ev.get("effectiveDate")))}
                for group in entity.get("eventGroups") or [] for ev in group.get("events") or []]},
        },
        "Registration": {
            "InitialRegistrationDate": _wrap(_api_day(registration.get("initialRegistrationDate"))),
            "RegistrationStatus": _wrap(registration.get("status")),
            "ValidationSources": _wrap(registration.get("corroborationLevel")),
            "LastUpdateDate": _wrap((registration.get("lastUpdateDate") or "")[:10] or None),
        },
    }


def fetch_gleif_record(lei: str) -> dict | None:
    """The LEI's record from GLEIF's API in the golden-copy shape, or None when
    GLEIF does not know the LEI. A failure to ask raises."""
    import httpx

    from app.scraper.gleif_incremental import _HEADERS
    r = httpx.get(GLEIF_RECORD_URL.format(lei=lei), headers=_HEADERS, timeout=30, follow_redirects=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    attributes = ((r.json() or {}).get("data") or {}).get("attributes")
    return golden_copy_record(attributes) if attributes else None


def _gleif_enabled() -> bool:
    # The catalogue switch alone: GLEIF has no environment switch (its bulk
    # imports run from the CLI). The 2026-10-29 rebuild wrote no node from an
    # LEI because this also asked for one that was never set.
    from app.scraper.sources import get_source_enabled
    return get_source_enabled("bods_gleif")


def _sec_enabled() -> bool:
    from app.config import settings
    from app.scraper.sources import get_source_enabled
    return bool(settings.SCRAPER_SEC_EDGAR_ENABLED and get_source_enabled("sec_edgar"))


def write_from_gleif(lei: str, candidates: list[str] | None = None, by_name: bool = True) -> str | None:
    """The node GLEIF states for this LEI, written by the GLEIF importer's own
    reading of the record; its id, or None when GLEIF does not know the LEI.
    A node merged away answers with its survivor, which already exists.

    An EDGAR filer node of the record's name without an LEI may be the same
    legal entity: when the registers agree (`legal_identity`), that node takes
    the LEI and GLEIF's facts, filling gaps only, and no second node is minted
    — NYT's GLEIF node was minted beside its SEC node because the SEC node
    happened to come first (2026-10-29b). ``candidates`` are node ids to weigh
    besides the name's (the node a Wikidata item resolved to by its CIK);
    ``by_name=False`` weighs only them."""
    from app.merged_ids import canonical_id
    from app.scraper.bulk_import import _BatchWriter
    from app.scraper.gleif_lei_cdf import _entity_props, hold_end_of_live_filer
    from app.scraper.runner import BODS_GLEIF_CREDIBILITY, GLEIF_SOURCE_NAME, GLEIF_SOURCE_URL, _ensure_source

    survivor = canonical_id(f"lei:{lei}")
    if survivor != f"lei:{lei}":
        return survivor
    rec = fetch_gleif_record(lei)
    if not rec:
        log.info("GLEIF does not know the LEI %s a knowledge base pointed at", lei)
        return None
    source_id = _ensure_source(GLEIF_SOURCE_NAME, GLEIF_SOURCE_URL, BODS_GLEIF_CREDIBILITY)
    built = _entity_props(rec, source_id, BODS_GLEIF_CREDIBILITY)
    if not built:
        return None
    node_id, props = built
    if joined := _join_filer(lei, props, candidates or [], by_name):
        return joined
    # the node may be a filer's already (joined on an earlier read): an end
    # GLEIF now states is not written over a company that still files (item 85a)
    hold_end_of_live_filer(node_id, props)
    batch = _BatchWriter()
    batch.entity(node_id, props)
    batch.flush()
    return node_id


#: what a GLEIF record never fills on a filer node it joins: the filer's own
#: name and provenance stay; its other names are united, not replaced
_JOIN_KEEPS = frozenset({"name", "name_normalized", "search_text", "other_names", "source_id",
                         "source_url", "name_credibility", "type", "lei_id"})


def _join_filer(lei: str, props: dict, extra: list[str], by_name: bool = True) -> str | None:
    """The id of the EDGAR filer node (CIK, no LEI) the GLEIF record ``props``
    is the same legal entity as, after stamping the LEI and GLEIF's facts on
    it; None when there is none. Asking EDGAR is scraping: nothing when SEC
    scraping is off."""
    from app.db.arcadedb import run_command, run_sql
    from app.scraper.legal_identity import JOIN, filer_facts, same_legal_entity
    from app.scraper.maintenance import _sec_lei_check_enabled
    from app.scraper.mapper import normalize_entity_name
    if not _sec_lei_check_enabled():
        return None
    keys = {normalize_entity_name(n) for n in [props.get("name"), *(props.get("other_names") or [])] if n} - {""} \
        if by_name else set()
    found: dict[str, dict] = {}
    for nn in sorted(keys):
        for r in run_sql("SELECT id, sec_cik, lei_id, country, other_names, search_text FROM Entity "
                         "WHERE name_normalized = :nn AND sec_cik IS NOT NULL AND lei_id IS NULL LIMIT 10",
                         {"nn": nn}):
            found.setdefault(r["id"], dict(r))
    for node_id in extra:
        for r in run_sql("SELECT id, sec_cik, lei_id, country, other_names, search_text FROM Entity "
                         "WHERE id = :id AND sec_cik IS NOT NULL AND lei_id IS NULL", {"id": node_id}):
            found.setdefault(r["id"], dict(r))
    for node_id, node in found.items():
        if node.get("country") and props.get("country") and node["country"] != props["country"]:
            continue
        facts = filer_facts(node["sec_cik"])
        if not facts:
            continue
        verdict, why = same_legal_entity({**props, "lei_id": lei}, facts)
        if verdict != JOIN:
            log.info("GLEIF %s is not the filer %s (CIK %s): %s", lei, node_id, node["sec_cik"], why)
            continue
        fill = {k: v for k, v in props.items() if k not in _JOIN_KEEPS and v is not None}
        others = list(dict.fromkeys([*(node.get("other_names") or []), props.get("name"),
                                     *(props.get("other_names") or [])]))
        search = " ".join([node.get("search_text") or ""] + [n for n in others if n and n not in (node.get("search_text") or "")])
        sets = ", ".join(f"e.{k} = COALESCE(e.{k}, ${k})" for k in sorted(fill))
        run_command(f"MATCH (e:Entity {{id: $id}}) SET {sets + ', ' if sets else ''}"
                    "e.lei_id = $lei, e.lei_source = $why, e.other_names = $others, e.search_text = $search",
                    {**fill, "id": node_id, "lei": lei, "why": why, "others": [n for n in others if n],
                     "search": search.strip()})
        log.info("GLEIF %s joined to the filer %s (CIK %s): %s", lei, node_id, node["sec_cik"], why)
        # the filer now carries an LEI: `sec:` becomes `lei:`, so the GLEIF
        # importers, which address the record by `lei:{LEI}`, find it
        from app.node_ids import rekey
        return rekey("Entity", node_id)
    return None


def write_from_edgar(cik: str, candidates: list[str] | None = None, lei: str | None = None) -> str | None:
    """The filer EDGAR states for this CIK, written as the SEC writer writes a
    filer it scrapes (EDGAR's name for the CIK, its former names with the day
    each ended, its country and business address); its id, or None when EDGAR
    has no such CIK. The former names are what lets `stale` see that an item
    pointing here describes the company the CIK used to be. ``candidates``
    are node ids the writer weighs before the name's; ``lei`` is an LEI the
    filer's own filing states as its own (`sec_lei.confirm_filer_lei`), on
    which the write lands by id — the name's candidates and the registers'
    comparison are not asked (findings item 63)."""
    import httpx

    from app.scraper.graph_writer import _upsert_entity_by_name
    from app.scraper.runner import (SEC_EDGAR_CREDIBILITY, SEC_EDGAR_SOURCE_NAME,
                                    SEC_EDGAR_SOURCE_URL, _ensure_source)
    from app.scraper.sec_edgar import (_submissions, former_name_entries, sec_country,
                                       sec_headquarters, sec_website)
    try:
        sub = _submissions(cik)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            log.info("EDGAR does not know the CIK %s a knowledge base pointed at", cik)
            return None
        raise
    name = (sub or {}).get("name")
    if not name:
        return None
    source_id = _ensure_source(SEC_EDGAR_SOURCE_NAME, SEC_EDGAR_SOURCE_URL, SEC_EDGAR_CREDIBILITY)
    return _upsert_entity_by_name(name=name, entity_type="company", cik=cik, lei=lei, source_id=source_id,
                                  register_name=True, country=sec_country(sub),
                                  headquarters=sec_headquarters(sub), website=sec_website(sub),
                                  former_names=former_name_entries(sub) or None,
                                  candidates=candidates)


def _record_legal_names(lei: str) -> list[str]:
    """The legal name and the other CURRENT names of GLEIF's record (its
    transliterations and alternative-language legal names, as the bulk
    importer reads them — never a previous legal name: BlackRock's old parent
    still lists "BLACKROCK, INC.", the item's label, findings item 22), or []
    when GLEIF does not know the LEI or cannot be asked (logged)."""
    from app.scraper.gleif_lei_cdf import _other_names
    try:
        rec = fetch_gleif_record(lei)
    except Exception as exc:  # noqa: BLE001 — a tie-break only; the stated order stands
        log.warning("GLEIF record %s unreadable for the name tie-break: %s", lei, exc)
        return []
    if not rec:
        return []
    entity = rec.get("Entity") or {}
    legal = (entity.get("LegalName") or {}).get("$")
    current, _previous, _trading = _other_names(entity, legal or "")
    return [n for n in [legal, *current] if n]


def ordered_leis(leis: list[str], names: list[str] | None = None) -> list[str]:
    """The LEIs a knowledge base's item states, in the order a register is
    asked for them (findings item 61). An item describes an organisation,
    and one that states several LEIs usually describes a group: Barclays'
    Q245343 states the Bank's and the PLC's at equal rank, and the first
    listed (the Bank, a subsidiary) took the item while BARCLAYS PLC, the
    parent and the filer, got nothing. The group's node is its top legal
    entity, so an LEI GLEIF names as the parent of another stated LEI comes
    first (`legal_identity.gleif_parents`); among the rest, one whose record
    bears the item's label or an alias with the legal form kept
    (`sec_ex21.legal_name_key`: "Barclays PLC" is BARCLAYS PLC, "Barclays
    Bank" is not BARCLAYS BANK PLC); then the stated order. A single LEI is
    asked nothing."""
    if len(leis) < 2:
        return list(leis)
    from app.scraper.legal_identity import gleif_parents
    from app.scraper.sec_ex21 import legal_name_key
    stated = list(dict.fromkeys(leis))
    parents_of: dict[str, set[str]] = {}
    for lei in stated:
        try:
            parents_of[lei] = {p.get("lei") for p in gleif_parents(lei) or [] if p.get("lei")}
        except Exception as exc:  # noqa: BLE001 — the rule falls back to the names and the stated order
            log.warning("GLEIF parents of %s unreadable: %s", lei, exc)
            parents_of[lei] = set()
    is_parent = {lei: sum(1 for other in stated if other != lei and lei in parents_of[other]) for lei in stated}
    item_keys = {legal_name_key(n) for n in (names or []) if n} - {""}
    named = {lei: (any(legal_name_key(n) in item_keys for n in _record_legal_names(lei)) if item_keys else False)
             for lei in stated}
    ordered = sorted(stated, key=lambda lei: (-is_parent[lei], -int(named[lei]), stated.index(lei)))
    if ordered != stated:
        log.info("several stated LEIs %s asked as %s (parent of another: %s; bears the item's name: %s)",
                 stated, ordered, [x for x in stated if is_parent[x]], [x for x in stated if named[x]])
    return ordered


def attach_stated_lei(node: dict, node_id: str, leis: list[str], names: list[str] | None = None) -> str | None:
    """A knowledge base resolved to ``node`` (an EDGAR filer: CIK, no LEI) and
    states an LEI the graph does not hold: GLEIF is asked, and the node takes
    the LEI when the registers agree (`legal_identity`); otherwise GLEIF's node
    is written on its own. Until 2026-10-30 the LEI was dropped (#480): Baillie
    Gifford & Co's record 549300UYMK70HLNJU654 was never fetched. Several
    stated LEIs are asked in `ordered_leis`' order (findings item 61). Returns
    the node the LEI landed on, or None."""
    from app.database import db
    from app.entity_resolution import resolve_entity_id
    if node.get("lei_id") or not node.get("sec_cik") or not leis or not _gleif_enabled():
        return None
    with db.get_session() as session:
        if any(resolve_entity_id(session, lei_id=x) for x in leis):
            return None
    for lei in ordered_leis(leis, names):
        if landed := write_from_gleif(lei, candidates=[node_id], by_name=False):
            return landed
    return None


def attach_stated_cik(node: dict, node_id: str, ciks: list[str]) -> str | None:
    """The mirror of `attach_stated_lei`: a knowledge base resolved to ``node``
    (a GLEIF record: LEI, no CIK) and states a CIK the graph does not hold.
    The node takes the CIK, with EDGAR's facts written as for any filer, when
    the filer's own filings state the node's LEI (`sec_lei.confirm_filer_lei`)
    or, failing that, when the registers agree (`legal_identity`); otherwise
    nothing is written. The filing is asked first, as the entity dedup's
    bridge asks it (`maintenance._bridge_sec_lei`): an id a filer states about
    itself outweighs a comparison of names and addresses — and the write then
    carries the LEI, so it lands on the node by id, past the writer's own
    name-only check (`graph_writer._not_this_filer`), which weighs the same
    comparison and refused the same record. Until 2026-11-06
    only the registers were asked, and a record they call weak lost the join
    a filing had settled — Capital Ventures International's Q104626435 states
    LEI PIRK3IP22W3DKV63YG19 and CIK 0001011712, GLEIF's record names a
    P.O. box the filer's address omits, and the CIK stayed off the node until
    the dedup joined the pair hours later by the filer's N-PX (findings item
    63). Until 2026-10-30 the CIK only found nodes: NV Energy's item Q1961658
    states CIK 0000741508, a filer EDGAR has not listed since 2014, and no
    name search finds it (findings item 5). Returns the node, or None."""
    from app.database import db
    from app.db.arcadedb import run_command, run_sql
    from app.entity_resolution import resolve_entity_id
    from app.scraper.legal_identity import IDENTITY_FIELDS, JOIN, filer_facts, same_legal_entity
    from app.scraper.sec_lei import confirm_filer_lei
    if node.get("sec_cik") or not node.get("lei_id") or not ciks or not _sec_enabled():
        return None
    with db.get_session() as session:
        if any(resolve_entity_id(session, sec_cik=c) for c in ciks):
            return None
    rows = run_sql(f"SELECT {', '.join(IDENTITY_FIELDS)} FROM Entity WHERE id = :id", {"id": node_id})
    if not rows:
        return None
    record = {k: rows[0].get(k) for k in IDENTITY_FIELDS}
    names = [n for n in [record.get("name"), *(record.get("other_names") or [])] if n]
    for cik in ciks:
        hit = confirm_filer_lei(cik, node["lei_id"], names)
        if not hit:
            facts = filer_facts(cik)
            if not facts:
                continue
            verdict, why = same_legal_entity(record, facts)
            if verdict != JOIN:
                log.info("the stated CIK %s is not %s: %s", cik, node_id, why)
                continue
        if write_from_edgar(cik, candidates=[node_id], lei=node["lei_id"] if hit else None) != node_id:
            continue
        if hit:
            # the filing that tied the CIK to the LEI, kept as the SEC scrape and
            # the dedup bridge keep theirs (`lei_source_url`, `lei_source`)
            run_command("MATCH (e:Entity {id: $id}) SET e.lei_source_url = COALESCE(e.lei_source_url, $url), "
                        "e.lei_source = COALESCE(e.lei_source, 'filing')",
                        {"id": node_id, "url": hit["source_url"]})
            log.info("the stated CIK %s is %s: its %s states the LEI (%s)", cik, node_id, hit.get("form"),
                     hit["source_url"])
        return node_id
    return None


def write_from_pointer(leis: list[str], ciks: list[str], candidates: list[str] | None = None,
                       names: list[str] | None = None) -> str | None:
    """The node a register writes for the first id it knows — an LEI before a
    CIK (GLEIF states the legal entity; a CIK names a filer) — or None when no
    register that is switched on knows any of them. ``candidates`` are nodes
    the register weighs as that legal entity on its own terms (the GLEIF join,
    the EDGAR filer match): the node a knowledge base's item found by name
    (findings item 50). Several stated LEIs are asked in `ordered_leis`' order
    — the parent of the others first, then the one bearing the item's
    ``names`` (findings item 61)."""
    if leis and _gleif_enabled():
        for lei in ordered_leis(leis, names):
            if node_id := (write_from_gleif(lei, candidates=candidates) if candidates else write_from_gleif(lei)):
                return node_id
    if ciks and _sec_enabled():
        for cik in ciks:
            if node_id := (write_from_edgar(cik, candidates=candidates) if candidates else write_from_edgar(cik)):
                return node_id
    return None


def contradiction(node: dict, leis: list[str], ciks: list[str]) -> str | None:
    """Why a node is not the legal entity a knowledge base's ids describe, or
    None. The node's LEI or CIK — written by its register — is not among the
    ones the item states. An id the item does not state is no contradiction,
    and nor is a node without one."""
    if node.get("lei_id") and leis and node["lei_id"] not in leis:
        return f"LEI {node['lei_id']} is not the stated {', '.join(leis)}"
    if node.get("sec_cik") and ciks and node["sec_cik"] not in ciks:
        return f"CIK {node['sec_cik']} is not the stated {', '.join(ciks)}"
    return None


def unconfirmed(node: dict, leis: list[str], ciks: list[str]) -> bool:
    """Whether a node found by NAME holds a register id (an LEI or a CIK) and
    none of the ones the knowledge base's item states — so only a register
    can say it is that legal entity (findings item 50). The item's ids are
    then asked first, with the node as the candidate. A node without register
    ids is not judged here: no register can join it, and the item stays its
    enrichment, as before."""
    held_lei, held_cik = node.get("lei_id"), node.get("sec_cik")
    if not (held_lei or held_cik) or not (leis or ciks):
        return False
    return not ((held_lei and held_lei in leis) or (held_cik and held_cik in ciks))


def stale(node: dict, item_names: list[str], on: str | None = None) -> str | None:
    """Why a knowledge base's ids name this node only under a name it no longer
    bears, or None. The item's label and aliases match a name the node's
    register says had stopped applying (`former_names`, each with its last day
    in force) and none it still bears (its name, the register's other names).
    BlackRock's Q219635 states the CIK and the LEI of the pre-2024 parent,
    which EDGAR has called "BlackRock Finance, Inc." since 2024-09-26 — the ids
    agree with that node, the names say the item has moved on. An item whose
    names match none of the node's, ended or not, says nothing either way (a
    label in another script, a brand) and is no reason to refuse."""
    from app import former_names
    from app.scraper.mapper import normalize_entity_name
    want = {normalize_entity_name(n) for n in item_names if n} - {""}
    if not want:
        return None
    live = {normalize_entity_name(n) for n in [node.get("name"), *(node.get("other_names") or [])] if n}
    if want & live:
        return None
    gone = want & former_names.ended_keys(former_names.of_node(node), node.get("name"), on)
    if not gone:
        return None
    return (f"the item's name ({', '.join(sorted(gone))}) is a former name of "
            f"{node.get('name') or 'the node'}")


def moved_on(node: dict, item_names: list[str], on: str | None = None) -> str | None:
    """`stale`'s reason when the item has moved on to another legal entity, or
    None. A former name the item bears says so only when a register says
    another registrant now bears that name: EDGAR lists "BlackRock, Inc." as
    CIK 0002012383, the 2024 parent, while the item's ids are the old parent's
    (BlackRock Finance, Inc., CIK 0001364742). A bare rename of the same
    registrant is not a move: EDGAR renamed CIK 0000038777 "FRANKLIN TEMPLETON
    INC" on 2026-08-14 (GLEIF's record RIFQSET379FOGTEFKS80 likewise, previous
    legal name "FRANKLIN RESOURCES, INC."), and the item Q744683 — label
    "Franklin Templeton Investments", alias "Franklin Resources", that LEI and
    that CIK — was refused as stale, with Wellington's (WELLINGTON MANAGEMENT
    CO LLP → GROUP LLP), Mesa Air Group's (→ REPUBLIC AIRWAYS HOLDINGS INC.,
    the same CIK 0000810332) and Grupo Televisa's (S A → S.A.B.) once the
    registers ran before the knowledge bases (2026-11-06 rebuild, findings
    item 66). Asked of EDGAR by each ended name the item bears
    (`sec_edgar.search_company`), the registrant found counts only when its
    name IS that name (`mapper.name_key`; EDGAR's similarity pass would hand
    'Franklin Resources' a Canadian subsidiary) and its CIK is not the
    node's. A node without a CIK, or EDGAR switched off, keeps `stale`'s
    verdict: no register can say the name stayed."""
    why = stale(node, item_names, on)
    if not why:
        return None
    if not node.get("sec_cik") or not _sec_enabled():
        return why
    from app import former_names
    from app.scraper.mapper import name_key, normalize_cik, normalize_entity_name
    from app.scraper.sec_edgar import search_company
    gone = former_names.ended_keys(former_names.of_node(node), node.get("name"), on)
    ended = [n for n in item_names if n and normalize_entity_name(n) in gone]
    for item_name in dict.fromkeys(ended):
        try:
            found = search_company(item_name)
        except Exception as exc:  # noqa: BLE001 — a lost request keeps the refusal, as before
            log.warning("EDGAR unreadable for %r (is the name another registrant's?): %s", item_name, exc)
            return why
        cik = normalize_cik((found or {}).get("cik"))
        if found and cik and cik != node["sec_cik"] and name_key(found.get("name") or "") == name_key(item_name):
            return f"{why}; EDGAR lists {found['name']} (CIK {cik}) under that name"
    log.info("%s — a rename of the same registrant (CIK %s): no other registrant bears %s",
             why, node["sec_cik"], ", ".join(ended))
    return None
