"""How often the backend asks each outside source, and how often the shared
bucket answered instead.

One question, two places to count it:

- **Network calls to a source** are counted in ONE place, the httpx transport,
  rather than at each call site. The backend reaches EDGAR, GLEIF, Wikidata,
  Wikimedia, Nominatim, Companies House and OpenCorporates through a pooled
  client in some modules and a bare ``httpx.get`` in others; a counter at the call
  sites would miss the next one somebody adds (the sibling-path lesson). Every
  retry is a call of its own — it is what the source sees.
- **The bucket** (``app/objectstore.py``) is counted in its four functions: a get
  that found the object (``hit``) is a source fetch saved, a ``miss`` falls
  through to the source.

The key space is closed: a host that is not in ``SOURCE_HOSTS`` is not counted
(the database itself, the mail provider, the dead man's switch, federation
peers). A source with a new host goes into the map, and a test checks every host
the scrapers name is in it.

Each call also carries **what was asked** (`request_type`): for EDGAR the
filer record, a filing document, a name search … — a rebuild's 17,800 EDGAR
calls were one number, and which request to save could only be sampled by hand
(findings item 135). A source without a rule has the empty type.

Counts are kept in memory and written as one ``SourceCall`` row per
(UTC day, source, kind, request type) — every minute by a daemon thread, and once more when the
process exits — so a rebuild's many short ``manage.py`` processes and the
long-running API all land in the same rows. ``start()`` turns it on; a process
that never calls it (the unit tests) never writes.
"""
from __future__ import annotations

import atexit
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: host suffix -> source name. Matched on the host or any subdomain of it.
SOURCE_HOSTS: dict[str, str] = {
    "sec.gov": "sec-edgar",
    "gleif.org": "gleif",
    "wikidata.org": "wikidata",
    "wikimedia.org": "wikimedia",
    "wikipedia.org": "wikimedia",
    "openstreetmap.org": "nominatim",
    "company-information.service.gov.uk": "companies-house",
    "companieshouse.gov.uk": "companies-house",
    "opencorporates.com": "opencorporates",
}

#: the source name every bucket count carries, before its prefix
BUCKET = "bucket"
FLUSH_SECONDS = 60

_counts: Counter = Counter()
_lock = threading.Lock()
_started = False


def source_for_host(host: str | None) -> str | None:
    """The source a host belongs to, or None when it is not one we count."""
    host = (host or "").lower().rstrip(".")
    for suffix, source in SOURCE_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return source
    return None


#: 4xx answers that mean "that record is not here" — an answer to the question
NOT_THERE = frozenset({404, 410})
#: the kind of every other 4xx (but 429)
REFUSED = "refused"


def status_kind(status: int) -> str:
    """A response's kind. 429 apart: a source telling us to slow down is worth
    seeing on its own. So is `refused`: a 4xx that is not "not there" (400, 401,
    403, 405, 422 …) says the REQUEST is not accepted — a parameter the source
    stopped taking, a key it no longer knows, an address it blocks. A day's
    scraping has hundreds of 404s and none of these; EDGAR's `Invalid count`
    (HTTP 400, findings item 132) sat among the 404s as one more "4xx" for a
    week. `app/alerts.py` reports them (`check_refused`)."""
    if status == 429:
        return "429"
    if status < 400:
        return "ok"
    if status >= 500:
        return "5xx"
    return "4xx" if status in NOT_THERE else REFUSED


#: EDGAR's request types, by what the URL asks for. A closed set: `other`
#: is a URL none of the rules know — a new endpoint somebody added, worth a rule.
SEC_TYPES = ("filer-record", "filer-record-page", "filing-document", "filing-folder",
             "filing-feed", "company-search", "person-search", "full-text", "xbrl",
             "insider-page", "tickers", "name-list", "other")
_SEC_FILER_RECORD = re.compile(r"/submissions/CIK\d+\.json$")
_SEC_ARCHIVE = re.compile(r"/Archives/edgar/data/\d+(/.*)?$")


def _sec_request_type(path: str, params: dict) -> str:
    if path.startswith("/submissions/"):
        # the record itself, or one of its older pages of filings
        return "filer-record" if _SEC_FILER_RECORD.search(path) else "filer-record-page"
    if path.endswith("/browse-edgar"):
        if params.get("CIK"):
            return "filing-feed"                 # one filer's filings, by its number
        if "company" in params:                  # the name index; type 4 = reporting owners
            return "person-search" if params.get("type") == "4" else "company-search"
        return "other"
    if path.endswith("/search-index"):
        return "full-text"
    if path.startswith("/api/xbrl/"):
        return "xbrl"
    if path.endswith("/own-disp"):
        return "insider-page"
    if path.endswith("/company_tickers.json"):
        return "tickers"
    if path.endswith("/cik-lookup-data.txt"):
        return "name-list"
    m = _SEC_ARCHIVE.search(path)
    if m:
        last = (m.group(1) or "").rstrip("/").rsplit("/", 1)[-1]
        # a folder, or its listing: no file with a document's extension
        return "filing-folder" if not last or last == "index.json" or "." not in last else "filing-document"
    return "other"


def request_type(source: str, url) -> str:
    """What a request asks its source for, from the URL (an ``httpx.URL`` or a
    string); "" for a source with no rule."""
    if source != "sec-edgar":
        return ""
    import httpx
    url = httpx.URL(str(url))
    return _sec_request_type(url.path, dict(url.params))


def count(source: str, kind: str, n: int = 1, what: str = "") -> None:
    with _lock:
        _counts[(_today(), source, kind, what)] += n


def count_bucket(key: str, kind: str) -> None:
    """One bucket operation, under the key's prefix (``edgar/…`` -> ``bucket:edgar``)."""
    prefix = (key or "").split("/", 1)[0] or "-"
    count(f"{BUCKET}:{prefix}", kind)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def drain() -> dict[tuple[str, str, str, str], int]:
    with _lock:
        out = dict(_counts)
        _counts.clear()
    return out


def flush() -> int:
    """Write what has been counted; on a failed write the counts go back, so the
    next flush retries them. Returns the rows written."""
    from app.db.arcadedb import run_sql

    pending = drain()
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for (day, source, kind, what), n in pending.items():
        try:
            run_sql(
                "UPDATE SourceCall SET key = :k, day = :d, source = :s, kind = :kind, what = :w, "
                "count = COALESCE(count, 0) + :n, last_seen = :now UPSERT WHERE key = :k",
                {"k": f"{day}|{source}|{kind}" + (f"|{what}" if what else ""), "d": day,
                 "s": source, "kind": kind, "w": what, "n": int(n), "now": now})
            written += 1
        except Exception as exc:  # noqa: BLE001 - counting must never break a scrape
            log.warning("could not write source call count %s %s: %s", source, kind, exc)
            with _lock:
                _counts[(day, source, kind, what)] += n
    return written


# ── the transport hook ───────────────────────────────────────────────────────

def _counted(handle):
    def wrapper(self, request):
        source = source_for_host(request.url.host)
        if source is None:
            return handle(self, request)
        what = request_type(source, request.url)
        try:
            response = handle(self, request)
        except Exception:
            count(source, "error", what=what)
            raise
        count(source, status_kind(response.status_code), what=what)
        return response
    wrapper.__source_calls__ = True
    return wrapper


def _counted_async(handle):
    async def wrapper(self, request):
        source = source_for_host(request.url.host)
        if source is None:
            return await handle(self, request)
        what = request_type(source, request.url)
        try:
            response = await handle(self, request)
        except Exception:
            count(source, "error", what=what)
            raise
        count(source, status_kind(response.status_code), what=what)
        return response
    wrapper.__source_calls__ = True
    return wrapper


def install() -> None:
    """Count every request httpx sends through its default transports (a bare
    ``httpx.get``, a pooled ``httpx.Client``, a stream). Idempotent."""
    import httpx

    if not getattr(httpx.HTTPTransport.handle_request, "__source_calls__", False):
        httpx.HTTPTransport.handle_request = _counted(httpx.HTTPTransport.handle_request)
    if not getattr(httpx.AsyncHTTPTransport.handle_async_request, "__source_calls__", False):
        httpx.AsyncHTTPTransport.handle_async_request = _counted_async(
            httpx.AsyncHTTPTransport.handle_async_request)


def _flush_forever() -> None:
    import time
    while True:
        time.sleep(FLUSH_SECONDS)
        try:
            flush()
        except Exception as exc:  # noqa: BLE001
            log.warning("source call flush failed: %s", exc)


def start() -> None:
    """Count and store, for the rest of this process. Idempotent; a no-op when
    ``SOURCE_CALLS_ENABLED`` is off (the test suite)."""
    global _started
    from app.config import settings
    if not settings.SOURCE_CALLS_ENABLED:
        return
    install()
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_flush_forever, name="source-calls", daemon=True).start()
    atexit.register(_flush_at_exit)


def _flush_at_exit() -> None:
    try:
        flush()
    except Exception as exc:  # noqa: BLE001 - the database may already be gone
        log.warning("source call flush at exit failed: %s", exc)


def totals(day_from: str, day_to: str) -> dict[str, dict[str, int]]:
    """{source: {kind: count}} for the UTC days ``day_from`` <= day < ``day_to``."""
    from app.db.arcadedb import run_sql

    out: dict[str, dict[str, int]] = {}
    for r in run_sql("SELECT source, kind, sum(count) AS n FROM SourceCall "
                     "WHERE day >= :a AND day < :b GROUP BY source, kind",
                     {"a": day_from, "b": day_to}):
        if r.get("source") and r.get("kind"):
            out.setdefault(r["source"], {})[r["kind"]] = int(r.get("n") or 0)
    return dict(sorted(out.items()))


def totals_by_type(day_from: str, day_to: str) -> dict[str, dict[str, int]]:
    """{source: {request type: count}} for the same days, every kind together
    (a retry is a call); only the calls that carry a type (`request_type`)."""
    from app.db.arcadedb import run_sql

    out: dict[str, dict[str, int]] = {}
    for r in run_sql("SELECT source, what, sum(count) AS n FROM SourceCall "
                     "WHERE day >= :a AND day < :b GROUP BY source, what",
                     {"a": day_from, "b": day_to}):
        if r.get("source") and r.get("what"):
            out.setdefault(r["source"], {})[r["what"]] = int(r.get("n") or 0)
    return dict(sorted(out.items()))


def format_totals(by_source: dict[str, dict[str, int]],
                  by_type: dict[str, dict[str, int]] | None = None) -> str:
    """The totals as text, network sources first, then the bucket prefixes;
    under a source its request types (``by_type``), the most asked first."""
    if not by_source:
        return "  none counted\n"
    lines = []
    network = {s: k for s, k in by_source.items() if not s.startswith(BUCKET + ":")}
    bucket = {s: k for s, k in by_source.items() if s.startswith(BUCKET + ":")}
    for src, kinds in sorted(network.items(), key=lambda kv: -sum(kv[1].values())):
        lines.append(f"  {src:22} {sum(kinds.values()):>9,}  "
                     + ", ".join(f"{k} {n:,}" for k, n in sorted(kinds.items())))
        for what, n in sorted(((by_type or {}).get(src) or {}).items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"      {what:22} {n:>9,}")
    for src, kinds in sorted(bucket.items()):
        lines.append(f"  {src:22} {'':>9}  "
                     + ", ".join(f"{k} {n:,}" for k, n in sorted(kinds.items())))
    return "\n".join(lines) + "\n"
