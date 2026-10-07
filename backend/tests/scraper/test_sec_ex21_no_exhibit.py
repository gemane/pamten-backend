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
                                  note_in_main_document, parse_exhibit)

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

    def test_the_note_the_exhibit_index_points_to(self):
        index = _index("d65314d20f.htm", "d65314dex121.htm", "d65314dex215.htm",
                       "0001193125-26-088105-index.html")
        with patch("app.scraper.sec_ex21._get", return_value=index), \
             patch("app.scraper.sec_ex21._get_text", return_value=_abinbev_main_document()):
            got = note_in_main_document("1668717", self.FILING, ABI)
        assert len(got["subsidiaries"]) == 65 and got["form"] == "20-F"
        assert got["url"].endswith("/d65314d20f.htm#note-34")
        assert got["filing_date"] == "2026-03-03"

    def test_nothing_when_the_index_does_not_say_so(self):
        with patch("app.scraper.sec_ex21._get", return_value=_index("x20f.htm")), \
             patch("app.scraper.sec_ex21._get_text", return_value=(FX / "abinbev_20f_note34.htm").read_text()):
            assert note_in_main_document("1", self.FILING, ABI) is None

    def test_a_10k_is_never_searched(self):
        with patch("app.scraper.sec_ex21._get", side_effect=AssertionError("fetched")):
            assert note_in_main_document("1", ("10-K", *self.FILING[1:]), ABI) is None

    def test_fetch_falls_back_to_it_when_no_exhibit_parses(self):
        with patch("app.scraper.sec_ex21.annual_filings", return_value=[self.FILING]), \
             patch("app.scraper.sec_ex21.exhibit_candidates",
                   return_value=[{"url": "u/d65314dex215.htm", "form": "20-F", "filing_date": "2026-03-03"}]), \
             patch("app.scraper.sec_ex21._get_text", return_value="<table><tr><td>Notes</td></tr></table>"), \
             patch("app.scraper.sec_ex21.note_in_main_document", return_value={"subsidiaries": [1]}) as note:
            assert fetch_subsidiaries("1668717", ABI) == {"subsidiaries": [1]}
        note.assert_called_once_with("1668717", self.FILING, ABI)

    def test_no_annual_filing_is_none_without_fetching_anything_else(self):
        with patch("app.scraper.sec_ex21.annual_filings", return_value=[]), \
             patch("app.scraper.sec_ex21.exhibit_candidates", side_effect=AssertionError("fetched")):
            assert fetch_subsidiaries("1065521", "SoftBank Group Corp.") is None


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
