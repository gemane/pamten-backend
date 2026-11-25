"""
SEC EDGAR — what a filer's own record says about it.

Country, headquarters, website, SIC code, former names, LEI, and the cover of
its latest annual report (stated incorporation and headquarters).

Source, rate limits and licence: see `sec_edgar`.
"""

import functools
import re
import html as html_lib
import logging

import httpx
from app import former_names
from app.scraper import sec_client

log = logging.getLogger(__name__)


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
    incorporation returns None rather than a guess. The incorporation the
    filer's newest tagged cover states comes first (`report_cover`): EDGAR's company
    record put Alibaba in Hong Kong, its 20-F in the Cayman Islands.
    """
    from app.scraper.maintenance import nationality_to_iso2

    if stated := stated_incorporation(submissions):              # the report first
        return stated.split("-")[0]
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

    The principal executive office the filer's newest tagged cover states comes
    first (`stated_headquarters`); the company record's business address only
    when the report states none.

    Returns ``{"address", "city", "country"}`` — country from the same two-letter
    field `sec_country` reads, where a US state code means the United States and
    anything else is one of SEC's own foreign codes. None when there is no
    address, or when it is too sparse to place.
    """
    from app.scraper.maintenance import nationality_to_iso2

    if stated := stated_headquarters(submissions):              # the report first
        return stated
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
        return sec_headquarters(sec_client._submissions(cik))
    except Exception as exc:  # noqa: BLE001 - an address is a nicety, not worth aborting a scrape
        log.warning("SEC EDGAR: address lookup failed for CIK=%s: %s", cik, exc)
        return None


def sec_website(submissions: dict) -> str | None:
    """The company's website from the submissions document, or None.

    Two fields exist and filers populate either: `website` (the corporate
    site) and `investorWebsite` (the IR site, often the same host). The
    corporate one wins; both pass through the same http(s)-only normalisation
    as Wikidata's P856 — this string becomes an <a href> in the panel.
    """
    from app.scraper.wikidata import normalize_url

    return (normalize_url(submissions.get("website"))
            or normalize_url(submissions.get("investorWebsite")))


def fetch_filer_website(cik: str) -> str | None:
    """The filer's website from EDGAR, or None if it cannot be determined."""
    try:
        return sec_website(sec_client._submissions(cik))
    except Exception as exc:  # noqa: BLE001 - a website is a nicety, not worth aborting a scrape
        log.warning("SEC EDGAR: website lookup failed for CIK=%s: %s", cik, exc)
        return None


def fetch_filer_sic(cik: str) -> tuple[str, str] | None:
    """The filer's SIC code and title from EDGAR — the SEC's own statement of
    what the company does (`sic` / `sicDescription` in the submissions
    document; "3711", "Motor Vehicles & Passenger Car Bodies"). None when
    the filer has none. See app.industry."""
    from app.industry import us_sic_from_submissions
    try:
        return us_sic_from_submissions(sec_client._submissions(cik))
    except Exception as exc:  # noqa: BLE001 - a sector is a nicety, not worth aborting a scrape
        log.warning("SEC EDGAR: SIC lookup failed for CIK=%s: %s", cik, exc)
        return None


def fetch_filer_country(cik: str) -> str | None:
    """The filer's country from EDGAR, or None if it cannot be determined."""
    try:
        return sec_country(sec_client._submissions(cik))
    except Exception as exc:  # noqa: BLE001 - a country is a nicety, not worth aborting a scrape
        log.warning("SEC EDGAR: country lookup failed for CIK=%s: %s", cik, exc)
        return None


def fetch_former_names(cik: str) -> list[str]:
    """
    Prior registered names of a company from its EDGAR submissions (``formerNames``).

    EDGAR records a rename under the *same* CIK (e.g. "Facebook Inc" → "Meta
    Platforms, Inc."), so these are aliases of one legal entity, not a successor
    link. Returns the distinct former names as ``app.former_names`` entries
    (``"Facebook Inc|2021-10-27"`` — the ``to`` timestamp's day is the last day
    the name applied; order preserved), or [] on any error.
    """
    try:
        sub = sec_client._submissions(cik)
    except Exception as exc:  # noqa: BLE001 - a missing/failed submissions file mustn't abort the scrape
        log.warning("SEC EDGAR: formerNames fetch failed for CIK=%s: %s", cik, exc)
        return []
    return former_name_entries(sub)


def former_name_entries(sub: dict | None) -> list[str]:
    """A submissions document's ``formerNames`` as ``app.former_names`` entries
    (the day each ended), distinct by name, order kept."""
    out: list[str] = []
    seen: set[str] = set()
    for entry in (sub or {}).get("formerNames") or []:
        name = (entry.get("name") or "").strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(former_names.entry(name, entry.get("to")))
    return out


def fetch_company_lei(cik: str) -> str | None:
    """The company's LEI from its EDGAR submissions (the ``lei`` field), or None.

    SEC exposes the LEI a filer reported — regulated/financial filers usually do,
    many operating companies don't — which lets a SEC entity merge with its GLEIF
    node by ``lei_id`` (no OpenCorporates/Wikidata bridge needed)."""
    try:
        sub = sec_client._submissions(cik)
    except Exception as exc:  # noqa: BLE001 - a missing/failed submissions file mustn't abort the scrape
        log.warning("SEC EDGAR: LEI fetch failed for CIK=%s: %s", cik, exc)
        return None
    return (sub.get("lei") or "").strip() or None


def company_by_cik(cik: str | None) -> dict | None:
    """The filer EDGAR holds under a CIK, as `search_company` returns it —
    {cik: zero-padded-10-digit, name: registered name} — or None when EDGAR
    has no such filer (404) or the value is no CIK. The submissions document
    is the identity: no name has to match."""
    from app.scraper.mapper import normalize_cik
    padded = normalize_cik(cik)
    if not padded:
        return None
    try:
        name = (sec_client._submissions(padded) or {}).get("name")
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            log.warning("SEC EDGAR: no filer under the stored CIK %s", padded)
            return None
        raise
    return {"cik": padded, "name": name} if name else None


#: A cover-page fact, tagged in every inline-XBRL 10-K/20-F/10-Q/8-K (since
#: 2019): its name, its context, its text.
_COVER_TAG = re.compile(
    r'<ix:nonNumeric\b(?=[^>]*\bname="dei:(\w+)")(?=[^>]*\bcontextRef="([^"]+)")[^>]*>(.*?)</ix:nonNumeric>',
    re.S | re.I)
_COVER_FACTS = frozenset({"EntityRegistrantName", "EntityIncorporationStateCountryCode",
                          "EntityAddressAddressLine1", "EntityAddressAddressLine2", "EntityAddressAddressLine3",
                          "EntityAddressCityOrTown", "EntityAddressStateOrProvince",
                          "EntityAddressPostalZipCode", "EntityAddressCountry", "EntityCentralIndexKey"})
#: The filer's own tagged statements about itself, read newest first. Not
#: only the annual report: Liberty Media converted from Delaware to Nevada on
#: 2026-05-12, its 10-K of February still said Delaware, and that refused the
#: join with its GLEIF record, which says Nevada as its 10-Q and 8-Ks do
#: (findings item 23, 2026-10-31). A 6-K tags nothing.
_STATED_FORMS = ("10-K", "20-F", "40-F", "10-Q", "8-K")
#: Covers opened at most for one answer (each is cached for good).
_MAX_COVERS = 3


@functools.lru_cache(maxsize=256)
def _cover_of(url: str, cik: str | None = None) -> dict[str, str]:
    """The cover facts of one filing, in the registrant's own context: the one
    tagging ``cik`` as its EntityCentralIndexKey — a joint 10-Q or 8-K carries
    one cover per registrant, parent and subsidiaries — else the one its name
    is tagged in: a 20-F tags a second address, the business contact's, under
    an axis (AB InBev). Raises when unreadable, so a failure is not
    remembered. A filing never changes: kept per URL."""
    facts: dict[str, dict[str, str]] = {}
    for name, context, text in _COVER_TAG.findall(sec_client._get_text(url) or ""):
        if name in _COVER_FACTS:
            value = html_lib.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
            facts.setdefault(context, {}).setdefault(name, " ".join(value.split()).strip(" ,;"))
    want = cik.lstrip("0") if cik else None
    own = next((c for c, f in facts.items()
                if want and (f.get("EntityCentralIndexKey") or "").lstrip("0") == want), None) \
        or next((c for c, f in facts.items() if "EntityRegistrantName" in f), None)
    return facts.get(own) or next(iter(facts.values()), {})


def report_cover(submissions: dict) -> dict[str, str]:
    """What the filer's newest tagged cover states about it — `{fact: text}`,
    {} when there is none or it tags nothing. Newest first among its 10-K,
    20-F, 40-F, 10-Q and 8-K, the first that states where it is incorporated
    (at most `_MAX_COVERS` opened): a company that changed its state since its
    annual report says so on the next quarterly report and every 8-K.

    The report is the filer's own signed statement, renewed every year. EDGAR's
    company record (`stateOfIncorporation`, `addresses`) is the filer's entry
    in EDGAR's database, checked against nothing: it says New York for
    HubSpot, whose 10-K says Delaware, and Hong Kong — its office — for
    Alibaba, whose 20-F says the Cayman Islands; it is empty for MercadoLibre,
    Embraer and Televisa (findings item 19, 2026-10-30). So the report comes
    first wherever EDGAR states a place, the record only when it is silent."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    cik = str(submissions.get("cik") or "").strip()
    if not cik.isdigit():
        return {}
    first: dict[str, str] = {}
    opened = 0
    for form, accession, doc in zip(recent.get("form") or [], recent.get("accessionNumber") or [],
                                    recent.get("primaryDocument") or []):
        if form not in _STATED_FORMS or not doc:
            continue
        if opened >= _MAX_COVERS:
            break
        opened += 1
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{doc}"
        try:
            cover = _cover_of(url, cik)
        except Exception as exc:  # noqa: BLE001 - the company record still answers
            log.info("SEC EDGAR: filing %s unreadable: %s", url, exc)
            continue
        if cover.get("EntityIncorporationStateCountryCode"):
            return cover
        first = first or cover
    return first


def _place_code(text: str | None) -> str | None:
    """A place as a jurisdiction code: a name ("Delaware", "Cayman Islands")
    or, untransformed, EDGAR's code — a US state's maps, a foreign one ("E9")
    does not."""
    from app.scraper.sec_ex21 import jurisdiction_country, jurisdiction_subdivision
    return (jurisdiction_subdivision(text) or jurisdiction_country(text)) if text else None


def stated_incorporation(submissions: dict) -> str | None:
    """Where the filer's newest tagged cover says it is incorporated, as a
    jurisdiction code; None when it does not say (`report_cover`)."""
    return _place_code(report_cover(submissions).get("EntityIncorporationStateCountryCode"))


#: a mailbox is no office: Inter & Co's 20-F tags its Cayman registered agent's
#: "PO Box 309, Ugland House", where EDGAR's record has Belo Horizonte
_PO_BOX = re.compile(r"\bP\.?\s*O\.?\s*Box\b", re.I)


def stated_headquarters(submissions: dict) -> dict | None:
    """The principal executive office the filer's newest tagged cover states, in
    `sec_headquarters`' shape; None when it states none, or only a P.O. box."""
    cover = report_cover(submissions)
    street = " ".join(cover[k] for k in ("EntityAddressAddressLine1", "EntityAddressAddressLine2",
                                          "EntityAddressAddressLine3") if cover.get(k)).strip()
    city = cover.get("EntityAddressCityOrTown") or ""
    if not (street or city) or _PO_BOX.search(street):
        return None
    region = _place_code(cover.get("EntityAddressStateOrProvince"))
    country = _place_code(cover.get("EntityAddressCountry")) or (region or "").split("-")[0] or None
    if country and "-" in country:
        country = country.split("-")[0]
    state = region.split("-")[1] if region and region.startswith("US-") else None
    # "00000": Alibaba's placeholder for a city without postcodes
    postcode = (cover.get("EntityAddressPostalZipCode") or "").strip()
    postcode = "" if not postcode.strip("0") else postcode
    parts = [p for p in (street, city, state or "", postcode, country or "") if p]
    return {"address": ", ".join(parts), "city": city or None, "country": country,
            "street": street or None, "postcode": postcode or None, "state": state}


def incorporation_place(submissions: dict) -> str | None:
    """Where the filer is incorporated, as a jurisdiction code: "US-NY" for a
    US state, the ISO-2 country otherwise ("KY" for Cayman), None when EDGAR
    does not say. Incorporation only — never the business address (see
    `sec_country`). The filer's newest tagged cover first
    (`stated_incorporation`), EDGAR's header field when it has none."""
    from app.scraper.maintenance import nationality_to_iso2

    if stated := stated_incorporation(submissions):
        return stated
    inc_code = (submissions.get("stateOfIncorporation") or "").strip().upper()
    if inc_code in _US_STATES:
        return "US" if inc_code == "PR" else f"US-{inc_code}"
    inc_name = (submissions.get("stateOfIncorporationDescription") or "").strip()
    return nationality_to_iso2(inc_name) if inc_name else None


def fetch_filer_identity(cik: str) -> tuple[str | None, str | None]:
    """(EDGAR's name for the filer, `incorporation_place`) from one
    submissions request; (None, None) when it fails."""
    try:
        subs = sec_client._submissions(cik)
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC EDGAR: submissions fetch failed for CIK=%s: %s", cik, exc)
        return None, None
    return (subs.get("name") or "").strip() or None, incorporation_place(subs)


def fetch_filer_name(cik: str) -> str | None:
    """The filer's own name from its EDGAR submissions index, or None."""
    try:
        subs = sec_client._submissions(cik)
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC EDGAR: name fetch failed for CIK=%s: %s", cik, exc)
        return None
    return (subs.get("name") or "").strip() or None
