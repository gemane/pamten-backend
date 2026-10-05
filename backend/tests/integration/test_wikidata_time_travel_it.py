"""
The generic writer and the remaining time-travel fixes, against a real
ArcadeDB: Wikidata ownership with its statements' periods, founding dates
that only move earlier, and the shortcut pass reading current paths only.
"""
import pytest

from app.scraper.graph_writer import _upsert_owns

pytestmark = pytest.mark.integration


@pytest.fixture
def pair(it_db):
    it_db.run_command("CREATE (:Entity {id:'o', name:'Owner', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'c', name:'Company', type:'company'})")
    return it_db


def _edges(db, a="o", b="c"):
    rows = db.run_command("MATCH (x {id:$a})-[r:OWNS]->(y {id:$b}) RETURN r.since AS since, "
                          "r.until AS until, r.source_date AS sdate", {"a": a, "b": b})
    return sorted(rows, key=lambda r: (r.get("since") or "", r.get("until") or ""))


class TestTheGenericWriter:
    def test_an_ended_statement_is_its_own_closed_edge_once(self, pair):
        _upsert_owns("o", "c", "wd", since="2015-07-01")                     # current
        _upsert_owns("o", "c", "wd", since="2001-03-05", until="2015-06-30")  # former
        _upsert_owns("o", "c", "wd", since="2001-03-05", until="2015-06-30")  # re-read
        assert [(e["since"], e["until"]) for e in _edges(pair)] == [
            ("2001-03-05", "2015-06-30"), ("2015-07-01", None)]

    def test_the_start_is_combined_earliest(self, pair):
        _upsert_owns("o", "c", "wd")
        _upsert_owns("o", "c", "wd", since="2009-00-00")
        _upsert_owns("o", "c", "wd", since="2012-05-01")
        assert [e["since"] for e in _edges(pair)] == ["2009-00-00"]

    def test_an_undated_statement_is_dated_by_the_scrape(self, pair):
        _upsert_owns("o", "c", "wd")
        assert (_edges(pair)[0]["sdate"] or "") >= "2026-01-01"
        claim = pair.run_sql("SELECT source_date FROM Claim WHERE kind = 'owns'")[0]
        assert (claim["source_date"] or "") >= "2026-01-01"


def test_a_scrape_never_postpones_a_founding_date(it_db):
    from app.scraper.runner import _upsert_entity
    it_db.run_command("CREATE (:Entity {id:'wd:Q1', name:'Co', wikidata_id:'Q1', founded:1975})")
    for year, expect in ((1993, 1975), (None, 1975), (1960, 1960)):
        _upsert_entity(name="Co", entity_type="company", country=None, founded=year, revenue=None,
                       description=None, wikidata_id="Q1", source_id="wd")
        got = it_db.run_command("MATCH (e:Entity {wikidata_id:'Q1'}) RETURN e.founded AS y")
        assert [r["y"] for r in got] == [expect]


def test_an_ended_direct_chain_proves_no_shortcut(it_db):
    from app.scraper.maintenance import mark_ownership_shortcuts
    for i in ("top", "mid", "low"):
        it_db.run_command(f"CREATE (:Entity {{id:'{i}', name:'{i}', type:'company'}})")
    it_db.run_command("MATCH (a {id:'top'}),(b {id:'mid'}) CREATE (a)-[:OWNS {direct_or_indirect:'direct'}]->(b)")
    it_db.run_command("MATCH (a {id:'mid'}),(b {id:'low'}) CREATE (a)-[:OWNS "
                      "{direct_or_indirect:'direct', until:'2020-01-01'}]->(b)")   # the chain ended
    it_db.run_command("MATCH (a {id:'top'}),(b {id:'low'}) CREATE (a)-[:OWNS {direct_or_indirect:'indirect'}]->(b)")
    mark_ownership_shortcuts()
    flag = it_db.run_command("MATCH (:Entity {id:'top'})-[r:OWNS]->(:Entity {id:'low'}) "
                             "RETURN r.shortcut AS s")[0].get("s")
    assert not flag, "an indirect holding hidden by a direct chain that no longer exists"
    # the same chain, current: then it is a shortcut
    it_db.run_command("MATCH (:Entity {id:'mid'})-[r:OWNS]->(:Entity {id:'low'}) SET r.until = null")
    mark_ownership_shortcuts()
    assert it_db.run_command("MATCH (:Entity {id:'top'})-[r:OWNS]->(:Entity {id:'low'}) "
                             "RETURN r.shortcut AS s")[0].get("s") is True


def test_opencorporates_incorporation_dates_the_company(it_db):
    from unittest.mock import patch
    from app.scraper import runner
    data = {"name": "Example Trading Ltd", "jurisdiction_code": "gb", "company_number": "01234567",
            "registered_address": {}, "incorporation_date": "1987-02-03", "company_type": "Private",
            "status": "Active", "officers": []}
    with patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_OPENCORPORATES_ENABLED", True), \
         patch("app.scraper.runner.get_source_enabled", return_value=True), \
         patch("app.scraper.open_corporates.scrape_company", return_value=data):
        runner.run_scrape_open_corporates("Example Trading Ltd")
    row = it_db.run_command("MATCH (e:Entity) WHERE e.name = 'Example Trading Ltd' "
                            "RETURN e.founded_date AS d, e.founded AS y")[0]
    assert (row["d"], row["y"]) == ("1987-02-03", 1987)


def test_proxy_voting_power_gets_the_proxys_date(it_db):
    from unittest.mock import patch
    from app.scraper.proxy_write import write_proxy_ownership
    it_db.run_command("CREATE (:Entity {id:'goog', name:'Alphabet Inc.', type:'company'})")
    it_db.run_command("CREATE (:Person {id:'lp', full_name:'Larry Page'})")
    it_db.run_command("MATCH (p {id:'lp'}),(c {id:'goog'}) CREATE (p)-[:OWNS {stake_percent: 6.0, source_id:'sec'}]->(c)")
    proxy = {"filing_date": "2026-04-24", "owners": [{"name": "Larry Page", "voting_power_pct": 26.1}]}
    with patch("app.scraper.proxy_statement.fetch_proxy_ownership", return_value=proxy):
        write_proxy_ownership("Alphabet Inc.", entity_id="goog")
    row = it_db.run_command("MATCH (:Person {id:'lp'})-[r:OWNS]->() RETURN r.voting_power_pct AS v, "
                            "r.source_date AS d")[0]
    assert (row["v"], row["d"]) == (26.1, "2026-04-24")
