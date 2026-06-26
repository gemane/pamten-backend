"""
OpenCorporates scraper — official company register data from 200+ jurisdictions.
Free API, no key required for basic use. Optional OPENCORPORATES_API_KEY env var.
Rate limit: 5 req/s on free tier → 0.2s sleep between requests.
Required header: User-Agent: Pamten/1.0 contact@pamten.com
"""

import time
import logging

import httpx

log = logging.getLogger(__name__)

BASE_URL     = "https://api.opencorporates.com/v0.4"
REQUEST_DELAY = 0.2   # 5 req/s max on free tier

HEADERS = {
    "User-Agent": "Pamten/1.0 contact@pamten.com",
    "Accept":     "application/json",
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _api_key() -> str:
    """Return the optional API key from settings (empty string = free tier)."""
    try:
        from app.config import settings
        return settings.OPENCORPORATES_API_KEY or ""
    except Exception:
        return ""


def _params(extra: dict | None = None) -> dict:
    """Build query params, injecting api_token when available."""
    p: dict = {"format": "json"}
    key = _api_key()
    if key:
        p["api_token"] = key
    if extra:
        p.update(extra)
    return p


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    r = httpx.get(url, params=_params(params), headers=HEADERS, timeout=20)
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return r.json()


# ── Company search ────────────────────────────────────────────────────────────

def search_company(name: str) -> dict | None:
    """
    Search OpenCorporates for a company by name.
    Returns {jurisdiction_code, company_number, name} for the best match, or None.
    """
    log.info("OpenCorporates: searching for %r", name)
    try:
        data = _get("/companies/search", {"q": name})
    except httpx.HTTPError as exc:
        log.error("OpenCorporates: search failed for %r: %s", name, exc)
        return None

    companies = (
        data.get("results", {})
            .get("companies", [])
    )
    if not companies:
        log.warning("OpenCorporates: no results for %r", name)
        return None

    # First result is the highest-scoring match
    company = companies[0].get("company", {})
    result = {
        "jurisdiction_code": company.get("jurisdiction_code"),
        "company_number":    company.get("company_number"),
        "name":              company.get("name"),
    }
    log.info(
        "OpenCorporates: matched %r → %r (%s/%s)",
        name, result["name"], result["jurisdiction_code"], result["company_number"],
    )
    return result


# ── Company details ───────────────────────────────────────────────────────────

def fetch_company_details(jurisdiction_code: str, company_number: str) -> dict:
    """
    Fetch full company record from OpenCorporates.
    Returns a dict with registered_address, incorporation_date, company_type, status.
    """
    log.info("OpenCorporates: fetching details %s/%s", jurisdiction_code, company_number)
    try:
        data = _get(f"/companies/{jurisdiction_code}/{company_number}")
    except httpx.HTTPError as exc:
        log.error("OpenCorporates: details fetch failed: %s", exc)
        return {}

    company = data.get("results", {}).get("company", {})

    raw_addr  = company.get("registered_address") or {}
    address = {
        "street":  raw_addr.get("street_address"),
        "city":    raw_addr.get("locality"),
        "country": raw_addr.get("country"),
        "zip":     raw_addr.get("postal_code"),
    }

    return {
        "registered_address":  address,
        "incorporation_date":  company.get("incorporation_date"),
        "company_type":        company.get("company_type"),
        "status":              company.get("current_status"),
    }


# ── Officers ──────────────────────────────────────────────────────────────────

def fetch_officers(jurisdiction_code: str, company_number: str) -> list:
    """
    Fetch the officer list for a company from OpenCorporates.
    Returns a list of {name, role, start_date, end_date}.
    """
    log.info("OpenCorporates: fetching officers %s/%s", jurisdiction_code, company_number)
    try:
        data = _get(f"/companies/{jurisdiction_code}/{company_number}/officers")
    except httpx.HTTPError as exc:
        log.error("OpenCorporates: officers fetch failed: %s", exc)
        return []

    raw_officers = (
        data.get("results", {})
            .get("officers", [])
    )
    officers = []
    seen: set[str] = set()

    for item in raw_officers:
        officer = item.get("officer", {})
        name = (officer.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        officers.append({
            "name":       name,
            "role":       (officer.get("position") or "").strip(),
            "start_date": officer.get("start_date"),
            "end_date":   officer.get("end_date"),
        })

    log.info("OpenCorporates: found %d officers for %s/%s",
             len(officers), jurisdiction_code, company_number)
    return officers


# ── Main entry point ──────────────────────────────────────────────────────────

def scrape_company(company_name: str) -> dict | None:
    """
    Full OpenCorporates scrape for one company.
    Returns structured dict or None if the company is not found.
    """
    match = search_company(company_name)
    if not match:
        return None

    jcode  = match["jurisdiction_code"]
    cnum   = match["company_number"]

    details  = fetch_company_details(jcode, cnum)
    officers = fetch_officers(jcode, cnum)

    return {
        "name":               match["name"],
        "jurisdiction_code":  jcode,
        "company_number":     cnum,
        "registered_address": details.get("registered_address", {}),
        "incorporation_date": details.get("incorporation_date"),
        "company_type":       details.get("company_type"),
        "status":             details.get("status"),
        "officers":           officers,
    }
