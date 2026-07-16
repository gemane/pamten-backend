"""
Real-ArcadeDB integration test for the person full-profile endpoint: a person's
positions (HAS_ROLE → entity) and ownerships (OWNS → entity) must surface, and
past edges (until set) must be excluded — the collect(DISTINCT {..}) map shape
and the until-filter can only be validated against a real ArcadeDB.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_person_profile_surfaces_positions_and_holdings(it_db):
    from app.routers.search import get_person_profile

    it_db.run_command("CREATE (:Person {id: 'musk', full_name: 'Elon Musk'})")
    it_db.run_command("CREATE (:Entity {id: 'spacex', name: 'SpaceX', type: 'company'})")
    it_db.run_command("CREATE (:Entity {id: 'tesla',  name: 'Tesla',  type: 'company'})")
    # Current CEO + owner of SpaceX; a FORMER CEO of Tesla (until set → excluded).
    it_db.run_command("MATCH (p:Person {id:'musk'}), (e:Entity {id:'spacex'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'musk'}), (e:Entity {id:'spacex'}) "
                      "CREATE (p)-[:OWNS {stake_percent: 42}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'musk'}), (e:Entity {id:'tesla'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO', until:'2020-01-01'}]->(e)")

    prof = get_person_profile("musk")
    assert prof["person"]["full_name"] == "Elon Musk"

    positions = {(x["entity"]["name"], x["role"]["role"]) for x in prof["positions"]}
    assert ("SpaceX", "CEO") in positions
    assert ("Tesla", "CEO") not in positions        # past role (until set) excluded

    holdings = {(x["entity"]["name"], x["relationship"]["stake_percent"]) for x in prof["holdings"]}
    assert ("SpaceX", 42) in holdings


def test_person_profile_empty_when_no_edges(it_db):
    from app.routers.search import get_person_profile

    it_db.run_command("CREATE (:Person {id: 'lonely', full_name: 'No Body'})")
    prof = get_person_profile("lonely")
    assert prof["positions"] == []
    assert prof["holdings"] == []


def test_person_profile_404_for_unknown(it_db):
    from app.routers.search import get_person_profile
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        get_person_profile("nobody")
    assert exc.value.status_code == 404
