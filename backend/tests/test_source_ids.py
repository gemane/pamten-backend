"""Stable Source ids (app.scraper.sources / app.source_ids, 2026-10-24): the
id a source gets, the writer's id-first lookup, and the heal for a database
whose Source nodes were uuids beside ScraperSource toggle rows."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.scraper.sources import KNOWN_SOURCES, SOURCE_ID, slug_id, source_id_for, source_key_for


class TestTheIds:
    def test_every_catalogue_entry_has_a_distinct_stable_id_and_a_type(self):
        ids = [m["id"] for m in KNOWN_SOURCES.values()]
        assert len(set(ids)) == len(ids) and all(i == slug_id(i) for i in ids)
        assert all(m["type"] in ("register", "knowledge_base") for m in KNOWN_SOURCES.values())
        assert SOURCE_ID == {"wikidata": "wikidata", "sec_edgar": "sec-edgar", "open_corporates": "opencorporates",
                             "bods_gleif": "gleif", "bods_uk_psc": "uk-psc"}

    def test_a_label_or_an_id_resolves_to_its_catalogue_key(self):
        assert source_key_for("GLEIF") == "bods_gleif" and source_key_for("gleif") == "bods_gleif"
        assert source_key_for("SEC EDGAR") == "sec_edgar" and source_key_for("Peer: X") is None
        assert source_key_for(None) is None

    def test_the_id_for_a_name(self):
        assert source_id_for("GLEIF") == "gleif"
        assert source_id_for("UK PSC", "register") == "uk-psc"
        assert source_id_for("Peer: Signed", "peer") == "peer:signed"
        assert source_id_for("Wikimedia Commons") == "wikimedia-commons"
        assert source_id_for("  ***  ") == "source"

    def test_the_same_name_gives_the_same_id_everywhere(self):
        assert slug_id("Companies House — Register") == slug_id("companies house register")


class TestEnsureSource:
    """graph_writer._ensure_source: by stable id, then by name (a node keyed
    the old way keeps its id until the heal), else created under the id."""

    def _run(self, answers):
        from app.scraper import graph_writer
        session = MagicMock()
        calls = []

        def run(stmt, **params):
            calls.append((stmt, params))
            r = MagicMock()
            r.single.return_value = answers(stmt, params)
            return r
        session.run.side_effect = run

        @contextmanager
        def _ctx():
            yield session
        with patch("app.scraper.graph_writer.db.get_session", _ctx):
            sid = graph_writer._ensure_source("GLEIF", "https://www.gleif.org", 92)
        return sid, calls

    def test_a_node_under_its_stable_id_is_found_first(self):
        sid, calls = self._run(lambda stmt, p: {"id": "gleif"} if "{id: $id}" in stmt else None)
        assert sid == "gleif" and len(calls) == 1 and calls[0][1] == {"id": "gleif"}

    def test_an_old_uuid_node_is_found_by_name_and_keeps_its_id(self):
        sid, calls = self._run(lambda stmt, p: {"id": "u-u-i-d"} if "{name: $name}" in stmt else None)
        assert sid == "u-u-i-d" and len(calls) == 2
        assert not any("CREATE" in s for s, _ in calls)

    def test_a_new_source_is_created_under_the_stable_id_with_its_catalogue_key(self):
        sid, calls = self._run(lambda stmt, p: None)
        assert sid == "gleif"
        create = [(s, p) for s, p in calls if "CREATE (s:Source" in s]
        assert len(create) == 1
        assert create[0][1]["id"] == "gleif" and create[0][1]["key"] == "bods_gleif"
        assert create[0][1]["type"] == "register" and create[0][1]["name"] == "GLEIF"
        assert "registry_key: $key" in create[0][0]
