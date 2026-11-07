"""Form F-6 — a foreign issuer's ADS CUSIP (app/scraper/sec_f6.py, findings item 72).

Fixtures are excerpts of the real filings: Nestlé's F-6 POS of 2026-02-20 (the
blank form-of-receipt template, then the table stating "ADS CUSIP No.:
641069406"), its F-6/A of 2004 (the Rule 144A ADS 641069 50 5, bracketed and
line-broken codes, prose about CUSIP numbers) and Wesfarmers' F-6EF of 2008
("CUSIP #" with nothing after it)."""
import pathlib
from unittest.mock import patch

import httpx
import pytest

from app.scraper import sec_edgar, sec_f6

FIX = pathlib.Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIX / name).read_text()


class TestParse:
    def test_the_stated_code_not_the_blank_template(self):
        assert sec_f6.parse_ads_cusips(_fixture("f6_nestle_2026_excerpt.txt")) == ["641069406"]

    def test_grouped_bracketed_and_line_broken_codes(self):
        assert sec_f6.parse_ads_cusips(_fixture("f6_nestle_2004_excerpt.txt")) == ["641069505", "641069406"]

    def test_a_blank_receipt_states_none(self):
        assert sec_f6.parse_ads_cusips(_fixture("f6_wesfarmers_2008_excerpt.txt")) == []

    @pytest.mark.parametrize("text", [
        "CUSIP NUMBER: ________",
        "CUSIP NUMBER:&nbsp;&nbsp;_______",
        "a CUSIP number that is different from any CUSIP number",
        "CUSIP No.: 641069407",          # wrong check digit
        "CUSIP No.: 6410694061",         # ten characters
        "ADS ISIN: US6410694060",        # no CUSIP label
    ])
    def test_no_candidate(self, text):
        assert sec_f6.parse_ads_cusips(text) == []


def _subs(*rows):
    return {"filings": {"recent": {
        "form": [r[0] for r in rows], "accessionNumber": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows]}}}


NESTLE = _subs(("6-K", "0000-26-000001", "2026-03-01"),
               ("F-6EF", "0001193805-09-001403", "2009-07-07"),
               ("F-6 POS", "0001193805-26-000160", "2026-02-20"),
               ("424B3", "0001193805-08-001663", "2008-07-03"),
               ("F-6/A", "0001193805-04-000900", "2004-06-14"),
               ("F-6", "0001193805-04-000683", "2004-04-27"))


class TestFilings:
    def test_the_f6_family_newest_first(self):
        assert [f["form"] for f in sec_f6.f6_filings(NESTLE)] == ["F-6 POS", "F-6EF", "F-6/A", "F-6"]

    def test_capped(self):
        assert len(sec_f6.f6_filings(NESTLE, max_filings=2)) == 2

    def test_the_full_submission_text(self):
        assert sec_f6.submission_url("0000792990", "0001193805-26-000160") == (
            "https://www.sec.gov/Archives/edgar/data/792990/000119380526000160/0001193805-26-000160.txt")


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://www.sec.gov/x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, request=req))


class TestFetch:
    def test_stops_at_the_newest_filing_that_states_one(self):
        texts = {"0001193805-26-000160": "CUSIP NUMBER: ____",          # newest: template only
                 "0001193805-09-001403": _fixture("f6_nestle_2026_excerpt.txt"),
                 "0001193805-04-000900": _fixture("f6_nestle_2004_excerpt.txt")}
        with patch.object(sec_edgar, "_submissions", return_value=NESTLE), \
             patch.object(sec_edgar, "_get_text", side_effect=lambda url: texts[url.rsplit("/", 1)[1][:-4]]) as get:
            out = sec_f6.fetch_ads_cusips("0000792990")
        assert out["cusips"] == ["641069406"] and out["filings_read"] == 2
        assert out["source_url"].endswith("/000119380509001403/0001193805-09-001403.txt")
        assert get.call_count == 2, "the 2004 filing is not read"

    def test_a_filer_without_f6_costs_the_submissions_read_only(self):
        with patch.object(sec_edgar, "_submissions", return_value=_subs(("10-K", "a", "2026-01-01"))), \
             patch.object(sec_edgar, "_get_text") as get:
            assert sec_f6.fetch_ads_cusips("0000320193") == {"cusips": [], "source_url": None, "filings_read": 0}
        get.assert_not_called()

    def test_none_stated_anywhere(self):
        with patch.object(sec_edgar, "_submissions", return_value=NESTLE), \
             patch.object(sec_edgar, "_get_text", return_value=_fixture("f6_wesfarmers_2008_excerpt.txt")):
            assert sec_f6.fetch_ads_cusips("0001436381") == {"cusips": [], "source_url": None, "filings_read": 4}

    def test_a_missing_filing_is_skipped(self):
        def get(url):
            if "000119380526000160" in url:
                raise _http_error(404)
            return _fixture("f6_nestle_2026_excerpt.txt")
        with patch.object(sec_edgar, "_submissions", return_value=NESTLE), patch.object(sec_edgar, "_get_text", side_effect=get):
            assert sec_f6.fetch_ads_cusips("0000792990")["cusips"] == ["641069406"]

    def test_a_lost_request_stops_the_reading_and_marks_the_run(self):
        # an older filing must not stand in for the newer one the network lost
        token = sec_edgar._failed_sections.set([])
        try:
            with patch.object(sec_edgar, "_submissions", return_value=NESTLE), \
                 patch.object(sec_edgar, "_get_text", side_effect=httpx.ReadTimeout("slow")) as get:
                assert sec_f6.fetch_ads_cusips("0000792990")["cusips"] == []
            assert get.call_count == 1
            assert sec_edgar._failed_sections.get() == [sec_f6.SECTION]
        finally:
            sec_edgar._failed_sections.reset(token)

    def test_a_lost_submissions_read_marks_the_run(self):
        token = sec_edgar._failed_sections.set([])
        try:
            with patch.object(sec_edgar, "_submissions", side_effect=_http_error(503)):
                assert sec_f6.fetch_ads_cusips("0000792990")["cusips"] == []
            assert sec_edgar._failed_sections.get() == [sec_f6.SECTION]
        finally:
            sec_edgar._failed_sections.reset(token)


class TestScrape:
    def test_the_scrape_carries_the_ads_cusips(self):
        stubs = {"company_by_cik": {"cik": "0000792990", "name": "NESTLE SA"},
                 "fetch_former_names": [], "fetch_company_lei": None, "fetch_ownership_filings": [],
                 "fetch_executives": [], "fetch_filer_holdings": [], "fetch_shares_outstanding_detail": None}
        with patch.multiple(sec_edgar, **{k: (lambda *a, _v=v, **k2: _v) for k, v in stubs.items()}), \
             patch.object(sec_f6, "fetch_ads_cusips",
                          return_value={"cusips": ["641069406"], "source_url": "u", "filings_read": 1}):
            data = sec_edgar.scrape_company("Nestlé", cik="0000792990")
        assert data["ads_cusips"] == ["641069406"] and data["ads_cusip_url"] == "u"
