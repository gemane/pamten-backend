"""
Counting what people look for, without watching who is looking.

The product question is "what do users search for, and what do they not find" —
so the roadmap can follow demand rather than guesswork. That question is fully
answered by counts per query. It does not need a user id, a session, an IP, or a
per-event timestamp, and none of those is stored.

That is the whole privacy design, and it is deliberate rather than incidental:

- **No linkage.** A row says "'siemens' in DE was searched 12 times, 3 of them
  found nothing". There is no way back from it to a person, so nobody's behaviour
  is being monitored and the record of processing can keep saying so.
- **No event log.** Only `first_seen`/`last_seen` on an aggregate. Two searches an
  hour apart and two a year apart leave the same row, which is precisely what
  makes a visit impossible to reconstruct.
- **Free text is the one residual risk.** Somebody will search a person's name,
  and that name lands in `query`. It is not tied to who searched, it is
  admin-only, and it is pruned after twelve idle months — but it is not nothing,
  and the RoPA says so rather than claiming the store is anonymous in every
  direction.

Written from an allow-list, never from client-supplied keys: this is fed by a
public endpoint, and an open-ended key column would be unbounded row growth and a
junk-injection vector in one.
"""
import logging
from datetime import datetime, timezone

from app.weekly import iso_week

log = logging.getLogger(__name__)

#: Every usage event the client may report. Anything else is rejected — see the
#: module docstring on why the key space is closed.
USAGE_EVENTS = frozenset({
    "export.png", "export.csv", "share.link",
    "map.basis.jurisdiction", "map.basis.hq", "map.drill",
    "panel.timeline", "graph.expand", "graph.filter.stake",
    "scrape.requested",
})

#: Clicked result positions worth distinguishing. Beyond this the answer is the
#: same either way — the ranking is wrong.
MAX_RANK = 49

SEARCH_OUTCOMES = frozenset({"selected", "zero", "abandoned"})

#: Latency buckets (ms, upper bound). Buckets rather than a running mean: a mean
#: hides the tail, and the tail is the thing worth fixing.
LATENCY_BUCKETS = (100, 500, 1000, 5000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def search_key(query: str, country: str | None) -> str:
    """One row per question asked.

    Normalised so "Alphabet Inc." and "alphabet" are one question rather than
    two, and country-scoped because "alphabet in France" is a different question
    from "alphabet" — the answer to one says nothing about the other.
    """
    from app.scraper.mapper import normalize_entity_name

    name = normalize_entity_name(query) or (query or "").strip().lower()
    return f"{name}|{(country or '').strip().upper()}"


def latency_bucket(ms: float) -> str:
    """The bucket a request duration falls in, as a label."""
    for bound in LATENCY_BUCKETS:
        if ms < bound:
            return f"<{bound}ms"
    return f">={LATENCY_BUCKETS[-1]}ms"


def record_search(query: str, country: str | None, outcome: str) -> None:
    """Count one *settled* search — a result chosen, or a query given up on.

    Never a keystroke. The search box queries every 300 ms while typing, so
    counting requests would record "mi", "mic", "micr" — useless as demand data,
    and a sharper picture of someone's typing than of their intent.
    """
    from app.db.arcadedb import run_sql

    if outcome not in SEARCH_OUTCOMES:
        raise ValueError(f"unknown search outcome {outcome!r}")

    key = search_key(query, country)
    now = _now()
    # Counters move by name so the three outcomes stay independent: a search can
    # be counted, find nothing, and still not be the same event as an abandonment.
    zero = 1 if outcome == "zero" else 0
    selected = 1 if outcome == "selected" else 0
    q, c = (query or "").strip()[:120], (country or "").strip().upper()
    run_sql(
        "UPDATE SearchDemand SET key = :k, query = :q, country = :c, "
        "searches = COALESCE(searches, 0) + 1, "
        "zero_results = COALESCE(zero_results, 0) + :zero, "
        "selected = COALESCE(selected, 0) + :sel, "
        "first_seen = COALESCE(first_seen, :now), last_seen = :now "
        "UPSERT WHERE key = :k",
        {"k": key, "q": q, "c": c, "zero": zero, "sel": selected, "now": now},
    )
    # The same total again, per calendar week — the lifetime row cannot say how
    # many of its searches happened THIS week, and the digest asks exactly that.
    # Still a total: no user, no session, no timestamp on any one search.
    week = iso_week()
    run_sql(
        "UPDATE SearchWeek SET key = :k, query = :q, country = :c, week = :w, "
        "searches = COALESCE(searches, 0) + 1, "
        "zero_results = COALESCE(zero_results, 0) + :zero, "
        "selected = COALESCE(selected, 0) + :sel "
        "UPSERT WHERE key = :k",
        {"k": f"{key}|{week}", "q": q, "c": c, "w": week, "zero": zero, "sel": selected},
    )


def record_usage(event: str) -> None:
    """Count one allow-listed interaction."""
    from app.db.arcadedb import run_sql

    if event not in USAGE_EVENTS:
        raise ValueError(f"unknown usage event {event!r}")
    _bump(run_sql, event)


def record_rank(rank: int) -> None:
    """Count which result position was chosen.

    Its own counter rather than a property of the search row: the question is
    about the ranking as a whole ("do people click the fourth result?"), not
    about any one query.
    """
    from app.db.arcadedb import run_sql

    if not 0 <= int(rank) <= MAX_RANK:
        raise ValueError(f"rank {rank} out of range")
    _bump(run_sql, f"result.rank.{int(rank)}")


def _bump(run_sql, key: str) -> None:
    now = _now()
    run_sql(
        "UPDATE UsageCounter SET key = :k, count = COALESCE(count, 0) + 1, "
        "first_seen = COALESCE(first_seen, :now), last_seen = :now UPSERT WHERE key = :k",
        {"k": key, "now": now},
    )
    week = iso_week()
    run_sql(
        "UPDATE UsageWeek SET key = :k, event = :e, week = :w, "
        "count = COALESCE(count, 0) + 1 UPSERT WHERE key = :k",
        {"k": f"{key}|{week}", "e": key, "w": week},
    )


def flush_endpoint_stats(counts: dict[str, int]) -> None:
    """Write accumulated request counts, from the in-memory window.

    Accumulated rather than written per request for the obvious reason, and keyed
    on the route *template* (`GET /entities/{id} 2xx <100ms`) rather than the path
    — a key per company id would be both unbounded and a list of what was looked
    at, which is exactly what this design refuses to keep.
    """
    from app.db.arcadedb import run_sql

    now = _now()
    for key, n in counts.items():
        try:
            run_sql(
                "UPDATE EndpointStat SET key = :k, count = COALESCE(count, 0) + :n, "
                "last_seen = :now UPSERT WHERE key = :k",
                {"k": key, "n": int(n), "now": now},
            )
        except Exception as exc:  # noqa: BLE001 - metrics must never break a request path
            log.warning("could not flush endpoint stat %s: %s", key, exc)


#: Rows untouched for this long are deleted. Storage limitation, and it also
#: stops one-off curiosities accumulating for ever.
RETENTION_DAYS = 365


def prune(days: int = RETENTION_DAYS, dry_run: bool = False) -> dict:
    """Delete counter rows nothing has touched in `days`.

    By `last_seen`, not `first_seen`: a query that people still ask is current
    however long ago it was first asked, and deleting it would throw away the
    trend that makes the whole store worth keeping.
    """
    from datetime import timedelta

    from app.db.arcadedb import run_sql

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff = cutoff_dt.isoformat()
    out: dict[str, int] = {}
    for vtype in ("SearchDemand", "UsageCounter", "EndpointStat"):
        rows = run_sql(f"SELECT count(*) AS n FROM {vtype} WHERE last_seen < :cut",
                       {"cut": cutoff})
        n = int((rows[0].get("n") if rows else 0) or 0)
        out[vtype] = n
        if n and not dry_run:
            run_sql(f"DELETE FROM {vtype} WHERE last_seen < :cut", {"cut": cutoff})
    # The weekly rows carry no last_seen — the week is their date. ISO week ids
    # sort as strings within a year and across years ("2025-W52" < "2026-W01").
    cutoff_week = iso_week(cutoff_dt)
    for vtype in ("SearchWeek", "UsageWeek"):
        rows = run_sql(f"SELECT count(*) AS n FROM {vtype} WHERE week < :w", {"w": cutoff_week})
        n = int((rows[0].get("n") if rows else 0) or 0)
        out[vtype] = n
        if n and not dry_run:
            run_sql(f"DELETE FROM {vtype} WHERE week < :w", {"w": cutoff_week})
    out["cutoff"] = cutoff
    return out
