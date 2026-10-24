"""Per-source data modes: a claims-only source may assert but not draw.

Unit level: the endpoints and the cached source-id → mode mapping. The write
path (claims recorded, edges withheld, sweep) is driven against a real
database in tests/integration/test_data_mode_it.py.
"""
from unittest.mock import patch

import pytest

from app.scraper import sources as src_mod
from app.scraper.sources import edge_writes_suppressed


@pytest.fixture(autouse=True)
def _fresh_cache():
    src_mod._MODE_CACHE["at"] = None
    src_mod._MODE_CACHE["by_source_id"] = {}
    yield
    src_mod._MODE_CACHE["at"] = None
    src_mod._MODE_CACHE["by_source_id"] = {}


class TestModeEndpoint:
    def test_set_and_serve(self, client, make_token):
        with patch.object(src_mod, "_ensure_sources"), \
             patch.object(src_mod.db, "get_session") as gs:
            rec = {"data_mode": "claims_only"}
            session = type("S", (), {"run": lambda self, *a, **k: type(
                "R", (), {"single": lambda s2: rec})()})()
            gs.return_value = type("C", (), {"__enter__": lambda s2: session,
                                             "__exit__": lambda *a: False})()
            r = client.patch("/v1/scraper/sources/wikidata/mode?mode=claims_only",
                             headers={"Authorization": f"Bearer {make_token(role='admin')}"})
        assert r.status_code == 200
        assert r.json() == {"name": "wikidata", "data_mode": "claims_only"}

    def test_unknown_mode_is_422_unknown_source_404(self, client, make_token):
        h = {"Authorization": f"Bearer {make_token(role='admin')}"}
        assert client.patch("/v1/scraper/sources/wikidata/mode?mode=hidden",
                            headers=h).status_code == 422
        assert client.patch("/v1/scraper/sources/nope/mode?mode=full",
                            headers=h).status_code == 404

    def test_admin_only(self, client, make_token):
        r = client.patch("/v1/scraper/sources/wikidata/mode?mode=full",
                         headers={"Authorization": f"Bearer {make_token(role='contributor')}"})
        assert r.status_code == 403


class TestEdgeWritesSuppressed:
    def test_reads_the_mode_by_the_source_node_id(self):
        with patch.object(src_mod, "_mode_by_source_id",
                          return_value={"src-uuid-1": "claims_only"}):
            assert edge_writes_suppressed("src-uuid-1") is True
            assert edge_writes_suppressed("src-uuid-2") is False
            assert edge_writes_suppressed(None) is False

    def test_an_empty_cache_on_a_freshly_booted_machine_still_loads(self):
        # time.monotonic() counts from boot. With the old 0.0 empty-sentinel,
        # a fresh container (CI, a new Render instance) had now - 0.0 < TTL,
        # so the EMPTY cache read as fresh and nothing ever loaded for the
        # first minute of process life — the flake that failed CI twice.
        with patch.object(src_mod, "_mode_by_source_id", wraps=src_mod._mode_by_source_id), \
             patch.object(src_mod.db, "get_session", side_effect=RuntimeError("down")) as gs, \
             patch("time.monotonic", return_value=45.0):
            src_mod.edge_writes_suppressed("x")
        assert gs.call_count == 1, "the empty cache must trigger a load, uptime be damned"

    def test_a_failed_lookup_fails_open_and_is_cached(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("db down")

        with patch.object(src_mod.db, "get_session", side_effect=boom):
            assert edge_writes_suppressed("x") is False
            assert edge_writes_suppressed("y") is False
        assert calls["n"] == 1, ("the failure must be cached for the TTL — "
                                 "one failed read per minute, not one per edge")


class TestSweepEndpoint:
    def test_confirm_must_echo_the_name(self, client, make_token):
        h = {"Authorization": f"Bearer {make_token(role='admin')}"}
        r = client.post("/v1/scraper/sources/wikidata/sweep-edges?confirm=oops",
                        headers=h)
        assert r.status_code == 422

    def test_admin_only(self, client, make_token):
        r = client.post("/v1/scraper/sources/wikidata/sweep-edges?confirm=wikidata",
                        headers={"Authorization": f"Bearer {make_token(role='contributor')}"})
        assert r.status_code == 403


class _Session:
    """A session whose `run` answers every read of the Source nodes with the
    same rows and records every call — enough for the catalogue reads and
    the seed MERGE (one node type since 2026-10-24: a Source node carries
    its switches; `source_rows` are what `MATCH (s:Source)` returns)."""

    def __init__(self, source_rows):
        self.source_rows, self.calls = source_rows, []

    def run(self, stmt, **params):
        self.calls.append((stmt, params))
        if "(s:Source)" in stmt and "MERGE" not in stmt:
            return list(self.source_rows)
        return []


def _with_session(session):
    return patch.object(src_mod.db, "get_session", return_value=type(
        "C", (), {"__enter__": lambda s2: session, "__exit__": lambda *a: False})())


class TestConfiguredDefaultMode:
    """SCRAPER_WIKIDATA_DATA_MODE: the mode a fresh database seeds Wikidata
    with, and the mode it runs in until its row says otherwise. A drop-and-
    rebuild of the dev database silently put Wikidata back in full mode once
    (2026-10-06); the env var is what keeps the choice across the next one."""

    def test_only_wikidata_reads_the_setting(self, monkeypatch):
        monkeypatch.setattr(src_mod.settings, "SCRAPER_WIKIDATA_DATA_MODE", "claims_only")
        assert src_mod.default_data_mode("wikidata") == "claims_only"
        assert src_mod.default_data_mode("sec_edgar") == "full"
        assert src_mod.default_data_mode("bods_gleif") == "full"

    def test_claims_only_is_the_built_in_default(self, monkeypatch):
        from app.config import Settings
        built_in = Settings.model_fields["SCRAPER_WIKIDATA_DATA_MODE"].default
        assert built_in == "claims_only"
        monkeypatch.setattr(src_mod.settings, "SCRAPER_WIKIDATA_DATA_MODE", built_in)
        assert src_mod.default_data_mode("wikidata") == "claims_only", \
            "with no override the default must be the strict one"
        assert src_mod.default_data_mode("sec_edgar") == "full"

    def test_full_must_be_asked_for_and_unknown_values_read_as_claims_only_with_a_warning(
            self, monkeypatch, caplog):
        monkeypatch.setattr(src_mod.settings, "SCRAPER_WIKIDATA_DATA_MODE", " Full ")
        assert src_mod.default_data_mode("wikidata") == "full", "case and spaces forgiven"
        monkeypatch.setattr(src_mod.settings, "SCRAPER_WIKIDATA_DATA_MODE", "")
        assert src_mod.default_data_mode("wikidata") == "claims_only", "empty = default"
        monkeypatch.setattr(src_mod.settings, "SCRAPER_WIKIDATA_DATA_MODE", "claims")
        with caplog.at_level("WARNING"):
            assert src_mod.default_data_mode("wikidata") == "claims_only", \
                "a typo must not widen what Wikidata may draw"
        assert "SCRAPER_WIKIDATA_DATA_MODE" in caplog.text

    def test_a_fresh_database_is_seeded_with_the_configured_mode(self, monkeypatch):
        monkeypatch.setattr(src_mod.settings, "SCRAPER_WIKIDATA_DATA_MODE", "claims_only")
        session = _Session([])
        with _with_session(session):
            src_mod._ensure_sources()
        merges = [(stmt, p) for stmt, p in session.calls if "MERGE (s:Source {id: $id})" in stmt]
        seeds = {p["key"]: p["mode"] for _, p in merges}
        assert seeds["wikidata"] == "claims_only"
        assert {m for n, m in seeds.items() if n != "wikidata"} == {"full"}
        assert all("s.data_mode = COALESCE(s.data_mode, $mode)" in stmt
                   and "s.enabled = COALESCE(s.enabled, true)" in stmt for stmt, _ in merges), \
            "the seed fills only an empty switch — an admin's later flip in the UI must survive restarts"
        # one node per source, keyed by its stable id, carrying the catalogue key
        assert {p["id"] for _, p in merges} == {"wikidata", "sec-edgar", "opencorporates", "gleif", "uk-psc"}
        assert all("s.registry_key = $key" in stmt for stmt, _ in merges)

    def test_writers_obey_the_setting_before_any_row_exists(self, monkeypatch):
        # A rebuild's scrape chain writes before any request has seeded the
        # catalogue: only the Source node the writer created, no mode on it
        # — and, on a node from before the fold, not even the catalogue key:
        # the label is enough.
        monkeypatch.setattr(src_mod.settings, "SCRAPER_WIKIDATA_DATA_MODE", "claims_only")
        session = _Session([{"id": "wd-uuid", "name": "Wikidata"},
                            {"id": "sec-edgar", "name": "SEC EDGAR", "key": "sec_edgar"}])
        with _with_session(session):
            assert edge_writes_suppressed("wd-uuid") is True
            assert edge_writes_suppressed("sec-edgar") is False

    def test_a_row_with_a_mode_wins_over_the_setting_a_row_without_one_does_not(self, monkeypatch):
        monkeypatch.setattr(src_mod.settings, "SCRAPER_WIKIDATA_DATA_MODE", "claims_only")
        rows = [{"id": "wikidata", "name": "Wikidata", "key": "wikidata", "data_mode": "full"}]
        with _with_session(_Session(rows)):
            assert edge_writes_suppressed("wikidata") is False, "the admin's flip to full is the live control"
        src_mod._MODE_CACHE["at"] = None
        rows = [{"id": "wikidata", "name": "Wikidata", "key": "wikidata", "data_mode": None}]
        with _with_session(_Session(rows)):
            assert edge_writes_suppressed("wikidata") is True, "a pre-field row reads as the default"
        src_mod._MODE_CACHE["at"] = None
        with _with_session(_Session([{"id": "peer:x", "name": "Peer: X", "data_mode": None}])):
            assert edge_writes_suppressed("peer:x") is False, "outside the catalogue reads as full"

    def test_the_listing_shows_the_default_for_a_row_without_a_mode(self, client, monkeypatch):
        monkeypatch.setattr(src_mod.settings, "SCRAPER_WIKIDATA_DATA_MODE", "claims_only")
        rows = [{"name": "wikidata", "id": "wikidata", "enabled": True, "description": "", "kind": "instant", "data_mode": None},
                {"name": "sec_edgar", "id": "sec-edgar", "enabled": True, "description": "", "kind": "instant", "data_mode": None}]
        with patch.object(src_mod, "_ensure_sources"), _with_session(_Session(rows)):
            r = client.get("/v1/scraper/sources")
        assert r.status_code == 200
        assert {s["name"]: s["data_mode"] for s in r.json()} == {"wikidata": "claims_only", "sec_edgar": "full"}
        assert {s["name"]: s["id"] for s in r.json()} == {"wikidata": "wikidata", "sec_edgar": "sec-edgar"}
