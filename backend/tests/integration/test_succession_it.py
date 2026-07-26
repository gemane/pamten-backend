"""
Real-ArcadeDB integration test for company succession (SUCCEEDED_BY): exercises
the directed edge type + the full-profile's forward/backward match + collect,
which the mocked unit tests can't validate. Twitter → X Corp. is the fixture.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_succession_surfaces_on_both_sides_of_full_profile(it_db):
    from app.routers.search import get_full_profile

    it_db.run_command("CREATE (:Entity {id: 'twitter', name: 'Twitter', type: 'company'})")
    it_db.run_command("CREATE (:Entity {id: 'x-corp',  name: 'X Corp.', type: 'company'})")
    # Directed predecessor → successor.
    it_db.run_command(
        "MATCH (p:Entity {id: 'twitter'}), (s:Entity {id: 'x-corp'}) "
        "CREATE (p)-[:SUCCEEDED_BY {source_id: 'src', source_url: 'https://www.wikidata.org/wiki/Q1390577'}]->(s)"
    )

    # Predecessor's panel shows who it was 'succeeded_by'.
    pred = get_full_profile("twitter")
    assert [e["name"] for e in pred["succeeded_by"]] == ["X Corp."]
    assert pred["replaces"] == []

    # Successor's panel shows who it 'replaces'.
    succ = get_full_profile("x-corp")
    assert [e["name"] for e in succ["replaces"]] == ["Twitter"]
    assert succ["succeeded_by"] == []


def test_succession_empty_when_none(it_db):
    from app.routers.search import get_full_profile

    it_db.run_command("CREATE (:Entity {id: 'solo', name: 'Solo Co', type: 'company'})")
    prof = get_full_profile("solo")
    assert prof["succeeded_by"] == []
    assert prof["replaces"] == []
