"""A filer's own 13D/G holdings, written by the company scrape against a real
ArcadeDB, carry their reading grade — the edge and its claim. The scrape's
holdings loop passes the row's `read_from` through; the rows had none, and 681
13D/G edges were ungraded after the 2026-10-27 rebuild. Only EDGAR is mocked."""
from unittest.mock import patch

import pytest

from app.config import settings
from app.db.arcadedb import run_query, run_sql
from tests.scraper.test_sec_holdings import XML, _doc_for, _subs

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.runner.get_source_enabled", return_value=True):
        yield


def test_the_scrape_grades_a_filer_side_holding(it_db):
    from app.scraper import runner, sec_edgar
    subs = _subs([("SCHEDULE 13G", "a-1", "2026-04-30")])
    docs = {"a-1": XML.format(cik="0000105770", name="West Pharmaceutical", pct="7.48")}
    with patch("app.scraper.sec_edgar._get", return_value=subs), \
         patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
        holdings = sec_edgar.fetch_filer_holdings("0002100119")
    payload = {"name": "VANGUARD CAPITAL MANAGEMENT LLC", "cik": "0002100119", "lei": None,
               "former_names": [], "ownership_filings": [], "holdings": holdings, "executives": []}
    with patch.object(sec_edgar, "scrape_company", return_value=payload), \
         patch.object(sec_edgar, "fetch_filer_country", return_value="US"), \
         patch.object(sec_edgar, "fetch_filer_headquarters", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_website", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_sic", return_value=None), \
         patch.object(sec_edgar, "fetch_departures", return_value=[]), \
         patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None):
        runner.run_scrape_sec_edgar("Vanguard Capital Management")
    edges = run_query("MATCH (a:Entity)-[r:OWNS]->(b:Entity {name: 'West Pharmaceutical'}) "
                      "RETURN r.read_from AS rf, r.filing_type AS ft, r.stake_percent AS pct")
    assert [dict(e) for e in edges] == [{"rf": "field", "ft": "13G", "pct": 7.48}]
    claims = run_sql("SELECT read_from FROM Claim WHERE kind = 'owns' AND filing_type = '13G'")
    assert [c["read_from"] for c in claims] == ["field"]
