"""
Companies House PSC snapshot importer — current UK beneficial-ownership data,
straight from the source (updated daily), replacing the frozen OpenOwnership UK
PSC BODS export (stuck at 2025-03).

The snapshot is newline-delimited JSON, one PSC per line:
``{"company_number": "...", "data": {"kind": "...", ...}}``. Each PSC controls the
company named only by its **number** (no company name — those come from the
Companies House BasicCompanyData product, a companion importer), so the controlled
company is a node keyed ``gb-coh:{number}`` that this import ensures exists but
leaves un-named.

Mapping:
  * individual PSC          → Person -[:OWNS]-> company
  * corporate/legal PSC     → Entity -[:OWNS]-> company (keyed on its own UK
                              company number when it has one)
  * natures_of_control      → stake_percent (ownership-of-shares band floor),
                              voting_power_pct (voting-rights band floor),
                              ownership_type (controlling when it has voting /
                              appointment / significant-influence)
  * super-secure PSCs and PSC statements are skipped.
"""
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from typing import IO

from app import addresses
from app.scraper.gleif_reference import (canonical_register_number,
                                          general_register_for_country, make_register_id,
                                          register_for_name, register_for_number_format,
                                          register_for_place, sole_register_for_country)
from app.scraper.bulk_import import (
    _BatchWriter, _drop_secondary_indexes, _entity, _max_pct, _now_iso,
    _ProgressBar, _rebuild_indexes,
)
from app.scraper.mapper import derive_ownership_type, parse_full_name
from app.scraper.edge_schema import READ_FIELD

#: the registered office's stored fields (app.addresses)
_REG = addresses.fields(addresses.REGISTERED)

log = logging.getLogger(__name__)

# Every `kind` the snapshot contains, sorted into what it becomes. Counts are per
# 4M sampled records, so the rare ones are rare register-wide, not rare in a sample.
_PERSON_KINDS = (
    "individual-person-with-significant-control",     # 3,698,654
    "individual-beneficial-owner",                    # 8,343 — Register of Overseas Entities
)
_ENTITY_KINDS = (
    "corporate-entity-person-with-significant-control",  # 284,471
    "legal-person-person-with-significant-control",      # 4,924
    # The corporate twins of `individual-beneficial-owner`, both silently dropped
    # until 2026-08-19 while the individual one was mapped. The individual/corporate
    # split is Companies House's, not a distinction the graph cares about.
    "corporate-entity-beneficial-owner",                 # 3,336
    "legal-person-beneficial-owner",                     # 139
)
# Deliberately not imported. A super-secure PSC is one whose details Companies House
# withholds for personal safety; the record carries no name to write. Named here so
# the skip is a decision, rather than whatever falls through the allow-list.
_SKIP_KINDS = (
    "super-secure-person-with-significant-control",   # 116
    "super-secure-beneficial-owner",                  # 17
)
#: A band's floor: ``25-to-50-percent`` → 25, and the Register of Overseas
#: Entities' open band ``more-than-25-percent`` → 25 (every one of the 3,977
#: overseas-entity share records in a 3M-record probe used it, and read as no
#: stake at all until 2026-10-18).
_BAND = re.compile(r"(?:(\d+)-to-\d+|more-than-(\d+))-percent")

#: A US Employer Identification Number, as filers write it: never a register's
#: number. 1,319 active US corporate PSCs give one (Berkshire Hathaway's
#: 47-0813844 beside "US Securities And Exchange Commission Register", Coca-
#: Cola's 58-0628465 beside "Delaware"), and ~1,100 became a register_id —
#: an EDGAR or Delaware number no register issued, which kept each such parent
#: apart from its GLEIF node (findings item 25, 2026-10-31). Only the hyphenated
#: shape: a plain nine digits may be a state's own file number.
_US_EIN = re.compile(r"^\d{2}-\d{7}$")

#: Address lines that are not a town: what the last line may not look like
#: before it is taken for the city a filer moved out of `locality`.
_NOT_A_TOWN = re.compile(r"\d|\b(floor|suite|unit|room|building|tower|p\.?\s*o\.?\s*box)\b", re.IGNORECASE)
#: US states that are also the name of a city — a locality saying so is the city.
_STATE_AND_CITY = frozenset({"new york", "washington"})
#: The natures that state an ECONOMIC interest: shares of a company, or an LLP
#: member's share of the surplus assets — the LLP's equivalent, 16,802 records
#: in the same probe, ignored before.
_ECONOMIC = ("ownership-of-shares", "right-to-share-surplus-assets")


def _band_floor(nature: str) -> int | None:
    m = _BAND.search(nature)
    return int(m.group(1) or m.group(2)) if m else None


def _control(natures: list[str] | None) -> tuple:
    """(stake_percent, voting_power_pct, ownership_type, interest_types) from a
    PSC's natures_of_control — economic vs voting kept separate."""
    natures = natures or []
    stake = voting = None
    controlling = False
    for nat in natures:
        if not nat:
            continue        # 2 of 15.8M records carry a null in the list
        if any(e in nat for e in _ECONOMIC):
            stake = _max_pct(stake, _band_floor(nat))
        elif "voting-rights" in nat:
            voting = _max_pct(voting, _band_floor(nat))
            controlling = True
        elif "right-to-appoint" in nat or "significant-influence" in nat:
            controlling = True
    if controlling:
        otype = "controlling"
    elif stake is not None:
        otype = derive_ownership_type(stake)
    else:
        otype = "minority"
    return stake, voting, otype, sorted(n for n in set(natures) if n)


def _psc_name(data: dict) -> str | None:
    """A corporate or legal PSC's name — the `name` field as the filer wrote it."""
    if name := (data.get("name") or "").strip():
        return name
    ne = data.get("name_elements") or {}
    parts = [ne.get("forename"), ne.get("middle_name"), ne.get("surname")]
    return " ".join(p for p in parts if p).strip() or None


#: The titles Companies House prints in front of an individual's `name`.
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor", "sir",
                     "dame", "lord", "lady", "rev", "reverend", "hon", "the hon"})


def _person_name(data: dict) -> tuple[str, str, str] | None:
    """(full_name, first_name, last_name) of an individual PSC.

    The record's `name` is a DISPLAY string Companies House assembles as
    title + forenames + surname — "Mr Michael Charles Saunders" — and 90 % of
    the 2.78M individuals in a 3M-record probe carry one. Read as a name it
    made `first_name = "Mr"`, so no PSC person ever met a Wikidata or SEC twin
    in the dedup (its name tokens were wrong) and the panel addressed people
    by their honorific. The parts are in `name_elements` (forename,
    middle_name, surname, title): the name is built from them, the title left
    out. A record without elements has the leading title stripped off `name`
    and the rest split as before.
    """
    ne = data.get("name_elements") or {}
    fore = (ne.get("forename") or "").strip()
    middle = (ne.get("middle_name") or "").strip()
    sur = (ne.get("surname") or "").strip()
    if fore or sur:
        full = " ".join(p for p in (fore, middle, sur) if p)
        return full, fore, sur
    name = (data.get("name") or "").strip()
    words = name.split()
    if words and words[0].rstrip(".").lower() in _TITLES:
        words = words[1:]
    name = " ".join(words)
    if not name:
        return None
    first, last = parse_full_name(name)
    return name, first, last


def _nationality(raw: str | None) -> str:
    """PSC nationality as an ISO-2 code where it can be recognised.

    Companies House records a **demonym** typed by the filer — "British", not "GB"
    — while Wikidata writes an ISO-2 code, so the field used to hold `GB` and
    `British` side by side, meaning the same thing and grouping as two.

    An unrecognised value is kept **verbatim**: it is free text and the tail is
    long, and what the register said is worth more than a tidy blank.
    """
    from app.scraper.maintenance import nationality_to_iso2
    return nationality_to_iso2(raw) or (raw or "").strip()


def _birth_date(data: dict) -> str | None:
    """The register's month and year of birth, `YYYY-MM` (or `YYYY`); None when
    it publishes none — never "": an empty string is not a date, and the
    schema's date rule refuses it (2026-10-25)."""
    dob = data.get("date_of_birth") or {}
    y, m = dob.get("year"), dob.get("month")
    return f"{y:04d}-{m:02d}" if y and m else (f"{y:04d}" if y else None)


def psc_slug_id(self_link: str) -> str:
    """A CH PSC self-link → a slug-safe node id: ``chpsc:{company}:{notification}``.

    The id used to be ``chpsc:{self_link}`` verbatim — slashes included — and an
    id with slashes can never match a path parameter (the ASGI server decodes
    %2F before routing), so every such node's page simply failed to load (the
    second Tesla, 2026-09-03). The link's company number plus its final segment
    (the notification id, unique within the company) identify the appointment
    just as well, in two colon-joined slug-safe tokens. The raw link itself
    still travels separately as provenance (``psc_self_link`` on the edge).

    An unrecognised link shape degrades to colon-for-slash substitution rather
    than crashing an import over one malformed record.
    """
    parts = [seg for seg in (self_link or "").strip().split("/") if seg]
    if len(parts) >= 3 and parts[0] == "company":
        return f"chpsc:{parts[1]}:{parts[-1]}"
    return "chpsc:" + (self_link or "").strip().replace("/", ":")


def _entity_psc_id(data: dict) -> tuple[str, str | None]:
    """(node_id, companies_house_id) for a corporate/legal PSC — keyed on its own
    UK company number when it has one, else on the PSC self-link."""
    ident = data.get("identification") or {}
    reg = (ident.get("registration_number") or "").strip()
    if reg and _psc_country(ident) == "GB":
        # Companies House numbers are eight characters; filers drop the
        # leading zeros ("41424" for Unilever PLC's 00041424), which minted a
        # second node beside the GLEIF one keyed on the padded form.
        if reg.isdigit() and len(reg) < 8:
            reg = reg.zfill(8)
        return f"gb-coh:{reg}", reg
    self_link = ((data.get("links") or {}).get("self") or "").strip()
    return psc_slug_id(self_link), None


#: The rules, in the order they are tried — see `resolve_register_code`.
REGISTER_RULES = ("named", "sole", "place", "format", "general")


def resolve_register_code(iso2: str | None, ident: dict, number: str) -> tuple[str | None, str | None]:
    """Which register a corporate PSC's number belongs to — (RA code, rule).

    1. **named** — the register the filer NAMED in `place_registered` or
       `legal_authority`: no guessing when the words fit exactly one register
       of the country, and the only rule that keys a fund (naming the CSSF
       gets the CSSF register) or a German company (naming the court).
    2. **sole** — the country has exactly one register in GLEIF's list.
    3. **place** — a US state named in any field (the audited place map).
    4. **format** — the number's format names the register (Japan, Switzerland).
    5. **general** — the register the country's companies sit on ≥ 90% of the
       time, from the GLEIF audit.
    Rule 1 needs the filer's words; the rest need only country and number.
    `manage.py audit-psc-registers` counts what each rule catches.
    """
    if iso2 == "GB":
        # UK controllers are keyed by companies_house_id (gb-coh ids); GB has
        # no dominant register and Companies House runs three. Naming would
        # only mis-key — "England And Wales" fits the Charity Commission.
        return None, None
    for field in ("place_registered", "legal_authority"):
        code = register_for_name(iso2, ident.get(field))
        if code:
            return code, "named"
    code = sole_register_for_country(iso2)
    if code:
        return code, "sole"
    # Filers scatter the register across three fields — Tesla's says
    # legal_authority "Texas", place_registered "N/A"; junk resolves to nothing.
    for field in ("place_registered", "legal_authority", "country_registered"):
        code = register_for_place(iso2, (ident.get(field) or "").strip())
        if code:
            return code, "place"
    code = register_for_number_format(iso2, number)
    if code:
        return code, "format"
    code = general_register_for_country(iso2)
    if code:
        return code, "general"
    return None, None


def _psc_jurisdiction(ident: dict, iso2: str | None) -> str | None:
    """The ISO 3166-2 subdivision a corporate PSC says it is registered in
    ("State Of Delaware", "Delaware, Usa" → US-DE), from the first of its
    register fields that names one in its own country; None otherwise."""
    from app.scraper.sec_ex21 import jurisdiction_subdivision
    for field in ("legal_authority", "place_registered", "country_registered"):
        text = re.sub(r"(?i)^\s*(the\s+)?(state|commonwealth)\s+of\s+", "", ident.get(field) or "")
        code = jurisdiction_subdivision(text)
        if code and (not iso2 or code.split("-")[0] == iso2):
            return code
    return None


def _psc_country(ident: dict) -> str | None:
    """The ISO-2 country a corporate PSC is registered in.

    `country_registered` first; when that says nothing usable ("Not
    Specified/Other" — 999 filers), the register fields: a filer who names
    "Companies House" or "England And Wales" there has said where.
    """
    iso2 = _iso2_country(ident.get("country_registered"))
    if iso2:
        return iso2
    for field in ("place_registered", "legal_authority"):
        text = (ident.get(field) or "").strip()
        if "companies house" in text.lower():
            return "GB"
        iso2 = _iso2_country(text)
        if iso2:
            return iso2
    return None


#: What filers write for a country that the shared name table does not carry:
#: shorthand, codes, subdivisions, one recurring typo. From the PSC survey,
#: each with its count among foreign controllers.
_COUNTRY_SPELLINGS = {
    "usa": "US", "u.s.a.": "US", "u.s.": "US", "united states of america": "US",
    "british virgin islands": "VG", "bvi": "VG", "virgin islands, british": "VG",   # 1,308 + 311
    "gbr": "GB", "gb-eng": "GB", "gb-sct": "GB", "gb-wls": "GB", "gb-nir": "GB",     # 949 + 936 + 427
    "united kingdon": "GB", "u.k.": "GB", "london": "GB",                            # 260 + 206 + 211
    "the netherlands": "NL", "holland": "NL", "republic of ireland": "IE",         # 347, 296
    "hong kong sar": "HK", "hong kong, china": "HK", "uae": "AE", "south korea": "KR",
    "russia": "RU", "czech republic": "CZ", "taiwan": "TW", "macau": "MO",
}


def _iso2_country(name: str | None) -> str | None:
    """A PSC ``country`` name → ISO-2 code. Companies House uses UK
    subdivisions ('England', 'England & Wales', 'Scotland', …) which aren't ISO
    countries — all map to GB; a US state named as the country ("Delaware",
    "Delaware, Usa" — 3,000 filers) is US; other names go through the shared
    name→code table, then the spellings above."""
    if not name:
        return None
    n = " ".join(name.strip().lower().split())
    if n in ("uk", "gb") or any(uk in n for uk in (
            "england", "wales", "scotland", "northern ireland",
            "united kingdom", "great britain")):
        return "GB"
    if n in _COUNTRY_SPELLINGS:
        return _COUNTRY_SPELLINGS[n]
    from app.scraper.bulk_import import _ISO2_COUNTRY
    from app.scraper.gleif_reference import _US_STATE_NAMES
    hit = {v.lower(): k for k, v in _ISO2_COUNTRY.items()}.get(n)
    if hit:
        return hit
    if n.split(",")[0].strip() in _US_STATE_NAMES:
        return "US"
    return name.strip().upper() if len(name.strip()) == 2 else None


def _psc_address(addr: dict | None) -> dict:
    """A corporate PSC's ``address`` (the register's "registered or principal
    office address" of the owner) as the Entity props it fills: the string
    ``address`` (the panel's "Registered address" row) with its matching key
    ``address_normalized`` (the same-name dedup's corroborator), and the PARTS ``reg_street`` (premises + address lines, joined — the
    register puts a building and a street across them and only their order
    says which is which), ``reg_city`` and ``reg_postcode``, so the geocoder
    runs a structured query for the REGISTERED pin instead of re-parsing the
    string. Until 2026-10-18 the parts were dropped — 203k of 219k corporate
    PSCs carry one.

    Nothing under ``hq_*``: the register states where the owner is registered,
    not where it is run. Until 2026-10-18 the same address was copied into the
    headquarters fields as well, which labelled an accountant's or agent's
    office "Headquarters" and overwrote the HQ GLEIF or Wikidata had put on a
    merged node (finding #7 of the PSC checklist walk). A headquarters now comes
    only from a source that states one. Absent values are None."""
    addr = addr or {}

    def _v(key: str) -> str | None:
        return (str(addr.get(key) or "")).strip() or None

    lines = [p for p in (_v("premises"), _v("address_line_1"), _v("address_line_2")) if p]
    joined = lines + [p for p in (_v("locality"), _v("region"), _v("postal_code"), _v("country")) if p]
    street, city = lines, _v("locality")
    if _locality_is_the_state(addr, lines, city):
        # Berkshire Hathaway's: premises "3555 Farnam Street", line 1 "Omaha",
        # locality "Nebraska" — the town moved up a line, the state into its place
        street, city = lines[:-1], lines[-1]
    return addresses.parts(addresses.REGISTERED, street=", ".join(street) or None, city=city,
                           postcode=_v("postal_code"), full=", ".join(joined) or None,
                           empty=("full", "key", "street", "city", "postcode"))


def _locality_is_the_state(addr: dict, lines: list[str], locality: str | None) -> bool:
    """A US address whose `locality` is a state's name and whose last address
    line, after the premises, is a town: the filer put the town one field up."""
    if not locality or len(lines) < 2 or (addr.get("region") or "").strip():
        return False
    if _iso2_country(addr.get("country")) != "US" or locality.strip().lower() in _STATE_AND_CITY:
        return False
    from app.scraper.sec_ex21 import jurisdiction_subdivision
    state = jurisdiction_subdivision(locality)
    return bool(state and state.startswith("US-") and not re.fullmatch(r"[A-Z]{2}", locality.strip())
                and not _NOT_A_TOWN.search(lines[-1]))


@dataclass(frozen=True)
class PscMapped:
    """One PSC record, mapped to graph shapes but not yet written anywhere.

    Pure: no writer, no database, no clock. That is what lets the incremental
    refresh reuse the mapping without reusing the bulk writer, and what lets the
    mapping be unit-tested at all — while it lived inside `_process` the only way
    to see its output was to hand it a batch writer and inspect the calls.
    """
    kind_cat: str                 # 'person' | 'entity'
    self_link: str                # data.links.self — identifies the APPOINTMENT
    company_id: str               # gb-coh:{number}, the company being controlled
    company_props: dict
    owner_id: str
    owner_label: str              # 'Person' | 'Entity'
    owner_props: dict
    edge_props: dict


#: The day the UK register of people with significant control began. Control
#: that already existed was notified on it, so a `notified_on` of this day says
#: only "since this day or earlier" (since_basis `register_start`).
PSC_REGISTER_START = "2016-04-06"

#: The end of the register's first year. A company that existed before the
#: register declared its PSCs on its first confirmation statement — due on the
#: anniversary of its last annual return plus 14 days, i.e. any day up to here
#: — and the snapshot's `notified_on` is THAT day, not when the control began
#: (54,815 records say 2016-07-01 and 28,737 say 2016-06-30 in the first 3M of
#: the 2026-10-16 snapshot: the confirmation statement replaced the annual
#: return on 30 June 2016). A company incorporated after the register began
#: named its PSCs at incorporation, so for it the day is a real start.
PSC_FIRST_YEAR_END = "2017-04-20"
#: since_basis of a first-year notification on a company that predates the
#: register: a lower bound ("held by then"), like `register_start`.
FIRST_YEAR_BASIS = "register_first_year"


def mark_first_year_lower_bounds(company_ids: list[str] | None = None, apply: bool = True,
                                 run_sql=None, run_sqlscript=None) -> dict:
    """Mark the PSC edges (and their claims) whose `since` is a notification
    inside the register's first year on a company incorporated BEFORE the
    register began as lower bounds (`since_basis = register_first_year`).

    The PSC import cannot do this itself: the company's incorporation date
    comes from BasicCompanyData (`ch-company-data`), which runs after it. So
    this runs at the end of that import — scoped to the companies it touched
    (``company_ids``) or, on a full run, over every ``gb-coh:`` company.
    Idempotent: only edges without a basis are touched,
    and the refresh keeps the mark on an edge whose record restates the same
    day. ``apply=False`` counts and writes nothing.

    Walks the companies by their id index and expands each page's incoming
    OWNS edges — adjacency, never a scan of OWNS (see `_owns_pairs_by_page`
    in maintenance for why). The claim is updated by `to_id` + `since`, the
    key the Sources panel reads it by.
    """
    from app.db import paging
    if run_sql is None or run_sqlscript is None:
        from app.db import arcadedb
        run_sql = run_sql or arcadedb.run_sql
        run_sqlscript = run_sqlscript or arcadedb.run_sqlscript
    counts = {"companies_before_register": 0, "edges": 0, "claims": 0, "applied": bool(apply)}

    def pages():
        cols = "id, @rid AS rid, incorporated_on"
        if company_ids is None:
            yield from paging.iter_id_pages("Entity", prefix="gb-coh:", columns=cols)
            return
        ids = sorted(set(company_ids))
        for i in range(0, len(ids), 500):
            quoted = ", ".join(f"'{paging._sql_str(x)}'" for x in ids[i:i + 500])
            yield run_sql(f"SELECT {cols} FROM Entity WHERE id IN [{quoted}]")

    for page in pages():
        old = {r["rid"]: r["id"] for r in page
               if r.get("rid") and r.get("incorporated_on") and str(r["incorporated_on"]) < PSC_REGISTER_START}
        counts["companies_before_register"] += len(old)
        if not old:
            continue
        edges = run_sql(
            "SELECT @rid AS rid, @in AS i, since AS s "
            f"FROM (SELECT expand(inE('OWNS')) FROM [{', '.join(old)}]) "
            "WHERE filing_type = 'PSC' AND since_basis IS NULL "
            f"AND since > '{PSC_REGISTER_START}' AND since <= '{PSC_FIRST_YEAR_END}'")
        if not edges:
            continue
        counts["edges"] += len(edges)
        statements = sorted({(old[str(e["i"])], str(e["s"])) for e in edges if str(e["i"]) in old})
        counts["claims"] += len(statements)
        if not apply:
            continue
        stmts = [f"UPDATE {e['rid']} SET since_basis = '{FIRST_YEAR_BASIS}'" for e in edges]
        params: dict = {}
        for k, (company_id, since) in enumerate(statements):
            params[f"t{k}"], params[f"s{k}"] = company_id, since
            stmts.append(f"UPDATE Claim SET since_basis = '{FIRST_YEAR_BASIS}' "
                         f"WHERE kind = 'owns' AND filing_type = 'PSC' AND to_id = :t{k} "
                         f"AND since = :s{k} AND since_basis IS NULL")
        run_sqlscript(";".join(stmts), params)
    log.info("PSC first-year lower bounds: %s", counts)
    return counts


def psc_record(rec: dict, source_id: str, credibility_score: int,
               snapshot_date: str | None = None) -> PscMapped | None:
    """Map one snapshot record, or None when it is not one we import.

    `last_scraped_at` is deliberately absent from `edge_props`: it is a clock
    reading, and leaving it to the caller keeps this function's output a pure
    function of its input — which is what the refresh's digest depends on.

    ``snapshot_date`` is the day Companies House published the file the record
    came from (stamped into the entry name): the edge's `source_date`, i.e. when
    the register last said this. It used to be `notified_on`, which is when
    the control BEGAN — a 2016 appointment confirmed by the 2026-10-16 snapshot
    read as ten-year-old evidence and lost every freshness tie to GLEIF. The
    start stays in `since`. Without a date (a hand-made file) `notified_on`
    stands in: a lower bound on when the register said it.
    """
    company_number = (rec.get("company_number") or "").strip()
    data = rec.get("data") or {}
    kind = data.get("kind")
    if not company_number or kind not in _PERSON_KINDS + _ENTITY_KINDS:
        return None
    name = _psc_name(data)
    if not name:
        return None
    self_link = ((data.get("links") or {}).get("self") or "").strip()

    stake, voting, otype, interest_types = _control(data.get("natures_of_control"))
    since = data.get("notified_on") or None
    until = data.get("ceased_on") or None
    # The register began on 6 April 2016, and control that already existed was
    # notified THAT day: a lower bound, not a start. As a stated start it hid
    # a parent that has held its subsidiary since 1990 before 2016.
    since_basis = "register_start" if since == PSC_REGISTER_START else None

    if kind in _PERSON_KINDS:
        parts = _person_name(data)
        if parts is None:
            return None
        name, first, last = parts
        owner_id, owner_label = psc_slug_id(self_link), "Person"
        nationality = _nationality(data.get("nationality"))
        owner_props = {
            "first_name": first, "last_name": last, "full_name": name,
            "search_text": name,
            "birth_date": _birth_date(data),      # no description: absent, never ''
            # the list only when the record states one: an empty list is a blank
            "aliases": [], **({"nationalities": [nationality]} if nationality else {}),
            # The corporate branch passes it to `_entity`; this one built its
            # props without it, and 7 dev persons had no source (2026-10-25).
            "source_id": source_id,
        }
        kind_cat = "person"
    else:
        owner_id, chid = _entity_psc_id(data)
        # The corporate PSC's own correspondence address (in the record) → its address
        # + map location + the parts; otherwise it's a name-only node.
        address = _psc_address(data.get("address"))
        owner_label, kind_cat = "Entity", "entity"
        ident = data.get("identification") or {}
        reg_number = (ident.get("registration_number") or "").strip() or None
        if chid and reg_number and chid != reg_number:
            reg_number = chid        # the padded UK number the id was minted from
        # A foreign parent's register number used to be dropped entirely (only
        # the UK special case in _entity_psc_id kept one). It is stored now, and
        # becomes the register_id hard identifier when the stated country maps
        # to exactly one register. No UK special-casing needed: GB has 16
        # registers so the sole-register map yields None there (and
        # companies_house_id already covers the merge); DE's 177 per-court
        # registers sharing HRB numbering yield None for the same reason.
        register_id = None
        if reg_number and not (_psc_country(ident) == "US" and _US_EIN.match(reg_number)):
            iso2 = _psc_country(ident)
            code, _rule = resolve_register_code(iso2, ident, reg_number)
            if code:
                register_id = make_register_id(code, canonical_register_number(iso2, reg_number))
        # The country as an ISO-2 code like every other source, not the filer's
        # words: "England", "England & Wales" and "United Kingdom" are one GB,
        # and a node reading "Switzerland" beside one reading "CH" looked like
        # two countries. The words stay in registration_authority when the
        # filer put the register there. A name that resolves to no code ("Not
        # Specified/Other" — 271 in 3M records — or an ambiguous "Korea") is
        # kept as the filer wrote it in `country_registered`, never in
        # `country`: that field is a code everywhere else, and a country
        # filter or flag given words finds nothing or draws nonsense.
        iso2 = _psc_country(ident)
        country_text = (ident.get("country_registered") or "").strip() or None
        owner_props = {
            "name": name, "entity_type": "company",
            "country": iso2,
            "country_registered": None if iso2 else country_text,
            "companies_house_id": chid,
            # the registered office with its key and parts; no headquarters —
            # the register does not say where the owner is run (see _psc_address)
            **address,
            "registration_number": reg_number,
            "registration_authority": (ident.get("place_registered") or "").strip() or None,
            "register_id": register_id,
            # where the filer says it is registered, to the state ("State Of
            # Delaware" → US-DE): what the GLEIF join compares (item 25)
            "jurisdiction_code": _psc_jurisdiction(ident, iso2),
        }

    return PscMapped(
        kind_cat=kind_cat,
        self_link=self_link,
        company_id=f"gb-coh:{company_number}",
        # The controlled company is ensured to exist, and named later from
        # BasicCompanyData — this import knows its number and nothing else.
        company_props={"companies_house_id": company_number, "source_id": source_id},
        owner_id=owner_id,
        owner_label=owner_label,
        owner_props=owner_props,
        edge_props={
            "filing_type": "PSC",
            "read_from": READ_FIELD,   # the snapshot's natures_of_control field
            "stale": False,            # a flag is true or false, never unset
            "stake_percent": stake, "voting_power_pct": voting, "ownership_type": otype,
            "interest_types": interest_types, "direct_or_indirect": None,
            "since": since, "since_basis": since_basis, "until": until,
            # why the edge ended: the register says the control ceased
            "until_reason": "ceased" if until else None,
            "source_id": source_id,
            "credibility_score": credibility_score,
            # The key the incremental refresh matches an edge on. Per appointment,
            # so exactly one edge per snapshot record — see ch_psc_incremental.
            "psc_self_link": self_link,
            "source_url": f"https://find-and-update.company-information.service.gov.uk/company/{company_number}/persons-with-significant-control",
            # when the register said it: the snapshot's date, not the start
            "source_date": snapshot_date or since,
        },
    )


def write_entity_owner(batch, mapped: PscMapped, source_id: str, credibility_score: int) -> None:
    """Upsert a corporate PSC's node from its mapped props — the ONE place the
    bulk load and the refresh write it. The refresh had its own copy that
    passed no `extra`, so a corporate PSC touched by a refresh lost its
    registration number, register authority and `register_id` hard id."""
    props = mapped.owner_props
    _entity(batch, mapped.owner_id, name=props["name"], entity_type=props["entity_type"],
            country=props["country"], founded=None, lei_id=None,
            companies_house_id=props["companies_house_id"],
            source_id=source_id, credibility_score=credibility_score,
            address_normalized=props[_REG["key"]],
            extra={"registration_number": props["registration_number"],
                   "registration_authority": props["registration_authority"],
                   "register_id": props["register_id"],
                   "country_registered": props["country_registered"],
                   # the display address and its parts only when the record
                   # has them: a bulk SET of null would blank what GLEIF wrote
                   # on a merged node
                   **{_REG[p]: props[_REG[p]] for p in ("full", "street", "city", "postcode")
                      if props.get(_REG[p])}})


def _process(rec: dict, batch: "_BatchWriter", source_id: str, credibility_score: int,
             snapshot_date: str | None = None) -> str | None:
    """Write one PSC record's nodes + OWNS edge. Returns 'person' | 'entity' | None."""
    mapped = psc_record(rec, source_id, credibility_score, snapshot_date)
    if mapped is None:
        return None

    batch.entity(mapped.company_id, mapped.company_props)
    if mapped.kind_cat == "person":
        batch.person(mapped.owner_id, mapped.owner_props)
    else:
        write_entity_owner(batch, mapped, source_id, credibility_score)

    batch.owns(mapped.owner_id, mapped.owner_label, mapped.company_id,
               {**mapped.edge_props, "last_scraped_at": _now_iso()})
    return mapped.kind_cat


def _snapshot_date_or_none(entry_name: str) -> str | None:
    """The snapshot's date from its entry name, None (with a warning) when the
    name carries none — a hand-made file; the edges then date by `notified_on`."""
    from app.scraper.ch_psc_incremental import snapshot_date
    try:
        return snapshot_date(entry_name)
    except ValueError:
        log.warning("CH PSC: no snapshot date in %r — edges dated by notified_on", entry_name)
        return None


def import_ch_psc(filepath: str, source_id: str, credibility_score: int,
                  limit: int | None = None, bulk_load: bool = False,
                  batch_size: int = 400,
                  only_companies: set[str] | None = None,
                  digest_out: str | None = None) -> dict:
    """Import a Companies House PSC snapshot (.zip/.txt). Returns counts.

    ``batch_size`` sets how many records flush per ``sqlscript`` round-trip. Behind
    a proxy with a short read timeout (e.g. dev-db's 60s nginx), a smaller batch
    keeps each flush well under the limit so heavy flushes don't 504-then-retry
    (the dominant cost of a slow import); connected directly to ArcadeDB, a larger
    batch cuts round-trips.

    ``only_companies`` restricts the import to that set of company numbers (the curated
    test subset). A cheap raw-bytes prefilter skips JSON parsing for non-matches, so a
    handful of companies still loads quickly — but the whole file is read.

    ``digest_out`` writes the digest sidecar the incremental refresh diffs against,
    from this same pass over the same bytes so the two can never describe different
    files. It covers **every record in the snapshot**, including those this load
    skipped: the refresh digests the whole file, so a digest of only the subset
    would make the next run see the entire register as new. That means a subset
    load with ``--digest-out`` gives up the prefilter and parses all 15.8M lines.

    It used to stop early, once every target company had been seen, on the stated
    assumption that "a CH snapshot groups a company's PSC records together". **It does
    not.** Measured over 300,000 lines of the real snapshot: 41,847 of 247,219 distinct
    companies reappear after another company has intervened — **16.9%**. Stopping at the
    first sighting therefore dropped later PSC records for about one company in six, and
    silently: the run reported success with a plausible count. Every curated database
    built this way, including the dev graph and `psc-import-test.sh`, is affected.

    A full pass over 12.9 GB costs a couple of minutes with the prefilter doing the work.
    That is the price of the subset being complete, and the refresh that rides on this
    baseline can only be correct if the baseline is."""
    if filepath.lower().endswith(".zip"):
        zf = zipfile.ZipFile(filepath)
        entry = zf.namelist()[0]
        total_bytes = zf.getinfo(entry).file_size
        raw: IO[bytes] = zf.open(entry)          # ZipExtFile — line-iterable
        log.info("CH PSC: reading %s (%s bytes)", entry, f"{total_bytes:,}")
    else:
        entry = os.path.basename(filepath)
        total_bytes = os.path.getsize(filepath)
        raw = open(filepath, "rb")  # noqa: WPS515
    snap_date = _snapshot_date_or_none(entry)

    counts = {"records": 0, "persons": 0, "entities": 0, "skipped": 0, "errors": 0}
    only_bytes = [c.encode() for c in only_companies] if only_companies else None
    # The incremental refresh diffs against a digest of the snapshot this load came
    # from. Writing it here, from the same pass over the same bytes, is what stops
    # baseline and digest ever describing different files. Streamed to disk — the
    # register is 15.6M rows and holding them would cost more memory than the
    # import itself.
    digest_sink = None
    if digest_out:
        from app.scraper.ch_psc_incremental import DigestSink
        digest_sink = DigestSink(digest_out)
    matched: set[str] = set()
    if bulk_load:
        _drop_secondary_indexes()
    batch = _BatchWriter(batch_size=batch_size)
    bar = _ProgressBar("CH PSC")
    done = 0
    try:
        for line in raw:                          # one JSON record per line
            done += len(line)
            line = line.strip()
            if not line:
                continue
            if limit and counts["records"] >= limit:
                break
            # Curated subset: reject non-matching lines before the JSON parse (cheap
            # bytes search), then confirm the exact company_number after parsing.
            #
            # …unless a digest is being written, in which case every line has to be
            # parsed anyway. The digest describes the SNAPSHOT, not the subset we
            # chose to import: the refresh digests the whole file and diffs the two,
            # so a subset digest would make the next run see 15.8M records "added".
            # A subset load with --digest-out therefore costs a full parse.
            prefiltered_out = only_bytes is not None and not any(cb in line for cb in only_bytes)
            if prefiltered_out and digest_sink is None:
                continue
            if not prefiltered_out:
                counts["records"] += 1
                if counts["records"] % 50000 == 0:
                    bar.render(done, total_bytes)
            try:
                rec = json.loads(line)
                if digest_sink is not None:
                    digest_sink.add_record(rec)
                if prefiltered_out:
                    continue                      # digested, but not ours to import
                if only_companies is not None:
                    cn = (rec.get("company_number") or "").strip()
                    if cn not in only_companies:
                        continue                  # substring hit in another field
                    matched.add(cn)
                cat = _process(rec, batch, source_id, credibility_score, snap_date)
                if cat == "person":
                    counts["persons"] += 1
                elif cat == "entity":
                    counts["entities"] += 1
                else:
                    counts["skipped"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad line mustn't abort
                counts["errors"] += 1
                if counts["errors"] <= 5:
                    log.warning("CH PSC record error: %s", exc)
        batch.flush()
        if only_companies is not None:
            # Now that the whole file is read, this is a real answer rather than a
            # restatement of the stop condition: how many of the companies asked for
            # actually carry PSC records. A company with none is normal (dissolved,
            # exempt, or simply not filed), but a large gap means the wrong list.
            counts["requested"] = len(only_companies)
            counts["found"] = len(matched)
            missing = len(only_companies) - len(matched)
            if missing:
                log.info("CH PSC: %d of %d requested companies had no PSC records",
                         missing, len(only_companies))
        bar.finish(f"{counts['records']:,} records, "
                   f"{counts['persons']:,} persons + {counts['entities']:,} entities"
                   + (f", {counts['found']}/{counts['requested']} requested companies found"
                      if only_companies is not None else ""))
    except BaseException:
        if digest_sink is not None:
            digest_sink.abandon()      # a partial sidecar must never look like a baseline
        raise
    finally:
        raw.close()
        if bulk_load:
            _rebuild_indexes()

    if digest_sink is not None:
        counts["digest"] = digest_sink.finish()
        counts["digest_records"] = digest_sink.rows

    log.info("CH PSC import done: %s", counts)
    return counts
