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

    # (G) name variant (Larry/Laurence) + shared company, no shared birth → MEDIUM.
    # Neither the name-token nor the birth signal links these; only surname+company.
    it_db.run_command("CREATE (:Person {id:'g1', full_name:'Larry Fink', first_name:'Larry', last_name:'Fink', wikidata_id:'Q9'})")
    it_db.run_command("CREATE (:Person {id:'g2', full_name:'Laurence Fink', first_name:'Laurence', last_name:'Fink'})")
    it_db.run_command("CREATE (:Entity {id:'blk', name:'BlackRock', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'g1'}),(e:Entity{id:'blk'}) CREATE (p)-[:HAS_ROLE {role:'Founder'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'g2'}),(e:Entity{id:'blk'}) CREATE (p)-[:OWNS {}]->(e)")

    # (H) same surname + same company but INCOMPATIBLE given names (brothers) → NOT flagged
    it_db.run_command("CREATE (:Person {id:'h1', full_name:'Elon Musk',   first_name:'Elon',   last_name:'Musk'})")
    it_db.run_command("CREATE (:Person {id:'h2', full_name:'Kimbal Musk', first_name:'Kimbal', last_name:'Musk'})")
    it_db.run_command("CREATE (:Entity {id:'tsla', name:'Tesla', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'h1'}),(e:Entity{id:'tsla'}) CREATE (p)-[:HAS_ROLE {role:'CEO'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'h2'}),(e:Entity{id:'tsla'}) CREATE (p)-[:HAS_ROLE {role:'Director'}]->(e)")

    # (I) compatible given names + same surname but NO shared company → NOT flagged
    it_db.run_command("CREATE (:Person {id:'i1', full_name:'Bob Anderson',    first_name:'Bob',    last_name:'Anderson'})")
    it_db.run_command("CREATE (:Person {id:'i2', full_name:'Robert Anderson', first_name:'Robert', last_name:'Anderson'})")

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

    gg = by_members[frozenset(["g1", "g2"])]                              # name variant + company
    assert gg["confidence"] == "medium"
    assert "surname" in gg["reason"]
    assert gg["suggested_keep_id"] == "g1"                                # the Wikidata node

    assert not any({"h1", "h2"} <= set(k) for k in by_members)            # brothers not flagged
    assert not any({"i1", "i2"} <= set(k) for k in by_members)            # no shared company
    assert not any("z1" in k for k in by_members)                         # unique not flagged
