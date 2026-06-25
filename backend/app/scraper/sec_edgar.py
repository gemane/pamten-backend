"""
SEC EDGAR scraper — ownership filings (SC 13D/13G) and proxy executives (DEF 14A).
No API key required; fully public data.
Rate limit: 10 req/s — 0.12s sleep between requests.
Required header: User-Agent: Pamten/1.0 contact@pamten.com
"""

import re
import time
import logging

import httpx

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Pamten/1.0 contact@pamten.com",
    "Accept":     "application/json",
}
REQUEST_DELAY   = 0.12          # stay comfortably under 10 req/s
SEARCH_URL      = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions"
ARCHIVES_URL    = "https://www.sec.gov/Archives/edgar/data"

# Map title keywords to canonical role strings
TITLE_ROLE_MAP = {
    "chief executive officer":  "CEO",
    "president and chief executive": "CEO",
    "chief financial officer":  "CFO",
    "chief operating officer":  "COO",
    "chief technology officer": "CTO",
    "chairman of the board":    "Chairman",
    "chairman":                 "Chairman",
    "independent director":     "Director",
    "director":                 "Director",
    "president":                "President",
    "general counsel":          "General Counsel",
    "chief legal officer":      "General Counsel",
}


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> dict:
    r = httpx.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return r.json()


def _get_text(url: str) -> str:
    r = httpx.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return r.text


# ── CIK helpers ───────────────────────────────────────────────────────────────

def _cik_from_accession(accession_no: str) -> str | None:
    """Extract the zero-padded 10-digit CIK from an EDGAR accession number."""
    # Format: XXXXXXXXXX-YY-ZZZZZZ  (first 10 chars after removing dashes = CIK)
    clean = accession_no.replace("-", "")
    if len(clean) >= 10:
        return clean[:10]
    return None


def _cik_int(cik: str) -> str:
    """Return CIK as a plain integer string (strips leading zeros), for Archives URL."""
    return str(int(cik))


# ── Company search ────────────────────────────────────────────────────────────

def search_company(name: str) -> dict | None:
    """
    Find a company's CIK and registered name on EDGAR.
    Searches their own 10-K / DEF 14A filings; the filer IS the company.
    Returns {cik: zero-padded-10-digit, name: str} or None.
    """
    log.info("SEC EDGAR: searching for company %r", name)
    for forms in ("10-K", "DEF 14A"):
        try:
            data = _get(SEARCH_URL, {"q": f'"{name}"', "forms": forms})
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                continue
            src          = hits[0]["_source"]
            entity_name  = src.get("entity_name", "").strip()
            accession_no = src.get("accession_no", "")
            cik          = _cik_from_accession(accession_no)
            if entity_name and cik:
                log.info("SEC EDGAR: matched %r → CIK=%s", entity_name, cik)
                return {"cik": cik, "name": entity_name}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("SEC EDGAR: company search error (%s): %s", forms, exc)

    log.warning("SEC EDGAR: company %r not found", name)
    return None


# ── Ownership filings (SC 13D / SC 13G) ──────────────────────────────────────

def fetch_ownership_filings(company_name: str, limit: int = 20) -> list:
    """
    Search for SC 13D / SC 13G filings that mention this company.
    Each filing was submitted by an investor who owns >5% of the company.
    Returns list of dicts with investor details; stake_percent is None
    (extracting that would require parsing each individual filing document).
    """
    log.info("SEC EDGAR: fetching SC 13D/13G filings for %r", company_name)
    try:
        data = _get(SEARCH_URL, {
            "q":           f'"{company_name}"',
            "forms":       "SC 13D,SC 13G",
            "dateRange":   "custom",
            "startdt":     "2018-01-01",
            "enddt":       "2026-12-31",
        })
    except httpx.HTTPError as exc:
        log.error("SEC EDGAR: ownership search failed: %s", exc)
        return []

    hits = data.get("hits", {}).get("hits", [])[:limit]
    results = []
    seen_investors: set[str] = set()

    for hit in hits:
        src           = hit.get("_source", {})
        investor_name = src.get("entity_name", "").strip()
        accession_no  = src.get("accession_no", "")
        form_type     = src.get("form_type", "")

        if not investor_name or investor_name in seen_investors:
            continue
        seen_investors.add(investor_name)

        investor_cik = _cik_from_accession(accession_no)
        ownership_type = "passive" if "13G" in form_type else "active"

        results.append({
            "investor_name":     investor_name,
            "investor_cik":      investor_cik,
            "form_type":         form_type,
            "file_date":         src.get("file_date"),
            "period_of_report":  src.get("period_of_report"),
            "stake_percent":     None,
            "ownership_type":    ownership_type,
        })

    log.info("SEC EDGAR: found %d investors for %r", len(results), company_name)
    return results


# ── Executives from DEF 14A ───────────────────────────────────────────────────

def fetch_executives(cik: str) -> list:
    """
    Fetch the company's most recent DEF 14A (proxy statement) and extract
    board members and named executive officers via best-effort regex parsing.
    Proxy statement formats vary widely; returns an empty list on failure.
    """
    log.info("SEC EDGAR: fetching DEF 14A for CIK=%s", cik)
    try:
        submissions = _get(f"{SUBMISSIONS_URL}/CIK{cik}.json")
    except httpx.HTTPError as exc:
        log.error("SEC EDGAR: submissions fetch failed for CIK=%s: %s", cik, exc)
        return []

    recent        = submissions.get("filings", {}).get("recent", {})
    forms         = recent.get("form",            [])
    accessions    = recent.get("accessionNumber",  [])
    primary_docs  = recent.get("primaryDocument",  [])

    # Filings are newest-first; take the first DEF 14A
    def14a_index = next(
        (i for i, f in enumerate(forms) if f == "DEF 14A"),
        None,
    )
    if def14a_index is None:
        log.info("SEC EDGAR: no DEF 14A found for CIK=%s", cik)
        return []

    accession    = accessions[def14a_index].replace("-", "")
    primary_doc  = primary_docs[def14a_index] if def14a_index < len(primary_docs) else ""
    cik_stripped = _cik_int(cik)

    if not primary_doc:
        log.warning("SEC EDGAR: DEF 14A has no primary document for CIK=%s", cik)
        return []

    doc_url = f"{ARCHIVES_URL}/{cik_stripped}/{accession}/{primary_doc}"
    log.info("SEC EDGAR: fetching DEF 14A %s", doc_url)
    try:
        html = _get_text(doc_url)
    except httpx.HTTPError as exc:
        log.error("SEC EDGAR: DEF 14A fetch failed: %s", exc)
        return []

    return _parse_executives_from_proxy(html)


def _parse_executives_from_proxy(html: str) -> list:
    """
    Best-effort extraction of executive names and roles from DEF 14A HTML.
    Strips tags, then looks for capitalized-name + title keyword pairs.
    """
    # Collapse HTML to plain text
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&(?:[a-z]+|#\d+);", " ", text)
    text = re.sub(r"\s+", " ", text)

    executives: list[dict] = []
    seen_names: set[str]   = set()

    # Longest titles first so "Chairman of the Board" beats "Chairman"
    sorted_titles = sorted(TITLE_ROLE_MAP.keys(), key=len, reverse=True)

    for title_kw in sorted_titles:
        role = TITLE_ROLE_MAP[title_kw]
        # Pattern: "Firstname [Middle] Lastname,  <optional age text>  <title>"
        pattern = (
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})"
            r"[,\s]{1,20}"
            r"(?:age\s+\d+[,\s]{1,10})?"
            r"(?:our\s+|has served as\s+)?"
            + re.escape(title_kw)
        )
        for m in re.finditer(pattern, text, re.IGNORECASE):
            name = m.group(1).strip()
            # Sanity checks: at least two words, not a generic phrase
            if not name or name in seen_names or len(name.split()) < 2:
                continue
            seen_names.add(name)
            executives.append({
                "name":  name,
                "title": title_kw.title(),
                "role":  role,
            })

    log.info("SEC EDGAR: extracted %d executives from DEF 14A", len(executives))
    return executives[:25]  # cap to avoid garbage from very long documents


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

    ownership  = fetch_ownership_filings(company_name)
    executives = fetch_executives(company["cik"]) if company.get("cik") else []

    return {
        "cik":               company["cik"],
        "name":              company["name"],
        "ownership_filings": ownership,
        "executives":        executives,
    }
