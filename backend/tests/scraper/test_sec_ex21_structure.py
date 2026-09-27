"""What an Exhibit 21 states beyond names: percentages, wherever the filer
puts them, and the group tree, where the filer draws one.

Fixtures are verbatim exhibits (or trimmed excerpts) as filed on EDGAR, picked
from a 62-filer sample for the shapes that exist in the wild: an indentation
tree drawn with non-breaking spaces (NYT), with CSS padding (Eversource,
LanzaTech), a tree that also states every stake and names co-holders (Chubb —
one list split over eleven printed pages, header on the first only), a heading
naming an intermediate parent between tables (Tenet's USPI section), a header
cell naming one inside a table (Inter & Co), a bare-number ownership column
under a blank header row (Lincoln National); and the look-alikes that must stay
flat: a uniform hanging indent (BlackRock) and a page heading repeated on every
page (Clearway).
"""
from pathlib import Path

import pytest

from app.scraper.sec_ex21 import (_clean_name, _named_parent, jurisdiction_country,
                                  jurisdiction_subdivision, parse_exhibit)

FIX = Path(__file__).parent / "fixtures"


def _load(name: str, registrant: str | None = None) -> list[dict]:
    return parse_exhibit((FIX / name).read_text(errors="replace"), registrant)


def _tree(subs: list[dict]) -> dict[str, str | None]:
    return {s["name"]: s.get("parent") for s in subs if s.get("parent_basis")}


class TestIndentationTrees:
    def test_nbsp_indented_rows_hang_off_the_row_above(self):
        tree = _tree(_load("nyt_ex21.htm", "The New York Times Company"))
        assert tree["Midtown Insurance Company"] == "NYT Capital, LLC"
        assert tree["International Media Concepts, Inc."] == "NYT Shared Service Center, Inc."
        assert tree["New York Times Limited"] == "NYT International LLC"
        # the filer's own root line makes its children direct holdings, not orphans
        assert tree["NYT Capital, LLC"] is None

    def test_css_padded_rows_form_a_three_level_tree(self):
        tree = _tree(_load("eversource_ex21.htm", "Eversource Energy"))
        assert tree["Aquarion Water Company"] == "Aquarion Company"
        assert tree["Abenaki Water Co., Inc."] == "Aquarion Water Company"
        assert tree["Harbor Electric Energy Company"] == "NSTAR Electric Company"
        assert "Aquarion Company" not in tree            # unindented: no parent

    def test_the_tree_survives_a_page_break(self):
        # Eversource's list is two tables; the second's first rows are indented
        # under the last unindented row of the first.
        tree = _tree(_load("eversource_ex21.htm", "Eversource Energy"))
        assert tree["Hopkinton LNG Corp."] == "Yankee Energy System, Inc."

    def test_lanzatech(self):
        tree = _tree(_load("lanzatech_ex21.htm", "LanzaTech Global, Inc."))
        assert tree["LanzaTech China Ltd."] == "LanzaTech Hong Kong Limited"
        assert tree["LanzaJet, Inc."] == "LanzaTech, Inc."

    def test_chubb_parents_and_stakes_together(self):
        by = {s["name"]: s for s in _load("chubb_ex21_excerpt.htm", "Chubb Limited")}
        assert by["Oasis Investments Ltd."]["parent"] == "Chubb Tempest Reinsurance Ltd."
        assert by["Chubb Bermuda Insurance Ltd."]["parent"] == "Chubb Group Management and Holdings Ltd."
        assert by["Chubb Insurance (Switzerland) Limited"]["parent"] is None   # under the filer's root line
        assert by["Chubb Insurance (Switzerland) Limited"]["parent_basis"] == "indent"

    def test_a_uniform_hanging_indent_is_not_a_tree(self):
        subs = _load("blackrock_ex21_excerpt.htm", "BlackRock, Inc.")
        assert len(subs) == 10
        assert not any(s.get("parent") or s.get("parent_basis") for s in subs)

    def test_a_short_list_is_never_a_tree(self):
        html = ("<table><tr><td>A Ltd</td><td>Ireland</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;B Ltd</td><td>Ireland</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;C Ltd</td><td>Ireland</td></tr></table>")
        assert not any(s.get("parent") for s in parse_exhibit(html))

    def test_fewer_than_three_indented_rows_is_not_a_tree(self):
        html = ("<table><tr><td>A Ltd</td><td>Ireland</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;B Ltd</td><td>Ireland</td></tr>"
                "<tr><td>C Ltd</td><td>Ireland</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;D Ltd</td><td>Ireland</td></tr>"
                "<tr><td>E Ltd</td><td>Ireland</td></tr></table>")
        assert not any(s.get("parent") for s in parse_exhibit(html))

    def test_an_indented_first_row_means_no_tree(self):
        html = ("<table>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;A Ltd</td><td>Ireland</td></tr>"
                "<tr><td>B Ltd</td><td>Ireland</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;C Ltd</td><td>Ireland</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;D Ltd</td><td>Ireland</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;E Ltd</td><td>Ireland</td></tr></table>")
        assert not any(s.get("parent") for s in parse_exhibit(html))

    def test_a_stray_far_indent_is_noise_not_a_level(self):
        rows = [("A Ltd", 0), ("B Ltd", 4), ("C Ltd", 4), ("D Ltd", 200), ("E Ltd", 4), ("F Ltd", 0)]
        html = "<table>" + "".join(
            f'<tr><td style="padding-left:{i}pt">{n}</td><td>Ireland</td></tr>' for n, i in rows) + "</table>"
        assert _tree(parse_exhibit(html))["D Ltd"] == "A Ltd"        # not "C Ltd"


class TestHeadings:
    def test_a_heading_between_tables_parents_the_rows_below_it(self):
        by = {s["name"]: s for s in _load("tenet_ex21_excerpt.htm", "Tenet Healthcare Corporation")}
        assert by["Advanced Ambulatory Surgical Care, L.P."]["parent"] == "USPI Holding Company, Inc."
        assert by["Advanced Ambulatory Surgical Care, L.P."]["parent_basis"] == "heading"
        assert "parent" not in by["601 N 30th Street I, L.L.C."]
        assert "USPI Holding Company, Inc." in by, "the footnote digit is stripped from the name"

    def test_a_header_cell_inside_the_table_opens_a_section(self):
        by = {s["name"]: s.get("parent") for s in _load("inter_ex81.htm", "Inter & Co, Inc.")}
        assert by["Banco Inter S.A."] is None                   # "Subsidiary of Inter&Co, Inc" = the filer
        assert by["Inter Asset Gestão de Recursos Ltda"] == "Banco Inter S.A."

    def test_a_heading_naming_the_filer_parents_nobody(self):
        subs = _load("clearway_ex21_excerpt.htm", "Clearway Energy, Inc.")
        assert len(subs) == 8 and not any(s.get("parent") for s in subs)

    @pytest.mark.parametrize("text, registrant, expected", [
        ("Subsidiaries of the Registrant", None, None),
        ("Subsidiaries of the Registrants as of February 17, 2026 (1)", None, None),
        ("SUBSIDIARIES OF CHEVRON CORPORATION1", "Chevron Corp", None),
        ("Subsidiaries of X Holdings at December 31, 2025", "Y", "X Holdings"),
        ("Consolidated Subsidiaries of USPI Holding Company, Inc.", "Tenet", "USPI Holding Company, Inc."),
        ("Subsidiary of Banco Inter S.A.", "Inter & Co, Inc.", "Banco Inter S.A."),
        ("Name of Subsidiary", None, None),
    ])
    def test_named_parent(self, text, registrant, expected):
        assert _named_parent(text, registrant) == expected


class TestOwnershipColumns:
    def test_chubb_keeps_its_ownership_column_past_the_first_page(self):
        subs = _load("chubb_ex21_excerpt.htm")
        by = {s["name"]: s for s in subs}
        # page 2 has no header row of its own — the column is inherited
        assert by["ABR Reinsurance Capital Holdings Ltd."]["stake_percent"] == 19.1483
        assert by["Oasis Investments 2 Ltd."]["stake_percent"] == 66.66
        # everything but the filer's own "Publicly held" line carries a stake
        assert len(subs) == 76
        assert sum(1 for s in subs if "stake_percent" in s) == 75

    def test_co_holders_are_read_with_their_shares(self):
        by = {s["name"]: s for s in _load("chubb_ex21_excerpt.htm")}
        oasis = by["Oasis Investments Ltd."]
        assert oasis["stake_percent"] == 66.66
        assert oasis["co_owners"] == [{"name": "Chubb Bermuda Insurance Ltd.", "stake_percent": 33.33}]
        # "87.9864606% 12.0135394% (Chubb Limited)" — the co-holder is the filer
        ina = by["Chubb INA Holdings LLC"]
        assert ina["stake_percent"] == 87.9864606
        assert ina["co_owners"] == [{"name": "Chubb Limited", "stake_percent": 12.0135394}]
        # "99.9999309%0.0000691% (…)" — no space between the shares
        brasil = by["Chubb Tempest Reinsurance Ltd. Escritório de Representação No Brasil Ltda."]
        assert brasil["stake_percent"] == 99.9999309
        assert brasil["co_owners"][0]["name"] == "Chubb Tempest Life Reinsurance Ltd."

    def test_a_bare_number_under_an_ownership_header_is_a_stake(self):
        subs = _load("lincoln_ex21.htm")
        assert len(subs) == 25
        assert {s.get("stake_percent") for s in subs} == {100.0}
        assert not any(s["name"].startswith("Organized") for s in subs)

    def test_a_number_in_a_column_not_headed_as_ownership_is_not_a_stake(self):
        html = ("<table><tr><td>Name</td><td>Jurisdiction</td><td>Year formed</td></tr>"
                "<tr><td>Acme Ltd</td><td>Ireland</td><td>100</td></tr></table>")
        assert all("stake_percent" not in s for s in parse_exhibit(html))

    def test_an_inline_stake_in_the_name_is_read_and_stripped(self):
        by = {s["name"]: s for s in _load("nyt_ex21.htm")}
        assert by["The New York Times Building LLC"]["stake_percent"] == 58.0
        assert by["New York Times France-Kathimerini Commercial S.A."]["stake_percent"] == 50.0

    @pytest.mark.parametrize("raw, expected", [
        ("USPI Holding Company, Inc.1", ("USPI Holding Company, Inc.", None)),
        ("NSTAR Electric Company (2) (3)", ("NSTAR Electric Company", None)),
        ("NE Media Group, Inc.2", ("NE Media Group, Inc.", None)),
        ("Acme GmbH*", ("Acme GmbH", None)),
        ("The New York Times Building LLC (58%)", ("The New York Times Building LLC", 58.0)),
        ("Area 51 Inc", ("Area 51 Inc", None)),
        ("Trust IV", ("Trust IV", None)),
    ])
    def test_clean_name(self, raw, expected):
        assert _clean_name(raw) == expected


class TestHeadersWithoutANameColumn:
    def test_eversource_is_read_completely(self):
        subs = _load("eversource_ex21.htm")
        assert len(subs) == 40
        assert {s["name"] for s in subs} >= {"Aquarion Water Company", "Hopkinton LNG Corp.",
                                             "NSTAR Electric Company"}

    def test_two_letter_state_codes_are_us_states_not_countries(self):
        assert jurisdiction_country("DE") == "US"          # Delaware, not Germany
        assert jurisdiction_subdivision("DE") == "US-DE"
        assert jurisdiction_country("CT") == "US"
        assert jurisdiction_country("Germany") == "DE"

    def test_a_place_is_never_taken_for_a_header_row(self):
        # "ERICO Global Company | United States": "Company" and "States" look
        # like header words; treating the row as a header dropped it.
        html = ("<table><tr><td>Name of Company</td><td>Jurisdiction of Incorporation</td></tr>"
                "<tr><td>ERICO France Sarl</td><td>France</td></tr>"
                "<tr><td>ERICO Global Company</td><td>United States</td></tr>"
                "<tr><td>Zeta Company Ltd</td><td>United States of America</td></tr></table>")
        assert [s["name"] for s in parse_exhibit(html)] == [
            "ERICO France Sarl", "ERICO Global Company", "Zeta Company Ltd"]

    def test_a_headerless_list_is_not_headed_by_one_of_its_own_rows(self):
        # News Corp: no header row; "Factiva, Inc. | United States of America"
        # was taken for the header and dropped, with every row before it.
        html = ("<table><tr><td>Alpha Ltd</td><td>Ireland</td></tr>"
                "<tr><td>Factiva, Inc.</td><td>United States of America</td></tr>"
                "<tr><td>Beta Ltd</td><td>France</td></tr></table>")
        assert [s["name"] for s in parse_exhibit(html)] == ["Alpha Ltd", "Factiva, Inc.", "Beta Ltd"]

    def test_a_row_with_header_words_in_both_cells_is_still_a_row(self):
        # "Company" and "Incorporated" are header words; the row is a subsidiary
        html = ("<table><tr><td>Name</td><td>Jurisdiction</td></tr>"
                "<tr><td>Acme Company</td><td>Incorporated in Ruritania</td></tr>"
                "<tr><td>Beta Ltd</td><td>Ireland</td></tr></table>")
        assert [s["name"] for s in parse_exhibit(html)] == ["Acme Company", "Beta Ltd"]

    def test_a_second_header_row_inside_a_table_switches_the_columns(self):
        html = ("<table><tr><td>Subsidiary</td><td>Jurisdiction</td></tr>"
                "<tr><td>Alpha Ltd</td><td>Ireland</td></tr>"
                "<tr><td>Subsidiary of Alpha Ltd</td><td>Jurisdiction</td><td>Ownership</td></tr>"
                "<tr><td>Beta Ltd</td><td>Ireland</td><td>75%</td></tr></table>")
        by = {s["name"]: s for s in parse_exhibit(html)}
        assert set(by) == {"Alpha Ltd", "Beta Ltd"}
        assert by["Beta Ltd"]["stake_percent"] == 75.0

    def test_an_inherited_header_still_needs_real_jurisdictions(self):
        # a securities table after the subsidiary list must not inherit its columns
        html = ("<table><tr><td>Name</td><td>Jurisdiction</td><td>Ownership</td></tr>"
                "<tr><td>Alpha Ltd</td><td>Ireland</td><td>100%</td></tr></table>"
                "<table><tr><td>Common Stock</td><td>New York Stock Exchange</td><td>NYSE</td></tr>"
                "<tr><td>Notes due 2030</td><td>Nasdaq</td><td>NAS</td></tr></table>")
        assert [s["name"] for s in parse_exhibit(html)] == ["Alpha Ltd"]


class TestFlatListsStayFlat:
    def test_the_flat_lists_parse_as_before(self):
        subs = _load("apple_ex21.htm", "Apple Inc.")
        assert len(subs) == 19 and not any(s.get("parent") or s.get("parent_basis") for s in subs)
        subs = _load("texasroadhouse_ex21.htm", "Texas Roadhouse, Inc.")
        assert len(subs) == 60 and not any(s.get("parent") or s.get("parent_basis") for s in subs)
