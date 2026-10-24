"""The bundle builder's pure rules (scripts/build_industry_bundle.py): the
code shapes, the SEC group codes, the UK fifth digit and the majority
descent that settles a US code's NACE node. The spreadsheet readers need
xlrd/openpyxl and run only when the bundle is regenerated."""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_industry_bundle.py"


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("build_industry_bundle", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestNaceCode:
    def test_eurostat_keys_become_printed_codes(self, builder):
        assert builder.nace_code("C") == "C"
        assert builder.nace_code("C23") == "23"
        assert builder.nace_code("C233") == "23.3"
        assert builder.nace_code("C2332") == "23.32"

    def test_aggregates_are_not_codes(self, builder):
        assert builder.nace_code("TOTAL") is None
        assert builder.nace_code("A_B") is None
        assert builder.nace_code("A01_A03_FOOD") is None


class TestSecGroupCodes:
    def test_a_listed_code_is_itself(self, builder):
        assert builder.expand_sec_code("3721", {"3721", "3724"}) == {"3721"}

    def test_a_rounded_code_is_every_industry_under_its_prefix(self, builder):
        sic = {"3251", "3253", "3255", "3259", "3261", "3721"}
        assert builder.expand_sec_code("3250", sic) == {"3251", "3253", "3255", "3259"}
        assert builder.expand_sec_code("3200", sic) == {"3251", "3253", "3255", "3259", "3261"}

    def test_nothing_under_an_all_zero_code(self, builder):
        assert builder.expand_sec_code("0000", {"0111"}) == set()


class TestMajorityNode:
    SECTION = {"56": "I", "90": "R", "29": "C", "30": "C", "58": "J", "18": "C"}

    def test_descends_while_one_node_holds_more_than_half(self, builder):
        # eating places: six restaurant classes and a dinner theater
        w = {"56.10": 3, "56.21": 1, "56.29": 1, "56.30": 1, "90.01": 1}
        assert builder.majority_node(w, self.SECTION) == "56"

    def test_down_to_the_class_when_it_dominates(self, builder):
        w = {"29.10": 3, "29.20": 1, "30.40": 1}
        assert builder.majority_node(w, self.SECTION) == "29.10"

    def test_a_tie_descends_nowhere(self, builder):
        assert builder.majority_node({"58.29": 1, "18.20": 1}, self.SECTION) is None

    def test_exactly_half_is_not_a_majority(self, builder):
        assert builder.majority_node({"56.10": 1, "90.01": 1}, self.SECTION) is None

    def test_empty(self, builder):
        assert builder.majority_node({}, self.SECTION) is None


class TestUkSic:
    NACE = {"23.32": {}, "23.3": {}, "23": {}, "74.9": {}, "74": {}, "98": {}, "99": {}}

    def test_the_fifth_digit_is_dropped(self, builder):
        out = builder.map_uk_sic({"23320": "Bricks"}, self.NACE)
        assert out["23320"] == {"label": "Bricks", "nace": "23.32"}

    def test_a_uk_regrouping_falls_back_to_the_group(self, builder):
        assert builder.map_uk_sic({"23390": "x"}, self.NACE)["23390"]["nace"] == "23.3"

    def test_the_uk_only_codes_map_to_nothing(self, builder):
        out = builder.map_uk_sic({"74990": "Non-trading company", "98000": "Residents property management",
                                  "99999": "Dormant Company"}, self.NACE)
        assert all(v["nace"] is None for v in out.values())
        assert out["99999"]["label"] == "Dormant Company"


class TestHtmlLists:
    def test_sec_rows(self, builder, tmp_path):
        p = tmp_path / "sec.html"
        p.write_text("<table><tr><th>SIC</th><th>Office</th><th>Title</th></tr>"
                     "<tr><td><a href='x?SIC=100'>100</a></td><td>Industrial Applications and Services</td>"
                     "<td>AGRICULTURAL PRODUCTION-CROPS</td></tr>"
                     "<tr><td>3250</td><td>Office of Manufacturing</td><td>STRUCTURAL CLAY PRODUCTS</td></tr></table>")
        out = builder.read_sec_sic(p)
        assert out["0100"]["label"] == "AGRICULTURAL PRODUCTION-CROPS"
        assert out["3250"]["office"] == "Office of Manufacturing"

    def test_companies_house_rows(self, builder, tmp_path):
        p = tmp_path / "ch.html"
        p.write_text("<table><tr><th>Code</th><th>Description</th></tr>"
                     "<tr><td>01110</td><td>Growing of cereals (except rice), leguminous crops and oil seeds </td></tr></table>")
        assert builder.read_uk_sic(p) == {"01110": "Growing of cereals (except rice), leguminous crops and oil seeds"}
