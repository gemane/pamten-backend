"""The SEC scrape reads a company by the CIK the database already holds.

The name search only finds what its indexes list: "SpaceX" is a trade name
(EDGAR knows SPACE EXPLORATION TECHNOLOGIES CORP) and Nestlé files no 10-K or
20-F, so neither was found, while both nodes carried the right CIK from
Wikidata (2026-10-27). Exhibit 21, acquisitions and 13F already read by the
stored number; the main scrape now does too, and searches by name only when
the database holds none — or EDGAR does not know the one it holds.
"""
from unittest.mock import patch

import httpx
import pytest

from app.scraper import runner, sec_edgar
from app.config import settings


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://data.sec.gov/submissions/CIK0000792990.json")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, request=req))


class TestCompanyByCik:
    def test_the_submissions_document_is_the_identity(self):
        with patch.object(sec_edgar, "_submissions", return_value={"name": "NESTLE SA"}) as sub:
            assert sec_edgar.company_by_cik("792990") == {"cik": "0000792990", "name": "NESTLE SA"}
        sub.assert_called_once_with("0000792990")          # always the padded form

    def test_a_cik_edgar_does_not_know_is_none(self):
        with patch.object(sec_edgar, "_submissions", side_effect=_http_error(404)):
            assert sec_edgar.company_by_cik("0000792990") is None

    def test_any_other_http_error_is_raised_not_read_as_unknown(self):
        with patch.object(sec_edgar, "_submissions", side_effect=_http_error(503)):
            with pytest.raises(httpx.HTTPStatusError):
                sec_edgar.company_by_cik("0000792990")

    def test_a_value_that_is_no_cik_fetches_nothing(self):
        with patch.object(sec_edgar, "_submissions") as sub:
            assert sec_edgar.company_by_cik("not-a-cik") is None
            assert sec_edgar.company_by_cik(None) is None
        sub.assert_not_called()


@pytest.fixture
def quiet_fetches():
    """Everything scrape_company reads after the filer is found, emptied."""
    names = {"fetch_former_names": [], "fetch_company_lei": None, "fetch_ownership_filings": [],
             "fetch_executives": [], "fetch_filer_holdings": [], "fetch_shares_outstanding_detail": None}
    patches = [patch.object(sec_edgar, n, return_value=v) for n, v in names.items()]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


class TestScrapeCompany:
    def test_a_stored_cik_skips_the_name_search(self, quiet_fetches):
        with patch.object(sec_edgar, "_submissions", return_value={"name": "NESTLE SA"}), \
             patch.object(sec_edgar, "search_company") as search:
            data = sec_edgar.scrape_company("Nestlé", cik="0000792990")
        search.assert_not_called()
        assert (data["cik"], data["name"]) == ("0000792990", "NESTLE SA")

    def test_a_stored_cik_edgar_does_not_know_falls_back_to_the_name(self, quiet_fetches):
        found = {"cik": "0001181412", "name": "SPACE EXPLORATION TECHNOLOGIES CORP"}
        with patch.object(sec_edgar, "_submissions", side_effect=_http_error(404)), \
             patch.object(sec_edgar, "search_company", return_value=found) as search:
            data = sec_edgar.scrape_company("Space Exploration Technologies Corp", cik="0000000001")
        search.assert_called_once_with("Space Exploration Technologies Corp")
        assert data["cik"] == "0001181412"

    def test_without_a_stored_cik_the_name_search_runs(self, quiet_fetches):
        with patch.object(sec_edgar, "_submissions") as sub, \
             patch.object(sec_edgar, "search_company", return_value=None) as search:
            assert sec_edgar.scrape_company("Unilever") is None
        search.assert_called_once_with("Unilever")
        sub.assert_not_called()


class TestTheRunnerPassesTheStoredCik:
    def _run(self, stored):
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner._stored_sec_cik", return_value=stored) as lookup, \
             patch("app.scraper.sec_edgar.scrape_company", return_value=None) as scrape:
            out = runner.run_scrape_sec_edgar("Nestlé", country="CH")
        return out, lookup, scrape

    def test_the_stored_cik_reaches_the_scrape(self):
        out, lookup, scrape = self._run("0000792990")
        lookup.assert_called_once_with("Nestlé", "CH")
        scrape.assert_called_once_with("Nestlé", cik="0000792990")
        assert out["status"] == "no_results"

    def test_no_stored_cik_means_the_name_search(self):
        _, _, scrape = self._run(None)
        scrape.assert_called_once_with("Nestlé", cik=None)


class TestStoredSecCik:
    def test_the_resolved_nodes_cik(self):
        with patch("app.routers.search.resolve_best_entity",
                   return_value={"id": "x", "sec_cik": "0000792990"}) as resolve:
            assert runner._stored_sec_cik("Nestlé", "CH") == "0000792990"
        resolve.assert_called_once_with("Nestlé", "CH")

    def test_no_node_or_no_cik_is_none(self):
        with patch("app.routers.search.resolve_best_entity", return_value=None):
            assert runner._stored_sec_cik("Nobody") is None
        with patch("app.routers.search.resolve_best_entity", return_value={"id": "x", "sec_cik": ""}):
            assert runner._stored_sec_cik("Unilever") is None

    def test_a_failing_lookup_is_the_name_search_not_a_failed_scrape(self):
        with patch("app.routers.search.resolve_best_entity", side_effect=RuntimeError("db down")):
            assert runner._stored_sec_cik("Nestlé") is None
