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


def test_public_reader_gets_the_run_but_not_the_reason(it_db):
    """The /scraper/runs redaction, over a real read rather than a stubbed one.

    The unit tests stub `list_runs`, so they cannot catch the redaction being
    applied to a shape the database doesn't actually return.
    """
    from app.scraper import run_log
    from app.scraper.router import scraper_runs

    with pytest.raises(RuntimeError):
        with run_log.record_run("sec_edgar", "Broken Ltd"):
            raise RuntimeError("credential=hunter2 leaked in here")

    anon = scraper_runs(limit=50, user=None)["runs"][0]
    assert anon["target"] == "Broken Ltd"
    assert anon["status"] == "failed"        # the failure still shows
    assert "error" not in anon
    assert "hunter2" not in str(anon)

    priv = scraper_runs(limit=50, user={"role": "contributor"})["runs"][0]
    assert "hunter2" in priv["error"]


def _raw(it_db, target):
    return it_db.run_sql("SELECT FROM ScrapeRun WHERE target = :t", {"t": target})[0]


def test_what_is_not_known_is_absent_not_an_empty_string(it_db):
    """No `''` placeholders (2026-10-25): a clean run has no `error`, a run
    that resolved no company has no `entity_id`, and no reason is no key."""
    from app.scraper import run_log

    with run_log.record_run("wikidata", "Quiet Co") as run:
        run["total"] = 1
    with run_log.record_run("sec_edgar", "Named Co", reason="forced") as run:
        run["entity_id"] = "lei:1"
    with pytest.raises(RuntimeError):
        with run_log.record_run("sec_edgar", "Broken Co"):
            raise RuntimeError("boom")

    quiet, named, broken = _raw(it_db, "Quiet Co"), _raw(it_db, "Named Co"), _raw(it_db, "Broken Co")
    for key in ("error", "entity_id", "reason"):
        assert quiet.get(key) is None and key not in quiet
    assert quiet["finished_at"] and quiet["status"] == "ok"
    assert named["entity_id"] == "lei:1" and named["reason"] == "forced" and "error" not in named
    assert broken["error"] == "boom" and "entity_id" not in broken
    listed = {r["target"]: r for r in run_log.list_runs()}
    assert listed["Quiet Co"]["entity_id"] is None and listed["Quiet Co"]["error"] == ""


def test_the_heal_drops_the_old_placeholders(it_db):
    from app.scraper.run_log import heal_placeholders

    it_db.run_command(
        "CREATE (:ScrapeRun {id:'old-1', source:'sec-ex21', target:'Old Co', status:'ok', "
        "started_at:'2026-10-01T00:00:00+00:00', finished_at:'2026-10-01T00:01:00+00:00', "
        "total:0, error:'', reason:'', entity_id:''})")
    it_db.run_command(
        "CREATE (:ScrapeRun {id:'old-2', source:'all', target:'Named Co', status:'failed', "
        "started_at:'2026-10-01T00:00:00+00:00', finished_at:'', total:0, error:'boom', entity_id:'lei:1'})")

    dry = heal_placeholders(apply=False)
    assert (dry["entity_id"], dry["finished_at"], dry["error"], dry["reason"]) == (1, 1, 1, 1)
    assert _raw(it_db, "Old Co")["error"] == ""                     # dry run wrote nothing

    res = heal_placeholders(apply=True)
    assert (res["entity_id"], res["finished_at"], res["error"], res["reason"]) == (1, 1, 1, 1)
    old, named = _raw(it_db, "Old Co"), _raw(it_db, "Named Co")
    for key in ("error", "entity_id", "reason"):
        assert key not in old
    assert old["finished_at"]                                        # a real value stays
    assert named["error"] == "boom" and named["entity_id"] == "lei:1" and "finished_at" not in named
    assert heal_placeholders(apply=True)["entity_id"] == 0           # idempotent
