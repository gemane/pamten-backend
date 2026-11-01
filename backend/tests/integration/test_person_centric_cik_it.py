"""The person-centric Form 4 pass against a real ArcadeDB (findings item 29).
Robyn Denholm sits on Tesla's board but has filed no Form 4 within the
issuer-side window: the pass looks her up by name, writes her holding and
gives her the CIK it was filed under — which keys her (`node_ids`: a CIK
outranks a QID), the old id forwarding. Each person is asked once, only for a
seat still held."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

HOLDING = {"shares_owned": 85000.0, "share_class": "Common Stock", "stake_percent": None,
           "shares_outstanding": None, "denominator_date": None, "cik": "0001242782",
           "source_url": "https://www.sec.gov/Archives/edgar/data/1242782/000110465925046296/"
                         "0001104659-25-046296-index.htm",
           "source_date": "2025-05-08"}


@pytest.fixture
def graph(it_db):
    it_db.run_command("CREATE (s:Source {id:'sec', name:'SEC EDGAR'})")
    it_db.run_command("CREATE (e:Entity {id:'tsla', name:'Tesla, Inc.', type:'company', sec_cik:'0001318605', "
                      "name_normalized:'tesla', search_text:'Tesla'})")
    for pid, name in (("wd:Q58345040", "Robyn Denholm"), ("wd:Q1903673", "Martin Eberhard")):
        it_db.run_command(f"CREATE (p:Person {{id:'{pid}', full_name:'{name}', wikidata_id:'{pid[3:]}'}})")
    # two open seats for Denholm, one ended seat for Eberhard
    for role in ("Chair", "Director"):
        it_db.run_command("MATCH (p:Person {id:'wd:Q58345040'}), (e:Entity {id:'tsla'}) "
                          f"CREATE (p)-[:HAS_ROLE {{role:'{role}', source_id:'wd'}}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'wd:Q1903673'}), (e:Entity {id:'tsla'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'Chief Executive Officer', until:'2007-11-01', source_id:'wd'}]->(e)")
    return it_db


def _scrape(holding):
    from app.scraper import runner, sec_edgar
    asked = []

    def fake_holding(name, cik, shares_out, person_cik=None):
        asked.append((name, person_cik))
        return holding
    payload = {"name": "Tesla, Inc.", "cik": "0001318605", "ownership_filings": [], "executives": [],
               "holdings": [], "former_names": [], "lei": None}
    with patch.object(sec_edgar, "scrape_company", return_value=payload), \
         patch.object(sec_edgar, "fetch_filer_country", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_headquarters", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_website", return_value=None), \
         patch.object(sec_edgar, "fetch_departures", return_value=[]), \
         patch.object(sec_edgar, "fetch_insider_holding", side_effect=fake_holding), \
         patch.object(runner, "_record_sec_industry", return_value=None), \
         patch.object(runner, "get_source_enabled", return_value=True), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        runner.run_scrape_sec_edgar("Tesla, Inc.")
    return asked


def test_a_director_without_a_window_form_4_gains_holding_and_cik(graph):
    asked = _scrape(HOLDING)
    assert asked == [("Robyn Denholm", None)]          # once, and not the ended seat
    people = graph.run_command("MATCH (p:Person) WHERE p.sec_cik = '0001242782' "
                               "RETURN p.id AS id, p.wikidata_id AS wid")
    assert [(r["id"], r["wid"]) for r in people] == [("sec:0001242782", "Q58345040")]
    assert [r["to"] for r in graph.run_command(
        "MATCH (m:MergedId {old_id:'wd:Q58345040'}) RETURN m.new_id AS to")] == ["sec:0001242782"]
    owns = graph.run_command("MATCH (p:Person {id:'sec:0001242782'})-[r:OWNS]->(e:Entity {id:'tsla'}) "
                             "RETURN r.shares AS shares, r.filing_type AS ft")
    assert [(r["shares"], r["ft"]) for r in owns] == [(85000.0, "Form 4")]


def test_a_cik_another_person_holds_is_not_stamped(graph):
    graph.run_command("CREATE (p:Person {id:'sec:0001242782', full_name:'DENHOLM ROBYN M', sec_cik:'0001242782'})")
    _scrape(HOLDING)
    people = graph.run_command("MATCH (p:Person) WHERE p.sec_cik = '0001242782' RETURN p.id AS id")
    assert [r["id"] for r in people] == ["sec:0001242782"]
    assert graph.run_command("MATCH (p:Person {id:'wd:Q58345040'}) RETURN p.sec_cik AS cik") == [{"cik": None}]


def test_a_second_run_asks_by_the_cik(graph):
    _scrape(HOLDING)
    graph.run_command("MATCH (p:Person {id:'sec:0001242782'})-[r:OWNS]->() DELETE r")
    assert _scrape(HOLDING) == [("Robyn Denholm", "0001242782")]
