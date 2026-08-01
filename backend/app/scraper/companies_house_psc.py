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
from typing import IO

from app.scraper.bulk_import import (
    _BatchWriter, _drop_secondary_indexes, _entity, _max_pct, _now_iso,
    _ProgressBar, _rebuild_indexes,
)
from app.scraper.mapper import derive_ownership_type, parse_full_name

log = logging.getLogger(__name__)

_PERSON_KINDS = ("individual-person-with-significant-control", "individual-beneficial-owner")
_ENTITY_KINDS = ("corporate-entity-person-with-significant-control",
                 "legal-person-person-with-significant-control")
_BAND = re.compile(r"(\d+)-to-\d+-percent")


def _band_floor(nature: str) -> int | None:
    m = _BAND.search(nature)
    return int(m.group(1)) if m else None


def _control(natures: list[str] | None) -> tuple:
    """(stake_percent, voting_power_pct, ownership_type, interest_types) from a
    PSC's natures_of_control — economic vs voting kept separate."""
    natures = natures or []
    stake = voting = None
    controlling = False
    for nat in natures:
        if "ownership-of-shares" in nat:
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
    return stake, voting, otype, sorted(set(natures))


def _psc_name(data: dict) -> str | None:
    if name := (data.get("name") or "").strip():
        return name
    ne = data.get("name_elements") or {}
    parts = [ne.get("forename"), ne.get("middle_name"), ne.get("surname")]
    return " ".join(p for p in parts if p).strip() or None


def _birth_date(data: dict) -> str:
    dob = data.get("date_of_birth") or {}
    y, m = dob.get("year"), dob.get("month")
    return f"{y:04d}-{m:02d}" if y and m else (f"{y:04d}" if y else "")


def _entity_psc_id(data: dict) -> tuple[str, str | None]:
    """(node_id, companies_house_id) for a corporate/legal PSC — keyed on its own
    UK company number when it has one, else on the PSC self-link."""
    ident = data.get("identification") or {}
    reg = (ident.get("registration_number") or "").strip()
    country = (ident.get("country_registered") or "").lower()
    if reg and ("england" in country or "wales" in country or "scotland" in country
                or "united kingdom" in country or country in ("uk", "gb")):
        return f"gb-coh:{reg}", reg
    self_link = ((data.get("links") or {}).get("self") or "").strip()
    return f"chpsc:{self_link}", None


def _iso2_country(name: str | None) -> str | None:
    """A PSC address ``country`` name → ISO-2 code. Companies House uses UK
    subdivisions ('England', 'England & Wales', 'Scotland', …) which aren't ISO
    countries — all map to GB; other names go through the shared name→code table."""
    if not name:
        return None
    n = name.strip().lower()
    if n in ("uk", "gb") or any(uk in n for uk in (
            "england", "wales", "scotland", "northern ireland",
            "united kingdom", "great britain")):
        return "GB"
    from app.scraper.bulk_import import _ISO2_COUNTRY
    return {v.lower(): k for k, v in _ISO2_COUNTRY.items()}.get(n) or (
        name.strip().upper() if len(name.strip()) == 2 else None)


def _psc_address(addr: dict | None) -> tuple[str | None, str | None, str | None]:
    """(display registered_address, hq_city, hq_country ISO-2) from a PSC record's
    correspondence ``address`` dict — the corporate PSC's own service address."""
    addr = addr or {}
    parts = [addr.get("premises"), addr.get("address_line_1"), addr.get("address_line_2"),
             addr.get("locality"), addr.get("region"), addr.get("postal_code"),
             addr.get("country")]
    display = ", ".join(p.strip() for p in parts if p and str(p).strip()) or None
    city = (addr.get("locality") or "").strip() or None
    return display, city, _iso2_country(addr.get("country"))


def _process(rec: dict, batch: "_BatchWriter", source_id: str, credibility_score: int) -> str | None:
    """Write one PSC record's nodes + OWNS edge. Returns 'person' | 'entity' | None."""
    company_number = (rec.get("company_number") or "").strip()
    data = rec.get("data") or {}
    kind = data.get("kind")
    if not company_number or kind not in _PERSON_KINDS + _ENTITY_KINDS:
        return None
    name = _psc_name(data)
    if not name:
        return None

    owned_id = f"gb-coh:{company_number}"
    # Ensure the controlled company exists (named later from BasicCompanyData).
    batch.entity(owned_id, {"companies_house_id": company_number, "source_id": source_id})

    stake, voting, otype, interest_types = _control(data.get("natures_of_control"))
    since = data.get("notified_on") or None
    until = data.get("ceased_on") or None

    if kind in _PERSON_KINDS:
        self_link = ((data.get("links") or {}).get("self") or "").strip()
        first, last = parse_full_name(name)
        batch.person(f"chpsc:{self_link}", {
            "first_name": first, "last_name": last, "full_name": name,
            "search_text": name, "nationality": data.get("nationality") or "",
            "birth_date": _birth_date(data), "description": "",
            "verified": False, "alias": [], "nationalities": [],
        })
        owner_id, owner_label = f"chpsc:{self_link}", "Person"
        kind_cat = "person"
    else:
        owner_id, chid = _entity_psc_id(data)
        # The corporate PSC's own correspondence address (in the record) → its address
        # + map location; otherwise it's a name-only node.
        reg_addr, hq_city, hq_country = _psc_address(data.get("address"))
        _entity(batch, owner_id, name=name, entity_type="company",
                country=((data.get("identification") or {}).get("country_registered") or None),
                founded=None, lei_id=None, companies_house_id=chid,
                source_id=source_id, credibility_score=credibility_score,
                registered_address=reg_addr, hq_city=hq_city, hq_country=hq_country)
        owner_label = "Entity"
        kind_cat = "entity"

    batch.owns(owner_id, owner_label, owned_id, {
        "stake_percent": stake, "voting_power_pct": voting, "ownership_type": otype,
        "interest_types": interest_types, "direct_or_indirect": None,
        "since": since, "until": until, "source_id": source_id,
        "credibility_score": credibility_score,
        "source_url": f"https://find-and-update.company-information.service.gov.uk/company/{company_number}/persons-with-significant-control",
        "source_date": since, "last_scraped_at": _now_iso(),
    })
    return kind_cat


def import_ch_psc(filepath: str, source_id: str, credibility_score: int,
                  limit: int | None = None, bulk_load: bool = False,
                  batch_size: int = 400,
                  only_companies: set[str] | None = None) -> dict:
    """Import a Companies House PSC snapshot (.zip/.txt). Returns counts.

    ``batch_size`` sets how many records flush per ``sqlscript`` round-trip. Behind
    a proxy with a short read timeout (e.g. dev-db's 60s nginx), a smaller batch
    keeps each flush well under the limit so heavy flushes don't 504-then-retry
    (the dominant cost of a slow import); connected directly to ArcadeDB, a larger
    batch cuts round-trips.

    ``only_companies`` restricts the import to that set of company numbers (the curated
    test subset). A cheap raw-bytes prefilter skips JSON parsing for non-matches, and —
    since a CH snapshot groups a company's PSC records together — reading stops once all
    target companies have been seen. So a handful of companies loads in seconds."""
    if filepath.lower().endswith(".zip"):
        zf = zipfile.ZipFile(filepath)
        entry = zf.namelist()[0]
        total_bytes = zf.getinfo(entry).file_size
        raw: IO[bytes] = zf.open(entry)          # ZipExtFile — line-iterable
        log.info("CH PSC: reading %s (%s bytes)", entry, f"{total_bytes:,}")
    else:
        total_bytes = os.path.getsize(filepath)
        raw = open(filepath, "rb")  # noqa: WPS515

    counts = {"records": 0, "persons": 0, "entities": 0, "skipped": 0, "errors": 0}
    only_bytes = [c.encode() for c in only_companies] if only_companies else None
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
            if only_bytes is not None and not any(cb in line for cb in only_bytes):
                continue
            counts["records"] += 1
            if counts["records"] % 50000 == 0:
                bar.render(done, total_bytes)
            try:
                rec = json.loads(line)
                if only_companies is not None:
                    cn = (rec.get("company_number") or "").strip()
                    if cn not in only_companies:
                        continue                  # substring hit in another field
                    matched.add(cn)
                cat = _process(rec, batch, source_id, credibility_score)
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
            if only_companies is not None and len(matched) >= len(only_companies):
                break                             # all target companies loaded
        batch.flush()
        bar.finish(f"{counts['records']:,} records, "
                   f"{counts['persons']:,} persons + {counts['entities']:,} entities")
    finally:
        raw.close()
        if bulk_load:
            _rebuild_indexes()

    log.info("CH PSC import done: %s", counts)
    return counts
