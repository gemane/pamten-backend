"""
Scrape-run log — a small, bounded record of scrape activity so the UI (and other
sessions) can see what's running and which runs failed.

Each scrape writes a `running` ScrapeRun row on start and updates it to `ok` or
`failed` (with node count / error) on finish. The log is capped: on every finish
the oldest rows beyond MAX_RUNS are pruned, so it can never grow the DB
unbounded — a few hundred tiny vertices at most.
"""
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from app.database import db

log = logging.getLogger(__name__)

MAX_RUNS = 5000             # keep at most this many run records — a week of on-demand
                            # traffic must fit, since the weekly digest reads them
STALE_AFTER_SEC = 1800     # a 'running' row older than this is almost certainly a crashed run


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def record_run(source: str, target: str, reason: str | None = None):
    """
    Record a scrape run for the duration of the block. Writes a `running` row on
    entry and, on exit, marks it `ok` (with the node count set via the yielded
    dict's `total`) or `failed` (with the exception message). Logging never
    interferes with the scrape — a failure to write the log is swallowed.

    The block may set `status` (and `note`) on the yielded dict to finish as
    something other than `ok` — `skipped` for a run that correctly did nothing, so
    a nightly cron that has no work to do doesn't read as a nightly failure.

    `reason` is why the run happened (the on-demand decision: absent, stale,
    forced…) and the block may set `entity_id` to the company it resolved —
    together they let the weekly digest tell a first scrape from a refresh and
    name the company, which a target string cannot.
    """
    run_id = str(uuid.uuid4())
    out: dict = {"total": 0}
    _safe_create(run_id, source, target, reason)
    try:
        yield out
    except Exception as exc:
        _safe_finish(run_id, "failed", 0, str(exc)[:500], out.get("entity_id"))
        raise
    else:
        _safe_finish(run_id, str(out.get("status") or "ok"),
                     int(out.get("total") or 0), str(out.get("note") or ""),
                     out.get("entity_id"))


def _safe_create(run_id: str, source: str, target: str, reason: str | None = None) -> None:
    try:
        with db.get_session() as s:
            s.run(
                "CREATE (r:ScrapeRun {id:$id, source:$src, target:$tgt, status:'running', "
                "started_at:$at, finished_at:'', total:0, error:'', reason:$why, entity_id:''})",
                id=run_id, src=source, tgt=(target or "")[:200], at=_now_iso(),
                why=(reason or "")[:40])
    except Exception as exc:  # noqa: BLE001 - logging must never break a scrape
        log.warning("scrape-run start log failed: %s", exc)


def _safe_finish(run_id: str, status: str, total: int, error: str,
                 entity_id: str | None = None) -> None:
    try:
        with db.get_session() as s:
            s.run(
                "MATCH (r:ScrapeRun {id:$id}) SET r.status=$st, r.finished_at=$at, "
                "r.total=$tot, r.error=$err, r.entity_id=$eid",
                id=run_id, st=status, at=_now_iso(), tot=total, err=error,
                eid=str(entity_id or ""))
        _prune()
    except Exception as exc:  # noqa: BLE001
        log.warning("scrape-run finish log failed: %s", exc)


def _prune() -> None:
    with db.get_session() as s:
        rows = sorted(
            ((r.get("at") or "", r.get("id")) for r in s.run(
                "MATCH (r:ScrapeRun) RETURN r.id AS id, r.started_at AS at")),
            reverse=True)                       # newest first
        for _, rid in rows[MAX_RUNS:]:
            s.run("MATCH (r:ScrapeRun {id:$id}) DETACH DELETE r", id=rid)


def list_runs(limit: int = 50) -> list[dict]:
    """Recent scrape runs, newest first; a long-stuck `running` row is flagged stale."""
    now = datetime.now(timezone.utc)
    with db.get_session() as s:
        runs = [
            {"id": r.get("id"), "source": r.get("source"), "target": r.get("target"),
             "status": r.get("status"), "started_at": r.get("started_at"),
             "finished_at": r.get("finished_at") or None, "total": r.get("total") or 0,
             "error": r.get("error") or "", "reason": r.get("reason") or None,
             "entity_id": r.get("entity_id") or None}
            for r in s.run(
                "MATCH (r:ScrapeRun) RETURN r.id AS id, r.source AS source, r.target AS target, "
                "r.status AS status, r.started_at AS started_at, r.finished_at AS finished_at, "
                "r.total AS total, r.error AS error, r.reason AS reason, r.entity_id AS entity_id")
        ]
    for run in runs:
        run["stale"] = False
        if run["status"] == "running" and run["started_at"]:
            try:
                started = datetime.fromisoformat(run["started_at"])
                if (now - started).total_seconds() > STALE_AFTER_SEC:
                    run["stale"] = True
            except ValueError:
                pass
    runs.sort(key=lambda x: x["started_at"] or "", reverse=True)
    return runs[:limit]
