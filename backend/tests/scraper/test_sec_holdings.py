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
    _parse_holding_filing, fetch_filer_holdings,
)

# Schema X0202, 13G spelling (`issuerCik`, `classPercent`), trimmed from the real
# Vanguard-Capital-Management/Apple filing 0002100119-26-000139.
#
# The cover's reporting-person block matters and was missing from the earlier
# version of this fixture: a real 13G states the percent per person there, and
# repeats a (sometimes different) figure under `items`. Without the block, the
# fixture agreed with a parser that took the first `classPercent` anywhere —
# so the fixture could not have caught a scoping mistake, and the shape it
# described was one EDGAR never emits.
XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g">
<formData><coverPageHeader><issuerInfo>
<issuerCik>{cik}</issuerCik><issuerName>{name}</issuerName>
</issuerInfo>
<coverPageHeaderReportingPersonDetails>
<reportingPersonName>Vanguard Capital Management</reportingPersonName>
<reportingPersonBeneficiallyOwnedNumberOfShares>
<soleVotingPower>145321305</soleVotingPower><sharedVotingPower>0</sharedVotingPower>
<soleDispositivePower>1099168953</soleDispositivePower><sharedDispositivePower>0</sharedDispositivePower>
</reportingPersonBeneficiallyOwnedNumberOfShares>
<reportingPersonBeneficiallyOwnedAggregateNumberOfShares>1099168953</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
<classPercent>{pct}</classPercent>
<typeOfReportingPerson>IA</typeOfReportingPerson>
</coverPageHeaderReportingPersonDetails>
</coverPageHeader>
<items><item4><classPercent>99.9</classPercent></item4></items>
</formData></edgarSubmission>"""

# A Schedule 13D — the OTHER schema. Trimmed from 0001213900-26-094095
# (Blue Bird Capital / Tactical Resources). Different tag spellings for the
# same facts, which is why a 13G-only parser silently returned None for every
# 13D even though `_HOLDING_FORMS` includes them.
XML_13D = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D">
<formData><coverPageHeader>
<issuerInfo><issuerCIK>{cik}</issuerCIK><issuerName>{name}</issuerName></issuerInfo>
</coverPageHeader>
<reportingPersons><reportingPersonInfo>
<reportingPersonCIK>0002110854</reportingPersonCIK>
<reportingPersonName>Blue Bird Capital Enterprises LLC</reportingPersonName>
<soleVotingPower>1598232.00</soleVotingPower><sharedVotingPower>0.00</sharedVotingPower>
<soleDispositivePower>1598232.00</soleDispositivePower><sharedDispositivePower>0.00</sharedDispositivePower>
<aggregateAmountOwned>1598232.00</aggregateAmountOwned>
<percentOfClass>{pct}</percentOfClass>
<typeOfReportingPerson>OO</typeOfReportingPerson>
</reportingPersonInfo></reportingPersons>
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

    def test_every_holding_says_it_was_read_from_the_xml(self):
        """The scrape's holdings loop writes `holding["read_from"]` as it comes;
        a row without it left the edge ungraded (681 after the 2026-10-27 rebuild)."""
        subs = _subs([("SCHEDULE 13G", "a-1", "2026-04-30"),
                      ("SCHEDULE 13D", "a-2", "2026-04-29")])
        docs = {"a-1": XML.format(cik="0000105770", name="West Pharmaceutical", pct="7.48"),
                "a-2": XML_13D.format(cik="0000859737", name="Hologic Inc", pct="7.49")}
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
            rows = fetch_filer_holdings("0002100119")
        assert len(rows) == 2 and {r["read_from"] for r in rows} == {"field"}

    def test_a_holding_carries_its_date_of_event(self):
        import pathlib
        real = (pathlib.Path(__file__).parent / "fixtures" / "13g_vanguard.xml").read_text()
        subs = _subs([("SCHEDULE 13G", "a-1", "2026-04-30")])
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for({"a-1": real})):
            rows = fetch_filer_holdings("0002100119")
        assert rows and rows[0]["event_date"] == "2026-03-31"
        assert rows[0]["file_date"] == "2026-04-30"

    def test_a_final_report_is_a_holding_dimmed_from_its_date_of_event(self):
        """Findings item 35: BlackRock, Inc.'s 13G/A on Tesla of 2026-10-27 ticks
        Item 5 at 4.96 % as of 2026-09-30 — its last report, not a sale."""
        import pathlib
        real = (pathlib.Path(__file__).parent / "fixtures" / "13ga_blackrock_tesla_final.xml").read_text()
        subs = _subs([("SCHEDULE 13G/A", "a-1", "2026-10-27")])
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for({"a-1": real})):
            rows = fetch_filer_holdings("0002012383")
        assert [(r["stake_percent"], r["until"], r["final_report"]) for r in rows] == [(4.96, None, "2026-09-30")]

    def test_a_live_holding_is_no_final_report(self):
        subs = _subs([("SCHEDULE 13G", "a-1", "2026-04-30")])
        docs = {"a-1": XML.format(cik="0000105770", name="West Pharmaceutical", pct="7.48")}
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
            rows = fetch_filer_holdings("0002100119")
        assert rows[0]["final_report"] is None

    def test_a_filing_about_the_filer_itself_is_not_a_holding(self):
        """EDGAR's index for a CIK carries filings the company is NAMED IN as well
        as ones it submitted, so somebody else's 13G about it appears here — and
        reading that filing's issuer hands the company straight back.

        The result was nine companies owning themselves, Apple, Microsoft and
        Alphabet among them, each "holding" a percentage that was really a third
        party's stake in it. `fetch_filer_holdings('0000320193')` returned exactly
        one row: Apple Inc, 7.48% of Apple Inc.
        """
        subs = _subs([("SCHEDULE 13G", "inbound", "2026-04-30"),
                      ("SCHEDULE 13G", "outbound", "2026-04-29")])
        docs = {
            # Somebody's 13G about Apple, sitting in Apple's own filing index.
            "inbound":  XML.format(cik="0000320193", name="Apple Inc", pct="7.48"),
            "outbound": XML.format(cik="0000105770", name="West Pharmaceutical", pct="6.1"),
        }
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
            rows = fetch_filer_holdings("0000320193")

        assert [r["subject_name"] for r in rows] == ["West Pharmaceutical"], \
            "the filer was reported as holding itself"

    def test_the_filer_is_matched_however_its_cik_is_written(self):
        # The subject arrives zero-padded from the filing; the caller's CIK may not
        # be. Comparing them raw would let every inbound filing straight through.
        subs = _subs([("SCHEDULE 13G", "inbound", "2026-04-30")])
        docs = {"inbound": XML.format(cik="0000320193", name="Apple Inc", pct="7.48")}
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
            assert fetch_filer_holdings("320193") == []

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
                      for i in range(5)])
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
         patch.object(sec_edgar, "fetch_shares_outstanding_detail", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_holdings") as fh:
        data = sec_edgar.scrape_company("X", holdings_limit=0)
    assert data["holdings"] == []
    assert not fh.called


@pytest.mark.parametrize("form", ["SCHEDULE 13G", "SCHEDULE 13G/A", "SCHEDULE 13D"])
def test_the_new_13dg_spellings_are_read_from_xml(form):
    # Since December 2024 EDGAR calls them "SCHEDULE 13G" and files XML.
    subs = _subs([(form, "a-1", "2026-04-30")])
    with patch("app.scraper.sec_edgar._get", return_value=subs), \
         patch("app.scraper.sec_edgar._get_text",
               return_value=XML.format(cik="0000859737", name="Hologic Inc", pct="7.49")):
        assert len(fetch_filer_holdings("0002100119")) == 1


# ── Old "SC 13D/G" filings: no XML, a cover page (findings item 44) ───────────
#
# Before December 2024 there is no primary_doc.xml. The holdings path asked for
# it anyway on every old filing — a certain 404, up to 175 per filer and scrape,
# never cached — and read nothing. Fixtures are the real Altaba → Alibaba 13G/A
# of 2019-02-14 (index page trimmed to its document table and filer blocks, the
# cover document as its plain text) and Farallon's 13G/A ABOUT Altaba, which
# Altaba's own filing index lists too.

def _fixture(name: str) -> str:
    import pathlib
    return (pathlib.Path(__file__).parent / "fixtures" / name).read_text()


ALTABA = "0001011006"
OLD_OUT = "0001193125-19-039407"       # Altaba's own 13G/A on Alibaba
OLD_IN = "0001214659-20-001388"        # Farallon's 13G/A on Altaba


def _old_docs(cover=None, requested=None):
    pages = {
        OLD_OUT.replace("-", "") + "/" + OLD_OUT + "-index.htm": _fixture("13ga_old_altaba_alibaba_index.htm"),
        "d657317dsc13ga.htm": cover if cover is not None else _fixture("13ga_old_altaba_alibaba.txt"),
        OLD_IN.replace("-", "") + "/" + OLD_IN + "-index.htm": _fixture("13ga_old_farallon_altaba_index.htm"),
    }

    def _get_text(url, params=None):
        if requested is not None:
            requested.append(url)
        for key, body in pages.items():
            if url.endswith(key):
                return body
        import httpx
        raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url),
                                    response=httpx.Response(404))
    return _get_text


def _old_holdings(filings, cover=None, requested=None, former=()):
    from app.scraper import sec_edgar
    with patch("app.scraper.sec_edgar._get", return_value=_subs(filings)), \
         patch("app.scraper.sec_edgar._get_text", side_effect=_old_docs(cover, requested)), \
         patch.object(sec_edgar, "fetch_filer_name", return_value="ALTABA INC."), \
         patch.object(sec_edgar, "fetch_former_names", return_value=list(former)):
        return fetch_filer_holdings(ALTABA)


class TestOldFilings:
    def test_an_old_13g_is_read_from_its_cover_page(self):
        rows = _old_holdings([("SC 13G/A", OLD_OUT, "2019-02-14")])
        assert len(rows) == 1
        h = rows[0]
        assert (h["subject_cik"], h["subject_name"]) == ("0001577552", "Alibaba Group Holding Ltd")
        assert h["stake_percent"] == 11.0          # Altaba Inc.'s own cover, row 11
        assert h["event_date"] == "2018-12-31"
        assert (h["since"], h["since_basis"]) == ("2018-12-31", "amendment")
        assert h["read_from"] == "form"            # the SEC's numbered rows, not XML fields
        assert h["filing_type"] == "13G/A"
        assert h["source_url"].endswith(OLD_OUT + "-index.htm")

    def test_a_joint_filing_is_read_from_the_filers_own_cover_page(self):
        # The same 13G/A carries Altaba Holdings Hong Kong Limited on its second
        # cover page at 9.7 %; read as that filer, the first page's 11.0 % is
        # someone else's figure.
        from app.scraper import sec_edgar
        with patch("app.scraper.sec_edgar._get", return_value=_subs([("SC 13G/A", OLD_OUT, "2019-02-14")])), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_old_docs()), \
             patch.object(sec_edgar, "fetch_filer_name", return_value="Altaba Holdings Hong Kong Limited"), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            rows = fetch_filer_holdings(ALTABA)
        assert [r["stake_percent"] for r in rows] == [9.7]

    def test_no_xml_is_asked_for_an_old_filing(self):
        requested: list[str] = []
        _old_holdings([("SC 13G/A", OLD_OUT, "2019-02-14"), ("SC 13G/A", OLD_IN, "2020-02-14")],
                      requested=requested)
        assert not [u for u in requested if u.endswith("primary_doc.xml")]

    def test_a_filing_about_the_filer_costs_only_its_index_page(self):
        requested: list[str] = []
        rows = _old_holdings([("SC 13G/A", OLD_IN, "2020-02-14")], requested=requested)
        assert rows == []
        assert requested == [f"https://www.sec.gov/Archives/edgar/data/1011006/"
                             f"{OLD_IN.replace('-', '')}/{OLD_IN}-index.htm"]

    def test_a_cover_naming_another_issuer_is_dropped(self):
        # The index page's subject is the filing agent's say (Embraer's agent
        # filed its Eve Holding 13D/A under EMBRAER); the cover decides.
        cover = _fixture("13ga_old_altaba_alibaba.txt").replace(
            "Alibaba Group Holding Limited (Name of Issuer)", "Weibo Corporation (Name of Issuer)")
        assert _old_holdings([("SC 13G/A", OLD_OUT, "2019-02-14")], cover=cover) == []

    def test_a_cover_under_the_subjects_former_name_is_kept(self):
        cover = _fixture("13ga_old_altaba_alibaba.txt").replace(
            "Alibaba Group Holding Limited (Name of Issuer)", "Yahoo! Inc. (Name of Issuer)")
        rows = _old_holdings([("SC 13G/A", OLD_OUT, "2019-02-14")], cover=cover,
                             former=["Yahoo! Inc.|2017-06-16"])
        assert [r["subject_cik"] for r in rows] == ["0001577552"]

    def test_an_unreadable_cover_is_skipped_not_an_exit(self):
        rows = _old_holdings([("SC 13G/A", OLD_OUT, "2019-02-14")],
                             cover="Alibaba Group Holding Limited (Name of Issuer) nothing else")
        assert rows == []

    def test_old_and_new_filings_mix_newest_first(self):
        # A zero in the new XML closes the stake an old cover disclosed.
        from app.scraper import sec_edgar
        zero = XML.format(cik="0001577552", name="Alibaba Group Holding Limited", pct="0")
        old = _old_docs()

        def _get_text(url, params=None):
            return zero if url.endswith("primary_doc.xml") else old(url)
        subs = _subs([("SCHEDULE 13G/A", "0001011006-25-000001", "2025-02-14"),
                      ("SC 13G/A", OLD_OUT, "2019-02-14")])
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_get_text), \
             patch.object(sec_edgar, "fetch_filer_name", return_value="ALTABA INC."), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            rows = fetch_filer_holdings(ALTABA)
        assert [(r["stake_percent"], r["until"], r["read_from"]) for r in rows] == [(11.0, "2025-02-14", "form")]


# ── Archived filing pages ─────────────────────────────────────────────────────
#
# EDGAR splits a prolific filer's index into `filings.recent` plus archive pages.
# Vanguard's old CIK has 13, back to 1999. The stake that matters is often only
# reachable there: its Microsoft holding was disclosed on 2024-02-13 and closed by
# a 0% amendment in March 2026, so the exit is in `recent` and the percentage it
# ends is not. Without the archive the holding can never be closed — the scraper
# sees an exit for a stake it has no record of.

def _subs_with_archive(recent, pages):
    return {"filings": {
        "recent": {"form": [f[0] for f in recent],
                   "accessionNumber": [f[1] for f in recent],
                   "filingDate": [f[2] for f in recent]},
        "files": [{"name": f"page-{i}.json"} for i in range(len(pages))],
    }}


def _page(filings):
    return {"form": [f[0] for f in filings],
            "accessionNumber": [f[1] for f in filings],
            "filingDate": [f[2] for f in filings]}


class TestArchivePages:
    def test_a_stake_disclosed_in_an_archive_is_closed_by_a_recent_exit(self):
        # The Microsoft case exactly.
        recent = [("SCHEDULE 13G/A", "a-exit", "2026-03-27")]
        archive = [("SCHEDULE 13G", "a-old", "2024-02-13")]
        subs = _subs_with_archive(recent, [archive])
        docs = {"a-exit": XML.format(cik="0000789019", name="Microsoft Corp", pct="0"),
                "a-old":  XML.format(cik="0000789019", name="Microsoft Corp", pct="8.95")}

        def _get(url):
            return _page(archive) if "page-0" in url else subs

        with patch("app.scraper.sec_edgar._get", side_effect=_get), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
            rows = fetch_filer_holdings("0000102909")

        assert len(rows) == 1
        assert rows[0]["stake_percent"] == 8.95
        assert rows[0]["until"] == "2026-03-27"

    def test_the_archive_is_not_read_when_recent_already_satisfies_the_limit(self):
        recent = [("SCHEDULE 13G", f"a-{i}", f"2026-04-{i + 1:02d}") for i in range(3)]
        subs = _subs_with_archive(recent, [[("SCHEDULE 13G", "old-1", "2020-01-01")]])
        fetched_pages = []

        def _get(url):
            if "page-" in url:
                fetched_pages.append(url)
                return _page([])
            return subs

        counter = {"n": 0}

        def _doc(_url):
            counter["n"] += 1
            return XML.format(cik=f"000000{counter['n']:04d}", name=f"Co {counter['n']}", pct="6")

        with patch("app.scraper.sec_edgar._get", side_effect=_get), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc):
            rows = fetch_filer_holdings("0000102909", limit=2)

        assert len(rows) == 2
        assert fetched_pages == [], "an archive page was fetched despite the limit being met"

    def test_a_failed_archive_page_stops_cleanly(self):
        recent = [("SCHEDULE 13G/A", "a-exit", "2026-03-27")]
        subs = _subs_with_archive(recent, [[("SCHEDULE 13G", "old", "2024-01-01")]])

        def _get(url):
            if "page-" in url:
                raise RuntimeError("archive 500")
            return subs

        with patch("app.scraper.sec_edgar._get", side_effect=_get), \
             patch("app.scraper.sec_edgar._get_text",
                   return_value=XML.format(cik="0000789019", name="Microsoft Corp", pct="0")):
            assert fetch_filer_holdings("0000102909") == []

    def test_archive_pages_are_capped(self):
        from app.scraper.sec_edgar import HOLDINGS_MAX_ARCHIVE_PAGES
        recent = [("SCHEDULE 13G/A", "a-0", "2026-03-27")]
        subs = {"filings": {
            "recent": _page(recent),
            "files": [{"name": f"page-{i}.json"} for i in range(20)],
        }}
        pages_read = []

        def _get(url):
            if "page-" in url:
                pages_read.append(url)
                return _page([("SCHEDULE 13G/A", f"z-{len(pages_read)}", "2020-01-01")])
            return subs

        with patch("app.scraper.sec_edgar._get", side_effect=_get), \
             patch("app.scraper.sec_edgar._get_text",
                   return_value=XML.format(cik="0000789019", name="Never", pct="0")):
            fetch_filer_holdings("0000102909", limit=5)

        assert len(pages_read) <= HOLDINGS_MAX_ARCHIVE_PAGES


# ── Affiliated managers (13F cover page) ──────────────────────────────────────
#
# A fund group files one 13F per manager and the cover page names the group's
# OTHER managers with their CIKs — ten for Vanguard, including the entity that
# took over its 13G reporting. Authoritative group membership from the filer
# itself, for one document fetch.
#
# It says nothing about ownership: "reports 13F holdings alongside" is the whole
# claim, which is why it becomes RELATED_TO{relation:'affiliate'} and not OWNS.

from app.scraper.sec_edgar import fetch_affiliated_managers  # noqa: E402

NT_XML = """<?xml version="1.0"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
<formData><coverPage>
<filingManager><name>VANGUARD GROUP INC</name></filingManager>
<reportType>13F NOTICE</reportType>
<otherManagersInfo>
  <otherManager><cik>0002100119</cik><name>VANGUARD CAPITAL MANAGEMENT LLC</name></otherManager>
  <otherManager><cik>000217448</cik><name>VANGUARD MARKETING CORPORATION</name></otherManager>
  <otherManager><name>NO CIK PARTNER</name></otherManager>
</otherManagersInfo>
</coverPage></formData></edgarSubmission>"""


class TestAffiliatedManagers:
    def _run(self, forms, xml=NT_XML):
        subs = _subs(forms)
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", return_value=xml):
            return fetch_affiliated_managers("0000102909")

    def test_reads_the_other_managers(self):
        rows = self._run([("13F-NT", "a-1", "2026-05-08")])
        assert [r["name"] for r in rows] == [
            "VANGUARD CAPITAL MANAGEMENT LLC", "VANGUARD MARKETING CORPORATION", "NO CIK PARTNER"]

    def test_pads_a_short_cik(self):
        # The real Vanguard notice carries one at 9 digits; unpadded it would not
        # match an EDGAR-sourced node, which always stores 10.
        rows = self._run([("13F-NT", "a-1", "2026-05-08")])
        assert rows[1]["cik"] == "0000217448"

    def test_a_manager_without_a_cik_is_kept_by_name(self):
        assert self._run([("13F-NT", "a-1", "2026-05-08")])[2]["cik"] is None

    def test_uses_the_newest_13f(self):
        forms = [("13F-HR", "old", "2025-01-01"), ("13F-NT", "new", "2026-05-08")]
        subs = _subs(forms)
        seen = {}

        def _doc(url):
            seen["url"] = url
            return NT_XML

        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc):
            fetch_affiliated_managers("0000102909")
        assert "new" in seen["url"].replace("-", "")

    def test_a_non_13f_filer_costs_no_document_fetch(self):
        subs = _subs([("10-K", "a-1", "2026-01-01")])
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text") as t:
            assert fetch_affiliated_managers("0000320193") == []
        assert not t.called

    def test_provenance_points_at_the_filing(self):
        rows = self._run([("13F-NT", "a-1", "2026-05-08")])
        assert rows[0]["source_date"] == "2026-05-08"
        assert rows[0]["source_url"], "no link back to the filing"

    def test_an_unparseable_cover_page_yields_nothing(self):
        assert self._run([("13F-NT", "a-1", "2026-05-08")], xml="<not-xml") == []


# ── A group member's own stake, not the bloc (findings item 98) ─────────────
#
# The filer side read the FIRST reporting person's row 13 as the filer's stake.
# BRC's 13D/A on AB InBev (May 2026, nine reporting persons) starts with the
# Stichting, and every block states the bloc's 52.3 % — written as BRC's stake
# beside Altria's 51.9 %, AB InBev summed to 110 %. The issuer side split both
# long ago (`_stake_from_person`); the filer side now does the same.

BRC, ALTRIA, ABI = "0001301486", "0000764180", "0001668717"


def _xml_for(name):
    def _get_text(url):
        return _fixture(name)
    return _get_text


@pytest.mark.parametrize("rows, bloc", [
    ({"shared_voting": 1031302286, "sole_dispositive": 0, "shared_dispositive": 769317631}, True),   # BRC
    ({"shared_voting": 1020598157, "sole_dispositive": 159121937, "shared_dispositive": 0}, True),   # Altria
    ({"shared_voting": 35892479, "sole_dispositive": 0, "shared_dispositive": 35892479}, False),      # Berkshire/DaVita
    ({"sole_voting": 636056024, "shared_voting": 1398022815, "sole_dispositive": 636056024,
      "shared_dispositive": 1398022815}, False),                                                     # Microsoft/Telewest
    ({"shared_voting": 1325544, "shared_dispositive": 1607013}, False),     # votes fewer than it holds
    ({"sole_voting": 100, "sole_dispositive": 100}, False),
    ({}, False),
])
def test_votes_others_shares(rows, bloc):
    from app.scraper.sec_edgar import _votes_others_shares
    assert _votes_others_shares(rows) is bloc


class TestFilerSideBloc:
    def test_a_group_members_row_13_is_the_blocs_voting_power_not_its_stake(self):
        with patch("app.scraper.sec_edgar._get_text", side_effect=_xml_for("13da_brc_abinbev_2026.xml")):
            row = _parse_holding_filing(BRC, "0001193125-26-226626")
        assert row["subject_cik"] == ABI
        assert (row["percent"], row["stake"], row["voting"]) == (52.3, None, 52.3)

    def test_a_13d_block_is_found_by_the_filers_cik_without_a_name_lookup(self):
        # the Stichting's block first with another figure: BRC's own is read
        doc = _fixture("13da_brc_abinbev_2026.xml").replace(
            "<percentOfClass>52.3</percentOfClass>", "<percentOfClass>52.4</percentOfClass>", 1)
        with patch("app.scraper.sec_edgar._get_text", return_value=doc), \
             patch("app.scraper.sec_edgar.fetch_filer_name") as name:
            row = _parse_holding_filing(BRC, "0001193125-26-226626")
        assert row["percent"] == 52.3
        name.assert_not_called()

    def test_a_single_block_costs_no_name_lookup(self):
        with patch("app.scraper.sec_edgar._get_text",
                   return_value=XML.format(cik="0001555280", name="Zoetis Inc", pct="7.49")), \
             patch("app.scraper.sec_edgar.fetch_filer_name") as name:
            assert _parse_holding_filing("0002100119", "a-1")["stake"] == 7.49
        name.assert_not_called()

    def test_a_parent_filing_with_its_subsidiaries_keeps_row_13(self):
        # Berkshire's 13D/A on DaVita: nine reporting persons, "member of a
        # group" ticked, and Berkshire disposes of every share it votes — its
        # 45 % held through its subsidiaries, not a bloc. Here: BRC's block
        # given the same shape (dispositive = voting).
        doc = _fixture("13da_brc_abinbev_2026.xml").replace(
            "<sharedDispositivePower>769317631", "<sharedDispositivePower>1031302286")
        with patch("app.scraper.sec_edgar._get_text", return_value=doc):
            row = _parse_holding_filing(BRC, "0001193125-26-226626")
        assert (row["stake"], row["voting"]) == (52.3, None)

    def test_a_lone_party_to_a_voting_agreement_keeps_its_own_shares(self):
        # Altria files alone: disposes of 159,121,937 shares, votes 1,020,598,157
        with patch("app.scraper.sec_edgar._get_text", side_effect=_xml_for("13d_altria_abinbev.xml")):
            row = _parse_holding_filing(ALTRIA, "0000950170-25-016305")
        assert row["percent"] == 51.9 and row["voting"] == 51.9
        assert row["stake"] == pytest.approx(8.1, abs=0.05)

    def test_the_filers_own_block_is_read_not_the_first(self):
        # Berkshire's joint 13G/A on Sirius XM: Buffett is listed first; a 13G
        # block has no CIK, so the filer's name picks its own block
        with patch("app.scraper.sec_edgar._get_text",
                   side_effect=_xml_for("13ga_berkshire_siriusxm_2025.xml")), \
             patch("app.scraper.sec_edgar.fetch_filer_name", return_value="NATIONAL INDEMNITY CO"):
            row = _parse_holding_filing("0000070866", "a-1")
        assert (row["percent"], row["stake"], row["voting"]) == (31.9, 31.9, None)

    def test_a_filer_named_in_no_block_falls_back_to_the_first(self):
        with patch("app.scraper.sec_edgar._get_text",
                   side_effect=_xml_for("13ga_berkshire_siriusxm_2025.xml")), \
             patch("app.scraper.sec_edgar.fetch_filer_name", return_value="SOMEBODY ELSE LLC"):
            assert _parse_holding_filing("0000000001", "a-1")["percent"] == 37.1

    def test_the_scan_writes_the_bloc_as_voting_power(self):
        subs = _subs([("SCHEDULE 13D/A", "0001193125-26-226626", "2026-05-15")])
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_xml_for("13da_brc_abinbev_2026.xml")):
            rows = fetch_filer_holdings(BRC)
        assert len(rows) == 1
        assert (rows[0]["stake_percent"], rows[0]["voting_power_pct"], rows[0]["until"]) == (None, 52.3, None)

    def test_a_lone_filers_stake_is_unchanged(self):
        subs = _subs([("SCHEDULE 13G", "a-1", "2026-04-30")])
        with patch("app.scraper.sec_edgar._get", return_value=subs), \
             patch("app.scraper.sec_edgar._get_text",
                   return_value=XML.format(cik="0000105770", name="West Pharmaceutical", pct="7.48")):
            rows = fetch_filer_holdings("0002100119")
        assert (rows[0]["stake_percent"], rows[0]["voting_power_pct"]) == (7.48, None)


class TestFilerSideBlocOldCover:
    # BRC's 2016 13D/A on Ambev (SC 13D/A, text), four cover pages. BRC votes
    # and disposes of the same 11,283,145,506 shares — a family holding (Ambev
    # through AB InBev, which BRC's bloc controls), so row 13's 71.9 % stays
    # its stake. Fixtures: the real index page and the cover pages as plain
    # text (up to Item 1).
    ACC = "0001193125-16-545192"
    FIRST_DISP = "10 Shared Dispositive Power: 11,283,145,506"

    def _rows(self, form, cover=None):
        from app.scraper import sec_edgar
        pages = {self.ACC.replace("-", "") + "/" + self.ACC + "-index.htm": _fixture("13da_old_brc_ambev_index.htm"),
                 "d168543dsc13da.htm": cover or _fixture("13da_old_brc_ambev.txt")}

        def _get_text(url, params=None):
            return next(body for key, body in pages.items() if url.endswith(key))
        with patch("app.scraper.sec_edgar._get", return_value=_subs([(form, self.ACC, "2016-04-13")])), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_get_text), \
             patch.object(sec_edgar, "fetch_filer_name", return_value="BRC S.a.R.L."), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            return fetch_filer_holdings(BRC)

    def test_a_family_holding_keeps_row_13_as_its_stake(self):
        rows = self._rows("SC 13D/A")
        assert [(r["subject_name"], r["stake_percent"], r["voting_power_pct"]) for r in rows] == \
            [("AMBEV S.A.", 71.9, None)]

    def test_an_old_cover_voting_others_shares_gives_the_bloc_as_voting_power(self):
        # the same cover, BRC's own page disposing of fewer shares than it votes
        cover = _fixture("13da_old_brc_ambev.txt").replace(
            self.FIRST_DISP, "10 Shared Dispositive Power: 5,000,000,000", 1)
        rows = self._rows("SC 13D/A", cover)
        assert [(r["stake_percent"], r["voting_power_pct"]) for r in rows] == [(None, 71.9)]
