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
