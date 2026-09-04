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
    src_mod._MODE_CACHE["at"] = 0.0
    src_mod._MODE_CACHE["by_source_id"] = {}
    yield
    src_mod._MODE_CACHE["at"] = 0.0
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
    def test_maps_source_node_id_through_the_label(self):
        with patch.object(src_mod, "_mode_by_source_id",
                          return_value={"src-uuid-1": "claims_only"}):
            assert edge_writes_suppressed("src-uuid-1") is True
            assert edge_writes_suppressed("src-uuid-2") is False
            assert edge_writes_suppressed(None) is False

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
