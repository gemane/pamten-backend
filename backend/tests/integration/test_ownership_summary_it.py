"""
Real-ArcadeDB test for the computed ownership breakdown (free-float residual +
>100% flag) surfaced by /entity/{id}/full-profile — confirms stake_percent flows
from real OWNS edges through _ownership_summary.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _entity(it_db, eid, name):
    it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{name}', type: 'company'}})")


def _owns(it_db, owner, owned, pct):
    it_db.run_command(
        f"MATCH (a:Entity {{id: '{owner}'}}), (b:Entity {{id: '{owned}'}}) "
        f"CREATE (a)-[:OWNS {{stake_percent: {pct}, until: null}}]->(b)"
    )


def test_free_float_is_the_residual(it_db):
    from app.routers.search import get_full_profile
    _entity(it_db, "co", "Public Co")
    _entity(it_db, "blackrock", "BlackRock")
    _entity(it_db, "vanguard", "Vanguard")
    _owns(it_db, "blackrock", "co", 7.0)
    _owns(it_db, "vanguard", "co", 5.0)

    own = get_full_profile("co")["ownership"]
    assert own["disclosed_pct"] == 12.0
    assert own["free_float_pct"] == 88.0
    assert own["exceeds_100"] is False


def test_flags_when_disclosed_exceeds_100(it_db):
    from app.routers.search import get_full_profile
    _entity(it_db, "co", "Conflicted Co")
    _entity(it_db, "a", "Holder A")
    _entity(it_db, "b", "Holder B")
    _owns(it_db, "a", "co", 80.0)
    _owns(it_db, "b", "co", 63.0)

    own = get_full_profile("co")["ownership"]
    assert own["exceeds_100"] is True
    assert own["free_float_pct"] is None


def test_no_free_float_when_an_owner_stake_unknown(it_db):
    from app.routers.search import get_full_profile
    _entity(it_db, "co", "Private Co")
    _entity(it_db, "parent", "Parent")
    it_db.run_command(
        "MATCH (a:Entity {id: 'parent'}), (b:Entity {id: 'co'}) "
        "CREATE (a)-[:OWNS {stake_percent: null, until: null}]->(b)"
    )
    own = get_full_profile("co")["ownership"]
    assert own["unknown_owners"] == 1
    assert own["free_float_pct"] is None
