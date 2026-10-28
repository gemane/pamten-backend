"""Several companies in one table row, one paragraph each (2026-10-28).

HubSpot's Exhibit 21.1 puts ten subsidiaries in ONE row: the name cell holds
ten <p>s, the jurisdiction cell ten more, paired by position. The cell's text
was joined with no separator, so the row read as one subsidiary "HubSpot
Australia Pty LtdHubSpot BelgiumHubSpot Canada Inc." in "AustraliaBelgiumCanada"
and 14 of its 15 were lost; NexPoint's glued names reached dev as nodes, and
Cellectis' list (header and both subsidiaries in one row) read nothing.

A header printed over two lines has the same shape — Herc's and JPMorgan's —
and must stay one row; so must a name wrapped over two lines beside one place.
Fixtures are the filers' own exhibits.
"""
from pathlib import Path

from app.scraper.sec_ex21 import _split_paragraph_row, parse_exhibit

FX = Path(__file__).parent / "fixtures"


def _names(name, registrant=None, form=None):
    return [(s["name"], s["jurisdiction"])
            for s in parse_exhibit((FX / name).read_text(), registrant, form)]


class TestOneCompanyPerParagraph:
    def test_hubspot_reads_all_fifteen(self):
        subs = _names("hubspot_ex21.htm", "HubSpot, Inc.")
        assert len(subs) == 15
        assert ("HubSpot Australia Pty Ltd", "Australia") in subs
        assert ("HubSpot Latin America S.A.S.", "Colombia") in subs
        assert ("HubSpot UK Holdings Limited", "United Kingdom") in subs
        assert not any("HubSpot Belgium" in n and n != "HubSpot Belgium" for n, _ in subs)

    def test_a_place_that_starts_with_state_is_kept(self):
        # "State of Delaware, United States" starts like the header word
        # "State"; the row was dropped as noise
        assert ("APIHub, Inc. d/b/a Clearbit", "State of Delaware, United States") \
            in _names("hubspot_ex21.htm", "HubSpot, Inc.")

    def test_nexpoint_names_are_no_longer_glued(self):
        subs = _names("nexpoint_ex21_excerpt.htm")
        assert ("HRTBH Timber Creek, LLC", "Delaware") in subs
        assert ("Landmark at West Place, LLC", "Delaware") in subs
        assert ("LAT Briley Parkway, LLC", "Delaware") in subs
        assert ("NXRT Verandas, LLC", "Delaware") in subs
        assert len(subs) == 6

    def test_the_header_in_the_same_row_as_the_companies(self):
        # Cellectis: "Name of Subsidiary / Cellectis, Inc. / Cellectis Biologics, Inc."
        assert _names("cellectis_ex81.htm", "Cellectis S.A.", "20-F") == [
            ("Cellectis, Inc.", "Delaware"), ("Cellectis Biologics, Inc.", "Delaware")]

    def test_a_split_row_inside_an_ordinary_list(self):
        assert _names("esg_ex81.htm", None, "20-F")[2:] == [
            ("Environmental Solutions (Asia) Pte. Ltd.", "Singapore"),
            ("ESG Chemicals Sdn. Bhd.", "Malaysia")]


class TestKeptAsOneRow:
    def test_a_two_line_header_is_not_split(self):
        # Herc: "SUBSIDIARIES OF HERC HOLDINGS INC. / As of December 31, 2025"
        # beside "JURISDICTION OF / INCORPORATION"
        subs = _names("herc_ex21.htm", "Herc Holdings Inc.")
        assert len(subs) == 25
        assert not any(n.startswith("As of") or j == "INCORPORATION" for n, j in subs)

    def test_a_two_line_header_with_a_date_is_not_split(self):
        # JPMorgan: "December 31, 2025 / Name" beside "Organized Under / The Laws Of"
        subs = _names("jpmorgan_ex21.htm", "JPMorgan Chase & Co.")
        assert len(subs) == 18
        assert subs[0][0] == "JPMorgan Chase Bank, National Association"

    def test_a_wrapped_name_beside_one_place_is_one_company(self):
        html = ("<table><tr><td>Name</td><td>Jurisdiction of Organization</td></tr>"
                "<tr><td><p>Chubb Fianzas Monterrey, Aseguradora de</p><p>Caución, S.A.</p></td>"
                "<td><p>Mexico</p></td></tr>"
                "<tr><td>ABA Garantías S.A. de C.V.</td><td>Mexico</td></tr></table>")
        assert [s["name"] for s in parse_exhibit(html)] == [
            "Chubb Fianzas Monterrey, Aseguradora de Caución, S.A.", "ABA Garantías S.A. de C.V."]

    def test_paragraphs_are_joined_with_a_space(self):
        # Chubb's "USA" / "(Pennsylvania)" read "USA(Pennsylvania)"
        html = ("<table><tr><td>Name</td><td>Jurisdiction of Organization</td></tr>"
                "<tr><td>Penn Millers Agency, Inc.</td><td><p>USA</p><p>(Pennsylvania)</p></td></tr>"
                "<tr><td>Chubb &amp; Son Inc.</td><td>USA<br>(New York)</td></tr></table>")
        assert [s["jurisdiction"] for s in parse_exhibit(html)] == [
            "USA (Pennsylvania)", "USA (New York)"]


class TestSplitRule:
    def test_equal_counts_with_places_split(self):
        assert _split_paragraph_row([["A Ltd", "B GmbH"], [], ["Ireland", "Germany"]]) == [
            ["A Ltd", "", "Ireland"], ["B GmbH", "", "Germany"]]

    def test_unequal_counts_do_not_split(self):
        assert _split_paragraph_row([["A Ltd", "B GmbH"], ["Ireland"]]) is None
        assert _split_paragraph_row([["A Ltd"], ["99%", "1% (B Ltd)"]]) is None

    def test_a_cell_with_one_paragraph_among_split_cells_does_not_split(self):
        assert _split_paragraph_row([["A Ltd", "B GmbH"], ["Ireland", "Germany"], ["100%"]]) is None

    def test_paragraphs_that_are_not_places_do_not_split(self):
        assert _split_paragraph_row([["Subsidiaries of X", "As of 2025"],
                                     ["JURISDICTION OF", "INCORPORATION"]]) is None

    def test_the_first_paragraph_may_be_the_header(self):
        assert _split_paragraph_row([["Name", "A Ltd"], ["Jurisdiction", "Ireland"]]) == [
            ["Name", "Jurisdiction"], ["A Ltd", "Ireland"]]

    def test_one_paragraph_is_no_split(self):
        assert _split_paragraph_row([["A Ltd"], ["Ireland"]]) is None
