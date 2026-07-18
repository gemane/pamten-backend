"""
Real-ArcadeDB integration test for "keep separate" (confirmed-distinct persons)
and the merge log. Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration

ROLE = {"role": "contributor"}


def _make_flagged_pair(it_db):
    # order-flipped name + shared company → a high-confidence duplicate group
    it_db.run_command("CREATE (:Person {id:'p1', full_name:'Warren E Buffett', wikidata_id:'Q1'})")
    it_db.run_command("CREATE (:Person {id:'p2', full_name:'Buffett Warren E'})")
    it_db.run_command("CREATE (:Entity {id:'brk', name:'Berkshire Hathaway', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'p1'}),(e:Entity{id:'brk'}) CREATE (p)-[:HAS_ROLE {role:'CEO'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'p2'}),(e:Entity{id:'brk'}) CREATE (p)-[:OWNS {}]->(e)")


def _flagged(res):
    return any({m["id"] for m in g["members"]} == {"p1", "p2"} for g in res["groups"])


def test_keep_separate_suppresses_and_undo_restores(it_db):
    from app.routers.persons import (find_duplicate_persons, keep_separate,
                                     undo_keep_separate, list_kept_separate)
    from app.models.person import KeepSeparateRequest
    _make_flagged_pair(it_db)

    assert _flagged(find_duplicate_persons(_=ROLE))               # flagged initially

    keep_separate(KeepSeparateRequest(ids=["p1", "p2"]), _=ROLE)
    assert not _flagged(find_duplicate_persons(_=ROLE))           # suppressed
    kept = list_kept_separate(_=ROLE)
    assert any({p["a_id"], p["b_id"]} == {"p1", "p2"} for p in kept["pairs"])

    undo_keep_separate(KeepSeparateRequest(ids=["p1", "p2"]), _=ROLE)
    assert _flagged(find_duplicate_persons(_=ROLE))               # flagged again
    assert list_kept_separate(_=ROLE)["count"] == 0


def test_merge_log_records_merges(it_db):
    from app.routers.persons import merge_persons, merge_log
    from app.models.person import PersonMergeRequest
    it_db.run_command("CREATE (:Person {id:'k', full_name:'Larry Page', wikidata_id:'Q1'})")
    it_db.run_command("CREATE (:Person {id:'d', full_name:'Page Lawrence'})")

    merge_persons(PersonMergeRequest(keep_id="k", dup_id="d"), _=ROLE)

    entries = merge_log(limit=200, _=ROLE)["entries"]
    hit = next((e for e in entries if e["keep_id"] == "k" and e["dup_name"] == "Page Lawrence"), None)
    assert hit is not None
    assert hit["keep_name"] == "Larry Page"
    assert hit["count"] == 1
