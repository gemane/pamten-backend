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
    with patch.object(scraper_router.maintenance, "deduplicate_entities",
                      return_value={"entities_merged": 7}):
        scraper_router._dedup_entities_job()
    patch.stopall()
    assert scraper_router._dedup_running is False    # guard released after the job
