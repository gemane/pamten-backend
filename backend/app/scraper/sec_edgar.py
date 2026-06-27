"""
SEC EDGAR scraper — ownership filings (SC 13D/13G) and executive data (Form 3/4).
No API key required; fully public data.
Rate limit: 10 req/s — 0.12s sleep between requests.
Required header: User-Agent: Pamten/1.0 contact@pamten.com

Executive data comes from Form 3/4 (insider ownership reports), which are
structured XML with explicit name and title fields. This is far more reliable
than parsing DEF 14A proxy HTML.
"""

import re
import time
import html as html_lib
import logging
import xml.etree.ElementTree as ET

import httpx
from app.scraper.mapper import _ENTITY_SUFFIXES

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Pamten/1.0 contact@pamten.com",
    "Accept":     "application/json",
}
REQUEST_DELAY    = 0.12   # stay comfortably under 10 req/s
MAX_FORM4_FETCH  = 25     # max unique insiders to fetch Form 3/4 for
MAX_PERCENT_FETCH = 5     # max investors to fetch actual stake % for

SEARCH_URL      = "https://efts.sec.gov/LATEST/search-index"
BROWSE_URL      = "https://www.sec.gov/cgi-bin/browse-edgar"
SUBMISSIONS_URL = "https://data.sec.gov/submissions"
ARCHIVES_URL    = "https://www.sec.gov/Archives/edgar/data"
TICKERS_URL     = "https://www.sec.gov/files/company_tickers.json"

# Module-level cache for the tickers file (populated on first use per process)
_tickers_cache: dict | None = None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> dict:
    r = httpx.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return r.json()


def _get_text(url: str, params: dict | None = None) -> str:
    r = httpx.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return r.text


# ── CIK helpers ───────────────────────────────────────────────────────────────

def _cik_from_accession(accession_no: str) -> str | None:
    """Extract the zero-padded 10-digit CIK from an EDGAR accession number."""
    clean = accession_no.replace("-", "")
    return clean[:10] if len(clean) >= 10 else None


def _cik_int(cik: str) -> str:
    """Return CIK as a plain integer string (no leading zeros), for Archives URLs."""
    return str(int(cik))


# ── Name helpers ──────────────────────────────────────────────────────────────

def _normalize_sec_name(raw: str) -> str:
    """
    SEC Form 3/4 stores individual names as 'LAST FIRST [MIDDLE]'.
    Converts to 'First [Middle] Last' with Title Case.
    e.g. 'COOK TIMOTHY D' → 'Timothy D Cook'
         'NADELLA SATYA'  → 'Satya Nadella'
    """
    words = [w.strip(".,") for w in raw.strip().split() if w.strip(".,")]
    if len(words) >= 2:
        last  = words[0].capitalize()
        first = " ".join(w.capitalize() for w in words[1:])
        return f"{first} {last}"
    return raw.title()


def _normalize_investor_name(raw_display: str) -> str:
    """
    Given a raw EDGAR display_name entry such as:
      'Musk Elon  (CIK 0001494730)'          ← individual, no ticker
      'BlackRock Inc.  (BLK)  (CIK ...)'     ← company, has ticker
    Returns a clean investor name.

    Companies include a ticker '(BLK)' before the CIK; individuals do not.
    SEC stores individual names as 'Last First' — we flip them to 'First Last'.
    """
    has_ticker = bool(re.search(r"\([A-Z]{1,5}\)\s+\(CIK", raw_display))
    name = re.split(r"\s{2,}|\s+\(", raw_display)[0].strip()
    if not name:
        return name
    words = name.split()
    if not has_ticker and len(words) == 2 and not _ENTITY_SUFFIXES.search(name):
        return f"{words[1].capitalize()} {words[0].capitalize()}"
    return name


def _title_to_role(title: str) -> str:
    """Map an officer title string to a canonical role."""
    t = title.lower()
    if "chief executive" in t or t == "ceo":
        return "CEO"
    if "chief financial" in t or t == "cfo":
        return "CFO"
    if "chief operating" in t or t == "coo":
        return "COO"
    if "chief technology" in t or "chief technical" in t or t == "cto":
        return "CTO"
    if "general counsel" in t or "chief legal" in t:
        return "General Counsel"
    if "chairman" in t:
        return "Chairman"
    if "president" in t and "vice" not in t:
        return "President"
    return title or "Officer"


# ── Ticker-file company lookup ────────────────────────────────────────────────

def _get_tickers() -> dict:
    """Fetch (and cache) EDGAR's company_tickers.json for the process lifetime."""
    global _tickers_cache
    if _tickers_cache is None:
        log.info("SEC EDGAR: loading company_tickers.json")
        r = httpx.get(TICKERS_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        time.sleep(REQUEST_DELAY)
        _tickers_cache = r.json()
    return _tickers_cache


_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|corp|corporation|llc|llp|ltd|limited|co|company|plc|sa|ag|nv|bv|lp)\b",
    re.IGNORECASE,
)

def _ticker_normalize(name: str) -> str:
    """Lowercase, strip punctuation and legal suffixes for name comparison."""
    name = name.lower()
    name = re.sub(r"[.,]", "", name)
    name = _LEGAL_SUFFIXES.sub("", name)
    return re.sub(r"\s+", " ", name).strip()


def _lookup_in_tickers(query: str) -> dict | None:
    """
    Look up a company in EDGAR's listed-company tickers file.
    Prefers exact normalized-name matches; falls back to prefix matches,
    preferring the shortest (most specific) result.
    Returns {cik: zero-padded-10-digit, name: str} or None.
    """
    try:
        tickers = _get_tickers()
    except httpx.HTTPError as exc:
        log.warning("SEC EDGAR: tickers file fetch failed: %s", exc)
        return None

    q = _ticker_normalize(query)
    exact: list[tuple[str, str, int]] = []   # (title, cik, len)
    prefix: list[tuple[str, str, int]] = []

    for entry in tickers.values():
        title = entry.get("title", "")
        cik   = str(entry.get("cik_str", "")).zfill(10)
        norm  = _ticker_normalize(title)
        if norm == q:
            exact.append((title, cik, len(title)))
        elif norm.startswith(q):
            prefix.append((title, cik, len(title)))

    for pool in (exact, prefix):
        if pool:
            pool.sort(key=lambda x: x[2])   # shortest name = most specific
            title, cik, _ = pool[0]
            log.info("SEC EDGAR: tickers matched %r → %r (CIK=%s)", query, title, cik)
            return {"cik": cik, "name": title}

    return None


# ── EDGAR search helpers ──────────────────────────────────────────────────────

def _parse_hit(src: dict) -> tuple[str | None, str | None]:
    """
    Extract (entity_name, cik) from an EDGAR full-text search _source dict.
    Real field names: display_names, ciks, adsh (not entity_name/accession_no).
    """
    ciks = src.get("ciks", [])
    cik  = ciks[0].zfill(10) if ciks else _cik_from_accession(src.get("adsh", ""))

    display_names = src.get("display_names", [])
    entity_name   = None
    if display_names:
        entity_name = re.split(r"\s{2,}|\s+\(", display_names[0])[0].strip()

    return entity_name, cik


# ── Company search ────────────────────────────────────────────────────────────

def search_company(name: str) -> dict | None:
    """
    Find a company's CIK and registered name on EDGAR.

    Strategy:
    1. Try company_tickers.json — unambiguous for all listed public companies.
    2. Fall back to full-text search (10-K then DEF 14A) for private/foreign
       companies not in the tickers file.

    Returns {cik: zero-padded-10-digit, name: str} or None.
    """
    log.info("SEC EDGAR: searching for company %r", name)

    result = _lookup_in_tickers(name)
    if result:
        return result

    log.info("SEC EDGAR: tickers miss for %r, falling back to full-text search", name)
    for forms in ("10-K", "DEF 14A"):
        try:
            data = _get(SEARCH_URL, {"q": f'"{name}"', "forms": forms})
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                continue
            entity_name, cik = _parse_hit(hits[0]["_source"])
            if entity_name and cik:
                log.info("SEC EDGAR: full-text matched %r → CIK=%s", entity_name, cik)
                return {"cik": cik, "name": entity_name}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("SEC EDGAR: company search error (%s): %s", forms, exc)

    log.warning("SEC EDGAR: company %r not found", name)
    return None


# ── Investor info from EDGAR submissions + filing document ────────────────────

_PERCENT_PATTERNS = [
    # Standard SC 13G/13D cover page: "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW N  X.X%"
    r'percent\s+of\s+class\s+represented\s+by\s+amount\s+in\s+row\s+\d+\s+(\d{1,2}\.?\d*)\s*%',
    # Item 13 label followed by the value (some filings use "Item 13" as the header)
    r'item\s*13\.?\s*percent\s+of\s+class[^\n]*?\n[^\n]*?(\d{1,2}\.?\d*)\s*%',
    # Fallback: "percent of class" anywhere, followed within 300 chars by a percentage
    # (uses .{0,300}? instead of [^\d%]{0,300}? to not break on digits in labels)
    r'percent\s+of\s+class\s+represented.{0,300}?(\d{1,2}\.?\d*)\s*%',
]


def _plain_text(raw: str) -> str:
    """Strip HTML tags and decode entities so regex patterns match cleanly."""
    decoded = html_lib.unescape(raw)
    stripped = re.sub(r'<[^>]+>', ' ', decoded)
    return re.sub(r'\s+', ' ', stripped)


def _parse_percent_from_text(text: str) -> float | None:
    """Extract stake % from Item 13 of a SC 13D/13G filing document."""
    plain = _plain_text(text)
    for pattern in _PERCENT_PATTERNS:
        m = re.search(pattern, plain, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                val = float(m.group(1))
                if 0 < val <= 100:
                    return val
            except (ValueError, IndexError):
                pass
    return None


def _fetch_filing_index(index_url: str) -> tuple[str | None, str | None, str | None]:
    """
    Fetch an EDGAR filing index HTML page and extract:
      - investor (filer) name
      - investor CIK (zero-padded 10 digits)
      - primary document URL (for parsing stake percentage)

    The index page has separate filerDiv blocks for the subject company ("Subject")
    and the reporting persons ("Filed by"). We want the latter.
    """
    try:
        html = _get_text(index_url)
    except httpx.HTTPError as exc:
        log.debug("SEC EDGAR: index fetch failed %s: %s", index_url, exc)
        return None, None, None

    # Extract the first "(Filed by)" company name and its CIK
    name = None
    cik  = None
    m = re.search(
        r'class="companyName">\s*([^<(]+?)\s*\(Filed by\).*?CIK=(\d+)',
        html, re.DOTALL | re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip()
        cik  = m.group(2).zfill(10)

    # Extract primary document URL: first doc-table row whose type cell says SC 13
    primary_url = None
    m2 = re.search(
        r'href="(/Archives/edgar/data/\d+/[^"]+\.(?:htm|txt))"[^>]*>[^<]*</a>\s*</td>\s*<td[^>]*>SC\s*13',
        html, re.DOTALL | re.IGNORECASE,
    )
    if m2:
        primary_url = f"https://www.sec.gov{m2.group(1)}"

    return name, cik, primary_url


# ── Ownership filings (SC 13D / SC 13G) ──────────────────────────────────────

def fetch_ownership_filings(company_name: str, company_cik: str | None = None,
                            limit: int = 20) -> list:
    """
    Find SC 13D/13G large-shareholder filings where this company is the issuer.
    Uses EDGAR's company browse Atom feed (keyed by CIK), which is more reliable
    than full-text search. Each entry represents an investor holding >5%.
    Names and stake percentages are fetched from individual submissions JSONs.
    """
    if not company_cik:
        log.warning("SEC EDGAR: CIK required for ownership search; skipping %r", company_name)
        return []

    log.info("SEC EDGAR: fetching SC 13D/13G via browse for CIK=%s (%r)", company_cik, company_name)
    try:
        atom_text = _get_text(BROWSE_URL, {
            "action": "getcompany",
            "CIK":    company_cik,
            "type":   "SC 13",
            "dateb":  "",
            "owner":  "include",
            "count":  "100",  # large institutions file many outbound SC 13s; need room for inbound
            "output": "atom",
        })
    except httpx.HTTPError as exc:
        log.error("SEC EDGAR: browse request failed for CIK=%s: %s", company_cik, exc)
        return []

    try:
        root = ET.fromstring(atom_text)
    except ET.ParseError as exc:
        log.error("SEC EDGAR: Atom feed parse error: %s", exc)
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}

    # Normalise company CIK to 10-digit zero-padded form for comparisons
    norm_company_cik = company_cik.zfill(10)

    # Parse entries from Atom feed; grab filing-href for each.
    # Pre-filter outbound filings (where this company is the FILER, not the subject)
    # by reading the accession number's embedded filer-CIK directly from the feed —
    # no HTTP request needed. Investment managers (JPMorgan, BlackRock) file hundreds
    # of outbound SC 13s per year; without this filter they swamp the count limit.
    raw_entries: list[dict] = []
    for entry in root.findall("a:entry", ns):
        cat       = entry.find("a:category", ns)
        form_type = (cat.get("term") or "").strip() if cat is not None else ""
        if "13" not in form_type:
            continue

        content = entry.find("a:content", ns)
        if content is None:
            continue
        href_elem = content.find("a:filing-href", ns)
        date_elem = content.find("a:filing-date", ns)
        acc_elem  = content.find("a:accession-number", ns)
        index_url = (href_elem.text or "").strip() if href_elem is not None else ""
        file_date = (date_elem.text or "").strip() if date_elem is not None else None
        accession = (acc_elem.text  or "").strip() if acc_elem  is not None else ""

        if not index_url:
            continue

        # Skip filings submitted BY this company (outbound) — filer CIK is the
        # first 10 digits of the accession number (after stripping dashes).
        if accession:
            filer_cik = accession.replace("-", "")[:10].zfill(10)
            if filer_cik == norm_company_cik:
                continue

        raw_entries.append({
            "index_url": index_url,
            "form_type": form_type,
            "file_date": file_date,
        })

    # Fetch filing index pages; deduplicate by investor CIK (Atom is most-recent-first)
    seen_investor_ciks: set[str] = set()
    if company_cik:
        seen_investor_ciks.add(company_cik)   # skip the company's own filings
    enriched: list[dict] = []

    for raw in raw_entries:
        if len(enriched) >= limit:
            break
        name, cik, primary_url = _fetch_filing_index(raw["index_url"])
        if not name or not cik:
            continue
        if cik in seen_investor_ciks:
            continue
        seen_investor_ciks.add(cik)
        enriched.append({
            "investor_name": name,
            "investor_cik":  cik,
            "form_type":     raw["form_type"],
            "file_date":     raw["file_date"],
            "primary_url":   primary_url,
        })

    # Fetch stake percentages for the top N investors
    results: list[dict] = []
    for i, inv in enumerate(enriched):
        pct = None
        if i < MAX_PERCENT_FETCH and inv.get("primary_url"):
            try:
                text = _get_text(inv["primary_url"])
                pct  = _parse_percent_from_text(text)
            except httpx.HTTPError:
                pass

        log.info("SEC EDGAR: investor %r (CIK=%s) stake=%s", inv["investor_name"], inv["investor_cik"], pct)
        results.append({
            "investor_name":    inv["investor_name"].title(),
            "investor_cik":     inv["investor_cik"],
            "form_type":        inv["form_type"],
            "file_date":        inv["file_date"],
            "period_of_report": None,
            "stake_percent":    pct,
            "ownership_type":   "passive" if "13G" in inv["form_type"] else "active",
        })

    log.info("SEC EDGAR: found %d investors for CIK=%s", len(results), company_cik)
    return results


# ── Executives from Form 3/4 (structured XML) ────────────────────────────────

def _parse_form34_xml(xml_text: str) -> dict | None:
    """
    Parse a Form 3 or Form 4 XML document.
    Returns {name, title, role} if the reporting person is an officer or director,
    None otherwise.

    Form 3/4 XML schema (key fields):
      reportingOwner/reportingOwnerId/rptOwnerName       — person's name
      reportingOwner/reportingOwnerRelationship/isOfficer   — "1" or "0"
      reportingOwner/reportingOwnerRelationship/isDirector  — "1" or "0"
      reportingOwner/reportingOwnerRelationship/officerTitle — e.g. "Chief Executive Officer"
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    owner = root.find(".//reportingOwner")
    if owner is None:
        return None

    name_elem = owner.find(".//rptOwnerName")
    if name_elem is None or not (name_elem.text or "").strip():
        return None

    rel = owner.find("reportingOwnerRelationship")
    if rel is None:
        return None

    is_director   = (rel.findtext("isDirector")  or "0").strip() == "1"
    is_officer    = (rel.findtext("isOfficer")    or "0").strip() == "1"
    officer_title = (rel.findtext("officerTitle") or "").strip()

    if not (is_director or is_officer):
        return None

    name = _normalize_sec_name(name_elem.text.strip())
    role = _title_to_role(officer_title) if is_officer else "Director"

    return {"name": name, "title": officer_title, "role": role}


def fetch_executives(cik: str) -> list:
    """
    Extract executives and directors from Form 3/4 filings (insider ownership reports).
    These are machine-readable XML with explicit name and title fields —
    no HTML parsing or heuristics required.

    Strategy: read the most recent Form 3/4 for each unique insider (by filer CIK),
    up to MAX_FORM4_FETCH unique insiders.

    EDGAR cross-indexes Form 3/4 filings under the issuer's CIK in Archives, even
    when the accession number starts with a filing agent's CIK. Always use the
    issuer CIK (the `cik` argument) to build the Archives URL.
    """
    log.info("SEC EDGAR: fetching Form 3/4 for CIK=%s", cik)
    try:
        submissions = _get(f"{SUBMISSIONS_URL}/CIK{cik}.json")
    except httpx.HTTPError as exc:
        log.error("SEC EDGAR: submissions fetch failed for CIK=%s: %s", cik, exc)
        return []

    recent       = submissions.get("filings", {}).get("recent", {})
    forms        = recent.get("form",           [])
    accessions   = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    # Collect one filing per unique filer CIK (newest first = most current title)
    seen_filer_ciks: set[str] = set()
    to_fetch: list[dict] = []
    issuer_cik_int = _cik_int(cik)

    for i, form in enumerate(forms):
        if form not in ("3", "4", "3/A", "4/A"):
            continue
        accession   = accessions[i]   if i < len(accessions)   else ""
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        # primaryDocument is sometimes prefixed with an XSLT stylesheet dir
        # (e.g. "xslF345X06/form4.xml") which serves HTML, not raw XML.
        # Strip any leading xsl.../  to get the actual document filename.
        primary_doc = primary_doc.split("/")[-1] if "/" in primary_doc else primary_doc
        filer_cik   = accession.replace("-", "")[:10]

        if not filer_cik or filer_cik in seen_filer_ciks or not primary_doc:
            continue
        seen_filer_ciks.add(filer_cik)
        to_fetch.append({
            "accession":   accession.replace("-", ""),
            "primary_doc": primary_doc,
        })
        if len(to_fetch) >= MAX_FORM4_FETCH:
            break

    executives: list[dict] = []
    seen_names: set[str]   = set()

    for filing in to_fetch:
        # Use the issuer's CIK for the Archives path (not the accession filer CIK)
        url = f"{ARCHIVES_URL}/{issuer_cik_int}/{filing['accession']}/{filing['primary_doc']}"
        try:
            xml_text = _get_text(url)
        except httpx.HTTPError as exc:
            log.debug("SEC EDGAR: Form 3/4 fetch failed %s: %s", url, exc)
            continue

        result = _parse_form34_xml(xml_text)
        if not result:
            continue
        name = result["name"]
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        executives.append(result)
        log.debug("SEC EDGAR: insider %s (%s)", name, result["role"])

    log.info("SEC EDGAR: found %d executives from Form 3/4 for CIK=%s",
             len(executives), cik)
    return executives


# ── Main entry point ──────────────────────────────────────────────────────────

def scrape_company(company_name: str) -> dict | None:
    """
    Full SEC EDGAR scrape for one company.
    Returns structured dict with ownership_filings and executives, or None
    if the company is not found on EDGAR.
    """
    company = search_company(company_name)
    if not company:
        return None

    ownership  = fetch_ownership_filings(company_name, company_cik=company.get("cik"))
    executives = fetch_executives(company["cik"]) if company.get("cik") else []

    return {
        "cik":               company["cik"],
        "name":              company["name"],
        "ownership_filings": ownership,
        "executives":        executives,
    }
