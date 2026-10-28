"""Real-ArcadeDB: the register's own name for a company wins by credibility,
whichever source created the node. After the 2026-10-27 rebuild 23 SEC filers
showed the Wikidata label (Molson Coors its pre-2020 name, "Molson Coors
Brewing Company") because Wikidata happened to create them, and 1,043 showed
EDGAR's name. Only EDGAR is mocked."""
from unittest.mock import patch

import pytest

from app.config import settings
from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration

CIK = "0000024545"


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.runner.get_source_enabled", return_value=True):
        yield


def _node():
    [row] = run_sql("SELECT id, name, name_credibility, name_normalized, aliases FROM Entity WHERE sec_cik = :c",
                    {"c": CIK})
    return dict(row)


def _sec_scrape():
    from app.scraper import runner, sec_edgar
    payload = {"name": "MOLSON COORS BEVERAGE CO", "cik": CIK, "lei": None, "former_names": [],
               "ownership_filings": [], "holdings": [], "executives": []}
    with patch.object(sec_edgar, "scrape_company", return_value=payload), \
         patch.object(sec_edgar, "fetch_filer_country", return_value="US"), \
         patch.object(sec_edgar, "fetch_filer_headquarters", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_website", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_sic", return_value=None), \
         patch.object(sec_edgar, "fetch_departures", return_value=[]), \
         patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None):
        runner.run_scrape_sec_edgar("Molson Coors")


def _wikidata():
    from app.scraper.runner import _upsert_entity
    return _upsert_entity("Molson Coors Brewing Company", None, None, "", "Q3319685",
                          source_id="wikidata", sec_cik=CIK, aliases=["Molson Coors"])


def test_wikidata_first_then_the_filer_takes_edgars_name(it_db):
    _wikidata()
    assert _node()["name"] == "Molson Coors Brewing Company"
    _sec_scrape()
    n = _node()
    assert (n["name"], n["name_credibility"]) == ("MOLSON COORS BEVERAGE CO", 98)
    assert n["name_normalized"] != "molson coors brewing"
    assert "Molson Coors Brewing Company" in n["aliases"]           # still found by the old label


def test_edgar_first_then_wikidata_leaves_edgars_name(it_db):
    _sec_scrape()
    _wikidata()
    assert _node()["name"] == "MOLSON COORS BEVERAGE CO"


def test_a_name_another_filing_spells_never_renames(it_db):
    """A 13D party, an Exhibit 21 row: the filer's spelling of someone else."""
    from app.scraper.graph_writer import _upsert_entity_by_name
    _wikidata()
    _upsert_entity_by_name("Molson Coors Beverage Co", cik=CIK, source_id="sec-edgar")
    assert _node()["name"] == "Molson Coors Brewing Company"


def test_a_more_credible_name_stays(it_db):
    from app.scraper.graph_writer import _upsert_entity_by_name
    it_db.run_command("CREATE (:Entity {id:'x', name:'Molson Coors Beverage Company', name_normalized:'molson coors beverage', "
                      "name_credibility: 99, sec_cik: $c})", {"c": CIK})
    _upsert_entity_by_name("MOLSON COORS BEVERAGE CO", cik=CIK, source_id="sec-edgar", register_name=True)
    assert _node()["name"] == "Molson Coors Beverage Company"
