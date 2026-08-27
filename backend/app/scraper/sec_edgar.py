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
"""

import re
import time
import threading
import html as html_lib
import unicodedata
import logging
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

import httpx
from app.scraper.mapper import _ENTITY_SUFFIXES, derive_ownership_type

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Owlgraph/1.0 contact@owlgraph.org",
    "Accept":     "application/json",
}
REQUEST_DELAY    = 0.12   # stay comfortably under 10 req/s
MAX_FORM4_FETCH  = 25     # max unique insiders to fetch Form 3/4 for

HOLDINGS_DEFAULT_LIMIT = 100    # subjects for an explicit holdings run (CLI)
# Default for a normal company scrape. Cheap where it matters: a filer whose
# recent filings are live stakes needs one fetch each, so 25 subjects is ~25
# requests. The look-back cap only bites on a filer that has exited positions.
HOLDINGS_SCRAPE_LIMIT  = 25
HOLDINGS_MAX_LOOKBACK  = 3      # earlier filings to check when the latest is an exit
# EDGAR splits a prolific filer's index into pages: `filings.recent` plus an
# archive list. Vanguard's old CIK has 13 pages covering back to 1999, so they are
# read lazily, newest page first, and only while the fetch budget lasts.
HOLDINGS_MAX_ARCHIVE_PAGES = 4

_HOLDING_FORMS = ("SCHEDULE 13G", "SCHEDULE 13D", "SC 13G", "SC 13D")


SEARCH_URL      = "https://efts.sec.gov/LATEST/search-index"
BROWSE_URL      = "https://www.sec.gov/cgi-bin/browse-edgar"
SUBMISSIONS_URL = "https://data.sec.gov/submissions"
ARCHIVES_URL    = "https://www.sec.gov/Archives/edgar/data"
TICKERS_URL     = "https://www.sec.gov/files/company_tickers.json"
COMPANYCONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept"

# Module-level cache for the tickers file (populated on first use per process)
_tickers_cache: dict | None = None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

# One pooled client for every SEC request, rather than httpx.get() per call.
# Establishing a connection to sec.gov costs ~60 s on this host; reusing it drops
# each subsequent request to ~10 ms. Every SEC scrape paid that per request —
# measured at 315 s to read 5 filings, ~0.01 s each once the connection is shared.
# Same pattern as scraper/geocode.py and db/arcadedb.py.
_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    headers=HEADERS,
                    # Short connect timeout on purpose: DNS returns IPv6 first and
                    # a host whose IPv6 route to sec.gov is blackholed waits out this
                    # timeout per address before falling back to IPv4. httpx has no
                    # Happy Eyeballs, so this bounds the stall (measured on the dev
                    # box: 30 s x 2 IPv6 addresses = 60 s per new connection).
                    timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=10.0),
                )
    return _client


def close_client() -> None:
    """Release the pooled connection (called on app shutdown)."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def _get(url: str, params: dict | None = None) -> dict:
    r = _get_client().get(url, params=params)
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return r.json()


def _get_text(url: str, params: dict | None = None) -> str:
    r = _get_client().get(url, params=params)
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


def _cik10(cik: str) -> str:
    """CIK zero-padded to 10 digits, which is what the submissions endpoint wants.

    Callers had drifted: some padded, some passed the raw value, and an unpadded
    CIK simply 404s. Normalising in one place removes the difference.

    A non-numeric value is passed through untouched rather than raising. Padding is
    a convenience, not a validator, and turning an odd input into an exception here
    would surface as a silent None two frames up, where the callers swallow errors.
    """
    try:
        return str(int(cik)).zfill(10)
    except (TypeError, ValueError):
        return str(cik)


def _submissions(cik: str) -> dict:
    """The EDGAR submissions document for a CIK.

    Deliberately NOT cached. Several helpers read this same document — the filer's
    name, former names, LEI and country — so caching looks like an easy saving of
    two or three requests per scrape. It is not worth it: the document also carries
    `filings.recent`, and a process-lifetime cache would serve a later scrape an
    earlier one's filings, silently missing new ones until a restart. Metadata
    tolerates staleness; filings do not, and they share a URL.

    (Tried it. The SEC holdings tests failed immediately with one test seeing
    another's filings — the same bug in miniature.)
    """
    return _get(f"{SUBMISSIONS_URL}/CIK{_cik10(cik)}.json")


#: SEC puts US states and foreign countries in the *same* two-letter field, so a
#: state has to be recognised rather than assumed to be a country.
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split())


def sec_country(submissions: dict) -> str | None:
    """ISO-2 country for a filer, or None when EDGAR cannot say.

    Incorporation first, business address second, and the order is the whole
    point. A foreign filer's EDGAR business address is often its US filing office
    — DEUTSCHE BANK AKTIENGESELLSCHAFT lists New York — so trusting the address
    would move German banks to the United States. Wrong data is worse than the
    blank it replaces, so a filer with only a US address and no stated
    incorporation returns None rather than a guess.
    """
    from app.scraper.maintenance import nationality_to_iso2

    inc_code = (submissions.get("stateOfIncorporation") or "").strip().upper()
    inc_name = (submissions.get("stateOfIncorporationDescription") or "").strip()
    if inc_code in _US_STATES:
        return "US"
    if inc_name:
        if code := nationality_to_iso2(inc_name):
            return code
    business = (submissions.get("addresses") or {}).get("business") or {}
    if name := (business.get("country") or "").strip():
        return nationality_to_iso2(name)
    return None


def sec_headquarters(submissions: dict) -> dict | None:
    """The filer's business address as a HEADQUARTERS — where it is run.

    EDGAR's `addresses.business` is the office the company gives the SEC: a real
    street address, "790 N Water Street, Milwaukee, WI 53202". `sec_country`
    deliberately refuses to read a *domicile* out of it, because a foreign filer
    often files through a US office and Deutsche Bank would become American. That
    refusal is about the wrong question. As the place a company is **run** the
    same address is exactly right, and we were fetching it on every scrape and
    throwing it away — 40 of 43 SEC companies in the graph had no headquarters at
    all while EDGAR held their street address.

    Returns ``{"address", "city", "country"}`` — country from the same two-letter
    field `sec_country` reads, where a US state code means the United States and
    anything else is one of SEC's own foreign codes. None when there is no
    address, or when it is too sparse to place.
    """
    from app.scraper.maintenance import nationality_to_iso2

    business = (submissions.get("addresses") or {}).get("business") or {}
    street = " ".join(p for p in (
        (business.get("street1") or "").strip(),
        (business.get("street2") or "").strip(),
    ) if p).strip()
    city = (business.get("city") or "").strip()
    state = (business.get("stateOrCountry") or "").strip().upper()

    country = None
    if named := (business.get("country") or "").strip():
        country = nationality_to_iso2(named)
    if not country and state:
        # A US state code means the office is in the United States. Any other code
        # is one of SEC's foreign codes, whose descriptions we resolve by name.
        country = "US" if state in _US_STATES else nationality_to_iso2(
            (business.get("stateOrCountryDescription") or "").strip())

    if not (city or street):
        return None

    postcode = (business.get("zipCode") or "").strip()
    parts = [p for p in (street, city,
                         state if state in _US_STATES else "",
                         postcode, country or "") if p]
    # The assembled string for display, AND the parts for a structured geocode —
    # EDGAR gives them separately and Nominatim takes them separately, so there is
    # no reason to flatten and re-parse.
    return {"address": ", ".join(parts), "city": city or None, "country": country,
            "street": street or None, "postcode": postcode or None,
            "state": state if state in _US_STATES else None}


def fetch_filer_headquarters(cik: str) -> dict | None:
    """The filer's headquarters from EDGAR, or None if it cannot be determined."""
    try:
        return sec_headquarters(_submissions(cik))
    except Exception as exc:  # noqa: BLE001 - an address is a nicety, not worth aborting a scrape
        log.warning("SEC EDGAR: address lookup failed for CIK=%s: %s", cik, exc)
        return None


def fetch_filer_country(cik: str) -> str | None:
    """The filer's country from EDGAR, or None if it cannot be determined."""
    try:
        return sec_country(_submissions(cik))
    except Exception as exc:  # noqa: BLE001 - a country is a nicety, not worth aborting a scrape
        log.warning("SEC EDGAR: country lookup failed for CIK=%s: %s", cik, exc)
        return None


def _filing_index_url(cik: str, accession: str) -> str | None:
    """
    Canonical, human-readable EDGAR filing index page for a filing, e.g.
    .../Archives/edgar/data/320193/000110465924021466/0001104659-24-021466-index.htm

    Preferred as a provenance link over a primary document: it always renders
    as a readable page (metadata + every document in the filing), regardless of
    whether the primary doc is HTML, .txt, or raw Form 3/4 XML.
    """
    if not cik or not accession:
        return None
    acc_nodash = accession.replace("-", "")
    return f"{ARCHIVES_URL}/{_cik_int(cik)}/{acc_nodash}/{accession}-index.htm"


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
        r = _get_client().get(TICKERS_URL)
        r.raise_for_status()
        time.sleep(REQUEST_DELAY)
        _tickers_cache = r.json()
    return _tickers_cache


#: Legal-form and filler words that carry no company identity. The single
#: source for BOTH name-comparison helpers in this module: `_LEGAL_SUFFIXES`
#: (ticker matching) and `_significant_tokens` (issuer verification) — two
#: shapes of the same knowledge, which drifted apart when they were two lists.
_LEGAL_FORM_WORDS = (
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "ltd", "limited", "llc", "lp", "llp", "plc", "sa", "nv", "se", "ag", "ab",
    "as", "asa", "spa", "gmbh", "bv", "kk", "sarl", "srl",
    "group", "holding", "holdings",
)

_LEGAL_SUFFIXES = re.compile(
    r"\b(" + "|".join(_LEGAL_FORM_WORDS) + r")\b",
    re.IGNORECASE,
)

def _ticker_normalize(name: str) -> str:
    """Lowercase, strip punctuation and legal suffixes for name comparison."""
    name = name.lower()
    name = re.sub(r"[.,]", "", name)
    name = _LEGAL_SUFFIXES.sub("", name)
    return re.sub(r"\s+", " ", name).strip()


# Minimum SequenceMatcher ratio for a match to be accepted.
# Rejects EFTS false positives (e.g. a Chesapeake Corp 10-K that merely
# *mentions* Nestlé in its text) and overly loose prefix matches.
_MIN_NAME_SIMILARITY = 0.55

def _name_similarity(a: str, b: str) -> float:
    """Normalized similarity [0, 1] between two company names after stripping suffixes."""
    na = _ticker_normalize(a)
    nb = _ticker_normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


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
            sim = _name_similarity(query, title)
            if sim < _MIN_NAME_SIMILARITY:
                log.warning(
                    "SEC EDGAR: tickers match rejected (similarity %.2f < %.2f): %r vs %r",
                    sim, _MIN_NAME_SIMILARITY, query, title,
                )
                continue
            log.info("SEC EDGAR: tickers matched %r → %r (CIK=%s, sim=%.2f)", query, title, cik, sim)
            return {"cik": cik, "name": title}

    return None


# ── EDGAR company-name index lookup (second vector) ───────────────────────────

def _lookup_edgar_by_name(name: str) -> dict | None:
    """
    Second vector: EDGAR's registered company-name index via browse-edgar.

    Unlike the EFTS full-text search, browse-edgar only returns companies whose
    *registered name* starts with the query — filing text that merely mentions
    the company never shows up here.  This correctly returns 0 results for
    foreign companies not registered with the SEC (Nestlé, Samsung, etc.) even
    though their names may appear in other filers' filings.

    The Atom feed has a server-side bug that loses the company name from <title>,
    so we extract the CIK from <id> and verify the official name from the
    submissions JSON.
    """
    for form_type in ("10-K", "20-F"):
        try:
            resp = _get_client().get(BROWSE_URL, params={
                "company":     name,
                "action":      "getcompany",
                "type":        form_type,
                "dateb":       "",
                "owner":       "include",
                "count":       "5",
                "search_text": "",
                "output":      "atom",
            }, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
        except httpx.HTTPError as exc:
            log.debug("SEC EDGAR: browse-edgar (%s) failed for %r: %s", form_type, name, exc)
            continue

        try:
            root = ET.fromstring(resp.content)
        except (ET.ParseError, TypeError):
            continue
        ns_a = {"a": "http://www.w3.org/2005/Atom"}

        # Collect all candidates, then pick highest similarity so we don't
        # accidentally prefer a subsidiary that appears before the parent company.
        best_sim  = 0.0
        best_match: dict | None = None

        for entry in root.findall("a:entry", ns_a):
            id_text = (entry.findtext("a:id", "", ns_a) or "").strip()
            m = re.search(r"cik=(\d+)", id_text, re.IGNORECASE)
            if not m:
                continue
            cik = m.group(1).zfill(10)

            # Official company name from submissions (one extra request, but
            # browse-edgar's Atom feed loses the name due to a server-side bug)
            try:
                sub  = _get(f"{SUBMISSIONS_URL}/CIK{cik}.json")
                registered_name = sub.get("name", "")
            except httpx.HTTPError:
                continue

            if not registered_name:
                continue

            s = _name_similarity(name, registered_name)
            log.debug(
                "SEC EDGAR: browse-edgar candidate (sim=%.2f): %r vs %r",
                s, name, registered_name,
            )
            if s > best_sim:
                best_sim   = s
                best_match = {"cik": cik, "name": registered_name}

        if best_match and best_sim >= _MIN_NAME_SIMILARITY:
            log.info(
                "SEC EDGAR: browse-edgar matched %r → %r (CIK=%s, sim=%.2f, form=%s)",
                name, best_match["name"], best_match["cik"], best_sim, form_type,
            )
            return best_match

    log.info("SEC EDGAR: browse-edgar found no registered match for %r", name)
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

    Three-vector strategy:
    1. company_tickers.json      — fast, unambiguous for all US-listed companies.
    2. browse-edgar name index   — EDGAR's registered company-name search; correctly
                                   returns nothing for companies not on EDGAR
                                   (Nestlé, Samsung…) since their names never appear
                                   as filer names, only inside other filings' text.
    3. EFTS full-text search     — last resort, guarded by name-similarity check.

    Returns {cik: zero-padded-10-digit, name: str} or None.
    """
    log.info("SEC EDGAR: searching for company %r", name)

    result = _lookup_in_tickers(name)
    if result:
        return result

    log.info("SEC EDGAR: tickers miss for %r, trying browse-edgar name index", name)
    result = _lookup_edgar_by_name(name)
    if result:
        return result

    log.info("SEC EDGAR: browse-edgar miss for %r, falling back to EFTS full-text search", name)
    for forms in ("10-K", "DEF 14A"):
        try:
            data = _get(SEARCH_URL, {"q": f'"{name}"', "forms": forms})
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                continue
            entity_name, cik = _parse_hit(hits[0]["_source"])
            if entity_name and cik:
                sim = _name_similarity(name, entity_name)
                if sim < _MIN_NAME_SIMILARITY:
                    log.warning(
                        "SEC EDGAR: EFTS match rejected (similarity %.2f < %.2f): %r vs %r",
                        sim, _MIN_NAME_SIMILARITY, name, entity_name,
                    )
                    continue
                log.info("SEC EDGAR: full-text matched %r → CIK=%s (sim=%.2f)", entity_name, cik, sim)
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


#: Cover-page rows 5–8 (13G) / 7–10 (13D). Row labels are stable across both.
_POWER_ROWS = {
    "sole_voting":       r'sole\s+voting\s+power',
    "shared_voting":     r'shared\s+voting\s+power',
    "sole_dispositive":  r'sole\s+dispositive\s+power',
    "shared_dispositive": r'shared\s+dispositive\s+power',
}


def _parse_power_rows(text: str) -> dict:
    """Share counts from the cover page's four power rows.

    These are what make a 13D honest. "Beneficial ownership" is about power,
    not property: a member of a voting group reports the WHOLE group's shares
    in row 11, so summing row 13 percentages across a group counts the same
    shares once per member. AB InBev summed to 109.9% that way — Altria and the
    Stichting each reporting the same billion shares.

    Sole dispositive power is what the filer can actually sell: its own stake.
    Altria's cover reads sole-voting 0, shared-voting 1,020,598,157, and
    sole-dispositive 159,121,937 — the last is Altria's real 8.1%, and the
    first is the Voting Agreement bloc it votes within.

    Missing rows are absent from the result rather than zero: "the filing did
    not say" and "the filing said none" must not be confused, since a zero
    would otherwise read as a genuine 0% stake.
    """
    plain = _plain_text(text)
    out: dict = {}
    for key, label in _POWER_ROWS.items():
        m = re.search(label + r'[^0-9\-]{0,40}([\d,]+)', plain, re.IGNORECASE)
        if not m:
            continue
        try:
            out[key] = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
    return out


def _shares_outstanding(text: str) -> int | None:
    """The denominator, from the filing's own footnote ("based on a total of
    N shares issued and outstanding"). Filings state it so the percentages can
    be checked; we use it to turn a share count into a percentage."""
    plain = _plain_text(text)
    m = re.search(r'based\s+(?:up)?on\s+a?\s*total\s+of\s+([\d,]{7,})', plain, re.IGNORECASE)
    if not m:
        m = re.search(r'([\d,]{9,})\s+(?:voting\s+)?shares?\s+issued\s+and\s+outstanding',
                      plain, re.IGNORECASE)
    if not m:
        return None
    try:
        n = int(m.group(1).replace(",", ""))
        return n if n > 0 else None
    except ValueError:
        return None


def _own_stake_and_voting(text: str, reported_pct: float | None) -> tuple:
    """Split a 13D/G cover into (own stake %, voting-bloc %).

    Returns the filer's OWN holding as the stake and the reported row-13
    figure as voting power whenever the two genuinely differ — i.e. when the
    filer is part of a group. For a lone filer sole-dispositive equals the
    aggregate and there is no bloc, so voting comes back None and nothing
    about the common case changes.
    """
    return _split_stake(_parse_power_rows(text), _shares_outstanding(text), reported_pct)


def _split_stake(rows: dict, total: int | None, reported_pct: float | None) -> tuple:
    """The stake/voting split over already-extracted numbers.

    Separated from the text parsing so the XML path can reuse the judgement
    without going near a regex; `_own_stake_and_voting` above is now a thin
    wrapper that supplies the numbers from an HTML cover page.
    """
    sole_disp = rows.get("sole_dispositive")
    shared_vote = rows.get("shared_voting")
    if sole_disp is None or not shared_vote:
        return reported_pct, None
    if sole_disp >= shared_vote:
        return reported_pct, None          # no group: the filer holds it all

    if sole_disp == 0:
        # Everything this filer holds, it holds jointly — BRC S.à.r.l. can
        # dispose of nothing alone; its shares sit in the Stichting it co-owns
        # with EPS. There is no individual stake to state, and 0.0 would read
        # as "owns nothing" — the opposite of the truth. Unknown, plus the
        # bloc it votes in; `unknown_owners` in the ownership summary already
        # accounts for owners whose share we cannot put a number on.
        return None, reported_pct

    if not total:
        # The bloc is real but the denominator is unknown. Keeping the bloc
        # figure as the stake is exactly what produced the >100% sums, so
        # state only what is certain.
        return None, reported_pct
    own_pct = round(sole_disp / total * 100, 4)
    return own_pct, reported_pct


def _plain_text(raw: str) -> str:
    """Strip HTML tags and decode entities so regex patterns match cleanly."""
    decoded = html_lib.unescape(raw)
    stripped = re.sub(r'<[^>]+>', ' ', decoded)
    return re.sub(r'\s+', ' ', stripped)


# SC 13G/13D Item 8 "Type of Reporting Person" codes that mean an individual.
# All other codes (CO, PN, IA, IC, BK, BD, SA, OO, HC, EP, GP …) are entities.
_INDIVIDUAL_CODES = {"IN"}

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


def _parse_issuer_from_text(text: str) -> str | None:
    """Extract the issuer's name from a SC 13D/13G cover page.

    Every 13D/G cover opens with the company whose shares are being reported:
    "Eve Holding, Inc. (Name of Issuer)". This is the only field on the whole
    filing that reliably says WHO the stake is in — the EDGAR index metadata
    does not: Embraer's agent filed its Eve Holding 13D/A with EMBRAER S.A. in
    the SUBJECT COMPANY header, so both the index page and the SGML header
    named the wrong company, and only the cover page told the truth.
    """
    plain = _plain_text(text)
    m = re.search(r'([^()]{2,100}?)\s*\(\s*Name\s+of\s+Issuer\s*\)', plain,
                  re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip()
    # The capture is greedy backwards into the boilerplate; keep the tail after
    # the last sentence-ish break so "…Act of 1934 Eve Holding, Inc." trims to
    # the company name alone.
    name = re.split(r'(?:\*|\d{4}|No\.\s*\d*\)?|schedule\s+13[dg](?:/a)?)\s+',
                    name, flags=re.IGNORECASE)[-1].strip(' .*')
    # Text-format filings underline the name with a rule of dashes; drop any
    # trailing run of punctuation ("EMBRAER S.A. ------------" -> "EMBRAER S.A.").
    name = re.sub(r'[\s\-_=~.]{3,}$', '', name).strip()
    return name or None


#: The issuer check filters tokens rather than substituting substrings, and
#: additionally ignores pure filler and depositary-receipt words that the
#: ticker matcher has no reason to strip.
_NAME_NOISE = frozenset(_LEGAL_FORM_WORDS) | {"the", "of", "and", "adr", "ads"}


def _significant_tokens(name: str) -> set:
    # Fold diacritics first: "Nestlé" must tokenize to "nestle", not split on
    # the é into a stump that matches nothing — that would silently reject
    # every legitimate filing about an accented-name company.
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    tokens = re.split(r"[^a-z0-9]+", folded.lower())
    return {t for t in tokens if t and t not in _NAME_NOISE}


def _issuer_matches(company_names, issuer_name: str | None) -> bool:
    """Does the filing's cover-page issuer plausibly mean the scraped company?

    Deliberately tolerant: it exists to catch a filing about a DIFFERENT company
    ("Eve Holding" vs "Embraer"), not to police spelling. Sharing one
    significant token with ANY known name of the company is agreement —
    "Anheuser-Busch InBev SA/NV" vs "Anheuser-Busch InBev", "EMBRAER S.A." vs
    "Embraer". `company_names` may be one name or an iterable of them: the
    caller passes the current name plus EDGAR's formerNames, because a rename
    keeps the CIK and the older covers carry the old name — a scrape of "Meta
    Platforms" must not throw away a 13G whose cover says "Facebook, Inc".

    No issuer extracted (None) is also agreement: a positive mismatch is the
    only safe ground to throw a filing away, and old text-format filings may
    not parse.
    """
    if not issuer_name:
        return True
    if isinstance(company_names, str):
        company_names = [company_names]
    theirs = _significant_tokens(issuer_name)
    if not theirs:
        return True
    seen_any = False
    for name in company_names:
        ours = _significant_tokens(name)
        if not ours:
            continue
        seen_any = True
        if ours & theirs:
            return True
    return not seen_any


def _parse_class_title_from_text(text: str) -> str | None:
    """The security a cover page's percentages refer to.

    Every 13D/G cover names it right under the issuer: "Ordinary Shares,
    without nominal value (Title of Class of Securities)". Pre-2024 filings
    have no XML, so this is the only way to know whether two percentages share
    a denominator.
    """
    plain = _plain_text(text)
    m = re.search(r'([^()]{2,120}?)\s*\(\s*Title\s+of\s+Class\s+of\s+Securities\s*\)',
                  plain, re.IGNORECASE)
    if not m:
        return None
    title = m.group(1).strip()
    # The capture reaches back through the issuer line; keep only what follows
    # the "(Name of Issuer)" marker when it is there.
    title = re.split(r'\(\s*Name\s+of\s+Issuer\s*\)', title, flags=re.IGNORECASE)[-1]
    title = re.sub(r'[\s\-_=~.]{3,}$', '', title).strip(' .,*')
    return title or None


def _parse_reporter_type_from_text(text: str) -> bool | None:
    """
    Extract Item 8 'Type of Reporting Person' from a SC 13D/13G filing.
    Returns True if the filer is an individual (code IN), False if it is
    a legal entity (CO, PN, IA, IC, BK, …), or None if the field is absent.
    This is authoritative — no name heuristics needed.
    """
    plain = _plain_text(text)
    m = re.search(
        r'type\s+of\s+reporting\s+person[^a-z]{0,80}'
        r'\b(IN|CO|PN|IA|IC|BK|BD|SA|OO|HC|EP|GP|LP)\b',
        plain, re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1).upper() in _INDIVIDUAL_CODES
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
        name = html_lib.unescape(m.group(1).strip())
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
            # "SC", not "SC 13": EDGAR matches this as a PREFIX, and the
            # December-2024 modernization renamed the forms from "SC 13G" to
            # "SCHEDULE 13G". The two sets are disjoint, so "SC 13" quietly
            # stopped returning anything filed after early 2024 — on Apple, the
            # newest hit was 2024-02-14 while the company had filings from 2026.
            # "SC" spans both; the SC TO-*/SC 14* noise it also matches is
            # dropped by the `"13" not in form_type` test below, before any
            # request is spent on it.
            "type":   "SC",
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
            "accession": accession,      # builds the structured-XML URL
        })

    # One pass per filing: fetch, verify, then claim the investor's slot.
    #
    # This used to be two passes, enrichment then verification, with the dedup
    # claim made in the first. That let a filing dropped for naming the wrong
    # issuer still burn its investor's CIK, so a later, correct filing by the
    # same investor was silently skipped. The claim now happens only after the
    # filing has been verified, which requires it to be in the same loop.
    seen_investor_ciks: set[str] = set()
    # The company filing about itself — compared zero-padded, because the
    # investor CIKs below are zfill(10) and an unpadded entry never matched.
    seen_investor_ciks.add(norm_company_cik)

    # Every name this CIK has filed under: renames keep the CIK, and covers
    # from before a rename carry the old name. Fetched at most once, and only
    # when a filing actually needs verifying.
    known_names: list[str] | None = None

    results: list[dict] = []
    for raw in raw_entries:
        if len(results) >= limit:
            break

        xml = None
        if _is_structured(raw["form_type"]) and raw.get("accession"):
            xml = _fetch_13dg_xml(company_cik, raw["accession"])

        if xml:
            # The XML names the filer, so the index page is not needed at all —
            # one request instead of two — and `_filing_index_url` still gives a
            # readable link for provenance without fetching it.
            person = _select_person(xml, xml.get("filer_cik"))
            if person is None:
                continue
            investor_name = person["name"]
            investor_cik  = _cik_int(person["cik"] or xml.get("filer_cik") or "").zfill(10)
            index_url     = _filing_index_url(company_cik, raw["accession"])
            primary_url   = None
        else:
            investor_name, investor_cik, primary_url = _fetch_filing_index(raw["index_url"])
            index_url = raw["index_url"]
            if not investor_name or not investor_cik:
                continue

        if not investor_cik or investor_cik in seen_investor_ciks:
            continue

        if known_names is None:
            known_names = [company_name] + fetch_former_names(company_cik)

        inv = {
            "investor_name": investor_name,
            "investor_cik":  investor_cik,
            "form_type":     raw["form_type"],
            "file_date":     raw["file_date"],
            "primary_url":   primary_url,
            "index_url":     index_url,
            "accession":     raw.get("accession"),
            "xml":           xml,
            "person":        person if xml else None,
        }

        pct           = None
        voting        = None
        is_individual = None
        share_class   = None
        group_members: list[dict] = []

        if inv.get("xml"):
            xml, person = inv["xml"], inv["person"]
            if not _xml_issuer_matches(xml, company_cik, known_names):
                log.info("SEC EDGAR: dropping filing by %r — its issuer is %r (CIK %s), not %r",
                         inv["investor_name"], xml.get("issuer_name"),
                         xml.get("issuer_cik"), company_name)
                continue
            pct, voting   = _stake_from_person(xml, person)
            share_class   = xml.get("class_title")
            is_individual = (person["type_code"] in _INDIVIDUAL_CODES
                             if person.get("type_code") else None)
            group_members = [{"name": o["name"], "cik": o["cik"], "source": "xml",
                              # Item 8's code: IN means a human being. Stated by
                              # the filer, so the writer need not guess from the
                              # shape of the name.
                              "type_code": o.get("type_code")}
                             for o in xml["persons"] if o is not person]
        elif inv.get("primary_url"):
            try:
                text          = _get_text(inv["primary_url"])
                issuer        = _parse_issuer_from_text(text)
                if not _issuer_matches(known_names, issuer):
                    log.info(
                        "SEC EDGAR: dropping filing by %r — its issuer is %r, not %r",
                        inv["investor_name"], issuer, company_name)
                    continue
                pct           = _parse_percent_from_text(text)
                pct, voting    = _own_stake_and_voting(text, pct)
                is_individual = _parse_reporter_type_from_text(text)
                share_class   = _parse_class_title_from_text(text)
                if voting and inv.get("accession"):
                    # A bloc without an XML membership list: the SGML header
                    # names the co-filers. Only fetched for the few filings that
                    # actually report one — 3 of AB InBev's 40, not all 40.
                    group_members = _sgml_group_members(company_cik, inv["accession"])
            except httpx.HTTPError:
                pass
        else:
            # Neither a parsed XML nor a cover page to check: admitting this
            # would be an unverified filing, which is how the Eve Holding rows
            # got in. A modern index page offers no `SC 13`-typed .htm at all,
            # so this is exactly the case a widened feed would otherwise slip
            # through unverified.
            log.info("SEC EDGAR: dropping unverifiable filing by %r (no XML, no cover page)",
                     inv["investor_name"])
            continue

        seen_investor_ciks.add(inv["investor_cik"])
        log.info(
            "SEC EDGAR: investor %r (CIK=%s) stake=%s is_individual=%s",
            inv["investor_name"], inv["investor_cik"], pct, is_individual,
        )
        results.append({
            "investor_name":    inv["investor_name"].title(),
            "investor_cik":     inv["investor_cik"],
            "form_type":        inv["form_type"],
            "file_date":        inv["file_date"],
            "period_of_report": None,
            "stake_percent":    pct,
            # The bloc a group member votes within — see _own_stake_and_voting.
            # None for a lone filer, whose stake already is its whole position.
            "voting_power_pct": voting,
            "ownership_type":   derive_ownership_type(pct, inv["form_type"]),
            # The security this percentage is a percentage OF.
            "share_class":      share_class,
            "is_individual":    is_individual,   # None = unknown (use name heuristic)
            # Provenance: the filing index page (readable), with the primary doc
            # as a fallback. file_date is the filing's date.
            "source_url":       inv.get("index_url") or inv.get("primary_url"),
            # Co-filers on this schedule — the filing group. Returned for the
            # caller to use; deliberately NOT written to the graph (see the
            # module docstring), because membership is not ownership.
            "group_members":    group_members,
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
      issuer/issuerCik                                   — whose shares (verified by caller)
      reportingOwner/reportingOwnerId/rptOwnerName       — person's name
      reportingOwner/reportingOwnerRelationship/isOfficer   — "1" or "0"
      reportingOwner/reportingOwnerRelationship/isDirector  — "1" or "0"
      reportingOwner/reportingOwnerRelationship/officerTitle — e.g. "Chief Executive Officer"

    The result carries `issuer_cik` so the caller can check the form is about
    the company being scraped. A company's submissions feed also lists forms it
    FILED about other issuers — Embraer's Form 4s about Eve Holding put
    "EMBRAER S.A." into Embraer's own executive list as a "Director", because
    isDirector meant director OF EVE. Unlike the 13D cover page this is exact:
    the XML states the issuer's CIK outright.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    owner = root.find(".//reportingOwner")
    if owner is None:
        return None

    issuer_cik = (root.findtext(".//issuer/issuerCik") or "").strip() or None

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

    # Shares held after the reported transaction(s) — the insider's current
    # non-derivative holding. Take the largest value across rows (the total).
    share_vals: list[float] = []
    for el in root.findall(".//sharesOwnedFollowingTransaction/value"):
        try:
            share_vals.append(float((el.text or "").replace(",", "").strip()))
        except (TypeError, ValueError):
            continue
    shares_owned = max(share_vals) if share_vals else None

    return {"name": name, "title": officer_title, "role": role,
            "shares_owned": shares_owned, "issuer_cik": issuer_cik}


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
    filing_dates = recent.get("filingDate",      [])

    # Collect one filing per unique filer CIK (newest first = most current title)
    executives: list[dict] = []
    seen_names: set[str]   = set()
    issuer_cik_int = _cik_int(cik)
    scanned  = 0
    SCAN_CAP = MAX_FORM4_FETCH * 2   # bound fetches (many insiders share a filing agent)

    # Fetch Form 3/4 filings newest-first and dedupe by the *reporting person* —
    # NOT the accession/filer-CIK prefix, which collapses distinct insiders who
    # share a filing agent (that missed most of a company's insiders). Stop at
    # MAX_FORM4_FETCH unique insiders or the scan cap.
    for i, form in enumerate(forms):
        if form not in ("3", "4", "3/A", "4/A"):
            continue
        # primaryDocument may be prefixed with an XSLT stylesheet dir
        # (e.g. "xslF345X06/form4.xml") which serves HTML — strip to the raw XML.
        doc = (primary_docs[i] if i < len(primary_docs) else "")
        doc = doc.split("/")[-1] if "/" in doc else doc
        acc = (accessions[i] if i < len(accessions) else "").replace("-", "")
        if not doc or not acc:
            continue

        # Archives path uses the issuer CIK (not the accession filer CIK).
        try:
            xml_text = _get_text(f"{ARCHIVES_URL}/{issuer_cik_int}/{acc}/{doc}")
        except httpx.HTTPError as exc:
            log.debug("SEC EDGAR: Form 3/4 fetch failed: %s", exc)
            continue
        scanned += 1

        result = _parse_form34_xml(xml_text)
        # A form about a different issuer is the company filing about a stake it
        # holds elsewhere — its reporting "owner" is usually the company itself,
        # and writing it here made Embraer a Director of Embraer.
        if result and result.get("issuer_cik") and \
                _cik_int(result["issuer_cik"]) != issuer_cik_int:
            log.info("SEC EDGAR: dropping Form 3/4 by %r — issuer CIK %s is not %s",
                     result["name"], result["issuer_cik"], cik)
            continue
        if result and result["name"] and result["name"] not in seen_names:
            seen_names.add(result["name"])
            result["source_url"]  = _filing_index_url(cik, accessions[i]) or None
            result["source_date"] = filing_dates[i] if i < len(filing_dates) else None
            executives.append(result)
            log.debug("SEC EDGAR: insider %s (%s)", result["name"], result["role"])

        if len(executives) >= MAX_FORM4_FETCH or scanned >= SCAN_CAP:
            break

    log.info("SEC EDGAR: found %d executives from Form 3/4 for CIK=%s",
             len(executives), cik)
    return executives


def _lookup_person_cik(name: str) -> str | None:
    """Resolve an individual's name to their EDGAR filer CIK (they file Form 4s)."""
    try:
        resp = _get_client().get(BROWSE_URL, params={
            "company": name, "action": "getcompany", "type": "4",
            "dateb": "", "owner": "include", "count": "5", "output": "atom",
        })
        resp.raise_for_status()
        time.sleep(REQUEST_DELAY)
    except httpx.HTTPError:
        return None
    m = re.search(r"cik=(\d+)", resp.text, re.IGNORECASE)
    return m.group(1).zfill(10) if m else None


def fetch_insider_holding(name: str, issuer_cik: str,
                          shares_outstanding: float | None = None) -> dict | None:
    """
    Person-centric insider lookup (all structured XML): resolve the individual's
    CIK, read THEIR most recent Form 4 that reports on `issuer_cik`, and return
    their current share holding (+ a stake %% when shares_outstanding is known).

    Reaches insiders the issuer-side Form-4 scan misses (e.g. a CEO whose filings
    are flooded out of the company's recent window). Verifying issuerCik on the
    filing guards against name collisions. Returns None if not found / no shares.
    """
    cik = _lookup_person_cik(name)
    if not cik:
        return None
    try:
        subs = _submissions(cik)
    except httpx.HTTPError:
        return None
    rec   = subs.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    accs  = rec.get("accessionNumber", [])
    docs  = rec.get("primaryDocument", [])
    dates = rec.get("filingDate", [])
    issuer_int = str(_cik_int(issuer_cik))

    for i, f in enumerate(forms):
        if f not in ("4", "4/A"):
            continue
        acc = accs[i].replace("-", "")
        doc = docs[i] if i < len(docs) else ""
        doc = doc.split("/")[-1] if "/" in doc else doc
        if not doc:
            continue
        try:
            xml = _get_text(f"{ARCHIVES_URL}/{int(cik)}/{acc}/{doc}")
        except httpx.HTTPError:
            continue
        m = re.search(r"<issuerCik>0*(\d+)</issuerCik>", xml)
        if not m or m.group(1) != issuer_int:
            continue                       # this Form 4 is about a different company
        shares = [float(v) for v in re.findall(
            r"<sharesOwnedFollowingTransaction>\s*<value>([\d.]+)</value>", xml)]
        held = max(shares) if shares else None
        if not held or held <= 0:
            return None
        stake = round(held / shares_outstanding * 100, 4) if shares_outstanding else None
        return {
            "shares_owned":  held,
            "stake_percent": stake,
            "source_url":    _filing_index_url(cik, accs[i]),
            "source_date":   dates[i] if i < len(dates) else None,
        }
    return None


def fetch_shares_outstanding(cik: str) -> float | None:
    """
    Latest reported common shares outstanding for an issuer (SEC XBRL facts),
    used to turn an insider's Form-4 share count into a stake percentage.
    Best-effort — returns None if unavailable.
    """
    for concept in ("dei/EntityCommonStockSharesOutstanding",
                    "us-gaap/CommonStockSharesOutstanding"):
        try:
            data = _get(f"{COMPANYCONCEPT_URL}/CIK{cik}/{concept}.json")
        except httpx.HTTPError:
            continue
        units = data.get("units") or {}
        vals = units.get("shares") or (next(iter(units.values()), []) if units else [])
        dated = [(v.get("end") or "", v.get("val")) for v in vals if v.get("val")]
        if dated:
            dated.sort()                      # most recent reporting period last
            try:
                return float(dated[-1][1])
            except (TypeError, ValueError):
                continue
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_former_names(cik: str) -> list[str]:
    """
    Prior registered names of a company from its EDGAR submissions (``formerNames``).

    EDGAR records a rename under the *same* CIK (e.g. "Facebook Inc" → "Meta
    Platforms, Inc."), so these are aliases of one legal entity, not a successor
    link. Returns the distinct former names (order preserved), or [] on any error.
    """
    try:
        sub = _submissions(cik)
    except Exception as exc:  # noqa: BLE001 - a missing/failed submissions file mustn't abort the scrape
        log.warning("SEC EDGAR: formerNames fetch failed for CIK=%s: %s", cik, exc)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in sub.get("formerNames") or []:
        name = (entry.get("name") or "").strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def fetch_company_lei(cik: str) -> str | None:
    """The company's LEI from its EDGAR submissions (the ``lei`` field), or None.

    SEC exposes the LEI a filer reported — regulated/financial filers usually do,
    many operating companies don't — which lets a SEC entity merge with its GLEIF
    node by ``lei_id`` (no OpenCorporates/Wikidata bridge needed)."""
    try:
        sub = _submissions(cik)
    except Exception as exc:  # noqa: BLE001 - a missing/failed submissions file mustn't abort the scrape
        log.warning("SEC EDGAR: LEI fetch failed for CIK=%s: %s", cik, exc)
        return None
    return (sub.get("lei") or "").strip() or None


def scrape_company(company_name: str, holdings_limit: int = HOLDINGS_SCRAPE_LIMIT) -> dict | None:
    """
    Full SEC EDGAR scrape for one company.
    Returns structured dict with ownership_filings and executives, or None
    if the company is not found on EDGAR.
    """
    company = search_company(company_name)
    if not company:
        return None

    cik          = company.get("cik")
    former_names = fetch_former_names(cik) if cik else []
    lei          = fetch_company_lei(cik) if cik else None
    ownership    = fetch_ownership_filings(company_name, company_cik=cik)
    executives   = fetch_executives(cik) if cik else []
    # What this company owns of others. Costs one submissions read for a company
    # that files no 13D/13G — almost all of them — and only fetches documents for
    # an actual institutional filer.
    holdings     = fetch_filer_holdings(cik, limit=holdings_limit) if (cik and holdings_limit) else []

    # Turn each insider's Form-4 share holding into a stake %, when we can read
    # the issuer's shares outstanding.
    shares_out = fetch_shares_outstanding(cik) if cik else None
    if shares_out:
        for ex in executives:
            so = ex.get("shares_owned")
            if so and so > 0:
                ex["stake_percent"] = round(so / shares_out * 100, 4)

    return {
        "cik":                company["cik"],
        "name":               company["name"],
        "lei":                lei,
        "former_names":       former_names,
        "ownership_filings":  ownership,
        "holdings":           holdings,
        "executives":         executives,
        "shares_outstanding": shares_out,
    }


# ── Filer-side holdings: what THIS company owns of others ─────────────────────
#
# Everything above reads filings where the company is the SUBJECT — who owns it
# (SC 13D/13G) and its insiders (Form 3/4). An asset manager has none of that:
# Vanguard is privately held and isn't a listed issuer, so its node stayed empty
# while its ~3,400 filings — 13D/13G disclosures it makes ABOUT other companies —
# went unread. This reads the filer side, which for an institutional holder is
# the whole point of it being in the graph at all.
#
# Modern 13D/13G filings are structured XML (`primary_doc.xml`, schema X0202)
# carrying the subject's CIK, its name and the percentage as fields, so there is
# no HTML to scrape and one fetch per filing. Older filings predate the schema
# and are skipped rather than guessed at.


def _xml_field(root, tag: str) -> str | None:
    """First value of `tag` at any depth, ignoring the XML namespace.

    Beware the "first": on a multi-person filing the same tag recurs once per
    reporting person AND again inside `items`, with different values. Use
    `_xml_child` against a person's own element when the value belongs to that
    person. Wellington's Nasdaq 13G/A is the case in point — four cover blocks
    reading 5.4 / 5.4 / 5.4 / 5.1 and a fifth value of 5.36 under `items`.
    """
    return _xml_child(root, tag)


def _xml_child(el, tag: str) -> str | None:
    """First value of `tag` within THIS element's subtree, namespace-agnostic."""
    found = el.find(f".//{{*}}{tag}")
    return found.text.strip() if found is not None and found.text else None


def _xml_num(el, tag: str, *, as_int: bool = True):
    """A numeric field, or None when the filing does not state it.

    Absent must not become zero: `_split_stake` reads "no sole dispositive row"
    and "sole dispositive of nothing" as different facts, and conflating them
    invents a 0% stake. Values arrive both as "0" and as "1312500.00", so parse
    through float().
    """
    raw = _xml_child(el, tag)
    if raw is None:
        return None
    try:
        val = float(raw.replace(",", ""))
    except ValueError:
        return None
    return int(val) if as_int else val


#: The two schedules use different tag names for the same facts, and EDGAR
#: publishes them under different namespaces. Layout is chosen by which person
#: container is present, not by the form string or the namespace URI, so a
#: schema bump that keeps the shape keeps working.
_XML_LAYOUTS = (
    # (person container, issuer-cik tag, percent tag, aggregate tag, person-cik tag)
    ("reportingPersonInfo", "issuerCIK", "percentOfClass",
     "aggregateAmountOwned", "reportingPersonCIK"),
    ("coverPageHeaderReportingPersonDetails", "issuerCik", "classPercent",
     "reportingPersonBeneficiallyOwnedAggregateNumberOfShares", None),
)


def _xml_issuer_matches(xml: dict, company_cik: str, known_names: list) -> bool:
    """Is this structured filing about the company being scraped?

    Two tiers, and both must fail before a filing is thrown away. The CIK is
    the strong signal, but it is typed by the filer's agent — the same kind of
    human who filed Embraer's Eve Holding schedule under EMBRAER's name — while
    the reverse also occurs: Wellington's Nasdaq 13G/A carries the correct CIK
    beside the long-superseded name "The NASDAQ OMX Group, Inc.". Rejecting on
    either alone would lose a real owner in one of those two cases.
    """
    issuer_cik = xml.get("issuer_cik")
    if issuer_cik and _cik_int(issuer_cik) == _cik_int(company_cik):
        return True
    if issuer_cik and _issuer_matches(known_names, xml.get("issuer_name")):
        return True          # right company, stale or reworded name
    return not issuer_cik    # nothing stated is not a mismatch


def _stake_from_person(xml: dict, person: dict) -> tuple:
    """(own stake %, voting-bloc %) for one reporting person of a structured filing.

    The same judgement `_own_stake_and_voting` applies to an HTML cover, fed
    from fields instead of regexes. The denominator is not a tagged value, so
    prefer the one the filer states in the comment and fall back to deriving it
    from the aggregate — derivation is only as precise as `percentOfClass`,
    which is often two significant figures.
    """
    rows = {k: person[k] for k in
            ("sole_voting", "shared_voting", "sole_dispositive", "shared_dispositive")
            if person.get(k) is not None}
    total = _shares_outstanding(xml.get("comment_text") or "")
    if not total:
        total = _derive_total(person.get("aggregate"), person.get("percent"))
        if total:
            log.info("SEC EDGAR: derived %s shares outstanding for %r from %s at %s%%",
                     total, person["name"], person.get("aggregate"), person.get("percent"))
    return _split_stake(rows, total, person.get("percent"))


def _derive_total(aggregate: int | None, percent: float | None) -> int | None:
    """Shares outstanding implied by "this many shares are that percent".

    `percent > 0` is not defensive dressing: a 13G/A amending to 0% — the
    filer announcing it has exited — is common, and would divide by zero.
    """
    if not aggregate or not percent or percent <= 0:
        return None
    return round(aggregate / (percent / 100))


def _sgml_group_members(subject_cik: str, accession: str) -> list[dict]:
    """Co-filers named in the submission's SGML header.

    Pre-2024 filings carry no XML, but EDGAR's header has always listed
    `GROUP MEMBERS:` — names only, no CIKs. Read from the header-only document
    (about 4 KB) rather than the full submission, which for AB InBev's schedule
    is 287 KB of exhibits.
    """
    nodash = accession.replace("-", "")
    url = (f"{ARCHIVES_URL}/{_cik_int(subject_cik)}/{nodash}/"
           f"{accession}-index-headers.htm")
    try:
        raw = _get_text(url)
    except Exception as exc:  # noqa: BLE001 - a missing header must not sink the scrape
        log.debug("SEC EDGAR: no SGML header for %s (%s)", accession, exc)
        return []
    # NOT _plain_text: it collapses every run of whitespace including newlines,
    # and this format is line-oriented — one GROUP MEMBERS per line. Flattened,
    # a single match swallows the rest of the header as one enormous "name".
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", raw))
    names = re.findall(r"GROUP MEMBERS:[ \t]*([^\r\n]+)", text)
    # No type code in the header — the writer falls back to a name heuristic here.
    return [{"name": n.strip(), "cik": None, "source": "sgml", "type_code": None}
            for n in names if n.strip()]


def _is_structured(form_type: str) -> bool:
    """Does this filing have a `primary_doc.xml`?

    Decided from the form name the feed already gave us, so no request is
    wasted discovering it: the December-2024 modernization both mandated the
    XML and renamed the forms, so "SCHEDULE 13D" has it and the older
    "SC 13D" does not. A modern filing whose XML is missing anyway costs one
    404 and falls back to HTML.
    """
    return form_type.strip().upper().startswith("SCHEDULE 13")


def _fetch_13dg_xml(subject_cik: str, accession: str) -> dict | None:
    """The structured filing, or None if it isn't there or won't parse.

    The Archives path accepts the SUBJECT's CIK even though another party
    filed, so this needs nothing the Atom feed did not already provide.
    """
    nodash = accession.replace("-", "")
    url = f"{ARCHIVES_URL}/{_cik_int(subject_cik)}/{nodash}/primary_doc.xml"
    try:
        raw = _get_text(url)
    except Exception as exc:  # noqa: BLE001 - absent XML is normal; fall back to HTML
        log.debug("SEC EDGAR: no structured XML for %s (%s)", accession, exc)
        return None
    return _parse_13dg_xml(raw)


def _select_person(xml: dict, filer_cik: str | None) -> dict | None:
    """Which reporting person is *the investor* on this filing.

    A group files one schedule listing every member, so one block has to be
    chosen as the edge's owner and the rest become the group. Prefer the block
    whose CIK is the filer's; 13G blocks carry no CIK, so fall back to the
    first, which is where the form puts the primary filer.
    """
    persons = xml.get("persons") or []
    if not persons:
        return None
    if filer_cik:
        want = _cik_int(filer_cik)
        for p in persons:
            if p.get("cik") and _cik_int(p["cik"]) == want:
                return p
        log.debug("SEC EDGAR: no reporting person matched filer CIK %s; using the first",
                  filer_cik)
    return persons[0]


def _parse_13dg_xml(raw: str) -> dict | None:
    """A Schedule 13D or 13G `primary_doc.xml`, normalised.

    Since the SEC's December 2024 beneficial-ownership modernization these
    filings are machine-readable, which removes the guesswork this module used
    to do against HTML cover pages: the issuer is a CIK rather than a name to
    match, the four power rows are fields rather than regex captures, and — the
    reason this was written — every member of a filing group gets its own
    block, so group membership no longer has to be read out of Item 6 prose.

    Returns None for anything unparseable, including pre-2024 filings, which
    simply have no XML; the caller falls back to the HTML path for those.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log.debug("SEC EDGAR: unparseable 13D/G XML: %s", exc)
        return None

    for container, issuer_tag, pct_tag, agg_tag, person_cik_tag in _XML_LAYOUTS:
        blocks = root.findall(f".//{{*}}{container}")
        if blocks:
            break
    else:
        return None

    persons = []
    for b in blocks:
        name = _xml_child(b, "reportingPersonName")
        if not name:
            continue
        persons.append({
            "name":               name,
            "cik":                _xml_child(b, person_cik_tag) if person_cik_tag else None,
            "sole_voting":        _xml_num(b, "soleVotingPower"),
            "shared_voting":      _xml_num(b, "sharedVotingPower"),
            "sole_dispositive":   _xml_num(b, "soleDispositivePower"),
            "shared_dispositive": _xml_num(b, "sharedDispositivePower"),
            "aggregate":          _xml_num(b, agg_tag),
            "percent":            _xml_num(b, pct_tag, as_int=False),
            "type_code":          _xml_child(b, "typeOfReportingPerson"),
        })
    if not persons:
        return None

    # The denominator is not a field, but filers usually state it in the
    # comment; that text is worth carrying so `_shares_outstanding` can read it.
    comments = [(e.text or "") for tag in ("commentContent", "comments")
                for e in root.findall(f".//{{*}}{tag}")]

    return {
        "issuer_cik":   _xml_child(root, issuer_tag),
        "issuer_name":  _xml_child(root, "issuerName"),
        "filer_cik":    _xml_child(root, "cik"),
        # WHICH security the percentages are percentages OF. A percent of class
        # is meaningless without it: Grupo Televisa's filers report 22.3% of
        # "Series A/B/Dividend Preferred" beside 9.7% of "CPOs and Global D
        # shares", and adding those gave the company 115.9% of itself.
        "class_title":  _xml_child(root, "securitiesClassTitle"),
        "persons":      persons,
        "comment_text": " ".join(comments),
    }


def _parse_holding_filing(filer_cik: str, accession: str) -> dict | None:
    """Subject company + stake from one 13D/13G, or None if it isn't parseable.

    Returns `percent` as a float — **0.0 is meaningful**, not missing: an
    amendment reporting 0% is the filer declaring it has dropped below the 5%
    threshold, i.e. the end of a holding rather than the absence of one.

    Reads through the shared `_parse_13dg_xml`, which knows both schedules.
    Written against 13G tag names alone, this function returned None for every
    Schedule 13D — `issuerCik`/`classPercent` are 13G spellings, and 13D uses
    `issuerCIK`/`percentOfClass`. Since `_HOLDING_FORMS` includes 13D, that was
    a silent hole in what a filer was seen to own.
    """
    xml = _fetch_13dg_xml(filer_cik, accession)
    if not xml or not xml.get("issuer_cik") or not xml.get("issuer_name"):
        return None

    # Read the percent off the first reporting person rather than the document,
    # because the same tag reappears under `items` with a different value —
    # Wellington's Nasdaq 13G/A carries 5.4 on the cover and 5.36 below it.
    percent = (xml["persons"][0].get("percent")) if xml.get("persons") else None

    return {
        "subject_cik":  _cik_int(xml["issuer_cik"]).zfill(10),
        "subject_name": xml["issuer_name"],
        "percent":      percent,
        "accession":    accession,
    }


def fetch_filer_name(cik: str) -> str | None:
    """The filer's own name from its EDGAR submissions index, or None."""
    try:
        subs = _submissions(cik)
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC EDGAR: name fetch failed for CIK=%s: %s", cik, exc)
        return None
    return (subs.get("name") or "").strip() or None


def _holding_filings_in(block: dict) -> list[dict]:
    """The 13D/13G rows of one EDGAR filing-index page, newest first."""
    return sorted(
        (
            {"form": f, "accession": a, "date": d}
            for f, a, d in zip(block.get("form", []),
                               block.get("accessionNumber", []),
                               block.get("filingDate", []))
            if any(f.startswith(p) for p in _HOLDING_FORMS)
        ),
        key=lambda r: r["date"], reverse=True,
    )


def _iter_filing_pages(cik: str):
    """Yield a filer's 13D/13G filings page by page, newest first.

    `filings.recent` comes first, then EDGAR's archive pages. A page is only
    fetched when the caller asks for it, so a scrape that finds what it needs in
    recent never pays for the archive — and one that needs to reach back for a
    stake disclosed years ago can.
    """
    try:
        subs = _submissions(cik)
    except Exception as exc:  # noqa: BLE001 - a filer with no submissions file isn't an error
        log.warning("SEC EDGAR: submissions fetch failed for CIK=%s: %s", cik, exc)
        return

    yield _holding_filings_in(subs.get("filings", {}).get("recent", {}))

    for page in (subs.get("filings", {}).get("files") or [])[:HOLDINGS_MAX_ARCHIVE_PAGES]:
        name = page.get("name")
        if not name:
            continue
        try:
            yield _holding_filings_in(_get(f"{SUBMISSIONS_URL}/{name}"))
        except Exception as exc:  # noqa: BLE001 - a missing archive page isn't fatal
            log.warning("SEC EDGAR: archive page %s failed: %s", name, exc)
            return


_AFFILIATE_FORMS = ("13F-NT", "13F-HR")


def fetch_affiliated_managers(cik: str) -> list[dict]:
    """The affiliated managers a filer names on its latest 13F cover page.

    A fund group files one 13F per manager, and the cover page lists the group's
    OTHER managers with their CIKs — ten of them for Vanguard, including the
    entity that took over its 13G reporting. That is an authoritative statement of
    group membership from the filer itself, far better evidence than matching on a
    shared name prefix, and it costs a single document fetch.

    What it does NOT establish is ownership or control: "reports 13F holdings
    alongside" is exactly as much as the form says. Callers should record it as an
    affiliation, not as an OWNS edge.
    """
    try:
        subs = _submissions(cik)
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC EDGAR: submissions fetch failed for CIK=%s: %s", cik, exc)
        return []

    recent = subs.get("filings", {}).get("recent", {})
    filings = sorted(
        (
            {"form": f, "accession": a, "date": d}
            for f, a, d in zip(recent.get("form", []),
                               recent.get("accessionNumber", []),
                               recent.get("filingDate", []))
            if any(f.startswith(p) for p in _AFFILIATE_FORMS)
        ),
        key=lambda r: r["date"], reverse=True,
    )
    if not filings:
        return []           # not a 13F filer — most companies

    latest = filings[0]
    url = f"{ARCHIVES_URL}/{_cik_int(cik)}/{latest['accession'].replace('-', '')}/primary_doc.xml"
    try:
        root = ET.fromstring(_get_text(url))
    except Exception as exc:  # noqa: BLE001 - pre-XML or malformed cover page
        log.debug("SEC EDGAR: no parseable 13F cover page for %s: %s", cik, exc)
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for el in root.findall(".//{*}otherManager"):
        raw_cik = _xml_field(el, "cik")
        name = _xml_field(el, "name")
        if not name:
            continue
        # Some entries carry a short/unpadded CIK (the Vanguard notice has one at
        # 9 digits); normalise so it matches an EDGAR-sourced node.
        manager_cik = _cik_int(raw_cik).zfill(10) if raw_cik and raw_cik.strip().isdigit() else None
        key = manager_cik or name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "cik":         manager_cik,
            "name":        name.strip(),
            "source_url":  _filing_index_url(cik, latest["accession"]),
            "source_date": latest["date"],
            "form_type":   latest["form"],
        })
    log.info("SEC EDGAR: %d affiliated managers for CIK=%s (from %s)",
             len(out), cik, latest["form"])
    return out


def fetch_filer_holdings(cik: str, limit: int = HOLDINGS_DEFAULT_LIMIT,
                         max_filings: int | None = None) -> list[dict]:
    """Companies this filer discloses a >5% stake in, newest disclosure per company.

    Only OUTBOUND filings. EDGAR's index for a CIK lists filings the company is
    *named in* as well as ones it submitted, so a 13G somebody else filed about it
    appears here too — and reading that filing's issuer gives the company itself
    back. See the subject == filer skip below.

    One row per subject company:
      stake_percent — the last disclosed non-zero percentage
      until         — set when a *newer* amendment reported 0%, i.e. the filer has
                      since dropped below the threshold; the holding is history,
                      not a current position

    That distinction is load-bearing. Vanguard moved its 13G reporting from
    VANGUARD GROUP INC (CIK 0000102909) to VANGUARD CAPITAL MANAGEMENT LLC
    (0002100119) in spring 2026, closing ~1,800 positions with 0% amendments on
    the way out. Keeping only the newest filing per subject would report that the
    old entity owns nothing — true, but useless; recording the last real stake
    with its end date preserves the history the filings actually describe.

    ``max_filings`` bounds the work. The subject company is only knowable by
    fetching the filing — the submissions index doesn't carry it — so filings
    cannot be de-duplicated by subject in advance, and a filer with thousands of
    amendments would otherwise be thousands of requests. Defaults to a multiple of
    ``limit``; raise both to walk further back (the ~1,800 zero amendments at the
    front of the old Vanguard CIK need roughly that many fetches before the last
    real stakes appear).
    """
    if max_filings is None:
        max_filings = limit * 5 + 50

    pages = _iter_filing_pages(cik)
    filings = next(pages, [])
    if not filings:
        return []       # not an institutional filer — the common case, one JSON read

    holdings: list[dict] = []
    closed_since: dict[str, str] = {}   # subject -> date of the newest 0% amendment
    done: set[str] = set()
    fetched = 0

    # Newest first, so the first non-zero filing seen for a subject is its most
    # recent real stake, and any 0% already seen for it is the exit that followed.
    # When a page runs out, pull the next archive page — a stake closed years ago
    # is only reachable there.
    while True:
        # Budget first, so a satisfied scrape never pays for an archive page.
        if len(holdings) >= limit or fetched >= max_filings:
            break
        if not filings:
            filings = next(pages, [])
            if not filings:
                break
        filing = filings.pop(0)
        parsed = _parse_holding_filing(cik, filing["accession"])
        fetched += 1
        if not parsed:
            continue
        sid = parsed["subject_cik"]
        # A filing whose subject IS this filer is an INBOUND one — somebody else's
        # 13D/13G about them — and EDGAR's index carries it because the company is
        # named in it, not because the company submitted it. Reading its issuer
        # gives the company back, so it lands as "X holds 7.48% of X", with the
        # percentage being some third party's stake in X.
        #
        # Nine of those were live in the graph, Apple, Microsoft and Alphabet among
        # them. An issuer does not file 13D/13G about its own stock (buybacks go in
        # a 10-K or 8-K), so subject == filer always means inbound and is always
        # safe to drop.
        if sid == _cik_int(cik).zfill(10):
            log.debug("SEC EDGAR: skipping inbound filing %s — %s is the subject, "
                      "not the filer", filing["accession"], sid)
            continue
        if sid in done:
            continue

        if not parsed["percent"]:
            closed_since.setdefault(sid, filing["date"])   # newest zero wins
            continue

        done.add(sid)
        holdings.append({
            "subject_cik":   sid,
            "subject_name":  parsed["subject_name"],
            "stake_percent": parsed["percent"],
            "file_date":     filing["date"],
            "form_type":     filing["form"],
            "until":         closed_since.get(sid),
            "source_url":    _filing_index_url(cik, filing["accession"]),
        })

    log.info("SEC EDGAR: %d holdings for CIK=%s (%d of %d filings fetched)",
             len(holdings), cik, fetched, len(filings))
    return holdings
