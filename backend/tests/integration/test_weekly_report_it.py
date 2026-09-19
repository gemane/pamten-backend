"""The weekly digest against a real ArcadeDB: per-week counters land in the
right week, a first scrape is told from a refresh by the run's reason, the
company is named, failures are listed, growth is measured against the stored
previous digest, and prune keeps the current weeks."""
from datetime import datetime, timedelta, timezone

import pytest

from app import analytics, weekly
from app.db.arcadedb import run_sql
from app.scraper import run_log
from app.weekly_report import store_report, weekly_report

pytestmark = pytest.mark.integration

THIS = weekly.iso_week()
LAST = weekly.previous_week()


def _at(week_id: str, day_offset: int = 1) -> str:
    start, _ = weekly.week_bounds(week_id)
    return (start + timedelta(days=day_offset, hours=10)).isoformat()


def _run(source, target, status="ok", total=0, week=THIS, reason=None, entity_id="", error=""):
    run_sql("CREATE VERTEX ScrapeRun SET id = :id, source = :s, target = :t, status = :st, "
            "started_at = :at, finished_at = :at, total = :n, error = :e, reason = :r, entity_id = :eid",
            {"id": f"{source}-{target}-{week}-{reason}-{status}", "s": source, "t": target, "st": status,
             "at": _at(week), "n": total, "e": error, "r": reason or "", "eid": entity_id})


class TestPerWeekCounters:
    def test_searches_are_counted_in_the_week_they_happen(self, it_db):
        analytics.record_search("SpaceX", None, "selected")
        analytics.record_search("SpaceX", None, "selected")
        analytics.record_search("Alphabet", "FR", "zero")
        # a search that happened LAST week, as the counter would have written it
        run_sql("UPDATE SearchWeek SET key = :k, query = 'Old Co', country = '', week = :w, "
                "searches = 5, zero_results = 0, selected = 5 UPSERT WHERE key = :k",
                {"k": f"old co||{LAST}", "w": LAST})
        r = weekly_report(THIS)["searches"]
        assert r["total"] == 3 and r["distinct_queries"] == 2 and r["zero_results"] == 1
        assert r["top"][0] == {"query": "SpaceX", "country": None, "searches": 2, "zero_results": 0}
        assert weekly_report(LAST)["searches"]["total"] == 5, "last week's five stay last week's"

    def test_the_lifetime_row_still_moves_too(self, it_db):
        analytics.record_search("Siemens", "DE", "selected")
        assert run_sql("SELECT searches FROM SearchDemand WHERE key = 'siemens|DE'")[0]["searches"] == 1
        assert run_sql("SELECT searches FROM SearchWeek WHERE key = :k",
                       {"k": f"siemens|DE|{THIS}"})[0]["searches"] == 1

    def test_usage_events_are_counted_per_week(self, it_db):
        analytics.record_usage("export.csv")
        analytics.record_usage("export.csv")
        assert weekly_report(THIS)["searches"]["usage"] == {"export.csv": 2}


class TestScrapes:
    def test_a_first_scrape_is_told_from_a_refresh_and_named(self, it_db):
        run_sql("CREATE VERTEX Entity SET id = 'e-giga', name = 'Gigafund Management Company, LLC', "
                "name_normalized = 'gigafund', search_text = 'Gigafund', type = 'company'")
        run_sql("CREATE VERTEX Entity SET id = 'e-sx', name = 'SPACE EXPLORATION TECHNOLOGIES CORP.', "
                "name_normalized = 'spacex', search_text = 'SpaceX', type = 'company'")
        _run("wikidata", "Gigafund", total=4, reason="absent", entity_id="e-giga")
        _run("sec_edgar", "Gigafund", total=0, reason="absent", entity_id="e-giga")
        _run("wikidata", "SpaceX", total=2, reason="forced", entity_id="e-sx")
        _run("sec-13f", "SpaceX", total=292)                         # enrichment, no reason
        _run("sec-13f", "SpaceX", status="failed", error="500 from efts", reason=None)
        sc = weekly_report(THIS)["scrapes"]
        assert [d["name"] for d in sc["first_scrapes"]] == ["Gigafund Management Company, LLC"]
        assert sc["first_scrapes"][0]["records"] == 4
        assert [d["name"] for d in sc["refreshed"]] == ["SPACE EXPLORATION TECHNOLOGIES CORP."]
        assert sc["by_source"]["sec-13f"] == {"ok": 1, "failed": 1}
        assert sc["failures"] == [{"source": "sec-13f", "target": "SpaceX", "error": "500 from efts", "count": 1}]
        assert sc["sec_enrichments"] == {"sec-13f": [{"target": "SpaceX", "total": 292}]}
        assert sc["records_written"] == 298

    def test_the_real_writer_records_reason_and_entity(self, it_db):
        with run_log.record_run("wikidata", "Acme", reason="absent") as run:
            run["total"] = 3
            run["entity_id"] = "e-acme"
        row = run_log.list_runs()[0]
        assert (row["reason"], row["entity_id"], row["total"]) == ("absent", "e-acme", 3)

    def test_last_weeks_runs_stay_out(self, it_db):
        _run("wikidata", "Old", total=9, week=LAST, reason="absent", entity_id="e-old")
        assert weekly_report(THIS)["scrapes"]["runs"] == 0
        assert weekly_report(LAST)["scrapes"]["runs"] == 1


class TestImportsAndGraph:
    def test_imports_are_their_own_section(self, it_db):
        _run("gleif-update", "1d", total=184)
        _run("gleif-update", "1d", status="skipped", total=0)
        r = weekly_report(THIS)
        assert r["imports"] == {"gleif-update": {"runs": 2, "ok": 1, "failed": 0, "skipped": 1, "records": 184}}
        assert "gleif-update" not in r["scrapes"]["by_source"]

    def test_growth_is_measured_against_the_stored_previous_digest(self, it_db):
        first = weekly_report(LAST)
        assert first["graph"]["delta"] is None, "nothing to compare with yet"
        store_report(first)
        run_sql("CREATE VERTEX Entity SET id = 'e-new', name = 'New Co', name_normalized = 'new co', "
                "search_text = 'New Co', type = 'company'")
        second = weekly_report(THIS)
        assert second["graph"]["since"] == LAST
        assert second["graph"]["delta"]["companies"] == 1

    def test_prune_drops_old_weeks_and_keeps_current(self, it_db):
        old = weekly.iso_week(datetime.now(timezone.utc) - timedelta(days=400))
        run_sql("UPDATE SearchWeek SET key = :k, query = 'x', country = '', week = :w, searches = 1 "
                "UPSERT WHERE key = :k", {"k": f"x||{old}", "w": old})
        analytics.record_search("Fresh", None, "selected")
        out = analytics.prune(days=365)
        assert out["SearchWeek"] == 1
        weeks = {r["week"] for r in run_sql("SELECT week FROM SearchWeek")}
        assert weeks == {THIS}


class TestEndpoint:
    def test_weekly_is_admin_only_and_returns_the_digest(self, it_db):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.auth.dependencies import require_admin
        c = TestClient(app)
        assert c.get("/v1/analytics/weekly").status_code in (401, 403)
        app.dependency_overrides[require_admin] = lambda: {"id": "a", "role": "admin"}
        try:
            r = c.get("/v1/analytics/weekly", params={"week": THIS})
            assert r.status_code == 200 and r.json()["week"] == THIS
            assert c.get("/v1/analytics/weekly", params={"week": "nope"}).status_code == 422
        finally:
            app.dependency_overrides.pop(require_admin, None)
