"""Percentages an Exhibit 21 states, wherever the filer puts them.

Fixtures are verbatim exhibits (or trimmed excerpts) as filed on EDGAR, picked
from a 62-filer sample: Chubb (every stake stated, co-holders named, one list
split over eleven printed pages with the header on the first only), Lincoln
National (a bare-number ownership column under a blank header row), Eversource
(a header naming only the jurisdiction column, two-letter state codes — 6 of 40
rows used to survive), NYT (a stake inline in the name).

The filer's LAYOUT — indentation, section headings — is deliberately not read
as group structure; these tests pin that the parser stays flat.
"""
from pathlib import Path

import pytest

from app.scraper.sec_ex21 import (_clean_name, jurisdiction_country,
                                  jurisdiction_subdivision, parse_exhibit)

FIX = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return parse_exhibit((FIX / name).read_text(errors="replace"))


class TestOwnershipColumns:
    def test_chubb_keeps_its_ownership_column_past_the_first_page(self):
        subs = _load("chubb_ex21_excerpt.htm")
        by = {s["name"]: s for s in subs}
        # page 2 has no header row of its own — the column is inherited
        assert by["ABR Reinsurance Capital Holdings Ltd."]["stake_percent"] == 19.1483
        assert by["Oasis Investments 2 Ltd."]["stake_percent"] == 66.66
        # everything but the filer's own "Publicly held" line carries a stake
        assert len(subs) == 47
        assert sum(1 for s in subs if "stake_percent" in s) == 46

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


class TestLayoutIsNotStructure:
    def test_indentation_and_headings_produce_no_parents(self):
        html = ("<p>Subsidiaries of Holding Co.:</p>"
                "<table><tr><td>Holding Co.</td><td>Ireland</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;Child Ltd</td><td>Ireland</td></tr>"
                "<tr><td style=\"padding-left:20pt\">Grandchild Ltd</td><td>Ireland</td></tr></table>")
        subs = parse_exhibit(html)
        assert [s["name"] for s in subs] == ["Holding Co.", "Child Ltd", "Grandchild Ltd"]
        assert not any("parent" in s for s in subs)

    def test_the_flat_lists_parse_as_before(self):
        assert len(_load("apple_ex21.htm")) == 19
        assert len(_load("texasroadhouse_ex21.htm")) == 60
