"""
Real-ArcadeDB integration test for the person full-profile endpoint: a person's
positions (HAS_ROLE → entity) and ownerships (OWNS → entity) must surface,
*including* the ones that have ended — the profile feeds a timeline, and a
career is mostly ended roles. The collect(DISTINCT {..}) map shape can only be
validated against a real ArcadeDB.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_person_profile_surfaces_positions_and_holdings(it_db):
    from app.routers.search import get_person_profile

    it_db.run_command("CREATE (:Person {id: 'musk', full_name: 'Elon Musk'})")
    it_db.run_command("CREATE (:Entity {id: 'spacex', name: 'SpaceX', type: 'company'})")
    it_db.run_command("CREATE (:Entity {id: 'tesla',  name: 'Tesla',  type: 'company'})")
    # Two CEO tenures at SpaceX with different `since` — two real spells, kept
    # apart. Plus an owner edge, and a role at Tesla that has ended, which the
    # profile used to drop.
    it_db.run_command("MATCH (p:Person {id:'musk'}), (e:Entity {id:'spacex'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO', since:'2002-03-14'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'musk'}), (e:Entity {id:'spacex'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO', since:'2018-01-01'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'musk'}), (e:Entity {id:'spacex'}) "
                      "CREATE (p)-[:OWNS {stake_percent: 42}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'musk'}), (e:Entity {id:'tesla'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO', until:'2020-01-01'}]->(e)")

    prof = get_person_profile("musk")
    assert prof["person"]["full_name"] == "Elon Musk"

    # Both spells at SpaceX survive: same company, same title, different start.
    spacex_ceo = sorted(x["role"]["since"] for x in prof["positions"]
                        if x["entity"]["name"] == "SpaceX" and x["role"]["role"] == "CEO")
    assert spacex_ceo == ["2002-03-14", "2018-01-01"]

    positions = {(x["entity"]["name"], x["role"]["role"]) for x in prof["positions"]}
    assert ("Tesla", "CEO") in positions            # ended, and still part of the record

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
