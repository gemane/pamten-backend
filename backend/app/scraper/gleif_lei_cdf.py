"""
GLEIF LEI-CDF (Level 1) *entity* importer — the current, authoritative GLEIF
entity data (name, country, address, legal form) from the golden copy.

Replaces the frozen OpenOwnership GLEIF BODS export (stuck at 2025-03) as the
source of GLEIF entities: LEI-CDF is the same global population (3.4M LEIs, all
jurisdictions) but current, and it names the entities the RR-CDF importer would
otherwise leave as LEI-only placeholders. Relationships still come from RR-CDF,
succession from the LEI-CDF succession importer.

Entities are keyed `lei:{LEI}` (the same key the RR/succession importers use),
so they all upsert the same nodes. It refreshes name/country/
address/legal-form-type + is_nominee, and leaves enrichment from other sources
(description, wikidata_id, hq_lat, revenue) untouched.

Each record also states how far GLEIF checked it (`ValidationSources`), which
scales the credibility stamped on the entity — see `_validated_credibility`.

LEI-CDF JSON quirks: array key `records`; scalars wrapped `{"$": value}`.
"""
import json
import logging
import os
import re
import zipfile
from typing import IO

from app import addresses
from app.scraper.bulk_import import (
    _BatchWriter, _drop_secondary_indexes, _legal_form_type, _ProgressBar,
    _ProgressStream, _rebuild_indexes,
)
from app.scraper.gleif_reference import (
    legal_form_name, make_register_id, registration_authority_name)
from app.scraper.gleif_succession import _iter_lei_records, _legal_name, _v
from app.scraper.mapper import is_nominee_name, normalize_entity_name

log = logging.getLogger(__name__)


def _country(entity: dict) -> str | None:
    """ISO-2 country from LegalJurisdiction (e.g. 'US', or 'US-DE' → 'US'),
    falling back to the headquarters country.

    Stays coarse on purpose: `country` is what the map groups by, what the search
    filter offers and what cross-source dedup compares. The subdivision is kept
    separately by `_jurisdiction_code`, so nothing that reads `country` has to
    learn that 'US-DE' is also the United States."""
    j = _v(entity.get("LegalJurisdiction")) or _v((entity.get("HeadquartersAddress") or {}).get("Country"))
    return j.split("-")[0][:2].upper() if j else None


def _jurisdiction_code(entity: dict) -> str | None:
    """The full ISO 3166-2 legal jurisdiction where GLEIF gives one, e.g. 'US-DE'.

    This is where a company chose to be domiciled at a finer grain than the
    country, and it is the same signal as an offshore registration one level down:
    across 250,000 LEI records the commonest subdivision by a wide margin is
    US-DE — Delaware — followed by Ontario, Scotland, Dubai and Nevis. It arrived
    in every import and was truncated away.

    Only ~1% of records carry one, and only six countries use them at all (US 90%,
    CA 74%, KN 40%, AE 27%, MY 19%, GB 0.3%). Elsewhere the register is regionally
    administered but the region is not a choice of domicile, so there is nothing
    to record. Sparse by nature — absent means "not stated", never "none".

    Note this is *not* how the offshore territories are represented: the Caymans
    are `KY`, their own country, not a subdivision of GB. See docs/data-model.md.
    """
    j = _v(entity.get("LegalJurisdiction"))
    if not j or "-" not in j:
        return None
    return j.strip().upper()


def _address_lines(addr: dict) -> list[str]:
    """Street lines of a GLEIF address: FirstAddressLine + AdditionalAddressLine.
    AdditionalAddressLine is a LIST in the CDF (0..n lines) — flatten it so a multi-
    line address (e.g. 'C/O …', street, suite) isn't truncated to one line."""
    lines = [_v(addr.get("FirstAddressLine"))]
    extra = addr.get("AdditionalAddressLine")
    if isinstance(extra, list):
        lines += [_v(x) for x in extra]
    elif extra:
        lines.append(_v(extra))
    return [ln.strip() for ln in lines if ln and ln.strip()]


def _display_address(entity: dict) -> str | None:
    """Human-readable LegalAddress as GLEIF stores it (original case), comma-joined:
    street line(s), city, postcode, country. For the node Details section. Region is
    skipped — GLEIF stores it as a raw ISO 3166-2 code (e.g. 'GB-LND'), not a name."""
    addr = entity.get("LegalAddress") or {}
    parts = _address_lines(addr) + [
        _v(addr.get("City")),
        _v(addr.get("PostalCode")),
        _v(addr.get("Country")),
    ]
    joined = ", ".join(p.strip() for p in parts if p and p.strip())
    return joined or None


def _hq_address(entity: dict) -> str | None:
    """Human-readable full HeadquartersAddress (the operating HQ) — geocoded to the
    map pin. Distinct from the legal `address`, which can be a registered-agent office."""
    hq = entity.get("HeadquartersAddress")
    if not hq:
        return None
    parts = _address_lines(hq) + [_v(hq.get("City")), _v(hq.get("PostalCode")), _v(hq.get("Country"))]
    joined = ", ".join(p.strip() for p in parts if p and p.strip())
    return joined or None


def _address_parts(addr: dict | None) -> dict:
    """A GLEIF address as its parts, for a structured geocode.

    Nominatim takes street/city/postcode/country separately, and GLEIF hands them
    to us that way — so the parts are kept, not just the string we assemble for
    display. Flattening them and asking a gazetteer to work out which piece was
    the city is a problem we were creating for ourselves, and one that does not
    generalise: every country writes an address differently.

    The street is the address lines joined, since GLEIF puts a care-of line, a
    building and a street across 0..n of them and only their order says which is
    which. Nominatim reads that as one street field happily enough.
    """
    if not addr:
        return {}
    street = ", ".join(_address_lines(addr))
    city = _v(addr.get("City"))
    postcode = _v(addr.get("PostalCode"))
    country = _v(addr.get("Country"))
    return {k: v for k, v in {
        "street": street or None,
        "city": city or None,
        "postcode": postcode or None,
        "country": (country[:2].upper() if country else None),
    }.items() if v}


def _legal_form_code(entity: dict) -> str | None:
    """The ISO 20275 ELF code as GLEIF states it (H0PO, 8888 for "other") —
    beside the resolved name, so a filter can group the 171 spellings the
    names come in (local-language and case variants on dev, 2026-10-25)."""
    return _v((entity.get("LegalForm") or {}).get("EntityLegalFormCode"))


def _legal_form(entity: dict) -> str | None:
    """Legal form name: resolve the ISO 20275 ELF code (e.g. H0PO → 'Private Limited
    Company') via the bundled GLEIF list; fall back to the free-text OtherLegalForm,
    then the raw code so nothing is silently dropped for an unlisted code."""
    lf = entity.get("LegalForm") or {}
    code = _v(lf.get("EntityLegalFormCode"))
    return legal_form_name(code) or _v(lf.get("OtherLegalForm")) or code


# GLEIF Registration Authority code for the UK Companies House register. A GB company's
# RegistrationAuthorityEntityID here IS its Companies House number, so we key it the same
# way the PSC importer keys gb-coh:{number} → the two sources dedup on companies_house_id.
_COMPANIES_HOUSE_RA = "RA000585"


def _registration(entity: dict) -> tuple[str | None, str | None, str | None]:
    """(registration authority name, registration number, RA code) from
    RegistrationAuthority — gleif.org's "Registered As / Registered At"."""
    ra = entity.get("RegistrationAuthority") or {}
    code = _v(ra.get("RegistrationAuthorityID"))
    authority = registration_authority_name(code)
    number = _v(ra.get("RegistrationAuthorityEntityID"))
    return authority, number, code


# ── how far GLEIF checked the record ──────────────────────────────────────────
# Every LEI record states its own `ValidationSources`: whether an LOU corroborated
# what the entity said against the business register, or simply took its word for
# it. GLEIF publishes it precisely so consumers can weigh records against each
# other, and it is the only per-record quality signal in the whole file.
#
# It matters here because our credibility score was flat: every GLEIF record
# scored 92 whether a registrar had confirmed it or nobody had looked. That score
# decides which source's name survives a conflict (`runner._upsert_entity`) and
# which claim wins in `claims.py`, so a self-declared name was outranking a
# registry-checked one from elsewhere on the strength of its source alone.
#
# The deductions are small on purpose. An entity's own statement of its own legal
# name is still good evidence — better than a Wikidata label, which is often the
# common name rather than the registered one — so an uncorroborated GLEIF record
# stays above the community sources (80) while losing to a corroborated one.
#
# On a day's delta of 18,166 records: 98.7% FULLY_CORROBORATED, 0.8%
# ENTITY_SUPPLIED_ONLY, 0.4% PARTIALLY_CORROBORATED. So this changes little for
# most companies and everything for the few it is about.
_VALIDATION_PENALTY = {
    "FULLY_CORROBORATED": 0,        # an LOU checked it against the register
    "PARTIALLY_CORROBORATED": -4,   # some of it, or a source short of the register
    "ENTITY_SUPPLIED_ONLY": -10,    # nobody checked; the entity said so
    "PENDING": -10,                 # validation not finished — treated as unchecked
}


def _lei_registration_date(rec: dict) -> str | None:
    """The day the LEI was first issued (``InitialRegistrationDate``).

    Kept because GLEIF's relationship records reuse it: in 27,874 of 260,250
    (2026-10-06) the stated relationship start IS the child's LEI registration
    day, years after the company was founded — Barclays Bank PLC "owned by
    Barclays PLC since 2012-06-06", the day its LEI was issued; owned since
    1985. Such a start is only "at least since" (gleif_rr.REGISTRATION_DAY)."""
    d = _v((rec.get("Registration") or {}).get("InitialRegistrationDate"))
    return d[:10] if d and len(d) >= 10 else None


def _registration_status(rec: dict) -> str | None:
    """The LEI's own ``RegistrationStatus`` (ISSUED, LAPSED, RETIRED, MERGED, …)."""
    return _v((rec.get("Registration") or {}).get("RegistrationStatus"))


def _last_update(rec: dict) -> str | None:
    """The day GLEIF last updated the record (``LastUpdateDate``). For a record
    no longer maintained (LAPSED) it is the day its data was last true — the
    reference day of `legal_identity.same_legal_entity`."""
    d = _v((rec.get("Registration") or {}).get("LastUpdateDate"))
    return d[:10] if d and len(d) >= 10 else None


def _entity_status(rec: dict) -> str | None:
    """``EntityStatus``: ACTIVE, or INACTIVE once the entity has ceased."""
    return _v((rec.get("Entity") or {}).get("EntityStatus"))


def _validation_sources(rec: dict) -> str | None:
    """GLEIF's `ValidationSources` for this record, e.g. 'FULLY_CORROBORATED'.

    Stored raw rather than as a label: it is a published enum with a defined
    meaning, and the UI can say it in prose without us baking a translation into
    the graph."""
    return _v((rec.get("Registration") or {}).get("ValidationSources"))


def _validated_credibility(rec: dict, credibility_score: int) -> int:
    """The source's credibility, reduced by how little of this record was checked.

    An unrecognised or absent value scores the source's own figure unchanged: the
    penalty is for GLEIF *telling us* a record is unverified, never for a field we
    failed to read.
    """
    penalty = _VALIDATION_PENALTY.get(_validation_sources(rec) or "", 0)
    return max(0, credibility_score + penalty)


def _incorporated_on(entity: dict) -> str | None:
    """The legal entity's creation date (EntityCreationDate, YYYY-MM-DD) — when
    this registration began, which is not when the business did: Barclays PLC
    was incorporated in 1896 and traces to 1690. The register states the
    former, so that is all it writes; `founded` (the year of origin) is
    Wikidata's to say (until 2026-10-24 both importers wrote the incorporation
    year into `founded` too, and the two fields contradicted each other on
    22 dev entities)."""
    d = _v(entity.get("EntityCreationDate"))
    if d and len(d) >= 10 and d[4] == "-" and d[7] == "-":
        return d[:10]
    return None


def _dissolved_on(entity: dict) -> str | None:
    """`EntityExpirationDate` (YYYY-MM-DD): the day the entity ceased, when
    the record states one — it rarely does (none in the first 120k records
    of the 2026-10-06 golden copy; INACTIVE alone sets `active = false`).
    `app.dissolution` closes what the company still holds."""
    d = _v(entity.get("EntityExpirationDate"))
    if d and len(d) >= 10 and d[4] == "-" and d[7] == "-":
        return d[:10]
    return None


PREVIOUS_LEGAL_NAME = "PREVIOUS_LEGAL_NAME"
TRADING_NAME = "TRADING_OR_OPERATING_NAME"


def _other_names(entity: dict, legal_name: str) -> tuple[list[str], list[str], list[str]]:
    """(current, previous, trading): every other name the register publishes
    for the entity, deduped, sorted by GLEIF's own ``@type``.

    - **current**: transliterations and alternative-language legal names —
      the bridge for non-Latin jurisdictions: 삼성전자(주) is unfindable by
      "Samsung" without its ALTERNATIVE_LANGUAGE_LEGAL_NAME, and Tencent's
      "Tencent Holdings Limited" is how EDGAR's filer meets its GLEIF record
      (`legal_identity`). An untyped name counts as current.
    - **previous**: PREVIOUS_LEGAL_NAME — ended, so not a name in force
      (findings item 16a: 306 of 538 other names on dev were previous ones,
      and every reader took them for current — the 2024 BlackRock parent's
      "BLACKROCK FUNDING, INC." among them).
    - **trading**: TRADING_OR_OPERATING_NAME — in use, never a legal name.

    Reads BOTH containers (OtherEntityNames, TransliteratedOtherEntityNames),
    tolerates the single-dict-instead-of-list shape the JSON uses for one
    entry, and excludes the legal name itself."""
    current: list[str] = []
    previous: list[str] = []
    trading: list[str] = []
    seen = {(legal_name or "").strip().casefold()}
    for container, key in (("OtherEntityNames", "OtherEntityName"),
                           ("TransliteratedOtherEntityNames",
                            "TransliteratedOtherEntityName")):
        raw = (entity.get(container) or {}).get(key) or []
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw:
            name = _v(item) if isinstance(item, dict) else None
            name = (name or "").strip()
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            kind = item.get("@type") if isinstance(item, dict) else None
            (previous if kind == PREVIOUS_LEGAL_NAME else trading if kind == TRADING_NAME
             else current).append(name)
    return current, previous, trading


def _name_change_day(entity: dict) -> str | None:
    """The effective day of the latest COMPLETED CHANGE_LEGAL_NAME event
    (``LegalEntityEvents``), as the golden copy states it — its local day:
    BlackRock's "2024-10-01T00:00:00+02:00", which the API gives as
    2024-09-30T22:00Z (`register_pointer._api_day` maps it back)."""
    raw = (entity.get("LegalEntityEvents") or {}).get("LegalEntityEvent") or []
    if isinstance(raw, dict):
        raw = [raw]
    days = [(_v(ev.get("LegalEntityEventEffectiveDate")) or "")[:10] for ev in raw
            if isinstance(ev, dict) and _v(ev.get("LegalEntityEventType")) == "CHANGE_LEGAL_NAME"
            and (ev.get("@event_status") or "COMPLETED") == "COMPLETED"]
    days = [d for d in days if re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
    return max(days) if days else None


def _gleif_former_names(entity: dict, previous: list[str]) -> list[str]:
    """The previous legal names as `app.former_names` entries. Only a single
    previous name is dated — the day before the latest name change took
    effect, the last day it applied. With several, GLEIF says neither which
    one a change ended (no record of 77 on dev carries AffectedFields) nor in
    which order it lists them (FMR Investment Management (UK) oldest first,
    Barclays and Eversource newest first), and a change can go unrecorded
    (Blackstone's 2021 rename has no event): they stay "NAME|", ended on an
    unknown day."""
    from datetime import date, timedelta

    from app import former_names
    day = _name_change_day(entity) if len(previous) == 1 else None
    until = (date.fromisoformat(day) - timedelta(days=1)).isoformat() if day else None
    return [e for e in (former_names.entry(n, until) for n in previous) if e]


def _entity_props(rec: dict, source_id: str, credibility_score: int) -> tuple[str, dict] | None:
    """(node_id, props) for one LEI-CDF record, or None when it lacks an LEI/name.
    `type` is set only when the legal form refines it (fund/foundation/nonprofit)
    — otherwise it's left untouched so an existing/Wikidata type isn't clobbered."""
    lei = _v(rec.get("LEI"))
    name = _legal_name(rec)
    if not lei or not name:
        return None
    entity = rec.get("Entity") or {}
    reg_authority, reg_number, reg_code = _registration(entity)
    other_names, previous, trading = _other_names(entity, name)
    props: dict = {
        "name": name,
        "name_normalized": normalize_entity_name(name),
        # The register number joins the search tokens (FULL_TEXT is whole-word),
        # so a company is findable by the id printed on its own paperwork —
        # and so do the register's OTHER names, which is what makes a company
        # with a non-Latin legal name findable in Latin at all.
        # A previous or trading name finds the company too.
        "search_text": " ".join(p for p in (name, *other_names, *trading, *previous,
                                            reg_number or "") if p),
        # The register's own other CURRENT names (alternative-language legal
        # names, transliterations). Separate properties, NOT aliases: the bulk
        # writer overwrites what it sets, and aliases belong to the
        # Wikidata/SEC writers whose union semantics a bulk UPSERT cannot honour.
        "other_names": other_names,
        # its trading names: in use, searchable, never a legal name
        "gleif_trading_names": trading,
        # its previous legal names, dated where GLEIF allows (app.former_names
        # entries). Not `former_names`: a GLEIF record can sit on an EDGAR
        # filer's node (legal_identity), whose `former_names` hold EDGAR's
        # dates, and the bulk writer would overwrite them. Readers take both
        # through `former_names.of_node`.
        "gleif_former_names": _gleif_former_names(entity, previous),
        # Per record, not per source: what this one is worth depends on whether
        # anybody corroborated it (see _validated_credibility).
        "name_credibility": _validated_credibility(rec, credibility_score),
        "country": _country(entity),
        "jurisdiction_code": _jurisdiction_code(entity),
        # the registered office's readable address and its matching key,
        # written even when absent (app.addresses)
        **addresses.parts(addresses.REGISTERED, full=_display_address(entity), empty=("full", "key")),
        "legal_form": _legal_form(entity),
        "legal_form_code": _legal_form_code(entity),
        "registration_authority": reg_authority,
        "registration_authority_code": reg_code,
        "registration_number": reg_number,
        # The fifth hard identifier: unambiguous register + number. UK companies
        # get this AND companies_house_id below — the keys agree on every merge,
        # and register_id also covers the Scottish/NI registers (RA000587/586)
        # that the companies_house_id special case never did.
        "register_id": make_register_id(reg_code, reg_number),
        "incorporated_on": _incorporated_on(entity),
        "lei_registration_date": _lei_registration_date(rec),
        # The register's own word on the record, stamped by BOTH the full load
        # and the daily delta: until 2026-10-13 only the delta did, so a rebuilt
        # database carried neither until a record happened to change. `active`
        # is false once GLEIF says the entity ceased (EntityStatus INACTIVE);
        # the registration status (ISSUED, LAPSED, RETIRED, …) is kept as
        # stated — LAPSED means the LOU no longer maintains the record, which
        # is what makes its relationship records stale (gleif_rr).
        "gleif_registration_status": _registration_status(rec) or "",
        "active": _entity_status(rec) != "INACTIVE",
        "lei_id": lei,
        "source_id": source_id,
        # Deep-link to this company's GLEIF record, not the source home page — the
        # node's "reported / verify" link should open the exact LEI.
        "source_url": f"https://search.gleif.org/#/record/{lei}",
        "is_nominee": is_nominee_name(name),
    }
    validation = _validation_sources(rec)
    if validation:
        props["validation_sources"] = validation
    last_update = _last_update(rec)
    if last_update:
        props["gleif_last_update"] = last_update
    dissolved = _dissolved_on(entity)
    if dissolved:
        props["dissolved_on"] = dissolved      # only when stated: a re-import never blanks it
    # Both places in parts, for a structured geocode (app.addresses): the real
    # operating location with its readable address, and the legal address's
    # parts (its country is the entity's `country`). Only what GLEIF states,
    # so a re-import never blanks what another source (e.g. Wikidata) supplied.
    props.update(addresses.parts(addresses.HEADQUARTERS, **_address_parts(entity.get("HeadquartersAddress")),
                                 full=_hq_address(entity)))
    props.update(addresses.parts(addresses.REGISTERED, **_address_parts(entity.get("LegalAddress"))))
    # UK company: its GLEIF registration number is the Companies House number, so key it
    # like the PSC import (gb-coh:{number}) → GLEIF and PSC nodes merge on this id.
    if reg_code == _COMPANIES_HOUSE_RA and reg_number:
        props["companies_house_id"] = reg_number
    # The legal form names a fund or a foundation; an ordinary company gets no
    # statement, and the bulk writer then keeps what stands or defaults to
    # `company` (bulk_import._DEFAULTED_FIELDS) — never an empty type.
    props["type"] = _legal_form_type(_v((entity.get("LegalForm") or {}).get("OtherLegalForm")))
    return f"lei:{lei}", props


# ── Fast subset path (only_leis) ──────────────────────────────────────────────
# ijson's pure-Python parse of the whole 3.4M-record file takes ~15 min, which is far
# too slow for a handful of test companies. When importing a small allow-list we skip
# ijson entirely: a C-level regex finds each record start + its LEI in the decompressed
# stream, and we json.loads only the matching records. Each record is a
# `{ "LEI": { "$": "<LEI>" }, ... }` element of the `records` array; the golden copy is
# pretty-printed, so the pattern is whitespace-tolerant.
_LEI_RECORD_RE = re.compile(rb'\{\s*"LEI"\s*:\s*\{\s*"\$"\s*:\s*"([0-9A-Z]{20})"')


def _extract_json_object(buf: bytes, start: int) -> bytes | None:
    """The balanced `{...}` object beginning at buf[start] (string/escape aware), or
    None if it is not yet fully present in buf (the caller should read more)."""
    depth = in_str = esc = 0
    for k in range(start, len(buf)):
        c = buf[k]
        if in_str:
            if esc:
                esc = 0
            elif c == 0x5C:          # backslash
                esc = 1
            elif c == 0x22:          # closing quote
                in_str = 0
        elif c == 0x22:              # opening quote
            in_str = 1
        elif c == 0x7B:              # {
            depth += 1
        elif c == 0x7D:              # }
            depth -= 1
            if depth == 0:
                return buf[start:k + 1]
    return None


def _iter_records_for_leis(raw: IO[bytes], targets: set[str], chunk_size: int = 8 << 20):
    """Yield the LEI-CDF records for `targets`, scanning the stream and parsing only
    those records. Stops once every target is found (or the stream ends)."""
    remaining = {t.encode() for t in targets}
    buf = b""
    while remaining:
        chunk = raw.read(chunk_size)
        if not chunk:
            break
        buf += chunk
        consumed = 0
        for m in _LEI_RECORD_RE.finditer(buf):
            if m.group(1) not in remaining:
                consumed = m.end()
                continue
            obj = _extract_json_object(buf, m.start())
            if obj is None:
                break                         # record continues past buf — keep from its start
            remaining.discard(m.group(1))
            try:
                yield json.loads(obj)
            except ValueError:
                pass
            consumed = m.start() + len(obj)
        buf = buf[consumed:]                   # retain the (possibly partial) tail


def import_lei_cdf_entities(
    filepath: str,
    source_id: str,
    credibility_score: int,
    limit: int | None = None,
    filter_jurisdiction: str | None = None,
    bulk_load: bool = False,
    only_leis: set[str] | None = None,
) -> dict:
    """
    Import GLEIF entities from a local LEI-CDF golden-copy .json/.zip.

    `only_leis` restricts the import to that set of LEIs (the curated test subset) —
    reading stops early once all of them are found, so a handful of companies loads
    from the real golden copy in seconds instead of the full 3.4M pass.

    Returns dict: {records, entities, skipped}.
    """
    if filepath.lower().endswith(".zip"):
        zf = zipfile.ZipFile(filepath)
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise ValueError(f"No .json file found inside ZIP: {filepath}")
        entry = names[0]
        total_bytes = zf.getinfo(entry).file_size
        raw: IO[bytes] = zf.open(entry)
        log.info("LEI-CDF entities: reading %s (%s bytes uncompressed)", entry, f"{total_bytes:,}")
    else:
        total_bytes = os.path.getsize(filepath)
        raw = open(filepath, "rb")  # noqa: WPS515

    jur = filter_jurisdiction.upper() if filter_jurisdiction else None
    counts = {"records": 0, "entities": 0, "skipped": 0, "inactive": 0}
    if bulk_load:
        _drop_secondary_indexes()
    batch = _BatchWriter()
    try:
        if only_leis is not None:
            bar = None                       # fast byte-scan path; yields only targets
            record_iter = _iter_records_for_leis(raw, only_leis)
        else:
            bar = _ProgressBar("LEI-CDF entities")
            record_iter = _iter_lei_records(_ProgressStream(raw, total_bytes, bar))
        for rec in record_iter:
            if limit and counts["records"] >= limit:
                break
            counts["records"] += 1
            out = _entity_props(rec, source_id, credibility_score)
            if not out:
                counts["skipped"] += 1
                continue
            node_id, props = out
            if jur and props.get("country") != jur:
                counts["skipped"] += 1
                continue
            batch.entity(node_id, props)
            counts["entities"] += 1
            if not props["active"]:
                counts["inactive"] += 1
        batch.flush()
        msg = (f"{counts['records']:,} records, {counts['entities']:,} entities "
               f"({counts['inactive']:,} inactive)")
        if bar:
            bar.finish(msg)
        else:
            log.info("LEI-CDF subset: %s", msg)
    finally:
        raw.close()
        if bulk_load:
            _rebuild_indexes()

    log.info("LEI-CDF entity import done: %s", counts)
    return counts
