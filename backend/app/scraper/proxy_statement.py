"""
SEC EDGAR DEF 14A (proxy statement) scraper — POC.

Data source:  https://www.sec.gov
Manual lookup:
  https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=DEF+14A&CIK=<cik>

Endpoints used:
  The company is resolved like every SEC scrape: by the CIK the caller holds,
  else by sec_edgar.search_company (tickers file, then EDGAR's indexes).
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
  Every request goes through sec_edgar._response (findings item 42): the pooled
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
    from app.scraper.sec_edgar import _response
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
    the caller holds, else `sec_edgar.search_company`. This module used to ask
    browse-edgar's name search, which matches the start of the registered
    name — "Amazon.com" never found AMAZON COM INC."""
    from app.scraper.sec_edgar import search_company
    if not cik:
        company = search_company(company_name)
        if not company:
            return None
        cik = company["cik"]
    cik = str(cik).zfill(10)
    try:
        sub = _get_json(f"{SUBMISSIONS_URL}/CIK{cik}.json")
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

# Street address appended to a person/entity name in some filings (e.g. Amazon)
_ADDRESS_RE = re.compile(r"\s+\d+\s+[A-Za-z]")
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
    """Index of the first row with a name cell and a value right of it."""
    for i, row in enumerate(grid[:12]):
        if len(row) >= 2 and not _VALUE.match(row[0][2]) and any(
                c[0] >= row[0][1] and _VALUE.match(c[2]) for c in row[1:]):
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
_GROUP_WORDS = re.compile(r"\s+(and|&)\s+(certain\s+)?(affiliated|related)\s+(entities|parties|persons)\b.*$"
                          r"|\s+c/o\s+.*$", re.IGNORECASE)


def holder_name(text: str) -> str:
    """A table's owner cell → the holder's name: footnotes, an appended
    address, "and affiliated entities" and "c/o …" cut ("Warren E. Buffett and
    affiliated entities" → "Warren E. Buffett"; "John C. Malone c/o Liberty
    Media Corporation" → "John C. Malone")."""
    name = _clean_name(text)
    name = _GROUP_WORDS.sub("", name)
    return _TRAILING_NOTE.sub("", name).strip(" ,")  # "(5 )", " 3", " 2, 4"


def _holder_rows(table) -> list[dict] | None:
    """One ownership table → [{"name", "percents": [float…]}], every percent
    column read under its header; None when it is no ownership table (no name
    column, no percent column, no ownership words in the header)."""
    grid = _grid(table)
    first = _first_data_row(grid)
    if first is None:
        return None
    header = [c for row in grid[:first] for c in row]
    names = [c for c in header if _NAME_HEADER.search(c[2])]
    percents = [c for c in header if _PERCENT.search(c[2])]
    if not names or not percents or not _OWNED.search(" ".join(c[2] for c in header)):
        return None
    name_col = names[0]
    rows: list[dict] = []
    current: dict | None = None
    for row in grid[first:]:
        if not row:
            continue
        name_cell = next((c for c in row if _overlaps(c, name_col) and not _VALUE.match(c[2])), None)
        has_data = any(c[0] >= name_col[1] and _VALUE.match(c[2]) for c in row)
        if name_cell is not None:
            name = holder_name(name_cell[2])
            if not name or _NOT_AN_OWNER.search(name) or not has_data:
                current = None
                continue
            current = {"name": name, "percents": []}
            rows.append(current)
        if current is not None:
            for col in percents:
                cells = [c[2] for c in row if c[0] >= name_col[1] and _overlaps(c, col)]
                pct = _parse_pct(" ".join(cells))[0] if cells else None
                if pct is not None:
                    current["percents"].append(pct)
    return rows


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
