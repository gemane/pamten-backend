"""
Real-ArcadeDB integration test for the trusted-peer federation foundation:
export → import round-trip (reconciled on external ids), and the peer registry.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_export_import_roundtrip(it_db):
    from app.routers.federation import build_export, import_snapshot

    it_db.run_command("CREATE (:Entity {id:'e1', name:'Alpha Corp', name_normalized:'alpha corp', type:'company', wikidata_id:'Q100'})")
    it_db.run_command("CREATE (:Entity {id:'e2', name:'Beta Holding', name_normalized:'beta holding', type:'holding', wikidata_id:'Q200'})")
    it_db.run_command("CREATE (:Person {id:'p1', full_name:'Jane Owner', wikidata_id:'Q300'})")
    it_db.run_command("MATCH (a:Entity{id:'e2'}),(b:Entity{id:'e1'}) CREATE (a)-[:OWNS {stake_percent:55.0, ownership_type:'majority'}]->(b)")
    it_db.run_command("MATCH (a:Person{id:'p1'}),(b:Entity{id:'e1'}) CREATE (a)-[:OWNS {stake_percent:5.0, ownership_type:'minority'}]->(b)")

    snap = build_export()
    assert snap["format"] == "pamten-federation"
    assert {e["name"] for e in snap["entities"]} >= {"Alpha Corp", "Beta Holding"}
    assert any(p["full_name"] == "Jane Owner" for p in snap["persons"])
    assert len(snap["ownerships"]) == 2

    # Wipe the graph and re-import — simulates a peer that had none of this data.
    it_db.run_command("MATCH (e:Entity) DETACH DELETE e")
    it_db.run_command("MATCH (p:Person) DETACH DELETE p")

    counts = import_snapshot(snap, source_name="Peer: Test", credibility=70)
    assert counts["entities"] == 2
    assert counts["persons"] == 1
    assert counts["ownerships"] == 2

    # Reconciled on QID; the ownership now carries the peer's Source.
    assert it_db.run_command("MATCH (e:Entity {wikidata_id:'Q100'}) RETURN e.name AS n")[0]["n"] == "Alpha Corp"
    peer_src = it_db.run_command("MATCH (s:Source {name:'Peer: Test'}) RETURN s.id AS id")[0]["id"]
    edge = it_db.run_command(
        "MATCH (:Person{wikidata_id:'Q300'})-[r:OWNS]->(:Entity{wikidata_id:'Q100'}) "
        "RETURN r.stake_percent AS s, r.source_id AS sid")[0]
    assert edge["s"] == 5.0
    assert edge["sid"] == peer_src


def test_import_reconciles_on_external_id_no_duplicate(it_db):
    from app.routers.federation import import_snapshot
    it_db.run_command("CREATE (:Entity {id:'x', name:'Gamma Inc', name_normalized:'gamma inc', type:'company', wikidata_id:'Q900'})")

    snap = {"format": "pamten-federation", "version": 1, "persons": [], "ownerships": [],
            "entities": [{"name": "Gamma Incorporated", "type": "company", "wikidata_id": "Q900"}]}
    import_snapshot(snap, "Peer: T", 60)

    n = it_db.run_command("MATCH (e:Entity {wikidata_id:'Q900'}) RETURN count(e) AS c")[0]["c"]
    assert n == 1   # matched on QID, not duplicated by the different name spelling


def test_peer_registry(it_db, monkeypatch):
    from app.config import settings
    from app.routers.federation import add_peer, list_peers, remove_peer
    from app.models.federation import PeerCreate
    monkeypatch.setattr(settings, "FEDERATION_ENABLED", True)
    admin = {"role": "admin"}

    out = add_peer(PeerCreate(name="Peer One", base_url="https://peer.example.com/", auth_token="secret"), _=admin)
    pid = out["id"]

    row = next(p for p in list_peers(_=admin)["peers"] if p["id"] == pid)
    assert row["base_url"] == "https://peer.example.com"   # trailing slash stripped
    assert row["has_token"] is True
    assert "auth_token" not in row                          # token never returned

    remove_peer(pid, _=admin)
    assert all(p["id"] != pid for p in list_peers(_=admin)["peers"])
