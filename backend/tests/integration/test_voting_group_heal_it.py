"""Real-ArcadeDB: a database written before 2026-10-26 — its 13D filing groups
Entities of type `voting_group`, membership RELATED_TO {group_member} — moves
to the VotingGroup vertex type and MEMBER_OF, keeping every property, every
holding and every claim (app.voting_groups.heal_voting_group_type).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed_old_shape(it_db):
    # the vocabulary before the change let an Entity be a voting group
    it_db.run_sql("ALTER PROPERTY Entity.type REGEXP '^(company|voting_group)$'")
    it_db.run_command("CREATE (:Entity {id:'abi', name:'Anheuser-Busch InBev', name_normalized:'anheuser busch inbev', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'stichting', name:'Stichting AB InBev', name_normalized:'stichting ab inbev', type:'company'})")
    it_db.run_command("CREATE (:Person {id:'lemann', full_name:'Jorge Paulo Lemann'})")
    it_db.run_command("CREATE (:Entity {id:'grp', name:'Voting group · 2 parties', name_normalized:'voting group anheuser busch inbev', "
                      "search_text:'Voting group · 2 parties Anheuser-Busch InBev', type:'voting_group', "
                      "member_keys:['|stichting ab inbev', '|jorge paulo lemann'], source_id:'sec', "
                      "description:'Parties acting together…', created_at:'2026-01-01T00:00:00+00:00', "
                      "last_scraped_at:'2026-02-01T00:00:00+00:00'})")
    for mid, label in (("stichting", "Entity"), ("lemann", "Person")):
        it_db.run_command(f"MATCH (m:{label} {{id:'{mid}'}}), (g:Entity {{id:'grp'}}) "
                          "CREATE (m)-[:RELATED_TO {relation:'group_member', source_id:'sec', "
                          "last_scraped_at:'2026-02-01T00:00:00+00:00'}]->(g)")
    it_db.run_command("MATCH (g:Entity {id:'grp'}), (b:Entity {id:'abi'}) "
                      "CREATE (g)-[:OWNS {source_id:'sec', stake_percent:null, voting_power_pct:52.3, "
                      "voting_shares:1033081237, filing_type:'13D/A', read_from:'form', since:'2025-02-07', "
                      "source_url:'https://sec.example.test/13d', until:null}]->(b)")
    it_db.run_command("CREATE (:Claim {claim_key:'k', kind:'owns', from_id:'grp', to_id:'abi', source_id:'sec', voting_power_pct:52.3})")


def test_an_older_database_moves_its_groups_to_their_own_type(it_db):
    from app.voting_groups import heal_voting_group_type
    from app.routers.search import get_full_profile
    _seed_old_shape(it_db)

    dry = heal_voting_group_type(apply=False)
    assert (dry["groups"], dry["members"], dry["holdings"]) == (1, 2, 1)
    assert dry["names"] == ["Voting group · 2 parties (2 parties, 1 holdings)"]
    assert it_db.run_sql("SELECT count(*) AS n FROM VotingGroup")[0]["n"] == 0

    res = heal_voting_group_type(apply=True)
    assert res["groups"] == 1
    g = it_db.run_sql("SELECT FROM VotingGroup")[0]
    assert g["id"] == "grp" and g["type"] == "voting_group" and g["member_keys"] == ["|stichting ab inbev", "|jorge paulo lemann"]
    assert g["search_text"].startswith("Voting group") and g["created_at"] == "2026-01-01T00:00:00+00:00"
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity WHERE id = 'grp'")[0]["n"] == 0
    members = {r["m"]: r for r in it_db.run_command(
        "MATCH (m)-[r:MEMBER_OF]->(g:VotingGroup {id:'grp'}) RETURN m.id AS m, labels(m)[0] AS l, r.source_id AS s")}
    assert set(members) == {"stichting", "lemann"} and members["lemann"]["l"] == "Person"
    assert all(m["s"] == "sec" for m in members.values())
    assert it_db.run_sql("SELECT count(*) AS n FROM RELATED_TO")[0]["n"] == 0
    held = it_db.run_command("MATCH (g:VotingGroup {id:'grp'})-[r:OWNS]->(b:Entity {id:'abi'}) "
                             "RETURN r.voting_power_pct AS v, r.voting_shares AS vs, r.filing_type AS ft, r.since AS s")
    assert [(h["v"], h["vs"], h["ft"], h["s"]) for h in held] == [(52.3, 1033081237, "13D/A", "2025-02-07")]
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 1
    # the claim is keyed by id and never moved
    assert it_db.run_sql("SELECT from_id FROM Claim WHERE claim_key = 'k'")[0]["from_id"] == "grp"
    # the group opens as before, and the company lists it among its owners
    prof = get_full_profile("grp")
    assert prof["entity"]["type"] == "voting_group"
    assert {m["party"]["id"] for m in prof["group_members"]} == {"stichting", "lemann"}
    assert [o["owner"]["id"] for o in get_full_profile("abi")["owners"]] == ["grp"]
    # idempotent
    assert heal_voting_group_type(apply=True)["groups"] == 0
