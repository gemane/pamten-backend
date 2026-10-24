"""Stable Source ids (app.scraper.sources / app.source_ids, 2026-10-24): the
id a source gets, the writer's id-first lookup, and the heal for a database
whose Source nodes were uuids beside ScraperSource toggle rows."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


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


class TestHeal:
    SOURCES = [{"id": "aaaa-1111", "name": "GLEIF", "type": "register"},
               {"id": "wikidata", "name": "Wikidata", "type": "knowledge_base"},     # already stable
               {"id": "bbbb-2222", "name": "Peer: Signed", "type": "peer"}]
    SCRAPER = [{"name": "bods_gleif", "enabled": False, "data_mode": "full", "kind": "bulk", "description": "d"},
               {"name": "wikidata", "enabled": True, "data_mode": "claims_only", "kind": "instant", "description": "d"}]

    def _run(self, monkeypatch, apply):
        from app import source_ids as m
        issued = []

        def fake_run_sql(sql, params=None, timeout=None):
            issued.append((sql, params))
            if sql.startswith("SELECT id, name, type FROM Source"):
                return self.SOURCES
            if sql.startswith("SELECT id FROM Source"):
                return [{"id": s["id"]} for s in self.SOURCES]
            if sql.startswith("SELECT count(*)"):
                return [{"n": 3 if "Claim" in sql and params["old"] == "aaaa-1111" else 0}]
            if "FROM ScraperSource" in sql and sql.startswith("SELECT"):
                return self.SCRAPER
            if sql.startswith("UPDATE Claim SET source_id"):
                return [{"count": 3 if params["old"] == "aaaa-1111" else 0}]
            if sql.startswith("DELETE FROM ScraperSource"):
                return [{"count": 2}]
            return [{"count": 0}]
        monkeypatch.setattr(m, "run_sql", fake_run_sql)
        rekeys = []
        monkeypatch.setattr("app.claims.heal_claim_keys", lambda dry_run=False, **k: rekeys.append(dry_run) or {"rekeyed": 3})
        return m.heal(apply=apply), issued, rekeys

    def test_plan_names_only_the_nodes_whose_id_is_not_stable(self, monkeypatch):
        from app import source_ids as m
        monkeypatch.setattr(m, "run_sql", lambda sql, params=None, timeout=None: self.SOURCES)
        assert m.plan() == [{"old": "aaaa-1111", "new": "gleif", "name": "GLEIF", "key": "bods_gleif"},
                            {"old": "bbbb-2222", "new": "peer:signed", "name": "Peer: Signed", "key": None}]

    def test_apply_repoints_every_holder_rekeys_the_node_folds_the_toggles_and_rekeys_claims(self, monkeypatch):
        res, issued, rekeys = self._run(monkeypatch, apply=True)
        from app.source_ids import HOLDERS
        gleif_updates = [p for s, p in issued if s.startswith("UPDATE ") and p and p.get("old") == "aaaa-1111"]
        assert len(gleif_updates) == len(HOLDERS) + 1          # every holder, then the node itself
        assert [s for s, p in issued if s.startswith("UPDATE Source SET id")] == ["UPDATE Source SET id = :new, registry_key = :key WHERE id = :old"] * 2
        assert res["sources"] == 2 and res["holders"] == {"Claim": 3}
        assert res["toggles_moved"] == 2 and res["scraper_rows_deleted"] == 2
        folds = [p for s, p in issued if s.startswith("UPDATE Source SET enabled")]
        assert {p["id"]: (p["e"], p["m"]) for p in folds} == {"gleif": (False, "full"), "wikidata": (True, "claims_only")}
        assert any(s.startswith("DROP TYPE ScraperSource") for s, _ in issued)
        assert rekeys == [False] and res["claims"] == {"rekeyed": 3}

    def test_a_dry_run_counts_the_holders_and_writes_nothing(self, monkeypatch):
        res, issued, rekeys = self._run(monkeypatch, apply=False)
        assert res["sources"] == 2 and res["holders"] == {"Claim": 3} and res["applied"] is False
        assert not any(s.startswith(("UPDATE", "DELETE", "DROP")) for s, _ in issued)
        assert rekeys == [True]

    def test_a_taken_id_is_a_conflict_not_a_merge(self, monkeypatch):
        from app import source_ids as m
        rows = [{"id": "aaaa-1111", "name": "GLEIF", "type": "register"}, {"id": "gleif", "name": "GLEIF", "type": "register"}]
        monkeypatch.setattr(m, "run_sql", lambda sql, params=None, timeout=None:
                            rows if "FROM Source" in sql and sql.startswith("SELECT") else [{"count": 0}])
        monkeypatch.setattr("app.claims.heal_claim_keys", lambda **k: pytest.fail("no claims re-key without a change"))
        res = m.heal(apply=True)
        assert res["sources"] == 0 and [c["old"] for c in res["conflicts"]] == ["aaaa-1111"]
