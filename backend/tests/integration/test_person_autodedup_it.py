"""
Real-ArcadeDB integration test for the auto-dedup step run after a scrape:
deduplicate_high_confidence() must merge ONLY high-confidence, non-distinct
groups and leave medium/low ones for manual review.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _high_and_medium(it_db):
    # HIGH: same name token set (order flipped) + shared company → auto-merge.
    it_db.run_command("CREATE (:Person {id:'p1', full_name:'Warren E Buffett', wikidata_id:'Q1'})")
    it_db.run_command("CREATE (:Person {id:'p2', full_name:'Buffett Warren E'})")
    it_db.run_command("CREATE (:Entity {id:'brk', name:'Berkshire Hathaway', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'p1'}),(e:Entity{id:'brk'}) CREATE (p)-[:HAS_ROLE {role:'CEO'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'p2'}),(e:Entity{id:'brk'}) CREATE (p)-[:OWNS {}]->(e)")

    # MEDIUM: nickname variant (surname + company, different given names) → review only.
    it_db.run_command("CREATE (:Person {id:'k1', full_name:'Rob Kapito', first_name:'Rob', last_name:'Kapito', wikidata_id:'Q2'})")
    it_db.run_command("CREATE (:Person {id:'k2', full_name:'Robert Kapito', first_name:'Robert', last_name:'Kapito'})")
    it_db.run_command("CREATE (:Entity {id:'blk', name:'BlackRock', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'k1'}),(e:Entity{id:'blk'}) CREATE (p)-[:HAS_ROLE {role:'Founder'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'k2'}),(e:Entity{id:'blk'}) CREATE (p)-[:OWNS {}]->(e)")


def test_autodedup_merges_high_confidence_only(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _high_and_medium(it_db)

    res = deduplicate_high_confidence(apply=True)

    # high-confidence pair merged: the SEC-order node is gone, the Wikidata one kept
    assert it_db.run_command("MATCH (p:Person {id:'p2'}) RETURN p.id AS id") == []
    assert it_db.run_command("MATCH (p:Person {id:'p1'}) RETURN p.id AS id")[0]["id"] == "p1"
    assert res["merged_count"] == 1
    assert res["merged"][0]["keep_id"] == "p1"

    # medium variant untouched, surfaced for review
    assert it_db.run_command("MATCH (p:Person {id:'k2'}) RETURN p.id AS id")[0]["id"] == "k2"
    review_ids = {m["id"] for g in res["needs_review"] for m in g["members"]}
    assert {"k1", "k2"} <= review_ids


def test_autodedup_dry_run_reports_without_merging(it_db):
    from app.routers.persons import deduplicate_high_confidence
    _high_and_medium(it_db)

    res = deduplicate_high_confidence(apply=False)

    assert res["applied"] is False
    assert res["merged_count"] == 1                                   # reported…
    assert it_db.run_command("MATCH (p:Person {id:'p2'}) RETURN p.id AS id")[0]["id"] == "p2"  # …but NOT merged
