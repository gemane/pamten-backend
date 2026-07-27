"""
Real-ArcadeDB tests for ownership data-quality: self-loop (A owns A) exclusion
from the full profile, circular-ownership (A↔B) surfacing, and the maintenance
detectors. The Cypher self-join + @out/@in checks can't run on the mocked suite.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _e(it_db, eid, name):
    it_db.run_command(f"CREATE (:Entity {{id:'{eid}', name:'{name}', type:'company'}})")


def _owns(it_db, a, b, pct=None):
    stake = "null" if pct is None else str(pct)
    it_db.run_command(f"MATCH (a:Entity{{id:'{a}'}}),(b:Entity{{id:'{b}'}}) "
                      f"CREATE (a)-[:OWNS{{stake_percent:{stake}, until:null}}]->(b)")


def test_self_loop_excluded_from_owners_and_summary(it_db):
    from app.routers.search import get_full_profile
    _e(it_db, "co", "Public Co")
    _e(it_db, "inv", "Investor")
    _owns(it_db, "co", "co", 20)      # self-loop (treasury/error)
    _owns(it_db, "inv", "co", 30)     # real owner

    prof = get_full_profile("co")
    assert [o["owner"]["id"] for o in prof["owners"]] == ["inv"]   # self-loop dropped
    assert prof["ownership"]["disclosed_pct"] == 30                # not 50
    assert prof["ownership"]["free_float_pct"] == 70
    assert prof["cross_holdings"] == []


def test_circular_ownership_surfaced(it_db):
    from app.routers.search import get_full_profile
    _e(it_db, "a", "Alpha AG")
    _e(it_db, "b", "Beta AG")
    _owns(it_db, "a", "b", 40)        # A owns B
    _owns(it_db, "b", "a", 25)        # B owns A → reciprocal

    prof_a = get_full_profile("a")
    # B is both an owner of A and a subsidiary of A → flagged as cross-holding.
    assert [c["name"] for c in prof_a["cross_holdings"]] == ["Beta AG"]


def test_maintenance_detectors(it_db):
    from app.scraper import maintenance
    _e(it_db, "a", "Alpha AG")
    _e(it_db, "b", "Beta AG")
    _owns(it_db, "a", "a")            # self-loop
    _owns(it_db, "a", "b")
    _owns(it_db, "b", "a")            # cross-holding a↔b

    assert maintenance.count_self_loop_owns()["self_loops"] == 1
    pairs = maintenance.find_cross_holdings()
    assert len(pairs) == 1                                   # reported once (a.id < b.id)
    assert {pairs[0]["a_id"], pairs[0]["b_id"]} == {"a", "b"}
