"""Unit tests for the GLEIF LEI-CDF succession parsing (DB not involved).

The end-to-end write (batch upsert + SUCCEEDED_BY edge) is covered against a real
ArcadeDB in tests/integration/test_gleif_succession_it.py."""

from app.scraper.gleif_succession import (
    _v, _legal_name, _successor_leis, _pairs_from_record,
)


class TestUnwrap:
    def test_unwraps_dollar_wrapped_scalar(self):
        assert _v({"$": "5493001KJTIIGC8Y1R12"}) == "5493001KJTIIGC8Y1R12"

    def test_plain_and_empty_and_none(self):
        assert _v("ABC") == "ABC"
        assert _v({"$": "  "}) is None       # whitespace-only → None
        assert _v(None) is None
        assert _v({}) is None


class TestLegalName:
    def test_reads_entity_legal_name(self):
        rec = {"Entity": {"LegalName": {"$": "Twitter, Inc."}}}
        assert _legal_name(rec) == "Twitter, Inc."

    def test_missing_entity_or_name(self):
        assert _legal_name({}) is None
        assert _legal_name({"Entity": {}}) is None


class TestSuccessorLeis:
    def test_repeating_successor_array(self):
        rec = {"Entity": {"SuccessorEntity": [
            {"SuccessorLEI": {"$": "AAA"}},
            {"SuccessorLEI": {"$": "BBB"}},
        ]}}
        assert _successor_leis(rec) == ["AAA", "BBB"]

    def test_successor_by_name_only_is_ignored(self):
        # An entry with only SuccessorEntityName (no LEI) can't be linked.
        rec = {"Entity": {"SuccessorEntity": [{"SuccessorEntityName": {"$": "X Corp."}}]}}
        assert _successor_leis(rec) == []

    def test_no_successor(self):
        assert _successor_leis({"Entity": {}}) == []
        assert _successor_leis({}) == []


class TestPairsFromRecord:
    def test_merged_record_yields_pair(self):
        rec = {"LEI": {"$": "PRED123"},
               "Registration": {"RegistrationStatus": {"$": "MERGED"}},
               "Entity": {"SuccessorEntity": [{"SuccessorLEI": {"$": "SUCC456"}}]}}
        assert _pairs_from_record(rec) == [("PRED123", "SUCC456")]

    def test_duplicate_status_also_yields_pair(self):
        rec = {"LEI": {"$": "DUP1"},
               "Registration": {"RegistrationStatus": {"$": "DUPLICATE"}},
               "Entity": {"SuccessorEntity": [{"SuccessorLEI": {"$": "KEEP1"}}]}}
        assert _pairs_from_record(rec) == [("DUP1", "KEEP1")]

    def test_active_record_yields_nothing(self):
        rec = {"LEI": {"$": "ACTIVE"},
               "Registration": {"RegistrationStatus": {"$": "ISSUED"}},
               "Entity": {"LegalName": {"$": "Acme"}}}
        assert _pairs_from_record(rec) == []

    def test_self_reference_dropped(self):
        rec = {"LEI": {"$": "SELF"},
               "Entity": {"SuccessorEntity": [{"SuccessorLEI": {"$": "SELF"}}]}}
        assert _pairs_from_record(rec) == []

    def test_no_lei(self):
        assert _pairs_from_record({"Entity": {"SuccessorEntity": [{"SuccessorLEI": {"$": "X"}}]}}) == []


class TestTheSubsetScan:
    """Findings item 85(b): the curated import reads only the successions that
    touch its companies — the record's own LEI, or a successor it names — from
    the golden copy's bytes, parsing only the hits. The file is pretty-printed
    like GLEIF's; `_LEI_RECORD_RE` finds each record start."""
    A, B, C, T2, D, E, F, G = ("AAAAAAAAAAAAAAAAAAA1", "BBBBBBBBBBBBBBBBBBB2", "CCCCCCCCCCCCCCCCCCC3",
                               "TTTTTTTTTTTTTTTTTTT4", "DDDDDDDDDDDDDDDDDDD5", "EEEEEEEEEEEEEEEEEEE6",
                               "FFFFFFFFFFFFFFFFFFF7", "GGGGGGGGGGGGGGGGGGG8")

    @staticmethod
    def _record(lei, name, successors=(), status="ISSUED", by_name=None):
        succ = [{"SuccessorLEI": {"$": s}} for s in successors]
        if by_name:
            succ.append({"SuccessorEntityName": {"$": by_name}})
        rec = {"LEI": {"$": lei}, "Entity": {"LegalName": {"$": name}},
               "Registration": {"RegistrationStatus": {"$": status}}}
        if succ:
            rec["Entity"]["SuccessorEntity"] = succ
        return rec

    def _file(self, hit_last=False):
        import io
        import json
        recs = [self._record(self.A, "A (ours, merged into B)", [self.B], "MERGED"),
                self._record(self.C, "C (merged into ours, T2)", [self.T2], "RETIRED"),
                self._record(self.D, "D (merged into E, neither ours)", [self.E], "MERGED"),
                self._record(self.F, "F (ours, alive)"),
                self._record(self.G, "G (successor by name only)", by_name="Somebody Else"),
                self._record(self.T2, "T2 (ours)"), self._record(self.B, "B"), self._record(self.E, "E")]
        if hit_last:
            recs.append(recs.pop(1))                                  # C closes the file
        return io.BytesIO(json.dumps({"records": recs}, indent=2).encode())

    def _scan(self, targets, hit_last=False, **kw):
        from app.scraper.gleif_succession import _iter_succession_records
        return [r["LEI"]["$"] for r in _iter_succession_records(self._file(hit_last), targets, **kw)]

    def test_a_hit_that_closes_the_file_is_found(self):
        # the last record has no successor record after it to bound it: it is read at EOF
        assert self._scan({self.T2}, hit_last=True) == [self.C]
        assert self._scan({self.T2}, hit_last=True, chunk_size=64) == [self.C]

    def test_records_touching_the_subset_as_predecessor_or_successor(self):
        assert self._scan({self.A, self.T2, self.F}) == [self.A, self.C]

    def test_a_tiny_chunk_size_splits_records_and_finds_the_same(self):
        for size in (7, 64, 300):
            assert self._scan({self.A, self.T2, self.F}, chunk_size=size) == [self.A, self.C], size

    def test_nothing_of_ours_means_nothing(self):
        assert self._scan({self.B}) == [self.A]                     # B is only a successor: A's record
        assert self._scan({"ZZZZZZZZZZZZZZZZZZZ9"}) == []

    def test_the_import_writes_the_hits_and_emits_the_new_endpoints(self, tmp_path):
        from unittest.mock import patch
        from app.scraper import gleif_succession as gs
        path = tmp_path / "lei2.json"
        path.write_bytes(self._file().getvalue())
        out = tmp_path / "new.txt"
        written = []
        with patch.object(gs, "_write", side_effect=lambda pairs, *a: written.extend(pairs) or 2):
            result = gs.import_lei_cdf_succession(str(path), "gleif", 92, only_leis={self.A, self.T2, self.F},
                                                  emit_leis_path=str(out))
        assert written == [(self.A, self.B), (self.C, self.T2)]
        assert result == {"records": 2, "pairs": 2, "nodes": 2, "errors": 0, "emitted": 2}
        assert out.read_text().split() == sorted([self.B, self.C])   # the ends the subset lacked, not T2 or A

    def test_without_a_subset_every_record_is_read_and_nothing_emitted(self, tmp_path):
        from unittest.mock import patch
        from app.scraper import gleif_succession as gs
        path = tmp_path / "lei2.json"
        path.write_bytes(self._file().getvalue())
        with patch.object(gs, "_write", return_value=6):
            result = gs.import_lei_cdf_succession(str(path), "gleif", 92)
        assert result == {"records": 8, "pairs": 3, "nodes": 6, "errors": 0}
