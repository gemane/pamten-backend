"""A "Former …" Form 4 read on a fresh database, against a real ArcadeDB
(findings item 74, the user's option (a) of 2026-11-07): the person and the
seat their own filings state are written CLOSED. Verra Mobility's CFO Craig
Conti filed "Former Chief Financial Officer" a day before the rebuild of
2026-11-07 and vanished from it, while the database before kept him with a
closed seat. Only EDGAR is mocked."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

CONTI = {"name": "Craig C Conti", "role": "CFO", "former": True, "person_cik": "0001747814",
         "since": "2019-04-01", "until": "2026-11-05", "document_type": "4",
         "source_url": "https://www.sec.gov/Archives/edgar/data/1682745/x-index.htm",
         "source_date": "2026-11-06"}


def _scrape(executives):
    from app.scraper import runner, sec_edgar
    payload = {"name": "VERRA MOBILITY Corp", "cik": "0001682745", "ownership_filings": [],
               "executives": executives, "holdings": [], "former_names": [], "lei": None}
    with patch.object(sec_edgar, "scrape_company", return_value=payload), \
         patch.object(sec_edgar, "fetch_filer_country", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_headquarters", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_website", return_value=None), \
         patch.object(sec_edgar, "fetch_departures", return_value=[]), \
         patch.object(sec_edgar, "fetch_insider_holding", return_value=None), \
         patch.object(runner, "_record_sec_industry", return_value=None), \
         patch.object(runner, "get_source_enabled", return_value=True), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        return runner.run_scrape_sec_edgar("VERRA MOBILITY Corp")


def _seats(db):
    return db.run_command(
        "MATCH (p:Person {sec_cik:'0001747814'})-[r:HAS_ROLE]->(e:Entity) "
        "RETURN p.id AS pid, r.role AS role, r.since AS since, r.until AS until")


def test_a_fresh_read_writes_the_former_cfo_with_a_closed_seat(it_db):
    _scrape([CONTI])
    seats = _seats(it_db)
    assert len(seats) == 1
    assert seats[0]["pid"] == "sec:0001747814" and seats[0]["role"] == "CFO"
    assert seats[0]["since"] == "2019-04-01" and seats[0]["until"] == "2026-11-05"
    _scrape([CONTI])                                          # a second read
    assert _seats(it_db) == seats, "no second seat, never reopened"


def test_an_open_seat_is_closed_as_before(it_db):
    _scrape([{**CONTI, "former": False, "until": None}])      # the database before the departure
    assert _seats(it_db)[0]["until"] is None
    _scrape([CONTI])
    seats = _seats(it_db)
    assert len(seats) == 1 and seats[0]["until"] == "2026-11-05"
