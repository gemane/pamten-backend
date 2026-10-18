"""
Companies House BasicCompanyData importer — fills in the **names, addresses,
incorporation dates and former names** of the UK companies the PSC pipeline
(``companies_house_psc``) created number-keyed (``gb-coh:{number}``) but left
un-named.

BasicCompanyData is the full UK company register (~5.6M rows, monthly one-file
CSV snapshot). We do **not** want 5.6M isolated company nodes, so this is a pure
**enrichment** pass: each CSV row ``UPDATE``s the Entity whose ``id`` is
``gb-coh:{CompanyNumber}`` — companies not already in the graph (no PSC / no other
reference) are silent no-ops. Nothing here creates nodes or edges.

Per matched company it sets: ``name`` / ``name_normalized`` / ``search_text``
(name + former names, FULL_TEXT-indexed), ``type`` (from CompanyCategory),
``country`` (``GB``), ``founded`` (IncorporationDate), ``registered_address`` (and
the registered office in parts — ``reg_street`` / ``reg_city`` / ``reg_postcode``,
mirrored to ``hq_street`` / ``hq_postcode`` since the HQ pin draws it too),
``aliases`` (PreviousName_1..10), ``former_names`` (the same names, each with
its change-of-name date — ``app.former_names``, the timeline's "Renamed from"),
``is_nominee`` (name-detected) and ``name_credibility`` (the register is the
authoritative name source).
"""
import csv
import logging
import zipfile
from typing import IO, Iterator

from app.scraper.bulk_import import (
    _drop_secondary_indexes, _flush_script, _now_iso, _ProgressBar,
    _rebuild_indexes,
)
from app import former_names
from app.scraper.mapper import is_nominee_name, normalize_entity_name

log = logging.getLogger(__name__)

# CompanyCategory substrings that mean "not an ordinary company".
_NONPROFIT_HINTS = ("charitable", "community interest")

# RegAddress.* columns assembled (in order) into a single registered_address.
_ADDRESS_KEYS = (
    "RegAddress.CareOf", "RegAddress.POBox", "RegAddress.AddressLine1",
    "RegAddress.AddressLine2", "RegAddress.PostTown", "RegAddress.County",
    "RegAddress.Country", "RegAddress.PostCode",
)


def _company_type(category: str | None) -> str:
    c = (category or "").lower()
    return "nonprofit" if any(h in c for h in _NONPROFIT_HINTS) else "company"


def _founded(incorp: str | None) -> str | None:
    """IncorporationDate 'DD/MM/YYYY' -> ISO 'YYYY-MM-DD' (None if unparseable)."""
    parts = (incorp or "").strip().split("/")
    if len(parts) == 3 and all(parts):
        d, m, y = parts
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return None


def _reg_address(row: dict) -> str | None:
    parts = [(row.get(k) or "").strip() for k in _ADDRESS_KEYS]
    return ", ".join(p for p in parts if p) or None


#: The street of the registered office: care-of, PO box and the two lines, in
#: the register's order (like GLEIF's address lines — only the order says
#: which is the building and which the street).
_STREET_KEYS = ("RegAddress.CareOf", "RegAddress.POBox", "RegAddress.AddressLine1",
                "RegAddress.AddressLine2")


def _reg_address_parts(row: dict) -> dict:
    """The registered office in PARTS, for a structured geocode: the same facts
    `_reg_address` joins into one string, kept separate so the geocoder never
    has to guess which piece was the town. Written under BOTH prefixes — the
    registered office is the only location the register knows, and it is what
    the HQ pin draws for a UK company (`hq_address` is the same string). Only
    the parts the row has, so a re-import never blanks what another source
    supplied. Dropped until 2026-10-18; the PostCode column is on every row."""
    street = ", ".join(p for p in ((row.get(k) or "").strip() for k in _STREET_KEYS) if p) or None
    city = (row.get("RegAddress.PostTown") or "").strip() or None
    postcode = (row.get("RegAddress.PostCode") or "").strip() or None
    parts = {"reg_street": street, "reg_city": city, "reg_postcode": postcode,
             "hq_street": street, "hq_postcode": postcode}
    return {k: v for k, v in parts.items() if v}


def _prev_names(row: dict, current: str) -> list[str]:
    """PreviousName_1..10.CompanyName as aliases — deduped, current name dropped."""
    out: list[str] = []
    cur = current.lower()
    seen = {cur}
    for i in range(1, 11):
        n = (row.get(f"PreviousName_{i}.CompanyName") or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def _former_names(row: dict, current: str) -> list[str]:
    """PreviousName_1..10 with their CONDATE (the day the name changed, so the
    last day the old name applied) as ``former_names`` entries. The register
    lists them newest first; kept so. A previous name equal to the current one
    (a company renamed back) is dropped, like in ``_prev_names``."""
    out: list[str] = []
    cur = current.lower()
    for i in range(1, 11):
        n = (row.get(f"PreviousName_{i}.CompanyName") or "").strip()
        if not n or n.lower() == cur:
            continue
        e = former_names.entry(n, _founded(row.get(f"PreviousName_{i}.CONDATE")))
        if e and e not in out:
            out.append(e)
    return out


def _text_lines(raw: IO[bytes], total: int, bar: _ProgressBar) -> Iterator[str]:
    """Yield decoded CSV lines from the (uncompressed) stream, driving the byte
    progress bar. Lines keep their newline so csv.reader can reassemble quoted
    fields that span lines."""
    read = 0
    buf = b""
    while True:
        chunk = raw.read(1 << 20)
        if not chunk:
            if buf:
                yield buf.decode("utf-8", "replace")
            return
        read += len(chunk)
        bar.render(read, total)
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", "replace") + "\n"


def _prefiltered_lines(lines: Iterator[str], targets: set[str]) -> Iterator[str]:
    """For a curated subset: yield the header, then only lines that contain a target
    company number. A cheap substring reject so csv.reader parses just the handful of
    matching rows (the exact CompanyNumber is confirmed after parsing) instead of all
    ~5.7M — the same idea as the PSC importer's prefilter."""
    it = iter(lines)
    try:
        yield next(it)                     # header — always
    except StopIteration:
        return
    for line in it:
        if any(t in line for t in targets):
            yield line


class _UpdateBatch:
    """Buffers UPDATE-only (no UPSERT) statements keyed on id, flushed via a single
    ``sqlscript`` per batch. Rows whose id isn't already in the graph are silent
    no-ops — this importer only *enriches* existing companies, never creates them."""

    def __init__(self, batch_size: int = 400):
        self._batch = batch_size
        self._buf: list[tuple[str, dict]] = []

    def update(self, node_id: str, props: dict) -> None:
        self._buf.append((node_id, props))
        if len(self._buf) >= self._batch:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        stmts, params = [], {}
        for k, (node_id, props) in enumerate(self._buf):
            sets = []
            for name, val in props.items():
                pk = f"{name}__{k}"
                params[pk] = val
                sets.append(f"{name} = :{pk}")
            params[f"id__{k}"] = node_id
            stmts.append(f"UPDATE Entity SET {', '.join(sets)} WHERE id = :id__{k};")
        _flush_script("\n".join(stmts), params)
        self._buf.clear()


def import_basic_company_data(filepath: str, credibility_score: int,
                              limit: int | None = None, bulk_load: bool = False,
                              batch_size: int = 400,
                              only_companies: set[str] | None = None) -> dict:
    """Enrich existing ``gb-coh:{number}`` companies from a BasicCompanyData CSV
    snapshot (.zip). Returns counts. ``batch_size`` = rows per flush (smaller stays
    under a short proxy timeout; larger cuts round-trips on a direct connection).

    ``only_companies`` restricts enrichment to that set of company numbers (the curated
    test subset), stopping once all are found — the companion to ch-psc's ``--only``."""
    zf = zipfile.ZipFile(filepath)
    entry = zf.namelist()[0]
    total_bytes = zf.getinfo(entry).file_size
    log.info("CH BasicData: reading %s (%s bytes)", entry, f"{total_bytes:,}")
    raw: IO[bytes] = zf.open(entry)

    counts = {"rows": 0, "companies": 0, "errors": 0}
    matched: set[str] = set()
    if bulk_load:
        _drop_secondary_indexes()
    batch = _UpdateBatch(batch_size=batch_size)
    bar = _ProgressBar("CH BasicData")
    try:
        lines = _text_lines(raw, total_bytes, bar)
        if only_companies is not None:
            lines = _prefiltered_lines(lines, only_companies)
        reader = csv.reader(lines)
        header = [h.strip() for h in next(reader)]
        for values in reader:
            if limit and counts["rows"] >= limit:
                break
            counts["rows"] += 1
            try:
                row = dict(zip(header, values))
                number = (row.get("CompanyNumber") or "").strip()
                name = (row.get("CompanyName") or "").strip()
                if not number or not name:
                    counts["errors"] += 1
                    continue
                if only_companies is not None:
                    if number not in only_companies:
                        continue                  # curated test subset
                    matched.add(number)
                aliases = _prev_names(row, name)
                search_text = name if not aliases else name + " " + " ".join(aliases)
                # `founded` is the year (consistent with GLEIF); the full incorporation
                # date goes in `founded_date` for the Details section.
                founded_date = _founded(row.get("IncorporationDate"))
                founded_year = int(founded_date[:4]) if founded_date else None
                batch.update(f"gb-coh:{number}", {
                    "name": name,
                    "name_normalized": normalize_entity_name(name),
                    "search_text": search_text,
                    "type": _company_type(row.get("CompanyCategory")),
                    "country": "GB",
                    # Registered-office town → hq so the geocoder can place it on the map
                    # (a UK Companies House company is GB-registered).
                    "hq_city": (row.get("RegAddress.PostTown") or "").strip() or None,
                    "hq_country": "GB",
                    "founded": founded_year,
                    "founded_date": founded_date,
                    "registered_address": _reg_address(row),
                    # PSC/UK companies have no separate HQ — the registered office is
                    # the location the map geocodes.
                    "hq_address": _reg_address(row),
                    "companies_house_id": number,
                    "aliases": aliases,
                    # The register of record for its own companies: its dated
                    # list replaces whatever stood here (a bulk UPDATE cannot
                    # union); the entity merge unions a SEC twin's list in.
                    "former_names": _former_names(row, name),
                    "is_nominee": is_nominee_name(name),
                    "name_credibility": credibility_score,
                    "last_scraped_at": _now_iso(),
                    # the registered office in parts, both pins (see _reg_address_parts)
                    **_reg_address_parts(row),
                })
                counts["companies"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad row mustn't abort
                counts["errors"] += 1
                if counts["errors"] <= 5:
                    log.warning("CH BasicData row error: %s", exc)
            if only_companies is not None and len(matched) >= len(only_companies):
                break                             # all target companies enriched
        batch.flush()
        bar.finish(f"{counts['rows']:,} rows, {counts['companies']:,} companies enriched")
    finally:
        raw.close()
        if bulk_load:
            _rebuild_indexes()

    log.info("CH BasicData import done: %s", counts)
    return counts
