"""Of record versus beneficial, against a real ArcadeDB: the SEC company scrape
sees a holder's 13G/A statement and their Form 4 in one run (Elon Musk at
Tesla, June 2026). The 13G/A is the stake — count, percentage, date — and the
Form 4 writes nothing; an insider without a 13D/G keeps the Form 4; a 13D/G
that reported an exit no longer speaks and the Form 4 writes again."""
from unittest.mock import patch

import pytest

from app.claims import KIND_OWNS, claims_for
from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration

MUSK_13GA = {
    "investor_name": "Elon R. Musk", "investor_cik": "0001494730", "is_individual": True,
    "form_type": "SCHEDULE 13G/A", "filing_type": "13G/A", "read_from": "field",
    "stake_percent": 19.9, "shares": 699580882, "shares_outstanding": None,
    "ownership_type": "minority", "file_date": "2026-06-17", "event_date": "2026-06-16",
    "source_url": "https://sec.gov/Archives/edgar/data/1318605/000110465926075203/-index.htm",
}
MUSK_FORM4 = {
    "name": "Musk Elon", "role": "CEO", "person_cik": "0001494730",
    "shares_owned": 1123324786, "stake_percent": 28.4188, "share_class": "Common Stock",
    "shares_outstanding": 3952757946, "denominator_date": "2026-10-15",
    "source_date": "2026-06-17",
    "source_url": "https://sec.gov/Archives/edgar/data/1318605/000110465926075213/-index.htm",
}
CFO_FORM4 = {
    "name": "Taneja Vaibhav", "role": "CFO", "person_cik": "0001771340",
    "shares_owned": 100000, "stake_percent": 0.0025, "share_class": "Common Stock",
    "shares_outstanding": 3952757946, "denominator_date": "2026-10-15",
    "source_date": "2026-06-17", "source_url": "https://sec.gov/f4-cfo-index.htm",
}


@pytest.fixture
def graph(it_db):
    it_db.run_command("CREATE (s:Source {id:'sec', name:'SEC EDGAR'})")
    it_db.run_command("CREATE (e:Entity {id:'tsla', name:'Tesla, Inc.', type:'company', "
                      "name_normalized:'tesla', search_text:'Tesla, Inc.'})")
    return it_db


def _scrape(filings, executives):
    from app.scraper import runner, sec_edgar
    payload = {"name": "Tesla, Inc.", "cik": "0001318605", "ownership_filings": filings,
               "executives": executives, "holdings": [], "former_names": [], "lei": None}
    with patch.object(sec_edgar, "scrape_company", return_value=payload), \
         patch.object(sec_edgar, "fetch_filer_country", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_headquarters", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_website", return_value=None), \
         patch.object(sec_edgar, "fetch_departures", return_value=[]), \
         patch.object(sec_edgar, "fetch_insider_holding", return_value=None), \
         patch.object(runner, "get_source_enabled", return_value=True), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        return runner.run_scrape_sec_edgar("Tesla, Inc.")


def _owner_rows(graph):
    return graph.run_command(
        "MATCH (p:Person)-[r:OWNS]->(e:Entity {id:'tsla'}) WHERE r.until IS NULL "
        "RETURN p.id AS pid, p.full_name AS name, r.stake_percent AS pct, r.shares AS sh, "
        "r.filing_type AS ft, r.event_date AS ed, r.ownership_type AS ot ORDER BY pct DESC")


def test_the_13ga_statement_is_the_stake_and_the_form_4_writes_nothing(graph):
    result = _scrape([MUSK_13GA], [MUSK_FORM4, CFO_FORM4])
    rows = _owner_rows(graph)
    assert [(r["pct"], r["sh"], r["ft"], r["ed"], r["ot"]) for r in rows] == [
        (19.9, 699580882, "13G/A", "2026-06-16", "minority"),
        (0.0025, 100000, "Form 4", None, "minority")]
    musk = rows[0]["pid"]
    assert len(graph.run_command("MATCH (p:Person) RETURN p")) == 2, "the 13G/A filer and the Form 4 insider are one person"
    claims = [c for c in claims_for(from_id=musk, to_id="tsla", kind=KIND_OWNS) if c["source_id"] == "sec"]
    assert [(c["filing_type"], c["stake_percent"]) for c in claims] == [("13G/A", 19.9)]
    assert [r["name"] for r in result["scraped"] if r["role"] == "insider owner (13D/G states the holding)"] == ["Musk Elon"]
    assert run_sql("SELECT FROM StakeHistory") == []            # one statement, never a flap


def test_a_rescrape_changes_nothing(graph):
    _scrape([MUSK_13GA], [MUSK_FORM4])
    before = _owner_rows(graph)
    _scrape([MUSK_13GA], [MUSK_FORM4])
    assert _owner_rows(graph) == before
    assert run_sql("SELECT FROM StakeHistory") == []


def test_a_form_4_claim_from_before_the_rule_is_replaced_by_the_13ga(graph):
    """Dev today: the pair holds the Form 4 figure from an earlier run. The next
    run states the 13G/A; its date precedes the Form 4's restatement, so the
    history keeps one row — the 13G/A, previous 28.4188 — and time travel
    never shows the count of record after June."""
    _scrape([], [MUSK_FORM4])
    assert [(r["pct"], r["ft"]) for r in _owner_rows(graph)] == [(28.4188, "Form 4")]
    _scrape([MUSK_13GA], [MUSK_FORM4])
    assert [(r["pct"], r["sh"], r["ft"]) for r in _owner_rows(graph)] == [(19.9, 699580882, "13G/A")]
    rows = run_sql("SELECT stake_percent, previous_percent, as_of, filing_type FROM StakeHistory")
    assert [(r["stake_percent"], r["previous_percent"], r["as_of"], r["filing_type"]) for r in rows] == [
        (19.9, 28.4188, "2026-06-16", "13G/A")]


def test_a_13ga_that_reported_an_exit_lets_the_form_4_write(graph):
    exited = {**MUSK_13GA, "stake_percent": 0.0, "shares": 0, "until": "2026-03-31",
              "event_date": "2026-03-31", "file_date": "2026-04-02"}
    _scrape([exited], [MUSK_FORM4])
    rows = _owner_rows(graph)
    assert [(r["pct"], r["ft"]) for r in rows] == [(28.4188, "Form 4")]
