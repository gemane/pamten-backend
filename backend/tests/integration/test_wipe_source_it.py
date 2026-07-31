"""
Real-ArcadeDB test for wipe_source — delete one source's data (edges + the nodes
only it created) while keeping nodes another source still references, and reset the
GLEIF import checkpoint when GLEIF is wiped.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _src(it_db, sid, name):
    it_db.run_command("CREATE (s:Source {id:$id, name:$n})", {"id": sid, "n": name})


def _ent(it_db, eid, source_id):
    it_db.run_command("CREATE (e:Entity {id:$id, source_id:$s})", {"id": eid, "s": source_id})


def _owns(it_db, a, b, source_id):
    it_db.run_command(
        "MATCH (a:Entity {id:$a}),(b:Entity {id:$b}) CREATE (a)-[:OWNS {source_id:$s}]->(b)",
        {"a": a, "b": b, "s": source_id})


def test_wipe_source_keeps_shared_and_other_sources(it_db):
    from app.scraper.maintenance import wipe_source

    _src(it_db, "src-A", "Wipe Me")
    _src(it_db, "src-B", "Keep Me")

    _ent(it_db, "a-parent", "src-A")
    _ent(it_db, "a-child", "src-A")
    _owns(it_db, "a-parent", "a-child", "src-A")     # A edge → both A nodes orphaned after wipe
    _ent(it_db, "a-lonely", "src-A")                 # A node, no edges → deleted
    _ent(it_db, "shared", "src-A")                   # A-origin but kept (B links it)
    _ent(it_db, "b-node", "src-B")
    _owns(it_db, "b-node", "shared", "src-B")        # B edge onto the A-origin node

    res = wipe_source("Wipe Me")
    assert res["edges"]["OWNS"] == 1                 # only A's edge
    assert res["nodes"]["Entity"] == 3              # a-parent, a-child, a-lonely
    assert res.get("reindexed") is not False         # stale-entry cleanup ran (REBUILD INDEX *)

    remaining = {r["id"] for r in it_db.run_command("MATCH (e:Entity) RETURN e.id AS id")}
    assert remaining == {"b-node", "shared"}         # A's orphans gone; shared + B kept
    # B's edge is untouched
    assert it_db.run_command("MATCH ()-[o:OWNS]->() RETURN count(o) AS c")[0]["c"] == 1
    # the Source node itself is kept (re-import reuses it)
    assert it_db.run_command("MATCH (s:Source {id:'src-A'}) RETURN count(s) AS c")[0]["c"] == 1

    with pytest.raises(ValueError, match="No Source named"):
        wipe_source("Does Not Exist")


def test_wipe_source_by_id_prefix_is_degree_aware(it_db):
    """The fast path: delete a source's nodes by an indexed id range, still keeping
    a prefixed node another source references."""
    from app.scraper.maintenance import wipe_source

    _src(it_db, "psc", "UK PSC")
    _src(it_db, "wd", "Wikidata")
    it_db.run_command("CREATE (p:Person {id:'chpsc:1', source_id:'psc'})")   # orphan → deleted
    it_db.run_command("CREATE (p:Person {id:'chpsc:2', source_id:'psc'})")   # orphan → deleted
    it_db.run_command("CREATE (p:Person {id:'chpsc:kept', source_id:'psc'})")
    _ent(it_db, "gb-coh:99", "psc")                                          # orphan company → deleted
    _ent(it_db, "wd:7", "wd")
    it_db.run_command("MATCH (e:Entity {id:'wd:7'}),(p:Person {id:'chpsc:kept'}) "
                      "CREATE (e)-[:HAS_ROLE {source_id:'wd'}]->(p)")        # Wikidata keeps chpsc:kept

    res = wipe_source("UK PSC", id_prefixes=["chpsc:", "gb-coh:"])
    assert res["nodes"]["Person"] == 2      # the two orphaned chpsc persons
    assert res["nodes"]["Entity"] == 1      # gb-coh:99

    people = {r["id"] for r in it_db.run_command("MATCH (p:Person) RETURN p.id AS id")}
    ents = {r["id"] for r in it_db.run_command("MATCH (e:Entity) RETURN e.id AS id")}
    assert people == {"chpsc:kept"}         # kept by the Wikidata edge (degree-aware)
    assert ents == {"wd:7"}


def test_wipe_gleif_resets_import_checkpoint(it_db):
    from app.scraper.gleif_incremental import full_load_present, mark_full_load_done, write_last_publish
    from app.scraper.maintenance import wipe_source

    _src(it_db, "gleif-src", "GLEIF")
    mark_full_load_done()
    write_last_publish("2026-07-30 00:00:00")
    assert full_load_present() is True

    res = wipe_source("GLEIF")
    assert res.get("reset_import_state") is True
    assert full_load_present() is False              # cron will now refuse until re-baselined
