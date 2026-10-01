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
first and falls back to line pairs; jurisdictions like "Delaware, U.S." are
split into country US (the state is kept as display text). Measured, not
documented: Apple/Microsoft/Alphabet exhibits are all two-column tables.
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from app.scraper.mapper import normalize_entity_name
from app.scraper.sec_edgar import SUBMISSIONS_URL, _cik10, _get, _get_text, _iso_date

log = logging.getLogger(__name__)

# 10-K first (domestic, Ex-21), then 20-F (foreign private issuers, Ex-8.1 —
# same content, different exhibit number in the rulebook).
_ANNUAL_FORMS = ("10-K", "20-F")
_EXHIBIT_PATTERNS = (re.compile(r"ex[-._]?21", re.I),
                     re.compile(r"exhibit[-._]?21", re.I),
                     re.compile(r"subsidiar", re.I),
                     re.compile(r"ex[-._]?8[-._]?1", re.I))


#: How far back the subsidiary history reads: one annual filing per year, two
#: requests each (the filing index and the exhibit; exhibits are Archives files
#: and cached forever). Electronic exhibits in HTML start around 2001.
HISTORY_MAX_FILINGS = 25
#: Older submission pages opened for filings beyond the inline "recent" list.
HISTORY_MAX_OLDER_PAGES = 3

_EX21_NAMES = (re.compile(r"ex[-._]?21", re.I), re.compile(r"exhibit[-._]?21", re.I),
               re.compile(r"subsidiar", re.I))
_EX8_NAMES = (re.compile(r"ex[-._]?8[-._]?1", re.I), re.compile(r"dex8", re.I),
              re.compile(r"subsidiar", re.I))


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
    text was indented. Filers draw the group tree with indentation (Chubb,
    Eversource, NYT), and a plain list of strings threw that away."""
    __slots__ = ("indent",)

    def __init__(self, cells=(), indent: float = 0.0):
        super().__init__(cells)
        self.indent = indent


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
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._cell_indent = _css_indent(attrs)
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
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                row = _Row(self._row, self._row_indent or 0.0)
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
        # rows outside any <table> (malformed HTML) — collect as one table
        if not self.tables or self.tables[-1] is not self.__dict__.setdefault(
                "_orphans", []):
            self.tables.append(self.__dict__["_orphans"])
            self.sequence.append(("table", self.__dict__["_orphans"]))
        return self.__dict__["_orphans"]

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
_H_JURISDICTION = re.compile(r"jurisdiction|incorporat|organi[sz]|country|state", re.I)
_H_OWNERSHIP = re.compile(r"ownership|percent|%|owned|interest", re.I)
# "Location"/"Address" is where an office SITS, not where the company is
# registered — Bank of America has both columns, and taking Location wrote
# "San Francisco, CA" as a jurisdiction.
_H_NOT_JURISDICTION = re.compile(r"location|address|city", re.I)

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
        name_i = jur_i = own_i = None
        for i, cell in enumerate(row):
            if not cell:
                continue
            if jur_i is None and _H_JURISDICTION.search(cell) \
                    and not _H_NOT_JURISDICTION.search(cell) \
                    and jurisdiction_country(cell) is None:
                jur_i = i
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
            return {"name": name_i, "jurisdiction": jur_i, "ownership": own_i,
                    "header_row": row}
    return None


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


def _same_company(a: str | None, b: str | None) -> bool:
    """Same filer under a different spelling: "Inter & Co, Inc." / "Inter&Co,
    Inc" / "NEWS CORPORATION" vs "News Corp". Legal forms and punctuation
    dropped — a name's letters are what survives every filer's typesetting."""
    def key(x): return re.sub(r"[^a-z0-9]", "", normalize_entity_name(x))
    return bool(a and b) and key(a) == key(b)


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
        if lv > 0:
            if not stack:
                return 0            # an indented row with nothing above it
            above = stack[-1][1]["name"]
            e["parent"] = None if _same_company(above, registrant) else above
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
_FOOTNOTE_TAIL = re.compile(r"(\s*\(\d{1,2}\)|(?<=[.)])\d{1,2}|\s*\*+)+$")
_INLINE_STAKE = re.compile(r"\s*\((\d{1,3}(?:\.\d+)?)\s*%\)\s*$")


def _clean_name(name: str) -> tuple[str, float | None]:
    """(name without footnote marks, a stake stated inline in the name)."""
    stake = None
    if m := _INLINE_STAKE.search(name):
        stake = float(m.group(1))
        name = name[:m.start()]
    return _FOOTNOTE_TAIL.sub("", name).strip(), stake


# Header/footer rows and the registrant's own line are not subsidiaries.
_NOISE = re.compile(
    r"^(subsidiar(?:y|ies)|name|entity|jurisdiction|state|country|list of|exhibit|"
    r"significant|(?:in)?directly[- ]|partially[- ]|wholly[- ]owned|\*+$)", re.I)


def parse_exhibit(html: str, registrant: str | None = None) -> list[dict]:
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
      of the two where both apply).
    `parent` is the listed name; resolving it to a node is the writer's job.
    `parent_basis` without a `parent` means the layout puts the row directly
    under the filer.

    Jurisdiction text is kept as filed; the ISO mapping is the writer's
    separate, lossy view of it."""
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
                                  if _H_NOT_JURISDICTION.search(c or "")):
            continue   # a labelled table that is about locations, not registration
        inherited = False
        if header is None and carried is not None:
            need = max(i for i in (carried["name"], carried["jurisdiction"],
                                   carried["ownership"]) if i is not None) + 1
            if sum(1 for r in table if len(r) >= need) >= len(table) / 2:
                header = {**carried, "header_row": None}
                inherited = True
        elif header is not None:
            carried = header
        table_parent = _header_parent(header, registrant) if header and header["header_row"] else None
        table_rows: list[dict] = []
        for row in table:
            stake, co_owners = None, []
            if header is not None:
                if row is header["header_row"]:
                    continue
                if (again := _section_header(row)) is not None:
                    header = carried = again      # a new section's columns
                    table_parent = _header_parent(again, registrant)
                    continue
                name = row[header["name"]] if header["name"] < len(row) else ""
                jurisdiction = (row[header["jurisdiction"]]
                                if header["jurisdiction"] < len(row) else "")
                if header["ownership"] is not None and header["ownership"] < len(row):
                    stake, co_owners = _ownership_cell(row[header["ownership"]] or "")
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
            if _NOISE.match(name) or _NOISE.match(jurisdiction):
                continue
            # a jurisdiction is short; a long second column means prose
            if len(jurisdiction) > 60 or len(name) < 2:
                continue
            name, inline_stake = _clean_name(name)
            if not name:
                continue
            entry = {"name": name, "jurisdiction": jurisdiction,
                     "_indent": getattr(row, "indent", 0.0)}
            if stake is None:
                stake = inline_stake
            if stake is not None:
                entry["stake_percent"] = stake
            if co_owners:
                entry["co_owners"] = co_owners
            parent = table_parent or section_parent
            if parent and not _same_company(parent, entry["name"]):
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
            key = entry["name"].casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
    # The indentation tree is exhibit-wide (a page break must not cut it) and
    # more specific than a section heading, so it wins where both apply.
    _assign_indent_parents(out, registrant)
    for entry in out:
        del entry["_indent"]
    return out


_US_SUFFIX = re.compile(r",?\s*(U\.?S\.?A?\.?|United States)$", re.I)
# Chubb writes "USA (Delaware)"; Occidental writes Canadian provinces bare.
_USA_PAREN = re.compile(r"^(U\.?S\.?A?\.?|United States)\s*\(", re.I)
_GB_NATIONS = {"england & wales", "england and wales", "england", "scotland",
               "wales", "northern ireland"}
_CA_PROVINCES = {"alberta", "british columbia", "manitoba", "new brunswick",
                 "newfoundland and labrador", "nova scotia", "ontario",
                 "prince edward island", "quebec", "saskatchewan"}
_EXTRA_PLACES = {"nevis": "KN", "cayman": "KY", "prc": "CN",
                 "british virgin islands": "VG", "virgin islands (british)": "VG",
                 "korea": "KR",       # bare "Korea" in practice means the South
                 "columbia": "CO",    # a recurring filer typo for Colombia
                 "dubai": "AE", "macau": "MO", "macau sar": "MO",
                 "macao sar": "MO"}
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
    numbering is ambiguous (ex215 = 2.15 or 21.5), so the content decides.
    {"subsidiaries": [...], "form", "filing_date", "url"} or None."""
    for meta in annual_exhibit_candidates(cik):
        subs = parse_exhibit(_get_text(meta["url"]), registrant)
        if subs:
            return {"subsidiaries": subs, "form": meta["form"],
                    "filing_date": meta["filing_date"], "url": meta["url"]}
        log.info("candidate %s parsed to zero subsidiaries — trying the next",
                 meta["url"])
    return None


def _parse_first(candidates: list[dict]) -> tuple[list[dict], dict] | None:
    """The first candidate exhibit that parses to subsidiaries, with its meta."""
    for meta in candidates:
        subs = parse_exhibit(_get_text(meta["url"]))
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
