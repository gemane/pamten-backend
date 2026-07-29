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
import logging
import os
import re
import zipfile
from typing import IO

from app.scraper.bulk_import import (
    _BatchWriter, _drop_secondary_indexes, _legal_form_type, _ProgressBar,
    _ProgressStream, _rebuild_indexes,
)
from app.scraper.gleif_succession import _iter_lei_records, _legal_name, _v
from app.scraper.mapper import is_nominee_name, normalize_entity_name

log = logging.getLogger(__name__)


def _country(entity: dict) -> str | None:
    """ISO-2 country from LegalJurisdiction (e.g. 'US', or 'US-DE' → 'US'),
    falling back to the headquarters country."""
    j = _v(entity.get("LegalJurisdiction")) or _v((entity.get("HeadquartersAddress") or {}).get("Country"))
    return j.split("-")[0][:2].upper() if j else None


def _registered_address(entity: dict) -> str | None:
    """Normalized LegalAddress (line + city + postcode + country), lowercased."""
    addr = entity.get("LegalAddress") or {}
    parts = [_v(addr.get("FirstAddressLine")), _v(addr.get("City")),
             _v(addr.get("PostalCode")), _v(addr.get("Country"))]
    raw = " ".join(p for p in parts if p)
    norm = re.sub(r"[^\w\s]", " ", raw.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm or None


def _founded(entity: dict) -> int | None:
    d = _v(entity.get("EntityCreationDate"))
    if d and len(d) >= 4 and d[:4].isdigit():
        return int(d[:4])
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
    props: dict = {
        "name": name,
        "name_normalized": normalize_entity_name(name),
        "search_text": name,
        "name_credibility": credibility_score,
        "country": _country(entity),
        "registered_address": _registered_address(entity),
        "founded": _founded(entity),
        "lei_id": lei,
        "source_id": source_id,
        "is_nominee": is_nominee_name(name),
    }
    legal_type = _legal_form_type(_v((entity.get("LegalForm") or {}).get("OtherLegalForm")))
    if legal_type:
        props["type"] = legal_type
    return f"lei:{lei}", props


def import_lei_cdf_entities(
    filepath: str,
    source_id: str,
    credibility_score: int,
    limit: int | None = None,
    filter_jurisdiction: str | None = None,
    bulk_load: bool = False,
) -> dict:
    """
    Import GLEIF entities from a local LEI-CDF golden-copy .json/.zip.

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
        bar = _ProgressBar("LEI-CDF entities")
        stream = _ProgressStream(raw, total_bytes, bar)
        for rec in _iter_lei_records(stream):
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
        bar.finish(f"{counts['records']:,} records, {counts['entities']:,} entities")
    finally:
        raw.close()
        if bulk_load:
            _rebuild_indexes()

    log.info("LEI-CDF entity import done: %s", counts)
    return counts
