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
(description, wikidata_id, hq_lat, revenue, verified) untouched.

LEI-CDF JSON quirks: array key `records`; scalars wrapped `{"$": value}`.
"""
import json
import logging
import os
import re
import zipfile
from typing import IO

from app.scraper.bulk_import import (
    _BatchWriter, _drop_secondary_indexes, _legal_form_type, _ProgressBar,
    _ProgressStream, _rebuild_indexes,
)
from app.scraper.gleif_reference import legal_form_name, registration_authority_name
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


def _registered_address(entity: dict) -> str | None:
    """Normalized LegalAddress (line + city + postcode + country), lowercased —
    for same-company dedup matching, NOT display (see _display_address)."""
    addr = entity.get("LegalAddress") or {}
    parts = [_v(addr.get("FirstAddressLine")), _v(addr.get("City")),
             _v(addr.get("PostalCode")), _v(addr.get("Country"))]
    raw = " ".join(p for p in parts if p)
    norm = re.sub(r"[^\w\s]", " ", raw.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm or None


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


def _hq_location(entity: dict) -> tuple[str | None, str | None]:
    """(city, ISO-2 country) from HeadquartersAddress — the entity's real operating
    location, distinct from the LegalAddress (often a registered-agent office, e.g. a
    Delaware C/O). Surfaced at the top of the node like other sources' HQ."""
    hq = entity.get("HeadquartersAddress") or {}
    city = _v(hq.get("City"))
    country = _v(hq.get("Country"))
    return (city or None), (country[:2].upper() if country else None)


def _hq_address(entity: dict) -> str | None:
    """Human-readable full HeadquartersAddress (the operating HQ) — geocoded to the
    map pin. Distinct from the legal `address`, which can be a registered-agent office."""
    hq = entity.get("HeadquartersAddress")
    if not hq:
        return None
    parts = _address_lines(hq) + [_v(hq.get("City")), _v(hq.get("PostalCode")), _v(hq.get("Country"))]
    joined = ", ".join(p.strip() for p in parts if p and p.strip())
    return joined or None


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


def _founded(entity: dict) -> int | None:
    d = _v(entity.get("EntityCreationDate"))
    if d and len(d) >= 4 and d[:4].isdigit():
        return int(d[:4])
    return None


def _founded_date(entity: dict) -> str | None:
    """Full YYYY-MM-DD creation date (EntityCreationDate) for the Details section —
    `_founded` keeps just the year for the headline, consistent across sources."""
    d = _v(entity.get("EntityCreationDate"))
    if d and len(d) >= 10 and d[4] == "-" and d[7] == "-":
        return d[:10]
    return None


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
    props: dict = {
        "name": name,
        "name_normalized": normalize_entity_name(name),
        "search_text": name,
        "name_credibility": credibility_score,
        "country": _country(entity),
        "jurisdiction_code": _jurisdiction_code(entity),
        "registered_address": _registered_address(entity),
        "address": _display_address(entity),
        "legal_form": _legal_form(entity),
        "registration_authority": reg_authority,
        "registration_number": reg_number,
        "founded": _founded(entity),
        "founded_date": _founded_date(entity),
        "lei_id": lei,
        "source_id": source_id,
        # Deep-link to this company's GLEIF record, not the source home page — the
        # node's "reported / verify" link should open the exact LEI.
        "source_url": f"https://search.gleif.org/#/record/{lei}",
        "is_nominee": is_nominee_name(name),
    }
    # Real operating location (top of the node). Only set when GLEIF has an HQ address,
    # so we never clobber an existing (e.g. Wikidata) HQ with a null.
    hq_city, hq_country = _hq_location(entity)
    if hq_city:
        props["hq_city"] = hq_city
    if hq_country:
        props["hq_country"] = hq_country
    hq_addr = _hq_address(entity)
    if hq_addr:
        props["hq_address"] = hq_addr
    # UK company: its GLEIF registration number is the Companies House number, so key it
    # like the PSC import (gb-coh:{number}) → GLEIF and PSC nodes merge on this id.
    if reg_code == _COMPANIES_HOUSE_RA and reg_number:
        props["companies_house_id"] = reg_number
    legal_type = _legal_form_type(_v((entity.get("LegalForm") or {}).get("OtherLegalForm")))
    if legal_type:
        props["type"] = legal_type
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
    counts = {"records": 0, "entities": 0, "skipped": 0}
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
        batch.flush()
        msg = f"{counts['records']:,} records, {counts['entities']:,} entities"
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
