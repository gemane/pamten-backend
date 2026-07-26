"""
Public data-scale stats for the landing page: how many companies, people,
ownership relationships, and sources the platform holds.

Counts come from ArcadeDB's `schema:types` metadata (the `records` field), which
is O(1) — a `count(*)` over the ~14M-row Entity type would be slow, the metadata
read is instant. Cached briefly (counts only move during imports) and best-effort
so the landing page never breaks on a DB hiccup.
"""
import logging
import time

from fastapi import APIRouter

from app.db.arcadedb import run_sql

log = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["Stats"])

# type name -> response key
_TYPE_MAP = {"Entity": "companies", "Person": "people",
             "OWNS": "relationships", "Source": "sources"}

_CACHE_TTL = 60.0  # seconds
_cache: tuple[float, dict] | None = None


def _compute() -> dict:
    counts = dict.fromkeys(_TYPE_MAP.values(), 0)
    rows = run_sql("SELECT name, records FROM schema:types")
    for r in rows:
        key = _TYPE_MAP.get(r.get("name"))
        if key:
            counts[key] = int(r.get("records") or 0)
    return counts


@router.get("")
def get_stats() -> dict:
    """Landing-page counts: {companies, people, relationships, sources}. Public."""
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    try:
        counts = _compute()
        _cache = (now, counts)
        return counts
    except Exception as exc:  # noqa: BLE001 - never 500 the landing page
        log.warning("stats query failed: %s", exc)
        # Serve a stale value if we have one, else zeros.
        return _cache[1] if _cache else dict.fromkeys(_TYPE_MAP.values(), 0)
