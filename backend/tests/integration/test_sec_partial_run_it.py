"""A company scrape that lost a section to the network, against a real ArcadeDB
(findings item 41): the run is `partial`, what arrived is written, and the
company carries `scrape_incomplete_at` so the next ensure scrapes it again after
the cooldown, not after the 30-day TTL. A complete scrape clears the flag. Only
EDGAR is mocked (`scrape_company`)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.scraper import runner
from app.scraper.ondemand import decide_scrape

pytestmark = pytest.mark.integration

CIK = "0001653482"


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.runner.get_source_enabled", return_value=True), \
         patch("app.scraper.sec_edgar.fetch_filer_country", return_value="US"), \
         patch("app.scraper.sec_edgar.fetch_filer_headquarters", return_value=None), \
         patch("app.scraper.sec_edgar.fetch_filer_website", return_value=None), \
         patch("app.scraper.sec_edgar.fetch_filer_sic", return_value=None), \
         patch("app.scraper.sec_edgar.fetch_departures", return_value=[]), \
         patch("app.scraper.graph_writer._geocode_after_scrape", return_value={}):
        yield


def _data(failed):
    return {"cik": CIK, "name": "GitLab Inc.", "lei": None, "former_names": [],
            "ownership_filings": [], "holdings": [], "executives": [],
            "shares_outstanding": None, "shares_outstanding_detail": None,
            "failed_sections": failed}


def _scrape(failed):
    with patch("app.scraper.sec_edgar.scrape_company", return_value=_data(failed)):
        return runner.run_scrape_sec_edgar("GitLab Inc.")


def _node(it_db):
    return it_db.run_sql("SELECT id, last_scraped_at, scrape_incomplete_at, on_demand_scraped "
                         "FROM Entity WHERE sec_cik = :c", {"c": CIK})[0]


def test_a_lost_section_is_written_partial_and_flagged(it_db):
    out = _scrape(["13D/G owners"])
    assert out["status"] == "partial" and out["failed_sections"] == ["13D/G owners"]
    node = _node(it_db)
    assert node["id"] == out["entity_id"]
    assert node["on_demand_scraped"] is True and node["last_scraped_at"]
    assert node["scrape_incomplete_at"] == node["last_scraped_at"]


def test_the_next_ensure_retries_after_the_cooldown_not_the_ttl(it_db):
    _scrape(["insiders"])
    node = _node(it_db)
    stamped = datetime.fromisoformat(node["last_scraped_at"])
    soon = decide_scrape(node, requested_depth=0, force=False, now=stamped + timedelta(hours=1))
    later = decide_scrape(node, requested_depth=0, force=False, now=stamped + timedelta(hours=25))
    assert (soon.should_scrape, later.should_scrape, later.reason) == (False, True, "incomplete")


def test_a_complete_scrape_clears_the_flag(it_db):
    _scrape(["13D/G owners"])
    out = _scrape([])
    node = _node(it_db)
    assert out["status"] == "ok" and node["scrape_incomplete_at"] is None
    later = decide_scrape(node, requested_depth=0, force=False,
                          now=datetime.now(timezone.utc) + timedelta(hours=25))
    assert not later.should_scrape


def test_the_run_log_row_is_partial(it_db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.dependencies import require_contributor
    app.dependency_overrides[require_contributor] = lambda: {"id": "u", "role": "contributor"}
    try:
        with patch("app.scraper.sec_edgar.scrape_company", return_value=_data(["holdings"])):
            r = TestClient(app).post("/scraper/sec-edgar/run", params={"company": "GitLab Inc."})
    finally:
        app.dependency_overrides.pop(require_contributor, None)
    assert r.status_code == 200 and r.json()["status"] == "partial"
    rows = it_db.run_sql("SELECT status, error FROM ScrapeRun WHERE source = 'sec_edgar'")
    assert rows == [{"status": "partial", "error": "lost to the network: holdings"}]


def test_a_lost_request_is_noted_on_an_ok_row(it_db):
    # findings item 45: a single request lost to the network (a Form 4 the scrape
    # went on without) leaves the run `ok` and the company not flagged — the
    # row's note says how many, which the rebuild's verify can count
    import httpx
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.dependencies import require_contributor
    from app.scraper import sec_edgar

    def scrape(*a, **k):
        sec_edgar._request_lost("https://www.sec.gov/Archives/x/form4.xml", None,
                                httpx.ReadTimeout("stalled"), True)
        return _data([])
    app.dependency_overrides[require_contributor] = lambda: {"id": "u", "role": "contributor"}
    try:
        with patch("app.scraper.sec_edgar.scrape_company", side_effect=scrape):
            r = TestClient(app).post("/scraper/sec-edgar/run", params={"company": "GitLab Inc."})
    finally:
        app.dependency_overrides.pop(require_contributor, None)
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["lost_requests"] == 1
    rows = it_db.run_sql("SELECT status, error FROM ScrapeRun WHERE source = 'sec_edgar'")
    assert rows == [{"status": "ok", "error": "1 request(s) lost to the network"}]
    assert _node(it_db)["scrape_incomplete_at"] is None
