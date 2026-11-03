"""An SEC request that ends in an error is told and counted (findings item 45).

`_response` logged "retrying once" and nothing after it; when the retry failed
too, the error went to a caller that drops it at debug level, so the
2026-11-02 rebuild could not say which of its 85 retries lost a filing (a
Form 4 stalled during Morgan Stanley's scrape, whose SEC count was 70 against
75 before). No database and no network here."""
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.scraper import runner, sec_edgar
from app.scraper.run_log import mark_partial

URL = "https://www.sec.gov/Archives/edgar/data/1367016/000106299322013957/form4.xml"


def _client(*answers):
    calls = []

    def get(url, params=None):
        calls.append(url)
        a = answers[len(calls) - 1]
        if isinstance(a, Exception):
            raise a
        if isinstance(a, int):
            return httpx.Response(a, headers={"Retry-After": "0"}, request=httpx.Request("GET", url))
        return httpx.Response(200, text=a, request=httpx.Request("GET", url))
    client = MagicMock()
    client.get.side_effect = get
    return client, calls


def _fetch(*answers, url=URL, params=None):
    """_get_text against scripted answers, inside a collector; returns
    (text or the exception, the collected URLs)."""
    client, _ = _client(*answers)
    with patch.object(sec_edgar, "_get_client", return_value=client), \
         patch.object(settings, "SEC_CACHE_DIR", None), \
         patch("app.objectstore.enabled", return_value=False), \
         patch("time.sleep"), \
         sec_edgar.collect_lost_requests() as lost:
        try:
            got = sec_edgar._get_text(url, params)
        except httpx.HTTPError as exc:
            got = exc
    return got, lost


def _lost_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == sec_edgar.log.name and "request lost" in r.getMessage()]


class TestTheLossIsTold:
    def test_a_second_stall_is_lost_after_the_retry(self, caplog):
        caplog.set_level(logging.WARNING, logger=sec_edgar.log.name)
        got, lost = _fetch(httpx.ReadTimeout("stalled"), httpx.ReadTimeout("again"))
        assert isinstance(got, httpx.ReadTimeout) and lost == [URL]
        assert _lost_lines(caplog) == [f"SEC EDGAR: request lost (ReadTimeout, after the retry) on {URL}"]

    def test_a_recovered_stall_is_not_lost(self, caplog):
        caplog.set_level(logging.WARNING, logger=sec_edgar.log.name)
        got, lost = _fetch(httpx.ReadTimeout("stalled"), "<xml/>")
        assert got == "<xml/>" and lost == [] and _lost_lines(caplog) == []

    def test_a_second_503_is_lost_and_its_retry_is_told(self, caplog):
        caplog.set_level(logging.WARNING, logger=sec_edgar.log.name)
        got, lost = _fetch(503, 503)
        assert isinstance(got, httpx.HTTPStatusError) and lost == [URL]
        msgs = [r.getMessage() for r in caplog.records]
        assert f"SEC EDGAR: HTTP 503 on {URL}, retrying once after 0 s" in msgs
        assert _lost_lines(caplog) == [f"SEC EDGAR: request lost (HTTP 503, after the retry) on {URL}"]

    def test_a_recovered_429_is_not_lost(self):
        got, lost = _fetch(429, "ok")
        assert got == "ok" and lost == []

    @pytest.mark.parametrize("answer", [httpx.ConnectError("refused"), 500, 403])
    def test_an_error_without_a_retry_is_lost_too(self, answer, caplog):
        caplog.set_level(logging.WARNING, logger=sec_edgar.log.name)
        _, lost = _fetch(answer)
        assert lost == [URL]
        assert _lost_lines(caplog) and "after the retry" not in _lost_lines(caplog)[0]

    def test_a_404_is_an_answer_not_a_loss(self, caplog):
        caplog.set_level(logging.WARNING, logger=sec_edgar.log.name)
        got, lost = _fetch(404)
        assert isinstance(got, httpx.HTTPStatusError) and lost == [] and _lost_lines(caplog) == []

    def test_the_query_is_part_of_the_url_told(self):
        url = "https://www.sec.gov/cgi-bin/browse-edgar"
        _, lost = _fetch(httpx.ConnectError("x"), url=url, params={"action": "getcompany", "CIK": "1"})
        assert lost == [f"{url}?action=getcompany&CIK=1"]

    def test_outside_a_collector_it_is_only_logged(self, caplog):
        caplog.set_level(logging.WARNING, logger=sec_edgar.log.name)
        client, _ = _client(httpx.ConnectError("down"))
        with patch.object(sec_edgar, "_get_client", return_value=client), \
             patch.object(settings, "SEC_CACHE_DIR", None), \
             patch("app.objectstore.enabled", return_value=False), patch("time.sleep"):
            with pytest.raises(httpx.ConnectError):
                sec_edgar._get_text(URL)
        assert sec_edgar._lost_requests.get() is None and len(_lost_lines(caplog)) == 1

    def test_the_collector_closes_with_its_block(self):
        _fetch(httpx.ConnectError("down"))
        assert sec_edgar._lost_requests.get() is None


class TestTheRunnerCountsThem:
    """A caller that drops the error (a Form 4 the scrape goes on without) still
    leaves the count on the result — the scrape and its writer's lookups both."""

    def _run(self, answers, result):
        client, _ = _client(*answers)

        def scrape(company_name, country):
            try:                                   # a caller that swallows, as ~20 do
                sec_edgar._get_text(URL)
            except httpx.HTTPError:
                pass
            return dict(result)
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
             patch.object(settings, "SEC_CACHE_DIR", None), \
             patch("app.objectstore.enabled", return_value=False), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch.object(runner, "_scrape_sec_edgar", side_effect=scrape), \
             patch.object(sec_edgar, "_get_client", return_value=client), patch("time.sleep"):
            return runner.run_scrape_sec_edgar("Morgan Stanley")

    def test_a_swallowed_loss_is_counted_and_the_run_stays_ok(self):
        out = self._run([httpx.ReadTimeout("a"), httpx.ReadTimeout("b")], {"status": "ok", "total": 70})
        assert (out["status"], out["total"], out["lost_requests"]) == ("ok", 70, 1)

    def test_a_clean_scrape_counts_zero(self):
        out = self._run(["<xml/>"], {"status": "ok", "total": 75})
        assert out["lost_requests"] == 0

    def test_no_results_carries_the_count_too(self):
        out = self._run([httpx.ConnectError("down")], {"status": "no_results", "total": 0})
        assert out["lost_requests"] == 1


class TestTheRunLogNotesThem:
    def test_an_ok_row_keeps_its_status_and_gets_a_note(self):
        run = {"total": 70}
        mark_partial(run, {"status": "ok", "lost_requests": 2})
        assert run == {"total": 70, "note": "2 request(s) lost to the network"}

    def test_a_partial_row_names_both(self):
        run = {}
        mark_partial(run, {"status": "partial", "failed_sections": ["insiders"], "lost_requests": 3})
        assert run == {"status": "partial",
                       "note": "lost to the network: insiders; 3 request(s) lost to the network"}

    @pytest.mark.parametrize("result", [{"status": "ok", "lost_requests": 0}, {"status": "ok"}, None])
    def test_no_loss_no_note(self, result):
        run = {}
        mark_partial(run, result)
        assert run == {}

    def test_run_all_names_the_source(self):
        from app.scraper.router import _mark_errored_sources
        run = {}
        _mark_errored_sources(run, {"results": {
            "wikidata": {"status": "ok"},
            "sec_edgar": {"status": "ok", "lost_requests": 1}}})
        assert run == {"note": "sec_edgar: 1 request(s) lost to the network"}

    def test_run_all_partial_and_lost_together(self):
        from app.scraper.router import _mark_errored_sources
        run = {}
        _mark_errored_sources(run, {"results": {
            "sec_edgar": {"status": "partial", "failed_sections": ["holdings"], "lost_requests": 2}}})
        assert run == {"status": "partial", "note": "lost to the network: sec_edgar: holdings; "
                                                   "sec_edgar: 2 request(s) lost to the network"}
