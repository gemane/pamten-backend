"""One known question to each outside source a day: does it still answer the
way the scrapers read it?

A source changes without telling us. EDGAR's browse-edgar took any `count`
until November 2026 and then answered HTTP 400 to everything outside its own
page sizes; the two name searches sent 5 and were dead for up to a week before
a rebuild happened to run them — 540 requests, a company's SEC part and seven
persons' SEC identity lost, each logged as a request "lost to the network"
(findings item 132). GLEIF, Wikidata and Companies House announce their
breaking changes; EDGAR's search page does not. So the backend asks itself.

A probe is one request (the feeds, two) for a record that does not go away —
Apple's filer number, its LEI, its Wikidata item, Tesco's company number — sent
THROUGH THE SCRAPER'S OWN FUNCTION, so the parameters probed are the ones a
scrape sends, and held to the one thing the scraper reads from the answer. A
probe that fails is asked once more after a pause: a source that is slow for a
moment is not a changed source.

About ten requests a run. `manage.py alerts` runs them as its `probes` check
(daily from the alerts cron); nothing is written.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

APPLE_CIK = "0000320193"
APPLE_LEI = "HWUPKR0MPOU8FGXBT394"
APPLE_QID = "Q312"
COOK_CIK = "0001214156"            # COOK TIMOTHY D, Apple's chief executive
TESCO_NUMBER = "00445790"          # TESCO PLC at Companies House
RETRY_PAUSE = 5.0


class Broken(Exception):
    """The source answered, and not with what the scraper reads."""


@dataclass(frozen=True)
class Probe:
    source: str                    # the name app/source_calls.py counts it under
    name: str                      # what is asked, in words an alert mail can carry
    run: Callable[[], None]        # raises when the answer is not the expected one


def _expect(ok, what: str) -> None:
    if not ok:
        raise Broken(what)


# ── SEC EDGAR ────────────────────────────────────────────────────────────────

def _sec(fn: Callable[[], None]) -> Callable[[], None]:
    """An EDGAR probe that names the request it lost: the scraper's functions
    swallow a failed request (one filing must not sink a scrape), so the probe
    would only see "nothing found" — the URL says what was sent."""
    def probe() -> None:
        from app.scraper import sec_edgar
        with sec_edgar.collect_lost_requests() as lost:
            try:
                fn()
            except Broken as exc:
                if lost:
                    raise Broken(f"{exc}; request lost: {lost[0]}") from exc
                raise
    return probe


def _sec_tickers() -> None:
    from app.scraper import sec_edgar
    rows = sec_edgar._get_tickers() or {}
    _expect(any(str(v.get("cik_str")) == str(int(APPLE_CIK)) for v in rows.values()),
            f"company_tickers.json does not list Apple ({len(rows)} rows)")


def _sec_company_search() -> None:
    from app.scraper import sec_edgar
    found = sec_edgar._browse_index("Apple Inc", "10-K")
    _expect(found is not None, "the search gave no readable answer (refused, or not the Atom feed)")
    _expect(any(cik == APPLE_CIK for cik, _ in found), f"'Apple Inc' among 10-K filers lists {found[:3]}")


def _sec_person_search() -> None:
    from app.scraper import sec_edgar
    found = sec_edgar._search_person_ciks(["Timothy", "Cook"])     # the live search, whatever list is kept
    _expect(COOK_CIK in found, f"'Timothy Cook' among Form 4 filers lists {found}")


def _sec_name_list() -> None:
    from app.scraper import sec_names
    path = sec_names.index_path()
    _expect(path, "the bulk name list could not be downloaded and none is at hand")
    found = sec_names.person_ciks(["Timothy", "Cook"], 50, path)
    _expect(COOK_CIK in (found or []), f"'Timothy Cook' in the bulk name list gives {(found or [])[:5]}")


def _sec_submissions() -> None:
    from app.scraper import sec_edgar
    recent = (sec_edgar._submissions(APPLE_CIK).get("filings") or {}).get("recent") or {}
    _expect(recent.get("form") and recent.get("accessionNumber") and recent.get("primaryDocument"),
            f"Apple's submissions document has no recent filings (keys {sorted(recent)[:6]})")


def _sec_holder_feed() -> None:
    from app.scraper import sec_edgar
    first = next(iter(sec_edgar._ownership_feed_entries(APPLE_CIK)), None)
    _expect(first and first.get("accession") and first.get("index_url"),
            f"the 13D/G feed about Apple gave {first!r}")


def _sec_full_text() -> None:
    from app.scraper import sec_edgar
    data = sec_edgar._get(sec_edgar.SEARCH_URL, {"q": '"Apple Inc"', "forms": "10-K"})
    hits = ((data or {}).get("hits") or {}).get("hits")
    _expect(hits and (hits[0].get("_source") or {}).get("ciks"),
            f"the full-text search gave no hit with a filer number (keys {sorted(data or {})[:5]})")


# ── GLEIF ────────────────────────────────────────────────────────────────────

def _gleif_record() -> None:
    import httpx

    from app.scraper.gleif_incremental import _HEADERS
    from app.scraper.legal_identity import GLEIF_API
    r = httpx.get(GLEIF_API.format(lei=APPLE_LEI, rel="").rstrip("/"), headers=_HEADERS,
                  timeout=30, follow_redirects=True)
    r.raise_for_status()
    entity = ((r.json().get("data") or {}).get("attributes") or {}).get("entity") or {}
    _expect((entity.get("legalName") or {}).get("name") and entity.get("legalAddress"),
            f"Apple's LEI record has no legal name or address (keys {sorted(entity)[:6]})")


def _gleif_relationship() -> None:
    from app.scraper.legal_identity import _gleif_get
    # Apple reports no parent: the answer is GLEIF's "no such relationship" (404,
    # read as None) or a record — anything else raises
    got = _gleif_get(APPLE_LEI, "direct-parent")
    _expect(got is None or "data" in got, f"the direct-parent answer has keys {sorted(got or {})[:5]}")


# ── Wikidata ─────────────────────────────────────────────────────────────────

def _wikidata_by_statement() -> None:
    from app.scraper import wikidata
    found = wikidata.search_by_statement("P5531", APPLE_CIK)
    _expect(any(h.get("id") == APPLE_QID for h in found), f"the item stating CIK {APPLE_CIK} is {found}")


def _wikidata_by_name() -> None:
    from app.scraper import wikidata
    found = wikidata.search_entity("Apple Inc.")
    _expect(any(h.get("id") == APPLE_QID for h in found),
            f"'Apple Inc.' finds {[h.get('id') for h in found][:5]}")


def _wikidata_entity() -> None:
    from app.scraper import wikidata
    names = wikidata._names_for([APPLE_QID]).get(APPLE_QID) or []
    _expect(names, f"{APPLE_QID} came back without a label")


# ── Companies House ──────────────────────────────────────────────────────────

def _companies_house_officers() -> None:
    from app.config import settings
    from app.scraper import ch_officers
    with ch_officers.client(settings.COMPANIES_HOUSE_API_KEY) as http:
        items = ch_officers.fetch_officers(http, TESCO_NUMBER, sleep=lambda _s: None)
    _expect(items, f"Tesco's officers list is {'empty' if items == [] else 'gone (404)'}")
    _expect(items[0].get("name") and (items[0].get("links") or {}).get("officer"),
            f"an officer has no name or no officer link (keys {sorted(items[0])[:6]})")


def probes() -> list[Probe]:
    """The probes this instance can run: Companies House only with its key, the
    bulk name list only where one is kept."""
    from app.config import settings
    out = [
        Probe("sec-edgar", "ticker list", _sec(_sec_tickers)),
        Probe("sec-edgar", "company-name search", _sec(_sec_company_search)),
        Probe("sec-edgar", "person-name search", _sec(_sec_person_search)),
        Probe("sec-edgar", "a filer's submissions", _sec(_sec_submissions)),
        Probe("sec-edgar", "13D/G feed about a company", _sec(_sec_holder_feed)),
        Probe("sec-edgar", "full-text search", _sec(_sec_full_text)),
        Probe("gleif", "an LEI record", _gleif_record),
        Probe("gleif", "an LEI's parent relationship", _gleif_relationship),
        Probe("wikidata", "item by a stated id", _wikidata_by_statement),
        Probe("wikidata", "item by name", _wikidata_by_name),
        Probe("wikidata", "an item's labels", _wikidata_entity),
    ]
    if settings.SEC_NAME_LIST_DIR:
        out.insert(3, Probe("sec-edgar", "bulk name list", _sec(_sec_name_list)))
    if settings.COMPANIES_HOUSE_API_KEY:
        out.append(Probe("companies-house", "a company's officers", _companies_house_officers))
    return out


def _why(exc: BaseException) -> str:
    """What went wrong, with the source's own words when it gave any."""
    import httpx
    if isinstance(exc, Broken):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        body = " ".join((exc.response.text or "").split())[:140]
        return f"HTTP {exc.response.status_code}" + (f": {body}" if body else "")
    return f"{type(exc).__name__}: {exc}"[:200]


def run(which: list[Probe] | None = None, sleep: Callable[[float], None] = time.sleep
        ) -> list[tuple[Probe, str]]:
    """Run the probes; return the ones that failed twice, each with the reason."""
    failed: list[tuple[Probe, str]] = []
    for probe in (probes() if which is None else which):
        why = None
        for attempt in (1, 2):
            try:
                probe.run()
                why = None
                break
            except Exception as exc:  # noqa: BLE001 - whatever a probe raises is its answer
                why = _why(exc)
                log.warning("source probe %s / %s failed (attempt %d): %s",
                            probe.source, probe.name, attempt, why)
                if attempt == 1:
                    sleep(RETRY_PAUSE)
        if why is not None:
            failed.append((probe, why))
    return failed
