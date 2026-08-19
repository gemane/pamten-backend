"""
Real-ArcadeDB tests for Entity identity + the dedup heal.

Two guarantees the mocked suite can't check:
  1. Re-importing the same company (same LEI) across two runs must NOT create a
     second Entity node — the importers key on `lei:{LEI}`, so the upsert collapses
     them (the Austria-doubling regression from the old recordId-keyed importer).
  2. POST /scraper/deduplicate-entities (maintenance.deduplicate_entities) heals
     pre-existing doubles by merging on the LEI and migrating their edges.
"""
import pytest

pytestmark = pytest.mark.integration


def test_reupsert_same_lei_is_idempotent(it_db):
    from app.scraper.bulk_import import _BatchWriter, _entity

    # Two separate imports of the same company, keyed on its LEI (as GLEIF LEI-CDF
    # does), must upsert onto one node — never a second.
    for _ in range(2):
        batch = _BatchWriter()
        _entity(batch, "lei:LEI-ACME", name="Acme AG", entity_type="company",
                country="AT", founded=None, lei_id="LEI-ACME",
                companies_house_id=None, source_id="s", credibility_score=90)
        batch.flush()

    rows = it_db.run_command("MATCH (e:Entity) WHERE e.lei_id = 'LEI-ACME' RETURN count(e) AS n")
    assert rows[0]["n"] == 1                      # one company, not two
    ids = it_db.run_command("MATCH (e:Entity) WHERE e.lei_id = 'LEI-ACME' RETURN e.id AS id")
    assert ids[0]["id"] == "lei:LEI-ACME"         # keyed on the LEI


def test_deduplicate_entities_heals_legacy_doubles(it_db):
    from app.scraper import maintenance

    # Simulate the legacy state: two nodes for the same LEI (old uuid-keyed node
    # + new lei-keyed node), each carrying a distinct edge.
    it_db.run_command("CREATE (e:Entity {id:'old-uuid', name:'Acme AG', lei_id:'LEI-ACME', "
                      "name_credibility:80, verified:false})")
    it_db.run_command("CREATE (e:Entity {id:'lei:LEI-ACME', name:'Acme AG', lei_id:'LEI-ACME', "
                      "name_credibility:90, verified:false})")
    it_db.run_command("CREATE (e:Entity {id:'owner-1', name:'Owner One'})")
    it_db.run_command("CREATE (e:Entity {id:'sub-1', name:'Subsidiary'})")
    # incoming OWNS onto the dead node; outgoing OWNS from the dead node
    it_db.run_command("MATCH (o:Entity {id:'owner-1'}), (e:Entity {id:'old-uuid'}) "
                      "CREATE (o)-[:OWNS {stake_percent:10, until:null}]->(e)")
    it_db.run_command("MATCH (e:Entity {id:'old-uuid'}), (s:Entity {id:'sub-1'}) "
                      "CREATE (e)-[:OWNS {stake_percent:55, until:null}]->(s)")

    res = maintenance.deduplicate_entities()
    assert res["entities_merged"] == 1

    # One survivor, and it's the higher-credibility node.
    surv = it_db.run_command("MATCH (e:Entity) WHERE e.lei_id = 'LEI-ACME' RETURN e.id AS id")
    assert len(surv) == 1
    assert surv[0]["id"] == "lei:LEI-ACME"

    # Both edges rehomed onto the survivor (incoming from owner, outgoing to sub).
    inc = it_db.run_command("MATCH (o)-[:OWNS]->(e:Entity {id:'lei:LEI-ACME'}) RETURN count(o) AS n")
    assert inc[0]["n"] == 1
    out = it_db.run_command("MATCH (e:Entity {id:'lei:LEI-ACME'})-[:OWNS]->(s) RETURN count(s) AS n")
    assert out[0]["n"] == 1


def test_deduplicate_entities_migrates_person_owner_edge(it_db):
    from app.scraper import maintenance

    # A person owns the dead node. Migration must relabel the OWNS source as
    # :Person (captured via labels(s)); a label-less match would full-scan and
    # hang at scale, and a wrong :Entity label would silently drop the edge.
    it_db.run_command("CREATE (e:Entity {id:'old-uuid', name:'Acme AG', lei_id:'LEI-P', "
                      "name_credibility:80, verified:false})")
    it_db.run_command("CREATE (e:Entity {id:'lei:LEI-P', name:'Acme AG', lei_id:'LEI-P', "
                      "name_credibility:90, verified:false})")
    it_db.run_command("CREATE (p:Person {id:'person-1', full_name:'Jane Owner'})")
    it_db.run_command("MATCH (p:Person {id:'person-1'}), (e:Entity {id:'old-uuid'}) "
                      "CREATE (p)-[:OWNS {stake_percent:30, until:null}]->(e)")

    res = maintenance.deduplicate_entities()
    assert res["entities_merged"] == 1

    # The person→entity OWNS edge is rehomed onto the survivor, still from a Person.
    rows = it_db.run_command(
        "MATCH (p:Person)-[:OWNS]->(e:Entity {id:'lei:LEI-P'}) RETURN p.id AS pid")
    assert [r["pid"] for r in rows] == ["person-1"]


def test_deduplicate_entities_batches_with_limit(it_db):
    from app.scraper import maintenance

    # Two independent duplicate groups (two LEIs, each with a double).
    for lei in ("LEI-A", "LEI-B"):
        it_db.run_command(f"CREATE (e:Entity {{id:'old-{lei}', name:'Co', lei_id:'{lei}'}})")
        it_db.run_command(f"CREATE (e:Entity {{id:'lei:{lei}', name:'Co', lei_id:'{lei}'}})")

    # First call: only one group heals, one still remains.
    r1 = maintenance.deduplicate_entities(limit=1)
    assert r1["duplicate_groups_found"] == 2
    assert r1["groups_processed"] == 1
    assert r1["entities_merged"] == 1
    assert r1["remaining"] == 1

    # Second call: the last group heals, nothing left.
    r2 = maintenance.deduplicate_entities(limit=1)
    assert r2["entities_merged"] == 1
    assert r2["remaining"] == 0

    # Idempotent: a third call finds no duplicates.
    r3 = maintenance.deduplicate_entities()
    assert r3["duplicate_groups_found"] == 0
    assert r3["entities_merged"] == 0


def test_bulk_heal_keeps_one_per_lei_and_drops_the_rest(it_db):
    from app.scraper import maintenance

    # Two nodes for LEI-X (a dup) + one for LEI-Y (not a dup). The survivor is the
    # min-id node ('x-a'); the loser 'x-b' carries an edge that must vanish with it.
    it_db.run_command("CREATE (e:Entity {id:'x-a', name:'X', lei_id:'LEI-X'})")
    it_db.run_command("CREATE (e:Entity {id:'x-b', name:'X dup', lei_id:'LEI-X'})")
    it_db.run_command("CREATE (e:Entity {id:'y-only', name:'Y', lei_id:'LEI-Y'})")
    it_db.run_command("CREATE (e:Entity {id:'owner', name:'Owner'})")
    it_db.run_command("MATCH (o:Entity {id:'owner'}), (e:Entity {id:'x-b'}) "
                      "CREATE (o)-[:OWNS {until:null}]->(e)")

    res = maintenance.deduplicate_entities_bulk()
    assert res["entities_removed"] == 1
    assert res["by"]["lei_id"]["groups"] == 1

    # Only the min-id keeper ('x-a') remains for LEI-X; LEI-Y untouched.
    survivors = it_db.run_command("MATCH (e:Entity) WHERE e.lei_id='LEI-X' RETURN e.id AS id")
    assert [r["id"] for r in survivors] == ["x-a"]
    y = it_db.run_command("MATCH (e:Entity) WHERE e.lei_id='LEI-Y' RETURN count(e) AS n")
    assert y[0]["n"] == 1
    # The dropped node's edge is gone with it.
    edges = it_db.run_command("MATCH (:Entity {id:'owner'})-[r:OWNS]->() RETURN count(r) AS n")
    assert edges[0]["n"] == 0


def test_a_merge_carries_every_edge_property(it_db):
    """An OWNS edge is *recreated* during a merge, not moved — so any property the
    migration query forgets is silently destroyed.

    Three were being lost: `interest_types`, `direct_or_indirect` (GLEIF's
    direct/ultimate marker, which the renderer and `mark-shortcuts` both read) and
    `psc_self_link` (the key the Companies House refresh matches an edge on — lose
    it and the next refresh cannot find the edge, so it creates a second one).

    Asserted on both directions, because the migration has a separate query for
    each and fixing only the one you happened to test is the easy mistake.
    """
    from app.scraper import maintenance

    it_db.run_command("CREATE (e:Entity {id:'old-props', name:'Acme AG', lei_id:'LEI-X', "
                      "name_credibility:80, verified:false})")
    it_db.run_command("CREATE (e:Entity {id:'lei:LEI-X', name:'Acme AG', lei_id:'LEI-X', "
                      "name_credibility:90, verified:false})")
    it_db.run_command("CREATE (e:Entity {id:'target-co', name:'Target Ltd'})")
    it_db.run_command("CREATE (p:Person {id:'person-props', full_name:'Ann Owner'})")

    # Outgoing: the dead node owns something.
    it_db.run_command(
        "MATCH (a:Entity {id:'old-props'}), (t:Entity {id:'target-co'}) "
        "CREATE (a)-[:OWNS {stake_percent:60, direct_or_indirect:'indirect', "
        "interest_types:['shareholding'], psc_self_link:'/link/out', until:null}]->(t)")
    # Incoming: a person owns the dead node.
    it_db.run_command(
        "MATCH (p:Person {id:'person-props'}), (b:Entity {id:'old-props'}) "
        "CREATE (p)-[:OWNS {stake_percent:30, direct_or_indirect:'direct', "
        "interest_types:['votingRights'], psc_self_link:'/link/in', until:null}]->(b)")

    assert maintenance.deduplicate_entities()["entities_merged"] == 1

    out = it_db.run_command(
        "MATCH (a:Entity {id:'lei:LEI-X'})-[r:OWNS]->(t:Entity {id:'target-co'}) "
        "RETURN r.direct_or_indirect AS doi, r.psc_self_link AS pscl, "
        "r.interest_types AS itypes")[0]
    assert out["doi"] == "indirect", "GLEIF's direct/ultimate marker was dropped"
    assert out["pscl"] == "/link/out", "the PSC refresh key was dropped"
    assert out["itypes"] == ["shareholding"]

    inc = it_db.run_command(
        "MATCH (p:Person {id:'person-props'})-[r:OWNS]->(b:Entity {id:'lei:LEI-X'}) "
        "RETURN r.direct_or_indirect AS doi, r.psc_self_link AS pscl, "
        "r.interest_types AS itypes")[0]
    assert inc["doi"] == "direct"
    assert inc["pscl"] == "/link/in"
    assert inc["itypes"] == ["votingRights"]
