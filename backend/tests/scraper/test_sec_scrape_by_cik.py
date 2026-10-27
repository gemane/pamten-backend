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
             patch("app.scraper.runner._stored_entity",
                   return_value={"id": "x", "sec_cik": stored} if stored else None) as lookup, \
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


class TestTheStoredNodesLeiForTheFiler:
    """`_confirm_stored_lei`: the node a query resolved to holds an LEI and no
    CIK; the filer EDGAR's name search found states no LEI; the names key
    alike. Then — and only then — the filer's own filings are asked, and a
    confirmation sends the write to that node (Unilever, 2026-10-27)."""
    STORED = {"id": "gb-coh:00041424", "name": "Unilever Plc", "name_normalized": "unilever",
              "lei_id": "549300MKFYEKVRWML317", "sec_cik": None}
    URL = "https://www.sec.gov/Archives/edgar/data/217410/000021741023000039/a991-directorpdmrshareho.htm"

    def _data(self, **kw):
        return {"cik": "0000217410", "name": "UNILEVER PLC", "lei": None, **kw}

    def test_a_confirmed_lei_joins_the_data(self):
        data = self._data()
        with patch("app.scraper.sec_lei.confirm_filer_lei",
                   return_value={"source_url": self.URL}) as confirm:
            runner._confirm_stored_lei(dict(self.STORED), data)
        confirm.assert_called_once_with("0000217410", "549300MKFYEKVRWML317",
                                        ["UNILEVER PLC", "Unilever Plc"])
        assert data["lei"] == "549300MKFYEKVRWML317"
        assert data["lei_source_url"] == self.URL

    def test_not_confirmed_changes_nothing(self):
        data = self._data()
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None):
            runner._confirm_stored_lei(dict(self.STORED), data)
        assert data == self._data()

    @pytest.mark.parametrize("stored, data", [
        (None, {}),                                                   # no node
        ({"sec_cik": "0000217410"}, {}),                              # the node has a CIK
        ({"lei_id": None}, {}),                                       # the node has no LEI
        ({}, {"lei": "549300MKFYEKVRWML317"}),                        # EDGAR states an LEI
        ({}, {"cik": None}),                                          # no filer number
        ({"name": "Unilever Ventures", "name_normalized": "unilever ventures"}, {}),  # names differ
    ])
    def test_only_the_one_shape_asks(self, stored, data):
        node = None if stored is None else {**self.STORED, **stored}
        with patch("app.scraper.sec_lei.confirm_filer_lei") as confirm:
            runner._confirm_stored_lei(node, self._data(**data))
        confirm.assert_not_called()

    def test_a_node_without_a_stored_key_is_keyed_from_its_name(self):
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None) as confirm:
            runner._confirm_stored_lei({**self.STORED, "name_normalized": None}, self._data())
        confirm.assert_called_once()

    def test_the_runner_asks_after_the_country_check(self):
        """A filer registered elsewhere is refused before EDGAR is asked anything more."""
        scraped = self._data(former_names=[], ownership_filings=[], holdings=[], executives=[])
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner._stored_entity", return_value=dict(self.STORED)), \
             patch("app.scraper.sec_edgar.scrape_company", return_value=scraped), \
             patch("app.scraper.sec_edgar.fetch_filer_country", return_value="GB"), \
             patch("app.scraper.runner._confirm_stored_lei") as confirm:
            out = runner.run_scrape_sec_edgar("Unilever", country="US")
        assert out["status"] == "country_mismatch"
        confirm.assert_not_called()
