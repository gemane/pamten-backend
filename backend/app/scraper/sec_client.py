"""
SEC EDGAR — the client every reader goes through.

One pooled HTTP client with the fair-access pause and User-Agent, the retry on
429/503 and read errors, the record of requests lost to the network and of the
sections a scrape lost with them, the filer record (`_submissions`) with its
per-scrape and per-pass memo, and the CIK, date and URL helpers.

Source, rate limits and licence: see `sec_edgar`.
"""

import collections
import contextlib
import contextvars
import re
import time
import threading
import logging

import httpx

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Owlgraph/1.0 contact@owlgraph.org",
    "Accept":     "application/json",
}
REQUEST_DELAY    = 0.12   # stay comfortably under 10 req/s
#: the sections a company scrape can lose to the network (findings item 41)
SECTION_OWNERS   = "13D/G owners"
SECTION_INSIDERS = "insiders"
SECTION_HOLDINGS = "holdings"
#: the issuer's ownership table: a holder's name search lost, so the holder was
#: not written — not "EDGAR knows no such registrant" (findings item 155)
SECTION_TABLE    = "ownership table"
READ_TIMEOUT     = 10.0   # seconds without a byte before the one retry (item 38)


SEARCH_URL      = "https://efts.sec.gov/LATEST/search-index"
BROWSE_URL      = "https://www.sec.gov/cgi-bin/browse-edgar"
SUBMISSIONS_URL = "https://data.sec.gov/submissions"
ARCHIVES_URL    = "https://www.sec.gov/Archives/edgar/data"
TICKERS_URL     = "https://www.sec.gov/files/company_tickers.json"
COMPANYCONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept"


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
                    # Short read timeout too (findings item 38): sec.gov leaves
                    # ~1 request in 30 hanging, and the retry in _response, on a
                    # new connection, answers in 0.1-0.2 s, so waiting longer is
                    # pure loss. The timeout is the gap between bytes, not the
                    # whole download, and the slowest normal answer measured
                    # (EFTS, browse, submissions, tickers) begins in 0.63 s.
                    timeout=httpx.Timeout(connect=5.0, read=READ_TIMEOUT, write=30.0, pool=10.0),
                )
    return _client


def close_client() -> None:
    """Release the pooled connection (called on app shutdown)."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


# One retry, honouring Retry-After, capped so a hostile header cannot park the
# scrape for an hour. efts.sec.gov (full-text search) throttles harder than
# www.sec.gov, and the new-source checklist has demanded this since it was
# written — every source shares these two functions, so they all get it.
_RETRY_STATUSES = (429, 503)
_RETRY_AFTER_CAP = 30.0

# A request sec.gov leaves hanging, or a connection it drops, gets the same one
# retry (findings item 38). Measured 2026-11-02: 4 of 120 plain archive GETs hung
# until the read timeout and some 503s took 24-27 s to arrive; the next request,
# on a new connection, answered every time. httpx discards the connection a
# timeout or a protocol error broke, so the retry never reuses it. Before this
# the error went straight to the caller, which drops the data (findings item 41).
_RETRY_ERRORS = (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)


# ── Requests lost to the network (findings item 45) ───────────────────────────
#
# `_response` used to log "retrying once" and nothing after it: when the retry
# failed too, or a 429/503 came back twice, or an error it does not retry (a
# refused connection, a 500, a 403) ended the request, the exception went to the
# caller — and about twenty callers drop it at debug level (a Form 4, a 13D/G
# document, a filing index), because one missing filing must not sink a scrape.
# The 2026-11-02 rebuild logged 85 "retrying once" lines and could not say which
# of them lost a filing: a Form 4 stalled during Morgan Stanley's scrape, whose
# SEC count was 70 against 75 in the two rebuilds before. So the loss is told
# where it happens: one warning per request that ended in an error, and a count
# for the scrape that asked (`collect_lost_requests`). A 404 is EDGAR saying
# "not there", an answer, and is neither.
_lost_requests: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "sec_lost_requests", default=None)


@contextlib.contextmanager
def collect_lost_requests():
    """Collect the URLs of the SEC requests lost inside the block.

    The runner wraps a whole company scrape in it — the scrape itself and the
    lookups its writer makes afterwards (an insider's own Form 4) — and reports
    the count as the run's `lost_requests`.

    Nested, it hands what it collected on to the block around it: a step that
    asks "did THIS lookup lose a request?" (a table holder's name search,
    findings item 155) must not hide the loss from the scrape that counts."""
    outer = _lost_requests.get()
    bucket: list[str] = []
    token = _lost_requests.set(bucket)
    try:
        yield bucket
    finally:
        _lost_requests.reset(token)
        if outer is not None:
            outer.extend(bucket)


def _refusal(exc: BaseException) -> str | None:
    """EDGAR's own words when it refused the request — a 4xx that is neither
    "not there" nor "slow down" — else None (the network, a 5xx)."""
    from app.source_calls import REFUSED, status_kind
    if not isinstance(exc, httpx.HTTPStatusError) or status_kind(exc.response.status_code) != REFUSED:
        return None
    try:
        return " ".join((exc.response.text or "").split())[:160]
    except Exception:  # noqa: BLE001 - a body that cannot be read is no reason to lose the line
        return ""


def _request_lost(url: str, params: dict | None, exc: BaseException, retried: bool) -> None:
    """Log and count a request that ended in an error; a 404 is not one."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
        return
    shown = str(httpx.URL(url, params=params)) if params else url
    what = (f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError)
            else type(exc).__name__)
    refusal = _refusal(exc)
    if refusal is None:
        log.warning("SEC EDGAR: request lost (%s%s) on %s", what,
                    ", after the retry" if retried else "", shown)
    else:
        # Not the network: EDGAR read the request and said no. Its reason is in
        # the body ("Invalid count, Received: 5, Count must be one of: …",
        # findings item 132) and a retry will get the same answer, so it is an
        # error, with the reason, on the same "request lost" line the logs are
        # searched for.
        log.error("SEC EDGAR: request lost (%s%s) on %s — REFUSED by EDGAR: %s", what,
                  ", after the retry" if retried else "", shown, refusal or "no reason given")
    bucket = _lost_requests.get()
    if bucket is not None:
        bucket.append(shown)


def _response(url: str, params: dict | None) -> httpx.Response:
    # Filings are immutable, so an Archives file is served from the on-disk
    # cache when SEC_CACHE_DIR is set; searches and listings never are — see
    # sec_cache.cacheable for the line, and why it is drawn there.
    from app import objectstore
    from app.config import settings
    from app.scraper import sec_cache
    key = (sec_cache.cacheable(url, params)
           if (settings.SEC_CACHE_DIR or objectstore.enabled()) else None)
    if key:
        cached = sec_cache.lookup(key)
        if cached is not None:
            return httpx.Response(200, content=cached,
                                  request=httpx.Request("GET", url))
    retried = False
    try:
        try:
            r = _get_client().get(url, params=params)
        except _RETRY_ERRORS as exc:
            log.warning("SEC EDGAR: %s on %s, retrying once", type(exc).__name__, url)
            retried = True
            time.sleep(REQUEST_DELAY)
            r = _get_client().get(url, params=params)
        else:
            if r.status_code in _RETRY_STATUSES:
                try:
                    wait = min(float(r.headers.get("Retry-After", 2)), _RETRY_AFTER_CAP)
                except ValueError:        # a date-formatted Retry-After — just pause briefly
                    wait = 2.0
                log.warning("SEC EDGAR: HTTP %d on %s, retrying once after %.0f s",
                            r.status_code, url, wait)
                retried = True
                time.sleep(wait)
                r = _get_client().get(url, params=params)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        _request_lost(url, params, exc, retried)
        raise
    if key:
        sec_cache.store(key, r.content)
    time.sleep(REQUEST_DELAY)
    return r


def _get(url: str, params: dict | None = None) -> dict:
    return _response(url, params).json()


# ── Sections a scrape lost to the network (findings item 41) ──────────────────
#
# A whole section of a company scrape rests on one request: the 13D/G list on
# the browse feed, the insiders and the filer-side holdings on a submissions
# file. When that request failed, the section came back [], the answer of a
# company with no such filings, and the run still ended `ok`: the 2026-11-01
# rebuild lost Gitlab's owners (36 -> 17), HubSpot's (30 -> 14) and Embraer's
# (10 -> 4) to read timeouts, and nothing said so. `scrape_company` opens a
# collector; a section whose request fails for a reason other than "not there"
# (404) is named in it, and the runner reports the run `partial`.
_failed_sections: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "sec_failed_sections", default=None)


def _section_failed(section: str, exc: BaseException) -> bool:
    """Record `section` as lost when `exc` is a network failure; True if so.

    A 404 is EDGAR saying there is nothing (a CIK with no submissions file),
    which is an answer, not a loss. Outside a `scrape_company` call there is no
    collector and nothing is recorded."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
        return False
    if not isinstance(exc, httpx.HTTPError):
        return False
    bucket = _failed_sections.get()
    if bucket is not None and section not in bucket:
        bucket.append(section)
    return True


def _get_text(url: str, params: dict | None = None) -> str:
    return _response(url, params).text


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


#: How many filer records a whole-graph pass keeps at hand (`filer_records_for_a_pass`).
#: A record is 0.8 MB in memory, BlackRock's 2.6 MB, and a pass over a thousand
#: issuers meets some 1,700 filers — all of them kept would be gigabytes on a
#: 4 GB host. The repeats are the big holders, met again within a few companies:
#: on 40 companies of the table pass the newest 50 saved 143 of the 151 requests
#: that keeping everything would have.
FILER_RECORDS_KEPT = 64


class _FilerRecords:
    """The filer records at hand, by padded CIK; with ``keep``, only the
    ``keep`` handed out or read most recently."""

    def __init__(self, keep: int | None = None):
        self.keep = keep
        self._records: collections.OrderedDict[str, dict] = collections.OrderedDict()

    def get(self, key: str) -> dict | None:
        sub = self._records.get(key)
        if sub is not None:
            self._records.move_to_end(key)
        return sub

    def put(self, key: str, sub: dict) -> None:
        self._records[key] = sub
        self._records.move_to_end(key)
        while self.keep and len(self._records) > self.keep:
            self._records.popitem(last=False)

    def __len__(self) -> int:
        return len(self._records)


#: The filer records read inside one scrape (`one_filer_record_per_scrape`) or
#: one pass (`filer_records_for_a_pass`); None outside one — every read is then
#: a request.
_filer_records: contextvars.ContextVar[_FilerRecords | None] = contextvars.ContextVar(
    "sec_filer_records", default=None)


@contextlib.contextmanager
def one_filer_record_per_scrape():
    """Inside the block a filer's record (`_submissions`) is requested once and
    handed out again after that.

    One scrape reads the same record over and over: a holder's scrape asked for
    its own record once per holding — Bank of New York Mellon's 87 times,
    Berkshire Hathaway's 70 — and of 774 records read for 48 targets, 663 had
    been read a moment before in the same scrape (median 0.2 s apart): 57 % of
    all the scrape's requests (findings item 135, measured 2026-11-23).

    The memo ends with the block, so the next scrape reads every record anew
    and sees the filings made since — the reason a process-long cache was
    refused (`_submissions`). Nested blocks share the outer one's memo: the
    runner opens it around a company's scrape AND its writer, `scrape_company`
    opens it for a caller that did not; inside a pass's block
    (`filer_records_for_a_pass`) it is the pass's. A failed read is not kept."""
    if _filer_records.get() is not None:
        yield
        return
    token = _filer_records.set(_FilerRecords())
    try:
        yield
    finally:
        _filer_records.reset(token)


@contextlib.contextmanager
def filer_records_for_a_pass(keep: int = FILER_RECORDS_KEPT):
    """Inside the block the ``keep`` filer records used last stay at hand — for
    a pass over the whole graph, one command's run.

    The pass that reads each issuer's ownership table and the 13D/G statements
    filed since asks for the record of every holder it writes, and the same
    holders stand at company after company: on 40 companies it made 292
    requests, 276 of them a filer's record for 125 filers — FMR's 20 times,
    BlackRock's 19 (measured 2026-11-24). It was over half of a rebuild's
    11,753 filer-record requests.

    Bounded, because a record is large (`FILER_RECORDS_KEPT`); and so a record
    is also never old: it leaves once ``keep`` others were used after it, a
    few companies later. The block ends with the pass — the next command reads
    everything anew. Opened by the pass itself, never around a server's life."""
    if _filer_records.get() is not None:
        yield
        return
    token = _filer_records.set(_FilerRecords(keep))
    try:
        yield
    finally:
        _filer_records.reset(token)


def _submissions(cik: str) -> dict:
    """The EDGAR submissions document for a CIK.

    Deliberately NOT cached for the process. Several helpers read this same
    document — the filer's name, former names, LEI and country — and it also
    carries `filings.recent`: a process-lifetime cache would serve a later scrape
    an earlier one's filings, silently missing new ones until a restart.
    Metadata tolerates staleness; filings do not, and they share a URL. (Tried
    it. The SEC holdings tests failed immediately with one test seeing another's
    filings — the same bug in miniature.)

    Within ONE scrape it is read once (`one_filer_record_per_scrape`), and a
    whole-graph pass keeps the few it used last (`filer_records_for_a_pass`): the
    document a scrape works from is the one it read first. The callers read it,
    none changes it — the memo hands out the same object.
    """
    return _filer_record(cik, _get)


def _filer_record(cik: str, get) -> dict:
    """`_submissions` for a reader that fetches with its own module's `_get`
    (the Exhibit 21 and Form D readers, whose tests stub theirs): the record at
    hand in the scrape's or the pass's block, else requested with ``get`` and
    kept. One read path for the record, whoever fetches — a reader that built
    the URL itself went past the memo, and the subsidiary list asked for its
    filer's record three and four times a company."""
    key = _cik10(cik)
    memo = _filer_records.get()
    kept = memo.get(key) if memo is not None else None
    if kept is not None:
        return kept
    sub = get(f"{SUBMISSIONS_URL}/CIK{key}.json")
    if memo is not None:
        memo.put(key, sub)
    return sub


def _iso_date(raw: str | None) -> str | None:
    """EDGAR's MM-DD-YYYY (13F periodOfReport) → ISO YYYY-MM-DD; ISO passes through."""
    if not raw:
        return None
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", raw.strip())
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else raw


def _short_form(form_type: str | None) -> str | None:
    """"SCHEDULE 13G/A" / "SC 13G/A" → "13G/A" — a display label, not the record.

    Both eras' spellings collapse to the number the rules are named after; the
    raw string stays reachable through source_url. Unknown shapes pass through
    untouched rather than being guessed at.
    """
    if not form_type:
        return None
    short = re.sub(r"^(SCHEDULE|SC)\s+", "", form_type.strip(), flags=re.I)
    short = re.sub(r"^13F-HR", "13F", short)
    return short or None


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

#: The page sizes browse-edgar accepts. Until November 2026 it took any number;
#: since then anything else is answered with HTTP 400 "Invalid count, Received:
#: 5, Count must be one of: 10, 20, …" — and the scraper's 5 lost every name
#: search of a rebuild: 540 requests, Petrobras' SEC part, the SEC identity of
#: seven Wikidata persons (findings item 132). So no caller writes the number
#: into its request: it says how many entries it uses, `browse_count` asks for
#: the smallest page that holds them, and the caller cuts the answer to its own
#: number.
BROWSE_PAGE_SIZES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 500)


def browse_count(wanted: int) -> str:
    """The `count` to send browse-edgar for ``wanted`` entries: the smallest
    page size EDGAR accepts that holds them (its largest for more)."""
    return str(next((n for n in BROWSE_PAGE_SIZES if n >= wanted), BROWSE_PAGE_SIZES[-1]))
