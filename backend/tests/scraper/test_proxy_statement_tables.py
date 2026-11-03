"""The DEF 14A ownership table, read as a grid (findings item 47).

The old reader guessed by position and read "*" as 0.4: on Alphabet's 2026
proxy Pichai, Porat, Hennessy and Shriram got 0.4 %, and Robin L. Washington,
who holds no shares, 92.2 % (the group row's Class B column). The fixtures are
the real 2026 tables of ten issuers, trimmed to the tables (and one sentence
naming the share classes), attributes other than colspan/rowspan dropped."""
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.scraper import proxy_statement as ps

FIXTURES = Path(__file__).parent / "fixtures"


def _read(key):
    return ps.read_ownership_tables((FIXTURES / f"def14a_{key}_2026.html").read_text())


def _owner(read, name):
    hits = [o for o in read["owners"] if o["name"] == name]
    assert len(hits) == 1, (name, [o["name"] for o in read["owners"]])
    return hits[0]


def _voting(read):
    return {o["name"]: o["voting_power_pct"] for o in read["owners"] if o["voting_power_pct"] is not None}


class TestAlphabet:
    def test_the_founders_total_voting_power(self):
        assert _voting(_read("alphabet")) == {
            "Larry Page": 27.4, "Sergey Brin": 25.3, "L. John Doerr": 1.6, "BlackRock, Inc.": 2.5}

    def test_a_star_is_below_one_percent_not_a_number(self):
        pichai = _owner(_read("alphabet"), "Sundar Pichai")
        assert pichai == {"name": "Sundar Pichai", "voting_power_pct": None, "below_one_percent": True}

    def test_a_director_without_shares_does_not_get_the_group_total(self):
        assert _owner(_read("alphabet"), "Robin L. Washington")["voting_power_pct"] is None

    def test_no_section_heading_or_group_row_is_an_owner(self):
        names = [o["name"] for o in _read("alphabet")["owners"]]
        assert not any("group" in n.lower() or "officers" in n.lower() for n in names)
        assert len(names) == 15

    def test_it_is_multi_class_and_says_which_column(self):
        read = _read("alphabet")
        assert read["share_class_structure"] == "multi_class"
        assert read["basis"] == ["voting power: Total Voting Power (1) %"]


def test_meta_reads_the_voting_table_not_the_bonus_table():
    read = _read("meta")
    assert _voting(read) == {"Mark Zuckerberg": 60.8, "Entities affiliated with BlackRock": 2.8,
                             "Entities affiliated with FMR LLC": 2.4}
    assert _owner(read, "Susan Li")["below_one_percent"] is True
    assert len(read["basis"]) == 1


def test_berkshire_two_rows_per_person():
    read = _read("berkshire")
    assert _voting(read) == {"Warren E. Buffett": 30.2, "Susan A. Buffett": 0.1}
    assert _owner(read, "Gregory E. Abel")["below_one_percent"] is True
    assert len(read["owners"]) == 13


def test_tesla_one_class_so_percent_of_class_is_voting_power():
    read = _read("tesla")
    assert read["share_class_structure"] == "single_class"
    assert _voting(read) == {"Elon Musk": 19.8, "Vanguard Capital Management": 5.3}


def test_tesla_lists_musk_twice_and_he_is_kept_once():
    assert [o["name"] for o in _read("tesla")["owners"]].count("Elon Musk") == 1


def test_apple():
    assert _voting(_read("apple")) == {"The Vanguard Group": 9.63, "BlackRock, Inc.": 7.1}


def test_amazon_street_address_is_not_part_of_the_name():
    assert _voting(_read("amazon")) == {"Jeffrey P. Bezos": 8.8, "The Vanguard Group, Inc.": 7.2,
                                        "BlackRock, Inc.": 5.9}


def test_microsoft_both_tables_and_the_name_column_is_name_not_beneficial_ownership():
    read = _read("microsoft")
    assert _voting(read) == {"Vanguard Capital Management LLC": 7.48, "BlackRock, Inc.": 7.3}
    assert _owner(read, "John Collison")["below_one_percent"] is True
    assert len(read["basis"]) == 2


@pytest.mark.parametrize("key", ["ford", "nike", "comcast"])
def test_a_multi_class_filing_without_a_voting_column_states_no_voting_power(key):
    # Vanguard's 11.68 % of Ford's common stock is not 11.68 % of Ford's votes
    read = _read(key)
    assert read["share_class_structure"] == "multi_class" and read["owners"] == []


@pytest.mark.parametrize("text,expected", [
    ("27.4", (27.4, False)), ("19.8 %", (19.8, False)), ("30.2 (2)", (30.2, False)),
    ("7.48%", (7.48, False)), ("33 1/3%", (33.33, False)), ("*", (None, True)),
    ("<1%", (None, True)), ("—", (None, False)), ("", (None, False)), (None, (None, False)),
    ("150", (None, False)), ("n/a", (None, False)),
])
def test_parse_pct(text, expected):
    assert ps._parse_pct(text) == expected


SYNTHETIC = """<table>
<tr><td>Name of Beneficial Owner</td><td>Class A Voting Power</td><td>Total Voting Power</td></tr>
<tr><td rowspan="2">Jane Founder</td><td>12.0</td><td>40.5</td></tr>
<tr><td>3.0</td></tr>
<tr><td>John Holder</td><td>9.0</td><td>*</td></tr>
</table>"""


def test_a_single_class_called_class_a_still_reads_its_percent():
    # one voting class that happens to be named "Class A": its percent is the vote
    html = """<table>
<tr><td>Name of Beneficial Owner</td><td>Shares of Class A Common Stock Beneficially Owned</td><td>Percent of Class</td></tr>
<tr><td>Jane Holder</td><td>1,000</td><td>6.2%</td></tr></table>"""
    assert _voting(ps.read_ownership_tables(html)) == {"Jane Holder": 6.2}


def test_the_total_voting_column_wins_over_a_class_one():
    assert _voting(ps.read_ownership_tables(SYNTHETIC)) == {"Jane Founder": 40.5}


def test_a_rowspan_name_does_not_shift_the_next_row():
    grid = ps._grid(__import__("bs4").BeautifulSoup(SYNTHETIC, "lxml").find("table"))
    assert grid[2] == [(1, 2, "3.0")]


class TestFindingTheFiling:
    SUBS = {"filings": {"recent": {"form": ["DEFA14A", "DEF 14A"],
                                   "accessionNumber": ["0001-26-1", "0001104659-26-041026"],
                                   "primaryDocument": ["a.htm", "tm261382-1_def14a.htm"],
                                   "filingDate": ["2026-04-10", "2026-04-09"]}}}

    def test_the_name_goes_through_the_sec_company_search(self):
        with patch("app.scraper.sec_edgar.search_company",
                   return_value={"cik": "0001018724", "name": "AMAZON COM INC"}) as search, \
             patch.object(ps, "_get_json", return_value=self.SUBS) as get:
            found = ps.find_latest_def14a("Amazon.com")
        search.assert_called_once_with("Amazon.com")
        assert get.call_args.args[0].endswith("/CIK0001018724.json")
        assert found == ("0001018724", "000110465926041026", "tm261382-1_def14a.htm", "2026-04-09")

    def test_a_held_cik_skips_the_search(self):
        with patch("app.scraper.sec_edgar.search_company") as search, \
             patch.object(ps, "_get_json", return_value=self.SUBS):
            assert ps.find_latest_def14a("anything", cik="1018724")[0] == "0001018724"
        search.assert_not_called()

    def test_an_unknown_company_or_no_proxy_is_none(self):
        with patch("app.scraper.sec_edgar.search_company", return_value=None):
            assert ps.find_latest_def14a("Nobody") is None
        with patch.object(ps, "_get_json", return_value={"filings": {"recent": {}}}):
            assert ps.find_latest_def14a("x", cik="1") is None

    def test_a_missing_submissions_file_is_none(self):
        req = httpx.Request("GET", "https://data.sec.gov/x")
        err = httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        with patch.object(ps, "_get_json", side_effect=err):
            assert ps.find_latest_def14a("x", cik="1") is None


def test_fetch_notes_a_multi_class_filing_without_voting_power():
    html = (FIXTURES / "def14a_ford_2026.html").read_text()
    with patch.object(ps, "find_latest_def14a",
                      return_value=("0000037996", "000155278126000164", "f.htm", "2026-03-27")), \
         patch.object(ps, "_get", return_value=httpx.Response(200, text=html)):
        out = ps.fetch_proxy_ownership("Ford Motor")
    assert out["owners"] == [] and out["note"].startswith("The filing states no voting power")
    assert out["filing_url"].endswith("/37996/000155278126000164/f.htm")
