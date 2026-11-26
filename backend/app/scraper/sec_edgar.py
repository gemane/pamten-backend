"""
SEC EDGAR scraper — ownership filings (SC 13D/13G) and executive data (Form 3/4).

Data source:  https://www.sec.gov/cgi-bin/browse-edgar
Manual lookup:
  Company search: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=<name>
  Submissions:    https://data.sec.gov/submissions/CIK<10-digit-cik>.json
  Tickers map:    https://www.sec.gov/files/company_tickers.json

Endpoints used:
  GET https://efts.sec.gov/LATEST/search-index         — full-text filing search
  GET https://data.sec.gov/submissions/CIK<CIK>.json  — company filing index
  GET https://www.sec.gov/Archives/edgar/data/<CIK>/<accession>/... — filing docs

Fields returned and Owlgraph mapping:
  Form 3/4 (insider ownership reports):
    reportingOwner/reportingOwnerId/rptOwnerName → person.name (normalised from LAST FIRST)
    reportingOwner/reportingOwnerRelationship/officerTitle → person.role
    isDirector / isOfficer flags                → HAS_ROLE edge type
    issuerName                                  → links to company entity
    filing index page URL                       → HAS_ROLE.source_url (provenance)
    filing date                                 → HAS_ROLE.source_date (provenance)
  SC 13D/13G (large-stake disclosures):
    percentOfClass in filing text               → ownership.stake_percent
    filer name                                  → person/entity node
    filing index page URL                       → OWNS.source_url (provenance)
    filing date                                 → OWNS.source_date (provenance)

Rate limits:
  SEC fair-access policy: max 10 requests/second per IP.
  We sleep 0.12 s between each request (~8.3 req/s).
  Required header: User-Agent must identify the application and a contact address.
  Docs: https://www.sec.gov/os/accessing-edgar-data

Data licence:
  All EDGAR filings are US federal government works — public domain (17 U.S.C. § 105).
  Bulk download explicitly permitted: https://www.sec.gov/os/accessing-edgar-data

How to verify:
  1. Find the company CIK at https://www.sec.gov/cgi-bin/browse-edgar
  2. Check https://data.sec.gov/submissions/CIK<CIK>.json for recent filings.
  3. Open the Form 3/4 filing on EDGAR and compare the parsed name/role with
     the XML at <Archives URL>/<accession>/<primary-doc>.xml.

Where the code lives (this module is the entry point, `scrape_company`):
  sec_client     — the HTTP client, retries, requests lost to the network, the
                   per-scrape and per-pass filer-record memo, CIK and URL helpers
  sec_lookup     — a name → its filer: the tickers file, EDGAR's name index,
                   the bulk name list, a person's CIKs
  sec_filer      — what a filer's own record says: country, headquarters,
                   website, SIC, former names, LEI, the annual report's cover
  sec_13dg       — an issuer's 13D/G statements (who owns it)
  sec_13dg_text  — reading a statement's cover page and text
  sec_13dg_xml   — reading a structured statement (Dec-2024 XML)
  sec_holdings   — a filer's own 13D/G statements (what it owns of others)
  sec_insiders   — Form 3/4/5 insiders, 8-K departures, an insider's holding
  sec_shares     — shares outstanding, the denominator of a stake
  sec_13f        — quarterly 13F holders and a manager's affiliated filers
"""

from dataclasses import asdict
import logging

from app.scraper import sec_13dg, sec_client, sec_filer, sec_holdings, sec_insiders, sec_lookup, sec_shares

log = logging.getLogger(__name__)


# ── Main entry point ──────────────────────────────────────────────────────────

def scrape_company(company_name: str, holdings_limit: int = sec_holdings.HOLDINGS_SCRAPE_LIMIT,
                   cik: str | None = None) -> dict | None:
    """
    Full SEC EDGAR scrape for one company.
    Returns structured dict with ownership_filings and executives, or None
    if the company is not found on EDGAR.

    With `cik` — the number the database already holds for the company — the
    filer is read by that number and the name search is skipped: an id is the
    answer a name only approximates. "SpaceX" (a trade name) and Nestlé (no
    10-K or 20-F, so the name search's indexes never list it) were found by
    neither of the name lookups, while both nodes carried the right CIK from
    Wikidata (2026-10-27). A CIK EDGAR does not know falls back to the name.
    """
    token = sec_client._failed_sections.set([])
    try:
        with sec_client.one_filer_record_per_scrape():
            return _scrape_company(company_name, holdings_limit, cik)
    finally:
        sec_client._failed_sections.reset(token)


def _scrape_company(company_name: str, holdings_limit: int, cik: str | None) -> dict | None:
    company = sec_filer.company_by_cik(cik) if cik else None
    if cik and not company:
        log.warning("SEC EDGAR: stored CIK %s unknown to EDGAR — searching %r by name", cik, company_name)
    company = company or sec_lookup.search_company(company_name)
    if not company:
        return None

    cik          = company.get("cik")
    former_names = sec_filer.fetch_former_names(cik) if cik else []
    lei          = sec_filer.fetch_company_lei(cik) if cik else None
    # who reported no position, and when: the ownership table step leaves
    # them out (findings item 159)
    ownership_exits: list = []
    ownership    = sec_13dg.fetch_ownership_filings(company_name, company_cik=cik, exit_days=ownership_exits)
    executives   = sec_insiders.fetch_executives(cik) if cik else []
    if cik:
        # the directors and officers the proxy names whom the insider window
        # misses — a board without yearly grants (findings item 115)
        from app.scraper.sec_roster import roster_insiders
        executives += roster_insiders(cik, executives)
    # What this company owns of others. Costs one submissions read for a company
    # that files no 13D/13G — almost all of them — and only fetches documents for
    # an actual institutional filer.
    holdings     = sec_holdings.fetch_filer_holdings(cik, limit=holdings_limit) if (cik and holdings_limit) else []
    # A foreign issuer's ADS CUSIP from its F-6 filings — the only CUSIP EDGAR
    # gives a filer with no 13D/G cover about itself (findings item 72).
    from app.scraper.sec_f6 import fetch_ads_cusips
    ads          = fetch_ads_cusips(cik) if cik else {}

    # Turn each insider's Form-4 holding into a stake %, class against class,
    # when the issuer's counts are known and fresh (see SharesOutstanding).
    detail = sec_shares.fetch_shares_outstanding_detail(cik) if cik else None
    shares_out = detail.total if detail else None
    # a vehicle several insiders report is one owner, not a part of each (item 111)
    vehicles = sec_insiders.split_shared_blocks(executives, detail)
    for ex in executives:
        if ex.get("holdings"):
            pick = sec_insiders.stake_for(ex["holdings"], detail)
            if pick:
                ex.update(pick)
    for v in vehicles:
        if pick := sec_insiders.stake_for({v["title"] or "": v["shares"]}, detail):
            v.update(pick)

    return {
        "cik":                company["cik"],
        "name":               company["name"],
        "lei":                lei,
        "former_names":       former_names,
        "ownership_filings":  ownership,
        "ownership_exits":    list(dict.fromkeys(ownership_exits)),
        "holdings":           holdings,
        "ads_cusips":         ads.get("cusips") or [],
        "ads_cusip_url":      ads.get("source_url"),
        "executives":         executives,
        "shared_vehicles":    vehicles,
        "shares_outstanding": shares_out,
        # dated and per class, for the insider lookups the runner adds later
        "shares_outstanding_detail": asdict(detail) if detail else None,
        # the sections a network failure emptied (findings item 41)
        "failed_sections":    list(sec_client._failed_sections.get() or []),
    }
