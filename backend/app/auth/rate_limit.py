"""DB-backed sliding-window rate limiter with progressive lockout.

Replaces the previous in-memory ``defaultdict[str, list[float]]`` which reset
on every Render deploy (triggered by every git push to main), giving an
attacker a fresh window each time.  Attempt timestamps are now stored in
ArcadeDB's ``RateLimit`` vertex type so the window survives restarts and
rolling deploys.

Progressive lockout
-------------------
A plain sliding window lets a patient attacker try the limit every window
period indefinitely.  When the window is exhausted a ``lockout_until``
timestamp is set that extends exponentially on each repeat violation:

  1st exhaustion — 15 min  (same as the sliding window; low friction for mistakes)
  2nd exhaustion — 1 hour
  3rd exhaustion — 4 hours
  4th+ exhaustion — 24 hours

``lockout_count`` resets to zero on a successful auth (``clear_attempts``),
so a legitimate user who finally gets their password right starts fresh.

Fail-open policy
----------------
Every DB operation is wrapped in a broad ``except``.  If ArcadeDB is
unreachable the rate limiter logs a warning and lets the call through rather
than locking every user out during a DB outage.  The auth endpoints themselves
will fail against the same unreachable DB anyway, so the attacker gains
nothing from the brief open window.

Key namespace
-------------
Callers must choose a key prefix to avoid collisions between independent
rate-limit buckets:

  - ``login:<ip>:<email>``  — per-IP+account login failures
  - ``email:<email>``       — outbound email sends (verify / reset)
  - ``mfa:<user_id>``       — TOTP / recovery-code verify attempts
"""
from __future__ import annotations

import logging
import time

from fastapi import HTTPException

from app.db.arcadedb import run_sql

log = logging.getLogger(__name__)

# Lockout durations indexed by lockout_count (capped at the last value).
# The first entry matches the sliding window so the very first lockout is
# a natural extension rather than a sudden jump.
_LOCKOUT_DURATIONS: tuple[int, ...] = (
    15 * 60,       # 1st exhaustion — 15 min
    60 * 60,       # 2nd exhaustion — 1 hour
    4 * 60 * 60,   # 3rd exhaustion — 4 hours
    24 * 60 * 60,  # 4th+ exhaustion — 24 hours
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_EMPTY_RECORD: dict = {"timestamps": [], "lockout_until": 0.0, "lockout_count": 0}


def _load_record(key: str, window: int) -> dict:
    """Return the full rate-limit record for *key*, pruning stale timestamps.

    Returns a dict with keys ``timestamps`` (list[float]), ``lockout_until``
    (float epoch), and ``lockout_count`` (int).  Falls back to an empty record
    on any DB error so callers always get a usable dict.
    """
    cutoff = time.time() - window
    try:
        rows = run_sql(
            "SELECT timestamps, lockout_until, lockout_count FROM RateLimit WHERE key = :k",
            {"k": key},
        )
        if not rows:
            return dict(_EMPTY_RECORD)
        row = rows[0]
        raw_ts = row.get("timestamps") or []
        return {
            "timestamps":   [float(t) for t in raw_ts if float(t) > cutoff],
            "lockout_until": float(row.get("lockout_until") or 0),
            "lockout_count": int(row.get("lockout_count") or 0),
        }
    except Exception as exc:  # noqa: BLE001 — fail open
        log.warning("rate_limit load error (key=%s): %s", key, exc)
        return dict(_EMPTY_RECORD)


def _save_record(key: str, timestamps: list[float],
                 lockout_until: float = 0.0, lockout_count: int = 0) -> None:
    """Upsert the full rate-limit record for *key*."""
    try:
        run_sql(
            "UPDATE RateLimit SET key = :k, timestamps = :ts, "
            "lockout_until = :lu, lockout_count = :lc UPSERT WHERE key = :k",
            {"k": key, "ts": timestamps, "lu": lockout_until, "lc": lockout_count},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("rate_limit save error (key=%s): %s", key, exc)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def check_rate_limit(key: str, limit: int, window: int) -> None:
    """Raise HTTP 429 if *key* is rate-limited.

    Two-stage check:

    1. **Hard lockout** — if a previous window exhaustion set ``lockout_until``
       in the future, reject immediately (no sliding-window check, no extra DB
       write).

    2. **Sliding window** — if the number of recorded attempts within *window*
       seconds reaches *limit*, apply a progressive lockout (duration doubles
       on each exhaustion up to 24 h) and raise 429.
    """
    now = time.time()
    record = _load_record(key, window)

    # Stage 1: hard lockout set by a prior window exhaustion.
    if record["lockout_until"] > now:
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")

    # Stage 2: sliding window.
    if len(record["timestamps"]) >= limit:
        count = record["lockout_count"]
        duration = _LOCKOUT_DURATIONS[min(count, len(_LOCKOUT_DURATIONS) - 1)]
        _save_record(key, record["timestamps"],
                     lockout_until=now + duration,
                     lockout_count=count + 1)
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")


def record_attempt(key: str, window: int) -> None:
    """Append the current timestamp to *key*'s sliding window.

    Preserves any existing ``lockout_until`` / ``lockout_count`` values so that
    recording an attempt never accidentally clears a lockout.
    """
    record = _load_record(key, window)
    record["timestamps"].append(time.time())
    _save_record(key, record["timestamps"],
                 lockout_until=record["lockout_until"],
                 lockout_count=record["lockout_count"])


def clear_attempts(key: str) -> None:
    """Remove all attempt records for *key* (call on successful auth).

    Deleting the record resets both the sliding window and the lockout
    escalation counter — a legitimate user who authenticates successfully
    starts completely fresh.
    """
    try:
        run_sql("DELETE FROM RateLimit WHERE key = :k", {"k": key})
    except Exception as exc:  # noqa: BLE001
        log.warning("rate_limit clear error (key=%s): %s", key, exc)
