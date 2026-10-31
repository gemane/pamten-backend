"""One CIK, one vertex type, against a real ArcadeDB (findings item 21). A
filer is a human being or a company: Dondero's HC code on NexPoint's 13D/A
made an Entity of him and his Form 4 a Person with the same id; Embraer's
Form 4 as a director of Eve Holding made a Person of a company."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

DONDERO_13DA = {
    "investor_name": "Dondero James D", "investor_cik": "0001228922", "is_individual": None,  # HC
    "form_type": "SCHEDULE 13D/A", "filing_type": "13D/A", "read_from": "field",
    "stake_percent": 20.1, "ownership_type": "controlling", "file_date": "2026-06-17",
    "source_url": "https://sec.gov/Archives/edgar/data/1620393/000143774926031229/-index.htm",
}
DONDERO_FORM4 = {
    "name": "James D Dondero", "role": "Director", "person_cik": "0001228922",
    "shares_owned": None, "source_date": "2026-06-17", "source_url": "https://sec.gov/f4-index.htm",
}
EMBRAER_FORM4 = {
    "name": "S.a Embraer", "role": "Director", "person_cik": "0001355444",
    "shares_owned": 220000000, "stake_percent": 70.0, "share_class": "Common Stock",
    "shares_outstanding": None, "source_date": "2026-06-17", "source_url": "https://sec.gov/f4-eve-index.htm",
}


@pytest.fixture
def graph(it_db):
    it_db.run_command("CREATE (s:Source {id:'sec', name:'SEC EDGAR'})")
    it_db.run_command("CREATE (e:Entity {id:'nxp', name:'NexPoint Real Estate Finance, Inc.', type:'company', "
                      "name_normalized:'nexpoint real estate finance', search_text:'NexPoint'})")
    return it_db


def _scrape(filings, executives):
    from app.scraper import runner, sec_edgar
    payload = {"name": "NexPoint Real Estate Finance, Inc.", "cik": "0001620393",
               "ownership_filings": filings, "executives": executives, "holdings": [],
               "former_names": [], "lei": None}
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
        return runner.run_scrape_sec_edgar("NexPoint Real Estate Finance, Inc.")


def _holders(graph, cik):
    return {label: [r["id"] for r in graph.run_command(
                f"MATCH (n:{label}) WHERE n.sec_cik = '{cik}' RETURN n.id AS id")]
            for label in ("Person", "Entity")}


@pytest.mark.parametrize("form_4_first", [True, False])
def test_an_hc_filer_and_their_form_4_are_one_person(graph, form_4_first):
    if form_4_first:
        _scrape([], [DONDERO_FORM4])
    _scrape([DONDERO_13DA], [] if form_4_first else [DONDERO_FORM4])
    held = _holders(graph, "0001228922")
    assert held == {"Person": ["sec:0001228922"], "Entity": []}
    owns = graph.run_command("MATCH (p:Person {id:'sec:0001228922'})-[r:OWNS]->(e:Entity {id:'nxp'}) "
                             "RETURN r.stake_percent AS pct")
    assert [r["pct"] for r in owns] == [20.1]


def test_a_form_4_cik_a_company_holds_is_that_company(graph):
    graph.run_command("CREATE (e:Entity {id:'sec:0001355444', name:'EMBRAER S.A.', type:'company', "
                      "sec_cik:'0001355444', name_normalized:'embraer', search_text:'EMBRAER S.A.'})")
    result = _scrape([], [EMBRAER_FORM4])
    assert _holders(graph, "0001355444") == {"Person": [], "Entity": ["sec:0001355444"]}
    assert graph.run_command("MATCH (p:Person)-[:HAS_ROLE]->(:Entity {id:'nxp'}) RETURN p") == []
    owns = graph.run_command("MATCH (o:Entity {id:'sec:0001355444'})-[r:OWNS]->(e:Entity {id:'nxp'}) "
                             "RETURN r.stake_percent AS pct, r.filing_type AS ft")
    assert [(r["pct"], r["ft"]) for r in owns] == [(70.0, "Form 4")]
    assert any(r.get("role") == "insider owner" for r in result["scraped"])
