"""Real-ArcadeDB: a node a scrape creates is keyed by its hard id
(app.node_ids, 2026-10-26), a rescrape finds the keyed node, and the heal
renames an older database's uuid nodes with their claims and a forwarding row.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_the_writers_key_a_new_node_and_find_it_again(it_db):
    from app.node_ids import is_uuid
    from app.scraper.runner import _upsert_entity, _upsert_person
    from app.scraper.graph_writer import _upsert_entity_by_name, _upsert_person_by_name
    apple = _upsert_entity("Apple Inc.", "company", "US", None, None, None, "Q312", source_id="wikidata",
                           sec_cik="0000320193")
    assert apple == "wd:Q312"                                        # the item outranks the CIK
    assert _upsert_entity("Apple Inc.", "company", "US", None, None, None, "Q312", source_id="wikidata") == "wd:Q312"
    assert _upsert_entity_by_name("APPLE INC", cik="0000320193", source_id="sec-edgar") == "wd:Q312"   # the CIK finds it
    brc = _upsert_entity_by_name("BRC S.a R.L.", cik="0001301486", source_id="sec-edgar")
    assert brc == "sec:0001301486"
    assert _upsert_entity_by_name("BRC S.A R.L.", cik="0001301486", source_id="sec-edgar") == brc
    sub = _upsert_entity_by_name("Sub Inc", source_id="sec-edgar")
    assert is_uuid(sub)                                               # name-only: nothing to key on
    assert _upsert_person("Tim Cook", None, None, "Q265852", source_id="wikidata") == "wd:Q265852"
    assert _upsert_person_by_name("COOK TIMOTHY D", source_id="sec-edgar", sec_cik="0001214156") == "sec:0001214156"
    assert is_uuid(_upsert_person_by_name("Nobody Known", source_id="sec-edgar"))
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 3


def test_the_heal_renames_an_older_database_and_old_links_keep_opening(it_db):
    from app.claims import KIND_OWNS, owns_claim_key, record_claim
    from app.node_ids import heal_keyed_ids
    from app.routers.search import get_full_profile
    old = "c75a2d35-c76f-4c91-a813-d8b001203bea"
    it_db.run_command("CREATE (:Entity {id:$id, name:'Apple Inc.', name_normalized:'apple', wikidata_id:'Q312', sec_cik:'0000320193'})",
                      {"id": old})
    it_db.run_command("CREATE (:Entity {id:'22222222-2222-4222-8222-222222222222', name:'Sub Inc', name_normalized:'sub inc'})")
    it_db.run_command("CREATE (:Entity {id:'wd:Q1', name:'Taken', name_normalized:'taken'})")
    it_db.run_command("CREATE (:Entity {id:'33333333-3333-4333-8333-333333333333', name:'Twin', name_normalized:'twin', wikidata_id:'Q1'})")
    it_db.run_command("CREATE (:Person {id:'44444444-4444-4444-8444-444444444444', full_name:'Tim Cook', wikidata_id:'Q265852'})")
    it_db.run_command("MATCH (p:Person {id:'44444444-4444-4444-8444-444444444444'}), (e:Entity {id:$id}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO', source_id:'wikidata'}]->(e)", {"id": old})
    record_claim(kind=KIND_OWNS, from_id="22222222-2222-4222-8222-222222222222", to_id=old, source_id="sec-edgar",
                 stake_percent=5.0, filing_type="13G")

    dry = heal_keyed_ids(apply=False)
    assert (dry["Entity"]["moved"], dry["Entity"]["unkeyed"], dry["Person"]["moved"]) == (2, 1, 1)
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity WHERE id = :id", {"id": old})[0]["n"] == 1

    res = heal_keyed_ids(apply=True)
    assert (res["Entity"]["moved"], res["Entity"]["taken"], res["Person"]["moved"]) == (1, 1, 1)
    ids = {r["id"] for r in it_db.run_sql("SELECT id FROM Entity")}
    assert ids == {"wd:Q312", "22222222-2222-4222-8222-222222222222", "wd:Q1", "33333333-3333-4333-8333-333333333333"}
    assert it_db.run_sql("SELECT id FROM Person")[0]["id"] == "wd:Q265852"
    # the seat edge followed the node; the claim was re-keyed, not just re-pointed
    assert it_db.run_command("MATCH (:Person {id:'wd:Q265852'})-[r:HAS_ROLE]->(e:Entity {id:'wd:Q312'}) RETURN r.role AS r")[0]["r"] == "CEO"
    [c] = it_db.run_sql("SELECT claim_key, to_id FROM Claim")
    assert c["to_id"] == "wd:Q312"
    assert c["claim_key"] == owns_claim_key("22222222-2222-4222-8222-222222222222", "wd:Q312", "sec-edgar", "13G")
    # an old link still opens the company, under its new id
    assert get_full_profile(old)["entity"]["id"] == "wd:Q312"
    # idempotent
    assert heal_keyed_ids(apply=True)["Entity"]["moved"] == 0
