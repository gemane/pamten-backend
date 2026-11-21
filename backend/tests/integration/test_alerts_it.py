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


def test_a_source_that_refused_requests_surfaces_in_a_plain_run(it_db, monkeypatch):
    """The refused check reads the SourceCall rows of yesterday and today; a
    day of 404s, an older day and the bucket's own counts say nothing (findings
    item 137)."""
    from app.alerts import collect
    from app.config import settings
    now = datetime.now(timezone.utc)
    today, yesterday, older = (str((now - timedelta(days=d)).date()) for d in (0, 1, 2))
    rows = [(today, "sec-edgar", "refused", 30), (yesterday, "sec-edgar", "refused", 25),   # 55: critical
            (today, "sec-edgar", "4xx", 600), (today, "sec-edgar", "ok", 9000),
            (yesterday, "wikidata", "refused", 6),                                         # a warning
            (older, "gleif", "refused", 500),                                              # too old
            (today, "gleif", "refused", 4),                                                # below the line
            (today, "bucket:edgar", "refused", 99)]                                        # not a source
    for day, source, kind, n in rows:
        run_sql("INSERT INTO SourceCall SET key = :k, day = :d, source = :s, kind = :kind, count = :n",
                {"k": f"{day}|{source}|{kind}", "d": day, "s": source, "kind": kind, "n": n})
    monkeypatch.setattr(settings, "ALERT_HEALTH_URL", "")
    monkeypatch.setattr(settings, "ALERT_BACKUP_MARKER", "")
    monkeypatch.setattr(settings, "ALERT_DISK_PATHS", "")
    alerts = collect(now=now)
    assert sorted((a.check, a.level, a.message.split()[0]) for a in alerts) == [
        ("refused", "crit", "sec-edgar"), ("refused", "warn", "wikidata")]
    [edgar] = [a for a in alerts if a.message.startswith("sec-edgar")]
    assert "refused 55 requests" in edgar.message and "9,000 answered" in edgar.message

