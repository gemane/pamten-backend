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

from app.merged_ids import record_merge, resolve_current_id


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
