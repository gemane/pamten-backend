"""Unit tests for GLEIF delta parsing — retirement detection (DB not involved).

Idempotency of the edge upserts and end-to-end apply are covered against a real
ArcadeDB in tests/integration/test_gleif_incremental_it.py."""

import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.scraper.gleif_incremental import (
    _PUBLISH_FMT,
    _entity_status,
    _registration_status,
    _relationship_end_date,
    _rr_delta_relationship,
    choose_catchup_interval,
    downloaded_deltas,
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


class TestChooseCatchupInterval:
    """Gap-aware window selection — the heart of the missed-run catch-up."""

    NOW = "2026-07-29 16:00:00"

    def _ago(self, days):
        return (datetime.strptime(self.NOW, _PUBLISH_FMT) - timedelta(days=days)).strftime(_PUBLISH_FMT)

    def test_cold_start_goes_wide(self):
        # No checkpoint (e.g. first run after the full load) → widest safe delta.
        assert choose_catchup_interval(None, self.NOW) == "LastMonth"

    def test_normal_daily_cadence(self):
        assert choose_catchup_interval(self._ago(1), self.NOW) == "LastDay"

    def test_one_missed_day_escalates_to_week(self):
        assert choose_catchup_interval(self._ago(2), self.NOW) == "LastWeek"

    def test_within_a_week(self):
        assert choose_catchup_interval(self._ago(6), self.NOW) == "LastWeek"

    def test_over_a_week_escalates_to_month(self):
        assert choose_catchup_interval(self._ago(10), self.NOW) == "LastMonth"

    def test_within_a_month(self):
        assert choose_catchup_interval(self._ago(28), self.NOW) == "LastMonth"

    def test_too_stale_for_a_delta(self):
        # Past ~30 days no delta window covers it → caller must full-reload.
        assert choose_catchup_interval(self._ago(45), self.NOW) is None

    def test_unparseable_checkpoint_is_safe(self):
        assert choose_catchup_interval("not-a-date", self.NOW) == "LastMonth"


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


class TestTheDownloadedDeltasAreCleanedUp:
    """`download_deltas` leaves its temp directory behind, and the nightly update
    called it directly — one `gleif-delta-*` directory per run, for months. By the
    time anyone looked there were thirteen of them and 135 MB in /tmp, which
    survives reboots here. Slow, silent and unbounded.

    The fix is a context manager rather than a `finally` at the call site, because
    the call site is exactly where it was forgotten.
    """

    def _patched(self):
        api = MagicMock()
        api.json.return_value = {"data": [{}]}
        stream_resp = MagicMock()
        stream_resp.iter_bytes.return_value = [b"zipbytes"]
        stream_ctx = MagicMock()
        stream_ctx.__enter__.return_value = stream_resp
        return patch("httpx.stream", return_value=stream_ctx)

    def _publish(self):
        def _section(name):
            return {"delta_files": {"LastDay": {"json": {
                "url": f"https://goldencopy.gleif.org/{name}-LastDay.json.zip"}}}}
        return {"lei2": _section("lei2"), "rr": _section("rr")}

    def test_the_files_exist_inside_the_block(self):
        with self._patched():
            with downloaded_deltas(self._publish(), "LastDay") as paths:
                assert os.path.exists(paths["lei2"]) and os.path.exists(paths["rr"])

    def test_and_are_gone_after_it(self):
        with self._patched():
            with downloaded_deltas(self._publish(), "LastDay") as paths:
                held = paths["lei2"]
            assert not os.path.exists(held)
            assert not os.path.exists(os.path.dirname(held)), "the temp directory leaked"

    def test_they_are_cleaned_up_when_the_apply_fails(self):
        # The case that actually accumulates: a run that dies partway still has to
        # take its 135 MB with it. The URLs come from a dated publish record, so a
        # retry re-fetches exactly the same bytes and keeping them buys nothing.
        held = {}
        with self._patched():
            with pytest.raises(RuntimeError, match="apply blew up"):
                with downloaded_deltas(self._publish(), "LastDay") as paths:
                    held["dir"] = os.path.dirname(paths["lei2"])
                    raise RuntimeError("apply blew up")
        assert not os.path.exists(held["dir"])

    def test_a_caller_supplied_directory_is_left_alone(self, tmp_path):
        # `download_deltas` with an explicit dest_dir is the caller's directory to
        # keep — deleting it would be a surprise, and `fetch_gleif_deltas` exists
        # precisely so a human can fetch files and look at them.
        with self._patched():
            out = fetch_gleif_deltas(interval="LastDay", dest_dir=str(tmp_path))
        assert os.path.exists(out["lei2"]), "an explicitly requested file was deleted"
