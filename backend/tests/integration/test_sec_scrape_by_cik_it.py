"""The SEC scrape's stored-CIK lookup against a real ArcadeDB: the query
resolves to its node through the same resolver Exhibit 21 uses, and that
node's CIK reaches the scrape — so a trade name EDGAR's name search cannot
match ("SpaceX") is read by number (2026-10-27). Only the network is mocked."""
import pytest
from unittest.mock import patch

from app.config import settings
from app.scraper import runner

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.runner.get_source_enabled", return_value=True):
        yield


def _spacex(it_db, cik="0001181412"):
    it_db.run_command(
        "CREATE (:Entity {id: 'lei:549300B9WLO96RQCXP87', name: 'SPACE EXPLORATION TECHNOLOGIES CORP.', "
        "name_normalized: 'space exploration technologies', type: 'company', "
        "search_text: 'SPACE EXPLORATION TECHNOLOGIES CORP. SpaceX', aliases: ['SpaceX'], "
        "sec_cik: $cik})", {"cik": cik})


def test_the_nodes_cik_reaches_the_scrape(it_db):
    _spacex(it_db)
    assert runner._stored_sec_cik("Space Exploration Technologies Corp") == "0001181412"
    with patch("app.scraper.sec_edgar.scrape_company", return_value=None) as scrape:
        out = runner.run_scrape_sec_edgar("Space Exploration Technologies Corp")
    scrape.assert_called_once_with("Space Exploration Technologies Corp", cik="0001181412")
    assert out["status"] == "no_results"


def test_a_node_without_a_cik_leaves_the_name_search(it_db):
    _spacex(it_db, cik=None)
    with patch("app.scraper.sec_edgar.scrape_company", return_value=None) as scrape:
        runner.run_scrape_sec_edgar("Space Exploration Technologies Corp")
    scrape.assert_called_once_with("Space Exploration Technologies Corp", cik=None)


def test_a_name_that_is_not_the_node_takes_no_cik(it_db):
    """A credible match only: a query sharing one word with the node is no
    reason to read that node's filer."""
    _spacex(it_db)
    assert runner._stored_sec_cik("Exploration Partners Fund") is None
