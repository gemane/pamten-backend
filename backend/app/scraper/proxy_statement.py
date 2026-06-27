"""
SEC EDGAR DEF 14A (proxy statement) scraper — POC.

Extracts the beneficial ownership table from the most recent annual proxy
filing to get per-person voting power percentages for companies with
multiple share classes (e.g. Alphabet Class A / Class B).

POC: returns parsed data only; does not write to the database.
"""

import re
import warnings
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

HEADERS = {"User-Agent": "Pamten/1.0 contact@pamten.com"}
BROWSE_URL      = "https://www.sec.gov/cgi-bin/browse-edgar"
SUBMISSIONS_URL = "https://data.sec.gov/submissions"
ARCHIVES_URL    = "https://www.sec.gov/Archives/edgar/data"
_TIMEOUT        = 25.0

_NS_A = {"a": "http://www.w3.org/2005/Atom"}

# How similar the company name must be to the EDGAR-registered name (0–1).
_MIN_SIM = 0.45


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get(url: str, **params) -> httpx.Response:
    return httpx.get(url, headers=HEADERS, timeout=_TIMEOUT,
                     follow_redirects=True, params=params or None)


def _get_json(url: str) -> dict:
    return _get(url).json()


# ── Name similarity ────────────────────────────────────────────────────────────

_SUFFIX_RE = re.compile(
    r"\b(inc|corp|ltd|llc|plc|sa|ag|co|company|group|holding|holdings)\b\.?",
    re.IGNORECASE,
)


def _sim(a: str, b: str) -> float:
    def norm(s: str) -> str:
        s = _SUFFIX_RE.sub("", s.lower())
        return re.sub(r"[^a-z0-9 ]", "", s).strip()
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


# ── EDGAR filing lookup ────────────────────────────────────────────────────────

def _search_recent_for_def14a(recent: dict) -> tuple[str, str, str] | None:
    """Scan a 'recent' filings dict for the most recent DEF 14A.
    Returns (accession_no_dashes, primary_doc_filename, filing_date) or None.
    """
    forms   = recent.get("form", [])
    accnums = recent.get("accessionNumber", [])
    docs    = recent.get("primaryDocument", [])
    dates   = recent.get("filingDate", [])

    for form, acc, doc, date in zip(forms, accnums, docs, dates):
        if form == "DEF 14A" and doc:
            return acc.replace("-", ""), doc, date
    return None


def _get_def14a_for_cik(cik: str) -> tuple[str, str, str] | None:
    """Return (accession_no_dashes, primary_doc, filing_date) for a CIK's most
    recent DEF 14A, searching across all submissions pages if needed."""
    cik_padded = cik.zfill(10)
    sub = _get_json(f"{SUBMISSIONS_URL}/CIK{cik_padded}.json")
    hit = _search_recent_for_def14a(sub.get("filings", {}).get("recent", {}))
    if hit:
        return hit

    # Large filers paginate older filings into extra files; scan them too
    for extra in sub.get("filings", {}).get("files", []):
        extra_data = _get_json(f"https://data.sec.gov/submissions/{extra['name']}")
        hit = _search_recent_for_def14a(extra_data)
        if hit:
            return hit
    return None


def _browse_edgar_def14a(search_name: str) -> tuple[str, str, str, str] | None:
    """
    Search browse-edgar for DEF 14A filings matching search_name.

    The EDGAR DEF 14A ATOM feed places the CIK in a feed-level
    <company-info><cik> element (not in the per-entry <id> tag).
    Returns (cik_padded, accession_no_dashes, primary_doc, filing_date) or None.
    """
    resp = _get(BROWSE_URL, company=search_name, action="getcompany",
                type="DEF 14A", dateb="", owner="include", count="5",
                search_text="", output="atom")
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return None

    # CIK and registered name live in the feed-level <company-info> block
    cik_el  = root.find("a:company-info/a:cik",            _NS_A)
    name_el = root.find("a:company-info/a:conformed-name", _NS_A)

    if cik_el is None or not (cik_el.text or "").strip():
        return None

    cik             = cik_el.text.strip().zfill(10)
    registered_name = (name_el.text or "").strip() if name_el is not None else ""

    if _sim(search_name, registered_name) < _MIN_SIM:
        return None

    # Most recent filing is the first <entry> in the feed
    for entry in root.findall("a:entry", _NS_A):
        content = entry.find("a:content", _NS_A)
        if content is None:
            continue
        accnum = (content.findtext("a:accession-number", "", _NS_A) or "").strip()
        date   = (content.findtext("a:filing-date",      "", _NS_A) or "").strip()
        if not accnum:
            continue
        acc_nodash = accnum.replace("-", "")

        # Resolve primary document filename from submissions JSON
        sub = _get_json(f"{SUBMISSIONS_URL}/CIK{cik}.json")
        recent = sub.get("filings", {}).get("recent", {})
        for form, acc, doc, d in zip(
            recent.get("form", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
            recent.get("filingDate", []),
        ):
            if form == "DEF 14A" and acc.replace("-", "") == acc_nodash and doc:
                return cik, acc_nodash, doc, d
        break  # only try the first (most recent) entry

    return None


# How old a proxy statement can be before we consider it stale (years)
_MAX_FILING_AGE_YEARS = 4


def _is_recent(filing_date: str) -> bool:
    """Return True if the filing is recent enough to be useful."""
    try:
        year = int(filing_date[:4])
        return year >= (2026 - _MAX_FILING_AGE_YEARS)
    except (ValueError, TypeError):
        return False


def _find_company(company_name: str) -> tuple[str, str, str, str] | None:
    """
    Find the most recent DEF 14A for company_name on EDGAR.

    Returns (cik, accession_no_dashes, primary_doc, filing_date) or None.
    Falls back to searching by the EDGAR-registered name if the first
    search returns a stale filing (e.g. 'Google' → 'Google Inc.' pre-2015,
    while the current entity is 'Alphabet Inc.').
    """
    result = _browse_edgar_def14a(company_name)
    if result and _is_recent(result[3]):
        return result

    # Stale or not found: try the registered name from the stale result
    # (handles Google → Alphabet split, etc.)
    if result:
        cik = result[0]
        try:
            sub = _get_json(f"{SUBMISSIONS_URL}/CIK{cik}.json")
            registered = sub.get("name", "")
        except Exception:
            registered = ""
        if registered and registered.lower() != company_name.lower():
            fallback = _browse_edgar_def14a(registered)
            if fallback and _is_recent(fallback[3]):
                return fallback

    return result  # return even if stale — caller can check filing_date


# ── Table parsing ──────────────────────────────────────────────────────────────

# First-cell patterns that identify header or section-divider rows to skip
_SKIP_FIRST_CELL = re.compile(
    r"name\s+of\s+beneficial|class\s+[abc]|shares|percent|voting\s+power"
    r"|executive\s+officers|other\s+[>5%]|all\s+(executive|directors)",
    re.IGNORECASE,
)

# Footnote references like (1), (2)
_FOOTNOTE_RE = re.compile(r"\s*\(\d+\)")


def _clean_name(text: str) -> str:
    return _FOOTNOTE_RE.sub("", text).strip()


def _parse_pct(text: str) -> float | None:
    """'27.4', '27.4%', '*', '—', '' → float or None."""
    text = _FOOTNOTE_RE.sub("", text).strip()
    if not text or text in ("—", "–", "-", "N/A", "n/a"):
        return None
    if text == "*":
        return 0.4   # proxy convention: < 1%
    text = text.replace("%", "").replace(",", "").strip()
    try:
        v = float(text)
        return v if 0.0 <= v <= 100.0 else None
    except ValueError:
        return None


def _parse_shares(text: str) -> int | None:
    """'1,234,567', '—', '*' → int or None."""
    text = _FOOTNOTE_RE.sub("", text).strip()
    if not text or text in ("—", "–", "-", "*", ""):
        return None
    text = text.replace(",", "").replace("%", "").strip()
    try:
        v = int(float(text))
        return v if v > 0 else None
    except ValueError:
        return None


def _find_voting_table(soup):
    """Return the beneficial ownership HTML table, or None."""
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        # Must mention voting power AND at least one share class
        if "voting power" in text and ("class a" in text or "class b" in text):
            if len(table.find_all("tr")) >= 5:
                return table
    return None


def _parse_ownership_table(table) -> list[dict]:
    """Parse the beneficial ownership table into a list of owner dicts."""
    rows = table.find_all("tr")

    # Detect where data rows start by scanning for the last "header-like" row
    data_start = 0
    for i, row in enumerate(rows[:8]):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        first = cells[0].get_text(" ", strip=True)
        all_text = " ".join(c.get_text(" ", strip=True) for c in cells)
        if (not first.strip()
                or "name of beneficial" in first.lower()
                or "class a" in first.lower()
                or "shares" in first.lower()
                or ("voting" in all_text.lower() and "percent" in all_text.lower())
                or re.match(r"^[\s—–\-]*$", first)):
            data_start = i + 1

    results = []

    for row in rows[data_start:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        texts = [c.get_text(" ", strip=True) for c in cells]
        name = _clean_name(texts[0])

        if not name:
            continue

        # Skip section-divider rows (e.g. "Executive Officers and Directors")
        if _SKIP_FIRST_CELL.search(name):
            continue

        # Strip trailing empty cells to find the last meaningful value
        meaningful = [t for t in texts[1:] if t.strip() not in ("", " ")]
        if not meaningful:
            continue

        entry: dict = {"name": name}

        # Total voting power % is conventionally the last column
        entry["voting_power_pct"] = _parse_pct(meaningful[-1])

        # Harvest share counts: values > 10,000 are almost certainly share counts
        share_counts = sorted(
            [_parse_shares(t) for t in texts[1:] if _parse_shares(t) and _parse_shares(t) > 10_000],
            reverse=True,
        )
        if share_counts:
            entry["largest_holding_shares"] = share_counts[0]

        results.append(entry)

    return results


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_proxy_ownership(company_name: str) -> dict:
    """
    Fetch and parse the most recent DEF 14A proxy statement for a company.

    Returns:
        {
            "company":      str,
            "cik":          str,
            "filing_date":  str,
            "filing_url":   str,
            "owners":       [{"name": str, "voting_power_pct": float|None, ...}],
        }
    or {"error": str, "owners": []} on failure.
    """
    found = _find_company(company_name)
    if not found:
        return {"error": f"No DEF 14A found on EDGAR for '{company_name}'", "owners": []}

    cik, acc_nodash, primary_doc, filing_date = found
    cik_num = str(int(cik))
    doc_url  = f"{ARCHIVES_URL}/{cik_num}/{acc_nodash}/{primary_doc}"

    try:
        resp = _get(doc_url)
    except Exception as exc:
        return {"error": f"Failed to fetch filing: {exc}", "owners": []}

    soup  = BeautifulSoup(resp.content, "lxml")
    table = _find_voting_table(soup)

    if not table:
        return {
            "company":     company_name,
            "cik":         cik,
            "filing_date": filing_date,
            "filing_url":  doc_url,
            "error":       "Could not locate beneficial ownership table in filing",
            "owners":      [],
        }

    owners = _parse_ownership_table(table)

    result: dict = {
        "company":      company_name,
        "cik":          cik,
        "filing_date":  filing_date,
        "filing_url":   doc_url,
        "owners":       owners,
    }

    if not _is_recent(filing_date):
        result["warning"] = (
            f"Most recent DEF 14A is from {filing_date[:4]}, which may predate a "
            f"corporate reorganisation. Try the current parent company name for "
            f"more recent data."
        )

    return result
