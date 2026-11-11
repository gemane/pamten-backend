"""A 13D/G whose holder is now the issuer's own subsidiary ends (findings item
99 part 3, the user's word of 2026-11-11): Texaco's 13D of 2000 on Chevron,
16.5 %, stayed open beside Chevron's Exhibit 21 naming Texaco — a two-company
cycle on dev."""
import pytest

pytestmark = pytest.mark.integration

LIST = "https://www.sec.gov/Archives/edgar/data/93410/000009341026000078/cvx12312025ex211.htm"


def _graph(it_db):
    it_db.run_command("CREATE (:Entity {id:'cvx', name:'CHEVRON CORP', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'tx', name:'Texaco Inc', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'pf', name:'Pass Fund', type:'company'})")
    it_db.run_command("MATCH (a:Entity {id:'tx'}), (b:Entity {id:'cvx'}) CREATE (a)-[:OWNS {stake_percent: 16.5, "
                      "filing_type: '13D', source_id: 'sec-edgar', source_date: '2000-10-25', since: '2000-10-15', "
                      "stale: true, stale_reason: 'not_restated', until: null}]->(b)")
    it_db.run_command("MATCH (a:Entity {id:'cvx'}), (b:Entity {id:'tx'}) CREATE (a)-[:OWNS {filing_type: 'EX-21', "
                      f"source_id: 'sec-edgar', source_date: '2026-02-24', source_url: '{LIST}', until: null}}]->(b)")


def test_the_subsidiary_s_old_statement_ends_on_the_list_s_date(it_db):
    from app.scraper.sec_writer import HOLDER_IS_SUBSIDIARY, close_held_by_subsidiary
    _graph(it_db)
    # a 13F position is another rulebook: kept
    it_db.run_command("MATCH (a:Entity {id:'tx'}), (b:Entity {id:'cvx'}) CREATE (a)-[:OWNS {stake_percent: 0.1, "
                      "filing_type: '13F', source_id: 'sec-edgar', source_date: '2000-11-14', until: null}]->(b)")
    lines = close_held_by_subsidiary()
    assert len(lines) == 1 and "Texaco Inc" in lines[0]
    r = it_db.run_command("MATCH (:Entity {id:'tx'})-[r:OWNS {filing_type:'13D'}]->(:Entity {id:'cvx'}) "
                          "RETURN r.until AS u, r.until_reason AS why, r.until_source_url AS url, r.stale AS st")[0]
    assert (r["u"], r["why"], r["url"], r["st"]) == ("2026-02-24", HOLDER_IS_SUBSIDIARY, LIST, False)
    assert it_db.run_command("MATCH (:Entity {id:'tx'})-[r:OWNS {filing_type:'13F'}]->(:Entity {id:'cvx'}) "
                             "RETURN r.until AS u")[0]["u"] is None
    # the subsidiary edge itself stays; a second pass changes nothing
    assert it_db.run_command("MATCH (:Entity {id:'cvx'})-[r:OWNS]->(:Entity {id:'tx'}) RETURN r.until AS u")[0]["u"] is None
    assert close_held_by_subsidiary() == []


def test_a_statement_after_the_list_or_another_issuer_stays(it_db):
    from app.scraper.sec_writer import close_held_by_subsidiary
    _graph(it_db)
    # filed after the list: a subsidiary holding its parent's shares now
    it_db.run_command("MATCH (:Entity {id:'tx'})-[r:OWNS]->(:Entity {id:'cvx'}) SET r.source_date = '2026-05-01'")
    # a passive holder that is nobody's subsidiary
    it_db.run_command("MATCH (a:Entity {id:'pf'}), (b:Entity {id:'cvx'}) CREATE (a)-[:OWNS {stake_percent: 6.0, "
                      "filing_type: '13G', source_id: 'sec-edgar', source_date: '2000-01-01', until: null}]->(b)")
    assert close_held_by_subsidiary() == []
    assert len(it_db.run_command("MATCH ()-[r:OWNS]->(:Entity {id:'cvx'}) WHERE r.until IS NULL RETURN r")) == 2


def test_the_pass_scoped_to_another_issuer_leaves_it(it_db):
    from app.scraper.sec_writer import close_held_by_subsidiary
    _graph(it_db)
    assert close_held_by_subsidiary(issuer_id="pf") == []
    assert len(close_held_by_subsidiary(issuer_id="cvx")) == 1
