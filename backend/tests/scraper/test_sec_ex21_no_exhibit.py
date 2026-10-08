"""Exhibit 21/8.1 for the filers that came back "no exhibit" on 2026-10-07
although they file a subsidiary list — fixtures captured from those filings:
- MercadoLibre's Workiva name "xexx2101" and Embraer's bare "xex8";
- Alibaba's Exhibit 8.1: one "Name (PRC)" paragraph per subsidiary, no table;
- AB InBev: no 8.1 file — "8.1 List of significant subsidiaries (included in
  note 34 …)", a list grouped under country rows, associates after it;
- SoftBank, Vanguard, FMR: no 10-K or 20-F at all.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from app.scraper import sec_ex21 as ex
from app.scraper.sec_ex21 import (exhibit_candidates, fetch_subsidiaries, jurisdiction_country,
                                  parse_exhibit)

FX = Path(__file__).parent / "fixtures"
ABI = "Anheuser-Busch InBev SA/NV"


def _index(*names):
    return {"directory": {"item": [{"name": n, "size": 100} for n in names]}}


class TestExhibitFilenames:
    @pytest.mark.parametrize("form,names,want", [
        ("10-K", ["meli-20251231.htm", "meli-20251231xexx2101.htm", "meli-20251231xexx3101.htm"],
         ["meli-20251231xexx2101.htm"]),
        ("20-F", ["erj-20251231.htm", "embj-20251231xex8.htm", "embj-20251231xexx121.htm"],
         ["embj-20251231xex8.htm"]),
        ("20-F", ["a-ex85.htm", "a-ex80.htm", "a-ex2_42.htm"], []),     # not 8.1
        # 38 of the 2026 20-Fs: Allot, Compugen, Caesarstone, Canada Goose …
        ("20-F", ["exhibit_12-1.htm", "exhibit_8-1.htm"], ["exhibit_8-1.htm"]),
        ("20-F", ["exhibit81fy2026.htm", "exhibit121fye26.htm"], ["exhibit81fy2026.htm"]),
    ])
    def test_the_names_filing_agents_write(self, form, names, want):
        with patch("app.scraper.sec_ex21._get", return_value=_index(*names)):
            got = exhibit_candidates("1", form, "0000000001-26-000001", "2026-02-25")
        assert [c["url"].rsplit("/", 1)[-1] for c in got] == want

    def test_the_parser_reads_both_once_found(self):
        # the filename was the whole problem for these two
        assert len(parse_exhibit((FX / "mercadolibre_ex2101.htm").read_text(), "MercadoLibre, Inc.")) == 70
        assert len(parse_exhibit((FX / "embraer_ex8.htm").read_text(), "Embraer S.A.")) == 37


class TestOnePerParagraph:
    def test_alibabas_list_without_a_table(self):
        subs = parse_exhibit((FX / "alibaba_ex81_excerpt.htm").read_text(), "Alibaba Group Holding Limited")
        assert len(subs) == 27
        assert all(s["jurisdiction"] == "PRC" and jurisdiction_country(s["jurisdiction"]) == "CN"
                   for s in subs)
        # the LAST bracket is the place; a bracket inside the name stays
        assert "Alibaba Information Port (Wulanchabu) Co., Ltd." in {s["name"] for s in subs}

    def test_prose_with_brackets_is_not_a_list(self):
        html = ("<p>The Company (as defined below) is described here.</p>"
                "<p>See the annual report (page 12).</p><p>Contact us (investor relations).</p>"
                "<p>Acme Ltd. (Delaware)</p>")
        assert parse_exhibit(html) == []           # three shaped lines, one place


class TestGroupedUnderCountries:
    @pytest.fixture(scope="class")
    def subs(self):
        return parse_exhibit((FX / "abinbev_20f_note34.htm").read_text(), ABI)

    def test_the_fully_consolidated_companies_with_their_stakes(self, subs):
        assert len(subs) == 65
        by = {s["name"]: s for s in subs}
        assert by["Cerveceria y Malteria Quilmes Saica Y G"] == {
            "name": "Cerveceria y Malteria Quilmes Saica Y G", "jurisdiction": "Argentina",
            "stake_percent": 61.63}                   # the registered office cut off
        assert by["Zambian Breweries PLC"]["jurisdiction"] == "Zambia"
        assert all(jurisdiction_country(s["jurisdiction"]) for s in subs)

    def test_neither_the_parent_nor_the_associates(self, subs):
        names = {s["name"] for s in subs}
        assert not any("Anheuser-Busch InBev NV/SA" in n for n in names)        # "Consolidating"
        for associate in ("Anadolu Efes Biracilik Ve Malt Sanayii A.S.", "AB InBev Efes B.V.",
                          "Delta Corporation Limited"):
            assert associate not in names

    def test_an_exhibit_the_table_reader_handles_is_read_as_before(self):
        # the fallbacks run only when the table reader found nothing
        apple = (FX / "apple_ex21.htm").read_text()
        with patch.object(ex, "_grouped_list", side_effect=AssertionError("ran")), \
             patch.object(ex, "_paragraph_list", side_effect=AssertionError("ran")):
            assert parse_exhibit(apple, "Apple Inc.")


def _abinbev_main_document():
    # a contents page lists note 34 and 35 first; the real heading comes later
    toc = '<div>>34. AB InBev companies</div><div>>35. Events after the balance sheet date</div>'
    return ("<html><body>" + toc + (FX / "abinbev_20f_exhibit_index_row.htm").read_text()
            + (FX / "abinbev_20f_note34.htm").read_text())


def test_a_headerless_table_after_an_associates_heading_is_not_taken():
    # a page break can leave the associates' rows without their header row
    rows = "".join(f"<tr><td>{n} - Some Street 1 - Paris</td><td>{p}%</td></tr>"
                   for n, p in (("Alpha SAS", 100), ("Beta SA", 90), ("Gamma SARL", 80)))
    html = ("<table><tr><td>Name and registered office of the fully consolidated companies</td>"
            "<td>% economic interest</td></tr><tr><td>France</td></tr>" + rows + "</table>"
            "<p>LIST OF THE MOST IMPORTANT ASSOCIATES</p>"
            "<table><tr><td>France</td></tr><tr><td>Delta SA - Rue 2 - Lyon</td><td>25%</td></tr></table>")
    assert {s["name"] for s in parse_exhibit(html, ABI)} == {"Alpha SAS", "Beta SA", "Gamma SARL"}


class TestListInTheMainDocument:
    FILING = ("20-F", "0001193125-26-088105", "2026-03-03", "2025-12-31")
    URL = "https://www.sec.gov/Archives/edgar/data/1668717/000119312526088105/d65314d20f.htm"

    def test_the_note_the_exhibit_index_points_to(self):
        with patch("app.scraper.sec_ex21._main_document", return_value=(self.URL, _abinbev_main_document())):
            got = ex.list_from_main_document("1668717", self.FILING, ABI)
        assert len(got["subsidiaries"]) == 65 and got["form"] == "20-F"
        assert got["url"] == self.URL + "#note-34" and got["filing_date"] == "2026-03-03"

    def test_nothing_when_the_index_does_not_say_so(self):
        note_only = "<html><body>" + (FX / "abinbev_20f_note34.htm").read_text()
        with patch("app.scraper.sec_ex21._main_document", return_value=(self.URL, note_only)):
            assert ex.list_from_main_document("1", self.FILING, ABI) is None

    def test_a_10k_is_never_searched(self):
        with patch("app.scraper.sec_ex21._main_document", side_effect=AssertionError("fetched")):
            assert ex.list_from_main_document("1", ("10-K", *self.FILING[1:]), ABI) is None

    def test_a_reference_to_an_earlier_filing_goes_there(self):
        doc = ("<p>8.1 List of Subsidiaries (incorporated herein by reference to the Annual Report on "
               "Form 20-F filed with the SEC on March 24, 2022) 11.1 Insider Trading Policy</p>")
        with patch("app.scraper.sec_ex21._main_document", return_value=(self.URL, doc)), \
             patch("app.scraper.sec_ex21.list_from_earlier_filing", return_value={"x": 1}) as earlier:
            assert ex.list_from_main_document("1", self.FILING, ABI) == {"x": 1}
        assert earlier.call_args.kwargs == {"confirmed_by": self.URL, "confirmed_on": "2026-03-03"}

    def test_fetch_falls_back_to_it_when_no_exhibit_parses(self):
        with patch("app.scraper.sec_ex21.annual_filings", return_value=[self.FILING]), \
             patch("app.scraper.sec_ex21.exhibit_candidates",
                   return_value=[{"url": "u/d65314dex215.htm", "form": "20-F", "filing_date": "2026-03-03"}]), \
             patch("app.scraper.sec_ex21._get_text", return_value="<table><tr><td>Notes</td></tr></table>"), \
             patch("app.scraper.sec_ex21.list_from_main_document", return_value={"subsidiaries": [1]}) as main:
            assert fetch_subsidiaries("1668717", ABI) == {"subsidiaries": [1]}
        main.assert_called_once_with("1668717", self.FILING, ABI)

    def test_no_annual_filing_is_none_without_fetching_anything_else(self):
        with patch("app.scraper.sec_ex21.annual_filings", return_value=[]), \
             patch("app.scraper.sec_ex21.exhibit_candidates", side_effect=AssertionError("fetched")):
            assert fetch_subsidiaries("1065521", "SoftBank Group Corp.") is None


# Real 8.1 entries from 2026 20-Fs (the measurement of 2026-10-07).
class TestTheEntry:
    @pytest.mark.parametrize("text,want", [
        # a reference inside the entry is not the next entry
        ("8.1 Subsidiaries of the registrant (incorporated by reference to Exhibit 21.1 to our registration "
         "statement on Form F-1 (File No. 333-286211) filed with the SEC on March 28, 2025) 11.1 Code of Ethics",
         "Exhibit 21.1"),
        # a table row: "8.1 March 9, 2023" is a column, not the next entry
        ("8.1 List of Subsidiaries. 20-F 001-41316 8.1 March 9, 2023 11.1 Insider Trading Compliance Policy",
         "March 9, 2023"),
    ])
    def test_it_runs_to_the_next_entry(self, text, want):
        entry = ex.exhibit_entry(text)
        assert want in entry and "Insider" not in entry and "Code of Ethics" not in entry

    def test_an_entry_about_something_else_is_skipped(self):
        assert ex.exhibit_entry("8.1 % per annum. The loan was fully repaid.") is None

    @pytest.mark.parametrize("entry,note", [
        ("List of significant subsidiaries (included in note 34 to our audited consolidated financial "
         "statements included in this Form 20-F).", 34),                                           # AB InBev
        ("List of subsidiaries of BW LPG Limited is set forth in Note 26 to the audited consolidated "
         "financial statements for the year ended on 31 December 2025.", 26),                      # BW LPG
        ("List of Significant Subsidiaries ( see Note 2 to the Consolidated Financial Statements)", 2),  # Ferroglobe
        ("List of Subsidiaries (incorporated by reference to Note 3 to our Audited Consolidated Financial "
         "Statements filed with this Annual Report on Form 20-F).", 3),                     # Santander Brasil
        ("Subsidiaries (incorporated by reference to Exhibit 8.1 of our Annual Report on Form 20-F filed "
         "with the Securities and Exchange Commission on April 2, 2024)", None),            # an earlier filing
        ("List of subsidiaries of Brookfield Renewable Corporation (incorporated by reference to Item 4.C)", None),
        # the note of ANOTHER filing is not one of this filing's notes
        ("Subsidiaries (filed as Exhibit 8.1 to our Form 20-F on March 3, 2022, see Note 2 thereto)", None),
    ])
    def test_the_note_it_points_to(self, entry, note):
        assert ex._note_of_entry(entry) == note


def test_a_note_list_must_be_places_nearly_throughout():
    # Novartis' note 31 read the CITY column as jurisdiction: 70 % mapped, wrong
    good = [{"name": f"Co {i}", "jurisdiction": "Germany"} for i in range(10)]
    cities = [{"name": f"Co {i}", "jurisdiction": "London 5" if i < 3 else "Germany"} for i in range(10)]
    assert ex._mostly_places(good) and not ex._mostly_places(cities) and not ex._mostly_places([])


class TestTheReference:
    @pytest.mark.parametrize("entry,forms,dates,exhibit", [
        ("List of Subsidiaries of Can-Fite BioPharma Ltd. (incorporated herein by reference to the Annual "
         "Report on Form 20-F filed with the SEC on March 24, 2022)", ["20-F"], ["2022-03-24"], None),
        ("List of Significant Subsidiaries of the Registrant (incorporated herein by reference to Exhibit 21.1 "
         "to the Form F-1 filed on June 6, 2025 (File No. 333-286214))", ["F-1"], ["2025-06-06"], "21.1"),
        ("List of Subsidiaries. 20-F 001-41316 8.1 March 9, 2023", ["20-F"], ["2023-03-09"], "8.1"),
        ("List of subsidiaries of the registrant 20-F 001-39374 8.1 03/15/21", ["20-F"], ["2021-03-15"], "8.1"),
    ])
    def test_what_an_entry_says(self, entry, forms, dates, exhibit):
        ref = ex.earlier_filing_reference(entry)
        assert ref["forms"] == forms and ref["exhibit"] == exhibit
        assert [d.isoformat() for d in ref["dates"]][:1] == dates

    def test_a_numeric_date_is_tried_both_ways(self):
        # Alvotech "01.03.2023": January 3 in the US, March 1 in Europe
        assert {d.isoformat() for d in ex._dates("01.03.2023")} == {"2023-01-03", "2023-03-01"}

    def test_a_footnote_mark_is_read_through(self):
        text = ("8.1 List of Subsidiaries (21) 10.3 Amendment … (21) Incorporated by reference to Exhibit "
                "21.1 to the Registration Statement on Form F-1 filed with the SEC on March 3, 2021.")
        ref = ex.earlier_filing_reference("List of Subsidiaries (21)", text)
        assert ref["forms"] == ["F-1"] and ref["exhibit"] == "21.1"
        assert ref["dates"][0].isoformat() == "2021-03-03"

    def test_an_accession_number_and_a_file_number(self):
        ref = ex.earlier_filing_reference("List of Subsidiaries and Associate. 20-F 0001641172-25-006627")
        assert ref["accession"] == "0001641172-25-006627"
        ref = ex.earlier_filing_reference("(incorporated herein by reference to Exhibit 21.1 to the Company's "
                                          "Form F-1 (File No. 333-286471))")
        assert ref["file_no"] == "333-286471" and ref["dates"] == []


FILINGS = [  # (form, accession, filed, report date, file number)
    ("20-F", "0000000001-26-000001", "2026-03-20", "2025-12-31", "001-40408"),
    ("20-F/A", "0000000001-22-000009", "2022-03-24", "2021-12-31", "001-40408"),
    ("20-F", "0000000001-22-000008", "2022-03-24", "2021-12-31", "001-40408"),
    ("20-F", "0000000001-21-000005", "2021-03-30", "2020-12-31", "001-40408"),
    ("F-1/A", "0000000001-25-000003", "2025-06-06", "", "333-286471"),
    ("F-1", "0000000001-25-000002", "2025-05-02", "", "333-286471"),
]


class TestResolving:
    def _resolve(self, entry):
        with patch("app.scraper.sec_ex21._all_filings", return_value=FILINGS):
            return [a for _f, a, _d in ex.resolve_reference("1", ex.earlier_filing_reference(entry))]

    def test_by_form_and_date_the_original_before_its_amendment(self):
        assert self._resolve("Annual Report on Form 20-F filed on March 24, 2022") == \
            ["0000000001-22-000008", "0000000001-22-000009"]

    def test_a_day_either_side(self):
        assert self._resolve("Form 20-F filed on March 25, 2022")[0] == "0000000001-22-000008"

    def test_the_year_ended_is_the_report_date(self):
        # Ceragon: "Annual Report on Form 20-F for the year ended December 31, 2020"
        assert self._resolve("Form 20-F for the year ended December 31, 2020") == ["0000000001-21-000005"]

    def test_by_file_number_when_no_date_the_original_first(self):
        got = self._resolve("Exhibit 21.1 to the Company's Form F-1 (File No. 333-286471)")
        assert got == ["0000000001-25-000002", "0000000001-25-000003"]   # the original F-1 first

    def test_by_accession(self):
        assert self._resolve("20-F 0000000001-21-000005") == ["0000000001-21-000005"]

    def test_the_wrong_form_never_matches(self):
        assert self._resolve("Form F-1 filed on March 24, 2022") == []


class TestTheEarlierList:
    def test_dated_by_the_earlier_filing_confirmed_by_this_one(self):
        with patch("app.scraper.sec_ex21.resolve_reference",
                   return_value=[("F-1", "0000000001-25-000002", "2025-05-02")]), \
             patch("app.scraper.sec_ex21.exhibit_candidates",
                   return_value=[{"url": "https://www.sec.gov/x/ex21-1.htm"}]) as cands, \
             patch("app.scraper.sec_ex21._get_text",       # an F-1's list is declared EX-21.1
                   return_value=(FX / "embraer_ex8.htm").read_text().replace("<TYPE>EX-8.1", "<TYPE>EX-21.1", 1)):
            got = ex.list_from_earlier_filing("1", "Exhibit 21.1 to our Form F-1 filed on May 2, 2025", "",
                                              "Embraer S.A.", confirmed_by="https://www.sec.gov/y/20f.htm",
                                              confirmed_on="2026-03-30")
        assert len(got["subsidiaries"]) == 37 and got["filing_date"] == "2025-05-02"
        assert got["exhibit"] == "21" and got["url"].endswith("ex21-1.htm")
        assert (got["confirmed_by"], got["confirmed_on"]) == ("https://www.sec.gov/y/20f.htm", "2026-03-30")
        assert cands.call_args.args[1] == "10-K"          # the ex-21 patterns first

    def test_nothing_when_the_filing_cannot_be_found(self):
        with patch("app.scraper.sec_ex21.resolve_reference", return_value=[]):
            assert ex.list_from_earlier_filing("1", "Form 20-F filed on May 2, 2015", "") is None


@pytest.mark.parametrize("raw,clean", [("Acctel, S.A. de C.V. (*)", "Acctel, S.A. de C.V."),
                                       ("Delico Handels GmbH (#)", "Delico Handels GmbH"),
                                       ("Some Holding Ltd (**)", "Some Holding Ltd"),
                                       ("Acme Inc. (1)", "Acme Inc."),
                                       ("Alibaba Information Port (Wulanchabu) Co., Ltd.",
                                        "Alibaba Information Port (Wulanchabu) Co., Ltd.")])
def test_footnote_marks_are_not_part_of_the_name(raw, clean):
    # Televisa and Magnum mark entries with bracketed symbols
    assert ex._clean_name(raw)[0] == clean


def test_bvi_is_the_british_virgin_islands():
    assert jurisdiction_country("BVI") == "VG"                   # ZTO Express


def test_unilevers_companies_act_list_is_not_read():
    # its 8.1 is the full s.409 list — subsidiaries, associates and joint
    # ventures in flowing columns; reading nothing beats taking associates
    assert parse_exhibit((FX / "unilever_a081.htm").read_text(), "Unilever PLC") == []


def test_rows_before_any_table_do_not_crash_the_parser():
    # a note cut from a 20-F can start inside a table: </tr> with no <table>
    html = ("<tr><td>Acme GmbH</td><td>Germany</td></tr><tr><td>Beta SA</td><td>France</td></tr>"
            "<tr><td>Gamma BV</td><td>Netherlands</td></tr></table>")
    assert {s["name"] for s in parse_exhibit(html)} == {"Acme GmbH", "Beta SA", "Gamma BV"}


@pytest.mark.parametrize("place,code", [
    ("The Republic of The Marshall Islands", "MH"),     # Scorpio Tankers
    ("São Paulo – Brazil", "BR"),                       # Bradesco: city – country
    ("Luxembourg – G. Ducado", "LU"),                   # the first part when the last is no place
    ("Panamá", "PA"),                                   # an accent
    ("Republic of China", None),                        # Taiwan — never stripped to China
    ("November 9, 2000", None),
])
def test_places_as_filers_write_them(place, code):
    assert jurisdiction_country(place) == code


def test_a_date_of_incorporation_column_is_not_the_jurisdiction():
    # Rezolve: Name | Date of Incorporation | Place of Incorporation | % —
    # "incorporat" matched the date column, and 50 dates became places
    subs = parse_exhibit((FX / "rezolve_ex81.htm").read_text(), "Rezolve AI PLC")
    assert len(subs) == 50
    assert all(jurisdiction_country(s["jurisdiction"]) for s in subs)
    assert {"name": "Rezolve Taiwan Inc.", "jurisdiction": "Taiwan"}.items() <= \
        next(s for s in subs if s["name"] == "Rezolve Taiwan Inc.").items()
