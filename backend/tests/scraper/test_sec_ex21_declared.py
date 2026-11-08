"""Exhibit 21 discovery by the type the filing index declares (findings item 81).

The folder listing (`index.json`) gives file names only; the filing index page
`{accession}-index.htm` says what each document IS. 281 of the 3,638 10-Ks of
2026 Q1–Q3 that carry an EX-21 named it so that no filename pattern finds it.
Fixtures are the REAL index pages of AMD's and Lennar's 10-Ks and Unilever's
20-F (the last trimmed to six of its documents), and AMD's real Exhibit 21."""
from pathlib import Path
from unittest.mock import patch

import pytest

from app.scraper import sec_ex21 as ex

FX = Path(__file__).parent / "fixtures"
AMD_INDEX = (FX / "amd_10k_2026_index.htm").read_text()
LENNAR_INDEX = (FX / "lennar_10k_2026_index.htm").read_text()
UNILEVER_INDEX = (FX / "unilever_20f_2026_index.htm").read_text()
AMD_EX21 = (FX / "amd_exh21_10kfy25.htm").read_text()


def _page(*docs):
    """An index page in EDGAR's shape: (file name, declared type) rows."""
    rows = "".join(
        f'<tr><td scope="row">{i}</td><td scope="row">{kind}</td>'
        f'<td scope="row"><a href="/Archives/edgar/data/1/000000000126000001/{name}">{name}</a></td>'
        f'<td scope="row">{kind}</td><td scope="row">100</td></tr>'
        for i, (name, kind) in enumerate(docs, 1))
    return ('<table class="tableFile" summary="Document Format Files"><tr><th>Seq</th>'
            f'<th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>{rows}</table>')


def _names(cands):
    return [c["url"].rsplit("/", 1)[-1] for c in cands]


def _candidates(page, form="10-K", folder=None):
    """exhibit_candidates with the index page served; the folder listing must
    not be read unless ``folder`` is given."""
    get = (lambda url: {"directory": {"item": [{"name": n} for n in folder]}}) if folder is not None \
        else (lambda url: pytest.fail(f"folder listing read: {url}"))
    text = page if callable(page) else (lambda url: page)
    with patch.object(ex, "_get_text", side_effect=text), patch.object(ex, "_get", side_effect=get):
        return ex.exhibit_candidates("1", form, "0000000001-26-000001", "2026-02-04", "2025-12-27")


class TestFilingDocuments:
    def test_every_document_of_amds_10k_with_its_declared_type(self):
        with patch.object(ex, "_get_text", return_value=AMD_INDEX) as get:
            docs = ex.filing_documents("2488", "0000002488-26-000018")
        assert get.call_args.args[0] == ("https://www.sec.gov/Archives/edgar/data/2488/"
                                         "000000248826000018/0000002488-26-000018-index.htm")
        # the main document links through the iXBRL viewer ("/ix?doc=/Archives/…")
        assert docs[0] == ("amd-20251227.htm", "10-K")
        assert ("exh_21-10kfy25.htm", "EX-21") in docs
        assert ("amd-20251227.xsd", "EX-101.SCH") in docs        # the data files table too

    def test_an_unreadable_page_is_none(self):
        with patch.object(ex, "_get_text", side_effect=RuntimeError("404 Not Found")):
            assert ex.filing_documents("1", "0000000001-26-000001") is None
        with patch.object(ex, "_get_text", return_value="<html><p>Access denied</p></html>"):
            assert ex.filing_documents("1", "0000000001-26-000001") is None


class TestDeclaredCandidates:
    def test_amds_list_no_filename_pattern_finds(self):
        # exh_21-…: "exh" defeats `ex+[-._]?21`, and "exhibit" is not spelt out
        assert not any(p.search("exh_21-10kfy25.htm") for p in ex._EX21_NAMES)
        got = _candidates(AMD_INDEX)
        assert _names(got) == ["exh_21-10kfy25.htm"]
        assert got[0]["declared"] == "EX-21"
        assert got[0]["url"] == ("https://www.sec.gov/Archives/edgar/data/1/000000000126000001/"
                                 "exh_21-10kfy25.htm")
        assert (got[0]["form"], got[0]["filing_date"], got[0]["period"]) == ("10-K", "2026-02-04", "2025-12-27")

    def test_workivas_exh21_name(self):
        assert _names(_candidates(LENNAR_INDEX)) == ["len-20251130x10kxexh21.htm"]

    def test_unilevers_ex81_not_its_ex17_guarantor_list(self):
        # by name the folder listing offered only a171-subsidiaryguarantor.htm
        # (an EX-17.1) and missed a081-groupcompaniesappro.htm (the EX-8.1)
        assert _names(_candidates(UNILEVER_INDEX, "20-F")) == ["a081-groupcompaniesappro.htm"]

    def test_a_filing_agents_numbered_names(self):
        page = _page(("ex_864720.htm", "EX-21.1"), ("ex_864721.htm", "EX-23.1"), ("aemetis-10k.htm", "10-K"))
        assert _names(_candidates(page)) == ["ex_864720.htm"]

    def test_one_list_per_registrant_in_the_filers_order(self):
        # Exelon's combined 10-K: eight lists, EX-21.1 … EX-21.8 (item 78 picks the own one)
        page = _page(*[(f"exc-20251231x10kxexh21{i}.htm", f"EX-21.{i}") for i in range(1, 9)])
        assert _names(_candidates(page)) == [f"exc-20251231x10kxexh21{i}.htm" for i in range(1, 9)]

    def test_a_name_declared_something_else_is_no_candidate(self):
        # AB InBev's dex215.htm is exhibit 2.15; a "subsidiaries" EX-99 is no list
        page = _page(("d1dex215.htm", "EX-2.15"), ("subsidiaries.htm", "EX-99.1"), ("a10-k.htm", "10-K"))
        assert _candidates(page) == []

    def test_a_20f_tries_its_ex8_before_an_ex21(self):
        page = _page(("x-ex21.htm", "EX-21.1"), ("x-ex8.htm", "EX-8.1"))
        assert _names(_candidates(page, "20-F")) == ["x-ex8.htm", "x-ex21.htm"]

    @pytest.mark.parametrize("form", ["10-K", "S-1", "F-1", "10-12B"])
    def test_ex8_outside_a_20f_is_counsels_opinion(self, form):
        page = _page(("x-ex8.htm", "EX-8.1"), ("x-ex82.htm", "EX-8.2"), ("x-ex211.htm", "EX-21.1"))
        assert _names(_candidates(page, form)) == ["x-ex211.htm"]

    def test_only_html_documents(self):
        # Axa Financial's 2011 list is a .txt; a scanned PDF is no table either
        page = _page(("e13800_ex21.txt", "EX-21"), ("ex21.pdf", "EX-21"))
        assert _candidates(page) == []


class TestTheFolderListingFallback:
    def test_names_decide_when_the_index_page_cannot_be_read(self):
        def gone(url):
            raise RuntimeError("request lost")
        got = _candidates(gone, folder=["a10-k.htm", "ex21.htm", "ex_864720.htm"])
        assert _names(got) == ["ex21.htm"]
        assert "declared" not in got[0]

    def test_names_decide_when_the_page_shows_no_documents(self):
        got = _candidates("<html><body>maintenance</body></html>", folder=["a10-k.htm", "ex21.htm"])
        assert _names(got) == ["ex21.htm"]


class TestTheRunnerReadsIt:
    def test_amds_91_subsidiaries_reach_fetch_subsidiaries(self):
        subs = {"filings": {"recent": {"form": ["10-K"], "accessionNumber": ["0000002488-26-000018"],
                                       "filingDate": ["2026-02-04"], "reportDate": ["2025-12-27"]}}}

        def text(url):
            if url.endswith("-index.htm"):
                return AMD_INDEX
            if url.endswith("/exh_21-10kfy25.htm"):
                return AMD_EX21
            raise AssertionError(f"unexpected request {url}")

        with patch.object(ex, "_get", return_value=subs), patch.object(ex, "_get_text", side_effect=text), \
                patch.object(ex, "co_registrants", return_value=[]):
            got = ex.fetch_subsidiaries("2488", "ADVANCED MICRO DEVICES INC")
        assert got["form"] == "10-K" and got["url"].endswith("/exh_21-10kfy25.htm")
        assert len(got["subsidiaries"]) == 91
        assert "Xilinx Development Corporation" in {s["name"] for s in got["subsidiaries"]}
