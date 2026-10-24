"""Real-ArcadeDB: stable Source ids end to end — the writer, the catalogue,
a peer, and the heal of a database written the old way.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

from app.database import db
from app.scraper import sources as src_mod

pytestmark = pytest.mark.integration


def test_the_writer_and_the_catalogue_share_one_node_under_the_stable_id(it_db):
    from app.scraper.graph_writer import _ensure_source

    assert _ensure_source("GLEIF", "https://www.gleif.org", 92) == "gleif"
    assert _ensure_source("GLEIF", "https://www.gleif.org", 92) == "gleif"      # idempotent
    src_mod._ensure_sources()                                                     # the catalogue's turn
    rows = it_db.run_sql("SELECT id, name, registry_key, enabled, data_mode, type FROM Source WHERE name = 'GLEIF'")
    assert len(rows) == 1
    assert rows[0]["id"] == "gleif" and rows[0]["registry_key"] == "bods_gleif"
    assert rows[0]["enabled"] is True and rows[0]["data_mode"] == "full" and rows[0]["type"] == "register"
    assert src_mod.get_source_enabled("bods_gleif") is True
    assert {r["id"] for r in it_db.run_sql("SELECT id FROM Source")} == {"wikidata", "sec-edgar", "opencorporates", "gleif", "uk-psc"}


def test_a_peer_source_is_keyed_by_its_slug(it_db):
    from app.routers.federation import _ensure_peer_source
    with db.get_session() as session:
        sid = _ensure_peer_source(session, "Peer: Signed", 60, True, "k1")
        assert sid == "peer:signed"
        assert _ensure_peer_source(session, "Peer: Signed", 60, False, "k2") == "peer:signed"
    row = it_db.run_sql("SELECT verified, key_id, type FROM Source WHERE id = 'peer:signed'")[0]
    assert (row["verified"], row["key_id"], row["type"]) == (False, "k2", "peer")


def test_the_heal_rekeys_a_database_written_the_old_way(it_db):
    from app.claims import claim_key, record_claim
    from app.source_ids import heal

    # the old shape: a uuid Source, a ScraperSource toggle row beside it, and
    # holders carrying the uuid — an entity, an edge, a claim keyed on it
    it_db.run_command("CREATE (:Source {id:'u-gleif', name:'GLEIF', type:'register', credibility_score:92, url:'https://www.gleif.org'})")
    it_db.run_sql("CREATE VERTEX TYPE ScraperSource IF NOT EXISTS")
    it_db.run_sql("INSERT INTO ScraperSource SET name = 'bods_gleif', enabled = false, data_mode = 'full', kind = 'bulk'")
    it_db.run_command("CREATE (:Entity {id:'a', name:'A', name_normalized:'a', source_id:'u-gleif'})")
    it_db.run_command("CREATE (:Entity {id:'b', name:'B', name_normalized:'b', source_id:'u-gleif'})")
    it_db.run_command("MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) CREATE (a)-[:OWNS {source_id:'u-gleif', stake_percent:100.0, until:null}]->(b)")
    record_claim(kind="owns", from_id="a", to_id="b", source_id="u-gleif", stake_percent=100.0,
                 source_url="https://x.test", source_date="2026-01-01", credibility_score=92)
    old_key = claim_key("owns", "a", "b", "u-gleif")
    assert it_db.run_sql("SELECT count(*) AS n FROM Claim WHERE claim_key = :k", {"k": old_key})[0]["n"] == 1

    dry = heal(apply=False)
    assert dry["sources"] == 1 and dry["holders"] == {"Entity": 2, "OWNS": 1, "Claim": 1} and dry["toggles_moved"] == 1
    assert it_db.run_sql("SELECT id FROM Source")[0]["id"] == "u-gleif"                  # nothing moved

    res = heal(apply=True)
    assert res["sources"] == 1 and res["holders"] == {"Entity": 2, "OWNS": 1, "Claim": 1}
    assert res["toggles_moved"] == 1 and res["scraper_rows_deleted"] == 1
    src = it_db.run_sql("SELECT id, registry_key, enabled, data_mode, kind FROM Source")
    assert src == [{"id": "gleif", "registry_key": "bods_gleif", "enabled": False, "data_mode": "full", "kind": "bulk"}] or \
        {k: src[0][k] for k in ("id", "registry_key", "enabled", "data_mode", "kind")} == \
        {"id": "gleif", "registry_key": "bods_gleif", "enabled": False, "data_mode": "full", "kind": "bulk"}
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity WHERE source_id = 'gleif'")[0]["n"] == 2
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS WHERE source_id = 'gleif'")[0]["n"] == 1
    # the claim followed, and its key was recomputed for the new source id
    claims = it_db.run_sql("SELECT claim_key, source_id FROM Claim")
    assert [(c["source_id"], c["claim_key"]) for c in claims] == [("gleif", claim_key("owns", "a", "b", "gleif"))]
    assert "ScraperSource" not in {r["name"] for r in it_db.run_sql("SELECT name FROM schema:types")}
    assert heal(apply=True)["sources"] == 0                                              # idempotent
    # and the writer now lands on the healed node
    from app.scraper.graph_writer import _ensure_source
    assert _ensure_source("GLEIF", "https://www.gleif.org", 92) == "gleif"
    assert it_db.run_sql("SELECT count(*) AS n FROM Source WHERE name = 'GLEIF'")[0]["n"] == 1
