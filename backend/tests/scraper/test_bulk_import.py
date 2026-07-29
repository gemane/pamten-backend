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
        # only Entity/Person are touched
        assert all(n.startswith("Entity[") or n.startswith("Person[") for n in names)
