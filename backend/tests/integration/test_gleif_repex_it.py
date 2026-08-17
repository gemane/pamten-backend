"""
Reporting exceptions against a real ArcadeDB.

Two properties of this importer only exist in the SQL, so a mocked session would
accept the code however it was written:

* it **never creates a node** — the writes are ``UPDATE … WHERE id`` with no
  ``UPSERT``, which is the whole reason a file describing hundreds of thousands
  of companies can be applied to a database holding a few hundred;
* it **counts the hits by asking**, because an ArcadeDB script returns only its
  last statement's result and the per-``UPDATE`` row counts never come back.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import json
import zipfile

import pytest

pytestmark = pytest.mark.integration


def _w(value):
    return {"$": value}


def _exception(lei, category="DIRECT_ACCOUNTING_CONSOLIDATION_PARENT",
               reasons=("NATURAL_PERSONS",), reference=None):
    rec = {"LEI": _w(lei), "ExceptionCategory": _w(category),
           "ExceptionReason": [_w(r) for r in reasons]}
    if reference:
        rec["ExceptionReference"] = [_w(reference)]
    return rec


def _repex_zip(tmp_path, records, name="repex.json"):
    path = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, json.dumps({"exceptions": records}))
    return str(path)


@pytest.fixture
def graph(it_db):
    """One company we hold, so "not here" means something."""
    from app.db.arcadedb import run_sql
    run_sql("UPDATE Entity SET name = 'Held Co', lei_id = 'AAAA0000000000000001' "
            "UPSERT WHERE id = 'lei:AAAA0000000000000001'")
    return run_sql


def _entity(run_sql, lei="AAAA0000000000000001"):
    rows = run_sql("SELECT FROM Entity WHERE id = :id", {"id": f"lei:{lei}"})
    return rows[0] if rows else None


class TestWhatItWrites:
    def test_the_reason_lands_on_the_company(self, graph, tmp_path):
        from app.scraper.gleif_repex import import_repex

        counts = import_repex(_repex_zip(tmp_path, [_exception("AAAA0000000000000001")]))
        assert counts["applied"] == 1 and counts["not_here"] == 0
        assert _entity(graph)["no_direct_parent_reason"] == "NATURAL_PERSONS"

    def test_both_questions_are_answered_on_one_node(self, graph, tmp_path):
        # The common case: 1,306 of 2,986 filers on a day's delta state both, and
        # the two records must not overwrite each other.
        from app.scraper.gleif_repex import import_repex

        import_repex(_repex_zip(tmp_path, [
            _exception("AAAA0000000000000001", reasons=("NATURAL_PERSONS",)),
            _exception("AAAA0000000000000001",
                       category="ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT",
                       reasons=("NO_LEI",)),
        ]))
        row = _entity(graph)
        assert row["no_direct_parent_reason"] == "NATURAL_PERSONS"
        assert row["no_ultimate_parent_reason"] == "NO_LEI"

    def test_two_records_about_one_company_become_one_write(self, graph, tmp_path):
        # Merged before the write, so the batch's ids stay distinct. That is what
        # makes the hit count meaningful: it comes from a single `IN` over those
        # ids, and a repeated id would be counted once however many records it came
        # from.
        from app.scraper.gleif_repex import import_repex

        counts = import_repex(_repex_zip(tmp_path, [
            _exception("AAAA0000000000000001"),
            _exception("AAAA0000000000000001",
                       category="ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT"),
        ]))
        assert counts["records"] == 2
        assert counts["writes"] == 1 and counts["applied"] == 1

    def test_a_reference_is_written_beside_its_reason(self, graph, tmp_path):
        from app.scraper.gleif_repex import import_repex

        import_repex(_repex_zip(tmp_path, [
            _exception("AAAA0000000000000001", reasons=("NO_LEI",),
                       reference="https://example.test/register/1"),
        ]))
        assert _entity(graph)["no_direct_parent_reason_reference"] \
            == "https://example.test/register/1"

    def test_running_it_twice_changes_nothing(self, graph, tmp_path):
        from app.scraper.gleif_repex import import_repex

        path = _repex_zip(tmp_path, [_exception("AAAA0000000000000001")])
        import_repex(path)
        first = _entity(graph)
        import_repex(path)
        assert _entity(graph) == first


class TestWhatItRefusesToCreate:
    def test_a_company_we_do_not_hold_is_not_invented(self, graph, tmp_path):
        # The property the whole design rests on. The full file covers hundreds of
        # thousands of companies; a curated database must not grow one node per
        # statement about a company it has never heard of.
        from app.scraper.gleif_repex import import_repex

        before = graph("SELECT count(*) AS n FROM Entity")[0]["n"]
        counts = import_repex(_repex_zip(tmp_path, [_exception("ZZZZ0000000000000009")]))

        assert graph("SELECT count(*) AS n FROM Entity")[0]["n"] == before
        assert counts["applied"] == 0 and counts["not_here"] == 1

    def test_a_mixed_file_applies_only_what_it_can(self, graph, tmp_path):
        from app.scraper.gleif_repex import import_repex

        counts = import_repex(_repex_zip(tmp_path, [
            _exception("AAAA0000000000000001"),
            _exception("ZZZZ0000000000000009"),
            _exception("ZZZZ0000000000000008"),
        ]))
        assert counts["applied"] == 1 and counts["not_here"] == 2
        assert _entity(graph)["no_direct_parent_reason"] == "NATURAL_PERSONS"

    def test_a_record_with_nothing_to_say_is_skipped(self, graph, tmp_path):
        from app.scraper.gleif_repex import import_repex

        counts = import_repex(_repex_zip(tmp_path, [
            {"LEI": _w("AAAA0000000000000001"),
             "ExceptionCategory": _w("DIRECT_ACCOUNTING_CONSOLIDATION_PARENT")},
        ]))
        assert counts["skipped"] == 1 and counts["applied"] == 0
        assert _entity(graph).get("no_direct_parent_reason") is None


class TestAcrossABatchBoundary:
    def test_hits_and_misses_are_counted_over_several_flushes(self, graph, tmp_path, monkeypatch):
        # The count comes from one SELECT per batch, so the arithmetic has to hold
        # when a file spans more than one — the only case a single-batch test
        # cannot see.
        from app.db.arcadedb import run_sql
        from app.scraper import gleif_repex

        monkeypatch.setattr(gleif_repex, "_BATCH", 2)
        held = [f"BBBB000000000000000{i}" for i in range(5)]
        for lei in held:
            run_sql("UPDATE Entity SET name = :n, lei_id = :lei UPSERT WHERE id = :id",
                    {"n": f"Co {lei}", "lei": lei, "id": f"lei:{lei}"})
        records = [_exception(lei) for lei in held] + \
                  [_exception(f"CCCC000000000000000{i}") for i in range(3)]

        counts = gleif_repex.import_repex(_repex_zip(tmp_path, records))
        assert counts["applied"] == 5 and counts["not_here"] == 3
