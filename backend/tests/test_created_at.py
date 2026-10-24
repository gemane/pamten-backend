"""`created_at` on every Entity and Person (app.created_at, 2026-10-24).

Three guarantees: every creation site stamps it, an UPSERT and a merge keep
the earlier one, and the backfill dates old nodes only from what was recorded.
"""
import pathlib
import re
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: Every CREATE of an Entity or Person in the application, as a regex over the
#: source. A new writer that creates nodes some other way must be added here —
#: and must stamp created_at.
_CREATE = re.compile(r"CREATE \((?:\w+)?:(?:Entity|Person)\s*\{(?P<props>.*?)\}\)", re.S)


def _creation_sites():
    for path in sorted(APP.rglob("*.py")):
        for m in _CREATE.finditer(path.read_text()):
            yield str(path.relative_to(APP)), m.group("props")


class TestEveryCreationSiteStampsIt:
    def test_the_list_is_not_empty(self):
        assert len(list(_creation_sites())) >= 8      # 2 graph_writer, 2 runner, 2 routers, 2 federation

    def test_every_literal_create_carries_created_at(self):
        missing = [f for f, props in _creation_sites() if "created_at" not in props]
        assert missing == []

    def test_every_node_upsert_coalesces_it(self):
        """The two UPSERT creators (the bulk writer, the GLEIF delta's placeholder)
        set it only where empty — a re-import is not a birth."""
        for rel in ("scraper/bulk_import.py", "scraper/gleif_incremental.py"):
            text = (APP / rel).read_text()
            assert "created_at = COALESCE(created_at, :" in text, rel


class TestScrapePathStampsIt:
    """The real writers, DB mocked on the new-node path, the CREATE captured."""

    def _create_query(self, module_name, invoke):
        session = MagicMock()
        run_mock = MagicMock()
        run_mock.single.return_value = None
        session.run.return_value = run_mock

        @contextmanager
        def _ctx():
            yield session
        with patch(f"app.scraper.{module_name}.db.get_session", _ctx):
            invoke()
        for call in session.run.call_args_list:
            if "CREATE (" in str(call.args[0]):
                return str(call.args[0]), call.kwargs
        pytest.fail("no CREATE issued")

    def test_wikidata_entity_and_person(self):
        from app.scraper import runner
        q, kw = self._create_query("runner", lambda: runner._upsert_entity("Acme", "company", "US", 2000, None, None, "Q1"))
        assert "created_at: $created_at" in q and kw["created_at"].endswith("+00:00")
        q, kw = self._create_query("runner", lambda: runner._upsert_person("Ada L", None, None, "Q2"))
        assert "created_at: $created_at" in q and kw["created_at"]

    def test_name_keyed_entity_and_person(self):
        from app.scraper import graph_writer
        q, kw = self._create_query("graph_writer", lambda: graph_writer._upsert_entity_by_name("Acme Ltd"))
        assert "created_at: $created_at" in q and kw["created_at"]
        q, kw = self._create_query("graph_writer", lambda: graph_writer._upsert_person_by_name("Jane Doe"))
        assert "created_at: $created_at" in q and kw["created_at"]


class TestBulkWriter:
    def test_every_node_flush_sets_it_once(self):
        from app.scraper.bulk_import import _BatchWriter
        flushes: list = []
        with patch("app.scraper.bulk_import.run_sqlscript",
                   side_effect=lambda script, params=None: flushes.append((script, params))):
            b = _BatchWriter(batch_size=100)
            b.entity("lei:1", {"name": "A"})
            b.entity("lei:2", {"name": "B"})
            b.person("p:1", {"full_name": "C"})
            b.flush()
        script = "\n".join(s for s, _ in flushes)
        assert script.count("created_at = COALESCE(created_at, :created_at__") == 3   # both labels
        entity_params = flushes[0][1]
        assert entity_params["created_at__0"] == entity_params["created_at__1"]
        assert "created_at = :" not in script            # never a plain overwrite


class TestMergeKeepsTheEarlier:
    def test_entity_merge_takes_the_older_birth(self):
        from app.scraper.maintenance import _merge_entity_props
        keep = {"id": "k", "name": "A", "created_at": "2026-03-01T00:00:00+00:00"}
        dead = {"id": "d", "name": "A", "created_at": "2025-01-01T00:00:00+00:00"}
        assert _merge_entity_props(keep, dead)["created_at"] == "2025-01-01T00:00:00+00:00"
        assert "created_at" not in _merge_entity_props(dead, keep)     # keep already the older
        assert _merge_entity_props({"id": "k", "name": "A"}, dead)["created_at"] == dead["created_at"]
        assert "created_at" not in _merge_entity_props(keep, {"id": "d", "name": "A"})

    def test_the_rule_is_decided_in_python_not_cypher(self):
        """ArcadeDB's Cypher coerces a datetime-looking string when it compares
        it (`$a < e.created_at` is null), so the earlier-of-two lives in Python
        and both merges call it."""
        import inspect
        from app.created_at import earliest
        from app.routers import persons
        from app.scraper import maintenance
        assert earliest("2026-03-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00") == "2025-01-01T00:00:00+00:00"
        assert earliest(None, "2025-01-01T00:00:00+00:00") == "2025-01-01T00:00:00+00:00"
        assert earliest(None, None) is None
        assert "earliest(" in inspect.getsource(persons._earliest_birth)
        assert "earliest(" in inspect.getsource(maintenance._merge_entity_props)
        assert "< keep.created_at" not in inspect.getsource(persons.merge_person_records)


class TestBackfill:
    ROWS = [{"id": "a", "rid": "#1:0", "created_at": None, "last_scraped_at": None},
            {"id": "b", "rid": "#1:1", "created_at": "2026-01-01", "last_scraped_at": None},
            {"id": "c", "rid": "#1:2", "created_at": None, "last_scraped_at": "2026-05-05T00:00:00+00:00"},
            {"id": "d", "rid": "#1:3", "created_at": None, "last_scraped_at": None},
            {"id": "e", "rid": "#1:4", "created_at": None, "last_scraped_at": "2026-05-05T00:00:00+00:00"}]
    CLAIMS = {"to_id": [{"i": "a", "t": "2025-06-01T00:00:00+00:00"}, {"i": "e", "t": "2024-01-01T00:00:00+00:00"}],
              "from_id": [{"i": "a", "t": "2025-02-01T00:00:00+00:00"}]}

    def _run(self, monkeypatch, apply):
        from app import created_at as m
        scripts = []

        def fake_run_sql(sql, params=None):
            col = "to_id" if "to_id AS i" in sql else "from_id"
            assert set(params["ids"]) == {"a", "c", "d", "e"}       # only the undated are asked about
            return self.CLAIMS[col]
        monkeypatch.setattr("app.db.paging.iter_id_pages", lambda label, **k: iter([self.ROWS]) if label == "Entity" else iter([]))
        monkeypatch.setattr(m, "run_sql", fake_run_sql)
        monkeypatch.setattr(m, "run_sqlscript", lambda s, p: scripts.append((s, p)) or [])
        return m.backfill(apply=apply), scripts

    def test_oldest_claim_beats_the_scrape_date_and_nothing_is_guessed(self, monkeypatch):
        res, scripts = self._run(monkeypatch, apply=True)
        c = res["Entity"]
        assert (c["scanned"], c["missing"], c["from_claims"], c["from_scrape"], c["unknown"], c["written"]) == (5, 4, 2, 1, 1, 3)
        (script, params), = scripts
        assert script.splitlines() == ["UPDATE #1:0 SET created_at = :v0;", "UPDATE #1:2 SET created_at = :v1;",
                                       "UPDATE #1:4 SET created_at = :v2;"]
        assert params == {"v0": "2025-02-01T00:00:00+00:00",     # the older of a's two claim ends
                          "v1": "2026-05-05T00:00:00+00:00",     # c: no claim, its own last scrape
                          "v2": "2024-01-01T00:00:00+00:00"}     # e has both: the claim wins
        assert res["Person"]["scanned"] == 0

    def test_a_dry_run_counts_and_writes_nothing(self, monkeypatch):
        res, scripts = self._run(monkeypatch, apply=False)
        assert res["Entity"]["missing"] == 4 and res["Entity"]["written"] == 0 and res["applied"] is False
        assert scripts == []
