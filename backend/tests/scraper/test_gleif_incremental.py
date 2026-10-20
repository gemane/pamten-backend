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
    _rr_delta_relationship,
    choose_catchup_interval,
    download_deltas,
    downloaded_deltas,
    fetch_publish_metadata,
)


def _rr(rtype, child, parent, status="ACTIVE", end_date=None, rr_status=None, amount=None,
        validation=None):
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
    if amount is not None:
        rel["RelationshipQuantifiers"] = {"RelationshipQuantifier": [
            {"MeasurementMethod": {"$": "ACCOUNTING_CONSOLIDATION"}, "QuantifierAmount": {"$": amount}}]}
    rec = {"RelationshipRecord": {"Relationship": rel}}
    registration = {"RegistrationStatus": {"$": rr_status}} if rr_status else {}
    for key, value in (validation or {}).items():
        registration[key] = {"$": value}
    if registration:
        rec["RelationshipRecord"]["Registration"] = registration
    return rec


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


class TestRrDeltaRelationship:
    def test_active_consolidation(self):
        (parent, child, marker, status, _, unmaintained, stake, implausible,
         validation) = _rr_delta_relationship(_rr("IS_DIRECTLY_CONSOLIDATED_BY", "CHILD", "PARENT"))
        assert (parent, child, marker, status) == ("PARENT", "CHILD", "direct", "ACTIVE")
        assert unmaintained is False and stake is None and implausible is False
        assert validation == {}

    def test_ultimate_is_indirect(self):
        parent, child, marker, status, *_ = _rr_delta_relationship(
            _rr("IS_ULTIMATELY_CONSOLIDATED_BY", "CHILD", "PARENT"))
        assert (marker, status) == ("indirect", "ACTIVE")

    def test_inactive_status_preserved(self):
        _, _, _, status, *_ = _rr_delta_relationship(
            _rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", status="INACTIVE", end_date="2023-06-01"))
        assert status == "INACTIVE"

    def test_a_lapsed_record_arrives_unmaintained(self):
        # the record that lapses is in that day's delta: the flag rides along
        assert _rr_delta_relationship(
            _rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", rr_status="LAPSED"))[5] is True
        assert _rr_delta_relationship(
            _rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", rr_status="PUBLISHED"))[5] is False

    def test_the_stated_share_arrives_with_the_record(self):
        assert _rr_delta_relationship(
            _rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", amount="100.00"))[6:8] == (100.0, False)
        # refused, flagged — the record still applies, without a stake
        assert _rr_delta_relationship(
            _rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", amount="-1"))[6:8] == (None, True)

    def test_how_far_the_record_was_checked_arrives_with_it(self):
        assert _rr_delta_relationship(_rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", validation={
            "ValidationSources": "PARTIALLY_CORROBORATED", "ValidationDocuments": "CONTRACTS"}))[8] == {
                "validation_sources": "PARTIALLY_CORROBORATED", "validation_documents": "CONTRACTS"}

    def test_an_annulled_or_duplicate_record_is_skipped(self):
        for st in ("ANNULLED", "DUPLICATE"):
            assert _rr_delta_relationship(
                _rr("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", rr_status=st)) is None

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
            out = download_deltas(fetch_publish_metadata(), "LastDay", str(tmp_path))

        assert out["lei2"].endswith("lei2-LastDay.json.zip")
        assert out["rr"].endswith("rr-LastDay.json.zip")
        assert (tmp_path / "lei2-LastDay.json.zip").read_bytes() == b"zipbytes"


class TestTheDeltaClientIdentifiesItselfAndRetries:
    """The nightly refresh used a bare `httpx.get` / `httpx.stream`: no
    User-Agent (every other client here sends one) and no retry, so one blip
    failed the night. Mirrors `gleif_rr_history._download`."""

    def _api(self):
        api = MagicMock()
        api.json.return_value = {"data": [{"publish_date": "2026-10-14"}]}
        return api

    def test_the_publish_read_sends_the_user_agent_and_follows_redirects(self):
        from app.scraper.gleif_incremental import fetch_publish_metadata
        with patch("httpx.get", return_value=self._api()) as get:
            assert fetch_publish_metadata() == {"publish_date": "2026-10-14"}
        kwargs = get.call_args.kwargs
        assert kwargs["headers"]["User-Agent"].startswith("Owlgraph/")
        assert kwargs["follow_redirects"] is True

    def test_a_blip_on_the_publish_read_is_retried_then_given_up(self):
        from app.scraper.gleif_incremental import fetch_publish_metadata
        import httpx
        with patch("httpx.get", side_effect=[httpx.ConnectError("blip"), self._api()]) as get, \
             patch("time.sleep") as sleep:
            assert fetch_publish_metadata()["publish_date"] == "2026-10-14"
        assert get.call_count == 2 and sleep.call_count == 1
        with patch("httpx.get", side_effect=httpx.ConnectError("down")) as get, \
             patch("time.sleep"), pytest.raises(httpx.ConnectError):
            fetch_publish_metadata()
        assert get.call_count == 3

    def test_a_blip_on_the_download_is_retried(self, tmp_path):
        from app.scraper.gleif_incremental import _download
        import httpx
        stream_resp = MagicMock()
        stream_resp.iter_bytes.return_value = [b"zipbytes"]
        ok = MagicMock()
        ok.__enter__.return_value = stream_resp
        with patch("httpx.stream", side_effect=[httpx.ReadTimeout("slow"), ok]) as stream, \
             patch("time.sleep"):
            _download("https://goldencopy.gleif.org/x.zip", str(tmp_path / "x.zip"))
        assert (tmp_path / "x.zip").read_bytes() == b"zipbytes"
        assert stream.call_count == 2
        assert stream.call_args.kwargs["headers"]["User-Agent"].startswith("Owlgraph/")


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
        # keep — deleting it would be a surprise.
        #
        # `fetch_publish_metadata` fetches the publish record before downloading, so
        # httpx.get is patched as well as httpx.stream. Without it this test spent
        # five seconds on the live GLEIF API — a unit test quietly depending on the
        # internet, which is how the country-filter tests came to fail in CI.
        api = MagicMock()
        api.json.return_value = {"data": [self._publish()]}
        with self._patched(), patch("httpx.get", return_value=api):
            out = download_deltas(fetch_publish_metadata(), "LastDay", str(tmp_path))
        assert os.path.exists(out["lei2"]), "an explicitly requested file was deleted"
