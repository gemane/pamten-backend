"""
Filer-side SEC ingestion — what a company OWNS, from the 13D/13G filings it makes
about others.

Everything else in the SEC scraper reads filings where the company is the
subject. An asset manager has none of those (privately held, not a listed
issuer), which is why Vanguard's node stayed empty however often it was scraped
while ~3,400 filings describing its holdings went unread.

The XML fixtures below are trimmed from real Vanguard filings. Network access is
mocked; the live query is verified separately against EDGAR.
"""
from unittest.mock import patch

import pytest

from app.scraper.sec_edgar import (
    _parse_holding_filing, fetch_filer_holdings, HOLDINGS_MAX_LOOKBACK,
)

# Real shape: schema X0202, default namespace, issuer + percent as fields.
XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g">
<formData><coverPageHeader><issuerInfo>
<issuerCik>{cik}</issuerCik><issuerName>{name}</issuerName>
</issuerInfo></coverPageHeader>
<items><item4><classPercent>{pct}</classPercent></item4></items>
</formData></edgarSubmission>"""


def _doc_for(docs: dict):
    """Serve a fixture by the accession embedded in the requested URL."""
    def _get_text(url):
        return docs[next(k for k in docs if k.replace("-", "") in url)]
    return _get_text


def _subs(filings):
    return {"filings": {"recent": {
        "form": [f[0] for f in filings],
        "accessionNumber": [f[1] for f in filings],
        "filingDate": [f[2] for f in filings],
    }}}


class TestParseHoldingFiling:
    def test_extracts_issuer_and_percent(self):
        with patch("app.scraper.sec_edgar._get_text",
                   return_value=XML.format(cik="0001555280", name="Zoetis Inc", pct="7.49")):
            row = _parse_holding_filing("0002100119", "0002100119-26-000001")
        assert row["subject_cik"] == "0001555280"
        assert row["subject_name"] == "Zoetis Inc"
        assert row["percent"] == 7.49

    def test_zero_percent_is_a_value_not_a_gap(self):
        # An amendment reporting 0% is the filer declaring it dropped below the
        # 5% threshold — the END of a holding, not a missing number.
        with patch("app.scraper.sec_edgar._get_text",
                   return_value=XML.format(cik="0001555280", name="Zoetis Inc", pct="0")):
            row = _parse_holding_filing("0002100119", "a-1")
        assert row["percent"] == 0.0

    def test_namespace_is_ignored(self):
        # The document declares a default namespace; a plain find("issuerCik") misses it.
        with patch("app.scraper.sec_edgar._get_text",
                   return_value=XML.format(cik="0000320193", name="Apple Inc", pct="5.5")):
            assert _parse_holding_filing("0002100119", "a-1")["subject_name"] == "Apple Inc"

    def test_pre_xml_filing_is_skipped_not_guessed(self):
        # Older filings have no primary_doc.xml and 404 here.
        with patch("app.scraper.sec_edgar._get_text", side_effect=Exception("404")):
            assert _parse_holding_filing("0002100119", "a-1") is None

    def test_unparseable_xml_is_skipped(self):
        with patch("app.scraper.sec_edgar._get_text", return_value="<not-xml"):
            assert _parse_holding_filing("0002100119", "a-1") is None

    def test_missing_issuer_is_skipped(self):
        with patch("app.scraper.sec_edgar._get_text",
                   return_value="<edgarSubmission><formData/></edgarSubmission>"):
            assert _parse_holding_filing("0002100119", "a-1") is None


class TestFetchFilerHoldings:
    def test_returns_live_stakes(self):
        subs = _subs([("SCHEDULE 13G", "a-1", "2026-04-30"),
                      ("SCHEDULE 13G", "a-2", "2026-04-29")])
        docs = {"a-1": XML.format(cik="0000105770", name="West Pharmaceutical", pct="7.48"),
                "a-2": XML.format(cik="0000859737", name="Hologic Inc", pct="7.49")}
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
            rows = fetch_filer_holdings("0002100119")
        assert {r["subject_name"] for r in rows} == {"West Pharmaceutical", "Hologic Inc"}
        assert all(r["until"] is None for r in rows)

    def test_a_later_zero_closes_the_earlier_stake(self):
        # The Vanguard case: the newest filing for a company reports 0%, so the
        # last real percentage is recorded as history with the exit date.
        subs = _subs([("SCHEDULE 13G/A", "a-new", "2026-03-27"),
                      ("SCHEDULE 13G",   "a-old", "2025-08-07")])
        docs = {"a-new": XML.format(cik="0000821189", name="EOG Resources", pct="0"),
                "a-old": XML.format(cik="0000821189", name="EOG Resources", pct="10.01")}
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
            rows = fetch_filer_holdings("0000102909")
        assert len(rows) == 1
        assert rows[0]["stake_percent"] == 10.01
        assert rows[0]["until"] == "2026-03-27", "the exit date must come from the newer filing"

    def test_a_company_only_ever_zero_is_not_reported(self):
        subs = _subs([("SCHEDULE 13G/A", f"a-{i}", f"2026-03-{20 + i:02d}")
                      for i in range(HOLDINGS_MAX_LOOKBACK + 2)])
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text",
                   return_value=XML.format(cik="0000111111", name="Never Held", pct="0")):
            assert fetch_filer_holdings("0000102909") == []

    def test_only_the_newest_filing_per_company_is_used(self):
        subs = _subs([("SCHEDULE 13G/A", "a-1", "2026-04-30"),
                      ("SCHEDULE 13G/A", "a-2", "2026-01-30")])
        docs = {"a-1": XML.format(cik="0000859737", name="Hologic Inc", pct="7.49"),
                "a-2": XML.format(cik="0000859737", name="Hologic Inc", pct="6.10")}
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
            rows = fetch_filer_holdings("0002100119")
        assert len(rows) == 1
        assert rows[0]["stake_percent"] == 7.49


class TestBounds:
    def test_limit_caps_the_companies_returned(self):
        subs = _subs([("SCHEDULE 13G", f"a-{i}", f"2026-04-{i + 1:02d}") for i in range(5)])
        counter = {"n": 0}

        def _doc(_url):
            counter["n"] += 1
            return XML.format(cik=f"000000{counter['n']:04d}", name=f"Co {counter['n']}", pct="6")

        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc):
            assert len(fetch_filer_holdings("0002100119", limit=2)) == 2

    def test_max_filings_bounds_the_fetches(self):
        # The subject is only knowable by fetching the filing — the submissions
        # index doesn't carry it — so filings can't be de-duplicated in advance.
        # Without this bound, a filer with 3,000 amendments is 3,000 requests.
        subs = _subs([("SCHEDULE 13G/A", f"a-{i}", "2026-03-27") for i in range(500)])
        calls = {"n": 0}

        def _doc(_url):
            calls["n"] += 1
            return XML.format(cik="0000111111", name="Never Held", pct="0")

        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc):
            fetch_filer_holdings("0002100119", limit=10, max_filings=20)
        assert calls["n"] == 20

    def test_a_company_that_files_nothing_costs_one_request(self):
        # The common case: not an institutional filer, so no documents are read.
        subs = _subs([("10-K", "a-1", "2026-01-01"), ("8-K", "a-2", "2026-02-01")])
        with patch("app.scraper.sec_edgar._get", return_value=subs) as g, \
             patch("app.scraper.sec_edgar._get_text") as t:
            assert fetch_filer_holdings("0000320193") == []
        assert g.call_count == 1
        assert not t.called, "no filing documents should be fetched"

    def test_a_failed_submissions_read_is_not_an_error(self):
        with patch("app.scraper.sec_edgar._get", side_effect=Exception("boom")):
            assert fetch_filer_holdings("0002100119") == []


def test_scrape_company_can_skip_holdings():
    # A caller that doesn't want the extra fetches passes 0.
    from app.scraper import sec_edgar
    with patch.object(sec_edgar, "search_company", return_value={"cik": "1", "name": "X"}), \
         patch.object(sec_edgar, "fetch_former_names", return_value=[]), \
         patch.object(sec_edgar, "fetch_company_lei", return_value=None), \
         patch.object(sec_edgar, "fetch_ownership_filings", return_value=[]), \
         patch.object(sec_edgar, "fetch_executives", return_value=[]), \
         patch.object(sec_edgar, "fetch_shares_outstanding", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_holdings") as fh:
        data = sec_edgar.scrape_company("X", holdings_limit=0)
    assert data["holdings"] == []
    assert not fh.called


@pytest.mark.parametrize("form", ["SCHEDULE 13G", "SCHEDULE 13G/A", "SC 13D", "SC 13G/A"])
def test_all_13dg_form_spellings_are_recognised(form):
    # EDGAR uses both "SCHEDULE 13G" and the older "SC 13G".
    subs = _subs([(form, "a-1", "2026-04-30")])
    with patch("app.scraper.sec_edgar._get", return_value=subs), \
         patch("app.scraper.sec_edgar._get_text",
               return_value=XML.format(cik="0000859737", name="Hologic Inc", pct="7.49")):
        assert len(fetch_filer_holdings("0002100119")) == 1
