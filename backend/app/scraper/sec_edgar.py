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

SEARCH_URL      = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions"
ARCHIVES_URL    = "https://www.sec.gov/Archives/edgar/data"


# ── HTTP helpers ──────────────────────────────────────────────────────────────

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
    Searches their own 10-K / DEF 14A filings (filer = the company itself).
    Returns {cik: zero-padded-10-digit, name: str} or None.
    """
    log.info("SEC EDGAR: searching for company %r", name)
    for forms in ("10-K", "DEF 14A"):
        try:
            data = _get(SEARCH_URL, {"q": f'"{name}"', "forms": forms})
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                continue
            entity_name, cik = _parse_hit(hits[0]["_source"])
            if entity_name and cik:
                log.info("SEC EDGAR: matched %r → CIK=%s", entity_name, cik)
                return {"cik": cik, "name": entity_name}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("SEC EDGAR: company search error (%s): %s", forms, exc)

    log.warning("SEC EDGAR: company %r not found", name)
    return None


# ── Ownership filings (SC 13D / SC 13G) ──────────────────────────────────────

def fetch_ownership_filings(company_name: str, company_cik: str | None = None,
                            limit: int = 20) -> list:
    """
    Search for SC 13D / SC 13G filings where this company is the subject/issuer.
    Each filing was submitted by an investor who owns >5% of the company.
    company_cik confirms the company is at ciks[0] (issuer position), filtering
    out filings the company itself made about other entities.
    """
    log.info("SEC EDGAR: fetching SC 13D/13G filings for %r", company_name)
    try:
        data = _get(SEARCH_URL, {
            "q":         f'"{company_name}"',
            "forms":     "SC 13D,SC 13G",
            "dateRange": "custom",
            "startdt":   "2018-01-01",
            "enddt":     "2026-12-31",
        })
    except httpx.HTTPError as exc:
        log.error("SEC EDGAR: ownership search failed: %s", exc)
        return []

    hits = data.get("hits", {}).get("hits", [])[:limit]
    results: list[dict] = []
    seen_investors: set[str] = set()

    for hit in hits:
        src           = hit.get("_source", {})
        form_type     = src.get("form", "")
        display_names = src.get("display_names", [])
        ciks          = src.get("ciks", [])

        if len(display_names) < 2 or len(ciks) < 2:
            continue
        # Confirm our company is the issuer (index 0), not the filer
        if company_cik and ciks[0].zfill(10) != company_cik:
            continue

        investor_name = _normalize_investor_name(display_names[1])
        investor_cik  = ciks[1].zfill(10)

        if not investor_name or investor_name in seen_investors:
            continue
        seen_investors.add(investor_name)

        results.append({
            "investor_name":    investor_name,
            "investor_cik":     investor_cik,
            "form_type":        form_type,
            "file_date":        src.get("file_date"),
            "period_of_report": src.get("period_ending"),
            "stake_percent":    None,
            "ownership_type":   "passive" if "13G" in form_type else "active",
        })

    log.info("SEC EDGAR: found %d investors for %r", len(results), company_name)
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
