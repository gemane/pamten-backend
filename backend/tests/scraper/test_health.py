"""Per-source health: the aggregation, and who may see how much of it.

The aggregation tests feed literal run lists; the visibility tests drive the
endpoint over HTTP so the auth dependency actually runs, with the aggregate
stubbed — the same split test_runs_visibility.py uses.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.scraper.health import _source_entry, _streak

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    # get_source_enabled reads the ScraperSource toggle — a real DB query. A
    # mocked suite that reaches a real server locks ArcadeDB out (again);
    # patch at the name health.py actually resolved.
    monkeypatch.setattr("app.scraper.health.get_source_enabled", lambda name: True)


def run(status, started, total=0, stale=False, error=""):
    return {"source": "wikidata", "status": status, "started_at": started,
            "total": total, "stale": stale, "error": error}


class TestStreak:
    def test_consecutive_failures_count_from_the_newest(self):
        assert _streak([run("failed", "t3"), run("failed", "t2"), run("ok", "t1")]) == 2

    def test_a_success_stops_the_streak_even_with_older_failures(self):
        assert _streak([run("ok", "t3"), run("failed", "t2"), run("failed", "t1")]) == 0

    def test_running_and_skipped_neither_break_nor_extend(self):
        # An in-flight attempt is not evidence either way; a skip never
        # touched the source.
        rs = [run("running", "t4"), run("failed", "t3"),
              run("skipped", "t2"), run("failed", "t1")]
        assert _streak(rs) == 2


class TestSourceEntry:
    def test_a_source_that_never_ran_still_appears(self):
        e = _source_entry("wikidata", [], NOW)
        assert e["last_run_at"] is None and e["last_status"] is None
        assert e["failure_streak"] == 0 and e["runs_24h"] == 0
        assert e["label"] == "Wikidata"

    def test_a_stuck_running_row_reports_stale_not_running(self):
        rs = [run("running", NOW.isoformat(), stale=True)]
        assert _source_entry("wikidata", rs, NOW)["last_status"] == "stale"

    def test_last_ok_reaches_past_newer_failures(self):
        ok_at = (NOW - timedelta(hours=2)).isoformat()
        rs = [run("failed", (NOW - timedelta(hours=1)).isoformat()),
              run("ok", ok_at, total=7)]
        e = _source_entry("wikidata", rs, NOW)
        assert e["last_ok_at"] == ok_at and e["last_status"] == "failed"

    def test_the_24h_window_counts_only_recent_runs(self):
        rs = [run("ok", (NOW - timedelta(hours=1)).isoformat()),
              run("ok", (NOW - timedelta(hours=23)).isoformat()),
              run("ok", (NOW - timedelta(hours=25)).isoformat())]
        assert _source_entry("wikidata", rs, NOW)["runs_24h"] == 2

    def test_a_pipeline_name_gets_its_label_and_no_toggle(self):
        e = _source_entry("sec-13f", [run("ok", NOW.isoformat())], NOW)
        assert e["label"] == "SEC 13F holders"
        assert e["enabled"] is None


HEALTH = {
    "sources": [{"name": "wikidata", "label": "Wikidata", "last_status": "failed",
                 "failure_streak": 1, "last_error": "boom at https://internal/x"}],
    "datasets": [],
    "import_lock": {"held": True, "holder": "new-database@host:123",
                    "acquired_at": "t", "age_seconds": 5, "stale": False},
}


@pytest.fixture(autouse=True)
def _stub_health():
    with patch("app.scraper.health.source_health",
               return_value={"sources": [dict(s) for s in HEALTH["sources"]],
                             "datasets": [], "import_lock": dict(HEALTH["import_lock"])}):
        yield


class TestVisibility:
    def test_anonymous_gets_health_without_error_or_holder(self, client):
        r = client.get("/v1/scraper/health")
        assert r.status_code == 200
        body = r.json()
        assert "last_error" not in body["sources"][0]
        assert "holder" not in body["import_lock"]
        assert body["import_lock"]["held"] is True
        assert body["sources"][0]["failure_streak"] == 1

    def test_a_viewer_is_redacted_like_anonymous(self, client, make_token):
        r = client.get("/v1/scraper/health",
                       headers={"Authorization": f"Bearer {make_token(role='viewer')}"})
        assert "last_error" not in r.json()["sources"][0]

    def test_a_contributor_sees_the_error_and_the_holder(self, client, make_token):
        r = client.get("/v1/scraper/health",
                       headers={"Authorization": f"Bearer {make_token(role='contributor')}"})
        body = r.json()
        assert body["sources"][0]["last_error"].startswith("boom")
        assert body["import_lock"]["holder"] == "new-database@host:123"
