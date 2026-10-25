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

A UK company does not always live under that id. GLEIF keys its node ``lei:…``
and stores the Companies House number in ``companies_house_id``; the external-id
dedup then merges a later ``gb-coh:`` twin INTO the LEI node, or never sees one
(a listed company has no PSC). Addressed by ``gb-coh:`` alone, the row for such a
company was a silent no-op too — 207 of the 238 UK companies on dev, Barclays and
Rolls-Royce among them, had never received their register name, address or codes
(found 2026-10-24, when the SIC codes became the first field nothing else
supplies). So the run starts by reading the numbers held under another id
(``_survivors_by_number``, through the ``companies_house_id`` index, BEFORE a
bulk load drops it) and addresses each row to the ``gb-coh:`` id and to every
such survivor. The register is the record of a UK company, so its name, address
and former names overwrite what GLEIF put there, as they do on a merged node.

Per matched company it sets: ``name`` / ``name_normalized`` / ``search_text``
(name + former names, FULL_TEXT-indexed), ``type`` (from CompanyCategory),
``country`` (``GB``), ``incorporated_on`` (IncorporationDate — the legal entity's
registration, never the headline ``founded`` year, which is the business's origin
and Wikidata's to state), ``address`` + its matching key ``address_normalized``
(and the registered office in parts — ``reg_street`` / ``reg_city`` /
``reg_postcode``; nothing under ``hq_*``, the register does not say where a
company is run),
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
from app.industry import parse_uk_sic, sync_uk_sic
from app.scraper.companies_house_psc import mark_first_year_lower_bounds
from app.scraper.mapper import is_nominee_name, normalize_address, normalize_entity_name

log = logging.getLogger(__name__)

# CompanyCategory substrings that mean "not an ordinary company".
_NONPROFIT_HINTS = ("charitable", "community interest")

# RegAddress.* columns assembled (in order) into the single `address` string.
_ADDRESS_KEYS = (
    "RegAddress.CareOf", "RegAddress.POBox", "RegAddress.AddressLine1",
    "RegAddress.AddressLine2", "RegAddress.PostTown", "RegAddress.County",
    "RegAddress.Country", "RegAddress.PostCode",
)


def _company_type(category: str | None) -> str:
    c = (category or "").lower()
    return "nonprofit" if any(h in c for h in _NONPROFIT_HINTS) else "company"


def _snapshot_date(entry_name: str) -> str | None:
    """'BasicCompanyDataAsOneFile-2026-07-01.csv' → '2026-07-01' (None when unnamed)."""
    import re
    m = re.search(r"(\d{4}-\d{2}-\d{2})", entry_name or "")
    return m.group(1) if m else None


def _uk_date(incorp: str | None) -> str | None:
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
    has to guess which piece was the town. Under the `reg_*` prefix ONLY — the
    registered office is the one location the register states, and it is not
    a headquarters: until 2026-10-18 it was copied into `hq_*` too, which drew
    an agent's or accountant's office as "Headquarters" and overwrote the HQ
    GLEIF or Wikidata had put on the node (PSC checklist finding #7). The
    display string `address` rides along so the panel's "Registered address"
    row has something to show. Only the parts the row has, so a re-import
    never blanks what another source supplied. The parts were dropped until
    2026-10-18; the PostCode column is on every row."""
    street = ", ".join(p for p in ((row.get(k) or "").strip() for k in _STREET_KEYS) if p) or None
    city = (row.get("RegAddress.PostTown") or "").strip() or None
    postcode = (row.get("RegAddress.PostCode") or "").strip() or None
    parts = {"address": _reg_address(row), "reg_street": street, "reg_city": city,
             "reg_postcode": postcode}
    return {k: v for k, v in parts.items() if v}


def _sic_codes(row: dict) -> list[str]:
    """SICCode.SicText_1..4 as the register writes them ("23320 - Manufacture
    of bricks…"), in order — the first is the principal activity. A cell that
    is no code ("None Supplied", blank) is dropped. Kept on the entity as
    `uk_sic_codes`; `industry.sync_uk_sic` turns them into statements."""
    out: list[str] = []
    for i in range(1, 5):
        text = (row.get(f"SICCode.SicText_{i}") or "").strip()
        if parse_uk_sic(text):
            out.append(text)
    return out


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
        e = former_names.entry(n, _uk_date(row.get(f"PreviousName_{i}.CONDATE")))
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


def _survivors_by_number(page: int = 5000) -> dict[str, list[str]]:
    """``{companies_house_id: [entity ids]}`` for every entity that holds a Companies
    House number under an id other than ``gb-coh:{number}`` — the GLEIF node a
    ``gb-coh:`` twin was merged into, or that never had one.

    Walked through the ``companies_house_id`` index as a two-sided range with a
    keyset cursor (the shape `app.db.paging` proved on the full-size database); the
    index is NOTUNIQUE, so the page's last value is re-read in full — a boundary
    shared by two nodes loses neither. ~160k numbers on a full GLEIF load, a few
    seconds; must run before ``_drop_secondary_indexes``."""
    from app.db.arcadedb import run_sql
    from app.db.paging import ID_CEILING
    cols = "id, companies_house_id AS n"
    found: dict[str, set[str]] = {}
    after = ""
    while True:
        rows = run_sql(f"SELECT {cols} FROM Entity WHERE companies_house_id > :lo "
                       "AND companies_house_id < :hi ORDER BY companies_house_id LIMIT :page",
                       {"lo": after, "hi": ID_CEILING, "page": page})
        if not rows:
            break
        last = str(rows[-1]["n"])
        rows = rows + run_sql(f"SELECT {cols} FROM Entity WHERE companies_house_id = :n", {"n": last})
        for r in rows:
            number, node_id = r.get("n"), r.get("id")
            if number and node_id and node_id != f"gb-coh:{number}":
                found.setdefault(str(number), set()).add(node_id)
        after = last
    return {n: sorted(ids) for n, ids in found.items()}


def _targets(number: str, survivors: dict[str, list[str]]) -> list[str]:
    """The ids a register row is written to: the number-keyed node (a no-op when it
    was merged away or never created) and every survivor holding the number."""
    return [f"gb-coh:{number}", *survivors.get(number, ())]


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

    counts = {"rows": 0, "companies": 0, "survivors": 0, "errors": 0}
    matched: set[str] = set()
    # Numbers held under another id (merged into / first seen by GLEIF): read
    # through the companies_house_id index, so before a bulk load drops it.
    survivors = _survivors_by_number()
    log.info("CH BasicData: %s company numbers held under another id", f"{len(survivors):,}")
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
                # The register's incorporation date is the legal entity's start,
                # not the business's: it goes in `incorporated_on` and leaves
                # `founded` (the year of origin, Wikidata's) alone.
                incorporated_on = _uk_date(row.get("IncorporationDate"))
                props = {
                    "name": name,
                    "name_normalized": normalize_entity_name(name),
                    "search_text": search_text,
                    "type": _company_type(row.get("CompanyCategory")),
                    "country": "GB",
                    "incorporated_on": incorporated_on,
                    # the register's address string is set by _reg_address_parts;
                    # this is its matching key for the same-name dedup
                    "address_normalized": normalize_address(_reg_address(row)),
                    # Nothing under hq_*: the register states the registered
                    # office, not where the company is run (_reg_address_parts).
                    "companies_house_id": number,
                    "aliases": aliases,
                    # The register of record for its own companies: its dated
                    # list replaces whatever stood here (a bulk UPDATE cannot
                    # union); the entity merge unions a SEC twin's list in.
                    "former_names": _former_names(row, name),
                    "is_nominee": is_nominee_name(name),
                    "name_credibility": credibility_score,
                    # What the company does, as filed (app.industry)
                    "uk_sic_codes": _sic_codes(row),
                    "last_scraped_at": _now_iso(),
                    # the registered office in parts, both pins (see _reg_address_parts)
                    **_reg_address_parts(row),
                }
                for target in _targets(number, survivors):
                    batch.update(target, props)
                counts["companies"] += 1
                if number in survivors:
                    counts["survivors"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad row mustn't abort
                counts["errors"] += 1
                if counts["errors"] <= 5:
                    log.warning("CH BasicData row error: %s", exc)
            if only_companies is not None and len(matched) >= len(only_companies):
                break                             # all target companies enriched
        batch.flush()
        bar.finish(f"{counts['rows']:,} rows, {counts['companies']:,} companies enriched, "
                   f"{counts['survivors']:,} of them held under another id")
    finally:
        raw.close()
        if bulk_load:
            _rebuild_indexes()

    # Now the incorporation dates are in: a PSC notified in the register's first
    # year on a company that predates it is a lower bound, which the PSC import
    # could not know (companies_house_psc.mark_first_year_lower_bounds). Scoped
    # to the companies this run enriched — under whichever id they live; a full
    # run walks every gb-coh company, then the survivors held under another id.
    scope = None
    if only_companies is not None:
        scope = [t for n in sorted(matched) for t in _targets(n, survivors)]
    counts["first_year_bounds"] = mark_first_year_lower_bounds(company_ids=scope)
    if scope is None and survivors:
        extra = mark_first_year_lower_bounds(
            company_ids=sorted({i for ids in survivors.values() for i in ids}))
        for key in ("companies_before_register", "edges", "claims"):
            counts["first_year_bounds"][key] = counts["first_year_bounds"].get(key, 0) + extra.get(key, 0)
    # The SIC codes just stored become the register's industry statements,
    # dated by the snapshot (app.industry); same scope (a full run reads every
    # entity carrying codes, whatever its id).
    counts["industry"] = sync_uk_sic(entity_ids=scope, snapshot_date=_snapshot_date(entry))

    log.info("CH BasicData import done: %s", counts)
    return counts
