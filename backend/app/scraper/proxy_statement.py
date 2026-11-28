"""
SEC EDGAR DEF 14A (proxy statement) scraper — POC.

Data source:  https://www.sec.gov
Manual lookup:
  https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=DEF+14A&CIK=<cik>

Endpoints used:
  The company is resolved like every SEC scrape: by the CIK the caller holds,
  else by sec_lookup.search_company (tickers file, then EDGAR's indexes).
  GET https://data.sec.gov/submissions/CIK<10-digit-cik>.json
    — filing index to find the most recent DEF 14A accession number
  GET https://www.sec.gov/Archives/edgar/data/<CIK>/<accession>/<primary-doc>.htm
    — HTML of the proxy statement, parsed with BeautifulSoup

Fields returned:
  The "Security Ownership of Certain Beneficial Owners and Management" table,
  read as a grid (findings item 47) — each owner's voting power, from:
    • the table's total-voting-power column ("Total Voting Power", "Percentage
      of Aggregate Voting Power"), or
    • its percent-of-class column, but only when the company has ONE class of
      voting stock: Vanguard's 11.68 % of Ford's common stock is not 11.68 %
      of Ford's votes (the Class B stock carries 40 %).
  A multi-class filing without a voting-power column (Ford, Nike, Comcast)
  states nobody's voting power, and the reader returns none.
  "*" is the proxy's "less than 1 %": no number (`below_one_percent`).

  This data supplements Form 3/4 specifically for companies with dual/multi-class
  share structures where voting power differs from economic ownership.

Rate limits:
  Every request goes through sec_client._response (findings item 42): the pooled
  client and its User-Agent, the 0.12 s spacing, one retry on a stall or a
  429/503, the Archives cache, and the "request lost" log (findings item 45).
  Docs: https://www.sec.gov/os/accessing-edgar-data

Data licence:
  US federal government works — public domain (17 U.S.C. § 105).

How to verify:
  1. Download the DEF 14A filing manually from EDGAR for the target company.
  2. Find the "Security Ownership of Certain Beneficial Owners and Management" table.
  3. Compare the parsed percentages with what the scraper returns for that CIK.

HTML structure varies widely across issuers; the reader is tested against the
2026 proxies of Alphabet, Meta, Berkshire Hathaway, Tesla, Apple, Amazon,
Microsoft, Ford, Nike and Comcast (tests/scraper/fixtures/def14a_*.html).
"""

import re
import warnings
from datetime import date

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

SUBMISSIONS_URL = "https://data.sec.gov/submissions"
ARCHIVES_URL    = "https://www.sec.gov/Archives/edgar/data"



# ── HTTP helpers ───────────────────────────────────────────────────────────────

# Through the SEC scraper's one request helper (findings item 42). This module
# used to call `httpx.get` itself: a new connection per request (~60 s to set
# up on a host with a dead IPv6 route), no spacing, no retry, no cache — and no
# status check, so a 503 page was parsed as the proxy statement and reported
# "Could not locate beneficial ownership table". `_response` raises on any
# error status; the callers turn that into their `error` result.
def _get(url: str, **params) -> httpx.Response:
    from app.scraper.sec_client import _response
    return _response(url, params or None)


def _get_json(url: str) -> dict:
    return _get(url).json()


# ── EDGAR filing lookup ────────────────────────────────────────────────────────

def _search_recent_for_def14a(recent: dict) -> tuple[str, str, str] | None:
    """Scan a 'recent' filings dict for the most recent DEF 14A.
    Returns (accession_no_dashes, primary_doc_filename, filing_date) or None.
    """
    forms   = recent.get("form", [])
    accnums = recent.get("accessionNumber", [])
    docs    = recent.get("primaryDocument", [])
    dates   = recent.get("filingDate", [])

    for form, acc, doc, filed in zip(forms, accnums, docs, dates):
        if form == "DEF 14A" and doc:
            return acc.replace("-", ""), doc, filed
    return None


# How old a proxy statement can be before we consider it stale (years)
_MAX_FILING_AGE_YEARS = 4


def _is_recent(filing_date: str, today: date | None = None) -> bool:
    """Return True if the filing is recent enough to be useful.

    Counted from today (findings item 48): the cut-off was written as
    `2026 - 4`, so from 2027 on the window would have stopped moving and a
    proxy from 2022 would still count as recent."""
    try:
        year = int(filing_date[:4])
    except (ValueError, TypeError):
        return False
    return year >= (today or date.today()).year - _MAX_FILING_AGE_YEARS


def find_latest_def14a(company_name: str, cik: str | None = None) -> tuple[str, str, str, str] | None:
    """The company's most recent DEF 14A: (cik, accession_no_dashes, primary_doc,
    filing_date), or None when it files none.

    The company is resolved like every SEC scrape (findings item 47): the CIK
    the caller holds, else `sec_lookup.search_company`. This module used to ask
    browse-edgar's name search, which matches the start of the registered
    name — "Amazon.com" never found AMAZON COM INC."""
    from app.scraper.sec_client import _filer_record
    from app.scraper.sec_lookup import search_company
    if not cik:
        company = search_company(company_name)
        if not company:
            return None
        cik = company["cik"]
    cik = str(cik).zfill(10)
    try:
        sub = _filer_record(cik, _get_json)      # the one read path for a filer's record
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    filing = _search_recent_for_def14a(sub.get("filings", {}).get("recent", {}))
    return (cik, *filing) if filing else None


# ── The ownership table, read as a grid (findings item 47) ───────────────────
#
# The old reader guessed by position — "the last non-empty cell", "the first
# decimal number" — and read "*" as 0.4. On Alphabet's 2026 proxy that gave
# Pichai, Porat, Hennessy and Shriram 0.4 % (the filing says "less than 1 %"),
# and Robin L. Washington, who holds no shares, 92.2 %: the Class B column of
# the next row, "All executive officers and directors as a group". It picked a
# defined-terms table at Nike and a director's biography at Comcast. Now each
# table is expanded into columns (colspan and rowspan), the header rows name
# the columns, and every value is read from under its header.

_ZERO_WIDTH = ("\u200b", "\ufeff")
_SUPERSCRIPTS = "¹²³⁴⁵⁶⁷⁸⁹⁰"

_VALUE = re.compile(r"^[\s\d.,%*—–\-/()" + _SUPERSCRIPTS + r"]+$")
# "beneficial owner" but not "Beneficial Ownership": Microsoft's director table
# spans that heading over its figures, and as the name column it hid them all
_NAME_HEADER = re.compile(r"\bname\b|beneficial owner\b", re.IGNORECASE)
_VOTING = re.compile(r"voting\s+power", re.IGNORECASE)
_VOTING_TOTAL = re.compile(r"total|aggregate|combined|all classes", re.IGNORECASE)
_PERCENT = re.compile(r"percent|%", re.IGNORECASE)
_OWNED = re.compile(r"beneficial|owned|ownership|outstanding|to be voted", re.IGNORECASE)
#: a percent that is no percent of a class: the votes a director nominee got
#: last year. NovaGold's nominee table ("Name, Province … | Age | … | 2025 AGM
#: Votes in Favor (%) | Meets Share Ownership Guidelines") read as an ownership
#: table gave every director 85–99 % of the company (findings item 117)
_NOT_A_STAKE = re.compile(r"votes?\s+(in\s+favou?r|for\b|cast\b|withheld)|\bAGM\b|\battendance\b", re.IGNORECASE)
#: A figure a header states is no percent sign of its own: the section heading
#: "More than 5% owners:" stood in the class column of United States
#: Antimony's table and read as a percent column (findings item 117). Only the
#: FIGURE is set aside, not the header: "Percentage (* Less than 1%)" — the
#: commonest note on a percent column — is one, and with every header that
#: states a figure refused, Whirlpool's director table read no row and no
#: record date; the statements pass then began at the filing day instead and
#: lost a 13D filed between the two (findings item 142).
_FIGURE_PERCENT = re.compile(r"\d[\d.,]*\s*%")


def _is_percent_header(text: str) -> bool:
    """A header cell that heads a percent-of-class column: it says percent
    beside any figure it states, and is no vote count."""
    return bool(_PERCENT.search(_FIGURE_PERCENT.sub(" ", text))) and not _NOT_A_STAKE.search(text)


#: the column that names each row's class: "Title of Class", "Class of Stock"
_CLASS_COLUMN = re.compile(r"\btitle\s+of\s+(class|securit)|\bclass\s+of\s+(stock|shares|securities)\b",
                           re.IGNORECASE)
#: what a name cell is when it continues the holder above it instead of naming
#: a new one: "c/o Liberty Global Ltd.", "Clarendon House, 2 Church Street",
#: "Executive Vice President &", "Director"
_CONTINUES = re.compile(
    r"^c/o\b|[&,]$|^(executive|senior|chief|vice|director|officer|president|chairman|chairwoman|chair"
    r"|general\s+counsel|counsel|secretary|treasurer|founder)\b", re.IGNORECASE)
#: a class cell that is one word: "Common", "Preferred" (Bancroft Fund)
_BARE_KIND = re.compile(r"^(common|preferred|ordinary)$", re.IGNORECASE)
# A second voting class named anywhere in the filing (findings item 47): all
# six multi-class 2026 proxies probed say "Class B common stock" / "Class B
# Stock"; none of the four single-class ones does.
_MULTI_CLASS = re.compile(
    r"\bclass\s+[b-c]\b[^.]{0,40}?\b(common|stock|shares|ordinary)|\bseries\s+b\s+common",
    re.IGNORECASE)
# Rows that are not an owner: section headings and the group total.
_NOT_AN_OWNER = re.compile(
    r"as a group|^all (current )?(executive|directors|named)|^(named )?executive officers"
    r"|^directors\b|^neos?\b|\b5\s*% (share|stock|security)holders|^other\b",
    re.IGNORECASE)

# Street address appended to a person/entity name in some filings (e.g. Amazon).
# Cut from the FIRST number of a run — "717 5 th Avenue" (the ordinal's "th"
# set apart by a superscript tag) was cut at " 5 th" and left "Slate Path
# Capital LP 717", a name no statement and no registrant matched, so the 11.7 %
# holder of Wolfspeed was not written at all — and from "P.O. Box", whose
# number came after the words: "Capital Ventures International P.O. Box"
# (findings item 164)
_ADDRESS_RE = re.compile(r"\s+(?:\d+(?:\s+\d+)*\s+[A-Za-z]|(?i:P\.?\s?O\.?\s+Box)\b)")
# Footnote references like (1), (2), and superscript ones
_FOOTNOTE_RE = re.compile(r"\s*\(\d+\)|[" + _SUPERSCRIPTS + r"]+")


def _cell_text(cell) -> str:
    text = cell.get_text(" ", strip=True).replace("\xa0", " ")
    for z in _ZERO_WIDTH:
        text = text.replace(z, "")
    return re.sub(r"\s+", " ", text).strip()


def _span(cell, attr: str) -> int:
    digits = re.sub(r"\D", "", str(cell.get(attr, 1)))
    return max(1, int(digits)) if digits else 1


def _grid(table) -> list[list[tuple[int, int, str]]]:
    """The table's rows as (first column, column after the last, text) for each
    non-empty cell, with colspan and rowspan expanded. A rowspan cell's text
    belongs to its first row only, so a person's second row (Berkshire's
    Class B line) has no name and continues the person above."""
    taken: set[tuple[int, int]] = set()
    rows: list[list[tuple[int, int, str]]] = []
    for ri, tr in enumerate(table.find_all("tr")):
        if tr.find_parent("table") is not table:
            continue          # a nested table's row
        col, out = 0, []
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (ri, col) in taken:
                col += 1
            width, height = _span(cell, "colspan"), _span(cell, "rowspan")
            for r in range(1, height):
                for k in range(col, col + width):
                    taken.add((ri + r, k))
            text = _cell_text(cell)
            if text:
                out.append((col, col + width, text))
            col += width
        rows.append(out)
    return rows


def _overlaps(cell: tuple[int, int, str], column: tuple[int, int, str]) -> bool:
    return cell[0] < column[1] and cell[1] > column[0]


def _clean_name(text: str) -> str:
    name = _FOOTNOTE_RE.sub("", text).strip()
    m = _ADDRESS_RE.search(name)
    return (name[:m.start()] if m else name).strip(" ,")


def _parse_pct(text: str | None) -> tuple[float | None, bool]:
    """A voting-power cell → (percent, below_one_percent).

    "27.4", "19.8 %", "30.2 (2)", "33 1/3%" → the number; "*" (the proxies'
    "less than 1 %") → (None, True), never a made-up figure; "—", "" → (None,
    False)."""
    if not text:
        return None, False
    t = _FOOTNOTE_RE.sub("", text).replace("%", "").replace(",", "").strip()
    if t.startswith("*") or t.startswith("<"):
        return None, True
    m = re.match(r"^(\d+(?:\.\d+)?)(?:\s+(\d+)/(\d+))?$", t)
    if not m:
        return None, False
    value = float(m.group(1))
    if m.group(2) and int(m.group(3)):
        value += int(m.group(2)) / int(m.group(3))
    return (round(value, 2) if 0.0 <= value <= 100.0 else None), False


def _first_data_row(grid) -> int | None:
    """Index of the first row with a name cell and a value right of it.

    The name cell is the row's first cell that is no value — not the row's
    first cell: Whirlpool's 5 % holders table opens every row with the day the
    Schedule 13G was filed ("4/5/2024 | BlackRock, Inc. … | 6,226,160 |
    11.01 %"), and with the date taken for the name's place the table had no
    data row at all (findings item 143)."""
    for i, row in enumerate(grid[:12]):
        k = next((j for j, c in enumerate(row) if not _VALUE.match(c[2])), None)
        # a figure has a digit: Skydance's header row "Name of Beneficial
        # Owner | Shares | % | Shares | %" is no data row (findings item 86)
        if k is not None and any(
                c[0] >= row[k][1] and _VALUE.match(c[2]) and re.search(r"\d", c[2]) for c in row[k + 1:]):
            return i
    return None


def _read_table(table, single_class: bool) -> dict | None:
    """One ownership table → {"basis", "owners": [...]}, or None when it is not
    an ownership table or states no voting power."""
    grid = _grid(table)
    first = _first_data_row(grid)
    if first is None:
        return None
    header = [c for row in grid[:first] for c in row]
    header_text = " ".join(c[2] for c in header)
    names = [c for c in header if _NAME_HEADER.search(c[2])]
    if not names:
        return None
    voting = [c for c in header if _VOTING.search(c[2])]
    percent = [c for c in header if _PERCENT.search(c[2]) and not _VOTING.search(c[2])]
    if voting:
        column = ([c for c in voting if _VOTING_TOTAL.search(c[2])] or voting)[0]
        basis = "voting power"
    elif percent and single_class and _OWNED.search(header_text):
        column, basis = percent[0], "percent of class (one voting class)"
    else:
        return None

    name_col = names[0]
    owners: list[dict] = []
    current: dict | None = None
    for row in grid[first:]:
        if not row:
            continue
        name_cell = next((c for c in row if _overlaps(c, name_col) and not _VALUE.match(c[2])), None)
        values = [c[2] for c in row if c[0] >= name_col[1] and _overlaps(c, column)]
        has_data = any(c[0] >= name_col[1] and _VALUE.match(c[2]) for c in row)
        if name_cell is not None:
            name = _clean_name(name_cell[2])
            if not name or _NOT_AN_OWNER.search(name) or not has_data:
                current = None        # a section heading or the group total
                continue
            current = {"name": name, "cell": None}
            owners.append(current)
        if current is not None and values and current["cell"] is None:
            current["cell"] = " ".join(values)
    out = []
    for o in owners:
        pct, below = _parse_pct(o["cell"])
        out.append({"name": o["name"], "voting_power_pct": pct, "below_one_percent": below})
    return {"basis": basis, "column": column[2], "owners": out}


def read_ownership_tables(html: str) -> dict:
    """Every owner's voting power from a proxy statement's HTML.

    {"share_class_structure": "multi_class" | "single_class",
     "basis": [...], "owners": [{"name", "voting_power_pct", "below_one_percent"}]}
    An owner listed twice (Tesla lists Musk as a 5 % holder and as an officer)
    is kept once, with the first figure."""
    soup = BeautifulSoup(html, "lxml")
    single_class = not _MULTI_CLASS.search(soup.get_text(" "))
    owners: list[dict] = []
    seen: set[str] = set()
    bases: list[str] = []
    for table in soup.find_all("table"):
        read = _read_table(table, single_class)
        if not read or not read["owners"]:
            continue
        bases.append(f"{read['basis']}: {read['column']}")
        for o in read["owners"]:
            if o["name"] not in seen:
                seen.add(o["name"])
                owners.append(o)
    return {"share_class_structure": "single_class" if single_class else "multi_class",
            "basis": bases, "owners": owners}


# ── Every holder the table names (findings item 99) ─────────────────────────
#
# The voting-power reader above keeps a table only when it states voting power.
# Whether a Schedule 13D/G holder is still a 5 % holder needs every name the
# issuer's table lists, whatever its columns: the 5 % holders section and the
# directors and officers. A name with a trailing footnote number ("BlackRock,
# Inc. 3" on DaVita's 2026 proxy) is cut at it.

_TRAILING_NOTE = re.compile(r"[\s,]+\d{1,2}(\s*,\s*\d{1,2})*$|\s*\(\s*\d{1,2}\s*\)")
#: proxies that name a group by its controlling person add these words
_GROUP_WORDS = re.compile(r"\s+(and|&)\s+(certain\s+)?(affiliated|related)\s+(entities|parties|persons)\b.*$",
                          re.IGNORECASE)
#: the holder's postal address, begun in the name's own cell: "John C. Malone
#: c/o Liberty Media Corporation". No group — the stake is the person's own —
#: and often all that is left of it once the street is cut: "Patrick W. Galley
#: c/o 360 South Rosemary Avenue" (RiverNorth, findings item 152)
_CARE_OF = re.compile(r"\s+c/o(\s+.*)?$", re.IGNORECASE)
#: a row's number in front of its holder: "1 The Vanguard Group (1)"
_ROW_NUMBER = re.compile(r"^(\d{1,2})[.)]?\s+(?=\D)")


def holder_name(text: str) -> str:
    """A table's owner cell → the holder's name: footnotes, an appended
    address, "and affiliated entities" and "c/o …" cut ("Warren E. Buffett and
    affiliated entities" → "Warren E. Buffett"; "John C. Malone c/o Liberty
    Media Corporation" → "John C. Malone")."""
    name = _clean_name(text)
    name = _CARE_OF.sub("", _GROUP_WORDS.sub("", name))
    return _TRAILING_NOTE.sub("", name).strip(" ,")  # "(5 )", " 3", " 2, 4"


_PERCENT_CELL = re.compile(r"^\d[\d.,]*\s*%$")
#: the header a percent column gets that stands under no header cell
_UNHEADED_PERCENT = "Percent"


def _states_percent(rows: list, column: tuple[int, int, str]) -> bool:
    """A column whose figures are percents by their own cells: a "%" in the
    figure's cell or in the cell right of it ("30.3 | %")."""
    for row in rows:
        cells = [c for c in row if _overlaps(c, column)]
        if not any(re.search(r"\d", c[2]) for c in cells):
            continue
        if any("%" in c[2] for c in cells) or any(c[0] == column[1] and c[2].strip() == "%" for c in row):
            return True
    return False


def _percent_columns(header: list, rows: list, name_col: tuple[int, int, str]) -> list[tuple[int, int, str]]:
    """The table's percent columns, by their header — and where the header
    does not say it, by the cells (findings items 139 and 144):

    * a voting-power column headed without "percent" or "%": Liberty Global's
      "Voting Power" (John C. Malone 30.3 %), Restaurant Brands' and Penguin
      Solutions' "Total Voting Power" state the sign only beside the figures;
    * a column under no header cell at all, when the header names no percent
      column anywhere: OneSpan's "Name and Address of Beneficial Owner |
      Amount and Nature of Beneficial Ownership" stands over "3,720,220 | (2)
      | 10.0%". One such column only, stated on two rows at least — a second
      one would need a header to say which percent is which."""
    percents = [c for c in header if _is_percent_header(c[2])
                or (_VOTING.search(c[2]) and not _NOT_A_STAKE.search(c[2]) and _states_percent(rows, c))]
    if percents:
        return percents
    spans: dict[tuple[int, int], int] = {}
    for row in rows:
        for c in row:
            if c[0] >= name_col[1] and _PERCENT_CELL.match(c[2]) and not any(_overlaps(c, h) for h in header):
                spans[(c[0], c[1])] = spans.get((c[0], c[1]), 0) + 1
    if len(spans) == 1:
        (span, count), = spans.items()
        if count >= 2:
            return [(span[0], span[1], _UNHEADED_PERCENT)]
    return []


_STREET_WORD = (r"(street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|blvd\.?|drive|dr\.?|lane|way|plaza|place"
                r"|square|floor|suite|yards|center|centre|parkway|pkwy\.?|broadway|highway|court|circle|building)")
_ADDRESS_START = re.compile(
    r"^(c/o\b|p\.?\s?o\.?\s+box\b|(building|suite|floor)\s)"
    r"|^(\d[\d-]*|one|two|three|four|five)\s+.*\b" + _STREET_WORD + r"(?!\w)",
    re.IGNORECASE)
#: the line an address ends with, alone in its cell: "New York, NY 10001",
#: "Boston, MA 02114-2016", "Hamilton, Bermuda HM11"
_PLACE_LINE = re.compile(r"[A-Za-z. ]+,\s*(?:[A-Z]{2}\s+\d{5}(?:-\d{4})?"
                         r"|[A-Za-z. ]*?\b(?=[A-Z0-9]*\d)[A-Z0-9]{3,8}(?:\s[A-Z0-9]{3})?)")
#: a legal form says the cell names somebody: "59 North Capital Management,
#: LP" and "1607 Capital Partners, LLC" begin with a number and are no street
_LEGAL_FORM = re.compile(r"\b(LLC|L\.L\.C\.|LP|L\.P\.|LLP|Inc\.?|Ltd\.?|Limited|Corp\.?|Corporation|Company|N\.A\.|PLC)(?!\w)")


def _is_address_line(text: str) -> bool:
    """A name cell that is a line of an address from its first word and names
    nobody: "50 Hudson Yards", "One Coleman Street, London", "c/o …", "New
    York, NY 10001". A cell that begins with a name and goes on with the
    address ("The Vanguard Group 100 Vanguard Blvd. Malvern, PA 19355") is
    the holder's own row."""
    text = _FOOTNOTE_RE.sub("", text).strip()
    if re.match(r"c/o\b", text, re.IGNORECASE):
        return True
    return bool(_ADDRESS_START.search(text) or _PLACE_LINE.fullmatch(text)) and not _LEGAL_FORM.search(text)


def _named_above(block: list[str]) -> str:
    """Of the lines that stood alone above an address row, the one that names
    the holder: the first with a legal form or a footnote mark ("State Street
    Corporation (3)" over "State Street Financial Center"), else the last (a
    heading the reader does not know, then the name)."""
    return next((t for t in block if _LEGAL_FORM.search(t) or _FOOTNOTE_RE.search(t)), block[-1])


def _holder_rows(table) -> list[dict] | None:
    """One ownership table → [{"name", "percents": [float…]}], every percent
    column read under its header; None when it is no ownership table (no name
    column, no percent column, no ownership words in the header).

    A table may name the class per ROW instead of per column (findings item
    117) — a "Title of Class" column whose cell stands beside each figure
    (Liberty Global: one holder over three rows, Class A / B / C) or alone on
    a row above a group of holders (Universal Display: "Common Stock", then
    "Series A Nonconvertible Preferred Stock"). The class then goes in front
    of the percent column's header, so a row is read as a table with a column
    per class is. In the first shape the name cell goes on below the name
    with an address or a title; a row that brings a class the holder has no
    figure for yet is the holder's next row, not a new holder."""
    grid = _grid(table)
    first = _first_data_row(grid)
    if first is None:
        return None
    header = [c for row in grid[:first] for c in row]
    names = [c for c in header if _NAME_HEADER.search(c[2])]
    if not names or not _OWNED.search(" ".join(c[2] for c in header)):
        return None
    name_col = names[0]
    percents = _percent_columns(header, grid[first:], name_col)
    if not percents:
        return None
    # a column of its own: "Title of Class" spanning the percent columns is a
    # heading over a column per class (Universal Health Services, LENSAR)
    class_col = next((c for c in header if _CLASS_COLUMN.search(c[2]) and c is not name_col
                      and not _PERCENT.search(c[2]) and not any(_overlaps(c, p) for p in percents)), None)

    def row_class(row) -> str | None:
        if class_col is None:
            return None
        text = next((c[2] for c in row if _overlaps(c, class_col) and c is not class_col
                     and not _VALUE.match(c[2]) and not _overlaps(c, name_col)), None)
        if text is None or _security(text):
            return text
        if _BARE_KIND.match(text.strip()):
            return f"{text} Stock"
        # alone on its row and no security the reader knows: a section heading
        # standing in the class column ("Directors and Executive Officers:",
        # United States Antimony), not a class
        if not any(c[0] >= name_col[1] and _VALUE.match(c[2]) for c in row):
            return None
        # beside figures, a class the reader has no word for is still a class
        return f"{_OTHER_SECURITY}: {text} |"

    # the class column's own cells are no heading of the percent columns
    above = [c for c in header if class_col is None or not _overlaps(c, class_col)]
    headers = {col: _column_header(above, col) for col in percents}
    securities = {_security(headers[c]) for c in percents if not _VOTING.search(c[2])}
    if class_col is not None:
        securities |= {t if t.startswith(_OTHER_SECURITY) else _security(t)
                       for t in (row_class(r) for r in grid) if t}
    # a table with a column per security (Class A beside Class B, common
    # beside a preferred series) names the security on every row it writes
    several = len(securities - {None}) > 1
    # the class in force: a class cell in the header rows (Universal Display's
    # "Common Stock" stands above the first holder), then each one met
    in_force = next((t for r in grid[:first] for t in [row_class(r)] if t), None)
    rows: list[dict] = []
    current: dict | None = None
    # the holder named alone on a row, its figures on the address row below
    # (findings item 150): "BlackRock, Inc." / "50 Hudson Yards | 6,085,591 |
    # 13.87%" — Dime Community's five holders were all named by their street.
    # `block`: the lines that named somebody since the last row with figures.
    block: list[str] = []

    def alone(text: str) -> None:
        name = holder_name(text)
        if not name or _NOT_AN_OWNER.search(name) or text.rstrip().endswith(":"):
            block.clear()                  # a heading: what stood above it is over
        elif not _is_address_line(text):
            block.append(text)

    # the first holder's name row has no figure, so it lies above the first data row
    last_header = max((i for i, r in enumerate(grid[:first]) if any(c in names for c in r)), default=first)
    for row in grid[last_header + 1:first]:
        if len(row) == 1 and _overlaps(row[0], name_col):
            alone(row[0][2])
    for row in grid[first:]:
        if not row:
            continue
        name_cell = next((c for c in row if _overlaps(c, name_col) and not _VALUE.match(c[2])), None)
        has_data = any(c[0] >= name_col[1] and _VALUE.match(c[2]) for c in row)
        here = row_class(row)
        if here:
            in_force = here
        # … once a row has continued the holder (no name, "c/o …", a title),
        # whatever the name cell goes on with below is the same holder's
        # ("Clarendon House, 2 Church Street" under "c/o Liberty Global Ltd.")
        continues = (here is not None and current is not None and here not in current["classes"]
                     and (name_cell is None or current["chain"] or bool(_CONTINUES.search(name_cell[2]))))
        if continues:
            current["chain"] = True
        if name_cell is not None and not continues:
            owner_cell = name_cell[2]
            if has_data and block and _is_address_line(owner_cell):
                owner_cell = _named_above(block)
            name = holder_name(owner_cell)
            if not has_data:
                alone(owner_cell)
            else:
                block.clear()
            if not name or _NOT_AN_OWNER.search(name) or not has_data:
                current = None
                continue
            current = {"name": name, "percents": [], "raw": _clean_name(owner_cell),
                       "several_classes": several, "classes": set(), "chain": False}
            rows.append(current)
        if current is not None:
            if here:
                current["classes"].add(here)
            for col in percents:
                cells = [c[2] for c in row if c[0] >= name_col[1] and _overlaps(c, col)]
                pct = _parse_pct(" ".join(cells))[0] if cells else None
                if pct is not None:
                    current["percents"].append(pct)
                    head = f"{in_force} {headers[col]}" if in_force else headers[col]
                    current.setdefault("columns", []).append((head, pct))
    for r in rows:
        r.pop("classes", None)
        r.pop("chain", None)
    _drop_row_numbers(rows)
    return rows


def _drop_row_numbers(rows: list[dict]) -> None:
    """A table that numbers its holders — Ovintiv's "1 The Vanguard Group (1)",
    "2 BlackRock, Inc. (2)", "3 FMR LLC (3)" — is read without the numbers
    (findings item 151). Only when the rows that begin with a number count 1,
    2, 3 … in the table's order, two of them at least: a number alone does
    not tell a row's from a name's ("59 North Capital Management, LP", "65
    Equity Partners", "3M Company")."""
    numbered = [(r, int(m.group(1))) for r in rows for m in [_ROW_NUMBER.match(r["raw"])] if m]
    if len(numbered) < 2 or [n for _, n in numbered] != list(range(1, len(numbered) + 1)):
        return
    for r, _ in numbered:
        r["name"] = _ROW_NUMBER.sub("", r["name"])
        r["raw"] = _ROW_NUMBER.sub("", r["raw"])


#: how `_column_header` and a class cell mark a security the reader has no
#: word for: "Other security: <its heading> | <the column's own header>"
_OTHER_SECURITY = "Other security"


def _column_header(header: list, column: tuple[int, int, str]) -> str:
    """Every header cell above a column, top to bottom: "Class A Common Stock"
    spanning "Shares | %" gives the percent column "Class A Common Stock %".
    A header that names no security takes that of the shares column left of
    it: Molson Coors' "Class A Shares | Percent of class (%)", QXO's "Shares of
    Convertible Preferred Stock Beneficially Owned | Percentage of Class
    Outstanding" (findings item 116).

    Only inside its own group, though (findings item 117). A percent column
    under a heading that spans it — Penguin Solutions' "Issued CPS" over
    "Amount | Percentage of Shares Beneficially Owned", beside "Common Stock"
    over the same pair — is a percent of what THAT heading names: read with
    the class of the group left of it, SK Telecom's 100 % of the convertible
    preferred was 100 % of the common. A heading the reader has no word for is
    marked as another security, which `holder_stake` keeps apart."""
    above = [c for c in header if _overlaps(c, column)]
    text = " ".join(c[2] for c in above)
    if _security(text):
        return text
    group = min((c for c in above if c[0] < column[0]), key=lambda c: c[0], default=None)
    left = [c for c in header if c[1] <= column[0] and _security(c[2])
            and (group is None or c[0] >= group[0])]
    if left:
        return f"{max(left, key=lambda c: c[1])[2]} {text}"
    named_elsewhere = any(_security(c[2]) for c in header if c[1] <= (group or column)[0])
    return f"{_OTHER_SECURITY}: {group[2]} | {text}" if group is not None and named_elsewhere else text


def read_holder_rows(html: str) -> list[dict]:
    """Every holder the proxy's ownership tables name: [{"name", "percents"}],
    in table order, a name listed twice kept once (its percents merged)."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, dict] = {}
    for table in soup.find_all("table"):
        for r in _holder_rows(table) or []:
            row = out.setdefault(r["name"], {"name": r["name"], "percents": []})
            row["percents"] += r["percents"]
    return list(out.values())


# ── The holders the table adds to the graph (findings item 86) ─────────────
#
# A controlling holder that files no Schedule 13D/G is invisible to the
# 13D/G reader: Skydance's 10-K/A (2026-04-24) names Harbor Lights
# Entertainment with 100 % of the Class A stock, and EDGAR holds no statement
# of it. The table states, per holder, the percent of each class and — on a
# multi-class issuer — of the votes, as of a record date.

_AS_OF = re.compile(r"\bas\s+of\s+([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})", re.IGNORECASE)
_CLASS = re.compile(r"\b(class|series)\s+([A-Z]{1,2}|\d)\b(?!\w)", re.IGNORECASE)
#: words after "class" that name no class: "Title or Class of Securities",
#: "Class of Shares or Units" (LENSAR, Forward Air read as "Class OF")
_NOT_A_CLASS = frozenset({"OF", "OR", "AS", "IN", "TO", "ON", "BY", "IS", "AT"})
#: a numbered class is one only with its security named after it ("Class 1
#: Common Stock"): "Percent of Class 1" is a footnote mark (Baxter, Equifax)
_CLASS_SECURITY = re.compile(r"\s*(common|ordinary|preferred|stock|shares|units)\b", re.IGNORECASE)


def _class_match(text: str | None) -> re.Match | None:
    """The first share class a header names, None when it names none."""
    for m in _CLASS.finditer(text or ""):
        word = m.group(2).upper()
        if word in _NOT_A_CLASS:
            continue
        if word.isdigit() and not _CLASS_SECURITY.match(text, m.end()):
            continue
        return m
    return None
#: "Entities affiliated with BlackRock, Inc." → "BlackRock, Inc."
_AFFILIATED_PREFIX = re.compile(r"^(certain\s+)?(entities|funds|persons)(\s+and\s+persons)?\s+affiliated\s+with\s+",
                                re.IGNORECASE)
#: an address run into the name: "GAMCO Investors, Inc. One Corporate Center
#: Rye, NY", "MUFG 4-5, Marunouchi 1-chome" — a street number, or "One"
#: followed by a place word
_STREET = re.compile(r"\s+(\d[\d-]*,?\s+[A-Z]|One\s+[A-Z][a-z]+\s+(Center|Centre|Plaza|Street|Avenue|Way|Drive|Road|Park|Place|Square)\b).*$")


def _iso_date(text: str) -> str | None:
    from datetime import datetime
    t = re.sub(r"\s+", " ", text.replace(".", "")).replace(",", "").strip()
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def table_as_of(header_text: str, before_text: str) -> str | None:
    """The record date a table speaks as of: its own header ("Shares
    Beneficially Owned as of April 2, 2026", Sirius XM) first, else the
    sentence before it ("… as of March 2, 2026, by …")."""
    for text in (header_text, before_text):
        for m in _AS_OF.finditer(text or ""):
            day = _iso_date(m.group(1))
            if day:
                return day
    return None


def holder_names(text: str) -> list[str]:
    """A table's owner cell → the names it may stand for, in order: footnotes,
    an appended address and "Entities affiliated with" cut, and two names
    joined by "/" split ("Warren E. Buffett/Berkshire Hathaway Inc." on
    Domino's 2026 proxy)."""
    name = _AFFILIATED_PREFIX.sub("", holder_name(text))
    name = _STREET.sub("", name).strip(" ,")
    return [p.strip(" ,") for p in name.split("/") if p.strip(" ,")] if name else []


def _share_class(header: str) -> str | None:
    m = _class_match(header)
    return f"{m.group(1).title()} {m.group(2).upper()}" if m else None


#: a security a column is a percent OF when its header gives it no letter:
#: "Shares of Convertible Preferred Stock Beneficially Owned" (QXO) — and the
#: totals over every class, which are no preferred: "% of Total Capital Stock"
#: (Victory Capital: Amundi's 27.8 %, beside 4.9 % of the common), "Total
#: Shares of Voting Stock" (Arbor Realty)
_SECURITY_KIND = re.compile(
    r"\b((?:convertible\s+)?preferred|common|ordinary|capital|voting)\s+(stock|shares|units)\b"
    # units of the operating company under an Up-C or an UPREIT, exchangeable
    # for the issuer's shares: "LLC Units" (Malibu Boats), "Partnership
    # Exchangeable Units" (Restaurant Brands), "OP Units" (findings item 117)
    r"|\b(?:(?:llc|op|lp|ltip|partnership|membership|exchangeable|operating|holdco|opco)\s+)+units\b",
    re.IGNORECASE)
_PREFERRED = re.compile(r"\bpreferred\b", re.IGNORECASE)
_UNITS = re.compile(r"\b(?:llc|op|lp|ltip|partnership|membership|exchangeable|operating|holdco|opco)\s+units\b",
                    re.IGNORECASE)
_COMMON = re.compile(r"\b(common|ordinary)\b", re.IGNORECASE)


def _security(header: str | None) -> str | None:
    """The security a header names — its class ("Class B", "Series A"), else
    its kind ("Common Stock", "Convertible Preferred Stock") — or None."""
    header = header or ""
    if header.startswith(_OTHER_SECURITY):          # named as its heading names it: "Issued CPS"
        return holder_name(header[len(_OTHER_SECURITY) + 1:].split("|")[0]) or _OTHER_SECURITY
    named = _share_class(header)
    if named:
        return named
    m = _SECURITY_KIND.search(header)
    return re.sub(r"\s+", " ", m.group(0)).title() if m else None


def _is_preferred(header: str) -> bool:
    """A column that is not a percent of the issuer's stock: a preferred
    class, units of the operating company, a security the header has no word
    the reader knows for (findings items 116 and 117)."""
    if header.startswith(_OTHER_SECURITY) or _UNITS.search(header):
        return True
    return bool(_PREFERRED.search(header)) and not _COMMON.search(header)


def holder_stake(columns: list[tuple[str, float]], several_classes: bool = False) -> dict:
    """A row's percent columns → {"stake_pct", "share_class", "voting_power_pct"}:
    the largest stated percent of a class is the stake (Better Home's Vishal
    Garg: 13.2 % of Class A, 40.4 % of Class B → 40.4, "Class B"), a
    voting-power column the voting power.

    A preferred class beside common stock is not the stake (findings item
    116): the common percent is beneficial ownership and already counts the
    shares the preferred converts into — QXO's Jacobs Private Equity II, 35.7 %
    of the common and 90.0 % of the Convertible Preferred, holds 35.7 %, and
    the 90 beside Vanguard's 7.8 summed to 102.3 %. A row that states a
    preferred only keeps it (Security Benefit Life: 100 % of Franklin BSP's
    Series H).

    ``several_classes``: the table has a column per security, so the class is
    named even where the header gives it no letter ("Common Stock") — a
    preferred-only holder beside holders of an unnamed class would be added
    to them (LENSAR: 100 % of the Series A beside 8.4 and 5.1 % of the common).
    A one-class table names none, as before."""
    voting = [p for h, p in columns if _VOTING.search(h)]
    classes = [(p, h) for h, p in columns if not _VOTING.search(h)]
    pool = [c for c in classes if not _is_preferred(c[1])] or classes
    best = max(pool, key=lambda c: c[0]) if pool else None
    named = (_security(best[1]) if several_classes else _share_class(best[1])) if best else None
    return {"stake_pct": best[0] if best else None,
            "share_class": named,
            "voting_power_pct": max(voting) if voting else None}


def read_holder_table(html: str) -> dict:
    """The holders an ownership table states a percent for, with what the
    graph needs to write them: {"as_of", "rows": [{"name", "names", "group",
    "stake_pct", "share_class", "voting_power_pct"}]} — a name listed twice
    kept once (its first row)."""
    soup = BeautifulSoup(html, "lxml")
    as_of = None
    out: dict[str, dict] = {}
    for table in soup.find_all("table"):
        rows = _holder_rows(table)
        if not rows:
            continue
        if as_of is None:
            grid = _grid(table)
            first = _first_data_row(grid) or 0
            header_text = " ".join(c[2] for row in grid[:first] for c in row)
            before = " ".join(reversed([t.strip() for t in table.find_all_previous(string=True, limit=40)]))
            as_of = table_as_of(header_text, before)
        for r in rows:
            if r["name"] in out or not r.get("columns"):
                continue
            raw = r.get("raw") or r["name"]
            out[r["name"]] = {"name": r["name"], "names": holder_names(raw),
                              # "Entities affiliated with X", "X and affiliated
                              # entities": the stake is the group's, not X's own
                              "group": bool(_AFFILIATED_PREFIX.search(raw) or _GROUP_WORDS.search(raw)),
                              **holder_stake(r["columns"], r.get("several_classes", False))}
    return {"as_of": as_of, "rows": list(out.values())}


#: the forms that carry the ownership table: the proxy, and a 10-K/A whose
#: Part III stands in for it (Skydance, 2026)
TABLE_FORMS = ("DEF 14A", "10-K/A")
#: a table older than this says nothing about today's holders
TABLE_MAX_AGE_DAYS = 550


def latest_table_filing(submissions: dict, today: date | None = None) -> dict | None:
    """The newest DEF 14A (or 10-K/A) on a submissions document filed within
    TABLE_MAX_AGE_DAYS: {"form", "date", "url"} or None."""
    from datetime import timedelta
    cik = str(submissions.get("cik") or "").lstrip("0")
    recent = (submissions.get("filings") or {}).get("recent") or {}
    oldest = ((today or date.today()) - timedelta(days=TABLE_MAX_AGE_DAYS)).isoformat()
    for form, acc, doc, filed in zip(recent.get("form") or [], recent.get("accessionNumber") or [],
                                     recent.get("primaryDocument") or [], recent.get("filingDate") or []):
        if form in TABLE_FORMS and doc and filed >= oldest and cik:
            return {"form": form, "date": filed,
                    "url": f"{ARCHIVES_URL}/{cik}/{acc.replace('-', '')}/{doc}"}
    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_proxy_ownership(company_name: str, cik: str | None = None) -> dict:
    """
    Fetch and read the most recent DEF 14A proxy statement for a company.

    Returns:
        {
            "company", "cik", "filing_date", "filing_url",
            "share_class_structure": "multi_class" | "single_class",
            "basis":  [which column of which table the figures are from],
            "owners": [{"name": str, "voting_power_pct": float|None,
                        "below_one_percent": bool}],
        }
    or {"error": str, "owners": []} on failure. A multi-class filing that
    states no voting power returns owners [] and a "note".
    """
    try:
        found = find_latest_def14a(company_name, cik)
    except httpx.HTTPError as exc:
        # the company search or its submissions file failed (already logged as
        # a lost request) — not "this company files no proxy"
        return {"error": f"EDGAR search failed: {exc}", "owners": []}
    if not found:
        return {"error": f"No DEF 14A found on EDGAR for '{company_name}'", "owners": []}

    cik, acc_nodash, primary_doc, filing_date = found
    doc_url = f"{ARCHIVES_URL}/{int(cik)}/{acc_nodash}/{primary_doc}"

    try:
        resp = _get(doc_url)
    except httpx.HTTPError as exc:
        return {"error": f"Failed to fetch filing: {exc}", "owners": []}

    read = read_ownership_tables(resp.text)
    result: dict = {
        "company":     company_name,
        "cik":         cik,
        "filing_date": filing_date,
        "filing_url":  doc_url,
        **read,
    }
    if not read["owners"]:
        result["note"] = ("The filing states no voting power per owner (several share "
                          "classes and no voting-power column)"
                          if read["share_class_structure"] == "multi_class"
                          else "Could not locate the beneficial ownership table")
    if not _is_recent(filing_date):
        result["warning"] = (
            f"Most recent DEF 14A is from {filing_date[:4]}; the company may have "
            f"been reorganised — pass the current filer's CIK."
        )
    return result
