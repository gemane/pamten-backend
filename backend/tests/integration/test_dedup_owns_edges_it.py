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


@pytest.mark.parametrize("order", [("direct", "indirect"), ("indirect", "direct")])
def test_dedup_keeps_direct_over_indirect_whichever_came_first(it_db, order):
    """Both twins carry a marker, so 'has a marker' cannot separate them and the
    winner used to be whichever @rid came first.

    Keeping the indirect one labels a direct holding as indirect, and the graph
    then hides it as an ownership shortcut — the company vanishes despite a
    perfectly good direct edge. The RR importer no longer creates this pair, but
    a BODS/RR overlap still can.
    """
    from app.scraper import maintenance
    for x in ("p", "c"):
        it_db.run_command(f"CREATE (:Entity {{id:'{x}'}})")
    for marker in order:
        it_db.run_command(
            f"MATCH (p:Entity{{id:'p'}}),(c:Entity{{id:'c'}}) "
            f"CREATE (p)-[:OWNS{{until:null, direct_or_indirect:'{marker}'}}]->(c)")

    maintenance.deduplicate_owns_edges()

    rows = it_db.run_query(
        "MATCH (p:Entity{id:'p'})-[r:OWNS]->(c:Entity{id:'c'}) RETURN r.direct_or_indirect AS d")
    assert [r["d"] for r in rows] == ["direct"]


def test_collection_walks_owners_of_both_kinds_page_by_page(it_db, monkeypatch):
    """The collector pages OWNERS by their id index and expands adjacency — no
    scan of OWNS. With a page of 2 it must cross page boundaries for Entity and
    Person alike, skip owners without edges, and survive an id with a quote."""
    from app.scraper import maintenance
    monkeypatch.setattr(maintenance, "_OWNER_PAGE", 2)
    for x in ("e1", "e2", "e3", "o'brien", "t"):
        it_db.run_command(f"CREATE (:Entity {{id:\"{x}\"}})")
    for p in ("p1", "p2", "p3"):
        it_db.run_command(f"CREATE (:Person {{id:'{p}', full_name:'{p}'}})")
    # e1→t twice, o'brien→t twice, p2→t twice, p3→t once; e2, e3, p1 own nothing
    for owner, label, n in (("e1", "Entity", 2), ("o'brien", "Entity", 2), ("p2", "Person", 2), ("p3", "Person", 1)):
        for _ in range(n):
            it_db.run_command(f"MATCH (a:{label} {{id:\"{owner}\"}}),(t:Entity {{id:'t'}}) "
                              "CREATE (a)-[:OWNS{until:null}]->(t)")
    c = maintenance.count_duplicate_owns_edges()
    assert c["active_edges"] == 7 and c["distinct_pairs"] == 4
    assert c["duplicate_pairs"] == 3 and c["redundant_edges"] == 3
    res = maintenance.deduplicate_owns_edges()
    assert res["duplicates_removed"] == 3
    assert maintenance.count_duplicate_owns_edges()["redundant_edges"] == 0


def test_every_owner_page_is_an_index_read_including_the_first(it_db):
    """On the sizing box the FIRST page — `ORDER BY id LIMIT n` with no
    predicate — was planned as a full scan of the 6.4 GB Entity type and hit the
    60 s timeout before one page came back; only the later pages, which carry
    `WHERE id > last`, were index reads. Every page must be a two-sided range
    from the start (see app/db/paging.py). Checked against the real planner,
    since only it decides."""
    from app.db import paging
    from app.scraper import maintenance

    issued: list[str] = []
    real_run_sql = paging.run_sql

    def spy(cmd, *a, **k):
        issued.append(cmd)
        return real_run_sql(cmd, *a, **k)

    it_db.run_command("CREATE (:Entity {id:'a'})")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(paging, "run_sql", spy)
        maintenance.count_duplicate_owns_edges()
    pages = [c for c in issued if c.startswith("SELECT id, @rid AS rid FROM")]
    assert pages and all("WHERE id > '" in c and "AND id < '" in c for c in pages), pages
    for page in pages:
        plan = it_db.explain_sql(page)
        assert "FETCH FROM INDEX" in plan, (page, plan)
        assert "FETCH FROM TYPE" not in plan, (page, plan)


def test_closed_twins_fold_and_distinct_intervals_stay(it_db):
    """A bulk re-import recreates every ceased appointment beside its old copy
    (`CREATE EDGE` is not idempotent): eight of 29 PSC edges on dev were doubled
    after the 2026-10-18 subset re-run because the dedup folded active twins
    only. Two closed edges on the SAME day are one fact; two that ended on
    different days are two holdings and must both stay."""
    from app.scraper import maintenance
    for x in ("p", "c", "d"):
        it_db.run_command(f"CREATE (:Entity {{id:'{x}'}})")
    # p→c: the current holding, plus a 2016–2020 appointment written twice —
    # the older copy with the earlier start that the fold must keep
    it_db.run_command("MATCH (p:Entity{id:'p'}),(c:Entity{id:'c'}) "
                      "CREATE (p)-[:OWNS{until:null, stake_percent:75, source_id:'psc'}]->(c)")
    for since in ("2016-04-06", "2017-01-01"):
        it_db.run_command(
            "MATCH (p:Entity{id:'p'}),(c:Entity{id:'c'}) "
            f"CREATE (p)-[:OWNS{{since:'{since}', until:'2020-05-01', stake_percent:50, "
            "source_id:'psc', psc_self_link:'/company/c/psc/1'}]->(c)")
    # p→d: held twice, ended on different days — history, not a duplicate
    for since, until in (("2005-01-01", "2012-01-01"), ("2018-01-01", "2022-01-01")):
        it_db.run_command("MATCH (p:Entity{id:'p'}),(d:Entity{id:'d'}) "
                          f"CREATE (p)-[:OWNS{{since:'{since}', until:'{until}', source_id:'psc'}}]->(d)")

    c = maintenance.count_duplicate_owns_edges()
    assert (c["active_edges"], c["distinct_pairs"]) == (1, 1)
    assert (c["duplicate_pairs"], c["redundant_edges"], c["closed_duplicate_pairs"]) == (1, 1, 1)

    res = maintenance.deduplicate_owns_edges()
    assert (res["duplicates_removed"], res["pairs_cleaned"], res["closed_pairs_cleaned"]) == (1, 1, 1)
    assert res["survivors_folded"] == 1          # the later start gave way to 2016-04-06

    pc = it_db.run_query("MATCH (p:Entity{id:'p'})-[r:OWNS]->(c:Entity{id:'c'}) "
                         "RETURN r.since AS s, r.until AS u ORDER BY r.until")
    assert [(r["s"], r["u"]) for r in pc] == [("2016-04-06", "2020-05-01"), (None, None)]
    pd = it_db.run_query("MATCH (p:Entity{id:'p'})-[r:OWNS]->(d:Entity{id:'d'}) "
                         "RETURN r.until AS u ORDER BY r.until")
    assert [r["u"] for r in pd] == ["2012-01-01", "2022-01-01"]
    assert maintenance.count_duplicate_owns_edges()["redundant_edges"] == 0
