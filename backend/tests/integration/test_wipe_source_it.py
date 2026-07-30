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

    remaining = {r["id"] for r in it_db.run_command("MATCH (e:Entity) RETURN e.id AS id")}
    assert remaining == {"b-node", "shared"}         # A's orphans gone; shared + B kept
    # B's edge is untouched
    assert it_db.run_command("MATCH ()-[o:OWNS]->() RETURN count(o) AS c")[0]["c"] == 1
    # the Source node itself is kept (re-import reuses it)
    assert it_db.run_command("MATCH (s:Source {id:'src-A'}) RETURN count(s) AS c")[0]["c"] == 1

    with pytest.raises(ValueError, match="No Source named"):
        wipe_source("Does Not Exist")


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
