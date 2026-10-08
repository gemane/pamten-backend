"""SEC Exhibit 21 — the statutory subsidiary list.

Every US public company's 10-K carries Exhibit 21 ("Subsidiaries of the
registrant"), a name + jurisdiction table; foreign private issuers file the
same as Exhibit 8.1 of the 20-F. It is the statutory answer to "who does this
company own" — the role Wikidata's community-edited subsidiary lists used to
play before they were demoted to claims-only.

Two honesty caveats carried onto every edge:
- Filers list only *significant* subsidiaries (Reg S-K Item 601(b)(21) lets
  them omit the rest); the absence of a subsidiary here proves nothing.
- The exhibit states existence and jurisdiction, never a stake — edges carry
  ownership_type "subsidiary" semantics with no invented percentage.

Parsing: the exhibit is free-form HTML, but in practice a two-column table
(name | jurisdiction) or its visual equivalent. The parser reads table rows
first; when they yield nothing it tries the other layouts filers use — names
under country rows (measured on the 2026 20-Fs). A list that names no place
for its subsidiaries, one subsidiary per line with its place in words, and
the text layer behind scanned pages, are not read. A document declared anything
but EX-8/EX-8.1 (in a 20-F) or EX-21.x is no list. Jurisdictions
like "Delaware, U.S." are split into country US (the state is kept as display
text). Measured, not documented: Apple/Microsoft/Alphabet exhibits are all
two-column tables.
"""
from __future__ import annotations

import html as _html
import logging
import re
import unicodedata
from html.parser import HTMLParser

from app.scraper.mapper import normalize_entity_name
from app.scraper.sec_edgar import SUBMISSIONS_URL, _cik10, _get, _get_text, _iso_date

log = logging.getLogger(__name__)

# 10-K first (domestic, Ex-21), then 20-F (foreign private issuers, Ex-8.1 —
# same content, different exhibit number in the rulebook).
_ANNUAL_FORMS = ("10-K", "20-F")


#: How far back the subsidiary history reads: one annual filing per year, two
#: requests each (the filing index and the exhibit; exhibits are Archives files
#: and cached forever). Electronic exhibits in HTML start around 2001.
HISTORY_MAX_FILINGS = 25
#: Older submission pages opened for filings beyond the inline "recent" list.
HISTORY_MAX_OLDER_PAGES = 3

# Filenames as filing agents write them (measured 2026-10-07): "ex21", "ex-21.1",
# Workiva's "meli-20251231xexx2101" (exx = exhibit, 2101 = 21.01), and for the
# 20-F "ex8_1", "dex81", Embraer's bare "xex8", and "exhibit_8-1" / "exhibit81"
# (38 of the 1,012 20-Fs of 2026). The 8 may not run on into
# another digit ("ex85" is not 8.1).
_EX21_NAMES = (re.compile(r"ex+[-._]?21", re.I), re.compile(r"exhibit[-._]?21", re.I),
               re.compile(r"subsidiar", re.I))
_EX8_NAMES = (re.compile(r"ex+[-._]?0?8(?:[-._]?0?1)?(?!\d)", re.I),
              re.compile(r"exhibit[-._]?0?8(?:[-._]?0?1)?(?!\d)", re.I),   # exhibit_8-1, exhibit81fy2026
              re.compile(r"dex8", re.I), re.compile(r"subsidiar", re.I))


def annual_filings(cik: str, include_older: bool = False) -> list[tuple[str, str, str, str]]:
    """(form, accession, filing date, report date) of the 10-K/20-F filings,
    newest first. The report date is the fiscal year-end the filing covers —
    what its subsidiary list is "as of" ("" when EDGAR does not give one).

    The inline "recent" list only, unless ``include_older`` — then also up to
    ``HISTORY_MAX_OLDER_PAGES`` of the older pages EDGAR splits long histories
    into. A 404 on the submissions API (a stale CIK on an old filer) is "no
    filings", not an error."""
    try:
        subs = _get(f"https://data.sec.gov/submissions/CIK{_cik10(cik)}.json")
    except Exception as exc:  # noqa: BLE001 - stale CIKs 404; treat as absent
        log.info("submissions unavailable for CIK %s: %s", cik, exc)
        return []
    pages = [(subs.get("filings") or {}).get("recent") or {}]
    if include_older:
        for f in ((subs.get("filings") or {}).get("files") or [])[:HISTORY_MAX_OLDER_PAGES]:
            try:
                pages.append(_get(f"{SUBMISSIONS_URL}/{f['name']}"))
            except Exception as exc:  # noqa: BLE001 - history is best-effort
                log.warning("older submissions page %s failed: %s", f.get("name"), exc)
                break
    out: list[tuple[str, str, str, str]] = []
    for page in pages:
        forms = page.get("form") or []
        periods = page.get("reportDate") or [""] * len(forms)
        for form, accession, filed, period in zip(forms, page.get("accessionNumber") or [],
                                                  page.get("filingDate") or [], periods):
            if form in _ANNUAL_FORMS:
                out.append((form, accession, filed, period or ""))
    out.sort(key=lambda r: r[2], reverse=True)
    return out


def exhibit_candidates(cik: str, form: str, accession: str, filed: str,
                       period: str = "") -> list[dict]:
    """Candidate subsidiary-exhibit files of ONE annual filing.

    A list, not one file: exhibit numbering is ambiguous by filename alone —
    AB InBev's `dex215.htm` is exhibit 2.15 (securities descriptions), not
    21.5, and only parsing tells them apart. For a 20-F the ex-8 patterns
    are tried first (that is where its subsidiary list lives), for a 10-K
    the ex-21 ones."""
    acc = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}"
    index = _get(f"{base}/index.json")
    names = [it.get("name") or "" for it in (index.get("directory") or {}).get("item") or []]
    patterns = _EX8_NAMES + _EX21_NAMES if form == "20-F" else _EX21_NAMES + _EX8_NAMES
    out, seen = [], set()
    for pat in patterns:
        for name in names:
            if name in seen or not name.lower().endswith((".htm", ".html")):
                continue
            if pat.search(name):
                seen.add(name)
                out.append({"url": f"{base}/{name}", "form": form,
                            "filing_date": _iso_date(filed), "accession": accession,
                            "period": _iso_date(period) if period else None})
    return out


def annual_exhibit_candidates(cik: str) -> list[dict]:
    """Candidate subsidiary-exhibit files from the NEWEST 10-K/20-F — the
    newest annual filing decides; this does not walk back to older years
    (``fetch_subsidiary_history`` does)."""
    filings = annual_filings(cik)
    return exhibit_candidates(cik, *filings[0]) if filings else []


class _Row(list):
    """One table row's cell texts, plus what its LAYOUT said: how far its first
    text was indented, and where each cell sits on the table's column grid.
    Filers draw the group tree with indentation (Chubb, Eversource, NYT), and
    a plain list of strings threw that away. The grid is what `colspan` says:
    TORM's header "Jurisdiction of Incorporation" spans grid columns 3–8 and is
    the second cell of its row, while each subsidiary's "Denmark" is the third
    cell of its row, at column 6 — by cell index the two never met."""
    __slots__ = ("indent", "spans")

    def __init__(self, cells=(), indent: float = 0.0, spans=None):
        super().__init__(cells)
        self.indent = indent
        self.spans = spans          # [(first grid column, past the last)] per cell


# CSS lengths a filer uses to push a name to the right. Points; other units
# converted approximately — only the ORDER of indents matters, never the value.
_CSS_INDENT = re.compile(
    r"(padding-left|margin-left|text-indent)\s*:\s*(-?[\d.]+)\s*(pt|px|in|em|%)?", re.I)
_PT_PER_UNIT = {"pt": 1.0, "px": 0.75, "in": 72.0, "em": 12.0, "%": 5.0}
#: one leading non-breaking space ≈ this many points of indent
_NBSP_PT = 3.0


def _css_indent(attrs) -> float:
    style = dict(attrs).get("style") or ""
    return sum(float(v) * _PT_PER_UNIT.get((u or "pt").lower(), 1.0)
               for _, v, u in _CSS_INDENT.findall(style) if float(v) > 0)


class _TableTextParser(HTMLParser):
    """Tables of rows of cell texts, in document order with the free text
    between them. Per-TABLE grouping matters: an exhibit can hold several
    tables (cover blocks, direct + indirect sections, one per printed page),
    and each carries its own header row naming its columns — or none, when
    the page break fell inside one list (Chubb: eleven tables, one header).

    ``sequence`` interleaves ("table", rows) and ("text", str): the headings
    BETWEEN tables are where Tenet says "Consolidated Subsidiaries of USPI
    Holding Company, Inc." — a parent the rows below never name themselves.
    Each row records the indentation of its first text (CSS on the cell or
    on anything inside it before the text, plus leading non-breaking spaces),
    which is how a filer draws the tree."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[_Row]] = []
        self.sequence: list[tuple[str, object]] = []
        self._table: list[_Row] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_indent = 0.0
        self._row_indent: float | None = None      # of the first non-empty cell
        self._free: list[str] = []                  # text outside any table
        self._spans: list[tuple[int, int]] = []     # grid columns of the row's cells
        self._colspan = 1

    @property
    def rows(self) -> list[list[str]]:   # flattened view (tests, debugging)
        return [r for t in self.tables for r in t]

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._flush_free()
            self._table = []
        elif tag == "tr":
            self._row = []
            self._row_indent = None
            self._spans = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._cell_indent = _css_indent(attrs)
            span = re.match(r"\s*(\d+)", str(dict(attrs).get("colspan") or "1"))
            self._colspan = min(max(int(span.group(1)) if span else 1, 1), 50)
        elif self._cell is not None and not "".join(self._cell).strip():
            # a block or span wrapping the text can carry the indent instead
            self._cell_indent += _css_indent(attrs)
        elif self._table is None and tag in ("p", "div", "br", "hr"):
            self._flush_free()

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            raw = "".join(self._cell)
            # Zero-width characters first: Texas Roadhouse's exhibit has a
            # whole filler column of U+200B, which is truthy and was taken as
            # the jurisdiction of all 62 subsidiaries.
            raw = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", raw)
            text = re.sub(r"\s+", " ", raw).strip()
            if text and self._row_indent is None:
                lead = len(raw) - len(raw.lstrip())
                self._row_indent = self._cell_indent + lead * _NBSP_PT
            self._row.append(text)
            start = self._spans[-1][1] if self._spans else 0
            self._spans.append((start, start + self._colspan))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                row = _Row(self._row, self._row_indent or 0.0, self._spans)
                (self._table if self._table is not None else self._orphan()).append(row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
                self.sequence.append(("table", self._table))
            self._table = None
        elif self._table is None and tag in ("p", "div", "li", "h1", "h2", "h3", "h4"):
            self._flush_free()

    def _orphan(self) -> list:
        # rows outside any <table> (malformed HTML, or an excerpt that starts
        # inside one — a note cut from a 20-F) — collect as one table. The
        # list used to be created only when a table had come before, so rows
        # before the first table raised KeyError.
        orphans = self.__dict__.setdefault("_orphans", [])
        if not self.tables or self.tables[-1] is not orphans:
            self.tables.append(orphans)
            self.sequence.append(("table", orphans))
        return orphans

    def _flush_free(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._free)).strip()
        self._free = []
        if text:
            self.sequence.append(("text", text))

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif self._table is None and self._row is None:
            self._free.append(data)

    def close(self):
        super().close()
        self._flush_free()


# Header detection: which column is which, by what the filer CALLS it.
_H_NAME = re.compile(r"subsidiar|name|entity|compan", re.I)
# "Place of incorp" (Reitar) — the word cut short; "domicile" (Brazilian filers)
_H_JURISDICTION = re.compile(r"jurisdiction|incorp|organi[sz]|country|state|domicil", re.I)
_H_OWNERSHIP = re.compile(r"ownership|percent|%|owned|interest", re.I)
# "Location"/"Address" is where an office SITS, not where the company is
# registered — Bank of America has both columns, and taking Location wrote
# "San Francisco, CA" as a jurisdiction. A cell that says both ("Location
# Jurisdiction of Organization", Mytheresa's one column) is the jurisdiction.
_H_NOT_JURISDICTION = re.compile(r"location|address|city", re.I)


def _not_jurisdiction(cell: str) -> bool:
    return bool(_H_NOT_JURISDICTION.search(cell)) and not re.search(r"jurisdiction", cell, re.I)
# "Date of Incorporation" names a DATE column ("November 9, 2000"), though it
# says "incorporat": Rezolve, Sentage and CCSC wrote their dates as places.
_H_DATE = re.compile(r"\s*date\b", re.I)
# A column naming each row's DIRECT holder — Almacenes Éxito's "Direct
# controlling entity", beside an ownership column that is that holder's stake.
# The whole cell, so "Name of parent and subsidiary" is not one; checked before
# the ownership words, so "Owned by" holds names, not percentages.
_H_PARENT = re.compile(r"^(?:(?:direct|immediate)\s+)?(?:controlling\s+entity|parent(?:\s+(?:company|entity))?|"
                       r"holding\s+company|shareholder)$|^(?:directly\s+)?(?:controlled|held|owned)\s+by$", re.I)

_PERCENT = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\s*%\s*$")
# Under a column the filer CALLS ownership, a bare "100" is a percentage too
# (Lincoln National writes the column without the sign).
_BARE_PERCENT = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\s*%?\s*$")
# Chubb's cell for a jointly held company: "66.66% 33.33% (Chubb Bermuda
# Insurance Ltd.)" — the first figure is the listed parent's share, each
# further "share (holder)" pair a co-holder. Also "99.99%0.01% (…)", no space.
_CO_OWNER = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*\(([^()]+)\)")
_FIRST_PERCENT = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\s*%")

# "Subsidiaries of USPI Holding Company, Inc." — a heading (or a header cell)
# that names the parent of the rows under it. "…of the Registrant/Company"
# names nobody in particular and resets to the filer.
_PARENT_HEADING = re.compile(
    r"^(?:consolidated |direct |indirect |wholly[- ]owned |significant )*"
    r"subsidiar(?:y|ies) of (.+?)\s*:?$", re.I)
_GENERIC_PARENT = re.compile(r"^(the )?(registrants?|compan(y|ies)|parent|issuer)\b", re.I)


def _find_header(table: list[list[str]]) -> dict | None:
    """Column indexes from a header row in the table's first few rows.

    A header that names the jurisdiction (and maybe the ownership) column but
    not the name column still counts — Eversource's header is just "State of
    Incorporation", Lincoln's "Organized Under Law of: | Ownership" — with the
    name column inferred as the first column left of it that the rows fill.
    A cell that IS a place ("United States of America" contains "state") is a
    subsidiary's row, never the header above it.
    """
    for row in table[:5]:
        name_i = jur_i = own_i = par_i = None
        for i, cell in enumerate(row):
            if not cell:
                continue
            if jur_i is None and _H_JURISDICTION.search(cell) \
                    and not _not_jurisdiction(cell) \
                    and not _H_DATE.match(cell) \
                    and jurisdiction_country(cell) is None:
                jur_i = i
            elif par_i is None and _H_PARENT.match(cell.strip()):
                par_i = i
            elif own_i is None and _H_OWNERSHIP.search(cell):
                own_i = i
            elif name_i is None and _H_NAME.search(cell):
                name_i = i
        if jur_i is not None and name_i is None:
            below = [r for r in table if r is not row]
            for i in range(jur_i):
                if sum(1 for r in below if i < len(r) and r[i]) >= max(1, len(below) // 2):
                    name_i = i
                    break
        if name_i is not None and jur_i is not None:
            return {"name": name_i, "jurisdiction": jur_i, "ownership": own_i, "parent": par_i,
                    "header_row": row, "spans": getattr(row, "spans", None)}
    return None


#: A cell that only numbers or bullets a row: "1.", "1.001", "·", "II."
_MARKER_CELL = re.compile(r"^(?:[\u00b7\u2022\u25aa\u25cf\u25cb\u25e6\-\u2013]|\d{1,3}(?:\.\d{1,4})*[.)]?|"
                          r"\(?[ivxlcIVXLC]{1,5}[.)])$")


def _column(row: list[str], header: dict, key: str) -> str:
    """The row's text under the header's column ``key``.

    By grid position when both rows carry one: the first non-empty cell that
    starts under the header cell's span — TORM's and Ellomay's header cells
    span several narrower data cells — else one that overlaps it and no other
    labelled column (BGM's place cell starts under the spacer before it). Not
    a cell spanning several labelled columns: AIFU's section label
    "Insurance Agencies and Brokers" spans the whole row and would be name
    and place at once. A name is never a bare row number ("1.", AIFU's first
    cell). Failing that, and without a grid, by cell index, as always."""
    i = header.get(key)
    if i is None:
        return ""
    by_index = row[i] if i < len(row) else ""
    spans, own = header.get("spans"), getattr(row, "spans", None)
    if not spans or not own or i >= len(spans):
        return by_index
    lo, hi = spans[i]
    usable = [(text, s, e) for text, (s, e) in zip(row, own)
              if text and not (key == "name" and _MARKER_CELL.match(text))]
    if hit := next((text for text, s, _e in usable if lo <= s < hi), None):
        return hit
    labelled = [spans[j] for j in (header.get("name"), header.get("jurisdiction"), header.get("ownership"),
                                   header.get("parent"))
                if j is not None and j < len(spans)]
    for text, s, e in usable:
        if s < hi and e > lo and sum(1 for a, b in labelled if s < b and e > a) == 1:
            return text
    return by_index


#: Points of indent one empty grid column before the name stands for.
_GRID_INDENT = 12.0


def _number_columns(table: list, header: dict) -> set[int]:
    """Grid columns inside the name column's span that hold a row number in
    some row: a row without one leaves that cell empty, which is no indent."""
    i, spans = header.get("name"), header.get("spans")
    if i is None or not spans or i >= len(spans):
        return set()
    lo, hi = spans[i]
    return {s for row in table for text, (s, _e) in zip(row, getattr(row, "spans", None) or [])
            if lo <= s < hi and text and _MARKER_CELL.match(text)}


def _grid_indent(row: list[str], header: dict, numbers: set[int] = frozenset()) -> float:
    """The indent a row draws with EMPTY cells before its name, inside the name
    column's span: PureCycle's "Subsidiary" header spans grid columns 0-3, and
    "PureCycle Technologies LLC" sits in the second, after one empty cell —
    under "… Holdings Corp." above it. A row number in front ("1.") is not an
    indent, nor the empty cell where another row has one (``numbers``);
    neither is anything without a grid."""
    i, spans, own = header.get("name"), header.get("spans"), getattr(row, "spans", None)
    if i is None or not spans or not own or i >= len(spans):
        return 0.0
    lo, hi = spans[i]
    lead = 0
    for text, (s, e) in zip(row, own):
        if s < lo or s in numbers:
            continue
        if s >= hi:
            return 0.0                    # no name inside the column: nothing drawn
        if text:
            return lead * _GRID_INDENT
        lead += e - s
    return 0.0


def _places_under(table: list, row: list, header: dict) -> int:
    """Rows below ``row`` that, read with ``header``, give a name and a place."""
    below = table[next(i for i, r in enumerate(table) if r is row) + 1:]
    return sum(1 for r in below if _column(r, header, "name")
               and jurisdiction_country(_column(r, header, "jurisdiction")))


def _section_header(row: list[str]) -> dict | None:
    """A header row met in the MIDDLE of a table (Inter & Co opens a second
    section "Subsidiary of Banco Inter S.A. | Jurisdiction | …"), held to a
    stricter test than the table's first rows: its name cell must be a LABEL.
    "Acme Company | State of Delaware" has header words in both cells and is a
    subsidiary all the same."""
    header = _find_header([row])
    if header is None:
        return None
    name_cell = row[header["name"]] if header["name"] < len(row) else ""
    return header if _NOISE.match(name_cell) else None


#: EDGAR's tag after a conformed name: "KEYCORP /NEW/", "DOW CHEMICAL CO /DE/"
_EDGAR_SUFFIX = re.compile(r"\s*/[A-Za-z]{2,5}/\s*$")


def _same_company(a: str | None, b: str | None) -> bool:
    """Same filer under a different spelling: "Inter & Co, Inc." / "Inter&Co,
    Inc" / "NEWS CORPORATION" vs "News Corp". Legal forms and punctuation
    dropped — a name's letters are what survives every filer's typesetting,
    accents too: EDGAR names the filer "Almacenes Exito S.A.", its own list
    "Almacenes Éxito S.A."."""
    def key(x):
        # NFKD splits "É" into "E" and an accent, which the filter below drops
        plain = unicodedata.normalize("NFKD", _EDGAR_SUFFIX.sub("", x))
        return re.sub(r"[^a-z0-9]", "", normalize_entity_name(plain))
    return bool(a and b) and key(a) == key(b)


def _compact(name: str) -> str:
    """A name's letters and digits, accents and case dropped — legal forms
    kept: "Vía Artika S. A." is "Vía Artika S.A.", "PureCycle Technologies
    LLC" is not "PureCycle Technologies, Inc."."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", _EDGAR_SUFFIX.sub("", name)).casefold())


def _list_key(entry: dict) -> tuple[str, str]:
    """One list entry per name AND place: a list repeats a row across printed
    pages, but Lavoro lists "Agrointegral Andina S.A.S." in Colombia and in
    Ecuador — two entries, two nodes (the writer matches each in its country)."""
    place = entry.get("jurisdiction") or ""
    return entry["name"].casefold(), jurisdiction_country(place) or place.casefold()


def _named_parent(text: str, registrant: str | None = None) -> str | None:
    """"Subsidiaries of X" → X; None for a generic X ("the Registrant"), the
    filer itself (Clearway heads every printed page "SUBSIDIARIES OF CLEARWAY
    ENERGY, INC." — that is the filer, not an intermediate) or no match."""
    m = _PARENT_HEADING.match(text.strip())
    if not m:
        return None
    who, _ = _clean_name(m.group(1).strip().rstrip(":").strip())
    who = re.sub(r"(?<=[A-Za-z.)])\d{1,2}$", "", who)                 # "…CORPORATION1": a footnote
    # "Subsidiaries of X at December 31, 2025" names X
    who = re.split(r"\s+(?:as of|as at|at|on)\s+\w+\s+\d", who, maxsplit=1)[0].strip()
    if not who or _GENERIC_PARENT.match(who) or len(who) > 100 or _same_company(who, registrant):
        return None
    return who


def _header_parent(header: dict, registrant: str | None) -> str | None:
    """The parent a header cell names: Inter & Co heads a section
    "Subsidiary of Banco Inter S.A." — that cell IS the name column header."""
    row = header.get("header_row") or []
    cell = row[header["name"]] if header["name"] < len(row) else ""
    return _named_parent(cell, registrant)


#: Indents closer than this (points) are the same level — filers' nbsp runs
#: and CSS paddings are not pixel-exact from row to row.
_LEVEL_STEP = 4.0
#: An indent this far beyond the previous row is a stray style, not a level.
_INDENT_OUTLIER = 60.0


def _assign_indent_parents(entries: list[dict], registrant: str | None = None) -> int:
    """Parent each entry by the nearest less-indented entry above it, when the
    exhibit draws a tree that way. Returns how many entries got a parent.

    Only an unambiguous tree counts: at least three indented rows (relative
    to the smallest indent, so a list indented uniformly — BlackRock's
    hanging indent — has none and stays flat) and every indented row with a
    less-indented row above it. A stray indent far beyond its neighbours is
    treated as noise. Levels are indents bucketed by `_LEVEL_STEP`.

    A row whose parent is the filer's own line (Chubb and NYT list themselves
    at the root) keeps `parent_basis` but no `parent`: the layout says it
    hangs directly off the filer.
    """
    if len(entries) < 4:
        return 0
    base = min(e["_indent"] for e in entries)
    levels = []
    prev = 0.0
    for e in entries:
        rel = e["_indent"] - base
        if rel - prev > _INDENT_OUTLIER:
            rel = prev
        levels.append(round(rel / _LEVEL_STEP))
        prev = rel
    if sum(1 for lv in levels if lv > 0) < 3:
        return 0
    stack: list[tuple[int, dict]] = []
    assigned = 0
    for e, lv in zip(entries, levels):
        while stack and stack[-1][0] >= lv:
            stack.pop()
        if lv > 0 and e.get("parent_basis") == "column":
            pass                    # a parent the filer NAMES beats one it draws
        elif lv > 0:
            if not stack:
                return 0            # an indented row with nothing above it
            # the filer's own line is a root: "PureCycle Technologies LLC",
            # indented under a holding, is not the filer "… Technologies, Inc."
            level, row_above = stack[-1]
            above = row_above["name"]
            e["parent"] = None if level == 0 and _same_company(above, registrant) else above
            e["parent_basis"] = "indent"
            assigned += 1
        elif _same_company(e["name"], registrant):
            e["parent_basis"] = "indent"        # the root: the filer's own line
        stack.append((lv, e))
    return assigned


def _ownership_cell(cell: str) -> tuple[float | None, list[dict]]:
    """(stake of the listed parent, co-holders) from a cell in a column the
    filer HEADS as ownership — which is why a bare number counts here."""
    if not cell:
        return None, []
    co = [{"name": n.strip(), "stake_percent": float(p)} for p, n in _CO_OWNER.findall(cell)]
    if co:
        first = _FIRST_PERCENT.match(cell)
        return (float(first.group(1)) if first else None), co
    if m := _BARE_PERCENT.match(cell):
        value = float(m.group(1))
        return (value if value <= 100 else None), []
    return None, []


# Footnote marks glued to a name — Tenet's "USPI Holding Company, Inc.1",
# Eversource's "NSTAR Electric Company (2) (3)", NYT's "NE Media Group, Inc.2"
# — and an inline stake, "The New York Times Building LLC (58%)".
# Footnote marks after a name: "(1)", "Ltd.2", "*", and the bracketed symbols
# Televisa and Magnum write — "(*)", "(#)", "(**)" (2026-10-07).
# XP's "(iv)" and BAT's "^" too.
_FOOTNOTE_TAIL = re.compile(r"(\s*\(\d{1,2}\)|(?<=[.)])\d{1,2}|\s*\*+|\s*\([*#†‡]{1,3}\)|"
                            r"\s*\((?:i{1,3}|iv|vi{0,3}|ix|x)\)|\s*\([a-h]\)|\s*\^+\d{0,2}|\s*#+)+$")
# One stake, or BAT's two — "(99.80%)(99.93%)": the first is the holding
_INLINE_STAKE = re.compile(r"\s*\((\d{1,3}(?:\.\d+)?)\s*%\)(?:\s*\(\d{1,3}(?:\.\d+)?\s*%\))?\s*$")


def _clean_name(name: str) -> tuple[str, float | None]:
    """(name without footnote marks, a stake stated inline in the name). A
    mark can follow the stake too: BAT's "… (Algérie) S.P.A. (51%)4". A tree
    marker before it goes too: Navigator's "~ Navigator Titan L.L.C."."""
    stake = None
    name = re.sub(r"^[~\u2013\u2022\u00b7-]+\s*", "", name)
    if m := _INLINE_STAKE.search(_FOOTNOTE_TAIL.sub("", name)):
        name = _FOOTNOTE_TAIL.sub("", name)
        stake = float(m.group(1))
        name = name[:m.start()]
    return _FOOTNOTE_TAIL.sub("", name).strip(), stake


# Header/footer rows and the registrant's own line are not subsidiaries.
_NOISE = re.compile(
    r"^(subsidiar(?:y|ies)|name|entity|jurisdiction|state|country|list of|exhibit|"
    r"significant|(?:in)?directly[- ]|partially[- ]|wholly[- ]owned|\*+$)", re.I)


# A list that is not about subsidiaries: AB InBev's note 34 goes on to "the
# most important companies consolidated by applying the equity method
# (ASSOCIATES)" — held, not controlled.
_NOT_SUBSIDIARIES = re.compile(r"associate|joint venture|equity method", re.I)
# "Cobrew N.V - Brouwerijplein 1, 3000 - Leuven": the registered office follows
# the name after a spaced dash.
_ADDRESS_TAIL = re.compile(r"\s+[-\u2013]\s+.*$")
# "Alibaba Information Port (Wulanchabu) Co., Ltd. (PRC)": the LAST bracket.
_PAREN_JURISDICTION = re.compile(r"^(?P<name>.+?)\s*\((?P<jur>[^()]{2,40})\)\s*\*?$")


def _grouped_list(sequence: list, registrant: str | None) -> list[dict]:
    """A list grouped under country rows, no jurisdiction column (AB InBev's
    note 34): a header "Name and registered office … | % economic interest",
    then a row holding only a country, then "Name - address | 61.63%" rows
    under it, across the tables a printed page splits it into. Read until a
    heading or header turns to associates or joint ventures. A row is taken
    only with a stake: footnote rows have none, and neither has the parent's
    own row ("Consolidating")."""
    out: list[dict] = []
    active, country = False, None
    for kind, item in sequence:
        if kind == "text":
            if _NOT_SUBSIDIARIES.search(item):
                active = False
            continue
        header_row = None
        for row in item[:3]:
            cells = [c for c in row if c]
            if len(cells) >= 2 and _H_NAME.search(cells[0]) \
                    and any(_H_OWNERSHIP.search(c) for c in cells[1:]) \
                    and not any(_H_JURISDICTION.search(c) for c in cells):
                header_row = row
                active, country = not _NOT_SUBSIDIARIES.search(" ".join(cells)), None
                break
        if not active:
            continue
        for row in item:
            if row is header_row:
                continue
            cells = [c for c in row if c and c != "."]
            if len(cells) == 1 and jurisdiction_country(cells[0]) is not None:
                country = cells[0]
                continue
            if country is None or len(cells) < 2:
                continue
            pct = _PERCENT.match(cells[-1])
            if not pct:
                continue       # a footnote, or "Consolidating" (the parent itself)
            name, _ = _clean_name(_ADDRESS_TAIL.sub("", cells[0]).strip())
            if not name or _NOISE.match(name) or _same_company(name, registrant):
                continue
            out.append({"name": name, "jurisdiction": country,
                        "stake_percent": float(pct.group(1))})
    return out if len(out) >= 3 else []


def _paragraph_list(sequence: list, registrant: str | None) -> list[dict]:
    """One subsidiary per paragraph, "Name (Jurisdiction)", no table at all
    (Alibaba's Exhibit 8.1: 160 paragraphs "… Co., Ltd. (PRC)"). Held to the
    content gate a headerless table meets: most such lines must name a place
    that maps to a country, and there must be a few of them."""
    shaped, out = 0, []
    for kind, item in sequence:
        if kind != "text":
            continue
        m = _PAREN_JURISDICTION.match(item.strip())
        if not m:
            continue
        shaped += 1
        jur = m.group("jur").strip()
        if jurisdiction_country(jur) is None:
            continue
        name, stake = _clean_name(m.group("name").strip())
        if not name or _NOISE.match(name) or _same_company(name, registrant):
            continue
        entry = {"name": name, "jurisdiction": jur}
        if stake is not None:
            entry["stake_percent"] = stake
        out.append(entry)
    return out if len(out) >= 3 and len(out) >= shaped / 2 else []

# ── what the filer declared the document to be ───────────────────────────────
# Every EDGAR document opens with its SGML header, "<TYPE>EX-2.1". A 20-F's
# Exhibit 2.1 is the description of securities, and Workiva names it
# "exhibit21descriptionofsecu.htm" — "ex21" to the filename patterns. 28 of the
# 86 2026 20-Fs whose candidate files all read nothing had such files (2.1,
# 2.10–2.14) or a guarantor list ("exhibit17subsidiaryissuers"), and only by
# luck did none parse to junk.
_DECLARED_TYPE = re.compile(r"<TYPE>\s*([^\s<]+)", re.I)
# EX-8 / EX-8.1 and EX-21.x — not EX-8.2: in an F-1 that is counsel's tax
# opinion ("We act as PRC counsel to Samfine …, a company incorporated in the
# Cayman Islands" read as a subsidiary).
_SUBSIDIARY_TYPE = re.compile(r"^EX-(?:0?8(?:\.0?1)?|21(?:\.\d+)?)$", re.I)


def declared_type(html: str) -> str | None:
    """The document type the filer declared ("EX-8.1"), or None without a header."""
    m = _DECLARED_TYPE.search(html[:2000])
    return m.group(1).upper() if m else None


def _subsidiary_document(kind: str, form: str | None) -> bool:
    """Whether a document declared ``kind`` is the subsidiary list. EX-8 is the
    list only in a 20-F: in an F-1, F-4 or 10-K it is counsel's tax opinion
    (AIR Global, Air Water: "We have acted as special United States counsel …").
    A list the filer mislabelled (Yatra's, declared "EX-10.8") is not read."""
    if kind.startswith("EX-8") and form and form.upper() not in ("20-F", "20-F/A"):
        return False
    return bool(_SUBSIDIARY_TYPE.match(kind))


# ── names grouped under one-cell country rows ────────────────────────────────
# BAT's Exhibit 8: "Albania" / "British American Tobacco – Albania SH.P.K." /
# "Algeria" / "… (Algérie) S.P.A. (51%)4" — one cell per row, 400 subsidiaries,
# the associates after them.
#
# A name that ends (or, Indonesian, starts) in a legal form is the shape of a
# company name: a one-cell row naming a country is a heading only without one.
_LEGAL_NAME = re.compile(
    r"(?:\b(?:limited|ltd|llc|l\.l\.c|inc|incorporated|corp|corporation|company|co|plc|p\.l\.c|gmbh|ag|"
    r"se|sa|s\.a|s\.a\.s|sas|s\.?r\.?l|b\.?v|n\.?v|pte\.?\s+ltd|pty\.?\s+ltd|ltda|limitada|sdn\.?\s+bhd|"
    r"k\.?k|oy|oyj|ab|as|a/s|aps|asa|spa|s\.p\.a|lp|l\.p|llp|dmcc|fze|fzco|fz-llc|jsc|tbk|sarl|s\.?a\.?r\.?l|"
    r"s\.? de r\.?l\.?(?: de c\.?v)?|s\.?a\.? de c\.?v|s\.?a\.?p\.?i\.? de c\.?v|kft|zrt|eood|ood|"
    r"sp\.? z o\.?o|s\.?r\.?o|d\.?o\.?o|bhd|pvt|private)\.?|有限公司|公司|"
    r"株式会社)\s*$|^PT\s", re.I)


def _finish(entry: dict, registrant: str | None) -> dict | None:
    """The row's name without its marks, or None for a row that is no company:
    a page header or footer (BAT's "… p.l.c. Form 20-F 2025"), a footnote, a
    registered office ("…, One Capital Place, PO Box 847, Grand Cayman …"),
    the filer itself."""
    name, stake = _clean_name(entry["name"].strip())
    if len(name) < 2 or len(name) > 150 or _NOISE.match(name) or _same_company(name, registrant) \
            or re.search(r"\bForm\s+(?:20-F|10-K)\b", name, re.I) or name.count(",") > 3:
        return None
    out = {**entry, "name": name}
    if stake is not None:
        out["stake_percent"] = stake
    return out


def _country_rows_list(sequence: list, registrant: str | None) -> list[dict]:
    out, country, countries = [], None, 0
    for kind, item in sequence:
        if kind == "text":
            if _NOT_SUBSIDIARIES.search(item):
                break
            continue
        for row in item:
            cells = [c for c in row if c]
            if len(cells) != 1:
                continue
            cell = cells[0]
            if len(cell) <= 40 and not _LEGAL_NAME.search(cell) and jurisdiction_country(cell):
                country, countries = cell, countries + 1
            elif country and not re.search(r"subsidiar|undertaking", cell, re.I):
                out.append({"name": re.sub(r"\s*[*#†]+(?:\s*\d{1,2})?$", "", cell),
                            "jurisdiction": country})
    found = [e for e in (_finish(e, registrant) for e in out) if e]
    company_like = sum(1 for e in found if _LEGAL_NAME.search(e["name"]))
    return found if countries >= 3 and len(found) >= 5 and company_like >= 0.6 * len(found) else []


def parse_exhibit(html: str, registrant: str | None = None, form: str | None = None) -> list[dict]:
    """[{name, jurisdiction, stake_percent?, co_owners?, parent?,
    parent_basis?}] from an Ex-21/Ex-8.1 page. ``registrant`` is the filer's
    name, so a heading or root row naming the filer itself is not taken for
    an intermediate parent.

    Per table: a header row naming the columns wins (that is how Bank of
    America's Location column is told apart from its Jurisdiction one, and
    how Astronics' Ownership Percentage column becomes a real stake). A
    table with a header that names NO jurisdiction-ish column is a different
    kind of table (AB InBev's securities listings) and is skipped whole.
    A headerless table inherits the previous table's header when its rows
    fit it — one list split over printed pages: Chubb's ownership column was
    lost on ten of its eleven pages — and is then held to the content gate
    below like any heuristic table; otherwise it falls back to the
    first-two-columns heuristic.

    Percentages are read wherever a filer puts them: an ownership column
    (with or without the % sign), inline in the name ("… LLC (58%)"), and a
    cell naming co-holders ("66.66% 33.33% (Chubb Bermuda Insurance Ltd.)"),
    which yields `co_owners` with their shares.

    Structure, where the filer shows it, and only then:
    - a heading between tables or a header cell naming a parent
      ("Consolidated Subsidiaries of USPI Holding Company, Inc.") parents
      the rows under it (`parent_basis: "heading"`);
    - a consistent indentation tree parents each row by the nearest
      less-indented row above (`parent_basis: "indent"`, the more specific
      of the two where both apply) — drawn by CSS, by non-breaking spaces or
      by empty cells before the name (PureCycle);
    - a column the filer heads "Direct controlling entity" (Almacenes Éxito)
      names each row's parent, and beats the other two (`parent_basis:
      "column"`; the filer named there means directly under it).
    `parent` is the listed name; resolving it to a node is the writer's job.
    `parent_basis` without a `parent` means the layout puts the row directly
    under the filer.

    Jurisdiction text is kept as filed; the ISO mapping is the writer's
    separate, lossy view of it.

    A document the filer declared something else ("<TYPE>EX-2.1") is no list,
    nor an EX-8 outside a 20-F (``form``) — see ``_subsidiary_document``.
    When the table reader finds nothing, the other layouts are tried in turn
    (``_grouped_list``, ``_paragraph_list``, the country rows only in a
    document declared EX-8 or EX-21). Every entry they read names its place.
    Not read: a list of names alone, and one subsidiary per line with its
    place in words ("Bluebottle Limited, a Hong Kong company") — that reader
    was removed 2026-10-08: it read combined filings' sibling lists as the
    filer's own and parents stated in words not at all."""
    kind = declared_type(html)
    if kind and not _subsidiary_document(kind, form):
        return []
    declared = kind is not None
    parser = _TableTextParser()
    parser.feed(html)
    parser.close()
    out, seen = [], set()
    carried: dict | None = None
    section_parent: str | None = None
    for kind, item in parser.sequence:
        if kind == "text":
            if _PARENT_HEADING.match(item.strip()):
                section_parent = _named_parent(item, registrant)
            continue
        table = item
        header = _find_header(table)
        if header is None and any(any(c) for r in table[:5] for c in r
                                  if _not_jurisdiction(c or "")):
            continue   # a labelled table that is about locations, not registration
        inherited = False
        if header is None and carried is not None:
            need = max(i for i in (carried["name"], carried["jurisdiction"],
                                   carried["ownership"]) if i is not None) + 1
            if sum(1 for r in table if len(r) >= need) >= len(table) / 2:
                # by cell index: the next page's grid is not the first page's
                # (BHP's header cell spans grid columns 1-2, the rows below
                # it on later pages one column each)
                header = {**carried, "header_row": None, "spans": None}
                inherited = True
        elif header is not None:
            carried = header
        table_parent = _header_parent(header, registrant) if header and header["header_row"] else None
        table_rows: list[dict] = []
        data_seen = False
        for row in table:
            stake, co_owners, holder = None, [], ""
            if header is not None:
                if row is header["header_row"]:
                    continue
                # A section header after rows of data re-maps the columns. Before
                # any, a row of labels is the rest of a header printed over
                # several rows, and the rows below decide which reading of it
                # holds: UTStarcom's third line "Name | Organization |
                # Ownership Interest" sits over the wrong cells, Supervielle's
                # second line "Subsidiary | incorporation | business" is the
                # one that names its subsidiary column.
                if (again := _section_header(row)) is not None and (
                        data_seen or _places_under(table, row, again) >= _places_under(table, row, header)):
                    header = carried = again      # a new section's columns
                    table_parent = _header_parent(again, registrant)
                    continue
                name = _column(row, header, "name")
                jurisdiction = _column(row, header, "jurisdiction")
                if header["ownership"] is not None:
                    stake, co_owners = _ownership_cell(_column(row, header, "ownership"))
                holder = _column(row, header, "parent")
            else:
                cells = [c for c in row if c]
                if len(cells) < 2:
                    continue
                name, jurisdiction = cells[0], cells[1]
                if _PERCENT.match(jurisdiction):
                    # a percent is a stake, not a place — try the next cell
                    stake = float(_PERCENT.match(jurisdiction).group(1))
                    jurisdiction = cells[2] if len(cells) > 2 else ""
            if not name or not jurisdiction:
                continue
            data_seen = True
            if _MARKER_CELL.match(name):
                continue                  # a row number is never a name (Chanson's "13")
            if _NOISE.match(name) or _NOISE.match(jurisdiction):
                continue
            # a jurisdiction is short; a long second column means prose
            if len(jurisdiction) > 60 or len(name) < 2:
                continue
            name, inline_stake = _clean_name(re.sub(r"^\d{1,3}\.\s+(?=\S)", "", name))   # AIOS: "1. YD …"
            if not name:
                continue
            entry = {"name": name, "jurisdiction": jurisdiction,
                     "_indent": getattr(row, "indent", 0.0) +
                     (_grid_indent(row, header, _number_columns(table, header)) if header else 0.0)}
            if stake is None:
                stake = inline_stake
            if stake is not None:
                entry["stake_percent"] = stake
            if co_owners:
                entry["co_owners"] = co_owners
            holder = _clean_name(holder)[0] if holder and not _NOISE.match(holder) else ""
            parent = table_parent or section_parent
            if holder:
                # the column the filer heads "Direct controlling entity": the
                # stake beside it is that holder's, so it is the parent — or
                # the filer itself, the row directly under it
                if _same_company(holder, registrant):
                    entry["parent_basis"] = "column"
                    if _compact(holder) != _compact(registrant or ""):
                        entry["_holder"] = holder         # or a listed namesake, see below
                elif not _same_company(holder, entry["name"]):
                    entry["parent"], entry["parent_basis"] = holder, "column"
            elif parent and not _same_company(parent, entry["name"]):
                entry["parent"], entry["parent_basis"] = parent, "heading"
            table_rows.append(entry)
        # Table-level sanity for HEADERLESS (or header-inheriting) tables: a
        # real subsidiary table's jurisdictions overwhelmingly map to
        # countries; a securities listing's ("Trading symbol", "New York
        # Stock Exchange") map not at all. A named header earns trust; a
        # heuristic table must earn it by content.
        if (header is None or inherited) and table_rows:
            mapped = sum(1 for e in table_rows
                         if jurisdiction_country(e["jurisdiction"]) is not None)
            if mapped < len(table_rows) / 2:
                continue
        for entry in table_rows:
            key = _list_key(entry)
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
    if not out:
        # Layouts the table reader cannot see, tried only when it found
        # nothing — so an exhibit it reads today is read exactly as before.
        seq = parser.sequence
        for entry in _grouped_list(seq, registrant) or _paragraph_list(seq, registrant) or \
                (declared and _country_rows_list(seq, registrant)) or []:
            if _list_key(entry) not in seen:
                seen.add(_list_key(entry))
                out.append({**entry, "_indent": 0.0})
    # A parent named as the list names it: "Vía Artika S. A." is the listed
    # "Vía Artika S.A."; a holder named like the filer but with another legal
    # form that the list carries is that listed company, not the filer.
    listed = {_compact(e["name"]): e["name"] for e in out}

    def as_listed(who: str) -> str | None:
        if _compact(who) in listed:
            return listed[_compact(who)]
        # "Seaspan Management Services Ltd." for the listed "… Limited":
        # the legal form aside, if exactly one listed name is it
        same = {e["name"] for e in out if _same_company(e["name"], who)}
        return same.pop() if len(same) == 1 else None

    for e in out:
        holder = e.pop("_holder", None)
        if holder and (hit := as_listed(holder)):
            e["parent"] = hit
        elif e.get("parent") and (hit := as_listed(e["parent"])):
            e["parent"] = hit
    # The indentation tree is exhibit-wide (a page break must not cut it) and
    # more specific than a section heading, so it wins where both apply.
    _assign_indent_parents(out, registrant)
    for entry in out:
        del entry["_indent"]
    return out


# Not glued to a word: "Mauritius", "Cyprus" and "Belarus" end in "us", and
# until 2026-10-07 all three mapped to the United States.
_US_SUFFIX = re.compile(r",?\s*(?<![^\W\d_])(U\.?S\.?A?\.?|United States)$", re.I)
# Chubb writes "USA (Delaware)"; Occidental writes Canadian provinces bare.
_USA_PAREN = re.compile(r"^(U\.?S\.?A?\.?|United States)\s*\(", re.I)
_GB_NATIONS = {"england & wales", "england and wales", "england", "scotland",
               "wales", "northern ireland"}
_CA_PROVINCES = {"alberta", "british columbia", "manitoba", "new brunswick",
                 "newfoundland and labrador", "nova scotia", "ontario",
                 "prince edward island", "quebec", "saskatchewan"}
_EXTRA_PLACES = {"nevis": "KN", "cayman": "KY", "prc": "CN",
                 "british virgin islands": "VG", "virgin islands (british)": "VG",
                 "bvi": "VG",         # ZTO Express writes the abbreviation
                 "korea": "KR",       # bare "Korea" in practice means the South
                 "columbia": "CO",    # a recurring filer typo for Colombia
                 "dubai": "AE", "macau": "MO", "macau sar": "MO",
                 "macao sar": "MO",
                 "cayman island": "KY", "curacao": "CW", "holland": "NL",
                 # BAT; "Congo" alone is the Republic of the Congo
                 "congo, democratic republic of": "CD", "congo, democratic republic of the": "CD",
                 "democratic republic of the congo": "CD", "democratic republic of congo": "CD"}
# "Saudi Arabia, Kingdom of" — inverted official names
_INVERTED_TAIL = re.compile(r",\s*(kingdom|republic|state|grand duchy)\s+of$", re.I)

# Some filers fuse the legal form into the jurisdiction cell — Texas
# Roadhouse writes "Kentucky limited liability company". Longest forms
# first, stripped repeatedly, and only as a FALLBACK after the direct
# lookups fail, so a real country name is never eaten.
_LEGAL_FORM_TAIL = re.compile(
    r"\s+(non-?profit corporation|limited liability compan(?:y|ies)|limited partnership|"
    r"general partnership|unlimited company|corporation|company|"
    r"partnership|entity|trust|llc|l\.?l\.?p\.?|l\.?p\.?|inc\.?)$", re.I)


def _is_us_state_code(code: str) -> bool:
    from app.scraper.gleif_reference import _US_STATES
    return code in _US_STATES


def jurisdiction_country(jurisdiction: str | None) -> str | None:
    """ISO-2 country for a filed jurisdiction, or None when unmappable.

    "Delaware, U.S." → US (the state stays in the stored jurisdiction text);
    bare US state names → US; country names via the shared nationality table.
    None means "store the text, skip the code" — never guess."""
    from app.scraper.gleif_reference import _US_STATE_NAMES
    from app.scraper.maintenance import nationality_to_iso2
    cleaned = (jurisdiction or "").strip()
    # Curly apostrophes and a leading article are filer typography, not
    # meaning: "The People\u2019s Republic of China" is China.
    cleaned = cleaned.replace("\u2019", "'")
    cleaned = re.sub(r"^the\s+", "", cleaned, flags=re.I)
    cleaned = _INVERTED_TAIL.sub("", cleaned).strip()
    if not cleaned:
        return None
    if _US_SUFFIX.search(cleaned) or _USA_PAREN.match(cleaned):
        return "US"
    low = cleaned.casefold()
    if low in _GB_NATIONS:
        return "GB"
    if low in _CA_PROVINCES:
        return "CA"
    if low in _EXTRA_PLACES:
        return _EXTRA_PLACES[low]
    # "Hong Kong SAR, China" is Hong Kong — the comma rule below took China;
    # "the Macau Special Administrative Region of the People's Republic of
    # China" (Melco) is Macau
    if m := re.match(r"^(hong kong|macau|macao)\s+(?:sar|special administrative region)"
                     r"(?:,?\s*china|\s+of\s+(?:the\s+)?(?:prc|people's republic of china))?$", low):
        return "HK" if m.group(1) == "hong kong" else "MO"
    if low in ("chinese mainland", "mainland china", "mainland"):     # Sinovac
        return "CN"
    if re.fullmatch(r"[A-Z]{2}", cleaned) and _is_us_state_code(cleaned):
        return "US"                              # Eversource: "CT", "DE" (Delaware, not Germany)
    if cleaned.casefold() in _US_STATE_NAMES:   # bare "Delaware" — filers vary
        return "US"
    if code := nationality_to_iso2(cleaned):
        return code
    # "Canada (Ontario)" — country with a parenthetical subdivision: the
    # country part decides (the _USA_PAREN special case, generalized).
    if m := re.match(r"^([^()]+?)\s*\(", cleaned):
        if code := jurisdiction_country(m.group(1)):
            return code
    # "Quebec, Canada" / "Toronto, Ontario, Canada" — the LAST comma part is
    # the country; the front is a subdivision the code doesn't need.
    if "," in cleaned:
        if code := jurisdiction_country(cleaned.rsplit(",", 1)[1]):
            return code
    # "São Paulo – Brazil" (Bradesco), "Luxembourg – G. Ducado": a spaced dash
    # parts a city or a description from the country — the last part, else the first.
    parts = re.split(r"\s+[-\u2013\u2014]\s+", cleaned)
    if len(parts) > 1:
        for part in (parts[-1], parts[0]):
            if code := jurisdiction_country(part):
                return code
    # "The Republic of the Marshall Islands" (Scorpio Tankers), "the Republic
    # of Chile" (Banco de Chile), "Kingdom of Saudi Arabia" (Borr), "the
    # Commonwealth of Virginia" (Shenandoah), "State of Israel" — but never
    # "Republic of China": Taiwan.
    if (m := re.match(r"^(?:republic|kingdom|sultanate|principality|grand duchy|commonwealth|state) of "
                      r"(?:the\s+)?(.+)$",
                      cleaned, re.I)) and m.group(1).casefold() != "china":
        if code := jurisdiction_country(m.group(1)):
            return code
    # "Panamá" — accents are typography the country table does not carry
    import unicodedata
    plain = "".join(c for c in unicodedata.normalize("NFKD", cleaned) if not unicodedata.combining(c))
    if plain != cleaned and (code := jurisdiction_country(plain)):
        return code
    # Fallback: peel fused legal forms ("Kentucky limited liability company")
    # and retry — repeatedly, since forms stack ("X company limited").
    stripped = cleaned
    while (peeled := _LEGAL_FORM_TAIL.sub("", stripped)) != stripped:
        stripped = peeled.strip()
        if stripped.casefold() in _US_STATE_NAMES:
            return "US"
        if code := nationality_to_iso2(stripped):
            return code
    return None


# ISO 3166-2 subdivision codes the FRONTEND can name (US states, CA provinces,
# GB nations, and the handful GLEIF uses). A jurisdiction like "Florida, USA"
# says more than "US" — it says Florida — and the panel shows it as a
# "Registered in" row. Only these families; elsewhere a region is administrative,
# not a domicile choice, so there is nothing to record (see _jurisdiction_code
# in gleif_lei_cdf for the same reasoning).
_CA_PROVINCE_CODES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL", "nova scotia": "NS",
    "northwest territories": "NT", "nunavut": "NU", "ontario": "ON",
    "prince edward island": "PE", "quebec": "QC", "saskatchewan": "SK",
    "yukon": "YT",
}
_GB_NATION_CODES = {"england": "ENG", "scotland": "SCT", "wales": "WLS",
                    "northern ireland": "NIR", "england & wales": "ENG",
                    "england and wales": "ENG"}
_OTHER_SUBDIVISION_CODES = {"nevis": "KN-N", "saint kitts": "KN-K",
                           "dubai": "AE-DU", "abu dhabi": "AE-AZ",
                           "sharjah": "AE-SH"}


def _us_state_code(name: str) -> str | None:
    from app.scraper.gleif_reference import _US_STATES
    inv = {v.casefold(): k for k, v in _US_STATES.items()}
    return inv.get(name.strip().casefold())


def jurisdiction_subdivision(jurisdiction: str | None) -> str | None:
    """ISO 3166-2 subdivision for a filed jurisdiction, or None.

    "Florida, USA" -> US-FL, "Delaware" -> US-DE, "England" -> GB-ENG,
    "Quebec, Canada" -> CA-QC, "Canada (Ontario)" -> CA-ON, "Nevis" -> KN-N.
    Only families the panel can name; None means "no finer grain to show",
    never a country (that is jurisdiction_country's job)."""
    cleaned = (jurisdiction or "").strip().replace("\u2019", "'")
    if not cleaned:
        return None
    if re.fullmatch(r"[A-Z]{2}", cleaned) and _is_us_state_code(cleaned):
        return f"US-{cleaned}"                   # a bare state code (Eversource)
    # peel a US/Canada country suffix or parenthetical to expose the state/province
    # "Florida, USA" / "Delaware, U.S." -> "Florida"; "USA (Delaware)" -> "Delaware"
    core = _US_SUFFIX.sub("", cleaned).strip().strip(",").strip()
    for pat, iso in ((_USA_PAREN, "US"),):
        if pat.match(cleaned):
            inside = re.search(r"\(([^)]+)\)", cleaned)
            if inside:
                core = inside.group(1).strip()
    # "Canada (Ontario)" / "Quebec, Canada" -> the province part
    m_paren = re.match(r"^(?:canada)\s*\(([^)]+)\)$", cleaned, re.I)
    if m_paren:
        core = m_paren.group(1).strip()
    elif "," in cleaned and cleaned.rsplit(",", 1)[1].strip().casefold() in (
            "canada", "usa", "u.s.", "u.s.a.", "united states"):
        core = cleaned.rsplit(",", 1)[0].strip()
        # a two-level "Toronto, Ontario, Canada" -> take the last non-country part
        if "," in core:
            core = core.rsplit(",", 1)[1].strip()
    # peel a fused legal form ("Virginia corporation" -> "Virginia"), same as
    # jurisdiction_country's fallback
    peeled = core
    while (nxt := _LEGAL_FORM_TAIL.sub("", peeled)) != peeled:
        peeled = nxt.strip()
    core = peeled or core
    core = re.sub(r"^(?:the\s+)?(?:commonwealth|state) of\s+", "", core, flags=re.I)  # "Commonwealth of Virginia"
    low = core.casefold()
    if code := _us_state_code(core):
        return f"US-{code}"
    if code := _CA_PROVINCE_CODES.get(low):
        return f"CA-{code}"
    if code := _GB_NATION_CODES.get(low):
        return f"GB-{code}"
    if code := _OTHER_SUBDIVISION_CODES.get(low):
        return code
    return None


def fetch_subsidiaries(cik: str, registrant: str | None = None) -> dict | None:
    """The latest annual filing's subsidiary list for a CIK, with provenance.

    Tries each candidate exhibit until one parses to subsidiaries — filename
    numbering is ambiguous (ex215 = 2.15 or 21.5), so the content decides —
    then, for a 20-F, the note of the main document its exhibit index points
    to (``note_in_main_document``).
    {"subsidiaries": [...], "form", "filing_date", "url"} or None."""
    filings = annual_filings(cik)
    if not filings:
        return None
    for meta in exhibit_candidates(cik, *filings[0]):
        subs = parse_exhibit(_get_text(meta["url"]), registrant, meta["form"])
        if subs:
            return {"subsidiaries": subs, "form": meta["form"],
                    "filing_date": meta["filing_date"], "url": meta["url"]}
        log.info("candidate %s parsed to zero subsidiaries — trying the next",
                 meta["url"])
    return list_from_main_document(cik, filings[0], registrant)


# The 8.1 entry of a 20-F's exhibit index, up to the next exhibit number:
# "8.1 List of significant subsidiaries (included in note 34 to our audited
# consolidated financial statements …)" (AB InBev), "… is set forth in Note 26"
# (BW LPG), "… (see Note 2 …)" (Ferroglobe), "… (set forth in Note 38 …)" (HSBC).
_EXHIBIT_ENTRY = re.compile(r"(?<![\d.])8\.1(?!\d)\W(.{0,450})", re.S)
# The next entry's number: "11.1 Insider Trading Policy", "12.1* Certification"
# — followed by its description, never "Exhibit 21.1 to our Form F-1" (a
# reference inside this entry) nor a table column "8.1 03/16/2023".
# The index is in ascending order, so the next entry is numbered 9 or above —
# a "8.1 March 9, 2023" column of the same row is not one.
_NEXT_EXHIBIT = re.compile(r"(?<!exhibit)(?<!exhibits)\s(?:9|[1-9]\d)\.\d{1,2}[*#+\u2020]*\s+(?=[A-Z])",
                           re.I)
_NOTE_NUMBER = re.compile(r"\bnotes?\s+(\d{1,3})\b", re.I)
# …but not a reference to ANOTHER filing ("Exhibit 8.1 to our Form 20-F filed
# on …"): that list lives in the earlier filing (``earlier_filing_reference``).
_OTHER_FILING = re.compile(r"exhibit\s+\d|filed (?:on|with)|form\s+(?:20-F|F-1|S-1|F-4)\b", re.I)
_THIS_FILING = re.compile(r"(?:this|the) (?:annual report|form 20-f)|included herein", re.I)


def exhibit_entry(text: str) -> str | None:
    """The text of the 8.1 entry that names subsidiaries, or None."""
    for m in _EXHIBIT_ENTRY.finditer(text):
        entry = m.group(1)
        nxt = _NEXT_EXHIBIT.search(entry)
        entry = entry[:nxt.start()] if nxt else entry
        if re.search(r"subsidiar", entry, re.I):
            return entry
    return None


def _note_of_entry(entry: str | None) -> int | None:
    """The note number an 8.1 entry points to inside THIS filing."""
    if not entry or (_OTHER_FILING.search(entry) and not _THIS_FILING.search(entry)):
        return None
    m = _NOTE_NUMBER.search(entry)
    return int(m.group(1)) if m else None


def _note_heading(n: int) -> re.Pattern:
    """A note's heading in the HTML: ">34. AB InBev companies", ">Note 26 –
    Subsidiaries", ">32 Group companies" — the number at the start of an
    element, then punctuation or space, then (past any tags) a capital."""
    sep = r"(?:\s|&#160;|&nbsp;|\xa0|[.:\u2013\u2014-])"
    return re.compile(rf">\s*(?:note\s*)?{n}{sep}+(?:<[^>]+>\s*)*[A-Z]", re.I)


#: Share of a note list's jurisdictions that must be places. Stricter than an
#: exhibit's gate (half): a note holds other tables too. Novartis' note 31 read
#: 30 of several hundred rows with the CITY column as jurisdiction ("East
#: Hanover, NJ", "London 5") and a header row as a company — 70 % mapped,
#: and wrong; AB InBev, BR Partners and Trinity Biotech map 100 %.
_NOTE_PLACES = 0.9


def _mostly_places(subs: list[dict]) -> bool:
    """The content gate for a list read from a note of the main document."""
    return bool(subs) and \
        sum(1 for e in subs if jurisdiction_country(e["jurisdiction"])) >= _NOTE_PLACES * len(subs)


def _main_document(cik: str, form: str, accession: str) -> tuple[str, str] | None:
    """(url, html) of a filing's main document — the one named like the form,
    else the largest that is not an exhibit or an XBRL page."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    items = (_get(f"{base}/index.json").get("directory") or {}).get("item") or []
    docs = [it for it in items if (it.get("name") or "").lower().endswith((".htm", ".html"))
            and not re.match(r"R\d+\.htm", it.get("name") or "")
            and not re.search(r"ex[-._]?\d|-index", it.get("name") or "", re.I)]
    if not docs:
        return None
    tag = form.lower().replace("-", "")
    main = next((d for d in docs if tag in d["name"].lower().replace("-", "")),
                max(docs, key=lambda d: int(d.get("size") or 0)))
    url = f"{base}/{main['name']}"
    return url, _get_text(url)


def list_from_main_document(cik: str, filing: tuple, registrant: str | None = None) -> dict | None:
    """A 20-F with no subsidiary exhibit file: what its exhibit index says
    under 8.1 decides where the list is (measured on all 1,012 20-Fs of 2026):
    - a note of this filing (``note_in_main_document``), or
    - an EARLIER filing — "incorporated by reference to Exhibit 8.1 of our
      Form 20-F filed on March 29, 2018", "Exhibit 21.1 to our Form F-1 (File
      No. 333-286211)" — 19 % of all 20-Fs (``list_from_earlier_filing``).
    The latest filing only: a main document is ~10 MB, so the multi-year
    history does not do this."""
    form, accession, filed, _period = filing
    if form != "20-F":
        return None
    got = _main_document(cik, form, accession)
    if not got:
        return None
    url, doc = got
    text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", doc)))
    entry = exhibit_entry(text)
    if entry is None:
        return None
    n = _note_of_entry(entry)
    if n is not None:
        return note_in_main_document(url, doc, n, form, filed, registrant)
    return list_from_earlier_filing(cik, entry, text, registrant,
                                    confirmed_by=url, confirmed_on=_iso_date(filed))


def note_in_main_document(url: str, doc: str, n: int, form: str, filed: str,
                          registrant: str | None = None) -> dict | None:
    """The list in note ``n`` of the main document: found by its heading (the
    LAST one — a contents page comes first), read up to the next note with the
    exhibit parser, and kept only if it passes the note gate."""
    heads = list(_note_heading(n).finditer(doc))
    if not heads:
        log.info("%s: note %d named in the exhibit index, heading not found", url, n)
        return None
    start = heads[-1].start()
    nxt = _note_heading(n + 1).search(doc, start)
    subs = parse_exhibit(doc[start:nxt.start() if nxt else len(doc)], registrant)
    if not _mostly_places(subs):
        return None
    return {"subsidiaries": subs, "form": form, "filing_date": _iso_date(filed),
            "url": f"{url}#note-{n}"}


# ── a list incorporated by reference to an earlier filing ─────────────────────

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_MONTH_RE = "|".join(_MONTHS)
_DATE_MDY = re.compile(rf"\b({_MONTH_RE})\s+(\d{{1,2}}),?\s+(\d{{4}})", re.I)
_DATE_DMY = re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_RE}),?\s+(\d{{4}})", re.I)
_DATE_NUM = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4}|\d{2})\b(?![/.]\d)")
_REF_FORM = re.compile(r"\b(?:form\s+)?(20-F|F-1|F-3|F-4|S-1|S-4|10-K)(?:/A)?\b", re.I)
_REF_EXHIBIT = re.compile(r"exhibit\s+(\d{1,2}\.\d{1,2})", re.I)
_REF_TABLE_EXHIBIT = re.compile(r"\b(\d{1,2}\.\d{1,2})\s+(?:\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|"
                                rf"(?:{_MONTH_RE})\s)", re.I)
_REF_ACCESSION = re.compile(r"\b(\d{10}-\d{2}-\d{6})\b")
_REF_FILE_NO = re.compile(r"\b((?:333|001|000)-\d{5,6})\b")
_FOOTNOTE = re.compile(r"\((\d{1,2})\)")


def _dates(text: str) -> list:
    """Every date the text states, as datetime.date — a numeric one both ways
    (05/03/2023 is March 5 in Europe, May 3 in the US; the filing that exists
    decides), two-digit years as 20xx."""
    import datetime as dt
    out = []
    for m in _DATE_MDY.finditer(text):
        try:
            out.append(dt.date(int(m.group(3)), _MONTHS.index(m.group(1).lower()) + 1, int(m.group(2))))
        except ValueError:
            pass
    for m in _DATE_DMY.finditer(text):
        try:
            out.append(dt.date(int(m.group(3)), _MONTHS.index(m.group(2).lower()) + 1, int(m.group(1))))
        except ValueError:
            pass
    for m in _DATE_NUM.finditer(text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        for month, day in ((a, b), (b, a)):
            try:
                out.append(dt.date(y, month, day))
            except ValueError:
                pass
    return out


def earlier_filing_reference(entry: str, text: str = "") -> dict:
    """What an 8.1 entry says about the filing that holds the list: forms,
    dates, exhibit number, accession, file number. A bare footnote mark
    ("List of Subsidiaries (21)") is replaced by the footnote's own text,
    found in the document ("(21) Incorporated by reference to …")."""
    fn = _FOOTNOTE.search(entry)
    if fn and not _REF_FORM.search(entry) and not _dates(entry) and text:
        m = re.search(rf"\({fn.group(1)}\)\s*((?:incorporated|previously|filed|included)"
                      rf"[^()]{{0,400}}(?:\([^()]*\)[^()]{{0,200}})?)", text, re.I)
        if m:
            entry = f"{entry} {m.group(1)}"
    forms = [f.upper() for f in _REF_FORM.findall(entry)]
    if re.search(r"annual report", entry, re.I) and "20-F" not in forms:
        forms.append("20-F")
    exhibit = _REF_EXHIBIT.search(entry) or _REF_TABLE_EXHIBIT.search(entry)
    accession = _REF_ACCESSION.search(entry)
    file_no = _REF_FILE_NO.search(entry)
    return {"forms": forms, "dates": _dates(entry),
            "exhibit": exhibit.group(1) if exhibit else None,
            "accession": accession.group(1) if accession else None,
            "file_no": file_no.group(1) if file_no else None,
            "year_ended": bool(re.search(r"year ended", entry, re.I))}


def _all_filings(cik: str) -> list[tuple[str, str, str, str, str]]:
    """(form, accession, filing date, report date, file number) of every filing
    on the submissions API, older pages included."""
    try:
        subs = _get(f"https://data.sec.gov/submissions/CIK{_cik10(cik)}.json")
    except Exception as exc:  # noqa: BLE001 - a stale CIK is absent, not an error
        log.info("submissions unavailable for CIK %s: %s", cik, exc)
        return []
    pages = [(subs.get("filings") or {}).get("recent") or {}]
    for f in ((subs.get("filings") or {}).get("files") or [])[:HISTORY_MAX_OLDER_PAGES]:
        try:
            pages.append(_get(f"{SUBMISSIONS_URL}/{f['name']}"))
        except Exception as exc:  # noqa: BLE001 - best-effort
            log.warning("older submissions page %s failed: %s", f.get("name"), exc)
            break
    out = []
    for p in pages:
        n = len(p.get("form") or [])
        out.extend(zip(p.get("form") or [], p.get("accessionNumber") or [], p.get("filingDate") or [],
                       p.get("reportDate") or [""] * n, p.get("fileNumber") or [""] * n))
    return out


def resolve_reference(cik: str, ref: dict) -> list[tuple[str, str, str]]:
    """The filings a reference can mean, best first: by accession; else the
    stated form on the stated date (± a day, or the report date for "the year
    ended …"); else, with no date, the stated form under the stated file
    number, newest first. An original before its amendment."""
    import datetime as dt
    filings = _all_filings(cik)
    if ref["accession"]:
        return [(f, a, d) for f, a, d, _r, _n in filings if a == ref["accession"]]
    families = {f.split("/")[0] for f in ref["forms"]}
    if not families:
        return []
    hits = []
    for day in ref["dates"]:
        for f, a, d, rd, _n in filings:
            if f.split("/")[0] not in families:
                continue
            near = abs((dt.date.fromisoformat(d) - day).days) <= 1
            if near or (ref["year_ended"] and rd == day.isoformat()):
                hits.append((f, a, d))
    if not hits and not ref["dates"] and ref["file_no"]:
        hits = sorted(((f, a, d) for f, a, d, _r, n in filings
                       if f.split("/")[0] in families and n == ref["file_no"]),
                      key=lambda h: h[2], reverse=True)
    seen, out = set(), []
    for h in sorted(hits, key=lambda h: "/A" in h[0]):
        if h[1] not in seen:
            seen.add(h[1])
            out.append(h)
    return out


def list_from_earlier_filing(cik: str, entry: str, text: str, registrant: str | None = None,
                             confirmed_by: str | None = None,
                             confirmed_on: str | None = None) -> dict | None:
    """The list a 20-F incorporates by reference: the exhibit of the earlier
    filing it names. Dated by THAT filing — the list describes the group as it
    was then (the user's call, 2026-10-07) — with the current 20-F that
    re-affirms it as ``confirmed_by`` / ``confirmed_on``."""
    ref = earlier_filing_reference(entry, text)
    exhibit_21 = (ref["exhibit"] or "").startswith("21")
    for form, accession, filed in resolve_reference(cik, ref)[:4]:
        for meta in exhibit_candidates(cik, "10-K" if exhibit_21 else "20-F", accession, filed):
            subs = parse_exhibit(_get_text(meta["url"]), registrant, form)
            if subs:
                return {"subsidiaries": subs, "form": form, "filing_date": _iso_date(filed),
                        "url": meta["url"], "exhibit": "21" if exhibit_21 else "8.1",
                        "confirmed_by": confirmed_by, "confirmed_on": confirmed_on}
    return None


def _parse_first(candidates: list[dict]) -> tuple[list[dict], dict] | None:
    """The first candidate exhibit that parses to subsidiaries, with its meta."""
    for meta in candidates:
        subs = parse_exhibit(_get_text(meta["url"]), form=meta["form"])
        if subs:
            return subs, meta
        log.info("candidate %s parsed to zero subsidiaries — trying the next", meta["url"])
    return None


def fetch_subsidiary_history(cik: str, max_filings: int = HISTORY_MAX_FILINGS) -> list[dict]:
    """The subsidiary lists of the company's annual filings, newest first.

    [{"as_of", "filing_date", "form", "url", "names": {normalised names}}] —
    ``as_of`` is the fiscal year-end the list describes (the filing date when
    EDGAR gives no report date); ``names`` is
    None for a filing whose exhibit could not be read (an old plain-text one, a
    missing exhibit). ``earliest_listing`` treats that as a gap, so a year we
    cannot read never extends a subsidiary's history."""
    from app.scraper.mapper import normalize_entity_name
    out: list[dict] = []
    for form, accession, filed, period in annual_filings(cik, include_older=True)[:max_filings]:
        as_of = _iso_date(period) if period else _iso_date(filed)
        try:
            got = _parse_first(exhibit_candidates(cik, form, accession, filed, period))
        except Exception as exc:  # noqa: BLE001 - one bad year is a gap, not a failure
            log.warning("annual filing %s: exhibit unreadable: %s", accession, exc)
            got = None
        if got:
            subs, meta = got
            names = {normalize_entity_name(sub["name"]) or sub["name"].lower() for sub in subs}
            out.append({"as_of": as_of, "filing_date": meta["filing_date"], "form": form,
                        "url": meta["url"], "names": names})
        else:
            out.append({"as_of": as_of, "filing_date": _iso_date(filed), "form": form,
                        "url": None, "names": None})
    return out


#: `since_basis` values for a start date read off the annual lists (see
#: `earliest_listing`). Neither is a stated start.
FIRST_LISTED, NEWLY_LISTED = "first_listed", "newly_listed"


def earliest_listing(history: list[dict], name: str) -> dict | None:
    """The oldest filing in the UNBROKEN run of annual lists naming ``name``,
    counting back from the newest — {"as_of", "filing_date", "url", "basis"} —
    or None when the newest list does not name it. ``as_of`` (that list's
    fiscal year-end) is the lower bound: held then, possibly longer.

    ``basis`` says how much the history knows about the time BEFORE the run:

    * ``"newly_listed"`` — the list for the year before was read, does not name
      the company, and neither does any older list we could read: it first
      appears here. Shown as "first listed 2025".
    * ``"first_listed"`` — nothing to say: the run reaches the oldest list, or
      the list before it could not be read, or an even OLDER list names the
      company (dropped and back: on News Corp's 14 lists that is 37 of the 181
      subsidiaries missing from the year before — one in five). Shown as
      "since 2025 or earlier".

    The run stops at the first year the name is missing or the list could not
    be read. Filers may leave out insignificant subsidiaries (Reg S-K Item
    601(b)(21)), so a gap proves nothing either way; stopping there keeps the
    result a LOWER bound that is still true: the company held the subsidiary
    at least since that filing, possibly longer."""
    from app.scraper.mapper import normalize_entity_name
    key = normalize_entity_name(name) or (name or "").lower()
    found, stopped = None, None
    for i, entry in enumerate(history):
        if not entry["names"] or key not in entry["names"]:
            stopped = i
            break
        found = {"as_of": entry["as_of"], "filing_date": entry["filing_date"], "url": entry["url"]}
    if found is None:
        return None
    absent_before = (stopped is not None and history[stopped]["names"] is not None
                     and not any(e["names"] and key in e["names"] for e in history[stopped + 1:]))
    found["basis"] = NEWLY_LISTED if absent_before else FIRST_LISTED
    return found
