"""
Tests for the shared bulk-import helpers in ``app.scraper.bulk_import``.

The BODS statement-processing engine was removed when the ingest migrated to the
GLEIF golden copy and Companies House snapshots; what remains here are unit tests
for the reusable plumbing those importers build on — ``_DiskMap``/``_tmp_dir``,
``_legal_form_type``, ``_BatchWriter``, ``_flush_script`` retry, the bulk-load
index list, and the ``_post_bods_import`` housekeeping hook. All DB writes are
mocked.
"""

import pytest
from unittest.mock import patch

# ── _DiskMap temp location (avoids filling a small tmpfs /tmp) ─────────────────

class TestDiskMapTmpDir:
    def test_uses_configured_tmp_dir(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.scraper.bulk_import import _DiskMap
        monkeypatch.setattr(settings, "SCRAPER_TMP_DIR", str(tmp_path))
        m = _DiskMap()
        try:
            assert m._path.startswith(str(tmp_path))   # spilled to the big disk, not /tmp
            m["k"] = "v"
            assert m["k"] == "v" and "k" in m
        finally:
            m.close()

    def test_creates_the_dir_if_missing(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.scraper.bulk_import import _tmp_dir
        target = tmp_path / "does" / "not" / "exist"
        monkeypatch.setattr(settings, "SCRAPER_TMP_DIR", str(target))
        assert _tmp_dir() == str(target)
        assert target.is_dir()


# ── _max_pct (merge two possibly-None ownership percentages) ──────────────────

class TestMaxPct:
    def test_both_none(self):
        from app.scraper.bulk_import import _max_pct
        assert _max_pct(None, None) is None

    def test_one_none_returns_the_other(self):
        from app.scraper.bulk_import import _max_pct
        assert _max_pct(None, 25.0) == 25.0
        assert _max_pct(25.0, None) == 25.0

    def test_both_present_takes_the_larger(self):
        from app.scraper.bulk_import import _max_pct
        assert _max_pct(10.0, 75.0) == 75.0
        assert _max_pct(75.0, 10.0) == 75.0
        assert _max_pct(50.0, 50.0) == 50.0

    def test_zero_is_a_value_not_absent(self):
        # 0.0 is a real percentage, not "missing" — must not be treated like None
        from app.scraper.bulk_import import _max_pct
        assert _max_pct(0.0, None) == 0.0
        assert _max_pct(0.0, 5.0) == 5.0


# ── _legal_form_type (GLEIF legal form → finer category) ──────────────────────

class TestLegalFormType:
    def test_foundation_forms(self):
        from app.scraper.bulk_import import _legal_form_type
        assert _legal_form_type("Stiftung des privaten Rechts") == "foundation"
        assert _legal_form_type("stichting") == "foundation"
        assert _legal_form_type("Fundación") == "foundation"

    def test_fund_forms(self):
        from app.scraper.bulk_import import _legal_form_type
        assert _legal_form_type("Mutual Fund-Sub Scheme") == "fund"
        assert _legal_form_type("Fonds à forme sociétale") == "fund"
        assert _legal_form_type("Statutory Trust") == "fund"

    def test_nonprofit_forms(self):
        from app.scraper.bulk_import import _legal_form_type
        assert _legal_form_type("eingetragener Verein") == "nonprofit"
        assert _legal_form_type("Association loi 1901") == "nonprofit"

    def test_plain_company_form_is_none(self):
        from app.scraper.bulk_import import _legal_form_type
        assert _legal_form_type("Gesellschaft mit beschränkter Haftung") is None
        assert _legal_form_type("Private Limited Company") is None
        assert _legal_form_type(None) is None


# ── source_statement_ids: per-statement provenance survives the name collapse ──

# ── _registered_address ───────────────────────────────────────────────────────

# ── _process_entity_statement ─────────────────────────────────────────────────

# ── _process_person_statement ─────────────────────────────────────────────────

# ── _process_relationship_statement ──────────────────────────────────────────

# ── _run_import: filter_jurisdiction and limit ────────────────────────────────

# ── _BatchWriter: batching and flush semantics ───────────────────────────────

class TestBatchWriter:
    def test_flushes_when_batch_size_reached(self):
        from app.scraper.bulk_import import _BatchWriter

        with patch("app.scraper.bulk_import.run_sqlscript") as mock_sql:
            b = _BatchWriter(batch_size=2)
            b.entity("e1", {"name": "A", "country": "GB"})
            mock_sql.assert_not_called()          # under the threshold
            b.entity("e2", {"name": "B", "country": "US"})
            assert mock_sql.called                 # threshold reached → auto-flush

    def test_nodes_flushed_before_edges(self):
        from app.scraper.bulk_import import _BatchWriter

        scripts: list = []
        with patch("app.scraper.bulk_import.run_sqlscript",
                   side_effect=lambda script, params=None: scripts.append(script)):
            b = _BatchWriter(batch_size=100)
            b.owns("e1", "Entity", "e2", {"stake_percent": 50.0})
            b.entity("e1", {"name": "A"})
            b.flush()

        # Entity upsert must be issued before the edge CREATE, so endpoints exist.
        joined = "\n---\n".join(scripts)
        assert "UPDATE Entity" in joined
        assert "CREATE EDGE OWNS" in joined
        assert joined.index("UPDATE Entity") < joined.index("CREATE EDGE OWNS")

    def test_empty_flush_issues_no_request(self):
        from app.scraper.bulk_import import _BatchWriter

        with patch("app.scraper.bulk_import.run_sqlscript") as mock_sql:
            _BatchWriter().flush()
            mock_sql.assert_not_called()


class TestBatchWriterClaims:
    """Every bulk edge write also records what that source asserted.

    Emitted by the writer rather than by its callers, so an importer cannot add
    an edge and forget the evidence — which is how provenance was lost before.
    """

    @staticmethod
    def _scripts(fn) -> str:
        from app.scraper.bulk_import import _BatchWriter

        scripts: list = []
        with patch("app.scraper.bulk_import.run_sqlscript",
                   side_effect=lambda script, params=None: scripts.append(script)):
            b = _BatchWriter(batch_size=100)
            fn(b)
            b.flush()
        return "\n---\n".join(scripts)

    def test_an_owns_edge_also_writes_a_claim(self):
        joined = self._scripts(lambda b: b.owns(
            "e1", "Entity", "e2", {"stake_percent": 50.0, "source_id": "gleif"}))
        assert "CREATE EDGE OWNS" in joined
        assert "UPDATE Claim" in joined
        assert "UPSERT WHERE claim_key" in joined

    def test_a_role_edge_also_writes_a_claim(self):
        joined = self._scripts(lambda b: b.role(
            "p1", "e1", {"role": "CEO", "source_id": "sec"}))
        assert "CREATE EDGE HAS_ROLE" in joined
        assert "UPDATE Claim" in joined

    def test_a_succession_edge_also_writes_a_claim(self):
        joined = self._scripts(lambda b: b.succeeded_by(
            "old", "new", {"source_id": "gleif"}))
        assert "CREATE EDGE SUCCEEDED_BY" in joined
        assert "UPDATE Claim" in joined

    def test_first_seen_at_is_preserved_across_re_imports(self):
        """Set with COALESCE against the stored value, so a re-import records
        when we first saw the claim rather than resetting it."""
        joined = self._scripts(lambda b: b.owns(
            "e1", "Entity", "e2", {"source_id": "gleif"}))
        assert "first_seen_at = COALESCE(first_seen_at," in joined

    def test_no_source_means_no_claim(self):
        # claim_key is (kind, from, to, source): an unsourced claim would collide
        # with every other unsourced claim about the same pair.
        joined = self._scripts(lambda b: b.owns("e1", "Entity", "e2", {"stake_percent": 50.0}))
        assert "CREATE EDGE OWNS" in joined
        assert "UPDATE Claim" not in joined


# ── Runner permission checks ──────────────────────────────────────────────────

class TestRunnerPermissions:
    def test_gleif_raises_when_master_disabled(self, monkeypatch):
        from app.scraper import runner as r

        monkeypatch.setattr(r.settings, "SCRAPER_ENABLED", False)

        with pytest.raises(PermissionError, match="SCRAPER_ENABLED"):
            r.run_import_gleif_lei_cdf("dummy.zip")

    def test_gleif_raises_when_source_flag_disabled(self, monkeypatch):
        from app.scraper import runner as r

        monkeypatch.setattr(r.settings, "SCRAPER_ENABLED", True)
        monkeypatch.setattr(r.settings, "SCRAPER_BODS_GLEIF_ENABLED", False)

        with pytest.raises(PermissionError, match="SCRAPER_BODS_GLEIF_ENABLED"):
            r.run_import_gleif_lei_cdf("dummy.zip")

class TestPostBodsImport:
    """Every BODS import auto-runs nominee flagging + edge dedup, best-effort."""

    def test_runs_nominees_and_edge_dedup(self):
        from app.scraper import runner as r
        with patch("app.scraper.maintenance.flag_nominee_entities", return_value={"flagged": 3}), \
             patch("app.scraper.maintenance.deduplicate_owns_edges", return_value={"duplicates_removed": 5}):
            out = r._post_bods_import()
        assert out == {"nominees": {"flagged": 3}, "edge_dedup": {"duplicates_removed": 5}}

    def test_best_effort_one_step_failing_still_runs_the_other(self):
        from app.scraper import runner as r
        with patch("app.scraper.maintenance.flag_nominee_entities", side_effect=RuntimeError("boom")), \
             patch("app.scraper.maintenance.deduplicate_owns_edges", return_value={"duplicates_removed": 0}):
            out = r._post_bods_import()
        assert "nominees" not in out                       # failure swallowed
        assert out["edge_dedup"] == {"duplicates_removed": 0}


# ── _flush_script: retry-with-backoff (survives transient proxy 504s) ─────────

class TestFlushRetry:
    def test_retries_then_succeeds(self):
        from app.scraper import bulk_import

        calls = {"n": 0}

        def flaky(script, params):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("ArcadeDB command failed [504]: gateway timeout")
            return [{"ok": True}]

        with patch("app.scraper.bulk_import.run_sqlscript", side_effect=flaky), \
             patch("app.scraper.bulk_import.time.sleep") as sleep:
            out = bulk_import._flush_script("UPDATE Entity ...", {"a": 1})

        assert out == [{"ok": True}]
        assert calls["n"] == 3            # failed twice, third attempt worked
        assert sleep.call_count == 2      # backed off before each retry

    def test_reraises_after_exhausting_attempts(self):
        from app.scraper import bulk_import

        with patch("app.scraper.bulk_import.run_sqlscript",
                   side_effect=RuntimeError("504")) as sql, \
             patch("app.scraper.bulk_import.time.sleep"):
            with pytest.raises(RuntimeError, match="504"):
                bulk_import._flush_script("UPDATE Entity ...", {})

        assert sql.call_count == bulk_import._FLUSH_ATTEMPTS


# ── bulk-load mode: drop secondary indexes for the load, rebuild after ────────

class TestBulkLoad:
    def test_secondary_index_list_excludes_id_and_other_types(self):
        from app.scraper.bulk_import import _bulk_load_secondary_indexes

        names = _bulk_load_secondary_indexes()
        assert "Entity[name_normalized]" in names
        assert "Person[full_name]" in names
        # never drop the id indexes the import relies on
        assert "Entity[id]" not in names
        assert "Person[id]" not in names
        # the FULL_TEXT search indexes are dropped too — maintaining them per-insert
        # under a bulk load is slow and can leave them incomplete; rebuilt at the end
        assert "Entity[search_text]" in names
        assert "Person[search_text]" in names
        # only Entity/Person are touched
        assert all(n.startswith("Entity[") or n.startswith("Person[") for n in names)

    # ── the post-load rebuild: one long CREATE per dropped index, never aborting ──
    #
    # The first full import (2026-09-21, 14M rows per type) ended with Entity
    # holding 3 of its 11 secondary indexes and Person none: the rebuild went
    # through ensure_indexes(), whose 60 s per-statement timeout fired on the
    # first 20-minute CREATE and stopped the loop.

    @staticmethod
    def _run_rebuild(catalog, fail_on=()):
        """Drive _rebuild_indexes() with a fake run_sql; returns (result, issued)
        where issued is [(statement, timeout)]."""
        from app.scraper import bulk_import
        issued: list[tuple[str, float | None]] = []

        def run_sql(cmd, params=None, timeout=None):
            issued.append((cmd, timeout))
            if cmd.startswith("SELECT name FROM schema:indexes"):
                return [{"name": n} for n in catalog]
            if any(f in cmd for f in fail_on):
                raise RuntimeError("timed out")
            return []
        with patch("app.db.arcadedb.run_sql", side_effect=run_sql), \
             patch("app.db.schema.ensure_indexes",
                   return_value={"ok": [], "failed": []}) as ens:
            res = bulk_import._rebuild_indexes()
        return res, issued, ens

    def test_every_dropped_index_is_created_with_the_long_timeout(self):
        from app.scraper.bulk_import import INDEX_BUILD_TIMEOUT, _bulk_load_secondary_indexes

        res, issued, ens = self._run_rebuild(catalog=["Entity[id]", "Person[id]"])
        creates = [(c, t) for c, t in issued if c.startswith("CREATE INDEX")]
        # one CREATE per dropped index — LSM and FULL_TEXT alike
        assert len(creates) == len(_bulk_load_secondary_indexes())
        assert all(t == INDEX_BUILD_TIMEOUT for _c, t in creates)
        assert INDEX_BUILD_TIMEOUT >= 3600           # a 14M-row build is ~25 min
        assert any("ON Entity (name_normalized) NOTUNIQUE" in c for c, _t in creates)
        assert any("ON Person (full_name) NOTUNIQUE" in c for c, _t in creates)
        assert any("ON Entity (search_text) FULL_TEXT" in c for c, _t in creates)
        assert sorted(res["ok"]) == sorted(_bulk_load_secondary_indexes())
        assert res["failed"] == []
        # the property is declared before its index, with the schema's type
        props = [c for c, _t in issued if c.startswith("CREATE PROPERTY")]
        assert any("Person.alias IF NOT EXISTS LIST" in c for c in props)
        # types + the small indexes still go through the bootstrap, after the big ones
        ens.assert_called_once()
        assert issued[-1][0].startswith("CREATE INDEX")   # the bootstrap is a mock; ours came first

    def test_a_failed_create_does_not_stop_the_rest(self):
        """The whole point: a timeout on Entity[name] used to end the rebuild."""
        from app.scraper.bulk_import import _bulk_load_secondary_indexes

        res, issued, _ens = self._run_rebuild(catalog=[], fail_on=("ON Entity (name)",))
        creates = [c for c, _t in issued if c.startswith("CREATE INDEX")]
        assert len(creates) == len(_bulk_load_secondary_indexes())
        assert [f["index"] for f in res["failed"]] == ["Entity[name]"]
        assert "Person[full_name]" in res["ok"] and "Entity[search_text]" in res["ok"]

    def test_an_index_still_in_the_catalog_is_not_created_again(self):
        res, issued, _ens = self._run_rebuild(
            catalog=["Entity[id]", "Entity[name]", "Entity[name_normalized]"])
        creates = [c for c, _t in issued if c.startswith("CREATE INDEX")]
        assert not any("ON Entity (name)" in c for c in creates)
        assert not any("ON Entity (name_normalized)" in c for c in creates)
        assert any("ON Entity (wikidata_id)" in c for c in creates)
        assert "Entity[name]" not in res["ok"]

    def test_a_fulltext_index_still_in_the_catalog_is_left_alone(self):
        """A live index is maintained by every write of the load, so it is
        complete; a REBUILD would only repeat the >1 h 45 min sub-index merge."""
        res, issued, _ens = self._run_rebuild(catalog=["Entity[search_text]"])
        assert not any(c.startswith("REBUILD INDEX") for c, _t in issued)
        creates = [c for c, _t in issued if c.startswith("CREATE INDEX")]
        assert any("ON Person (search_text) FULL_TEXT" in c for c in creates)
        assert not any("ON Entity (search_text)" in c for c in creates)
        assert "Entity[search_text]" not in res["ok"]

    def test_no_rebuild_ever_follows_a_create(self):
        """CREATE already indexed every row; the REBUILD that used to follow
        merged sub-indexes for >1 h 45 min on 14M rows and was abandoned."""
        _res, issued, _ens = self._run_rebuild(catalog=[])
        assert not any(c.startswith("REBUILD INDEX") for c, _t in issued)

    def test_an_unreadable_catalog_still_rebuilds_everything(self):
        from app.scraper import bulk_import
        from app.scraper.bulk_import import _bulk_load_secondary_indexes
        issued: list[str] = []

        def run_sql(cmd, params=None, timeout=None):
            if cmd.startswith("SELECT name FROM schema:indexes"):
                raise RuntimeError("timed out")
            issued.append(cmd)
            return []
        with patch("app.db.arcadedb.run_sql", side_effect=run_sql), \
             patch("app.db.schema.ensure_indexes", return_value={"ok": [], "failed": []}):
            res = bulk_import._rebuild_indexes()
        creates = [c for c in issued if c.startswith("CREATE INDEX")]
        assert len(creates) == len(_bulk_load_secondary_indexes())
        assert res["failed"] == []

    def test_rebuild_fulltext_issues_rebuild_per_index(self):
        from app.db import schema

        issued: list[str] = []
        with patch("app.db.schema.run_sql",
                   side_effect=lambda cmd, *a, **k: issued.append(cmd) or []):
            res = schema.rebuild_fulltext_indexes()

        assert res["failed"] == []
        assert "REBUILD INDEX `Entity[search_text]`" in issued
        assert "REBUILD INDEX `Person[search_text]`" in issued

    def test_hard_rebuild_drops_physical_and_logical_then_recreates(self):
        from app.db import schema

        # schema:indexes discovery returns a physical + logical index per type.
        catalog = [
            {"name": "Entity_0_999", "properties": [["search_text"]]},
            {"name": "Entity[search_text]", "properties": [["search_text"]]},
            {"name": "Entity_0_111", "properties": [["name"]]},        # different prop → ignored
            {"name": "Person_0_888", "properties": [["search_text"]]},
            {"name": "Person[search_text]", "properties": [["search_text"]]},
        ]

        issued: list[str] = []

        def _fake(cmd, *a, **k):
            issued.append(cmd)
            return catalog if cmd.startswith("SELECT name, properties FROM schema:indexes") else []

        with patch("app.db.schema.run_sql", side_effect=_fake):
            res = schema.rebuild_fulltext_indexes(hard=True)

        assert res["failed"] == []
        # every FULL_TEXT index (physical + logical) dropped, then re-created, then rebuilt
        assert "DROP INDEX `Entity_0_999` IF EXISTS" in issued
        assert "DROP INDEX `Entity[search_text]` IF EXISTS" in issued
        assert "DROP INDEX `Person_0_888` IF EXISTS" in issued
        assert "DROP INDEX `Entity_0_111` IF EXISTS" not in issued   # wrong property, untouched
        assert "CREATE INDEX IF NOT EXISTS ON Entity (search_text) FULL_TEXT" in issued
        assert "REBUILD INDEX `Entity[search_text]`" in issued
