"""Exhibit 21 (statutory subsidiary list): parser, jurisdiction mapping, and
the exhibit finder. The fixture is Apple's REAL Ex-21.1 from the 10-K filed
2025-10-31 (19 subsidiaries), captured verbatim — measured, not invented."""
from pathlib import Path
from unittest.mock import patch

from app.scraper.sec_ex21 import (annual_exhibit_candidates,
                                  jurisdiction_country, jurisdiction_subdivision,
                                  parse_exhibit)

FIXTURE = (Path(__file__).parent / "fixtures" / "apple_ex21.htm").read_text()
# The hostile one: a ZWSP-only filler column, jurisdiction fused with the
# legal form ("Kentucky limited liability company"), and section-header rows
# ("INDIRECTLY WHOLLY-OWNED SUBSIDIARIES"). Captured verbatim from the 10-K.
TXRH = (Path(__file__).parent / "fixtures" / "texasroadhouse_ex21.htm").read_text()


class TestParseExhibit:
    def test_parses_apples_real_exhibit_completely(self):
        subs = parse_exhibit(FIXTURE)
        assert len(subs) == 19
        by_name = {s["name"]: s["jurisdiction"] for s in subs}
        assert by_name["Apple Asia Limited"] == "Hong Kong"
        assert by_name["Braeburn Capital, Inc."] == "Nevada, U.S."
        assert by_name["iTunes K.K."] == "Japan"

    def test_headers_and_registrant_line_are_not_subsidiaries(self):
        names = {s["name"] for s in parse_exhibit(FIXTURE)}
        assert "Apple Inc." not in names          # the registrant's own line
        assert not any(n.lower().startswith(("jurisdiction", "subsidiaries"))
                       for n in names)

    def test_significance_asterisk_is_stripped_from_names(self):
        html = ("<table><tr><td>Acme GmbH*</td><td>Germany</td></tr></table>")
        assert parse_exhibit(html)[0]["name"] == "Acme GmbH"

    def test_prose_rows_and_duplicates_are_dropped(self):
        html = ("<table>"
                "<tr><td>Acme Ltd</td><td>Ireland</td></tr>"
                "<tr><td>Acme Ltd</td><td>Ireland</td></tr>"           # dupe
                "<tr><td>Note</td><td>" + "x" * 80 + "</td></tr>"      # prose
                "</table>")
        assert len(parse_exhibit(html)) == 1

    def test_texas_roadhouse_zwsp_column_and_section_headers(self):
        # 60 real subsidiaries: the U+200B filler column must vanish (it was
        # read as the jurisdiction of all 62 rows) and the two ALL-CAPS
        # section-header rows must not become companies.
        subs = parse_exhibit(TXRH)
        assert len(subs) == 60
        js = {s["jurisdiction"] for s in subs}
        assert "\u200b" not in js and "" not in js
        names = {s["name"] for s in subs}
        assert not any("SUBSIDIARIES" in n for n in names)

    def test_empty_page_parses_to_empty_not_crash(self):
        assert parse_exhibit("<html><body><p>nothing here</p></body></html>") == []


class TestJurisdictionCountry:
    def test_the_shapes_real_filers_use(self):
        # every jurisdiction in Apple's real exhibit must map — the fixture
        # pins the parser, this pins the mapper against the same reality
        for sub in parse_exhibit(FIXTURE):
            assert jurisdiction_country(sub["jurisdiction"]) is not None, sub

    def test_state_comma_us(self):
        assert jurisdiction_country("Delaware, U.S.") == "US"
        assert jurisdiction_country("Nevada, U.S.") == "US"

    def test_bare_state_name(self):
        assert jurisdiction_country("Delaware") == "US"     # Alphabet files this

    def test_united_states_spelled_out(self):
        assert jurisdiction_country("United States") == "US"  # Microsoft files this

    def test_country_names(self):
        assert jurisdiction_country("Ireland") == "IE"
        assert jurisdiction_country("Hong Kong") == "HK"
        assert jurisdiction_country("South Korea") == "KR"

    def test_the_whole_texas_roadhouse_band_maps(self):
        for sub in parse_exhibit(TXRH):
            assert jurisdiction_country(sub["jurisdiction"]) == "US", sub

    def test_fused_legal_forms_are_peeled(self):
        assert jurisdiction_country("Kentucky limited liability company") == "US"
        assert jurisdiction_country("Virginia corporation") == "US"
        assert jurisdiction_country("Arkansas non-profit corporation") == "US"

    def test_long_form_country_names(self):
        # Vera Bradley files "The People\u2019s Republic of China" — curly
        # apostrophe, leading article and all
        assert jurisdiction_country("The People\u2019s Republic of China") == "CN"
        assert jurisdiction_country("Russian Federation") == "RU"

    def test_unmappable_returns_none_never_guesses(self):
        assert jurisdiction_country("Grand Duchy of Ruritania") is None
        assert jurisdiction_country("") is None
        assert jurisdiction_country(None) is None


class TestJurisdictionSubdivision:
    """The finer grain the filing states, kept as ISO 3166-2 so the panel can
    show "Registered in: Florida" instead of only "United States". This is the
    fix for the reported bug: Diagnosys (Pinpoint) Inc. filed "Florida, USA"
    and showed no location beyond the country."""

    def test_us_states_in_the_forms_filers_use(self):
        assert jurisdiction_subdivision("Florida, USA") == "US-FL"
        assert jurisdiction_subdivision("Delaware") == "US-DE"
        assert jurisdiction_subdivision("Delaware, U.S.") == "US-DE"
        assert jurisdiction_subdivision("USA (Delaware)") == "US-DE"

    def test_canada_and_uk_and_offshore(self):
        assert jurisdiction_subdivision("Quebec, Canada") == "CA-QC"
        assert jurisdiction_subdivision("Canada (Ontario)") == "CA-ON"
        assert jurisdiction_subdivision("Toronto, Ontario, Canada") == "CA-ON"
        assert jurisdiction_subdivision("England") == "GB-ENG"
        assert jurisdiction_subdivision("Nevis") == "KN-N"
        assert jurisdiction_subdivision("Dubai") == "AE-DU"

    def test_a_plain_country_has_no_subdivision(self):
        assert jurisdiction_subdivision("Germany") is None
        assert jurisdiction_subdivision("Ireland") is None
        assert jurisdiction_subdivision("") is None
        assert jurisdiction_subdivision(None) is None

    def test_every_us_row_in_the_hostile_fixture_yields_a_state(self):
        # Texas Roadhouse: every "<State>, USA"-shaped row maps to a US-XX code
        for sub in parse_exhibit(TXRH):
            code = jurisdiction_subdivision(sub["jurisdiction"])
            assert code and code.startswith("US-"), sub


class TestAnnualExhibitCandidates:
    def _subs(self, forms):
        return {"filings": {"recent": {
            "form": [f for f, _, _ in forms],
            "accessionNumber": [a for _, a, _ in forms],
            "filingDate": [d for _, _, d in forms]}}}

    def test_finds_the_exhibit_in_the_newest_annual_filing(self):
        subs = self._subs([("8-K", "0000000000-25-000001", "2025-11-01"),
                           ("10-K", "0000320193-25-000079", "2025-10-31")])
        index = {"directory": {"item": [
            {"name": "a10-k.htm"}, {"name": "a10-kexhibit21109272025.htm"}]}}
        with patch("app.scraper.sec_ex21._get", side_effect=[subs, index]):
            cands = annual_exhibit_candidates("320193")
        assert cands and cands[0]["form"] == "10-K"
        assert cands[0]["url"].endswith("a10-kexhibit21109272025.htm")
        assert cands[0]["filing_date"] == "2025-10-31"

    def test_a_20f_prefers_ex8_over_ex21_lookalikes(self):
        # AB InBev's dex215.htm is exhibit 2.15, not 21.5 — on a 20-F the
        # ex-8 patterns must rank first so the real subsidiary exhibit (when
        # present) is tried before the ambiguous name.
        subs = self._subs([("20-F", "0000000000-25-000002", "2025-04-30")])
        index = {"directory": {"item": [{"name": "d1dex215.htm"},
                                        {"name": "d1dex81.htm"}]}}
        with patch("app.scraper.sec_ex21._get", side_effect=[subs, index]):
            cands = annual_exhibit_candidates("999999")
        assert [c["url"].rsplit("/", 1)[-1] for c in cands] == \
            ["d1dex81.htm", "d1dex215.htm"]

    def test_newest_annual_without_exhibit_means_none_not_an_older_year(self):
        subs = self._subs([("10-K", "0000000000-25-000003", "2025-10-31"),
                           ("10-K", "0000000000-24-000003", "2024-10-31")])
        index = {"directory": {"item": [{"name": "a10-k.htm"}]}}
        with patch("app.scraper.sec_ex21._get", side_effect=[subs, index]):
            assert annual_exhibit_candidates("888888") == []

    def test_a_stale_cik_404_is_absent_not_an_error(self):
        def boom(url):
            raise RuntimeError("404 Not Found")
        with patch("app.scraper.sec_ex21._get", side_effect=boom):
            assert annual_exhibit_candidates("777777") == []


class TestHeaderAwareTables:
    def test_bofa_location_vs_jurisdiction_columns(self):
        # Bank of America has BOTH columns; Location must lose.
        html = ("<table><tr><td></td><td>Name</td><td>Location</td><td>Jurisdiction</td></tr>"
                "<tr><td></td><td>BAC North America Holding Company</td>"
                "<td>Charlotte, NC</td><td>Delaware</td></tr></table>")
        subs = parse_exhibit(html)
        assert subs == [{"name": "BAC North America Holding Company",
                         "jurisdiction": "Delaware"}]

    def test_astronics_ownership_percentage_becomes_a_stake(self):
        html = ("<table><tr><td>Subsidiary</td><td>Ownership Percentage</td>"
                "<td>State (Province), Country of Incorporation</td></tr>"
                "<tr><td>Astronics Test Systems Inc.</td><td>100%</td>"
                "<td>Delaware, USA</td></tr></table>")
        subs = parse_exhibit(html)
        assert subs[0]["stake_percent"] == 100.0
        assert subs[0]["jurisdiction"] == "Delaware, USA"

    def test_tata_spacer_columns_and_serial_numbers(self):
        html = ("<table><tr><td>Sr.No.</td><td></td><td>Name of the Subsidiary Company</td>"
                "<td></td><td>Country ofincorporation</td></tr>"
                "<tr><td></td><td></td><td>(A) DIRECT SUBSIDIARIES</td><td></td><td></td></tr>"
                "<tr><td>1.</td><td></td><td>TML Business Services Limited</td>"
                "<td></td><td>India</td></tr></table>")
        subs = parse_exhibit(html)
        assert subs == [{"name": "TML Business Services Limited",
                         "jurisdiction": "India"}]

    def test_a_headerless_junk_table_is_rejected_by_the_sanity_gate(self):
        # AB InBev's securities listings: headerless (to this parser) rows
        # whose "jurisdictions" map to no country — the whole table drops.
        html = ("<table><tr><td>Ordinary shares</td><td>New York Stock Exchange</td></tr>"
                "<tr><td>4.000% Notes due 2043</td><td>BUD/43</td></tr></table>")
        assert parse_exhibit(html) == []

    def test_a_headerless_real_table_still_parses(self):
        html = ("<table><tr><td>Acme GmbH</td><td>Germany</td></tr>"
                "<tr><td>Acme Ltd</td><td>Ireland</td></tr></table>")
        assert len(parse_exhibit(html)) == 2
