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


def test_the_pass_alone_writes_the_holding_of_a_seat_added_after_the_scrape(graph):
    """findings item 67: with the registers running first, Wikidata's officers
    arrive after the SEC part ran; run-all asks the seats once more at the end
    (`run_sec_seat_holders`) — Denholm's open seats are looked up, Eberhard's
    ended one is not, the holding and the CIK land as on the SEC's own run."""
    from app.scraper import runner, sec_edgar
    asked = []

    def fake_holding(name, cik, shares_out, person_cik=None):
        asked.append(name)
        return HOLDING
    assert runner.seat_holders_without_holdings("tsla") == frozenset({"wd:Q58345040"})
    sec = {"status": "ok", "cik": "0001318605", "entity_id": "tsla", "company": "Tesla, Inc.", "seats_asked": []}
    assert runner.rerun_for_new_seats(sec) is True                         # a seat the SEC run never looked up
    with patch.object(sec_edgar, "fetch_shares_outstanding_detail", return_value=None), \
         patch.object(sec_edgar, "fetch_departures", return_value=[]), \
         patch.object(sec_edgar, "fetch_insider_holding", side_effect=fake_holding), \
         patch.object(runner, "get_source_enabled", return_value=True), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        out = runner.run_sec_seat_holders("tsla", "0001318605", "Tesla, Inc.")
    assert asked == ["Robyn Denholm"] and out["total"] == 1 and out["status"] == "ok"
    assert graph.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 1
    [row] = graph.run_sql("SELECT id, sec_cik, wikidata_id FROM Person WHERE wikidata_id = 'Q58345040'")
    assert row["sec_cik"] == "0001242782" and row["id"] == "sec:0001242782"      # keyed by the CIK found
    assert runner.rerun_for_new_seats(sec) is False                          # nothing left to look up


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
    # the holding is the CIK holder's — the Form 4's own reporting owner — not the twin's
    owners = graph.run_command("MATCH (p:Person)-[r:OWNS]->(e:Entity {id:'tsla'}) RETURN p.id AS id, r.filing_type AS ft")
    assert owners == [{"id": "sec:0001242782", "ft": "Form 4"}]


def test_a_13dg_on_the_cik_holder_keeps_the_form_4_out(graph):
    """findings item 73: Galperin's 13G/A sat on his EDGAR node, the Form 4 found
    for his Wikidata twin was written to the twin, and the merge kept it."""
    from app.scraper import sec_writer
    graph.run_command("CREATE (p:Person {id:'sec:0001242782', full_name:'DENHOLM ROBYN M', sec_cik:'0001242782'})")
    sec_writer._upsert_owns_sec(owner_id="sec:0001242782", owned_id="tsla", source_id="sec",
                                ownership_type="minority", file_date="2026-02-14", stake_percent=6.1,
                                source_url="https://www.sec.gov/13g.xml", owner_label="Person",
                                filing_type="13G/A", read_from="field")
    _scrape(HOLDING)
    owners = graph.run_command("MATCH (p:Person)-[r:OWNS]->(e:Entity {id:'tsla'}) RETURN p.id AS id, r.filing_type AS ft")
    assert owners == [{"id": "sec:0001242782", "ft": "13G/A"}], "no Form 4 edge on the twin, none over the 13G"


def test_a_company_cik_is_not_a_person_s_holding(graph):
    graph.run_command("CREATE (e:Entity {id:'sec:0001242782', name:'DENHOLM HOLDINGS LLC', type:'company', "
                      "sec_cik:'0001242782'})")
    _scrape(HOLDING)
    assert graph.run_command("MATCH (p:Person)-[r:OWNS]->(e:Entity {id:'tsla'}) RETURN p.id AS id") == []


def test_a_second_run_asks_by_the_cik(graph):
    _scrape(HOLDING)
    graph.run_command("MATCH (p:Person {id:'sec:0001242782'})-[r:OWNS]->() DELETE r")
    assert _scrape(HOLDING) == [("Robyn Denholm", "0001242782")]


def test_the_rerun_finds_a_filer_re_keyed_after_the_sec_run(it_db):
    """findings item 67 b (rebuild 2026-11-07b): the SEC part wrote BofA as
    sec:0000070858; its Wikidata part stated the LEI and `node_ids` renamed the
    node lei:9DJT…, leaving a forwarding row; Wikidata's seat sits on the new
    id. The rerun reads the seats under the id the filer has now."""
    from app.merged_ids import rename_node_id
    from app.scraper import runner
    it_db.run_command("CREATE (e:Entity {id:'sec:0000070858', name:'BANK OF AMERICA CORP /DE/', type:'company', "
                      "sec_cik:'0000070858', lei_id:'9DJT3UXIJIZJI4WXO774'})")
    with runner.db.get_session() as session:
        assert rename_node_id(session, "Entity", "sec:0000070858", "lei:9DJT3UXIJIZJI4WXO774")
    it_db.run_command("CREATE (p:Person {id:'wd:Q109659430', full_name:'Frank Bramble', wikidata_id:'Q109659430'})")
    it_db.run_command("MATCH (p:Person {id:'wd:Q109659430'}), (e:Entity {id:'lei:9DJT3UXIJIZJI4WXO774'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'Board Member', source_id:'wikidata'}]->(e)")
    sec = {"status": "ok", "cik": "0000070858", "entity_id": "sec:0000070858",
           "company": "BANK OF AMERICA CORP /DE/", "seats_asked": []}
    assert runner.seat_holders_without_holdings("sec:0000070858") == frozenset()   # the old id: no node, no seat
    assert runner.seat_rerun_target(sec) == "lei:9DJT3UXIJIZJI4WXO774"
    assert runner.seat_rerun_target({**sec, "seats_asked": ["wd:Q109659430"]}) is None
    assert runner._node_id_now("lei:9DJT3UXIJIZJI4WXO774") == "lei:9DJT3UXIJIZJI4WXO774"  # a live id stays
