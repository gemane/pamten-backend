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
    r"\b(inc|corp|ltd|llc|plc|sa|ag|co|company)\b\.?",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    s = _SUFFIX_RE.sub("", s.lower())
    return re.sub(r"[^a-z0-9 ]", "", s).strip()


def _sim(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    seq = SequenceMatcher(None, na, nb).ratio()
    # Boost only when the query matches complete leading words of the registered name,
    # e.g. "Meta" → "meta platforms" (word boundary after "meta").
    # Avoid false boosts like "Meta" → "metabank" (no space after).
    if nb == na or nb.startswith(na + " ") or na.startswith(nb + " "):
        seq = max(seq, 0.75)
    return seq


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
    Returns (cik_padded, accession_no_dashes, primary_doc, filing_date) or None.

    Two modes depending on whether EDGAR returns a single matched company
    (unambiguous) or a list of company candidates (ambiguous):

    • Unambiguous: feed has a top-level <company-info> with the CIK and entries
      are individual filings with <accession-number>.
    • Ambiguous:   feed has no top-level <company-info>; entries are company
      listings with their CIK in the <id> field.  We score each candidate by
      name similarity and pick the one with the most recent DEF 14A.
    """
    resp = _get(BROWSE_URL, company=search_name, action="getcompany",
                type="DEF 14A", dateb="", owner="include", count="8",
                search_text="", output="atom")
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return None

    cik_el  = root.find("a:company-info/a:cik",            _NS_A)
    name_el = root.find("a:company-info/a:conformed-name", _NS_A)

    # ── Unambiguous: single company matched ────────────────────────────────────
    if cik_el is not None and (cik_el.text or "").strip():
        cik             = cik_el.text.strip().zfill(10)
        registered_name = (name_el.text or "").strip() if name_el is not None else ""

        if _sim(search_name, registered_name) < _MIN_SIM:
            return None

        # Entries are filings; grab the first (most recent) accession number
        for entry in root.findall("a:entry", _NS_A):
            content = entry.find("a:content", _NS_A)
            if content is None:
                continue
            accnum = (content.findtext("a:accession-number", "", _NS_A) or "").strip()
            if not accnum:
                continue
            acc_nodash = accnum.replace("-", "")

            # Map accession → primary document via submissions JSON
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
            break
        return None

    # ── Ambiguous: entries are company listings ────────────────────────────────
    # Each entry's <id> is "urn:tag:www.sec.gov:cik=XXXXXXXXXX".
    # Fetch submissions for each, score by name similarity, pick the one
    # with the best similarity AND a recent DEF 14A.
    candidates: list[tuple[float, str, dict]] = []  # (sim, cik, sub_json)
    for entry in root.findall("a:entry", _NS_A)[:8]:
        id_text = (entry.findtext("a:id", "", _NS_A) or "").strip()
        m = re.search(r"cik=(\d+)", id_text, re.IGNORECASE)
        if not m:
            continue
        candidate_cik = m.group(1).zfill(10)
        try:
            sub = _get_json(f"{SUBMISSIONS_URL}/CIK{candidate_cik}.json")
        except Exception:
            continue
        s = _sim(search_name, sub.get("name", ""))
        if s >= _MIN_SIM:
            candidates.append((s, candidate_cik, sub))

    if not candidates:
        return None

    # Sort by similarity DESC, then by most recent DEF 14A date DESC.
    # This breaks ties like "Meta Platforms" vs "Meta Materials" in favour of
    # the company that filed more recently (the larger / more active one).
    def _sort_key(item: tuple) -> tuple:
        s, _, sub = item
        recent  = sub.get("filings", {}).get("recent", {})
        filing  = _search_recent_for_def14a(recent)
        date    = filing[2] if filing else ""
        return (s, date)

    candidates.sort(key=_sort_key, reverse=True)

    # Return the first candidate that has a recent DEF 14A
    for s, candidate_cik, sub in candidates:
        recent  = sub.get("filings", {}).get("recent", {})
        filing  = _search_recent_for_def14a(recent)
        if filing:
            return candidate_cik, *filing

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
    r"name\s+of\s+beneficial|name\s+and\s+address|class\s+[abc]|shares|percent"
    r"|voting\s+power|executive\s+officers|other\s+[>5%]|all\s+(executive|directors)",
    re.IGNORECASE,
)

# Street address appended to a person/entity name in some filings (e.g. Amazon)
_ADDRESS_RE = re.compile(r"\s+\d+\s+[A-Za-z]")

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


def _find_voting_tables(soup) -> tuple:
    """
    Return (tables_list, is_dual_class).

    Dual-class: single table with 'voting power' + 'class a/b'.
    Single-class: all tables mentioning 'beneficial ownership' or 'percent of
    common/outstanding' with at least 3 data rows — covers both the 5%+
    holders table and the directors/officers table.
    """
    dual_candidate: object | None   = None
    single_candidates: list[object] = []

    _SINGLE_RE = re.compile(
        r"beneficial ownership|beneficially owned|percent of common"
        r"|percent of outstanding|percent of class|percent\xa0of",
        re.IGNORECASE,
    )

    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True)
        tl   = text.lower().replace("\xa0", " ")  # normalise non-breaking spaces
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue

        if "voting power" in tl and ("class a" in tl or "class b" in tl):
            dual_candidate = table
            break

        if _SINGLE_RE.search(tl):
            # The pattern must appear in the table's own first few rows, NOT just
            # in deeply nested content.  This prevents outer wrapper tables (e.g.
            # Amazon's page-level container) from matching because they inherit
            # text from inner tables.
            header_tl = " ".join(
                c.get_text(" ", strip=True).lower().replace("\xa0", " ")
                for r in rows[:5]
                for c in r.find_all(["td", "th"])
            )
            if not _SINGLE_RE.search(header_tl):
                continue

            # Only keep tables with actual data (not just footnotes).
            # Some filings pad every row with an empty first cell (​, Amazon),
            # so look for the first non-empty cell in each row rather than cell 0.
            first_cells = []
            for r in rows:
                tds = r.find_all(["td", "th"])
                for cell in tds:
                    ct = cell.get_text(" ", strip=True).replace("​", "").strip()
                    if ct:
                        first_cells.append(ct)
                        break
            has_names = any(
                len(c.split()) >= 2 and any(w[0].isupper() for w in c.split() if w)
                for c in first_cells
            )
            if has_names:
                single_candidates.append(table)

    if dual_candidate is not None:
        return [dual_candidate], True
    return single_candidates, False


def _parse_ownership_table(table, is_dual_class: bool = True) -> list[dict]:
    """Parse the beneficial ownership table into a list of owner dicts."""
    rows = table.find_all("tr")

    # Detect where data rows start by scanning for header rows.
    # Only rows with a non-empty first cell that looks like a column header
    # advance data_start.  Rows with empty first cells are continuation rows
    # (e.g. Class B row for the same person) and must NOT push data_start
    # forward, or we'd skip real data.
    data_start = 0
    for i, row in enumerate(rows[:6]):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        # Strip zero-width spaces before checking (Amazon pads cell 0 with ​)
        first = cells[0].get_text(" ", strip=True).replace("​", "").strip()
        all_text = " ".join(c.get_text(" ", strip=True) for c in cells)
        if not first:
            continue  # continuation or padded — don't advance data_start
        if ("name of beneficial" in first.lower()
                or "name" == first.lower()
                or "shares" in first.lower()
                or ("voting" in all_text.lower().replace("\xa0", " ")
                    and "percent" in all_text.lower())
                or re.match(r"^[\s—–\-]*$", first)):
            data_start = i + 1

    results = []

    for row in rows[data_start:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        # Normalise all cell text: strip zero-width spaces (used as visual padding
        # in some filings, e.g. Amazon's proxy tables)
        texts = [c.get_text(" ", strip=True).replace("​", "").strip() for c in cells]

        # Find the first non-empty cell — this is the name cell for standard tables
        # (cell 0) and also for padded tables (cell 1 in Amazon).  For Berkshire's
        # Class B continuation rows the first non-empty cell is "Class B", which
        # matches _SKIP_FIRST_CELL and triggers the continuation handler below.
        name_idx = next((i for i, t in enumerate(texts) if t), None)
        if name_idx is None:
            continue

        raw_name = _clean_name(texts[name_idx])
        # Strip appended street addresses (e.g. "Jeffrey P. Bezos 410 Terry Ave...")
        m_addr = _ADDRESS_RE.search(raw_name)
        name = raw_name[:m_addr.start()].strip() if m_addr else raw_name
        # All non-empty values after the name cell
        meaningful = [t for t in texts[name_idx + 1:] if t]

        if not name or _SKIP_FIRST_CELL.search(name):
            # Continuation / header row.  For dual-class two-row-per-person tables
            # (Berkshire), the aggregate voting power sits at meaningful[-2] in the
            # Class B row; update the previous entry.
            if results and is_dual_class and len(meaningful) >= 2:
                vp = _parse_pct(meaningful[-2])
                if vp is not None:
                    results[-1]["voting_power_pct"] = vp
                for t in meaningful:
                    v = _parse_shares(t)
                    if v and v > 10_000:
                        prev = results[-1].get("largest_holding_shares", 0) or 0
                        if v > prev:
                            results[-1]["largest_holding_shares"] = v
            continue

        if not meaningful:
            continue

        entry: dict = {"name": name}

        if is_dual_class:
            # Total voting power % is the last column in single-row formats
            # (Alphabet, Meta).  For two-row-per-person formats (Berkshire) the
            # continuation row handler above will overwrite this with the correct value.
            entry["voting_power_pct"] = _parse_pct(meaningful[-1])
        else:
            # Single-class: find the % of common stock column.
            # Real percentages are either "*" (< 1%) or have a "%" sign or are
            # decimal numbers like "7.3".  Plain integers (1, 2, … 20) without
            # a "%" are footnote reference numbers — skip them.
            pct_val = None
            for t in meaningful:
                stripped = _FOOTNOTE_RE.sub("", t).strip()
                if stripped == "*":
                    pct_val = 0.4
                    break
                if "%" in stripped:
                    pct_val = _parse_pct(stripped)
                    break
                # Accept a decimal number (X.X) as a percentage
                raw = stripped.replace(",", "")
                if "." in raw:
                    try:
                        v = float(raw)
                        if 0.0 < v < 100.0:
                            pct_val = v
                            break
                    except ValueError:
                        pass
            entry["voting_power_pct"] = pct_val

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
    tables, is_dual_class = _find_voting_tables(soup)

    if not tables:
        return {
            "company":     company_name,
            "cik":         cik,
            "filing_date": filing_date,
            "filing_url":  doc_url,
            "error":       "Could not locate beneficial ownership table in filing",
            "owners":      [],
        }

    # Parse all matching tables and merge, deduplicating by name
    seen: set[str] = set()
    owners: list[dict] = []
    for tbl in tables:
        for entry in _parse_ownership_table(tbl, is_dual_class=is_dual_class):
            if entry["name"] not in seen:
                seen.add(entry["name"])
                owners.append(entry)

    result: dict = {
        "company":            company_name,
        "cik":                cik,
        "filing_date":        filing_date,
        "filing_url":         doc_url,
        "share_class_structure": "dual_class" if is_dual_class else "single_class",
        "owners":             owners,
    }

    if not _is_recent(filing_date):
        result["warning"] = (
            f"Most recent DEF 14A is from {filing_date[:4]}, which may predate a "
            f"corporate reorganisation. Try the current parent company name for "
            f"more recent data."
        )

    return result
