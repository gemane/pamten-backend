"""
Real-ArcadeDB integration test for the duplicate-person suggestion scan:
name-token matches, birth date+place matches, and shared-company corroboration
must produce the right confidence.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_duplicate_scan_confidence(it_db):
    from app.routers.persons import find_duplicate_persons

    # (A) name order-reversal + a shared company → HIGH
    it_db.run_command("CREATE (:Person {id:'a1', full_name:'Marcos Galperin', wikidata_id:'Q1'})")
    it_db.run_command("CREATE (:Person {id:'a2', full_name:'Galperin Marcos'})")
    it_db.run_command("CREATE (:Entity {id:'ml', name:'MercadoLibre', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'a1'}),(e:Entity{id:'ml'}) CREATE (p)-[:HAS_ROLE {role:'Founder'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'a2'}),(e:Entity{id:'ml'}) CREATE (p)-[:OWNS {}]->(e)")

    # (B) different name spelling but same birth date + place → HIGH
    it_db.run_command("CREATE (:Person {id:'b1', full_name:'Larry Page',    birth_date:'1973-03-26', birth_place:'East Lansing'})")
    it_db.run_command("CREATE (:Person {id:'b2', full_name:'Lawrence Page', birth_date:'1973-03-26', birth_place:'East Lansing'})")

    # (C) common 2-token name, honorific only, no corroboration → LOW
    it_db.run_command("CREATE (:Person {id:'c1', full_name:'David Taylor'})")
    it_db.run_command("CREATE (:Person {id:'c2', full_name:'Mr David Taylor'})")

    # (D) distinctive 3-token name, honorific only, no birth info → MEDIUM
    it_db.run_command("CREATE (:Person {id:'d1', full_name:'Alexander Julius Halpert'})")
    it_db.run_command("CREATE (:Person {id:'d2', full_name:'Mr Alexander Julius Halpert'})")

    # (E) same birth date, no place (BODS-style) → HIGH
    it_db.run_command("CREATE (:Person {id:'e1', full_name:'Grace Hopper', birth_date:'1906-12'})")
    it_db.run_command("CREATE (:Person {id:'e2', full_name:'Mrs Grace Hopper', birth_date:'1906-12'})")

    # (F) same distinctive name but DIFFERENT birth dates → likely two people
    it_db.run_command("CREATE (:Person {id:'f1', full_name:'Peter David Jones',    birth_date:'1974-08'})")
    it_db.run_command("CREATE (:Person {id:'f2', full_name:'Mr Peter David Jones', birth_date:'1966-03'})")

    # a genuinely unique person must NOT be flagged
    it_db.run_command("CREATE (:Person {id:'z1', full_name:'Unique Personne'})")

    res = find_duplicate_persons(_={"role": "contributor"})
    by_members = {frozenset(m["id"] for m in g["members"]): g for g in res["groups"]}

    assert by_members[frozenset(["a1", "a2"])]["confidence"] == "high"    # shared company
    assert by_members[frozenset(["a1", "a2"])]["suggested_keep_id"] == "a1"  # the Wikidata node
    assert by_members[frozenset(["b1", "b2"])]["confidence"] == "high"    # same DOB + place
    assert by_members[frozenset(["c1", "c2"])]["confidence"] == "low"     # common name only
    assert by_members[frozenset(["d1", "d2"])]["confidence"] == "medium"  # distinctive name
    assert by_members[frozenset(["e1", "e2"])]["confidence"] == "high"    # same birth date (no place)
    fg = by_members[frozenset(["f1", "f2"])]
    assert fg["confidence"] == "low" and fg["likely_distinct"] is True    # conflicting DOB
    assert not any("z1" in k for k in by_members)                         # unique not flagged
