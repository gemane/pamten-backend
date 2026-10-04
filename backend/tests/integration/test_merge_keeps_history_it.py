"""
Merges and clean-ups keep history — against a real ArcadeDB.

Time travel can only show a past year that the graph still holds. These paths
used to destroy it: a merge dropped the removed node's ENDED edge whenever the
kept node had a current one to the same company (and the DETACH DELETE took
it for good), two current edges were collapsed without combining their start
dates, the manual person merge folded an ended holding INTO a current one and
closed it, the same-source edge dedup deleted the loser unfolded, and the
bloc-retire step deleted every stake-less SEC edge of the pair, ended or not.
"""
import pytest

pytestmark = pytest.mark.integration


def _owns(it_db, src_id, tgt_id):
    return sorted(
        ({k: v for k, v in r.items() if not k.startswith("@")}
         for r in it_db.run_command(
             "MATCH (a {id: $s})-[r:OWNS]->(b {id: $t}) RETURN r.since AS since, "
             "r.since_basis AS since_basis, r.until AS until, r.stake_percent AS stake_percent, "
             "r.source_id AS source_id", {"s": src_id, "t": tgt_id})),
        key=lambda e: (e.get("since") or "", e.get("until") or ""))


def _roles(it_db, pid, eid):
    return sorted(
        ({k: v for k, v in r.items() if not k.startswith("@")}
         for r in it_db.run_command(
             "MATCH (a {id: $p})-[r:HAS_ROLE]->(b {id: $e}) RETURN r.role AS role, "
             "r.since AS since, r.until AS until", {"p": pid, "e": eid})),
        key=lambda e: (e.get("since") or "", e.get("until") or ""))


@pytest.fixture
def two_companies(it_db):
    for i in ("keep", "dead", "target", "owner"):
        it_db.run_command(f"CREATE (:Entity {{id:'{i}', name:'{i}', type:'company'}})")
    return it_db


class TestEntityMerge:
    def test_an_ended_period_survives_beside_a_current_one(self, two_companies):
        from app.scraper.maintenance import _migrate_entity_edges
        db = two_companies
        db.run_command("MATCH (a:Entity {id:'keep'}),(t:Entity {id:'target'}) "
                       "CREATE (a)-[:OWNS {since:'2020-01-01', stake_percent:60, source_id:'gleif'}]->(t)")
        db.run_command("MATCH (a:Entity {id:'dead'}),(t:Entity {id:'target'}) "
                       "CREATE (a)-[:OWNS {since:'2005-03-01', until:'2012-06-30', stake_percent:40, source_id:'psc'}]->(t)")
        _migrate_entity_edges("dead", "keep")
        edges = _owns(db, "keep", "target")
        assert [(e.get("since"), e.get("until")) for e in edges] == [
            ("2005-03-01", "2012-06-30"), ("2020-01-01", None)]

    def test_two_current_edges_fold_to_the_earliest_start(self, two_companies):
        from app.scraper.maintenance import _migrate_entity_edges
        db = two_companies
        db.run_command("MATCH (a:Entity {id:'keep'}),(t:Entity {id:'target'}) "
                       "CREATE (a)-[:OWNS {since:'2020-01-01', stake_percent:60, source_id:'gleif'}]->(t)")
        db.run_command("MATCH (a:Entity {id:'dead'}),(t:Entity {id:'target'}) "
                       "CREATE (a)-[:OWNS {since:'2005-03-01', stake_percent:60, source_id:'gleif'}]->(t)")
        _migrate_entity_edges("dead", "keep")
        edges = _owns(db, "keep", "target")
        assert len(edges) == 1 and edges[0]["since"] == "2005-03-01" and edges[0].get("until") is None

    def test_the_same_period_twice_is_one_edge(self, two_companies):
        from app.scraper.maintenance import _migrate_entity_edges
        db = two_companies
        for n in ("keep", "dead"):
            db.run_command(f"MATCH (a:Entity {{id:'{n}'}}),(t:Entity {{id:'target'}}) "
                           "CREATE (a)-[:OWNS {since:'2005-03-01', until:'2012-06-30', source_id:'psc'}]->(t)")
        _migrate_entity_edges("dead", "keep")
        assert len(_owns(db, "keep", "target")) == 1

    def test_incoming_ownership_and_roles_keep_their_periods_too(self, two_companies):
        from app.scraper.maintenance import _migrate_entity_edges
        db = two_companies
        db.run_command("CREATE (:Person {id:'ceo', full_name:'A CEO'})")
        db.run_command("MATCH (o:Entity {id:'owner'}),(k:Entity {id:'keep'}) "
                       "CREATE (o)-[:OWNS {since:'2019-01-01', source_id:'x'}]->(k)")
        db.run_command("MATCH (o:Entity {id:'owner'}),(d:Entity {id:'dead'}) "
                       "CREATE (o)-[:OWNS {since:'2001-01-01', until:'2010-01-01', source_id:'x'}]->(d)")
        db.run_command("MATCH (p:Person {id:'ceo'}),(k:Entity {id:'keep'}) "
                       "CREATE (p)-[:HAS_ROLE {role:'CEO', since:'2018-01-01'}]->(k)")
        db.run_command("MATCH (p:Person {id:'ceo'}),(d:Entity {id:'dead'}) "
                       "CREATE (p)-[:HAS_ROLE {role:'CEO', since:'2003-01-01', until:'2009-01-01'}]->(d)")
        db.run_command("MATCH (p:Person {id:'ceo'}),(d:Entity {id:'dead'}) "
                       "CREATE (p)-[:HAS_ROLE {role:'Director', since:'2011-01-01'}]->(d)")
        _migrate_entity_edges("dead", "keep")
        assert [(e.get("since"), e.get("until")) for e in _owns(db, "owner", "keep")] == [
            ("2001-01-01", "2010-01-01"), ("2019-01-01", None)]
        assert [(e["role"], e.get("since"), e.get("until")) for e in _roles(db, "ceo", "keep")] == [
            ("CEO", "2003-01-01", "2009-01-01"), ("Director", "2011-01-01", None), ("CEO", "2018-01-01", None)]

    def test_two_current_roles_fold_to_the_earliest_start(self, two_companies):
        from app.scraper.maintenance import _migrate_entity_edges
        db = two_companies
        db.run_command("CREATE (:Person {id:'ceo', full_name:'A CEO'})")
        db.run_command("MATCH (p:Person {id:'ceo'}),(k:Entity {id:'keep'}) "
                       "CREATE (p)-[:HAS_ROLE {role:'CEO', since:'2018-01-01'}]->(k)")
        db.run_command("MATCH (p:Person {id:'ceo'}),(d:Entity {id:'dead'}) "
                       "CREATE (p)-[:HAS_ROLE {role:'CEO', since:'2012-01-01'}]->(d)")
        _migrate_entity_edges("dead", "keep")
        roles = _roles(db, "ceo", "keep")
        assert len(roles) == 1 and roles[0]["since"] == "2012-01-01"


class TestPersonMerges:
    @pytest.fixture
    def people(self, it_db):
        it_db.run_command("CREATE (:Person {id:'keep', full_name:'Larry Page', description:''})")
        it_db.run_command("CREATE (:Person {id:'dup', full_name:'Page Lawrence', description:''})")
        it_db.run_command("CREATE (:Entity {id:'co', name:'Co', type:'company'})")
        it_db.run_command("MATCH (p:Person {id:'keep'}),(e:Entity {id:'co'}) "
                          "CREATE (p)-[:OWNS {since:'2020-01-01', stake_percent:6, source_id:'sec'}]->(e)")
        it_db.run_command("MATCH (p:Person {id:'dup'}),(e:Entity {id:'co'}) "
                          "CREATE (p)-[:OWNS {since:'2004-01-01', until:'2015-01-01', stake_percent:9, source_id:'sec'}]->(e)")
        return it_db

    def test_the_manual_merge_keeps_both_periods_and_does_not_close_the_current_one(self, people):
        # The MERGE … SET COALESCE this replaced folded the dup's ended holding
        # into keep's current one and set its `until`: a live holder, closed.
        from app.routers.persons import merge_person_records
        merge_person_records("keep", "dup")
        assert [(e.get("since"), e.get("until")) for e in _owns(people, "keep", "co")] == [
            ("2004-01-01", "2015-01-01"), ("2020-01-01", None)]

    def test_the_auto_merge_keeps_both_periods(self, people):
        from app.scraper.maintenance import _migrate_person_edges
        _migrate_person_edges("dup", "keep")
        assert [(e.get("since"), e.get("until")) for e in _owns(people, "keep", "co")] == [
            ("2004-01-01", "2015-01-01"), ("2020-01-01", None)]


def test_the_edge_dedup_folds_one_sources_duplicates_too(it_db):
    # Two active edges of ONE source (a re-import beside the old edge): the
    # loser's earlier start used to go with it.
    from app.scraper.maintenance import deduplicate_owns_edges
    it_db.run_command("CREATE (:Entity {id:'a', name:'A', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'b', name:'B', type:'company'})")
    for since in ("2016-04-06", "2009-02-01"):
        it_db.run_command("MATCH (a:Entity {id:'a'}),(b:Entity {id:'b'}) "
                          f"CREATE (a)-[:OWNS {{since:'{since}', source_id:'gleif', stake_percent:100}}]->(b)")
    deduplicate_owns_edges()
    edges = _owns(it_db, "a", "b")
    assert len(edges) == 1 and edges[0]["since"] == "2009-02-01"


def test_retiring_a_bloc_edge_spares_history_and_other_filings(it_db):
    from app.scraper.sec_writer import _retire_superseded_bloc_edge
    it_db.run_command("CREATE (:Entity {id:'brc', name:'BRC', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'abi', name:'AB InBev', type:'company'})")
    for props in ("{source_id:'sec', filing_type:'13D/A'}",                         # the old bloc row: retired
                  "{source_id:'sec', filing_type:'13D/A', until:'2018-01-01'}",     # history: kept
                  "{source_id:'sec', filing_type:'13F'}"):                          # a below-floor 13F row: kept
        it_db.run_command(f"MATCH (a:Entity {{id:'brc'}}),(b:Entity {{id:'abi'}}) CREATE (a)-[:OWNS {props}]->(b)")
    _retire_superseded_bloc_edge("brc", "abi", "sec", "Entity")
    left = it_db.run_sql("SELECT filing_type, until FROM OWNS")
    assert sorted((r.get("filing_type"), r.get("until")) for r in left) == [
        ("13D/A", "2018-01-01"), ("13F", None)]
