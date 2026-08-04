"""DB-backed sliding-window rate limiter.

Replaces the previous in-memory ``defaultdict[str, list[float]]`` which reset
on every Render deploy (triggered by every git push to main), giving an
attacker a fresh window each time.  Attempt timestamps are now stored in
ArcadeDB's ``RateLimit`` vertex type so the window survives restarts and
rolling deploys.

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_timestamps(key: str, window: int) -> list[float]:
    """Return the still-valid attempt timestamps for *key*.

    Prunes entries older than *window* seconds on the way out so the stored
    list never grows unboundedly.
    """
    cutoff = time.time() - window
    try:
        rows = run_sql(
            "SELECT timestamps FROM RateLimit WHERE key = :k",
            {"k": key},
        )
        if not rows:
            return []
        raw = rows[0].get("timestamps") or []
        return [float(t) for t in raw if float(t) > cutoff]
    except Exception as exc:  # noqa: BLE001 — fail open
        log.warning("rate_limit load error (key=%s): %s", key, exc)
        return []


def _save_timestamps(key: str, ts: list[float]) -> None:
    try:
        # UPDATE … UPSERT inserts a new record when no rows match, updates
        # when one matches.  key must appear in both SET and WHERE so it is
        # included in the inserted record.
        run_sql(
            "UPDATE RateLimit SET key = :k, timestamps = :ts UPSERT WHERE key = :k",
            {"k": key, "ts": ts},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("rate_limit save error (key=%s): %s", key, exc)


# ---------------------------------------------------------------------------
# Public interface (matches the old _check/_record/_clear contract)
# ---------------------------------------------------------------------------

def check_rate_limit(key: str, limit: int, window: int) -> None:
    """Raise HTTP 429 if *key* has >= *limit* attempts within *window* seconds."""
    if len(_load_timestamps(key, window)) >= limit:
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")


def record_attempt(key: str, window: int) -> None:
    """Append the current timestamp to *key*'s sliding window."""
    ts = _load_timestamps(key, window)
    ts.append(time.time())
    _save_timestamps(key, ts)


def clear_attempts(key: str) -> None:
    """Remove all attempt records for *key* (call on successful auth)."""
    try:
        run_sql("DELETE FROM RateLimit WHERE key = :k", {"k": key})
    except Exception as exc:  # noqa: BLE001
        log.warning("rate_limit clear error (key=%s): %s", key, exc)
