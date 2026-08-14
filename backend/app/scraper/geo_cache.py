"""A durable address → coordinate cache.

Nominatim is a shared free service on a one-request-per-second budget, so the
cheapest request is the one never sent. The in-process dict in `geocode.py` only
lasts as long as the process; this survives restarts and is shared by every path
— scrapes, the backfill, the delta cron.

It pays because **company addresses repeat**. 36% of the registered addresses in
the dev graph are duplicates: 51 companies at 1 Churchill Place, 24 at 251 Little
Falls Drive in Wilmington, which is one registered agent's building. At full-
import scale that ratio gets far better, because a handful of agents serve
enormous numbers of companies. Cost becomes proportional to distinct addresses
rather than to companies.

**Misses are cached too**, with the date they were checked. 230 of the dev
addresses resolve to nothing — agents' buildings OpenStreetMap has never heard
of — and re-asking about them on every run is the single most wasteful thing this
module could do. They are retried after `_MISS_TTL_DAYS`, because OSM does
improve and a permanent "no" would be a lie.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.db.arcadedb import run_query, run_command

log = logging.getLogger(__name__)

Coord = tuple[float, float]

#: How long a "not found" stands before we ask again. OpenStreetMap gains
#: addresses continually; a month is short enough to pick that up and long
#: enough that a full pass does not re-ask about everything.
_MISS_TTL_DAYS = 30


def lookup(query: str) -> tuple[Coord | None, str | None] | None:
    """A cached answer for `query`, or None if we have never (recently) asked.

    Returns ``(coord, precision)`` on a hit and ``(None, None)`` for a cached
    miss — distinguishable from "not cached", which is the whole point.
    """
    if not query:
        return None
    try:
        rows = run_query(
            "MATCH (g:GeoCache {query: $q}) RETURN g.lat AS lat, g.lng AS lng, "
            "g.precision AS precision, g.checked_at AS checked_at LIMIT 1",
            {"q": query},
        )
    except ConnectionError:
        # Deliberately NOT swallowed. If the database is unreachable the caller
        # cannot store the answer either — on the entity or here — so geocoding
        # anyway would spend a request from a rate-limited free service to
        # produce a coordinate that goes nowhere, and would hide the outage
        # behind a slow, silently useless run. Fail here, before the request.
        #
        # Nothing else needs catching: a MATCH on a type that does not exist
        # returns an empty result, so a database predating GeoCache reads as
        # "not cached" without any special handling.
        raise
    if not rows:
        return None
    row = rows[0]
    if row.get("lat") is not None and row.get("lng") is not None:
        return (float(row["lat"]), float(row["lng"])), row.get("precision")

    # A cached miss — honour it only while it is fresh.
    checked = row.get("checked_at")
    if checked:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(checked)
            if age < timedelta(days=_MISS_TTL_DAYS):
                return None, None
        except ValueError:
            pass          # unparseable stamp → treat as stale and ask again
    return None


def store(query: str, coord: Coord | None, precision: str | None) -> None:
    """Remember an answer, hit or miss. Best-effort: a cache write must never be
    the reason a geocode fails."""
    if not query:
        return
    now = datetime.now(timezone.utc).isoformat()
    lat, lng = coord if coord else (None, None)
    try:
        run_command(
            "MATCH (g:GeoCache {query: $q}) SET g.lat = $lat, g.lng = $lng, "
            "g.precision = $prec, g.checked_at = $now",
            {"q": query, "lat": lat, "lng": lng, "prec": precision, "now": now},
        )
        # Upsert without MERGE: ArcadeDB's Cypher MERGE on a non-indexed property
        # has bitten this codebase before, and a duplicate row here is harmless —
        # lookup takes the first.
        existing = run_query("MATCH (g:GeoCache {query: $q}) RETURN g.query AS q LIMIT 1",
                             {"q": query})
        if not existing:
            run_command(
                "CREATE (:GeoCache {query: $q, lat: $lat, lng: $lng, "
                "precision: $prec, checked_at: $now})",
                {"q": query, "lat": lat, "lng": lng, "prec": precision, "now": now},
            )
    except ConnectionError:
        # Same reasoning as `lookup`: an unreachable database is the caller's
        # problem to hear about, not something to paper over.
        raise
    except Exception as exc:  # noqa: BLE001
        # Anything else is cache-specific and survivable — most plausibly two
        # workers racing to CREATE the same address and one losing to the UNIQUE
        # index. The coordinate is already paid for and still gets written to the
        # entity; only the cache entry is lost.
        log.warning("Geocode cache write failed for %r: %s", query[:60], exc)


def stats() -> dict:
    """Counts for the scraper panel and for judging whether this is earning its keep."""
    total = run_query("MATCH (g:GeoCache) RETURN count(g) AS n")
    hits = run_query("MATCH (g:GeoCache) WHERE g.lat IS NOT NULL RETURN count(g) AS n")
    t = (total[0]["n"] if total else 0) or 0
    h = (hits[0]["n"] if hits else 0) or 0
    return {"addresses": t, "resolved": h, "unresolved": t - h}
