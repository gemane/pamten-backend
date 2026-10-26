"""A new node's id carries the register's key when the writer knows one
(app.node_ids, 2026-10-26); the heal renames an older database's nodes."""
from unittest.mock import MagicMock

from app.node_ids import heal_keyed_ids, is_uuid, keyed_id, mint_node_id


class TestMinting:
    def test_the_lei_outranks_the_item_outranks_the_cik(self):
        assert keyed_id("529900T8BM49AURSDO55", "Q312", "0000320193") == "lei:529900T8BM49AURSDO55"
        assert keyed_id(None, "Q312", "0000320193") == "wd:Q312"
        assert keyed_id(None, None, "0000320193") == "sec:0000320193"

    def test_a_name_only_node_gets_a_uuid(self):
        assert keyed_id(None, None, None) is None
        assert is_uuid(mint_node_id())
        assert mint_node_id(wikidata_id="Q1") == "wd:Q1"

    def test_a_malformed_key_is_not_keyed_on(self):
        """A merge key must never be minted from a value that is not one."""
        assert keyed_id("not-an-lei", None, None) is None
        assert keyed_id(None, "Apple", None) is None
        assert keyed_id(None, None, "CIK 320193") is None
        assert keyed_id(None, " Q7 ", None) == "wd:Q7"                  # whitespace tolerated, not kept

    def test_is_uuid(self):
        assert is_uuid("c75a2d35-c76f-4c91-a813-d8b001203bea")
        assert not is_uuid("lei:529900T8BM49AURSDO55") and not is_uuid("") and not is_uuid(None)


ROWS = {
    "Entity": [{"id": "c75a2d35-c76f-4c91-a813-d8b001203bea", "wikidata_id": "Q312", "sec_cik": "0000320193", "name": "Apple"},
               {"id": "11111111-1111-4111-8111-111111111111", "sec_cik": "0001301486", "name": "BRC"},
               {"id": "22222222-2222-4222-8222-222222222222", "name": "Sub Inc"},            # name-only
               {"id": "lei:529900T8BM49AURSDO55", "wikidata_id": "Q9", "name": "Keyed"}],    # already keyed
    "Person": [{"id": "33333333-3333-4333-8333-333333333333", "wikidata_id": "Q317521", "full_name": "Elon Musk"}],
}


def _patch(monkeypatch, renames):
    from app.db import paging
    from app import merged_ids
    monkeypatch.setattr(paging, "iter_id_pages", lambda label, columns="": iter([ROWS[label]]))
    monkeypatch.setattr(merged_ids, "rename_node_id", lambda session, label, old, new: renames.append((label, old, new)) or new != "sec:0001301486")
    from app.database import db
    monkeypatch.setattr(db, "get_session", lambda: MagicMock().__enter__.__self__ if False else _Session())


class _Session:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class TestHeal:
    def test_a_dry_run_counts_and_renames_nothing(self, monkeypatch):
        renames = []
        _patch(monkeypatch, renames)
        res = heal_keyed_ids(apply=False)
        assert res["Entity"] == {"scanned": 3, "moved": 2, "unkeyed": 1, "taken": 0,
                                 "sample": [("Apple", "c75a2d35-c76f-4c91-a813-d8b001203bea", "wd:Q312"),
                                            ("BRC", "11111111-1111-4111-8111-111111111111", "sec:0001301486")]}
        assert res["Person"]["moved"] == 1 and res["Person"]["sample"][0][2] == "wd:Q317521"
        assert renames == [] and res["applied"] is False

    def test_apply_renames_through_the_merge_machinery_and_reports_a_taken_key(self, monkeypatch):
        renames = []
        _patch(monkeypatch, renames)
        res = heal_keyed_ids(apply=True)
        assert renames == [("Entity", "c75a2d35-c76f-4c91-a813-d8b001203bea", "wd:Q312"),
                           ("Entity", "11111111-1111-4111-8111-111111111111", "sec:0001301486"),
                           ("Person", "33333333-3333-4333-8333-333333333333", "wd:Q317521")]
        assert (res["Entity"]["moved"], res["Entity"]["taken"], res["Entity"]["unkeyed"]) == (1, 1, 1)
        assert res["Person"]["moved"] == 1
