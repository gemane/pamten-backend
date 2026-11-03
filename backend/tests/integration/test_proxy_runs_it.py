"""The proxy-statement endpoints leave a ScrapeRun row, against a real ArcadeDB
(findings item 42). Only EDGAR is mocked (`fetch_proxy_ownership`)."""
from unittest.mock import patch

import pytest

from app.config import settings

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.dependencies import require_contributor
    app.dependency_overrides[require_contributor] = lambda: {"id": "u", "role": "contributor"}
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        yield TestClient(app)
    app.dependency_overrides.pop(require_contributor, None)


def _rows(it_db, source):
    return it_db.run_sql("SELECT status, total, error, entity_id FROM ScrapeRun WHERE source = :s",
                         {"s": source})


def test_the_write_run_row_names_the_company_and_counts_the_edges(it_db, client):
    it_db.run_command("CREATE (:Entity {id:'goog', name:'Alphabet Inc.', type:'company'})")
    it_db.run_command("CREATE (:Person {id:'lp', full_name:'Larry Page'})")
    it_db.run_command("MATCH (p {id:'lp'}),(c {id:'goog'}) CREATE (p)-[:OWNS {stake_percent: 6.0, source_id:'sec'}]->(c)")
    proxy = {"filing_date": "2026-04-24", "owners": [{"name": "Larry Page", "voting_power_pct": 27.4}]}
    with patch("app.scraper.proxy_statement.fetch_proxy_ownership", return_value=proxy):
        r = client.post("/scraper/proxy-statement/write",
                        params={"company": "Alphabet Inc.", "entity_id": "goog"})
    assert r.status_code == 200 and r.json()["entity_id"] == "goog"
    assert _rows(it_db, "sec-proxy-write") == [{"status": "ok", "total": 1, "error": None, "entity_id": "goog"}]


def test_a_company_without_a_proxy_is_a_skipped_read(it_db, client):
    with patch("app.scraper.proxy_statement.fetch_proxy_ownership",
               return_value={"error": "No DEF 14A found on EDGAR for 'Acme'", "owners": []}):
        r = client.post("/scraper/proxy-statement/run", params={"company": "Acme"})
    assert r.status_code == 200
    assert _rows(it_db, "sec-proxy") == [{"status": "skipped", "total": 0,
                                          "error": "No DEF 14A found on EDGAR for 'Acme'",
                                          "entity_id": None}]


def test_switched_off_is_403_and_no_row(it_db, client):
    with patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", False):
        r = client.post("/scraper/proxy-statement/run", params={"company": "Acme"})
    assert r.status_code == 403 and _rows(it_db, "sec-proxy") == []
