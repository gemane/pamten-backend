"""Exhibit 8.1 / 21 layouts the table reader could not see — measured on the
2026 20-Fs whose subsidiary file read nothing (86 filers, 61 real lists),
fixtures captured from those filings:
- files the filer declared something else ("<TYPE>EX-2.1", the description of
  securities Workiva names "exhibit21descriptionofsecu.htm");
- header cells spanning several data cells (TORM, Ellomay) and a header
  printed over three rows (UTStarcom);
- one subsidiary per line, the place in words (FinVolution, Melco, Ambev) or
  not at all (Karooooo, Recon, Banco de Chile);
- tables naming no place column (Banco Santander Chile, Enel Chile);
- names under one-cell country rows (BAT);
- the text layer behind scanned pages (Amer Sports, Borr, Polestar);
- and places the mapping got wrong ("Mauritius" was the United States).
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from app.scraper import sec_ex21 as ex
from app.scraper.sec_ex21 import _clean_name, fetch_subsidiaries, jurisdiction_country, parse_exhibit

FX = Path(__file__).parent / "fixtures"


def _read(name, registrant):
    return parse_exhibit((FX / name).read_text(), registrant)


def _by_name(subs):
    return {s["name"]: s for s in subs}


def _doc(body, kind="EX-8.1"):
    head = f"<DOCUMENT>\n<TYPE>{kind}\n<SEQUENCE>3\n<FILENAME>x.htm\n<TEXT>\n" if kind else ""
    return f"{head}<html><body>{body}</body></html>"


TABLE = ("<table><tr><td>Name of Subsidiary</td><td>Jurisdiction</td></tr>"
         "<tr><td>Alpha Ltd.</td><td>Bermuda</td></tr><tr><td>Beta GmbH</td><td>Germany</td></tr></table>")


class TestDeclaredType:
    # EX-8.2 of an F-1 is counsel's tax opinion (Samfine)
    @pytest.mark.parametrize("kind", ["EX-2.1", "EX-2.11", "EX-17", "EX-99.1", "EX-8.2"])
    def test_a_document_declared_something_else_is_no_list(self, kind):
        assert parse_exhibit(_doc(TABLE, kind)) == []

    @pytest.mark.parametrize("kind", ["EX-8.1", "EX-8", "EX-21", "EX-21.1", "EX-21.01", None])
    def test_the_subsidiary_exhibit_and_an_undeclared_page_are_read(self, kind):
        assert len(parse_exhibit(_doc(TABLE, kind))) == 2

    def test_ex8_outside_a_20f_is_a_tax_opinion(self):
        # AIR Global's and Air Water's F-1/F-4: "We have acted as special United States counsel …"
        assert len(parse_exhibit(_doc(TABLE), form="20-F")) == 2
        assert parse_exhibit(_doc(TABLE), form="F-1") == []
        assert len(parse_exhibit(_doc(TABLE, "EX-21.1"), form="F-1")) == 2

    def test_a_mislabelled_list_that_says_what_it_is(self):
        # Yatra's subsidiary list is declared EX-10.8
        assert len(parse_exhibit(_doc("<p>List of Subsidiaries</p>" + TABLE, "EX-10.8"))) == 2
        assert parse_exhibit(_doc("<p>Credit Agreement</p>" + TABLE, "EX-10.8")) == []
        # read like an undeclared page: no names-only fallback
        names = "<p>List of Subsidiaries</p><p>Alpha Holdings Limited</p><p>Beta Pte. Ltd.</p>"
        assert parse_exhibit(_doc(names, "EX-10.8")) == []

    def test_the_header_is_read_from_the_document_start(self):
        assert ex.declared_type(_doc("", "ex-8.1")) == "EX-8.1"
        assert ex.declared_type("<html>no header</html>") is None

    def test_fetch_skips_the_description_of_securities_for_the_list_after_it(self):
        pages = {"https://x/exhibit21descriptionofsecu.htm": _doc(TABLE, "EX-2.1"),
                 "https://x/ex8-1.htm": _doc(TABLE)}
        cands = [{"url": u, "form": "20-F", "filing_date": "2026-03-01"} for u in pages]
        with patch.object(ex, "annual_filings", return_value=[("20-F", "a", "2026-03-01", "")]), \
             patch.object(ex, "exhibit_candidates", return_value=cands), \
             patch.object(ex, "_get_text", side_effect=pages.get):
            got = fetch_subsidiaries("1", "Registrant plc")
        assert got["url"] == "https://x/ex8-1.htm" and len(got["subsidiaries"]) == 2


class TestColumnGrid:
    def test_a_header_cell_spanning_several_data_cells(self):
        # "Jurisdiction of Incorporation" spans grid columns 3-8, "Denmark" is the
        # third cell of its row at column 6
        subs = _read("torm_ex81.htm", "TORM plc")
        assert len(subs) == 17
        assert _by_name(subs)["TORM A/S"]["jurisdiction"] == "Denmark"

    def test_split_percent_cells_under_a_spanning_ownership_header(self):
        subs = _by_name(_read("ellomay_ex8.htm", "Ellomay Capital Ltd."))
        assert len(subs) == 57
        assert subs["Ellomay Clean Energy Ltd."] == {"name": "Ellomay Clean Energy Ltd.",
                                                     "jurisdiction": "Israel", "stake_percent": 100.0}

    def test_a_header_printed_over_three_rows_keeps_its_columns(self):
        # the third line "Name | Organization | Ownership Interest" is the rest
        # of the header, not a new section
        subs = _read("utstarcom_ex81.htm", "UTSTARCOM HOLDINGS CORP.")
        assert len(subs) == 14 and _by_name(subs)["UTStarcom, Inc."]["jurisdiction"] == "U.S.A"

    def test_the_second_header_row_that_names_the_subsidiary_column(self):
        # Grupo Supervielle: "Name under which the subsidiary does business" in
        # the first header row, "Subsidiary" in the second
        html = ("<table><tr><td></td><td>Jurisdiction of</td><td>Name under which the subsidiary does business</td></tr>"
                "<tr><td>Subsidiary</td><td>incorporation</td><td>business</td></tr>"
                "<tr><td>Banco Supervielle S.A.</td><td>Argentina</td><td>Supervielle</td></tr>"
                "<tr><td>Supervielle Seguros S.A.</td><td>Argentina</td><td>Supervielle Seguros</td></tr></table>")
        assert [s["name"] for s in parse_exhibit(html)] == ["Banco Supervielle S.A.", "Supervielle Seguros S.A."]

    def test_the_next_page_keeps_its_own_cells(self):
        # BHP: the header's name cell spans two grid columns; a later page's rows
        # have one column each, and the old page's grid would read "100%" as place
        html = ('<table><tr><td colspan="2">Name</td><td>Jurisdiction</td></tr>'
                '<tr><td colspan="2">Alpha Ltd.</td><td>Bermuda</td></tr></table>'
                '<table><tr><td>Beta GmbH</td><td>Germany</td><td>100%</td></tr>'
                '<tr><td>Gamma AB</td><td>Sweden</td><td>100%</td></tr></table>')
        assert {s["name"]: s["jurisdiction"] for s in parse_exhibit(html)} == {
            "Alpha Ltd.": "Bermuda", "Beta GmbH": "Germany", "Gamma AB": "Sweden"}

    def test_a_place_cell_starting_under_the_spacer(self):
        # BGM: the place cell spans the spacer and the place column
        html = ("<table><tr><td>Name</td><td></td><td>Jurisdiction of Incorporation</td></tr>"
                '<tr><td>Alpha Ltd.</td><td colspan="2">Bermuda</td></tr></table>')
        assert parse_exhibit(html) == [{"name": "Alpha Ltd.", "jurisdiction": "Bermuda"}]

    def test_the_cell_index_when_the_grid_finds_nothing(self):
        html = ("<table><tr><td>Name</td><td>Jurisdiction</td></tr>"
                '<tr><td colspan="2">Alpha Ltd.</td><td>Bermuda</td></tr></table>')
        assert parse_exhibit(html) == [{"name": "Alpha Ltd.", "jurisdiction": "Bermuda"}]

    def test_a_row_number_is_never_a_name(self):
        # Chanson: "Variable Interest Entities" reads as an ownership label, the
        # name column falls on the row numbers
        html = ("<table><tr><td></td><td>Variable Interest Entities</td><td></td><td>Place of Incorporation</td></tr>"
                "<tr><td>12</td><td>Tianshan District Minzhu Co., Ltd.</td><td></td><td>PRC</td></tr>"
                "<tr><td>13</td><td>Tianshan District Xinmin Co., Ltd.</td><td></td><td>PRC</td></tr></table>")
        assert not any(s["name"].isdigit() for s in parse_exhibit(html))

    def test_a_section_header_after_data_still_remaps(self):
        html = ("<table><tr><td>Subsidiary</td><td>Jurisdiction</td></tr>"
                "<tr><td>Alpha Ltd.</td><td>Bermuda</td></tr>"
                "<tr><td>Subsidiary of Alpha Ltd.</td><td></td><td>Jurisdiction</td></tr>"
                "<tr><td>Beta GmbH</td><td></td><td>Germany</td></tr></table>")
        subs = _by_name(parse_exhibit(html))
        assert subs["Beta GmbH"]["jurisdiction"] == "Germany" and subs["Beta GmbH"]["parent"] == "Alpha Ltd."

    def test_a_label_spanning_the_whole_row_is_neither_name_nor_place(self):
        subs = _read("aifu_ex81.htm", "AIFU Inc.")
        assert len(subs) == 30
        assert "Insurance Agencies and Brokers" not in _by_name(subs)
        html = ('<table><tr><td colspan="2">Name</td><td colspan="2">Place of Incorporation</td></tr>'
                '<tr><td colspan="4">Insurance Agencies and Brokers</td></tr>'
                '<tr><td>1.</td><td>Alpha Ltd.</td><td></td><td>BVI</td></tr></table>')
        assert parse_exhibit(html) == [{"name": "Alpha Ltd.", "jurisdiction": "BVI"}]

    def test_a_row_number_in_the_name_cell_is_dropped(self):
        html = ("<table><tr><td>Subsidiaries</td><td>Place of Incorporation</td></tr>"
                "<tr><td>1. YD Network Technology Company Limited</td><td>Hong Kong SAR</td></tr>"
                "<tr><td>2. Everbright Solutions Limited</td><td>BVI</td></tr></table>")
        assert [s["name"] for s in parse_exhibit(html)] == ["YD Network Technology Company Limited",
                                                            "Everbright Solutions Limited"]

    def test_place_of_incorp_and_location_with_jurisdiction(self):
        cut = ("<table><tr><td>Companies</td><td>Place of incorp</td></tr>"
               "<tr><td>Reitar Capital Partners Limited</td><td>BVI</td></tr></table>")
        assert parse_exhibit(cut)[0]["jurisdiction"] == "BVI"
        both = ("<table><tr><td>Subsidiary</td><td>Location Jurisdiction of Organization</td></tr>"
                "<tr><td>Mytheresa SE</td><td>Munich, Germany</td></tr></table>")
        assert parse_exhibit(both)[0]["jurisdiction"] == "Munich, Germany"


class TestOnePerLine:
    def test_a_hong_kong_company(self):
        subs = _by_name(_read("finvolution_ex81.htm", "FinVolution Group"))
        assert len(subs) == 16
        assert subs["Bluebottle Limited"]["jurisdiction"] == "Hong Kong"
        assert jurisdiction_country(subs["Shanghai Erxu Information Technology Co., Ltd."]["jurisdiction"]) == "CN"

    def test_incorporated_in_the_macau_special_administrative_region(self):
        subs = _by_name(_read("melco_ex81.htm", "Melco Resorts & Entertainment LTD"))
        assert len(subs) == 20
        assert jurisdiction_country(subs["COD Resorts Limited"]["jurisdiction"]) == "MO"

    def test_owns_a_share_of_the_interests_in(self):
        subs = _by_name(_read("ambev_ex81.htm", "AMBEV S.A."))
        assert len(subs) == 18
        assert subs["Cerveceria Y Malteria Quilmes Saica Y G"] == {
            "name": "Cerveceria Y Malteria Quilmes Saica Y G", "jurisdiction": "Argentina",
            "stake_percent": 99.83}

    @pytest.mark.parametrize("line,name,place", [
        ("Vuela, S.A., a corporation organized under the laws of Guatemala", "Vuela, S.A.", "Guatemala"),
        ("CASI Pharmaceuticals (China) Co., Ltd, a company of limited liability, incorporated and existing "
         "under the laws of the People’s Republic of China", "CASI Pharmaceuticals (China) Co., Ltd",
         "People’s Republic of China"),
        ("VERAXA Biotech GmbH, validly existing under the laws of Germany.", "VERAXA Biotech GmbH", "Germany"),
        ("Zooz Power Cayman, a Cayman Islands exempted company and a wholly owned subsidiary of the Company.",
         "Zooz Power Cayman", "Cayman Islands"),
        ("Renovation Investment (Hong Kong) Co., Ltd. (“Renovation”) is a Hong Kong company and is "
         "wholly-owned by the Company.", "Renovation Investment (Hong Kong) Co., Ltd.", "Hong Kong"),
        ("ATA Education Technology (Beijing) Limited (formerly known as “ATA Testing Authority (Beijing) "
         "Limited”), incorporated in the People’s Republic of China",
         "ATA Education Technology (Beijing) Limited", "People’s Republic of China"),
        ("Sinovac Biotech Co., Ltd., a company incorporated in the Chinese mainland", "Sinovac Biotech Co., Ltd.",
         "Chinese mainland"),
        ("Beijing Rongsanliuling Information Technology Co., Ltd. a PRC company",
         "Beijing Rongsanliuling Information Technology Co., Ltd.", "PRC"),
    ])
    def test_one_stated_line_is_enough(self, line, name, place):
        assert parse_exhibit(_doc(f"<p>Exhibit 8.1</p><p>{line}</p>", None)) == [
            {"name": name, "jurisdiction": place}]

    def test_a_sentence_is_no_subsidiary(self):
        # Samfine's F-1 Exhibit 8.2, a tax opinion
        body = ("<p>We act as PRC counsel to Samfine Creation Holdings Group Limited, a company incorporated "
                "in the Cayman Islands</p><p>12-14th Floor, China World Office 2, No. 1 Jianguomenwai Avenue, "
                "Beijing 100004, China</p><p>This opinion is given to Samfine Limited</p>")
        assert parse_exhibit(_doc(body, "EX-21.1")) == []

    @pytest.mark.parametrize("lines", [
        ["Sportradar AG, Switzerland", "Sports Data AG, Switzerland", "Sportradar AB, Sweden"],
        ["1.XPACSponsor LLC - Cayman", "2.XProject LTD - Cayman", "3.XP Holding UK Ltd - UK"],
    ])
    def test_a_set_off_place_needs_three(self, lines):
        body = "".join(f"<p>{x}</p>" for x in lines)
        assert len(parse_exhibit(_doc(body, None))) == 3
        assert parse_exhibit(_doc("".join(f"<p>{x}</p>" for x in lines[:2]), None)) == []

    def test_numbered_table_rows_with_the_place_in_brackets(self):
        rows = "".join(f"<tr><td>{i}.</td><td>{n}</td></tr>" for i, n in enumerate(
            ["DLP Capital LLC (USA - Delaware)", "Stone ALP Holding SARL (Luxembourg)",
             "Stone Capital AG (Switzerland)"], 1))
        subs = parse_exhibit(_doc(f"<table>{rows}</table>", None))
        assert [jurisdiction_country(s["jurisdiction"]) for s in subs] == ["US", "LU", "CH"]


class TestNamesWithoutAPlace:
    def test_a_declared_list_of_names_beside_some_with_places(self):
        subs = _read("karooooo_ex81.htm", "Karooooo Ltd.")
        assert len(subs) == 46
        assert sum(1 for s in subs if s["jurisdiction"]) == 16
        assert _by_name(subs)["Cartrack Holdings (Pty) Ltd"]["jurisdiction"] == ""

    def test_only_from_a_declared_subsidiary_exhibit(self):
        names = "".join(f"<p>{n}</p>" for n in ("Alpha Holdings Limited", "Beta Pte. Ltd.", "Gamma GmbH"))
        assert len(parse_exhibit(_doc(names))) == 3
        assert parse_exhibit(_doc(names, None)) == []
        # nor beside a stated line on an undeclared page
        stated = "<p>Delta Limited, a Hong Kong company</p>" + names
        assert [s["name"] for s in parse_exhibit(_doc(stated, None))] == ["Delta Limited"]
        assert len(parse_exhibit(_doc(stated))) == 4

    def test_a_heading_gives_the_place_and_the_registrant_is_left_out(self):
        subs = _by_name(_read("recon_ex81.htm", "Recon Technology, Ltd"))
        assert len(subs) == 9 and "Recon Technology, Ltd" not in subs
        assert subs["Recon Investment Ltd."]["jurisdiction"] == "Hong Kong"
        assert subs["Gan Su BHD Environmental Technology Co., Ltd."] == {
            "name": "Gan Su BHD Environmental Technology Co., Ltd.", "jurisdiction": "PRC",
            "stake_percent": 51.0}

    def test_the_document_says_where_they_all_are(self):
        subs = _read("bancodechile_ex81.htm", "BANK OF CHILE")
        assert len(subs) == 5 and {jurisdiction_country(s["jurisdiction"]) for s in subs} == {"CL"}

    def test_organized_in_the_commonwealth_of_virginia(self):
        # Shenandoah's 10-K Exhibit 21: one sentence, then the names
        body = ("<p>The following are all significant subsidiaries of Shenandoah Telecommunications Company, and "
                "are organized in the Commonwealth of Virginia.</p><p>Shenandoah Cable Television LLC</p>"
                "<p>Shentel Management Company</p><p>Horizon Telcom Inc.</p>")
        subs = parse_exhibit(_doc(body, "EX-21"), "SHENANDOAH TELECOMMUNICATIONS CO", "10-K")
        assert [jurisdiction_country(s["jurisdiction"]) for s in subs] == ["US", "US", "US"]

    def test_all_but_some_is_not_a_place_for_all(self):
        # Highway Holdings
        body = ("<p>Alpha Holdings Limited</p><p>Beta Shenzhen Limited</p><p>All subsidiaries, with the exception "
                "of Beta Shenzhen Limited (organized in China), are incorporated in Hong Kong.</p>")
        assert {s["jurisdiction"] for s in parse_exhibit(_doc(body))} == {""}

    def test_headings_and_titles_are_not_names(self):
        body = ("<p>Principal Subsidiaries of Eason Technology Limited</p><p>Subsidiaries:</p>"
                "<p>True Silver Limited</p><p>Four Divisions Limited</p>")
        assert [s["name"] for s in parse_exhibit(_doc(body), "Eason Technology Ltd")] == [
            "True Silver Limited", "Four Divisions Limited"]


class TestTableWithoutAPlaceColumn:
    def test_the_place_from_a_sentence_and_the_total_stake(self):
        subs = _by_name(_read("santanderchile_ex81.htm", "BANCO SANTANDER CHILE"))
        assert len(subs) == 6
        assert subs["Santander Corredora de Seguros Limitada"] == {
            "name": "Santander Corredora de Seguros Limitada", "jurisdiction": "Chile", "stake_percent": 99.76}

    def test_direct_indirect_total(self):
        subs = _by_name(_read("enelchile_ex81.htm", "Enel Chile S.A."))
        assert len(subs) == 11
        assert subs["Empresa Eléctrica Pehuenche S.A."]["stake_percent"] == 92.65
        assert {s["jurisdiction"] for s in subs.values()} == {"Chile"}

    def test_a_name_wrapped_over_rows(self):
        html = _doc("<p>All direct subsidiaries are domiciled in Indonesia.</p>"
                    "<table><tr><td>Subsidiary</td><td>Business</td><td>%</td></tr>"
                    "<tr><td>PT Telekomunikasi</td><td>Mobile</td><td>70</td></tr>"
                    "<tr><td>Selular</td><td>telecommunication</td><td></td></tr>"
                    "<tr><td>PT Dayamitra Telekomunikasi Tbk.</td><td>Towers</td><td>72</td></tr></table>")
        assert parse_exhibit(html) == [
            {"name": "PT Telekomunikasi Selular", "jurisdiction": "Indonesia", "stake_percent": 70.0},
            {"name": "PT Dayamitra Telekomunikasi Tbk.", "jurisdiction": "Indonesia", "stake_percent": 72.0}]

    def test_not_in_an_undeclared_page_nor_for_rows_that_are_no_companies(self):
        table = ("<table><tr><td>Name of Subsidiary</td><td>% held</td></tr>"
                 "<tr><td>Merit Stone Limited</td><td>100%</td></tr></table>")
        assert parse_exhibit(_doc(table)) == [{"name": "Merit Stone Limited", "jurisdiction": "",
                                               "stake_percent": 100.0}]
        assert parse_exhibit(_doc(table, None)) == []
        notes = ("<table><tr><td>Name</td><td>Interest</td></tr><tr><td>Senior notes due 2030</td>"
                 "<td>5%</td></tr><tr><td>Ordinary shares</td><td>100%</td></tr></table>")
        assert parse_exhibit(_doc(notes)) == []


class TestCountryRows:
    def test_bats_names_under_their_countries(self):
        subs = _by_name(_read("bat_ex8_excerpt.htm", "British American Tobacco p.l.c."))
        assert len(subs) == 32
        assert subs["British American Tobacco (Algérie) S.P.A."] == {
            "name": "British American Tobacco (Algérie) S.P.A.", "jurisdiction": "Algeria", "stake_percent": 51.0}
        assert subs["British American Tobacco – Albania SH.P.K."]["jurisdiction"] == "Albania"

    def test_a_registered_office_line_is_no_company(self):
        rows = "".join(f"<tr><td>{c}</td></tr>" for c in (
            "Cayman Islands", "Alpha Ltd.",
            ("Trident Trust Company (Cayman) Ltd., One Capital Place, PO Box 847, "
             "Grand Cayman KY1-1103, Cayman Islands"), "France", "Beta SAS", "Italy", "Gamma S.p.A.", "Delta S.r.l.",
            "Epsilon SpA"))
        assert [s["name"] for s in parse_exhibit(_doc(f"<table>{rows}</table>"))] == [
            "Alpha Ltd.", "Beta SAS", "Gamma S.p.A.", "Delta S.r.l.", "Epsilon SpA"]

    def test_up_to_the_associates(self):
        rows = "".join(f"<tr><td>{c}</td></tr>" for c in (
            "France", "Alpha SAS", "Germany", "Beta GmbH", "Italy", "Gamma S.p.A.", "Delta S.r.l.", "Epsilon SpA"))
        html = _doc(f"<table>{rows}</table><p>Associate undertakings</p>"
                    "<table><tr><td>Spain</td></tr><tr><td>Zeta S.A.</td></tr></table>")
        assert [s["name"] for s in parse_exhibit(html)] == ["Alpha SAS", "Beta GmbH", "Gamma S.p.A.",
                                                            "Delta S.r.l.", "Epsilon SpA"]


class TestScannedPages:
    def test_amer_sports_text_layer(self):
        subs = _by_name(_read("amersports_ex81.htm", "Amer Sports, Inc."))
        assert len(subs) == 79
        assert subs["Amer Sports Austria GmbH Magyarországi Fióktelepe"]["jurisdiction"] == "Hungary"  # wrapped
        assert subs["Amer Sports Sourcing Limited Rep office in Thailand"]["jurisdiction"] == "Thailand"
        assert subs["Amer Sports Holding (HK) Limited"]["jurisdiction"] == "Hong Kong SAR, China"
        assert subs["Amer Sports Korea Ltd."]["jurisdiction"] == "South Korea"                    # footnote "1"
        assert subs["Amer Sports Winter & Outdoor Company"]["jurisdiction"] == "United States"
        assert "Amer Sports Company" not in subs        # the filer's own name, as everywhere

    def test_borr_branches_and_an_inverted_place(self):
        subs = _by_name(_read("borr_ex81.htm", "Borr Drilling Ltd"))
        assert len(subs) == 92
        assert subs["Borr Arabia Well Drilling LLC"]["jurisdiction"] == "Kingdom of Saudi Arabia"
        assert jurisdiction_country(subs["Borr (Mauritius) Holdings Limited"]["jurisdiction"]) == "MU"
        assert subs["Borr Gerd Inc. (Cameroon Branch)"]["jurisdiction"] == "Cameroon"

    def test_polestar_lines_end_in_a_stake(self):
        subs = _read("polestar_ex81.htm", "Polestar Automotive Holding UK PLC")
        assert len(subs) == 34
        assert subs[0] == {"name": "Polestar Holding AB", "jurisdiction": "Sweden", "stake_percent": 100.0}

    def test_no_image_no_text_layer(self):
        layer = '<font style="font-size:1pt;color:white">Alpha Ltd. Bermuda  Beta GmbH Germany  Gamma AB Sweden</font>'
        assert len(ex._hidden_text_list(f"<img src='p1.jpg'>{layer}", None)) == 3
        assert ex._hidden_text_list(layer, None) == []


class TestPlaces:
    @pytest.mark.parametrize("place,code", [
        ("Mauritius", "MU"), ("Cyprus", "CY"), ("Belarus", "BY"),          # were "US" until 2026-10-07
        ("Delaware, US", "US"), ("Texas, U.S.A.", "US"), ("USA", "US"),
        ("Hong Kong SAR, China", "HK"), ("Macao SAR, China", "MO"),
        ("Hong Kong Special Administrative Region of the PRC", "HK"),
        ("Macau Special Administrative Region of the People’s Republic of China", "MO"),
        ("Chinese mainland", "CN"), ("Republic of Chile", "CL"), ("Kingdom of Saudi Arabia", "SA"),
        ("Congo, Democratic Republic of", "CD"), ("Curacao", "CW"), ("Holland", "NL"), ("Cayman Island", "KY"),
        ("Commonwealth of Virginia", "US"), ("State of Israel", "IL"), ("Commonwealth of the Bahamas", "BS"),
        ("Republic of China", None), ("Hong Kong SAR, China Amer", None), ("in", "IN"),
    ])
    def test_mapping(self, place, code):
        assert jurisdiction_country(place) == code

    def test_the_state_behind_commonwealth_of(self):
        assert ex.jurisdiction_subdivision("Commonwealth of Virginia") == "US-VA"
        assert ex.jurisdiction_subdivision("State of Delaware") == "US-DE"

    @pytest.mark.parametrize("raw,clean", [
        ("British American Tobacco – B.A.T. Angola, Limitada (99.80%)(99.93%)^",
         ("British American Tobacco – B.A.T. Angola, Limitada", 99.8)),
        ("British American Tobacco (Algérie) S.P.A. (51%)4", ("British American Tobacco (Algérie) S.P.A.", 51.0)),
        ("Liggett & Myers Tobacco Company of Canada Limited (70%) (50%)^3",
         ("Liggett & Myers Tobacco Company of Canada Limited", 70.0)),
        ("Cactos Fundo de Investimento (iv)", ("Cactos Fundo de Investimento", None)),
        ("Genstar Corporation#", ("Genstar Corporation", None)),
        ("Bidangil Picture #1", ("Bidangil Picture #1", None)),
        ("~ Navigator Titan L.L.C.", ("Navigator Titan L.L.C.", None)),          # a tree marker
    ])
    def test_marks_and_stakes_after_a_name(self, raw, clean):
        assert _clean_name(raw) == clean


def test_a_table_the_reader_handles_is_read_as_before():
    # every new reader runs only when the table reader found nothing
    apple = (FX / "apple_ex21.htm").read_text()
    with patch.object(ex, "_country_rows_list", side_effect=AssertionError("ran")), \
         patch.object(ex, "_line_list", side_effect=AssertionError("ran")), \
         patch.object(ex, "_unplaced_table_list", side_effect=AssertionError("ran")), \
         patch.object(ex, "_hidden_text_list", side_effect=AssertionError("ran")):
        assert parse_exhibit(apple, "Apple Inc.")
