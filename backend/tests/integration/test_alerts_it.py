"""`manage.py alerts` over a real run log: the imports check reads ScrapeRun
through list_runs, so a failed delta and a crashed (stale) run must surface."""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration


def test_failed_and_stale_imports_surface_and_healthy_ones_do_not(it_db, monkeypatch):
    from app.alerts import collect
    from app.config import settings
    now = datetime.now(timezone.utc)
    rows = [("a1", "gleif-update", "failed", now - timedelta(hours=2), "delta file 404"),
            ("a2", "gleif-update", "ok", now - timedelta(hours=26), ""),
            ("a3", "ch-psc-update", "running", now - timedelta(hours=3), ""),     # stale
            ("a4", "wikidata", "failed", now - timedelta(hours=1), "a scrape, not an import")]
    for rid, src, st, at, err in rows:
        run_sql("CREATE VERTEX ScrapeRun SET id = :id, source = :s, target = 't', status = :st, "
                "started_at = :at, finished_at = '', total = 0, error = :e",
                {"id": rid, "s": src, "st": st, "at": at.isoformat(), "e": err})
    monkeypatch.setattr(settings, "ALERT_HEALTH_URL", "")
    monkeypatch.setattr(settings, "ALERT_BACKUP_MARKER", "")
    monkeypatch.setattr(settings, "ALERT_DISK_PATHS", "")
    alerts = collect(now=now)
    assert sorted((a.check, a.level) for a in alerts) == [("imports", "crit"), ("imports", "warn")]
    assert any("delta file 404" in a.message for a in alerts)
    assert any("ch-psc-update" in a.message and "crashed" in a.message for a in alerts)
