"""Exhibit 8.1 / 21 layouts the table reader could not see — measured on the
2026 20-Fs whose subsidiary file read nothing (86 filers, 61 real lists),
fixtures captured from those filings:
- files the filer declared something else ("<TYPE>EX-2.1", the description of
  securities Workiva names "exhibit21descriptionofsecu.htm");
- header cells spanning several data cells (TORM, Ellomay) and a header
  printed over three rows (UTStarcom);
- names under one-cell country rows (BAT);
- places the mapping got wrong ("Mauritius" was the United States);
- and the layouts deliberately NOT read: one subsidiary per line with its
  place in words (FinVolution, Melco, Ambev), names without a place
  (Karooooo, Recon, Banco de Chile), tables naming no place column (Banco
  Santander Chile), the text layer behind scanned pages (Amer Sports),
  mislabelled lists (Yatra).
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

    def test_the_next_page_on_the_same_grid_reads_by_the_grid(self):
        # Western Union: the place sits behind a spacer cell on every page; by
        # cell index pages two and three read no place (44 of 102)
        header = ('<tr><td colspan="2">Name of Subsidiary</td><td></td>'
                  '<td>Jurisdiction of Incorporation</td></tr>')
        html = "".join(
            f"<table>{header if i == 0 else ''}"
            + "".join(f"<tr><td>{n}</td><td></td><td></td><td>{p}</td></tr>" for n, p in page) + "</table>"
            for i, page in enumerate([[("Alpha Corporation", "Delaware, USA"), ("Beta Ltda.", "Brazil")],
                                      [("Gamma Limited", "Hong Kong"), ("Delta SARL", "France")],
                                      [("Epsilon S.A.", "Mexico")]]))
        assert {s["name"]: s["jurisdiction"] for s in parse_exhibit(html)} == {
            "Alpha Corporation": "Delaware, USA", "Beta Ltda.": "Brazil", "Gamma Limited": "Hong Kong",
            "Delta SARL": "France", "Epsilon S.A.": "Mexico"}

    def test_a_tie_keeps_the_cell_index(self):
        # both readings find a place in every row: the next page is read as
        # it was before the grid was tried
        html = ('<table><tr><td colspan="2">Name</td><td>Jurisdiction</td></tr>'
                '<tr><td colspan="2">Alpha Ltd.</td><td>Bermuda</td></tr></table>'
                '<table><tr><td>Beta GmbH</td><td>Germany</td><td>Austria</td></tr></table>')
        assert {s["name"]: s["jurisdiction"] for s in parse_exhibit(html)} == {
            "Alpha Ltd.": "Bermuda", "Beta GmbH": "Germany"}

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


class TestNotRead:
    """Layouts deliberately left unread. Names without a place, tables naming
    no place column, scanned pages and mislabelled lists each served one to
    five filers (removed 2026-10-07): a reader that guesses at them can turn a
    heading or a sentence into a subsidiary. One subsidiary per line with its
    place in words served more (20 of the 287 lists read from 600 random
    10-Ks) and was removed 2026-10-08 all the same: it read combined 10-Ks'
    group lists (Duke Energy Ohio, Texas-New Mexico Power, Idaho Power) as the
    filer's own, and parents stated in words ("a subsidiary of …") not at
    all."""

    @pytest.mark.parametrize("fixture,registrant", [
        ("finvolution_ex81.htm", "FinVolution Group"),            # "Bluebottle Limited, a Hong Kong company"
        ("melco_ex81.htm", "Melco Resorts & Entertainment LTD"),  # "…, incorporated in the Macau SAR …"
        ("ambev_ex81.htm", "AMBEV S.A."),                         # "owns 99.83% of … interests in …"
        ("karooooo_ex81.htm", "Karooooo Ltd."),                   # "Cartrack Inc. (USA)", most without a place
    ])
    def test_one_subsidiary_per_line(self, fixture, registrant):
        assert _read(fixture, registrant) == []

    @pytest.mark.parametrize("line", [
        "Vuela, S.A., a corporation organized under the laws of Guatemala",
        "VERAXA Biotech GmbH, validly existing under the laws of Germany.",
        "Renovation Investment (Hong Kong) Co., Ltd. (“Renovation”) is a Hong Kong company and is "
        "wholly-owned by the Company.",
        "Beijing Rongsanliuling Information Technology Co., Ltd. a PRC company",
    ])
    def test_a_place_in_words(self, line):
        assert parse_exhibit(_doc(f"<p>Exhibit 8.1</p><p>{line}</p>")) == []

    @pytest.mark.parametrize("lines", [
        ["Sportradar AG, Switzerland", "Sports Data AG, Switzerland", "Sportradar AB, Sweden"],
        ["1.XPACSponsor LLC - Cayman", "2.XProject LTD - Cayman", "3.XP Holding UK Ltd - UK"],
    ])
    def test_a_place_set_off_after_the_name(self, lines):
        assert parse_exhibit(_doc("".join(f"<p>{x}</p>" for x in lines))) == []

    def test_numbered_one_cell_rows(self):
        rows = "".join(f"<tr><td>{i}.</td><td>{n}</td></tr>" for i, n in enumerate(
            ["DLP Capital LLC (USA - Delaware)", "Stone ALP Holding SARL (Luxembourg)",
             "Stone Capital AG (Switzerland)"], 1))
        assert parse_exhibit(_doc(f"<table>{rows}</table>")) == []

    def test_a_sentence_is_no_subsidiary(self):
        # Samfine's F-1 Exhibit 8.2, a tax opinion
        body = ("<p>We act as PRC counsel to Samfine Creation Holdings Group Limited, a company incorporated "
                "in the Cayman Islands</p><p>12-14th Floor, China World Office 2, No. 1 Jianguomenwai Avenue, "
                "Beijing 100004, China</p><p>This opinion is given to Samfine Limited</p>")
        assert parse_exhibit(_doc(body, "EX-21.1")) == []

    def test_a_list_of_names_without_places(self):
        names = "".join(f"<p>{n}</p>" for n in ("Alpha Holdings Limited", "Beta Pte. Ltd.", "Gamma GmbH"))
        assert parse_exhibit(_doc(names)) == []

    def test_a_place_given_by_a_heading_or_a_sentence(self):
        # Recon: "Subsidiary (PRC):"; Banco de Chile, Shenandoah: "… are organized in …"
        heading = "<p>Subsidiary (PRC):</p><p>Alpha Technology Co., Ltd.</p><p>Beta Energy Co., Ltd.</p>"
        sentence = ("<p>All subsidiaries listed below are incorporated in Chile.</p>"
                    "<p>Alpha Asesorias Limitada</p><p>Beta Corredores S.A.</p><p>Gamma Leasing S.A.</p>")
        assert parse_exhibit(_doc(heading)) == []
        assert parse_exhibit(_doc(sentence)) == []
        assert parse_exhibit(_doc(sentence, "EX-21"), form="10-K") == []

    def test_a_table_without_a_place_column(self):
        # Banco Santander Chile, Enel Chile
        table = ("<p>All subsidiaries listed below are incorporated in Chile.</p>"
                 "<table><tr><td>Name of Subsidiary</td><td>Direct</td><td>Indirect</td><td>Total</td></tr>"
                 "<tr><td>Alpha Corredora Limitada</td><td>99.75</td><td>0.01</td><td>99.76</td></tr>"
                 "<tr><td>Beta Leasing S.A.</td><td>100</td><td></td><td>100</td></tr></table>")
        assert parse_exhibit(_doc(table)) == []

    def test_the_text_layer_behind_a_scanned_page(self):
        # Amer Sports, Borr, Polestar: page images over 1pt white text
        layer = ('<img src="p1.jpg"><font style="font-size:1pt;color:white">Alpha Ltd. Bermuda  '
                 'Beta GmbH Germany  Gamma AB Sweden  Delta B.V. Netherlands</font>')
        assert parse_exhibit(_doc(layer)) == []

    def test_a_mislabelled_list(self):
        # Yatra's subsidiary list is declared EX-10.8
        assert parse_exhibit(_doc("<p>List of Subsidiaries</p>" + TABLE, "EX-10.8")) == []


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

    def test_a_page_footer_is_no_company(self):
        # BAT prints "British American Tobacco p.l.c. Form 20-F 2025" on every page
        rows = "".join(f"<tr><td>{c}</td></tr>" for c in (
            "France", "Alpha SAS", "British American Tobacco p.l.c. Form 20-F 2025", "Germany", "Beta GmbH",
            "Italy", "Gamma S.p.A.", "Delta S.r.l.", "Epsilon SpA"))
        assert [s["name"] for s in parse_exhibit(_doc(f"<table>{rows}</table>"))] == [
            "Alpha SAS", "Beta GmbH", "Gamma S.p.A.", "Delta S.r.l.", "Epsilon SpA"]

    def test_headers_marks_footnotes_and_the_filer_are_no_company(self):
        note = ("4 The Group holds a controlling interest through a shareholder agreement and consolidates "
                "the company in full although its direct holding is below one half of the shares")
        rows = "".join(f"<tr><td>{c}</td></tr>" for c in (
            "France", "Alpha SAS", "Exhibit 8.1", "British American Tobacco p.l.c.", "Germany", "Beta GmbH", "†",
            "Italy", "Gamma S.p.A.", "Delta S.r.l.", "Epsilon SpA", note))
        assert [s["name"] for s in parse_exhibit(_doc(f"<table>{rows}</table>"), "British American Tobacco PLC")] \
            == ["Alpha SAS", "Beta GmbH", "Gamma S.p.A.", "Delta S.r.l.", "Epsilon SpA"]

    def test_up_to_the_associates(self):
        rows = "".join(f"<tr><td>{c}</td></tr>" for c in (
            "France", "Alpha SAS", "Germany", "Beta GmbH", "Italy", "Gamma S.p.A.", "Delta S.r.l.", "Epsilon SpA"))
        html = _doc(f"<table>{rows}</table><p>Associate undertakings</p>"
                    "<table><tr><td>Spain</td></tr><tr><td>Zeta S.A.</td></tr></table>")
        assert [s["name"] for s in parse_exhibit(html)] == ["Alpha SAS", "Beta GmbH", "Gamma S.p.A.",
                                                            "Delta S.r.l.", "Epsilon SpA"]

    def test_only_in_a_declared_subsidiary_exhibit(self):
        rows = "".join(f"<tr><td>{c}</td></tr>" for c in (
            "France", "Alpha SAS", "Germany", "Beta GmbH", "Italy", "Gamma S.p.A.", "Delta S.r.l.", "Epsilon SpA"))
        assert len(parse_exhibit(_doc(f"<table>{rows}</table>"))) == 5
        assert parse_exhibit(_doc(f"<table>{rows}</table>", None)) == []


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
        ("Grupo Disco Uruguay S.A. (a)", ("Grupo Disco Uruguay S.A.", None)),    # a letter footnote
        ("Crown Castle Fiber LLC(a)", ("Crown Castle Fiber LLC", None)),
        ("Class (A) Holdings", ("Class (A) Holdings", None)),
        ("Fund (Series A)", ("Fund (Series A)", None)),
    ])
    def test_marks_and_stakes_after_a_name(self, raw, clean):
        assert _clean_name(raw) == clean


# The three regressions the held-out check found (2026-10-07), fixed.
EXITO = _doc(
    "<table><tr><td>Name</td><td>Direct controlling entity</td><td>Country</td>"
    "<td>Stock ownership of direct controlling entity</td><td>Total direct and indirect ownership</td></tr>"
    "<tr><td>Spice Investment Mercosur S.A.</td><td>Almacenes Éxito S.A.</td><td>Uruguay</td>"
    "<td>100.00%</td><td>100.00%</td></tr>"
    "<tr><td>Patrimonio Autónomo Viva Malls</td><td>Almacenes Éxito S.A.</td><td>Colombia</td>"
    "<td>51.00%</td><td>51.00%</td></tr>"
    "<tr><td>Patrimonio Autónomo Viva Laureles</td><td>Patrimonio Autónomo Viva Malls</td><td>Colombia</td>"
    "<td>80.00%</td><td>40.80%</td></tr>"
    "<tr><td>Grupo Disco Uruguay S.A. (a)</td><td>Spice Investment Mercosur S. A.</td><td>Uruguay</td>"
    "<td>76.65%</td><td>76.65%</td></tr>"
    "<tr><td>Ameluz S.A.</td><td>Grupo Disco Uruguay S.A.</td><td>Uruguay</td><td>100.00%</td>"
    "<td>76.65%</td></tr></table>")


class TestParentColumn:
    """Almacenes Éxito: the ownership column is the DIRECT controlling
    entity's stake, and a column names that entity."""

    def test_each_row_under_the_holder_its_column_names(self):
        subs = _by_name(parse_exhibit(EXITO, "Almacenes Exito S.A."))
        assert subs["Patrimonio Autónomo Viva Laureles"] == {
            "name": "Patrimonio Autónomo Viva Laureles", "jurisdiction": "Colombia", "stake_percent": 80.0,
            "parent": "Patrimonio Autónomo Viva Malls", "parent_basis": "column"}
        # the filer named there (accents aside): directly under it, no parent
        assert subs["Spice Investment Mercosur S.A."]["parent_basis"] == "column"
        assert "parent" not in subs["Spice Investment Mercosur S.A."]

    def test_the_parent_as_the_list_writes_it(self):
        subs = _by_name(parse_exhibit(EXITO, "Almacenes Exito S.A."))
        # "Spice Investment Mercosur S. A." is the listed "… S.A."; the listed
        # "Grupo Disco Uruguay S.A. (a)" is "Grupo Disco Uruguay S.A."
        assert subs["Grupo Disco Uruguay S.A."]["parent"] == "Spice Investment Mercosur S.A."
        assert subs["Ameluz S.A."]["parent"] == "Grupo Disco Uruguay S.A."
        html = ("<table><tr><td>Name</td><td>Parent</td><td>Jurisdiction</td></tr>"
                "<tr><td>Seaspan Management Services Limited</td><td>Atlas Corp.</td><td>Bermuda</td></tr>"
                "<tr><td>Seaspan Advisory Services Limited</td><td>Seaspan Management Services Ltd.</td>"
                "<td>Bermuda</td></tr></table>")
        assert parse_exhibit(html, "Atlas Corp.")[1]["parent"] == "Seaspan Management Services Limited"

    def test_rows_the_filer_holds_beside_the_filers_own_listed_line(self):
        # Atlas lists itself; the rows its column says Atlas holds are under
        # the filer, not under that line
        html = ("<table><tr><td>Name</td><td>Parent</td><td>Jurisdiction</td></tr>"
                "<tr><td>Atlas Corp.</td><td>Poseidon Corp.</td><td>Marshall Islands</td></tr>"
                "<tr><td>Seaspan Corporation</td><td>Atlas Corp.</td><td>Marshall Islands</td></tr></table>")
        assert parse_exhibit(html, "Atlas Corp.")[1] == {
            "name": "Seaspan Corporation", "jurisdiction": "Marshall Islands", "parent_basis": "column"}

    def test_the_filer_under_its_edgar_name_and_a_namesake_with_another_legal_form(self):
        html = ("<table><tr><td>Name</td><td>Owned by</td><td>Jurisdiction</td></tr>"
                "<tr><td>KeyBank National Association</td><td>KeyCorp</td><td>United States</td></tr></table>")
        assert parse_exhibit(html, "KEYCORP /NEW/") == [
            {"name": "KeyBank National Association", "jurisdiction": "United States", "parent_basis": "column"}]
        # "Foo Technologies LLC" is a listed company, not the filer "Foo Technologies, Inc."
        html = ("<table><tr><td>Name</td><td>Held by</td><td>Jurisdiction</td></tr>"
                "<tr><td>Foo Technologies LLC</td><td>Foo Technologies, Inc.</td><td>Delaware</td></tr>"
                "<tr><td>Foo Ohio LLC</td><td>Foo Technologies LLC</td><td>Ohio</td></tr></table>")
        subs = _by_name(parse_exhibit(html, "Foo Technologies, Inc."))
        assert "parent" not in subs["Foo Technologies LLC"]
        assert subs["Foo Ohio LLC"]["parent"] == "Foo Technologies LLC"

    def test_a_holder_named_without_its_legal_form(self):
        # Atlas: "Seaspan Holdco III" is the one listed "Seaspan Holdco III Ltd."
        # — a column names group companies; with two such listed, neither
        html = ("<table><tr><td>Name</td><td>Owned by</td><td>Jurisdiction</td></tr>"
                "<tr><td>Seaspan Holdco III Ltd.</td><td>Atlas Corp.</td><td>Marshall Islands</td></tr>"
                "<tr><td>Seaspan 2180 Ltd.</td><td>Seaspan Holdco III</td><td>Marshall Islands</td></tr>"
                "<tr><td>Alpha Inc.</td><td>Atlas Corp.</td><td>Delaware</td></tr>"
                "<tr><td>Alpha LLC</td><td>Atlas Corp.</td><td>Ohio</td></tr>"
                "<tr><td>Beta GmbH</td><td>Alpha</td><td>Germany</td></tr></table>")
        subs = _by_name(parse_exhibit(html, "Atlas Corp."))
        assert subs["Seaspan 2180 Ltd."]["parent"] == "Seaspan Holdco III Ltd."
        assert subs["Beta GmbH"]["parent"] == "Alpha"

    def test_a_holder_named_like_the_row_with_another_legal_form(self):
        # a row is not its own parent, but "Covestor Limited" held by
        # "Covestor, Inc." is a company under another
        html = ("<table><tr><td>Name</td><td>Owned by</td><td>Jurisdiction</td></tr>"
                "<tr><td>Covestor, Inc.</td><td>Alpha Corp.</td><td>Delaware</td></tr>"
                "<tr><td>Covestor Limited</td><td>Covestor, Inc.</td><td>United Kingdom</td></tr>"
                "<tr><td>Beta LLC</td><td>Beta, LLC</td><td>Ohio</td></tr></table>")
        subs = _by_name(parse_exhibit(html, "Gamma Inc."))
        assert subs["Covestor Limited"]["parent"] == "Covestor, Inc."
        assert "parent" not in subs["Beta LLC"]

    @pytest.mark.parametrize("label,parent", [
        ("Direct controlling entity", True), ("Parent", True), ("Immediate parent company", True),
        ("Owned by", True), ("Controlled by", True),
        ("Name of parent and subsidiary", False), ("Ownership", False),
    ])
    def test_which_header_names_the_holder(self, label, parent):
        html = (f"<table><tr><td>Subsidiary</td><td>Jurisdiction</td><td>{label}</td></tr>"
                "<tr><td>Alpha Ltd.</td><td>Bermuda</td><td>Beta Holdings Ltd.</td></tr></table>")
        assert (parse_exhibit(html, "Registrant plc")[0].get("parent") == "Beta Holdings Ltd.") is parent

    def test_a_named_parent_beats_a_drawn_one(self):
        rows = "".join(f'<tr><td style="padding-left:{pad}pt">{n}</td><td>{p}</td><td>Delaware</td></tr>'
                       for n, p, pad in (("Alpha LLC", "Registrant Inc.", 0), ("Beta LLC", "Alpha LLC", 12),
                                         ("Gamma LLC", "Alpha LLC", 24), ("Delta LLC", "Alpha LLC", 24),
                                         ("Epsilon LLC", "Alpha LLC", 12)))
        html = f"<table><tr><td>Name</td><td>Parent</td><td>Jurisdiction</td></tr>{rows}</table>"
        subs = _by_name(parse_exhibit(html, "Registrant Inc."))
        assert subs["Gamma LLC"]["parent"] == "Alpha LLC" and subs["Gamma LLC"]["parent_basis"] == "column"


PURECYCLE = (
    '<table><tr><td colspan="4">Subsidiary</td><td>State of Jurisdiction Of Incorporation</td></tr>'
    '<tr><td colspan="4">PureCycle Technologies Holdings Corp.</td><td>Delaware</td></tr>'
    '<tr><td></td><td colspan="3">PureCycle Technologies LLC</td><td>Delaware</td></tr>'
    '<tr><td></td><td></td><td colspan="2">PureCycle Managed Services, LLC</td><td>Delaware</td></tr>'
    '<tr><td></td><td></td><td colspan="2">PCTO Holdco, LLC</td><td>Delaware</td></tr>'
    '<tr><td></td><td></td><td></td><td>PureCycle: Ohio, LLC</td><td>Ohio</td></tr>'
    '<tr><td colspan="4">PureCycle Belgium, BV</td><td>Belgium</td></tr></table>')


class TestEmptyCellsDrawATree:
    def test_purecycles_tree(self):
        subs = _by_name(parse_exhibit(PURECYCLE, "PureCycle Technologies, Inc."))
        assert {n: s.get("parent") for n, s in subs.items()} == {
            "PureCycle Technologies Holdings Corp.": None,
            "PureCycle Technologies LLC": "PureCycle Technologies Holdings Corp.",
            # under "… Technologies LLC", which is NOT the filer "… Technologies, Inc."
            "PureCycle Managed Services, LLC": "PureCycle Technologies LLC",
            "PCTO Holdco, LLC": "PureCycle Technologies LLC",
            "PureCycle: Ohio, LLC": "PCTO Holdco, LLC",
            "PureCycle Belgium, BV": None}

    def test_the_filers_own_root_line_still_counts_as_the_filer(self):
        html = PURECYCLE.replace("PureCycle Technologies Holdings Corp.", "PureCycle Technologies, Inc.")
        subs = _by_name(parse_exhibit(html, "PureCycle Technologies, Inc."))
        assert subs["PureCycle Technologies LLC"]["parent"] is None
        assert subs["PureCycle Technologies LLC"]["parent_basis"] == "indent"

    def test_a_row_number_is_no_indent(self):
        rows = "".join(f'<tr><td>{m}</td><td colspan="2">{n}</td><td>Delaware</td></tr>'
                       for m, n in (("1.", "Alpha LLC"), ("", "Beta LLC"), ("3.", "Gamma LLC"), ("", "Delta LLC"),
                                    ("", "Epsilon LLC"), ("6.", "Zeta LLC"), ("", "Eta LLC")))
        html = f'<table><tr><td colspan="3">Name</td><td>Jurisdiction</td></tr>{rows}</table>'
        assert not any("parent_basis" in s for s in parse_exhibit(html))


class TestOneNameTwoPlaces:
    def test_one_entry_per_country(self):
        # Lavoro's "Agrointegral Andina S.A.S." in Colombia and Ecuador: two
        # entries, two nodes (the user's call, 2026-10-08)
        html = ("<table><tr><td>Legal Name</td><td>Jurisdiction of Incorporation</td></tr>"
                "<tr><td>Agrointegral Andina S.A.S.</td><td>Colombia</td></tr>"
                "<tr><td>Agrointegral Andina S.A.S. (vii)</td><td>Ecuador</td></tr></table>")
        assert [s["jurisdiction"] for s in parse_exhibit(html)] == ["Colombia", "Ecuador"]

    def test_a_row_repeated_on_the_next_page_is_still_one(self):
        page = ("<table><tr><td>Name</td><td>Jurisdiction</td></tr>"
                "<tr><td>Alpha Ltd.</td><td>Bermuda</td></tr></table>")
        assert len(parse_exhibit(page + page.replace("Bermuda", "BERMUDA"))) == 1
