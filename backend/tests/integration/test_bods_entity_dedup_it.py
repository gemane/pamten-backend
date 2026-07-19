"""
Real-ArcadeDB tests for BODS entity identity + the dedup heal.

Two guarantees the mocked suite can't check:
  1. Re-importing the same company (same LEI) with a different BODS recordId
     across two runs must NOT create a second Entity node — the Austria-doubling
     regression from the recordId-keyed importer.
  2. POST /scraper/deduplicate-entities (maintenance.deduplicate_entities) heals
     pre-existing doubles by merging on the LEI and migrating their edges.
"""
import pytest

pytestmark = pytest.mark.integration


def _entity(rid, name, lei):
    return {"recordType": "entity", "recordId": rid,
            "recordDetails": {"name": name, "jurisdiction": {"code": "AT"},
                              "entityType": {"type": "registeredEntity"},
                              "identifiers": [{"scheme": "XI-LEI", "id": lei}]}}


def test_reimport_same_lei_different_recordid_is_idempotent(it_db):
    from app.scraper.bods import _run_import

    # 1) "Austria-only" run, then 2) "full GLEIF" run — same company + LEI,
    # different bods recordId (as a fresh dump would assign).
    _run_import(iter([_entity("AT-001", "Acme AG", "LEI-ACME")]),
                source_id="s", credibility_score=90, limit=None, filter_jurisdiction="AT")
    _run_import(iter([_entity("GLEIF-999", "Acme AG", "LEI-ACME")]),
                source_id="s", credibility_score=90, limit=None, filter_jurisdiction=None)

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
