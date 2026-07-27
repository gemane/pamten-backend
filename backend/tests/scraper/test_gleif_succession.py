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
