"""
Cross-process import lock — at most one bulk/delta import at a time.

DB-backed (an ``ImportState {key:'import-lock'}`` record) so it holds no matter which
host or process starts an import — unlike a host-local flock, which only works if
everything runs on the same box. Acquisition is atomic via the UNIQUE ``key`` index:
the winning INSERT holds the lock; a duplicate-key error means someone else has it.

The one trade-off vs flock: a DB lock does **not** auto-release when a process is
killed. So a lock older than ``STALE_AFTER_SEC`` (longer than any real import) is
treated as abandoned and stolen, and ``manage.py import-lock {status,release}`` is
the manual escape hatch.

Concurrent imports we prevent: the daily ``gleif-update`` cron firing mid
``full-import.sh`` (concurrent writes on top of a ``--bulk-load`` corrupt/partial the
load). ``full-import.sh`` holds the lock across its whole chain and sets
``IMPORT_ORCHESTRATED``, which makes the per-runner lock a no-op so the chain's own
steps don't fight each other.
"""
import functools
import logging
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone

from app.db.arcadedb import run_command, run_sql

log = logging.getLogger(__name__)

_KEY = "import-lock"
STALE_AFTER_SEC = 6 * 3600   # longer than any real import → an older lock is abandoned


class ImportLocked(RuntimeError):
    """Raised when another import already holds the lock."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _held() -> dict | None:
    rows = run_command(
        "MATCH (s:ImportState {key:$k}) RETURN s.holder AS holder, s.acquired_at AS at",
        {"k": _KEY})
    if rows and rows[0].get("holder"):
        return {"holder": rows[0]["holder"], "acquired_at": rows[0].get("at")}
    return None


def _age_seconds(iso: str | None) -> float:
    if not iso:
        return 1e12
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return 1e12


def acquire(holder: str) -> None:
    """Acquire the lock or raise ImportLocked. A stale (abandoned) lock is stolen."""
    cur = _held()
    if cur and _age_seconds(cur["acquired_at"]) < STALE_AFTER_SEC:
        raise ImportLocked(f"another import holds the lock: {cur['holder']} since {cur['acquired_at']}")
    if cur:   # stale → steal it
        log.warning("stealing stale import lock (%s since %s)", cur["holder"], cur["acquired_at"])
        run_sql("DELETE FROM ImportState WHERE key = :k", {"k": _KEY})
    tag = f"{holder}@{socket.gethostname()}:{os.getpid()}"
    try:
        # Atomic: the UNIQUE key index makes exactly one concurrent INSERT win.
        run_sql("INSERT INTO ImportState SET key = :k, holder = :h, acquired_at = :t",
                {"k": _KEY, "h": tag, "t": _now_iso()})
    except RuntimeError as exc:
        raise ImportLocked(f"another import acquired the lock first ({str(exc)[:120]})") from exc
    log.info("acquired import lock as %s", tag)


def release() -> None:
    run_sql("DELETE FROM ImportState WHERE key = :k", {"k": _KEY})


def status() -> dict:
    cur = _held()
    if not cur:
        return {"held": False}
    age = _age_seconds(cur["acquired_at"])
    return {"held": True, **cur, "age_seconds": int(age), "stale": age >= STALE_AFTER_SEC}


@contextmanager
def import_lock(holder: str):
    """Hold the import lock for the block. A no-op when IMPORT_ORCHESTRATED is set —
    full-import.sh already holds the lock across the whole chain, so the chain's own
    steps must not re-acquire (they'd deadlock against their orchestrator)."""
    if os.getenv("IMPORT_ORCHESTRATED"):
        yield
        return
    acquire(holder)
    try:
        yield
    finally:
        release()


def guarded(holder: str):
    """Decorator form of `import_lock` for an import runner."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with import_lock(holder):
                return fn(*args, **kwargs)
        return wrapper
    return deco
