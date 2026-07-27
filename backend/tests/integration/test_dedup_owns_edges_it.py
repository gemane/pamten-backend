"""
Real-ArcadeDB test for OWNS edge dedup: counting and collapsing duplicate active
OWNS edges between the same (owner, target) pair — done by paging + Python-side
grouping (a global GROUP BY over the edges blows the query heap) and deleting the
redundant edges by @rid (preserving the kept edge's properties).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed(it_db):
    for x in ("a", "b", "c"):
        it_db.run_command(f"CREATE (:Entity {{id:'{x}'}})")
    # a→b: three edges (two redundant); keep the largest stake (50)
    for st in (10, 50, 30):
        it_db.run_command(
            f"MATCH (a:Entity{{id:'a'}}),(b:Entity{{id:'b'}}) "
            f"CREATE (a)-[:OWNS{{stake_percent:{st}, until:null}}]->(b)")
    # a→c: single edge (not a duplicate)
    it_db.run_command("MATCH (a:Entity{id:'a'}),(c:Entity{id:'c'}) "
                      "CREATE (a)-[:OWNS{stake_percent:5, until:null}]->(c)")
    # b→c: two edges with no stake (one redundant)
    for _ in range(2):
        it_db.run_command("MATCH (b:Entity{id:'b'}),(c:Entity{id:'c'}) "
                          "CREATE (b)-[:OWNS{until:null}]->(c)")


def test_count_reports_duplicate_owns_edges(it_db):
    from app.scraper import maintenance
    _seed(it_db)
    c = maintenance.count_duplicate_owns_edges()
    assert c["active_edges"] == 6
    assert c["distinct_pairs"] == 3
    assert c["duplicate_pairs"] == 2       # a→b and b→c
    assert c["redundant_edges"] == 3       # 2 from a→b, 1 from b→c


def test_dedup_collapses_and_keeps_largest_stake(it_db):
    from app.scraper import maintenance
    _seed(it_db)

    res = maintenance.deduplicate_owns_edges()
    assert res["duplicates_removed"] == 3
    assert res["pairs_cleaned"] == 2

    # one edge per pair remains, and a→b kept the stake-50 edge
    assert maintenance.count_duplicate_owns_edges()["redundant_edges"] == 0
    ab = it_db.run_query(
        "MATCH (a:Entity {id:'a'})-[r:OWNS]->(b:Entity {id:'b'}) RETURN r.stake_percent AS s")
    assert [r["s"] for r in ab] == [50]


def test_dedup_keeps_direct_indirect_flagged_edge(it_db):
    """RR-CDF vs BODS overlap: two stakeless edges for one pair, one carrying the
    direct/indirect marker — the flagged (RR) edge must survive."""
    from app.scraper import maintenance
    for x in ("p", "c"):
        it_db.run_command(f"CREATE (:Entity {{id:'{x}'}})")
    it_db.run_command("MATCH (p:Entity{id:'p'}),(c:Entity{id:'c'}) "
                      "CREATE (p)-[:OWNS{until:null}]->(c)")                      # BODS: flagless
    it_db.run_command("MATCH (p:Entity{id:'p'}),(c:Entity{id:'c'}) "
                      "CREATE (p)-[:OWNS{until:null, direct_or_indirect:'direct'}]->(c)")  # RR

    res = maintenance.deduplicate_owns_edges()
    assert res["duplicates_removed"] == 1
    rows = it_db.run_query(
        "MATCH (p:Entity{id:'p'})-[r:OWNS]->(c:Entity{id:'c'}) RETURN r.direct_or_indirect AS d")
    assert [r["d"] for r in rows] == ["direct"]      # flagged edge survived
