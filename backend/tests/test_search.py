"""
Unit tests for the search router's pure helpers (no DB needed). The endpoint
itself is exercised end-to-end in tests/integration/test_person_profile_it.py.
"""
from app.routers.search import (
    _dedupe_positions, _dedupe_holdings, _clean, _ownership_summary,
)


def _owner(stake):
    return {"owner": {"id": "x"}, "relationship": {"stake_percent": stake}}


class TestOwnershipSummary:
    def test_free_float_is_the_residual_when_all_known(self):
        s = _ownership_summary([_owner(7.0), _owner(5.0)])
        assert s["disclosed_pct"] == 12.0
        assert s["free_float_pct"] == 88.0
        assert s["exceeds_100"] is False

    def test_no_free_float_when_an_owner_stake_is_unknown(self):
        # can't tell what's left if one owner's % is missing
        s = _ownership_summary([_owner(30.0), _owner(None)])
        assert s["unknown_owners"] == 1
        assert s["free_float_pct"] is None

    def test_flags_over_100_and_no_free_float(self):
        s = _ownership_summary([_owner(80.0), _owner(63.0)])
        assert s["disclosed_pct"] == 143.0
        assert s["exceeds_100"] is True
        assert s["free_float_pct"] is None

    def test_no_free_float_when_fully_held(self):
        # residual below the 0.5% noise threshold → nothing to show
        assert _ownership_summary([_owner(100.0)])["free_float_pct"] is None
        assert _ownership_summary([_owner(99.8)])["free_float_pct"] is None

    def test_no_owners_or_no_known_stakes(self):
        assert _ownership_summary([])["disclosed_pct"] is None
        assert _ownership_summary([_owner(None)])["free_float_pct"] is None


class TestClean:
    def test_strips_arcadedb_metadata_keys(self):
        row = {"@rid": "#1:0", "@type": "Entity", "@cat": "v",
               "id": "acme", "name": "Acme"}
        assert _clean(row) == {"id": "acme", "name": "Acme"}

    def test_keeps_all_data_keys(self):
        row = {"id": "x", "name": "X", "country": "US", "search_text": "X"}
        assert _clean(row) == row


def _row(entity, rel):
    return {"entity": entity, "rel": rel}


class TestDedupePositions:
    def test_collapses_same_entity_role_keeping_latest_tenure(self):
        rows = [
            _row({"id": "tesla", "name": "Tesla"}, {"role": "CEO", "since": "2008-10-01"}),
            _row({"id": "tesla", "name": "Tesla"}, {"role": "CEO", "since": "2021-01-01"}),
        ]
        out = _dedupe_positions(rows)
        assert len(out) == 1
        assert out[0]["role"]["since"] == "2021-01-01"   # most recent tenure kept

    def test_keeps_distinct_roles_at_the_same_entity(self):
        rows = [
            _row({"id": "spacex", "name": "SpaceX"}, {"role": "CEO", "since": None}),
            _row({"id": "spacex", "name": "SpaceX"}, {"role": "Founder", "since": None}),
        ]
        out = _dedupe_positions(rows)
        assert {p["role"]["role"] for p in out} == {"CEO", "Founder"}

    def test_keeps_same_role_at_different_entities(self):
        rows = [
            _row({"id": "a", "name": "Alpha"}, {"role": "CEO"}),
            _row({"id": "b", "name": "Beta"},  {"role": "CEO"}),
        ]
        assert len(_dedupe_positions(rows)) == 2

    def test_skips_null_entities(self):
        assert _dedupe_positions([_row(None, None)]) == []

    def test_sorted_by_entity_then_role(self):
        rows = [
            _row({"id": "b", "name": "Beta"},  {"role": "CEO"}),
            _row({"id": "a", "name": "Alpha"}, {"role": "Founder"}),
        ]
        out = _dedupe_positions(rows)
        assert [p["entity"]["name"] for p in out] == ["Alpha", "Beta"]


class TestDedupeHoldings:
    def test_collapses_same_entity_keeping_largest_stake(self):
        rows = [
            _row({"id": "tesla", "name": "Tesla"}, {"stake_percent": 10}),
            _row({"id": "tesla", "name": "Tesla"}, {"stake_percent": 20.5}),
        ]
        out = _dedupe_holdings(rows)
        assert len(out) == 1
        assert out[0]["relationship"]["stake_percent"] == 20.5

    def test_keeps_distinct_entities(self):
        rows = [
            _row({"id": "a", "name": "Alpha"}, {"stake_percent": 5}),
            _row({"id": "b", "name": "Beta"},  {"stake_percent": 5}),
        ]
        assert len(_dedupe_holdings(rows)) == 2

    def test_skips_null_entities(self):
        assert _dedupe_holdings([_row(None, None)]) == []
