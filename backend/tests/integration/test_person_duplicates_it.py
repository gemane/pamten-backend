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

    # (J) SEC last-first name matches a Wikidata *alias*, not the full name → linked.
    # "Gates William H Iii" shares no name-token set with "Bill Gates", but does
    # with its alias "William H. Gates III"; a shared company makes it HIGH.
    it_db.run_command("CREATE (:Person {id:'j1', full_name:'Bill Gates', wikidata_id:'Q5', alias:$a})",
                      {"a": ["William Henry Gates", "William H. Gates III"]})
    it_db.run_command("CREATE (:Person {id:'j2', full_name:'Gates William H Iii', first_name:'Gates', last_name:'William H Iii'})")
    it_db.run_command("CREATE (:Entity {id:'msft', name:'Microsoft', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'j1'}),(e:Entity{id:'msft'}) CREATE (p)-[:HAS_ROLE {role:'Founder'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'j2'}),(e:Entity{id:'msft'}) CREATE (p)-[:OWNS {}]->(e)")

    # (K) same SEC CIK, different name spellings, NO shared company → HIGH (hard id).
    # The CIK is definitive: one filer, however the name is written.
    it_db.run_command("CREATE (:Person {id:'k1b', full_name:'Timothy D Cook', sec_cik:'0001214156', wikidata_id:'Q7'})")
    it_db.run_command("CREATE (:Person {id:'k2b', full_name:'Tim Cook', sec_cik:'0001214156'})")

    # (L) IDENTICAL name + same company, no birth date, no CIK → MEDIUM.
    # This is the father/son trap: cannot tell one person from two relatives.
    it_db.run_command("CREATE (:Person {id:'l1', full_name:'John Q Public', first_name:'John', last_name:'Public'})")
    it_db.run_command("CREATE (:Person {id:'l2', full_name:'John Q Public', first_name:'John', last_name:'Public'})")
    it_db.run_command("CREATE (:Entity {id:'acme', name:'Acme Corp', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'l1'}),(e:Entity{id:'acme'}) CREATE (p)-[:HAS_ROLE {role:'CEO'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'l2'}),(e:Entity{id:'acme'}) CREATE (p)-[:OWNS {}]->(e)")

    # (M) punctuation-only variance + shared company → HIGH (formatting, not
    # identity — "C." and "C" are the same; user decision 2026-09-11)
    it_db.run_command("CREATE (:Person {id:'m1', full_name:'Monica C. Lozano', wikidata_id:'Q11'})")
    it_db.run_command("CREATE (:Person {id:'m2', full_name:'Monica C Lozano'})")
    it_db.run_command("CREATE (:Entity {id:'aapl2', name:'Apple', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'m1'}),(e:Entity{id:'aapl2'}) CREATE (p)-[:HAS_ROLE {role:'Director'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'m2'}),(e:Entity{id:'aapl2'}) CREATE (p)-[:OWNS {}]->(e)")

    # (N) same name, same company, but DIFFERENT SEC CIKs → definitively two
    # people (father/son who both file) — never merged, however alike.
    it_db.run_command("CREATE (:Person {id:'n1', full_name:'Sam T. Rivers', sec_cik:'0000333333'})")
    it_db.run_command("CREATE (:Person {id:'n2', full_name:'Sam T Rivers', sec_cik:'0000444444'})")
    it_db.run_command("CREATE (:Entity {id:'riv', name:'Rivers Corp', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'n1'}),(e:Entity{id:'riv'}) CREATE (p)-[:HAS_ROLE {role:'CEO'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'n2'}),(e:Entity{id:'riv'}) CREATE (p)-[:OWNS {}]->(e)")

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

    kg = by_members[frozenset(["k1b", "k2b"])]                            # same SEC CIK
    assert kg["confidence"] == "high"
    assert "CIK" in kg["reason"]
    assert kg["suggested_keep_id"] == "k1b"                               # the Wikidata node

    lg = by_members[frozenset(["l1", "l2"])]                              # identical name + company
    assert lg["confidence"] == "medium", "father/son: identical name is not enough to merge"
    assert "relative" in lg["reason"]

    mg = by_members[frozenset(["m1", "m2"])]                              # punctuation-only + company
    assert mg["confidence"] == "high", "C. and C are the same — formatting merges"
    assert mg["suggested_keep_id"] == "m1"                                # the Wikidata node

    ng = by_members[frozenset(["n1", "n2"])]                              # conflicting CIKs
    assert ng["likely_distinct"] is True and ng["confidence"] == "low", \
        "two SEC CIKs are two people, however alike the names"
    assert "CIK" in ng["reason"]

    assert not any({"h1", "h2"} <= set(k) for k in by_members)            # brothers not flagged
    assert not any({"i1", "i2"} <= set(k) for k in by_members)            # no shared company

    jg = by_members[frozenset(["j1", "j2"])]                              # matched via alias
    assert jg["confidence"] == "high"                                     # + shared company
    assert jg["suggested_keep_id"] == "j1"                                # the Wikidata node

    assert not any("z1" in k for k in by_members)                         # unique not flagged
