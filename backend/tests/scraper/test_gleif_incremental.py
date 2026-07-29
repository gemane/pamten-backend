"""Unit tests for GLEIF delta parsing — retirement detection (DB not involved).

Idempotency of the edge upserts and end-to-end apply are covered against a real
ArcadeDB in tests/integration/test_gleif_incremental_it.py."""

from unittest.mock import MagicMock, patch

from app.scraper.gleif_incremental import (
    _entity_status,
    _registration_status,
    _relationship_end_date,
    _rr_delta_relationship,
    fetch_gleif_deltas,
)


def _rr(rtype, child, parent, status="ACTIVE", end_date=None):
    rel = {
        "StartNode": {"NodeID": {"$": child}, "NodeIDType": {"$": "LEI"}},
        "EndNode":   {"NodeID": {"$": parent}, "NodeIDType": {"$": "LEI"}},
        "RelationshipType":   {"$": rtype},
        "RelationshipStatus": {"$": status},
    }
    if end_date:
        rel["RelationshipPeriods"] = {"RelationshipPeriod": [
            {"PeriodType": {"$": "ACCOUNTING_PERIOD"}, "EndDate": {"$": "2020-12-31"}},
            {"PeriodType": {"$": "RELATIONSHIP_PERIOD"}, "EndDate": {"$": end_date}},
        ]}
    return {"RelationshipRecord": {"Relationship": rel}}


class TestRegistrationStatus:
    def test_issued_and_retired(self):
        assert _registration_status({"Registration": {"RegistrationStatus": {"$": "ISSUED"}}}) == "ISSUED"
        assert _registration_status({"Registration": {"RegistrationStatus": {"$": "LAPSED"}}}) == "LAPSED"

    def test_missing(self):
        assert _registration_status({}) is None


class TestEntityStatus:
    def test_active_vs_inactive(self):
        assert _entity_status({"Entity": {"EntityStatus": {"$": "ACTIVE"}}}) == "ACTIVE"
        assert _entity_status({"Entity": {"EntityStatus": {"$": "INACTIVE"}}}) == "INACTIVE"

    def test_missing(self):
        assert _entity_status({}) is None


class TestRelationshipEndDate:
    def test_picks_relationship_period_end(self):
        rel = _rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P",
                  status="INACTIVE", end_date="2023-06-01")["RelationshipRecord"]["Relationship"]
        assert _relationship_end_date(rel) == "2023-06-01"

    def test_single_object_not_list(self):
        rel = {"RelationshipPeriods": {"RelationshipPeriod":
               {"PeriodType": {"$": "RELATIONSHIP_PERIOD"}, "EndDate": {"$": "2022-01-01"}}}}
        assert _relationship_end_date(rel) == "2022-01-01"

    def test_no_end_date(self):
        rel = _rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P")["RelationshipRecord"]["Relationship"]
        assert _relationship_end_date(rel) is None


class TestRrDeltaRelationship:
    def test_active_consolidation(self):
        parent, child, marker, status, _ = _rr_delta_relationship(
            _rr("IS_DIRECTLY_CONSOLIDATED_BY", "CHILD", "PARENT"))
        assert (parent, child, marker, status) == ("PARENT", "CHILD", "direct", "ACTIVE")

    def test_ultimate_is_indirect(self):
        parent, child, marker, status, _ = _rr_delta_relationship(
            _rr("IS_ULTIMATELY_CONSOLIDATED_BY", "CHILD", "PARENT"))
        assert (marker, status) == ("indirect", "ACTIVE")

    def test_inactive_status_preserved(self):
        _, _, _, status, rel = _rr_delta_relationship(
            _rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", status="INACTIVE", end_date="2023-06-01"))
        assert status == "INACTIVE"
        assert _relationship_end_date(rel) == "2023-06-01"

    def test_non_consolidation_skipped(self):
        assert _rr_delta_relationship(_rr("IS_FUND-MANAGED_BY", "C", "P")) is None

    def test_self_reference_skipped(self):
        assert _rr_delta_relationship(_rr("IS_DIRECTLY_CONSOLIDATED_BY", "X", "X")) is None


class TestFetchDeltas:
    """The publishes API is mocked — the test never touches goldencopy.gleif.org."""

    def _publishes(self):
        def _section(name):
            return {"delta_files": {"LastDay": {"json": {
                "url": f"https://goldencopy.gleif.org/{name}-LastDay.json.zip"}}}}
        return {"data": [{"lei2": _section("lei2"), "rr": _section("rr")}]}

    def test_downloads_lei2_and_rr(self, tmp_path):
        api = MagicMock()
        api.json.return_value = self._publishes()

        stream_resp = MagicMock()
        stream_resp.iter_bytes.return_value = [b"zipbytes"]
        stream_ctx = MagicMock()
        stream_ctx.__enter__.return_value = stream_resp

        with patch("httpx.get", return_value=api), \
             patch("httpx.stream", return_value=stream_ctx):
            out = fetch_gleif_deltas(interval="LastDay", dest_dir=str(tmp_path))

        assert out["lei2"].endswith("lei2-LastDay.json.zip")
        assert out["rr"].endswith("rr-LastDay.json.zip")
        assert (tmp_path / "lei2-LastDay.json.zip").read_bytes() == b"zipbytes"
