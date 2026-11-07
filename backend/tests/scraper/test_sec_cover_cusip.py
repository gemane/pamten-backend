"""The issuer's CUSIP from a Schedule 13D/G cover (findings item 71, 2026-11-07).

The securities bridge reads a node's CUSIP as its US ISIN and asks GLEIF's
LEI-ISIN file for the issuer's LEI. Before this, only the issuer's own scrape kept
the cover's CUSIP, only from an XML schedule, and never from a 13D (its XML spells
the field `issuerCUSIP`): 75 dev nodes carried one. Now every cover read keeps it —
XML in all three spellings, older text/HTML covers too, the issuer side and the
filer side — and only a CUSIP whose check digit is right counts.

The text snippets are the shapes of real covers (BlackRock's 2024 .txt filings,
Berkshire Hathaway's .htm ones), with the CUSIPs they state.
"""
import pathlib
from unittest.mock import patch

import pytest

from app.scraper import sec_edgar
from app.scraper.sec_edgar import (
    _cover_cusip, _parse_13dg_xml, _parse_cusip_from_text, _parse_holding_filing,
    _parse_holding_filing_text, fetch_filer_holdings,
)
from tests.scraper.test_sec_edgar import _serve
from tests.scraper.test_sec_holdings import XML, _doc_for, _subs

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class TestCoverCusip:
    @pytest.mark.parametrize("raw, want", [
        ("084670702", "084670702"),          # Berkshire Hathaway class B
        ("82968b103", "82968B103"),          # Sirius XM, lower case
        ("82968B 10 3", "82968B103"),        # spaced the way old covers print it
        ("82968B-10-3", "82968B103"),
        (" 00507V109 ", "00507V109"),
        ("46435G509", None),                 # one digit off: the check digit refuses it
        ("46435G50", None),                  # eight characters
        ("0846707021", None),                # ten
        ("See Item 3", None),
        ("", None),
        (None, None),
    ])
    def test_only_a_cusip_with_a_right_check_digit(self, raw, want):
        assert _cover_cusip(raw) == want


class TestCusipFromTextCover:
    @pytest.mark.parametrize("text, want", [
        # BlackRock's .txt covers (2024): the rule above "(CUSIP Number)", the
        # number on each reporting person's page, and an Item 3 that is no number
        ("iShares Gold Trust (Name of Issuer)\nShares (Title of Class of Securities)\n"
         "---------------------------------------\n(CUSIP Number)\n"
         "CUSIP No. 46435G508\n(1)Names of reporting persons. BlackRock, Inc.\n"
         "CUSIP No.: See Cover Page Item 3.", "46435G508"),
        # Berkshire's .htm covers: the number right before "(CUSIP Number)"
        ("Common Stock (Title of Class of Securities) 00507V109 (CUSIP Number) "
         "CUSIP No. 00507V109 13G Page 2 of 15 Pages", "00507V109"),
        ("CUSIP Number: 084670702 Date of Event Which Requires Filing", "084670702"),
        ("CUSIP No.: 084670702 1. NAME OF REPORTING PERSON", "084670702"),
        # the capture runs on into the next field; the first nine count
        ("CUSIP No. 084670702 1. Names of Reporting Persons", "084670702"),
        # spaced
        ("Common Stock 82968B 10 3 (CUSIP Number)", "82968B103"),
        # an invalid one first (a typo), the valid one later in the document
        ("CUSIP No. 46435G509 ... CUSIP No. 46435G508", "46435G508"),
        ("CUSIP No.: See Cover Page Item 3.", None),
        ("no number at all", None),
    ])
    def test_the_first_valid_cusip_the_cover_states(self, text, want):
        assert _parse_cusip_from_text(text) == want


class TestCusipFromXmlSchedule:
    def test_a_13d_spells_it_issuerCUSIP(self):
        # Berkshire Hathaway's own 13D/A of 2025 (0001193125-25-277436) states
        # <issuerCUSIP>084670108</issuerCUSIP>; it was never read
        xml = (FIXTURES / "13d_tactical.xml").read_text().replace(
            "<issuerCusips>\n          <issuerCusipNumber>269710109</issuerCusipNumber>\n        </issuerCusips>",
            "<issuerCUSIP>084670108</issuerCUSIP>")
        assert "<issuerCUSIP>" in xml
        assert _parse_13dg_xml(xml)["issuer_cusip"] == "084670108"

    def test_the_list_form_is_read(self):
        assert _parse_13dg_xml((FIXTURES / "13d_tactical.xml").read_text())["issuer_cusip"] == "269710109"

    def test_a_13g_spells_it_issuerCusip(self):
        assert _parse_13dg_xml((FIXTURES / "13ga_softbank_alibaba.xml").read_text())["issuer_cusip"] == "01609W102"

    def test_an_invalid_value_is_skipped_for_the_next_valid_one(self):
        xml = (FIXTURES / "13d_tactical.xml").read_text().replace(
            "<issuerCusipNumber>269710109</issuerCusipNumber>",
            "<issuerCusipNumber>269710100</issuerCusipNumber><issuerCusipNumber>269710109</issuerCusipNumber>")
        assert _parse_13dg_xml(xml)["issuer_cusip"] == "269710109"

    def test_no_valid_value_is_none(self):
        xml = (FIXTURES / "13d_tactical.xml").read_text().replace(
            "<issuerCusipNumber>269710109</issuerCusipNumber>", "<issuerCusipNumber>N/A</issuerCusipNumber>")
        assert _parse_13dg_xml(xml)["issuer_cusip"] is None


def _xml_with_cusip(cusip: str) -> str:
    return XML.format(cik="0000908937", name="Sirius XM Holdings Inc.", pct="7.1").replace(
        "</issuerCik>", f"</issuerCik><issuerCusip>{cusip}</issuerCusip>")


class TestTheFilerSide:
    def test_an_xml_holding_carries_its_subjects_cusip(self):
        with patch("app.scraper.sec_edgar._get_text", return_value=_xml_with_cusip("82968B103")):
            row = _parse_holding_filing("0002100119", "a-1")
        assert row["subject_cusip"] == "82968B103"

    def test_an_old_cover_holding_carries_its_subjects_cusip(self):
        index = ('<span class="companyName">Sirius XM Holdings Inc. (Subject)'
                 '</span> <a href="x">CIK=0000908937</a>'
                 '<table><tr><td><a href="/Archives/edgar/data/1364742/a.txt">doc</a>'
                 '</td><td>SC 13G</td></tr></table>')
        cover = ("SCHEDULE 13G Sirius XM Holdings Inc. (Name of Issuer) Common Stock "
                 "(Title of Class of Securities) 82968B103 (CUSIP Number) "
                 "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 9: 7.1% ")
        pages = {sec_edgar._filing_index_url("0001364742", "0001086364-24-006303"): index,
                 "https://www.sec.gov/Archives/edgar/data/1364742/a.txt": cover}
        with patch.object(sec_edgar, "_get_text", side_effect=lambda url, params=None: pages[url]):
            row = _parse_holding_filing_text("0001364742", "0001086364-24-006303", "BlackRock, Inc.")
        assert row["subject_cik"] == "0000908937"
        assert row["subject_cusip"] == "82968B103"

    def test_the_holdings_list_passes_it_on(self):
        docs = {"a-1": _xml_with_cusip("82968B103")}
        with patch("app.scraper.sec_edgar._submissions",
                   return_value=_subs([("SCHEDULE 13G", "a-1", "2026-02-01")])), \
             patch("app.scraper.sec_edgar._get_text", side_effect=_doc_for(docs)):
            holdings = fetch_filer_holdings("0002100119")
        assert [h["subject_cusip"] for h in holdings] == ["82968B103"]


class TestTheIssuerSide:
    def test_an_old_cover_gives_the_issuer_its_cusip(self):
        # before 2026-11-07 only an XML schedule did
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SC 13G"/><content type="text/xml">
            <filing-href>https://x.test/old-index.htm</filing-href>
            <filing-date>2021-02-01</filing-date>
            <accession-number>0001104659-21-000001</accession-number>
          </content></entry>
        </feed>"""
        old_index = ('<span class="companyName">Vanguard Group Inc (Filed by)'
                     '</span> <a href="x">CIK=0000102909</a>'
                     '<table><tr><td><a href="/Archives/edgar/data/1/old.htm">doc</a>'
                     '</td><td>SC 13G</td></tr></table>')
        old_doc = ("SCHEDULE 13G Sirius XM Holdings Inc. (Name of Issuer) Common Stock "
                   "(Title of Class of Securities) 82968B103 (CUSIP Number) "
                   "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11: 7.0% ")
        pages = {None: atom, "https://x.test/old-index.htm": old_index,
                 "https://www.sec.gov/Archives/edgar/data/1/old.htm": old_doc}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            results = sec_edgar.fetch_ownership_filings("Sirius XM Holdings Inc.", "908937")
        assert [r["issuer_cusip"] for r in results] == ["82968B103"]

    def test_an_xml_schedule_gives_the_issuer_its_cusip(self):
        # Vanguard's real 13G on Apple (0002100119-26-000139) lists 037833100
        from tests.scraper.test_sec_edgar import TestStructuredScrapeEndToEnd as E
        pages = {E.XML_URL: (FIXTURES / "13g_vanguard.xml").read_text()}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(E.ATOM, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            results = sec_edgar.fetch_ownership_filings("Apple Inc", "0000320193")
        assert [r["issuer_cusip"] for r in results] == ["037833100"]
