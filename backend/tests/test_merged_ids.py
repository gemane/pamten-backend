"""
Forwarding addresses for ids a merge folded away.

A merge deletes one node, and its id is not private bookkeeping: it lives in
shared links, in client caches, and in federation peers' copies of our data. A
peer that pulled the losing id and pulls again would find nothing and recreate
the duplicate the merge just removed.

Stored as an indexed MergedId vertex rather than a list on the survivor —
`also_known_ids CONTAINS $id` cannot use an index and would scan the whole Entity
type (4.2M rows on the dev database) on every by-id miss.
"""
from unittest.mock import MagicMock

import pytest

import app.merged_ids as merged_ids
from app.merged_ids import record_merge, resolve_current_id

_REAL_REPOINT = merged_ids.repoint_references


@pytest.fixture(autouse=True)
def repointed(monkeypatch):
    """`record_merge` re-points the stored references (flags, pins, …) through
    SQL; here that is recorded, not run — the SQL itself is tested below and
    against a real database in tests/integration/test_merge_repoints_references_it.py."""
    calls: list[tuple] = []
    monkeypatch.setattr(merged_ids, "repoint_references",
                        lambda old, new: calls.append((old, new)) or 0)
    return calls


class _Session:
    """Fake session backed by a dict of old_id -> new_id."""

    def __init__(self, redirects: dict | None = None):
        self.redirects = dict(redirects or {})
        self.writes: list[tuple] = []

    def run(self, cypher, **params):
        result = MagicMock()
        if "MATCH (m:MergedId {old_id: $id})" in cypher:
            new_id = self.redirects.get(params["id"])
            result.single.return_value = {"new_id": new_id} if new_id else None
        else:
            self.writes.append((cypher, params))
            result.single.return_value = None
        return result


class TestResolveCurrentId:
    def test_returns_none_for_an_id_that_was_never_merged(self):
        assert resolve_current_id(_Session(), "unknown") is None

    def test_follows_a_single_hop(self):
        s = _Session({"old": "new"})
        assert resolve_current_id(s, "old") == "new"

    def test_follows_a_chain_written_by_an_older_version(self):
        # Chains are collapsed at write time, but a row written before that (or
        # by hand) must still resolve rather than land on a deleted node.
        s = _Session({"a": "b", "b": "c"})
        assert resolve_current_id(s, "a") == "c"

    def test_bails_out_of_a_cycle(self):
        # Malformed data must not spin forever.
        s = _Session({"a": "b", "b": "a"})
        assert resolve_current_id(s, "a") is None

    def test_ignores_empty_input(self):
        assert resolve_current_id(_Session(), "") is None
        assert resolve_current_id(_Session(), None) is None


class TestRecordMerge:
    def test_writes_a_forwarding_row(self):
        s = _Session()
        record_merge(s, old_id="dup", new_id="keep", kind="Person")
        merge_writes = [(c, p) for c, p in s.writes if "MERGE (m:MergedId" in c]
        assert len(merge_writes) == 1
        params = merge_writes[0][1]
        assert params["old"] == "dup" and params["new"] == "keep"
        assert params["kind"] == "Person"

    def test_repoints_existing_redirects_to_the_new_survivor(self):
        # A→B then B→C must leave A→C, so a lookup stays one hop and never lands
        # on a node that was itself merged away.
        s = _Session()
        record_merge(s, old_id="B", new_id="C")
        repoint = [(c, p) for c, p in s.writes if "MATCH (m:MergedId {new_id: $old})" in c]
        assert len(repoint) == 1
        assert repoint[0][1] == {"old": "B", "new": "C", "now": repoint[0][1]["now"]}

    def test_ignores_a_self_merge(self):
        s = _Session()
        record_merge(s, old_id="same", new_id="same")
        assert s.writes == []

    def test_ignores_missing_ids(self):
        s = _Session()
        record_merge(s, old_id="", new_id="keep")
        record_merge(s, old_id="dup", new_id="")
        assert s.writes == []

    def test_the_stored_references_follow_the_forwarding_row(self, repointed):
        """A flag, a pin, a suppression or a scrape run that named the loser
        names the survivor afterwards — the merge paths never did this until
        2026-10-24, only the rename did."""
        record_merge(_Session(), old_id="dup", new_id="keep")
        assert repointed == [("dup", "keep")]

    def test_a_self_merge_repoints_nothing(self, repointed):
        record_merge(_Session(), old_id="same", new_id="same")
        assert repointed == []

    def test_the_sql_path_repoints_too(self, repointed, monkeypatch):
        monkeypatch.setattr("app.db.arcadedb.run_command", lambda *a, **k: [])
        merged_ids.record_merge_sql("dup", "keep", kind="Entity")
        assert repointed == [("dup", "keep")]

    def test_a_failed_repoint_does_not_fail_the_merge(self, monkeypatch):
        def boom(old, new):
            raise RuntimeError("db away")
        monkeypatch.setattr(merged_ids, "repoint_references", boom)
        s = _Session()
        record_merge(s, old_id="dup", new_id="keep")       # no raise
        assert any("MERGE (m:MergedId" in c for c, _ in s.writes)


class TestRepointReferences:
    """The SQL behind it: one indexed UPDATE per (table, column) of
    REFERENCE_COLUMNS, and the moved rows summed from what ArcadeDB reports."""

    @pytest.fixture(autouse=True)
    def real_function(self, monkeypatch):
        # the module fixture replaced it; this class tests the real one
        monkeypatch.setattr(merged_ids, "repoint_references", _REAL_REPOINT)

    def test_every_reference_column_is_updated_and_the_counts_add_up(self, monkeypatch):
        issued = []

        def fake_run_sql(sql, params=None):
            issued.append((sql, params))
            return [{"count": 2}] if "Flag" in sql else [{"count": 0}]
        monkeypatch.setattr("app.db.arcadedb.run_sql", fake_run_sql)
        moved = merged_ids.repoint_references("dup", "keep")
        assert moved == 6                                  # three Flag columns × 2
        assert [s for s, _ in issued] == [
            "UPDATE Flag SET from_id = :new WHERE from_id = :old",
            "UPDATE Flag SET to_id = :new WHERE to_id = :old",
            "UPDATE Flag SET node_id = :new WHERE node_id = :old",
            "UPDATE Suppression SET from_id = :new WHERE from_id = :old",
            "UPDATE Suppression SET to_id = :new WHERE to_id = :old",
            "UPDATE Suppression SET node_id = :new WHERE node_id = :old",
            "UPDATE Pin SET from_id = :new WHERE from_id = :old",
            "UPDATE Pin SET to_id = :new WHERE to_id = :old",
            "UPDATE ScrapeRun SET entity_id = :new WHERE entity_id = :old",
        ]
        assert all(p == {"new": "keep", "old": "dup"} for _, p in issued)

    def test_nothing_runs_for_a_self_merge_or_a_missing_id(self, monkeypatch):
        monkeypatch.setattr("app.db.arcadedb.run_sql", lambda *a, **k: pytest.fail("ran SQL"))
        assert merged_ids.repoint_references("same", "same") == 0
        assert merged_ids.repoint_references("", "keep") == 0


class TestHealMergedReferences:
    """The one-off for the merges that ran before the re-point existed."""

    ROWS = [{"old_id": "a", "new_id": "k"}, {"old_id": "b", "new_id": "k"},
            {"old_id": "k", "new_id": "k"}]

    def _fake(self, stale_for):
        issued = []

        def run_sql(sql, params=None):
            issued.append((sql, params))
            if sql.startswith("SELECT old_id"):
                return self.ROWS
            if sql.startswith("SELECT count"):
                return [{"n": stale_for.get((sql.split(" FROM ")[1].split(" ")[0], params["old"]), 0)}]
            return [{"count": 1}]
        return run_sql, issued

    def test_a_dry_run_counts_the_stale_rows_and_writes_nothing(self, monkeypatch):
        run_sql, issued = self._fake({("Flag", "a"): 3, ("Pin", "b"): 1})
        monkeypatch.setattr("app.db.arcadedb.run_sql", run_sql)
        res = merged_ids.heal_merged_references(apply=False)
        assert res == {"redirects": 2, "stale": 3 * 3 + 1 * 2, "moved": 0, "applied": False}
        assert not any(s.startswith("UPDATE") for s, _ in issued)

    def test_apply_updates_only_where_something_is_stale(self, monkeypatch):
        run_sql, issued = self._fake({("Flag", "a"): 3})
        monkeypatch.setattr("app.db.arcadedb.run_sql", run_sql)
        res = merged_ids.heal_merged_references(apply=True)
        updates = [(s, p) for s, p in issued if s.startswith("UPDATE")]
        assert [s for s, _ in updates] == ["UPDATE Flag SET from_id = :new WHERE from_id = :old",
                                           "UPDATE Flag SET to_id = :new WHERE to_id = :old",
                                           "UPDATE Flag SET node_id = :new WHERE node_id = :old"]
        assert all(p == {"new": "k", "old": "a"} for _, p in updates)
        assert res["moved"] == 3 and res["applied"] is True


class TestForwardingMap:
    """The importers' cached forwarding map — many-into-one and chains, the
    cases raised when we built the fix: does it hold for several merged ids?"""

    def _load(self, monkeypatch, rows):
        import app.merged_ids as m
        m.invalidate_forwarding_cache()
        monkeypatch.setattr("app.db.arcadedb.run_sql", lambda *a, **k: rows)
        return m

    def test_several_ids_merged_into_one_survivor(self, monkeypatch):
        m = self._load(monkeypatch, [
            {"old_id": "lei:A", "new_id": "keep"},
            {"old_id": "lei:B", "new_id": "keep"},
            {"old_id": "chpsc:C", "new_id": "keep"},
        ])
        assert m.canonical_id("lei:A") == "keep"
        assert m.canonical_id("lei:B") == "keep"
        assert m.canonical_id("chpsc:C") == "keep"
        assert m.canonical_id("lei:D") == "lei:D"   # never merged → unchanged

    def test_a_chain_collapses_to_the_terminal_survivor(self, monkeypatch):
        m = self._load(monkeypatch, [
            {"old_id": "A", "new_id": "B"},
            {"old_id": "B", "new_id": "C"},
        ])
        assert m.canonical_id("A") == "C"   # not the intermediate B
        assert m.canonical_id("B") == "C"

    def test_a_cycle_is_survived_not_spun(self, monkeypatch):
        m = self._load(monkeypatch, [
            {"old_id": "X", "new_id": "Y"},
            {"old_id": "Y", "new_id": "X"},
        ])
        # bails at the cycle rather than looping; the exact terminal is
        # undefined but the call must return and not hang
        assert m.canonical_id("X") in ("X", "Y")

    def test_a_failed_load_fails_open_to_no_forwarding(self, monkeypatch):
        import app.merged_ids as m
        m.invalidate_forwarding_cache()
        def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr("app.db.arcadedb.run_sql", boom)
        assert m.canonical_id("lei:A") == "lei:A"   # unchanged, no crash
