"""
Real-ArcadeDB integration test for the scrape-run log: records success/failure,
flags stale runs, and stays bounded by MAX_RUNS.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_records_success_and_failure(it_db):
    from app.scraper import run_log

    with run_log.record_run("wikidata", "Acme Corp") as run:
        run["total"] = 42
    with pytest.raises(RuntimeError):
        with run_log.record_run("sec_edgar", "Broken Ltd"):
            raise RuntimeError("boom")

    runs = {r["target"]: r for r in run_log.list_runs()}
    assert runs["Acme Corp"]["status"] == "ok"
    assert runs["Acme Corp"]["total"] == 42
    assert runs["Acme Corp"]["finished_at"]
    assert runs["Broken Ltd"]["status"] == "failed"
    assert "boom" in runs["Broken Ltd"]["error"]


def test_prune_keeps_only_max_runs(it_db, monkeypatch):
    from app.scraper import run_log
    monkeypatch.setattr(run_log, "MAX_RUNS", 3)

    for i in range(6):
        with run_log.record_run("wikidata", f"co-{i}") as run:
            run["total"] = i

    runs = run_log.list_runs(100)
    assert len(runs) == 3                                  # capped
    assert [r["target"] for r in runs] == ["co-5", "co-4", "co-3"]   # newest kept, newest first


def test_stale_running_flagged(it_db):
    from app.scraper import run_log
    # a running row with an old start time is flagged stale
    it_db.run_command(
        "CREATE (:ScrapeRun {id:'old', source:'all', target:'Stuck Co', status:'running', "
        "started_at:'2020-01-01T00:00:00+00:00', finished_at:'', total:0, error:''})")
    stuck = next(r for r in run_log.list_runs(100) if r["id"] == "old")
    assert stuck["stale"] is True
