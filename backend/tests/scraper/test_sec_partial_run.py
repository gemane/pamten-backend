"""A section of a company scrape lost to the network makes the run `partial`
(findings item 41). On 2026-11-01 a read timeout on the 13D/G browse request
came back as [], the answer of a company with no 13D/G filings, and the run
ended `ok`: Gitlab's owners went 36 -> 17, HubSpot's 30 -> 14, Embraer's 10 -> 4,
and the target was stamped fresh for 30 days. No database and no network here."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.scraper import graph_writer, sec_edgar
from app.scraper.ondemand import decide_scrape
from app.scraper.run_log import mark_partial


def _status(code):
    req = httpx.Request("GET", "https://www.sec.gov/x")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


class TestSectionFailed:
    def _collect(self, exc):
        token = sec_edgar._failed_sections.set([])
        try:
            lost = sec_edgar._section_failed("13D/G owners", exc)
            return lost, sec_edgar._failed_sections.get()
        finally:
            sec_edgar._failed_sections.reset(token)

    def test_a_timeout_is_a_lost_section(self):
        assert self._collect(httpx.ReadTimeout("stalled")) == (True, ["13D/G owners"])

    def test_a_server_error_is_a_lost_section(self):
        assert self._collect(_status(500)) == (True, ["13D/G owners"])

    def test_a_404_is_an_answer_not_a_loss(self):
        assert self._collect(_status(404)) == (False, [])

    def test_a_parse_error_is_not_the_network(self):
        assert self._collect(ValueError("bad json")) == (False, [])

    def test_outside_a_scrape_nothing_is_recorded(self):
        assert sec_edgar._failed_sections.get() is None
        assert sec_edgar._section_failed("insiders", httpx.ReadTimeout("x")) is True
        assert sec_edgar._failed_sections.get() is None


class TestScrapeCompanyReportsLostSections:
    COMPANY = {"cik": "0001653482", "name": "GitLab Inc."}

    def _scrape(self, get_text=None, get=None, submissions=None):
        """scrape_company with every request stubbed; the three that carry a
        whole section can be made to fail."""
        ok_subs = {"filings": {"recent": {}}}
        with patch.object(sec_edgar, "company_by_cik", return_value=self.COMPANY), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]), \
             patch.object(sec_edgar, "fetch_company_lei", return_value=None), \
             patch.object(sec_edgar, "fetch_shares_outstanding_detail", return_value=None), \
             patch.object(sec_edgar, "_get_text", side_effect=get_text or (lambda *a, **k: "<feed/>")), \
             patch.object(sec_edgar, "_get", side_effect=get or (lambda *a, **k: ok_subs)), \
             patch.object(sec_edgar, "_submissions", side_effect=submissions or (lambda *a, **k: ok_subs)):
            return sec_edgar.scrape_company("Gitlab", cik="0001653482")

    def test_a_complete_scrape_lost_nothing(self):
        assert self._scrape()["failed_sections"] == []

    def test_a_timed_out_browse_loses_the_owners(self):
        def browse(*a, **k):
            raise httpx.ReadTimeout("stalled")
        out = self._scrape(get_text=browse)
        assert out["ownership_filings"] == [] and out["failed_sections"] == ["13D/G owners"]

    def test_a_404_browse_lost_nothing(self):
        def browse(*a, **k):
            raise _status(404)
        assert self._scrape(get_text=browse)["failed_sections"] == []

    def test_a_timed_out_submissions_file_loses_the_insiders(self):
        def get(url, *a, **k):
            raise httpx.ReadTimeout("stalled")
        assert self._scrape(get=get)["failed_sections"] == ["insiders"]

    def test_a_timed_out_submissions_file_loses_the_holdings(self):
        def subs(*a, **k):
            raise httpx.ReadTimeout("stalled")
        assert self._scrape(submissions=subs)["failed_sections"] == ["holdings"]

    def test_a_filer_without_a_submissions_file_lost_nothing(self):
        def subs(*a, **k):
            raise _status(404)
        assert self._scrape(submissions=subs)["failed_sections"] == []

    def test_the_collector_closes_with_the_scrape(self):
        def browse(*a, **k):
            raise httpx.ReadTimeout("stalled")
        self._scrape(get_text=browse)
        assert sec_edgar._failed_sections.get() is None


class TestFreshnessStamp:
    def _stamped(self, steps):
        token = graph_writer._scrape_target.set(None)
        try:
            for step in steps:
                step()
            target = graph_writer._scrape_target.get()
        finally:
            graph_writer._scrape_target.reset(token)
        with patch("app.db.arcadedb.run_command") as run:
            graph_writer._stamp_scrape_freshness(target)
        return target, run.call_args.args[1]

    def test_an_incomplete_scrape_is_stamped_with_the_flag(self):
        target, params = self._stamped([lambda: graph_writer.mark_scrape_incomplete("e1", 0)])
        assert target["incomplete"] is True and params["inc"] == params["now"]

    def test_a_complete_scrape_clears_the_flag(self):
        _, params = self._stamped([lambda: graph_writer.set_scrape_target("e1", 1)])
        assert params["inc"] is None

    def test_another_source_in_the_scope_cannot_clear_it(self):
        target, params = self._stamped([lambda: graph_writer.mark_scrape_incomplete("e1", 0),
                                        lambda: graph_writer.set_scrape_target("e1", 2)])
        assert target == {"id": "e1", "depth": 2, "incomplete": True} and params["inc"]

    def test_a_source_before_it_keeps_its_depth(self):
        target, _ = self._stamped([lambda: graph_writer.set_scrape_target("e1", 2),
                                   lambda: graph_writer.mark_scrape_incomplete("e1", 0)])
        assert target == {"id": "e1", "depth": 2, "incomplete": True}

    def test_no_target_no_flag(self):
        token = graph_writer._scrape_target.set(None)
        try:
            graph_writer.mark_scrape_incomplete(None)
            assert graph_writer._scrape_target.get() is None
        finally:
            graph_writer._scrape_target.reset(token)


class TestTheNextEnsureRetries:
    NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    def _ent(self, hours_ago, incomplete):
        at = (self.NOW - timedelta(hours=hours_ago)).isoformat()
        return {"id": "e1", "on_demand_scraped": True, "last_scraped_at": at, "scrape_depth": 1,
                "scrape_incomplete_at": at if incomplete else None}

    def test_after_the_cooldown_an_incomplete_company_is_scraped_again(self):
        d = decide_scrape(self._ent(25, True), requested_depth=1, force=False, now=self.NOW)
        assert d.should_scrape and d.reason == "incomplete"

    def test_within_the_cooldown_it_is_served_from_the_db(self):
        d = decide_scrape(self._ent(2, True), requested_depth=1, force=False, now=self.NOW)
        assert not d.should_scrape and d.reason == "fresh"

    def test_a_complete_company_waits_for_the_ttl(self):
        d = decide_scrape(self._ent(25, False), requested_depth=1, force=False, now=self.NOW)
        assert not d.should_scrape and d.reason == "fresh"


class TestTheRunLogSaysPartial:
    def test_a_partial_result_finishes_the_row_partial(self):
        run = {"total": 3}
        mark_partial(run, {"status": "partial", "failed_sections": ["13D/G owners", "insiders"]})
        assert run["status"] == "partial" and run["note"] == "lost to the network: 13D/G owners, insiders"

    @pytest.mark.parametrize("result", [{"status": "ok"}, None, "x"])
    def test_anything_else_is_left_alone(self, result):
        run = {"total": 3}
        mark_partial(run, result)
        assert "status" not in run

    def test_run_all_is_partial_when_a_source_is(self):
        from app.scraper.router import _mark_errored_sources
        run = {}
        _mark_errored_sources(run, {"results": {
            "wikidata": {"status": "ok"},
            "sec_edgar": {"status": "partial", "failed_sections": ["13D/G owners"]}}})
        assert run == {"status": "partial", "note": "lost to the network: sec_edgar: 13D/G owners"}

    def test_an_error_still_wins_over_partial(self):
        from app.scraper.router import _mark_errored_sources
        run = {}
        _mark_errored_sources(run, {"results": {
            "wikidata": {"status": "error", "detail": "boom"},
            "sec_edgar": {"status": "partial", "failed_sections": ["insiders"]}}})
        assert run["status"] == "failed"

    def test_the_on_demand_row_is_partial(self):
        from app.scraper import ondemand
        spec = MagicMock(kind="instant", depth_aware=False, reads_stored_ids=False)
        spec.name = "sec_edgar"
        spec.enabled.return_value = True
        spec.run.return_value = {"status": "partial", "failed_sections": ["holdings"], "total": 4}
        rows = []
        from contextlib import contextmanager

        @contextmanager
        def fake_record_run(*a, **k):
            run = {"total": 0}
            yield run
            rows.append(run)
        decision = ondemand.ScrapeDecision(True, "incomplete", 1)
        with patch("app.scraper.scraper_registry.registered", return_value=[spec]), \
             patch("app.scraper.run_log.record_run", fake_record_run), \
             patch("app.scraper.runner.rerun_for_new_ids", return_value=False):
            ondemand._run_instant_sources.__wrapped__("Gitlab", decision, None)
        assert rows == [{"total": 4, "status": "partial", "note": "lost to the network: holdings"}]
