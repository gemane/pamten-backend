"""A foreign issuer's ADS CUSIP from its F-6 filings, against a real ArcadeDB
(findings item 72): the SEC scrape adds it to the node's `cusips` — beside a
13F table's or a 13D/G cover's code, never instead of one — and the securities
bridge reads it from there as the US ISIN GLEIF maps to the issuer's LEI.
Only EDGAR is mocked."""
from unittest.mock import patch

import pytest

from app.config import settings
from app.scraper import gleif_isin, runner

pytestmark = pytest.mark.integration

NESTLE = {"cik": "0000792990", "name": "NESTLE SA", "lei": None, "former_names": [],
          "ownership_filings": [], "holdings": [], "executives": [], "shares_outstanding": None,
          "shares_outstanding_detail": None, "ads_cusips": ["641069406"],
          "ads_cusip_url": "https://www.sec.gov/Archives/edgar/data/792990/000119380526000160/0001193805-26-000160.txt"}


@pytest.fixture(autouse=True)
def _offline():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.runner.get_source_enabled", return_value=True), \
         patch("app.scraper.sec_edgar.fetch_filer_headquarters", return_value=None), \
         patch("app.scraper.sec_edgar.fetch_filer_website", return_value=None), \
         patch("app.scraper.sec_edgar.fetch_filer_sic", return_value=None), \
         patch("app.scraper.sec_edgar.fetch_filer_country", return_value="CH"):
        yield


def _scrape(data):
    with patch("app.scraper.sec_edgar.scrape_company", return_value=data):
        return runner.run_scrape_sec_edgar("Nestlé")


def _node(it_db):
    rows = it_db.run_sql("SELECT id, cusip, cusips FROM Entity WHERE sec_cik = '0000792990'")
    assert len(rows) == 1
    return rows[0]


def test_the_scrape_stores_the_ads_cusip_and_the_bridge_reads_it(it_db):
    assert _scrape(NESTLE)["status"] in ("ok", "partial")
    node = _node(it_db)
    assert node["cusips"] == ["641069406"] and node["cusip"] == "641069406"
    bridged = {n["id"]: gleif_isin._isins_of(n) for n in gleif_isin._nodes_with_cusips()}
    assert bridged[node["id"]] == ["US6410694060"]


def test_added_beside_a_known_code_never_instead(it_db):
    # SQL: ArcadeDB's Cypher refuses a list literal
    it_db.run_sql(
        "INSERT INTO Entity SET id = 'sec:0000792990', name = 'NESTLE SA', name_normalized = 'nestle sa', "
        "type = 'company', sec_cik = '0000792990', cusip = '641069505', cusips = ['641069505']")
    _scrape(NESTLE)
    node = _node(it_db)
    assert node["cusips"] == ["641069505", "641069406"]
    assert node["cusip"] == "641069505", "a code the node holds is kept"
    _scrape(NESTLE)
    assert _node(it_db)["cusips"] == ["641069505", "641069406"], "a second read adds nothing"


def test_a_filer_without_one_is_untouched(it_db):
    _scrape({**NESTLE, "ads_cusips": [], "ads_cusip_url": None})
    node = _node(it_db)
    assert node.get("cusips") is None and node.get("cusip") is None
