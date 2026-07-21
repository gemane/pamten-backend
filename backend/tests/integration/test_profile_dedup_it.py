"""
Real-ArcadeDB test: the entity full-profile must not show a subsidiary/owner
twice when the graph has duplicate OWNS edges (a re-imported BODS dump creates a
second identical edge — CREATE EDGE isn't idempotent). collect(DISTINCT …) does
NOT collapse them because the two edges are distinct objects, so the profile
dedupes by node id.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_full_profile_dedupes_duplicate_owns_edges(it_db):
    from app.routers.search import get_full_profile

    it_db.run_command("CREATE (:Entity {id: 'parent', name: 'Parent Co', type: 'company'})")
    it_db.run_command("CREATE (:Entity {id: 'sub', name: 'Sub Co', type: 'company'})")
    # Two identical active OWNS edges parent → sub (the duplicate-import scenario).
    for _ in range(2):
        it_db.run_command(
            "MATCH (p:Entity {id: 'parent'}), (s:Entity {id: 'sub'}) "
            "CREATE (p)-[:OWNS {stake_percent: 60, until: null}]->(s)"
        )

    prof = get_full_profile("parent")
    subs = prof["subsidiaries"]
    assert len(subs) == 1                       # one row, not two
    assert subs[0]["entity"]["id"] == "sub"


def test_full_profile_keeps_larger_stake_on_duplicate(it_db):
    from app.routers.search import get_full_profile

    it_db.run_command("CREATE (:Entity {id: 'p2', name: 'P2', type: 'company'})")
    it_db.run_command("CREATE (:Entity {id: 's2', name: 'S2', type: 'company'})")
    it_db.run_command("MATCH (p:Entity {id:'p2'}),(s:Entity {id:'s2'}) "
                      "CREATE (p)-[:OWNS {stake_percent: 25, until: null}]->(s)")
    it_db.run_command("MATCH (p:Entity {id:'p2'}),(s:Entity {id:'s2'}) "
                      "CREATE (p)-[:OWNS {stake_percent: 80, until: null}]->(s)")

    subs = get_full_profile("p2")["subsidiaries"]
    assert len(subs) == 1
    assert subs[0]["relationship"]["stake_percent"] == 80   # keeps the larger stake
