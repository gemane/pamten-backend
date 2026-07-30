"""
Real-ArcadeDB test that the scoped auto-dedup is wired to EVERY scrape entry point
(not just run-all): the `_with_autodedup` decorator runs the person + entity dedup
after a standalone single-source scrape, and — because run_scrape_all calls the
single-source runners — a nested scrape defers to the outer scope so the merge runs
exactly once.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_single_source_scrape_dedups_and_nesting_runs_once(it_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", True)

    from app.scraper.runner import (
        _upsert_entity_by_name,
        _upsert_person_by_name,
        _with_autodedup,
    )

    # A standalone single-source scrape (like /scraper/run or /scraper/sec-edgar/run)
    # now collects what it touched and self-cleans.
    @_with_autodedup
    def single_source_scrape():
        _upsert_person_by_name("Some Person", "src")
        _upsert_entity_by_name("Some Company", source_id="src")
        return {"status": "ok"}

    res = single_source_scrape()
    assert "deduplication" in res            # person dedup ran
    assert "entity_deduplication" in res     # entity dedup ran

    # Nesting (run_scrape_all → single-source runners): the inner scrape shares the
    # outer collector and skips its own dedup; only the outer one dedups.
    @_with_autodedup
    def inner():
        _upsert_person_by_name("Nested Person", "src")
        return {"status": "ok"}

    @_with_autodedup
    def outer():
        r = inner()
        assert "deduplication" not in r      # nested scrape defers to the outer scope
        return {"status": "ok", "results": {"inner": r}}

    outer_res = outer()
    assert "deduplication" in outer_res
    assert "entity_deduplication" in outer_res
