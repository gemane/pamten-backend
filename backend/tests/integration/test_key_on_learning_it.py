"""Real-ArcadeDB: a node that learns a hard id after its creation is re-keyed
by the writer that stamps it (app.node_ids.key_on_learning) — its claims
follow under re-hashed keys, an old link forwards, and a keyed id already
taken leaves the uuid for the dedup. The 2026-10-27 rebuild left WARBURG
PINCUS LLC and Steven Sarracino under uuids once the one-off heal was gone.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_an_entity_created_by_name_is_keyed_when_its_cik_arrives(it_db):
    from app.claims import KIND_OWNS, owns_claim_key, record_claim
    from app.merged_ids import resolve_current_id
    from app.database import db
    from app.node_ids import is_uuid
    from app.scraper.graph_writer import _upsert_entity_by_name
    old = _upsert_entity_by_name("WARBURG PINCUS LLC", source_id="sec-edgar")      # a 13D party, no CIK
    assert is_uuid(old)
    it_db.run_command("CREATE (:Entity {id:'sec:0000000001', name:'Target Co', name_normalized:'target co'})")
    record_claim(kind=KIND_OWNS, from_id=old, to_id="sec:0000000001", source_id="sec-edgar",
                 stake_percent=7.0, filing_type="13D")
    new = _upsert_entity_by_name("WARBURG PINCUS LLC", cik="1162870", source_id="sec-edgar")
    assert new == "sec:0001162870"
    assert [r["id"] for r in it_db.run_sql("SELECT id FROM Entity WHERE name = 'WARBURG PINCUS LLC'")] == [new]
    [c] = it_db.run_sql("SELECT claim_key, from_id FROM Claim")
    assert c["from_id"] == new and c["claim_key"] == owns_claim_key(new, "sec:0000000001", "sec-edgar", "13D")
    with db.get_session() as session:
        assert resolve_current_id(session, old) == new                  # an old link forwards
    assert _upsert_entity_by_name("WARBURG PINCUS LLC", cik="0001162870", source_id="sec-edgar") == new


def test_a_person_matched_by_name_is_keyed_when_their_cik_arrives(it_db):
    from app.node_ids import is_uuid
    from app.scraper.graph_writer import _upsert_person_by_name
    old = _upsert_person_by_name("Steven Sarracino", source_id="sec-edgar")
    assert is_uuid(old)
    assert _upsert_person_by_name("Steven Sarracino", source_id="sec-edgar", sec_cik="1624037") == "sec:0001624037"
    assert [r["id"] for r in it_db.run_sql("SELECT id FROM Person")] == ["sec:0001624037"]


def test_wikidata_identifying_a_name_only_node_keys_it(it_db):
    from app.scraper.graph_writer import _upsert_entity_by_name
    from app.scraper.runner import _upsert_entity
    old = _upsert_entity_by_name("Gone Holdings Ltd", source_id="sec-edgar")
    assert _upsert_entity("Gone Holdings Ltd", None, None, "", "Q99", source_id="wikidata") == "wd:Q99"
    assert [r["id"] for r in it_db.run_sql("SELECT id FROM Entity")] == ["wd:Q99"] and old != "wd:Q99"


def test_a_taken_keyed_id_is_a_twin_for_the_dedup_not_forced(it_db):
    from app.node_ids import is_uuid, key_on_learning
    from app.database import db
    it_db.run_command("CREATE (:Entity {id:'sec:0000000777', name:'Keyed', name_normalized:'keyed', sec_cik:'0000000777'})")
    it_db.run_command("CREATE (:Entity {id:'33333333-3333-4333-8333-333333333333', name:'Twin', "
                      "name_normalized:'twin', sec_cik:'0000000777'})")
    with db.get_session() as session:
        got = key_on_learning(session, "Entity", "33333333-3333-4333-8333-333333333333")
    assert is_uuid(got)
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 2
