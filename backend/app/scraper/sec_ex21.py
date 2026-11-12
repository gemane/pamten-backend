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

from app import former_names
from app.scraper.edge_schema import READ_LAYOUT, READ_PROSE, READ_TABLE
from app.scraper.mapper import normalize_entity_name, strip_edgar_tag
from app.scraper.sec_edgar import SUBMISSIONS_URL, _cik10, _get, _get_text, _iso_date

log = logging.getLogger(__name__)

# 10-K first (domestic, Ex-21), then 20-F (foreign private issuers, Ex-8.1 —
# same content, different exhibit number in the rulebook).
_ANNUAL_FORMS = ("10-K", "20-F")
# The registration statement of an IPO carries the same Item 601(b)(21) list as
# Exhibit 21(.1) — S-1 for a US filer, F-1 for a foreign private issuer (whose
# Exhibit 8 there is counsel's tax opinion, `_subsidiary_document`), Form 10
# (10-12B / 10-12G) for a spin-off, which is registered without an offering. The first
# 10-K comes up to a year after the IPO: SpaceX filed its S-1 on 2026-05-20,
# listed on 2026-06-12 and in November 2026 had filed only 10-Qs, so the list
# read `no_annual_filing` (findings item 77). Read only while no annual filing
# exists; the first 10-K / 20-F replaces it.
_REGISTRATION_FORMS = ("S-1", "S-1/A", "F-1", "F-1/A", "10-12B", "10-12B/A", "10-12G", "10-12G/A")
#: Registration filings opened, newest first: an amendment usually re-files
#: only the exhibits that changed (SpaceX's two S-1/As carry no Exhibit 21.1).
REGISTRATION_MAX_FILINGS = 6


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


def filing_documents(cik: str, accession: str) -> list[tuple[str, str]] | None:
    """(file name, declared type) of every document of ONE filing, in the
    filer's order, from the filing's index page ``{accession}-index.htm`` —
    the page that says what each document IS ("EX-21.1", "EX-8.1", "EX-17").
    None when the page cannot be read or shows no document table.

    The folder's ``index.json`` lists file names only, and a name says nothing
    reliable: 281 of the 3,638 10-Ks of 2026 Q1–Q3 that carry an EX-21 named
    it so that no pattern finds it (findings item 81) — one filing agent
    numbers every exhibit ``ex_864720.htm``, Workiva writes
    ``len-20251130x10kxexh21.htm``, VIAVI ``…ex2x211xsubsidia.htm``, AMD
    ``exh_21-10kfy25.htm``. Like every Archives file the page is cached."""
    acc = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{accession}-index.htm"
    try:
        page = _get_text(url)
    except Exception as exc:  # noqa: BLE001 - the folder listing is the fallback
        log.warning("filing index %s unavailable, reading the folder listing: %s", url, exc)
        return None
    out: list[tuple[str, str]] = []
    for row in _INDEX_ROW.findall(page):
        cells = _INDEX_CELL.findall(row)
        if len(cells) < 4:
            continue
        # iXBRL documents link through the viewer: "/ix?doc=/Archives/…/x.htm"
        href = re.search(r'href="([^"]+)"', cells[2])
        kind = _html.unescape(re.sub(r"<[^>]+>", "", cells[3])).strip().upper()
        if href and kind:
            out.append((href.group(1).rsplit("/", 1)[-1], kind))
    return out or None


_INDEX_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_INDEX_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def exhibit_candidates(cik: str, form: str, accession: str, filed: str,
                       period: str = "") -> list[dict]:
    """Candidate subsidiary-exhibit files of ONE filing (annual or registration).

    The filing index declares each document's type, and that decides
    (findings item 81): the documents declared a subsidiary list come first,
    in the filer's order — EX-21.x, and EX-8/EX-8.1 in a 20-F, where its list
    lives (``_subsidiary_document``). A document declared anything else is no
    candidate, whatever its name: Unilever's ``a171-subsidiaryguarantor.htm``
    is an EX-17 guarantor list, AB InBev's ``dex215.htm`` exhibit 2.15.

    Only when the index page cannot be read do the file names of the folder
    listing decide, as before: a list, not one file, because numbering is
    ambiguous by name alone and only parsing tells the files apart; for a
    20-F the ex-8 patterns first, for a 10-K the ex-21 ones."""
    acc = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}"
    meta = {"form": form, "filing_date": _iso_date(filed), "accession": accession,
            "period": _iso_date(period) if period else None}
    documents = filing_documents(cik, accession)
    if documents is not None:
        declared = [(name, kind) for name, kind in documents
                    if name.lower().endswith((".htm", ".html")) and _subsidiary_document(kind, form)]
        if form.upper() in ("20-F", "20-F/A"):
            declared.sort(key=lambda d: not d[1].startswith("EX-8"))     # stable: filer's order kept
        return [{"url": f"{base}/{name}", "declared": kind, **meta} for name, kind in declared]
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
                out.append({"url": f"{base}/{name}", **meta})
    return out


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


#: a paragraph break inside a cell (<p>, <div>, <br>) — a private-use character,
#: so the whitespace collapsing below cannot swallow it
_PARA = "\ue000"


def _split_paragraph_row(cells: list[list[str]]) -> list[list[str]] | None:
    """Rows when a table row lists several companies, one paragraph each.

    HubSpot (2026) puts ten subsidiaries in ONE row: the name cell holds ten
    paragraphs, the jurisdiction cell ten more, paired by position. Joined, the
    row was one subsidiary "HubSpot Australia Pty LtdHubSpot BelgiumHubSpot
    Canada Inc." in "AustraliaBelgiumCanada", and 14 of its 15 were lost
    (NexPoint, Cellectis and Environmental Solutions draw the same layout).

    Split only when every non-empty cell has the same number of paragraphs and
    a later cell's paragraphs — the first excepted, which may be the header
    (Cellectis: "Name of Subsidiary", then two companies) — all read as
    jurisdictions. A header broken over two lines is the same shape, so the
    place test is what keeps Herc's "SUBSIDIARIES OF HERC HOLDINGS INC. / As of
    December 31, 2025" beside "JURISDICTION OF / INCORPORATION" one row. A name
    wrapped over two lines beside one jurisdiction (Chubb) is not split either.
    """
    filled = [c for c in cells if c]
    n = len(filled[0]) if filled else 0
    if n < 2 or len(filled) < 2 or any(len(c) != n for c in filled):
        return None
    if not any(all(jurisdiction_country(p) for p in c[1:]) for c in filled[1:]):
        return None
    return [[c[i] if c else "" for c in cells] for i in range(n)]


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
        self._paras: list[list[str]] = []           # each cell's paragraphs

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
            self._paras = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._cell_indent = _css_indent(attrs)
            span = re.match(r"\s*(\d+)", str(dict(attrs).get("colspan") or "1"))
            self._colspan = min(max(int(span.group(1)) if span else 1, 1), 50)
        elif self._cell is not None and not "".join(self._cell).strip():
            # a block or span wrapping the text can carry the indent instead
            self._cell_indent += _css_indent(attrs)
        elif self._cell is not None and tag in ("p", "div", "br"):
            self._cell.append(_PARA)
        elif self._table is None and tag in ("p", "div", "br", "hr"):
            self._flush_free()

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            raw = "".join(self._cell)
            # Zero-width characters first: Texas Roadhouse's exhibit has a
            # whole filler column of U+200B, which is truthy and was taken as
            # the jurisdiction of all 62 subsidiaries.
            raw = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", raw)
            # Paragraphs joined by a space: "LLC" + "Landmark" was glued into
            # one word, and one name ("…, LLCLandmark at West Place, LLC").
            paras = [p for p in (re.sub(r"\s+", " ", x).strip() for x in raw.split(_PARA)) if p]
            raw = raw.replace(_PARA, "")
            text = " ".join(paras)
            if text and self._row_indent is None:
                lead = len(raw) - len(raw.lstrip())
                self._row_indent = self._cell_indent + lead * _NBSP_PT
            self._row.append(text)
            self._paras.append(paras)
            start = self._spans[-1][1] if self._spans else 0
            self._spans.append((start, start + self._colspan))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                target = self._table if self._table is not None else self._orphan()
                for cells in _split_paragraph_row(self._paras) or [self._row]:
                    target.append(_Row(cells, self._row_indent or 0.0, self._spans))
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
# names nobody in particular and resets to the filer. Also with a lead-in:
# Interactive Brokers heads each table after the first "The following is a
# list of subsidiaries of IB Exchange Corp:", and "… and branches of".
_PARENT_HEADING = re.compile(
    r"^(?:(?:the\s+following\s+is|below\s+is)\s+a\s+list\s+of\s+(?:the\s+)?|list(?:ing)?\s+of\s+(?:the\s+)?)?"
    r"(?:consolidated |direct |indirect |wholly[- ]owned |significant )*"
    r"subsidiar(?:y|ies)(?:\s+and\s+branch(?:es)?)?\s+of\s+(.+?)\s*:?$", re.I)
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


#: A cell that only numbers, bullets or footnotes a row: "1.", "1.001", "·",
#: "II.", and a cell of footnote marks — Quaker Houghton's "*", "**", "+*"
#: stand in their own cell under the header "Name", whose span covers the mark
#: and the name; the mark was read as the name (findings item 106: 11 rows
#: named "+", the 136 "*" rows dropped as noise, the list empty).
_MARKER_CELL = re.compile(r"^(?:[\u00b7\u2022\u25aa\u25cf\u25cb\u25e6\-\u2013]|\d{1,3}(?:\.\d{1,4})*[.)]?|"
                          r"\(?[ivxlcIVXLC]{1,5}[.)]|[*+#\u2020\u2021\u00a7\u00b6]{1,4})$")


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


def _places_read(rows: list, header: dict) -> int:
    """Rows that, read with ``header``, give a name and a place."""
    return sum(1 for r in rows if _column(r, header, "name")
               and jurisdiction_country(_column(r, header, "jurisdiction")))


def _places_under(table: list, row: list, header: dict) -> int:
    """Rows below ``row`` that, read with ``header``, give a name and a place."""
    return _places_read(table[next(i for i, r in enumerate(table) if r is row) + 1:], header)


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


# "Northwest Natural Gas Company (dba NW Natural)": the name is before it
_ALIAS = re.compile(r"\s*\((?:dba|d/b/a|fka|f/k/a|formerly|aka|a/k/a)\b[^()]*\)", re.I)
_LEADING_THE = re.compile(r"^\s*the\s+", re.I)


def _same_company(a: str | None, b: str | None) -> bool:
    """Same filer under a different spelling: "Inter & Co, Inc." / "Inter&Co,
    Inc" / "NEWS CORPORATION" vs "News Corp". Legal forms and punctuation
    dropped — a name's letters are what survives every filer's typesetting,
    accents too: EDGAR names the filer "Almacenes Exito S.A.", its own list
    "Almacenes Éxito S.A."."""
    def key(x):
        # NFKD splits "É" into "E" and an accent, which the filter below drops;
        # normalize_entity_name drops EDGAR's tag
        plain = unicodedata.normalize("NFKD", x)
        return re.sub(r"[^a-z0-9]", "", normalize_entity_name(plain))
    return bool(a and b) and key(a) == key(b)


def _compact(name: str) -> str:
    """A name's letters and digits, accents and case dropped — legal forms
    kept: "Vía Artika S. A." is "Vía Artika S.A.", "PureCycle Technologies
    LLC" is not "PureCycle Technologies, Inc."."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", strip_edgar_tag(name)).casefold())


# One legal form, spelled either way.
_FORM_SPELLING = ((r"\blimited\b", "ltd"), (r"\bcorporation\b", "corp"), (r"\bincorporated\b", "inc"),
                  (r"\bcompany\b", "co"), (r"\bpublic limited company\b", "plc"))


def _form_plain(name: str) -> str:
    """The name casefolded, accents and EDGAR's state tag dropped, each legal
    form in one spelling: "Ltd." is "Limited"."""
    plain = unicodedata.normalize("NFKD", strip_edgar_tag(name)).casefold()
    for long, short in sorted(_FORM_SPELLING, key=lambda p: -len(p[0])):
        plain = re.sub(long, short, plain)
    return plain


def _form_key(name: str) -> str:
    """``_compact`` with each legal form in one spelling: "Ltd." is "Limited"."""
    return re.sub(r"[^a-z0-9]", "", _form_plain(name))


def _own_name(name: str) -> str:
    """A name without what is not the name: a leading "The", an alias in
    brackets ("(dba NW Natural)")."""
    return _LEADING_THE.sub("", _ALIAS.sub("", name or ""))


def legal_name_key(name: str) -> str:
    """One legal entity's name, however it is typeset: a leading "The", an
    alias in brackets, EDGAR's state tag, punctuation, accents and case
    dropped, the legal form kept and spelled one way — "The New York Times
    Company" and "NEW YORK TIMES CO" agree, "PBF Energy Limited" and "PBF
    Energy Inc." do not (`_is_filer`, `legal_identity`)."""
    return _form_key(_own_name(name))


def _unordered_key(name: str) -> tuple[str, ...]:
    """``legal_name_key``'s words, sorted — the same legal name with its words
    in another order. EDGAR conforms a filer named after a person surname
    first: "PRICE T ROWE GROUP INC" is "T. Rowe Price Group, Inc." (findings
    item 58). A possessive is no word of its own: "Domino's" stays one word,
    as `_form_key` keeps it."""
    plain = re.sub(r"['’]", "", _form_plain(_own_name(name)))
    return tuple(sorted(re.findall(r"[a-z0-9]+", plain)))


def _filer_names(registrant: str | list[str] | None) -> list[str]:
    """The filer's names: one, or several — the node's own, its other names,
    EDGAR's conformed name (`own_names`)."""
    if not registrant:
        return []
    return [n for n in ([registrant] if isinstance(registrant, str) else registrant) if n]


def own_names(entity: dict, edgar_name: str | None) -> list[str]:
    """The names a filer node goes by ITSELF, for the own-row rule: its name
    (GLEIF's or Wikidata's spelling), the register's other names, the dated
    former names of its own CIK or LEI (`app.former_names`), EDGAR's conformed
    name — never its ``aliases``. That bag merges Wikidata's aliases, which
    can name another legal entity: Wikidata lists "T. Rowe Price Associates,
    Inc." (the adviser, its own CIK) as an alias of the Group's item, and with
    it among the filer's names the adviser's row read as the filer's own and
    was dropped — 34 of the 35 rows of the Group's Exhibit 21 (findings item
    58, 2026-11-05). The former names of the same CIK are the filer's; they
    reach ``aliases`` too, but dated and by themselves only here."""
    names = [entity.get("name"), *(entity.get("other_names") or []),
             *former_names.names(former_names.of_node(entity)), edgar_name]
    return list(dict.fromkeys(n for n in names if n))


def _is_filer(name: str | None, registrant: str | list[str] | None) -> bool:
    """The row names the filer itself, under any of its names: NYT lists
    "The New York Times Company" (EDGAR: "NEW YORK TIMES CO") at the root of
    its tree, Occidental "Occidental Petroleum Corporation" (EDGAR: "… CORP
    /DE/") among its rows. A leading "The", an alias in brackets and EDGAR's
    state tag do not count; the legal form does, spelled either way — "PBF
    Energy Limited" (British Columbia) is a subsidiary of "PBF Energy Inc.",
    "The New York Times Company Pty Limited" one of NYT. A row with no legal
    form at all ("Chubb" at the root under "CHUBB LTD") is the filer when
    its letters are. The words may come in another order: EDGAR's "PRICE T
    ROWE GROUP INC" is the list's "T. Rowe Price Group, Inc." (findings item
    58) — before, that row was the filer's only through a Wikidata alias."""
    if not name:
        return False
    bare = not _LEGAL_NAME.search(strip_edgar_tag(name).strip())
    key, words = legal_name_key(name), _unordered_key(name)
    return any(key == legal_name_key(n)
               or (words and words == _unordered_key(n))
               or (bare and _same_company(_LEADING_THE.sub("", name), _LEADING_THE.sub("", n)))
               for n in _filer_names(registrant))


def _at_home(place: str | None, home: str | None) -> bool:
    """A row's place is the filer's own, or cannot tell otherwise. ``home``
    is the filer's jurisdiction code ("US-NY", "KY"; None when unknown). A
    namesake registered elsewhere is a subsidiary: Perfect Corp. (Cayman)
    lists a "Perfect Corp." in Japan and one in the US."""
    country = jurisdiction_country(place or "")
    if not home or not country:
        return True
    if country != home.split("-")[0]:
        return False
    sub = jurisdiction_subdivision(place or "")
    return not (sub and "-" in home and sub != home)


def _list_key(entry: dict) -> tuple[str, str]:
    """One list entry per name AND place: a list repeats a row across printed
    pages, but Lavoro lists "Agrointegral Andina S.A.S." in Colombia and in
    Ecuador — two entries, two nodes (the writer matches each in its country).
    A state counts where the list names one: Quaker Houghton lists "Quaker
    Chemical Corporation" in Pennsylvania and in Delaware (findings item 112)."""
    place = entry.get("jurisdiction") or ""
    return entry["name"].casefold(), jurisdiction_subdivision(place) or jurisdiction_country(place) or place.casefold()


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


def _assign_indent_parents(entries: list[dict], registrant: str | list[str] | None = None) -> int:
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
            e["parent"] = None if level == 0 and _is_filer(above, registrant) else above
            e["parent_basis"] = "indent"
            assigned += 1
        elif _is_filer(e["name"], registrant):
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
        # The first percent is the listed parent's only when it names no holder
        # of its own: "83.17% 16.82% (DHC Corporation)". Chubb's Colombia row
        # starts "46.3425521% (Vigilant Insurance Company) … 17.0140717% (Chubb
        # INA International Holdings Ltd.)" — Vigilant's share, which the parent
        # got as well, beside its own (Colombia 129 %, Chile 199 %; item 100).
        first = _FIRST_PERCENT.match(cell)
        named = first and _CO_OWNER.match(cell.lstrip())
        return (float(first.group(1)) if first and not named else None), co
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


# ── layouts read again since 2026-11-09 (findings item 82, the user's call) ──
# The names-only list was removed 2026-10-08 because it read combined 10-Ks'
# sibling lists as the filer's own; since then the filing index's declared type
# (item 81) says what a document IS and the own-row rule drops the filer's own
# names, so a list of names alone in a declared EX-21 / EX-8.1 is read again —
# with the place a lead sentence gives every name ("… are organized in the
# Commonwealth of Virginia", Shenandoah), none otherwise (Sirius XM's 18 names).
#: the sentence that gives every name below one place
_LEAD_PLACE = re.compile(
    r"\b(?:organi[sz]ed|incorporated|formed|existing|registered)\s+(?:in|under\s+the\s+laws\s+of)\s+"
    r"(?:the\s+)?(?:Commonwealth\s+of\s+|State\s+of\s+|Republic\s+of\s+)?"
    r"(?P<place>[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,3})")
#: lowercase words a company name may carry; any other lowercase word is prose
_NAME_SMALL_WORDS = frozenset((
    "of and de del della di da do dos das la le les du des der den und van von y e en für &amp; & dba d/b/a f/k/a "
    "na n.a. s.r.l. s.a. s.p.a. s.r.o. a.s. d.o.o. b.v. n.v. gmbh mbh co. kg ag ab oy as a/s aps sa sas srl spa llc "
    "lp llp plc ltd inc pty pte bhd sdn ltda limited company corp corporation incorporated"
).split())
#: "name, Place" / "name - Place": the place set off after the name — a layout
#: left unread on purpose (one reader for it guessed at headings), not a name
_PLACE_TAIL = re.compile(r"^(?P<name>.+?)\s*(?:,|\s[-–—]\s)\s*(?P<place>[A-Z][\w .'’-]{1,30})$")
#: a header or caption block of a list, never a name
_CAPTION_WORDS = re.compile(r"^(?:company|companies|name|names|entity|entities|subsidiary|subsidiaries|jurisdiction|state|"
                            r"country|incorporation(?:/organi[sz]ation)?|organi[sz]ation|(?:state|place|jurisdiction)\s+of\s+"
                            r"(?:incorporation|organi[sz]ation|formation)|incorporated\s+state|business\s+name)\s*:?$", re.I)
_DATE_CAPTION = re.compile(r"^\(?\s*(?:as\s+of|at|dated?)\s+(?:[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})\)?\.?$", re.I)
_STREET = re.compile(r"^\d{1,6}\s+\S.*\b(?:street|st\.|avenue|ave\.?|road|rd\.|drive|dr\.|parkway|boulevard|blvd\.?|suite|floor|lane|way|plaza)\b", re.I)
#: a numbered row: "1. Green Acquisition, Inc.", "1.XPACSponsor LLC"
_ROW_NUMBER = re.compile(r"^\d{1,3}[.)]\s*(?=[A-Za-z(])")      # not "84.51 LLC", a name that starts with a number
#: "Also Doing Business As:" — the trade names under it belong to the row above, they are no subsidiaries
_DBA = re.compile(r"^(?:also\s+)?(?:doing\s+business\s+as|d/?b/?a)\b", re.I)


def _looks_like_a_name(block: str, registrant: str | None) -> bool:
    """A text block that is a company name and nothing more: short, every
    word capitalised (or a small word a name may hold), no sentence, not a
    heading, not the filer, not a name with its place set off after it."""
    s = block.strip().rstrip(";:")
    if not 2 <= len(s) <= 110 or not re.search(r"[A-Za-z]", s):
        return False
    words = s.split()
    if len(words) > 10 or _NOISE.match(s) or _PARENT_HEADING.match(s) or re.match(r"^ex-?\d", s, re.I):
        return False
    if re.fullmatch(r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}", s) or not re.search(r"[A-Za-z]{2}", s) \
            or re.search(r"\b(?:incorporat|organi[sz]|jurisdiction|formed|domicil)\w*\s*:", s, re.I):
        return False                      # a date ("DEC. 31, 2025"), a number, a labelled place
    if _CAPTION_WORDS.match(s) or _DATE_CAPTION.match(s) or block.strip().endswith(":") or _STREET.match(s) \
            or re.search(r"\b(?:segment|division)$", s, re.I) or s.endswith("%") \
            or re.search(r"\b(?:business\s+)?name\s*:|\bstate\s*:", s, re.I) \
            or re.match(r"^(?:suite|floor|p\.?o\.? box)\s+\w", s, re.I) or re.search(r",\s*[A-Z][a-z]+\s+\d{5}(?:-\d{4})?$", s) \
            or (len(words) <= 4 and all(_CAPTION_WORDS.match(w) for w in words)):
        return False                      # a caption, an address, a label, a stake tail, "Company Name Jurisdiction"
    if len(words) <= 7 and jurisdiction_country(s.rstrip(":.")) is not None:
        return False                      # a place heading ("ALABAMA", "The Republic of the Marshall Islands") groups the names under it
    if re.search(r"\bsubsidiar(?:y|ies)\b", s, re.I):
        return False
    if s.endswith(".") and re.sub(r"[^a-z]", "", words[-1].lower()) not in _FORM_TAILS:
        return False                      # a sentence ends with a period; "Inc.", "N.A.", "S.A." do too
    for w in words:
        core = w.strip("(),;:\"“”'’")
        if not core:
            continue
        if core[0].islower() and core.lower() not in _NAME_SMALL_WORDS:
            return False
    if _same_company(s, registrant):
        return False
    m = _PAREN_JURISDICTION.match(s)
    if m and jurisdiction_country(m.group("jur")) is not None:
        return False                      # the paragraph list's shape, with a place
    m = _PLACE_TAIL.match(s)
    if m and jurisdiction_country(m.group("place")) is not None \
            and re.sub(r"[^a-z]", "", m.group("place").lower()) not in _FORM_TAILS:
        return False                      # "Sportradar AG, Switzerland": the place after the name — not "…, N.A."
    last = words[-1].strip("(),;:")
    if re.fullmatch(r"[A-Z]{2}", last) and last not in _FORM_TAILS_UPPER and jurisdiction_country(last) \
            and any(w != w.upper() for w in words[:-1]):
        return False                      # "Ameriprise Certificate Company DE": a state code glued to the name
    return True


#: legal-form abbreviations a name may end with — never a place
_FORM_TAILS = frozenset(("na sa ag se nv bv ab as kg lp pc lc llc ltd inc co corp plc gmbh sarl srl spa sas sasu sl slu "
                         "oy oyj aps asa hf ehf sro doo kft zrt nyrt spzoo ood eood ad ead eurl snc bvba cvba cv vof ltda "
                         "sapi scv uab sia ou ltd pte pty bhd sdn kk gk yk ag sa sagl scpa scarl").split())
_FORM_TAILS_UPPER = frozenset(x.upper() for x in _FORM_TAILS if len(x) == 2)


def _names_only_list(sequence: list, registrant: str | None) -> list[dict]:
    """Subsidiary names alone, one per block, in a document declared EX-21 /
    EX-8.1 — Sirius XM's 18, Shenandoah's 19 — with the place a lead sentence
    before the first name gives them all (``_LEAD_PLACE``), else none. At
    least three names, and from the first name on the document must be the
    list and little else — at most one block in ten that is not a name (a
    footer, a page number): a tax opinion or a cover letter declared EX-21 by
    mistake has sentences between its company names (findings item 82c)."""
    blocks = [str(item).strip() for kind, item in sequence if kind == "text" and str(item).strip()]
    names: list[tuple[str, str]] = []            # (block, the place that governs it)
    lead_place, first, headings, trade_names = None, None, 0, 0
    place, dba = None, False
    for i, b in enumerate(blocks):
        b = _ROW_NUMBER.sub("", b)
        if _DBA.match(b):
            dba, headings = True, headings + 1       # Kroger: "Also Doing Business As:" then "Food 4 Less", "Ralphs"
        elif _looks_like_a_name(b, registrant):
            if first is None:
                first = i
            if dba and not _glued_place(b):
                trade_names += 1                     # a trade name of the row above, until the next row with its place
                continue
            dba = False
            names.append((b, place or lead_place or ""))
        elif len(b.split()) <= 7 and jurisdiction_country(b.rstrip(":.")) is not None and not _CAPTION_WORDS.match(b):
            # a place heading groups the names under it (HCA's "ALABAMA", Movado's "Australia:")
            place, dba = b.rstrip(":."), False
            headings += 1
        elif first is None and (m := _LEAD_PLACE.search(b)) \
                and jurisdiction_country(lp := m.group("place").rstrip(".,;:")) is not None:
            lead_place = lp
        else:
            dba = False
    if len(names) < 3 or first is None \
            or (len(blocks) - first) - len(names) - headings - trade_names > 1 + len(names) // 10:
        return []
    # "Ashwoods Innovations Limited United Kingdom", "84.51 LLC [Ohio]": a list whose names
    # end with their place reads the place off every name that has one (most do, or none)
    tails = [_glued_place(n) for n, _p in names]
    glued = sum(1 for t in tails if t) >= 0.6 * len(names)
    out = []
    for (n, p), tail in zip(names, tails):
        if glued and tail:
            n, p = tail
        name, stake = _clean_name(n.rstrip(" -–—"))
        if not name:
            continue
        entry = {"name": name, "jurisdiction": p}
        if stake is not None:
            entry["stake_percent"] = stake
        out.append(entry)
    return out if len(out) >= 3 else []


def _glued_place(block: str) -> tuple[str, str] | None:
    """("Reddit Canada Limited", "Canada") for a name with its place glued to
    its end — the last one to three words, or "[Ohio]" in brackets — when what
    is left still reads as a name of two words or more; None otherwise."""
    m = re.match(r"^(?P<name>.+?)\s*\[(?P<place>[^\]]{2,40})\]\s*$", block)
    if m and jurisdiction_country(m.group("place")) is not None:
        return m.group("name").strip(), m.group("place").strip()
    words = block.split()
    for k in (3, 2, 1):
        if len(words) > k + 1:
            tail = " ".join(words[-k:])
            if tail.rstrip(".,").lower() not in _FORM_TAILS and jurisdiction_country(tail) is not None \
                    and (words[-k - 1][0].isupper() or re.sub(r"[^a-z]", "", words[-k - 1].lower()) in _FORM_TAILS):
                return " ".join(words[:-k]), tail
    return None


#: "•BNY Capital Markets Holdings, Inc. – State of Incorporation: New York":
#: a name, a dash, the place behind its label
_LABELLED_PLACE = re.compile(
    r"^[\s•·\-–]*(?P<name>.+?)\s+[–-]\s+(?:(?:state|country|place|jurisdiction)\s+of\s+)?"
    r"(?:incorporation|organi[sz]ation|formation|domicile)\s*:\s*(?P<place>.+?)\s*$", re.I)


def _place_token(tail: str) -> bool:
    """A place as a grid writes it: a jurisdiction that maps to a country, a
    two-letter US state code, "Federal" (a federal savings bank)."""
    tail = tail.strip()
    if not tail or re.sub(r"[^a-z]", "", tail.split()[-1].lower()) in _FORM_TAILS:
        return False                      # "KG", "Inc." end a name, never a place
    return jurisdiction_country(tail) is not None or bool(re.fullmatch(r"[A-Z]{2}", tail)) or tail.lower() == "federal"


def _labelled_place_list(sequence: list, registrant: str | None) -> list[dict]:
    """One subsidiary per block with its place beside the name, drawn without
    a <table> (findings item 82b). Two shapes: under a header block that names
    both columns ("Subsidiary Name Incorp State", Ameriprise) the place is the
    block's last word or words — "Ameriprise Certificate Company DE",
    "Ameriprise Bank, FSB Federal"; or the place follows its label after a
    dash ("… – State of Incorporation: New York", BNY Mellon's 23 rows),
    header or not. At least three rows."""
    blocks = [str(item).strip() for kind, item in sequence if kind == "text" and str(item).strip()]
    out: list[dict] = []
    header_seen = False
    for b in blocks:
        if len(b) <= 60 and len(b.split()) <= 7 and not b.endswith(".") and not _LEAD_PLACE.search(b) \
                and _H_NAME.search(b) and _H_JURISDICTION.search(b) and not _not_jurisdiction(b):
            header_seen = True                            # "Subsidiary Name Incorp State" — a caption, not a sentence
            continue
        name = place = None
        if m := _LABELLED_PLACE.match(b):
            if _place_token(m.group("place")):
                name, place = m.group("name"), m.group("place").strip()
        elif header_seen and not _place_token(b):          # a whole block that is a place is a heading, not a row
            words = b.split()
            if g := _glued_place(b):                      # "84.51 LLC [Ohio]"
                name, place = g
                words = []
            for k in (3, 2, 1):
                if len(words) > k and _place_token(" ".join(words[-k:])) and (
                        words[-k - 1][0].isupper() or re.sub(r"[^a-z]", "", words[-k - 1].lower()) in _FORM_TAILS):
                    name, place = " ".join(words[:-k]), " ".join(words[-k:])
                    break
        if name is None and header_seen and _looks_like_a_name(b, registrant):
            name, place = b, ""                          # a row of the grid whose place is no place ("… GmbH & Co. KG")
        if name:
            name = _ROW_NUMBER.sub("", name).rstrip(" -–—")
        if not name or not _looks_like_a_name(name, registrant):
            continue
        cleaned, stake = _clean_name(name)
        if not cleaned:
            continue
        entry = {"name": cleaned, "jurisdiction": place}
        if stake is not None:
            entry["stake_percent"] = stake
        out.append(entry)
    return out if len(out) >= 3 else []


#: "None." / "has no subsidiaries" / "… omitted … not a significant subsidiary":
#: the filer's statement that the list is empty, not a list the parser missed
_NO_SUBSIDIARIES = re.compile(
    r"^\s*(?:none|not applicable|n/?a)\b|"
    r"\b(?:has|have|had)\s+no\s+(?:significant\s+)?subsidiar|\bno\s+subsidiaries\b", re.I)


def states_no_subsidiaries(html: str) -> bool:
    """Whether a declared EX-21 / EX-8.1 that lists nothing SAYS so: ESS Tech
    and Ethos write "None." under the heading, with or without Regulation S-K's
    omission clause (findings item 82a). Only for a document that parses to
    no subsidiaries; a list that merely omits some ("certain subsidiaries …
    omitted", Ameriprise) is still a list."""
    parser = _TableTextParser()
    parser.feed(html)
    parser.close()
    blocks = [str(item).strip() for kind, item in parser.sequence if kind == "text" and str(item).strip()]
    return any(_NO_SUBSIDIARIES.search(b) for b in blocks)

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
    if len(name) < 2 or len(name) > 150 or _NOISE.match(name) or _is_filer(name, registrant) \
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


def parse_exhibit(html: str, registrant: str | list[str] | None = None,
                  form: str | None = None, home: str | None = None) -> list[dict]:
    """[{name, jurisdiction, stake_percent?, co_owners?, parent?, former_names?, aliases?,
    parent_basis?}] from an Ex-21/Ex-8.1 page. ``registrant`` is the filer's
    name, or a list of its names, so a heading or root row naming the filer
    itself is not taken for an intermediate parent, and a row naming the
    filer (``_is_filer``) where it is registered (``home``, ``_at_home``) is
    not a subsidiary. With no registrant the filer's own row stays
    (``registrants_part`` needs it).

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
    (``_grouped_list``, ``_paragraph_list``; in a document declared EX-8 or
    EX-21 also the country rows, a grid drawn without a table
    (``_labelled_place_list``) and names alone (``_names_only_list``, with the
    place a lead sentence gives them — findings item 82, read again since
    2026-11-09 under the declared type that keeps a combined filing's sibling
    list out). Not read: one subsidiary per line with its place in words
    ("Bluebottle Limited, a Hong Kong company") or set off after the name
    ("Sportradar AG, Switzerland") — those readers were removed 2026-10-08:
    they read parents stated in words not at all."""
    names = _filer_names(registrant)
    registrant = names[0] if names else None
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
                # By cell index, unless the header's grid reads more places:
                # the next page's grid need not be the first page's (BHP's
                # header cell spans grid columns 1-2, the rows below it on
                # later pages one column each), but where it is, the grid is
                # what found the column — Western Union's place sits behind a
                # spacer cell, and by index pages two and three read none.
                header = {**carried, "header_row": None, "spans": None}
                if _places_read(table, grid := {**carried, "header_row": None}) > _places_read(table, header):
                    header = grid
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
            # "State of Delaware" starts like the header word "State" but is a
            # place: HubSpot's APIHub, Inc. was dropped as a header row
            if _NOISE.match(name) or (_NOISE.match(jurisdiction)
                                      and jurisdiction_country(jurisdiction) is None):
                continue
            # a jurisdiction is short; a long second column means prose
            if len(jurisdiction) > 60 or len(name) < 2:
                continue
            name, inline_stake = _clean_name(re.sub(r"^\d{1,3}\.\s+(?=\S)", "", name))   # AIOS: "1. YD …"
            if not name:
                continue
            entry = {"name": name, "jurisdiction": jurisdiction,
                     # how surely the cells were read: under a header the
                     # filer wrote for THIS table, or by position (a carried
                     # header, the first-two-cells heuristic)
                     "read_from": READ_LAYOUT if header is None or inherited else READ_TABLE,
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
                elif _compact(holder) != _compact(entry["name"]):
                    entry["parent"], entry["parent_basis"] = holder, "column"
            # a row is not its own parent — but a namesake with another legal
            # form is another company: "Covestor Limited" under "… subsidiaries
            # of Covestor, Inc."
            elif parent and _compact(parent) != _compact(entry["name"]):
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
        readers = [(_grouped_list, READ_LAYOUT), (_paragraph_list, READ_PROSE)]
        if declared:
            # only in a document the filing index declares a subsidiary list
            # (item 81): the block grid and the names-only list (item 82)
            readers += [(_country_rows_list, READ_LAYOUT), (_labelled_place_list, READ_LAYOUT),
                        (_names_only_list, READ_LAYOUT)]
        for reader, grade in readers:
            found = reader(seq, registrant)
            if not found:
                continue
            for entry in found:
                if _list_key(entry) not in seen:
                    seen.add(_list_key(entry))
                    out.append({**entry, "_indent": 0.0, "read_from": grade})
            break
    out = _lift_name_notes(out, names)
    # A parent named as the list names it: "Vía Artika S. A." is the listed
    # "Vía Artika S.A."; a holder named like the filer but with another legal
    # form that the list carries is that listed company, not the filer.
    listed = {_compact(e["name"]): e["name"] for e in out}

    def as_listed(who: str, heading: bool = False) -> str | None:
        if _compact(who) in listed:
            return listed[_compact(who)]
        # A parent column names group companies: "Seaspan Holdco III" is the
        # one listed "Seaspan Holdco III Ltd.", the legal form aside. A
        # heading often names the filer by another name, so there only the
        # same legal form spelled another way counts — "ADS-TEC ENERGY PLC"
        # is not the listed "ads-tec Energy Inc.", "Subsidiaries of WISeKey"
        # not the listed "WISeKey SA".
        same = {e["name"] for e in out if (_form_key(e["name"]) == _form_key(who) if heading
                                           else _same_company(e["name"], who))}
        return same.pop() if len(same) == 1 else None

    for e in out:
        holder = e.pop("_holder", None)
        if holder and (hit := as_listed(holder)):
            e["parent"] = hit
        elif e.get("parent") and (hit := as_listed(e["parent"], e.get("parent_basis") == "heading")) \
                and hit != e["name"]:
            e["parent"] = hit
        elif e.get("parent_basis") == "heading":
            # A heading naming a company the list does not carry names the
            # filer under another name — "THE PROGRESSIVE CORPORATION",
            # Altaba's "Yahoo! Inc.", "HAFNIA LIMITED – EXHIBIT 8.", "the
            # VIEs" (1,166 rows of 1,196 across 1,889 filers, 2026-10-08).
            # The row is the filer's; a parent the writer cannot find would
            # cost it its stake (Hafnia's 91).
            del e["parent"], e["parent_basis"]
    # The indentation tree is exhibit-wide (a page break must not cut it) and
    # more specific than a section heading, so it wins where both apply.
    _assign_indent_parents(out, names)
    for entry in out:
        del entry["_indent"]
    # The filer's own row is kept until here, so the tree sees its root; no
    # company is its own subsidiary. A row under it hangs directly off the
    # filer, as the tree draws it (NYT's nine, 2026-10-29).
    filer = [_is_filer(e["name"], names) and _at_home(e.get("jurisdiction"), home) for e in out]
    own = {e["name"] for e, f in zip(out, filer) if f}
    out = [e for e, f in zip(out, filer) if not f]
    kept = {e["name"] for e in out}
    for e in out:
        if e.get("parent") in own - kept:
            del e["parent"]
    return out


# What a row writes after a company's name that is no part of it (findings item
# 80): another name it bears or bore, the filing's own shorthand, a stake note,
# a tax id. Measured over the 4,384 cached exhibits of 7k filings (208,442
# rows): 952 bracketed f/k/a / d/b/a notes, 363 defined terms, 159 bracketed
# stakes, 31 "owned by …" notes, 10 tax ids, 5 note rows.
# "(F/K/A RCC Real Estate, Inc.)", "(fka: SF Kino SA)", "(formerly known as
# NYMT Loan Trust I)", "(d/b/a Ameren Illinois)" — one bracket inside is a name's:
# "(fka AMC (UK) Acquisition Limited)"
_OTHER_NAME_NOTE = re.compile(
    r"\s*\(\s*(?:also\s+)?(?P<kind>f/?k/?a|formerly(?:\s+known\s+as)?|d/?b/?a|doing\s+business\s+as|a/?k/?a|"
    r"also\s+known\s+as)\b[\s:]*(?P<other>[^()]*(?:\([^()]*\)[^()]*)*)\)", re.I)
# "Old AGI, Inc. f/k/a AIRO Group, Inc.", "Fin Technologies, Inc. d/b/a MANTL"
_OTHER_NAME_TAIL = re.compile(
    r"\s+(?P<kind>f/k/a|d/b/a|formerly\s+known\s+as|doing\s+business\s+as|a/k/a)\s+(?P<other>\S.*)$", re.I)
_FORMER_KIND = re.compile(r"^(?:f|formerly)", re.I)
_MORE_NAMES = re.compile(r"\s+(?:and|&|;)\s+(f/?k/?a|d/?b/?a|a/?k/?a)\s+", re.I)
# "Argan, Inc. (“AGX”)", 'ACM Research (Chengdu), Inc. ("ACM Chengdu")': the
# filing's shorthand for the company, no name it is registered or known by
_DEFINED_TERM = re.compile(r"\s*\(\s*(?:the\s+)?[“\"‘'][^”\"’'()]{1,60}[”\"’']\s*\)", re.I)
# "(37.4% owned)", "(50% interest)", "(95% owned by Registrant)", "(100% owned by
# Roadrunner Gas Transmission Holdings, LLC)"
_STAKE_NOTE = re.compile(
    r"\s*\(\s*(?P<pct>\d{1,3}(?:\.\d+)?)\s*%\s*(?:owned|interest|held|ownership|equity)?"
    r"(?:\s+(?:owned\s+)?by\s+(?P<by>[^()]+?))?\s*\)\s*$", re.I)
# "(majority owned by unrelated third party)": a note, no stake of the filer's
_OWNED_NOTE = re.compile(r"\s*\((?:majority|minority|wholly|partially)[- ]owned\b[^()]*\)\s*$", re.I)
# "LucidDx Labs Inc. (87-41661458)": a US employer id
_TAX_ID = re.compile(r"\s*\(\d{2}-\d{7,8}\)")
# "Wholly-Owned Subsidiary of Lucid Diagnostics Inc.": a note row, no company
_NOTE_ROW = re.compile(r"^(?:an?\s+)?(?:wholly|majority|minority|partially)[- ]owned\s+subsidiar(?:y|ies)\s+of\b",
                       re.I)


# "Quaker Chemical Corporation (DE)", "Quaker Houghton Brazil Holdings Ltda.
# (Brazil)": a place in brackets after the legal form (findings item 112)
_PLACE_NOTE = re.compile(r"^(?P<name>.+?)\s*\((?P<place>[^()]{2,40})\)$")
#: the last word of a legal name that ends with its form, spelled out or abbreviated
_LONG_FORMS = frozenset(("corporation", "company", "limited", "incorporated", "partnership", "ltda"))


def _ends_with_form(name: str) -> bool:
    last = re.sub(r"[^a-z]", "", name.split()[-1].lower()) if name.split() else ""
    return last in _FORM_TAILS or last in _LONG_FORMS


def _place_note(name: str, jurisdiction: str | None) -> tuple[str, str] | None:
    """(name, jurisdiction) for a row whose name ends with its legal form and
    then a place in brackets, or None (findings item 112). The bracket is
    where the company is registered, written into the name cell: Quaker
    Houghton lists "Quaker Chemical Corporation (PA)" and "… (DE)" under the
    jurisdiction "United States". Kept in the name, the Delaware company —
    GLEIF's 549300PJQ1R3WCWCPS54 — was written as a node of its own beside
    GLEIF's, and the Pennsylvania one, the filer itself, as a subsidiary of
    itself. Only where the place agrees with the jurisdiction column's country
    (or the row has no other place): "Foo GmbH (UK)" under Germany is left as
    filed. The bracket becomes the jurisdiction when it is the finer one ("DE"
    under "United States" is Delaware; `jurisdiction_country` reads a bare
    state code as a US state, not as Germany). A bracket inside a name —
    "Alibaba Information Port (Wulanchabu) Co., Ltd." — is no note: the
    form comes after it."""
    m = _PLACE_NOTE.match(name.strip())
    if not m or not _ends_with_form(m.group("name")):
        return None
    place = m.group("place").strip()
    country = jurisdiction_country(place)
    if country is None or (jurisdiction and jurisdiction_country(jurisdiction) not in (None, country)):
        return None
    finer = jurisdiction_subdivision(place) and not jurisdiction_subdivision(jurisdiction or "")
    return m.group("name").strip(" ,"), place if finer or not jurisdiction else jurisdiction


def _lift_name_notes(entries: list[dict], registrant: str | list[str] | None) -> list[dict]:
    """Each row's name without the notes written after it (findings item 80):
    a former name ("f/k/a", "formerly") goes to ``former_names``, a trade name
    ("d/b/a", "a/k/a") to ``aliases``; a stake note gives the row's stake when
    no column did, and the holder it names becomes the row's parent the way a
    parent column would ("owned by Registrant" is the filer: no parent); the
    filing's defined terms ("“AGX”") and a tax id go. A note row ("Wholly-Owned
    Subsidiary of …") is no company. Rows that become one name are one row."""
    out: list[dict] = []
    seen: set = set()
    for e in entries:
        name = e["name"]
        if _NOTE_ROW.match(name):
            continue
        former, aliases = [], []

        def other(m):
            # "(d/b/a ArcBest and d/b/a U-Pack)": two names
            first, *more = _MORE_NAMES.split(m.group("other"))
            kinds = [m.group("kind"), *more[0::2]]
            for kind, text in zip(kinds, [first, *more[1::2]]):
                if kept := _clean_name(text.strip(" ;,:"))[0]:
                    (former if _FORMER_KIND.match(kind) else aliases).append(kept)
            return ""
        name = _OTHER_NAME_NOTE.sub(other, name)
        name = _OTHER_NAME_TAIL.sub(other, name)
        name = _TAX_ID.sub("", _DEFINED_TERM.sub("", name))
        name = _OWNED_NOTE.sub("", name)
        entry = dict(e)
        if (m := _STAKE_NOTE.search(name)) and "%" in (m.group("by") or ""):
            # "(31.05% owned by Kadant Lamort SAS and 68.95% owned by Kadant
            # Inc.)": two holders — neither is THE parent, the layout decides
            name, m = name[:m.start()], None
        if m:
            name = name[:m.start()]
            if entry.get("stake_percent") is None:
                entry["stake_percent"] = float(m.group("pct"))
            by = (m.group("by") or "").strip()
            if by and not entry.get("parent") and not entry.get("parent_basis"):
                if _GENERIC_PARENT.match(by) or _is_filer(by, registrant):
                    entry["parent_basis"] = "column"          # the filer: directly under it
                else:
                    entry["parent"], entry["parent_basis"] = by, "column"
        while (cleaned := _clean_name(name.strip(" ,;"))[0]) != name:
            name = cleaned          # "NL Industries, Inc. (2), (3),": one mark at a time
        if place := _place_note(name, entry.get("jurisdiction")):
            name, entry["jurisdiction"] = place
        if len(name) < 2:
            continue
        if name != e["name"]:
            entry["name"] = name
        if former:
            entry["former_names"] = former
        if aliases:
            entry["aliases"] = aliases
        if _list_key(entry) in seen:
            continue
        seen.add(_list_key(entry))
        out.append(entry)
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
                 "democratic republic of the congo": "CD", "democratic republic of congo": "CD",
                 # official names on 20-F covers (Televisa, Embraer)
                 "united mexican states": "MX", "federative republic of brazil": "BR"}
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


# ── one filing, several registrants ──────────────────────────────────────────
# AEP files one 10-K for itself and seven subsidiaries, Duke Energy for eight,
# Eversource for four: one Exhibit 21, the group's, reached from each
# co-registrant's CIK. Read as AEP Texas's own list, it made AEP Texas's
# parent and siblings its subsidiaries. 40 of 1,829 annual filings measured
# (2026-10-08) are combined; the SGML header names every registrant.
_FILER = re.compile(r"<CONFORMED-NAME>([^\n<]*)\s*<CIK>(\d+)")


def co_registrants(cik: str, accession: str) -> list[tuple[str, str]]:
    """(cik, name) of every registrant of one filing, from its SGML header —
    one pair for a filing of one company; [] when the header cannot be read."""
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}.hdr.sgml"
    try:
        text = _get_text(url) or ""
    except Exception as exc:  # noqa: BLE001 - without it, the filing reads as one filer's
        log.info("filing header %s unreadable: %s", url, exc)
        return []
    return [(str(int(c)), name.strip()) for name, c in _FILER.findall(text)]


def _same_name(a: str, b: str) -> bool:
    """One company's name, the legal form spelled either way — never another
    legal form: a REIT and its operating partnership are "Hudson Pacific
    Properties, Inc." and "…, L.P.", and both file the 10-K."""
    return _form_key(_ALIAS.sub("", a)) == _form_key(_ALIAS.sub("", b))


def _words(text: str) -> list[str]:
    """A text's words in one spelling for a name match: legal forms one way
    (`_form_plain`), "&" as "and", dotted letters joined — "L.P." is "LP"."""
    words = re.findall(r"[a-z0-9]+", _form_plain(text.replace("&", " and ")))
    out: list[str] = []
    run: list[str] = []
    for w in words + [""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if run:
            out.append("".join(run))
            run = []
        if w:
            out.append(w)
    return out


def _says(words: list[str], name: str) -> bool:
    """Whether ``words`` carry ``name`` whole — its words in a row, legal form
    included, so "Kilroy Realty Corporation" never reads as "Kilroy Realty,
    L.P.". A name without a legal form is too loose to tell a registrant
    from its partnership and never counts."""
    if not _LEGAL_NAME.search(strip_edgar_tag(_own_name(name)).strip()):
        return False
    want = _words(_own_name(name))
    n = len(want)
    return bool(n) and any(words[i:i + n] == want for i in range(len(words) - n + 1))


def _heading(html: str, form: str | None) -> list[str] | None:
    """The words a subsidiary list opens with, up to its first row — its
    SGML description and title ("SUBSIDIARIES - COMMONWEALTH EDISON COMPANY",
    "Exhibit 21.2 Subsidiaries of VICI Properties L.P."). None for a
    document that is no list."""
    full = parse_exhibit(html, None, form)
    if not full:
        return None
    text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", html)))
    first = text.find(full[0]["name"])
    return _words(text[:first if 0 <= first < 1500 else 600])


def _lists_one_of(meta: dict, names: list[str]) -> bool:
    """Whether the candidate's list carries the filer as one of its rows."""
    return any(_same_name(e["name"], n) for e in parse_exhibit(meta["html"], None, meta.get("form"))
               for n in names)


def own_list_first(candidates: list[dict], cik: str,
                   registrant: str | list[str] | None = None) -> list[dict]:
    """A filing's candidate lists, the filer's OWN first (findings item 78).

    A 10-K several registrants file together can carry one list per
    registrant — Exelon's eight (Exhibits 21.1–21.8, "SUBSIDIARIES -
    COMMONWEALTH EDISON COMPANY" …), a REIT's and its operating
    partnership's ("Subsidiaries of VICI Properties Inc." / "… L.P.") — and
    the first that parsed was everybody's: the partnership read the REIT's
    list and kept, of it, what the list draws under its own row (mostly
    nothing); SL Green's partnership, filing its own 10-K with both lists,
    read the REIT's whole list as its own. Measured 2026-11-08 over the 6,360
    10-Ks of 2026: 43 carry several lists, every one a list per registrant,
    named in its heading. So: a list whose heading names the filer (any of
    its names, or its name in the filing's header) comes first; one whose
    heading names another registrant of the filing is that one's and is
    dropped — unless the filer is a row of it (the group's list, whose branch
    under the filer `registrants_part` reads); the rest keep their order. A
    list its heading names as the filer's is marked ``own`` and is not
    re-read as another's by `registrants_part`, though the filer is a row of
    it: "BRIXMOR OPERATING PARTNERSHIP LP AND SUBSIDIARIES" opens with the
    partnership itself. Each candidate is fetched once here
    (``meta["html"]``) and not again by the caller — one candidate too, as
    its heading decides whether it is the filer's own."""
    if not candidates:
        return candidates
    me = str(int(cik))
    accession = candidates[0].get("accession")
    filers = co_registrants(cik, accession) if accession else []
    mine = _filer_names(registrant) + [n for c, n in filers if c == me]
    others = [n for c, n in filers if c != me and not any(_same_name(n, m) for m in mine)]
    own, rest = [], []
    for meta in candidates:
        if "html" not in meta:
            meta["html"] = _get_text(meta["url"])
        words = _heading(meta["html"], meta.get("form"))
        if words is None:
            rest.append(meta)
        elif any(_says(words, n) for n in mine):
            meta["own"] = True          # its heading says so: never read as another's (Brixmor's OP)
            own.append(meta)
        elif any(_says(words, n) for n in others) and not _lists_one_of(meta, mine):
            # another registrant's own list — the group's, where the filer is a
            # row, stays: `registrants_part` reads the filer's branch of it
            log.info("candidate %s is another registrant's list — skipped", meta["url"])
        else:
            rest.append(meta)
    return own + rest


def registrants_part(html: str, form: str | None, subs: list[dict], filers: list[tuple[str, str]],
                     cik: str, registrant: str | list[str] | None = None) -> tuple[list[dict], str | None]:
    """(the part of a combined filing's list that is ``cik``'s own, whose
    list it is — None for the filer's own).

    A list that names the filer among its rows is not the filer's own: no
    company is its own subsidiary. When another registrant of the filing is
    absent from it, the list is that one's (Eversource's, with NSTAR Electric
    a row in it), and the filer keeps only the branch the list draws under
    its own row — with no branch, nothing. Otherwise (every registrant
    listed, or the filer not) the list is read as before."""
    me = str(int(cik))
    if len(filers) < 2:
        return subs, None                          # one company's filing: no second parse
    full = parse_exhibit(html, None, form)        # the filer's own row kept
    names = [n for c, n in filers if c == me] + _filer_names(registrant)
    row = next((e["name"] for e in full if any(_same_name(e["name"], n) for n in names)), None)
    owners = [n for c, n in filers if c != me and not any(_same_name(e["name"], n) for e in full)]
    if not row or not owners:
        return subs, None
    branch: set[str] = set()
    grew = True
    while grew:
        grew = False
        for e in full:
            if e["name"] not in branch and e.get("parent") in branch | {row}:
                branch.add(e["name"])
                grew = True
    out = []
    for e in full:
        if e["name"] in branch:
            e = dict(e)
            if e.get("parent") == row:
                del e["parent"]                    # directly under the filer, as the tree draws it
            out.append(e)
    return out, owners[0]


def fetch_subsidiaries(cik: str, registrant: str | list[str] | None = None,
                       home: str | None = None) -> dict | None:
    """The latest annual filing's subsidiary list for a CIK, with provenance.

    Tries each candidate exhibit until one parses to subsidiaries — filename
    numbering is ambiguous (ex215 = 2.15 or 21.5), so the content decides —
    then, for a 20-F, the note of the main document its exhibit index points
    to (``note_in_main_document``).
    {"subsidiaries": [...], "form", "filing_date", "url"} or None."""
    filings = annual_filings(cik)
    if not filings:
        return list_from_registration(cik, registrant, home)
    empty: list[tuple[dict, str]] = []            # the declared lists that parsed to nothing
    for meta in own_list_first(exhibit_candidates(cik, *filings[0]), cik, registrant):
        html = meta.get("html") or _get_text(meta["url"])
        subs = parse_exhibit(html, registrant, meta["form"], home)
        if subs:
            subs, group_of = (subs, None) if meta.get("own") else registrants_part(
                html, meta["form"], subs, co_registrants(cik, meta["accession"]), cik, registrant)
            return {"subsidiaries": subs, "form": meta["form"], "filing_date": meta["filing_date"],
                    "url": meta["url"], **({"group_of": group_of} if group_of else {})}
        log.info("candidate %s parsed to zero subsidiaries — trying the next",
                 meta["url"])
        if (kind := declared_type(html)) and _subsidiary_document(kind, meta["form"]):
            empty.append((meta, html))
    if found := list_from_main_document(cik, filings[0], registrant, home):
        return found
    # The filer's own word that there is nothing to list (findings item 82a):
    # ESS Tech's and Ethos' Exhibit 21.1 read "None." — a statement, not a
    # missing exhibit, and the gate closes on it like on a list.
    for meta, html in empty:
        if states_no_subsidiaries(html):
            return {"subsidiaries": [], "none_stated": True, "form": meta["form"],
                    "filing_date": meta["filing_date"], "url": meta["url"]}
    return None


def registration_filings(cik: str) -> list[tuple[str, str, str, str]]:
    """(form, accession, filing date, "") of the S-1 / F-1 / Form 10
    registration statements and their amendments, newest first."""
    out = [(f, a, d, "") for f, a, d, _r, _n in _all_filings(cik) if f in _REGISTRATION_FORMS]
    out.sort(key=lambda r: r[2], reverse=True)
    return out


def list_from_registration(cik: str, registrant: str | list[str] | None = None,
                           home: str | None = None) -> dict | None:
    """The subsidiary list of the newest S-1 / F-1 / Form 10 that carries one, for a
    company with no 10-K or 20-F yet (findings item 77). Dated by that filing:
    the list describes the group as it was registered, and the first annual
    filing's list replaces it (its exhibit URL opens the runner's gate)."""
    for form, accession, filed, _period in registration_filings(cik)[:REGISTRATION_MAX_FILINGS]:
        for meta in own_list_first(exhibit_candidates(cik, form, accession, filed), cik, registrant):
            html = meta.get("html") or _get_text(meta["url"])
            subs = parse_exhibit(html, registrant, form, home)
            if subs:
                subs, group_of = (subs, None) if meta.get("own") else registrants_part(
                    html, form, subs, co_registrants(cik, accession), cik, registrant)
                return {"subsidiaries": subs, "form": form, "filing_date": meta["filing_date"],
                        "url": meta["url"], "exhibit": "21", "registration": True,
                        **({"group_of": group_of} if group_of else {})}
            log.info("candidate %s parsed to zero subsidiaries — trying the next", meta["url"])
    return None


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


def list_from_main_document(cik: str, filing: tuple, registrant: str | list[str] | None = None,
                            home: str | None = None) -> dict | None:
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
        return note_in_main_document(url, doc, n, form, filed, registrant, home)
    return list_from_earlier_filing(cik, entry, text, registrant, home=home,
                                    confirmed_by=url, confirmed_on=_iso_date(filed))


def note_in_main_document(url: str, doc: str, n: int, form: str, filed: str,
                          registrant: str | list[str] | None = None, home: str | None = None) -> dict | None:
    """The list in note ``n`` of the main document: found by its heading (the
    LAST one — a contents page comes first), read up to the next note with the
    exhibit parser, and kept only if it passes the note gate."""
    heads = list(_note_heading(n).finditer(doc))
    if not heads:
        log.info("%s: note %d named in the exhibit index, heading not found", url, n)
        return None
    start = heads[-1].start()
    nxt = _note_heading(n + 1).search(doc, start)
    subs = parse_exhibit(doc[start:nxt.start() if nxt else len(doc)], registrant, home=home)
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


def list_from_earlier_filing(cik: str, entry: str, text: str, registrant: str | list[str] | None = None,
                             home: str | None = None,
                             confirmed_by: str | None = None,
                             confirmed_on: str | None = None) -> dict | None:
    """The list a 20-F incorporates by reference: the exhibit of the earlier
    filing it names. Dated by THAT filing — the list describes the group as it
    was then (the user's call, 2026-10-07) — with the current 20-F that
    re-affirms it as ``confirmed_by`` / ``confirmed_on``."""
    ref = earlier_filing_reference(entry, text)
    exhibit_21 = (ref["exhibit"] or "").startswith("21")
    for form, accession, filed in resolve_reference(cik, ref)[:4]:
        for meta in own_list_first(exhibit_candidates(cik, "10-K" if exhibit_21 else "20-F", accession, filed),
                                   cik, registrant):
            subs = parse_exhibit(meta.get("html") or _get_text(meta["url"]), registrant, form, home)
            if subs:
                return {"subsidiaries": subs, "form": form, "filing_date": _iso_date(filed),
                        "url": meta["url"], "exhibit": "21" if exhibit_21 else "8.1",
                        "confirmed_by": confirmed_by, "confirmed_on": confirmed_on}
    return None


def _parse_first(candidates: list[dict], cik: str | None = None) -> tuple[list[dict], dict] | None:
    """The first candidate exhibit that parses to subsidiaries, with its meta
    — of a combined filing, ``cik``'s own part (``registrants_part``)."""
    for meta in (own_list_first(candidates, cik) if cik else candidates):
        html = meta.get("html") or _get_text(meta["url"])
        subs = parse_exhibit(html, form=meta["form"])
        if subs:
            if cik and not meta.get("own"):
                subs, _ = registrants_part(html, meta["form"], subs, co_registrants(cik, meta["accession"]), cik)
            return subs, meta
        log.info("candidate %s parsed to zero subsidiaries — trying the next", meta["url"])
    return None


def fetch_subsidiary_history(cik: str, max_filings: int = HISTORY_MAX_FILINGS) -> list[dict]:
    """The subsidiary lists of the company's annual filings, newest first, then
    the registration statement's (S-1 / F-1 / Form 10) filed before the oldest
    of them — the list the company registered with, older than its first
    annual report (findings item 79).

    [{"as_of", "filing_date", "form", "url", "names": {normalised names}}] —
    ``as_of`` is the fiscal year-end the list describes (the filing date when
    EDGAR gives no report date, and for a registration statement, which
    describes the group as registered); ``names`` is None for a filing whose
    exhibit could not be read (an old plain-text one, a missing exhibit).
    ``earliest_listing`` treats that as a gap, so a year we cannot read never
    extends a subsidiary's history.

    The registration statements come after the annual lists in the walk, so a
    subsidiary the first 10-K names and the S-1 named too is dated by the S-1
    (Tesla's S-1 of 2010-06, its first 10-K's year-end 2010-12 — on dev 24 of
    the 59 Exhibit 21 filers registered after 2001 with such a list). Until
    2026-11-09 the walk stopped at the first annual filing, and the registration
    list served only a company with no annual filing yet (item 77). Only those
    filed BEFORE the oldest annual filing read: a later S-1 (a follow-on
    offering) is not older than the annual lists and would re-order the run.
    Newest first, as the annual ones, at most ``REGISTRATION_MAX_FILINGS`` opened
    (an amendment often re-files only the exhibits that changed: Tesla's five
    S-1/As of June 2010 carry no list, the S-1/A of 2010-05-27 does). A
    registration filing without a readable list is therefore NOT a gap — the
    list it stands on is the earlier filing's — and is left out, where an
    annual filing whose exhibit cannot be read is a year that says nothing and
    ends the run."""
    from app.scraper.mapper import normalize_entity_name
    out: list[dict] = []

    def read(form: str, accession: str, filed: str, period: str, as_of: str, gap_if_unreadable: bool) -> None:
        try:
            got = _parse_first(exhibit_candidates(cik, form, accession, filed, period), cik)
        except Exception as exc:  # noqa: BLE001 - one bad year is a gap, not a failure
            log.warning("filing %s: exhibit unreadable: %s", accession, exc)
            got = None
        if got:
            subs, meta = got
            names = {normalize_entity_name(sub["name"]) or sub["name"].lower() for sub in subs}
            out.append({"as_of": as_of, "filing_date": meta["filing_date"], "form": form,
                        "url": meta["url"], "names": names})
        elif gap_if_unreadable:
            out.append({"as_of": as_of, "filing_date": _iso_date(filed), "form": form,
                        "url": None, "names": None})

    annual = annual_filings(cik, include_older=True)[:max_filings]
    for form, accession, filed, period in annual:
        read(form, accession, filed, period, _iso_date(period) if period else _iso_date(filed), True)
    oldest_annual = min((filed for _f, _a, filed, _p in annual), default=None)
    if oldest_annual:
        older = [r for r in registration_filings(cik) if r[2] < oldest_annual]
        for form, accession, filed, _period in older[:REGISTRATION_MAX_FILINGS]:
            read(form, accession, filed, "", _iso_date(filed), False)
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
