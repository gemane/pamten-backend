"""Per-source health, aggregated from what the system already records.

The OpenCorporates lesson: a data product's trustworthiness is the visibility
of its plumbing. "Is a source alive, when did it last succeed, is it failing
repeatedly, how old is the bulk data" was all answerable — but only by reading
the raw run feed and knowing which ImportState keys hold which checkpoint.
This module answers it in one shape, computed in Python over the run log's
≤500 retained rows (the cap makes a full read cheap by construction).

Everything here is a REUSE of existing records: `list_runs` (which already
derives the stale flag), the source catalogue, and the GLEIF / Companies House
import checkpoints. Nothing new is written anywhere.
"""
from datetime import datetime, timedelta, timezone

from app.scraper.run_log import list_runs
from app.scraper.sources import KNOWN_SOURCES

# Run-log source names that are not catalogue sources: pipelines and passes
# whose runs are still health information. The catalogue's own names label
# themselves.
_RUNLOG_LABELS = {
    "all": "All sources (run-all)",
    "sec-13f": "SEC 13F holders",
    "gleif-update": "GLEIF daily delta",
    "ch-psc-update": "UK PSC refresh",
    "deduplicate-entities": "Entity dedup",
}


def _source_toggles() -> dict:
    """DB toggle per catalogue source, in ONE read. The per-name
    `get_source_enabled` runs `_ensure_sources()` every call - five MERGEs
    plus a point read per source, ~30 round trips per poll once a panel
    refreshes every 15 seconds. Health only reads; absent rows read as
    disabled, same as the per-name helper."""
    from app.database import db
    from app.scraper.sources import _ensure_sources

    def read() -> dict:
        with db.get_session() as session:
            rows = session.run("MATCH (s:ScraperSource) RETURN s.name AS name, "
                               "s.enabled AS enabled")
            return {r["name"]: bool(r["enabled"]) for r in rows}

    toggles = read()
    if not toggles:
        # A fresh database has no toggle nodes yet — bootstrap once and
        # re-read. Every later request stays a single query.
        _ensure_sources()
        toggles = read()
    return toggles


def _streak(runs: list[dict]) -> int:
    """Consecutive failures, newest first, stopped by the first success.

    `running`/`skipped` rows neither break nor extend the streak: an
    in-flight attempt is not evidence either way, and a skip never touched
    the source.
    """
    n = 0
    for r in runs:
        if r["status"] == "failed":
            n += 1
        elif r["status"] == "ok":
            break
    return n


def _source_entry(name: str, runs: list[dict], now: datetime,
                  toggles: dict) -> dict:
    meta = KNOWN_SOURCES.get(name)
    entry: dict = {
        "name": name,
        "label": (meta or {}).get("label") or _RUNLOG_LABELS.get(name, name),
        "kind": (meta or {}).get("kind"),
        "quality": (meta or {}).get("quality"),
        # Only catalogue sources have a toggle; pipelines report null rather
        # than pretending an enabled state they do not have.
        "enabled": toggles.get(name, False) if meta else None,
        "last_run_at": None, "last_status": None, "last_total": None,
        "last_ok_at": None, "failure_streak": 0, "runs_24h": 0,
    }
    if not runs:
        return entry   # never ran — that absence IS the health information
    newest = runs[0]
    entry["last_run_at"] = newest["started_at"]
    # The activity feed derives running+old → "stale" client-side; computed
    # here once so every consumer agrees on the third state.
    entry["last_status"] = "stale" if newest.get("stale") else newest["status"]
    entry["last_total"] = newest["total"]
    entry["last_error"] = newest.get("error") or ""
    entry["last_ok_at"] = next(
        (r["started_at"] for r in runs if r["status"] == "ok"), None)
    entry["failure_streak"] = _streak(runs)
    cutoff = (now - timedelta(hours=24)).isoformat()
    entry["runs_24h"] = sum(1 for r in runs if (r["started_at"] or "") >= cutoff)
    return entry


def _datasets() -> list[dict]:
    """Bulk-data freshness from the import checkpoints."""
    from app.scraper.ch_psc_incremental import (days_since, psc_load_scope,
                                                read_last_snapshot)
    from app.scraper.gleif_incremental import load_scope, read_last_publish

    out = []
    last_publish = read_last_publish()
    out.append({
        "name": "bods_gleif",
        "label": KNOWN_SOURCES["bods_gleif"]["label"],
        "scope": load_scope(),
        "last_publish_date": last_publish,
        "behind_days": days_since(last_publish[:10]) if last_publish else None,
    })
    snap = read_last_snapshot() or {}
    snapshot_date = snap.get("snapshot_date")
    out.append({
        "name": "bods_uk_psc",
        "label": KNOWN_SOURCES["bods_uk_psc"]["label"],
        "scope": psc_load_scope(),
        "loaded_at": snap.get("last_run_at"),
        "snapshot_date": snapshot_date,
        "record_count": snap.get("record_count"),
        "behind_days": days_since(snapshot_date) if snapshot_date else None,
    })
    return out


def source_health() -> dict:
    """The whole health picture. Unredacted — the ROUTER decides who may see
    error strings and the lock holder, exactly as /scraper/runs does."""
    from app.db.import_lock import status as lock_status

    now = datetime.now(timezone.utc)
    runs = list_runs(limit=500)
    by_source: dict[str, list[dict]] = {}
    for r in runs:
        by_source.setdefault(r["source"] or "?", []).append(r)

    # Catalogue sources always appear, run or not; run-log-only pipelines
    # appear when they have runs. Catalogue order first, then by recency.
    toggles = _source_toggles()
    names = list(KNOWN_SOURCES) + [n for n in by_source if n not in KNOWN_SOURCES]
    sources = [_source_entry(n, by_source.get(n, []), now, toggles) for n in names]

    return {"sources": sources, "datasets": _datasets(),
            "import_lock": lock_status()}
