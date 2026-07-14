"""
Real-ArcadeDB integration test for the dual-listed relationship: exercises the
MERGE write endpoint's Cypher and the full-profile's undirected match + collect,
which the mocked unit tests can't validate.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_dual_listed_edge_surfaces_in_full_profile(it_db):
    from app.routers.relationships import create_dual_listed
    from app.routers.search import get_full_profile
    from app.models.relationship import DualListedCreate

    it_db.run_command("CREATE (:Entity {id: 'unilever-plc', name: 'Unilever PLC', type: 'company'})")
    it_db.run_command("CREATE (:Entity {id: 'unilever-nv', name: 'Unilever NV', type: 'company'})")

    # Create the (symmetric, non-ownership) dual-listed link via the endpoint fn.
    create_dual_listed(
        DualListedCreate(entity_a_id="unilever-plc", entity_b_id="unilever-nv",
                         source_url="https://www.wikidata.org/wiki/Q157062"),
        _={"role": "contributor"},
    )

    # The undirected match must surface the pair from EITHER side.
    prof_a = get_full_profile("unilever-plc")
    assert [d["name"] for d in prof_a["dual_listed"]] == ["Unilever NV"]

    prof_b = get_full_profile("unilever-nv")
    assert [d["name"] for d in prof_b["dual_listed"]] == ["Unilever PLC"]


def test_full_profile_dual_listed_empty_when_none(it_db):
    from app.routers.search import get_full_profile

    it_db.run_command("CREATE (:Entity {id: 'solo', name: 'Solo Co', type: 'company'})")
    assert get_full_profile("solo")["dual_listed"] == []
