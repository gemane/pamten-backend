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


# ── findings item 20 (2026-10-30): the id no longer depends on the write order ──

@pytest.mark.parametrize("sec_first", [True, False])
def test_viavi_is_sec_whichever_source_writes_first(it_db, sec_first):
    """Wikidata's search was too busy for Viavi in one rebuild and not in the
    other: the node was `sec:` once and `wd:` once. A register's id ranks
    above a knowledge base's (LEI > CIK > QID), so it is `sec:` both ways."""
    from app.scraper.graph_writer import _upsert_entity_by_name
    from app.scraper.runner import _upsert_entity
    if sec_first:
        _upsert_entity_by_name("VIAVI SOLUTIONS INC.", cik="912093", source_id="sec-edgar")
    _upsert_entity("VIAVI SOLUTIONS INC.", None, None, "", "Q28222763", source_id="wikidata")
    if not sec_first:
        assert _upsert_entity_by_name("VIAVI SOLUTIONS INC.", cik="912093", source_id="sec-edgar") == "sec:0000912093"
    [row] = it_db.run_sql("SELECT id, wikidata_id, sec_cik FROM Entity")
    assert (row["id"], row["wikidata_id"], row["sec_cik"]) == ("sec:0000912093", "Q28222763", "0000912093")
    if not sec_first:
        assert it_db.run_sql("SELECT new_id FROM MergedId WHERE old_id = 'wd:Q28222763'")[0]["new_id"] == "sec:0000912093"


def test_a_lei_node_merged_into_its_filer_gives_the_survivor_its_id(it_db):
    """Altaba: the dedup folded `lei:549300ZJC0V1K2ZCKG85` into the filer
    `sec:0001011006`, which then held the LEI under the CIK's id — and the GLEIF
    delta, which updates `lei:` ids, never saw it again. The survivor now takes
    the LEI's id; both old ids forward in one hop, and none forwards to itself."""
    from app.scraper.maintenance import merge_entities_by_id
    lei, cik = "549300ZJC0V1K2ZCKG85", "0001011006"
    it_db.run_command(f"CREATE (:Entity {{id:'sec:{cik}', name:'ALTABA INC.', name_normalized:'altaba', "
                      f"sec_cik:'{cik}', type:'company'}})")
    it_db.run_command(f"CREATE (:Entity {{id:'lei:{lei}', name:'ALTABA INC.', name_normalized:'altaba', "
                      f"lei_id:'{lei}', type:'company'}})")
    it_db.run_command("CREATE (:MergedId {old_id:'33333333-3333-4333-8333-333333333333', new_id:'sec:0001011006'})")
    res = merge_entities_by_id(f"sec:{cik}", f"lei:{lei}", apply=True)
    assert res["keep"]["now_id"] == f"lei:{lei}"
    [row] = it_db.run_sql("SELECT id, lei_id, sec_cik FROM Entity")
    assert (row["id"], row["lei_id"], row["sec_cik"]) == (f"lei:{lei}", lei, cik)
    fwd = {r["old_id"]: r["new_id"] for r in it_db.run_sql("SELECT old_id, new_id FROM MergedId")}
    assert fwd == {f"sec:{cik}": f"lei:{lei}", "33333333-3333-4333-8333-333333333333": f"lei:{lei}"}
    from app.merged_ids import canonical_id, invalidate_forwarding_cache
    invalidate_forwarding_cache()
    assert canonical_id(f"lei:{lei}") == f"lei:{lei}"              # a GLEIF import writes the live node


def test_the_gleif_delta_finds_a_filer_that_took_its_lei(it_db):
    """The guest list of the delta is the `lei:` ids (`existing_lei_ids`): a
    filer joined to its GLEIF record is on it now."""
    from app.database import db
    from app.node_ids import key_on_learning
    from app.scraper.gleif_incremental import existing_lei_ids
    it_db.run_command("CREATE (:Entity {id:'sec:0000019617', name:'JPMORGAN CHASE & CO', name_normalized:'jpmorgan chase', "
                      "sec_cik:'0000019617', lei_id:'8I5DZWZKVSZI1NUHU748', type:'company'})")
    assert "lei:8I5DZWZKVSZI1NUHU748" not in existing_lei_ids()
    with db.get_session() as session:
        assert key_on_learning(session, "Entity", "sec:0000019617") == "lei:8I5DZWZKVSZI1NUHU748"
    assert "lei:8I5DZWZKVSZI1NUHU748" in existing_lei_ids()
