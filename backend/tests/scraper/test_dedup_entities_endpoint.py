"""
Unit tests for the /scraper/deduplicate-entities dispatch (background vs sync).

The heal logic itself is covered by tests/integration/test_bods_entity_dedup_it.py
against a real ArcadeDB; here we only check the endpoint's threading/guard
behaviour, with the DB and thread mocked.
"""
from unittest.mock import patch

import pytest

from app.scraper import router as scraper_router

ADMIN = {"role": "admin"}


@pytest.fixture(autouse=True)
def _reset_flag():
    scraper_router._dedup_running = False
    yield
    scraper_router._dedup_running = False


def test_background_starts_a_thread_and_returns_started():
    with patch("app.scraper.router.threading.Thread") as Thread:
        res = scraper_router.deduplicate_entities(background=True, limit=300, _=ADMIN)
    assert res["status"] == "started"
    Thread.assert_called_once()
    Thread.return_value.start.assert_called_once()
    assert scraper_router._dedup_running is True    # guard armed until the job clears it


def test_background_refuses_a_second_concurrent_job():
    scraper_router._dedup_running = True             # a job is already running
    with patch("app.scraper.router.threading.Thread") as Thread:
        res = scraper_router.deduplicate_entities(background=True, limit=300, _=ADMIN)
    assert res["status"] == "already_running"
    Thread.assert_not_called()                       # no second scan launched


def test_sync_mode_runs_inline_with_limit():
    with patch.object(scraper_router.maintenance, "deduplicate_entities",
                      return_value={"entities_merged": 2, "remaining": 0}) as dedup:
        res = scraper_router.deduplicate_entities(background=False, limit=50, _=ADMIN)
    dedup.assert_called_once_with(limit=50)
    assert res["entities_merged"] == 2


def test_job_records_run_and_clears_the_guard():
    scraper_router._dedup_running = True
    fake_cm = patch("app.scraper.router.record_run").start()
    fake_cm.return_value.__enter__.return_value = {"total": 0}
    with patch.object(scraper_router.maintenance, "deduplicate_entities_bulk",
                      return_value={"entities_removed": 7}):
        scraper_router._dedup_entities_job("bulk")
    patch.stopall()
    assert scraper_router._dedup_running is False    # guard released after the job


def test_bulk_heal_aggregates_then_batches_deletes():
    """deduplicate_entities_bulk: grouped scan per id kind, then batched
    DELETE VERTEX keeping the min-id survivor."""
    from app.scraper import maintenance

    # lei_id pass finds one dup group (keep e1, drop the other); coh pass none.
    def fake_run_sql(q, params=None):
        return [{"k": "LEI-A", "c": 2, "keep": "e1"}] if "lei_id" in q else []

    scripts = []
    with patch.object(maintenance, "run_sql", side_effect=fake_run_sql), \
         patch.object(maintenance, "run_sqlscript",
                      side_effect=lambda s, p=None: scripts.append((s, p))):
        res = maintenance.deduplicate_entities_bulk()

    assert res["entities_removed"] == 1                      # c-1 per group
    assert res["by"]["lei_id"]["groups"] == 1
    assert "DELETE VERTEX FROM Entity" in scripts[0][0]      # a delete was issued
    assert scripts[0][1] == {"k__0": "LEI-A", "keep__0": "e1"}


def test_bulk_heal_subshards_when_group_cap_is_hit():
    """A shard that trips ArcadeDB's in-heap group cap is split one level deeper."""
    from app.scraper import maintenance

    cap_error = RuntimeError(
        "ArcadeDB command failed [500]: Limit of allowed groups for in-heap "
        "GROUP BY in a single query exceeded (500000). ... queryMaxHeapElementsAllowedPerOp")

    calls = {"n": 0}

    def fake_run_sql(q, params=None):
        # Only the lei_id root shard (prefix "") trips the cap; sub-shards succeed.
        if "lei_id" not in q:
            return []
        calls["n"] += 1
        if params["lo"] == "":            # root shard → over the cap
            raise cap_error
        if params["lo"] == "5":           # one sub-shard has a duplicate
            return [{"k": "5LEI", "c": 2, "keep": "a"}]
        return []

    with patch.object(maintenance, "run_sql", side_effect=fake_run_sql), \
         patch.object(maintenance, "run_sqlscript", side_effect=lambda s, p=None: None):
        res = maintenance.deduplicate_entities_bulk()

    assert res["by"]["lei_id"]["groups"] == 1          # found via a sub-shard
    assert calls["n"] > len(maintenance._SHARD_CHARSET)  # root failed, then 36 sub-shards scanned
