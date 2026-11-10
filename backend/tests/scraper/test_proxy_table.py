"""Every holder the issuer's own ownership table names (findings item 99).

Fixtures: the ownership tables of the real 2026 proxies of Sirius XM, Occidental
and DaVita (the tables only, cut from the filings)."""
import pathlib
from datetime import date

import pytest

from app.scraper import proxy_statement as ps
from app.scraper.sec_writer import named_in_table

FIX = pathlib.Path(__file__).parent / "fixtures" / "proxy_table"


def _rows(name):
    return ps.read_holder_rows((FIX / f"{name}.html").read_text())


@pytest.mark.parametrize("cell, name", [
    ("BlackRock, Inc. 3", "BlackRock, Inc."),
    ("D. James Bidzos (5 )", "D. James Bidzos"),
    ("Kathleen A. Waters 5", "Kathleen A. Waters"),
    ("Warren E. Buffett and affiliated entities", "Warren E. Buffett"),
    ("John C. Malone c/o Liberty Media Corporation", "John C. Malone"),
    ("The Vanguard Group (2)", "The Vanguard Group"),
    ("Dodge & Cox", "Dodge & Cox"),
    ("3M Company", "3M Company"),
])
def test_holder_name(cell, name):
    assert ps.holder_name(cell) == name


class TestReadHolderRows:
    def test_sirius_xm_names_berkshire_vanguard_and_malone_as_its_5_percent_holders(self):
        rows = _rows("sirius_xm_def14a_2026")
        big = {r["name"]: max(r["percents"]) for r in rows if any(p >= 5 for p in r["percents"])}
        assert big == {"Berkshire Hathaway Inc.": 37.1, "The Vanguard Group": 5.4, "John C. Malone": 6.6}
        assert "Gregory B. Maffei" in [r["name"] for r in rows]          # the directors are listed too

    def test_occidental_lists_the_group_by_its_controlling_person(self):
        rows = _rows("occidental_def14a_2026")
        assert rows[0] == {"name": "Warren E. Buffett", "percents": [32.43]}
        assert not any("group" in r["name"].lower() and "vanguard" not in r["name"].lower() for r in rows)

    def test_davita_strips_the_footnote_number(self):
        names = [r["name"] for r in _rows("davita_def14a_2026")]
        assert names[:2] == ["Warren E. Buffett", "BlackRock, Inc."]

    def test_no_table_no_rows(self):
        assert ps.read_holder_rows("<html><table><tr><td>Revenue</td><td>12</td></tr></table></html>") == []


def _subs(rows, cik="908937"):
    return {"cik": cik, "filings": {"recent": {
        "form": [r[0] for r in rows], "filingDate": [r[1] for r in rows],
        "accessionNumber": [r[2] for r in rows], "primaryDocument": [r[3] for r in rows]}}}


class TestLatestTableFiling:
    TODAY = date(2026, 11, 10)

    def test_the_newest_proxy_within_18_months(self):
        subs = _subs([("10-Q", "2026-10-29", "1-26-1", "q.htm"),
                      ("DEF 14A", "2026-04-10", "0001104659-26-041777", "tm261591-1_def14a.htm"),
                      ("DEF 14A", "2025-04-11", "2-25-1", "old.htm")])
        assert ps.latest_table_filing(subs, self.TODAY) == {
            "form": "DEF 14A", "date": "2026-04-10",
            "url": "https://www.sec.gov/Archives/edgar/data/908937/000110465926041777/tm261591-1_def14a.htm"}

    def test_a_10ka_part_iii_stands_in(self):
        subs = _subs([("10-K/A", "2026-04-24", "0001140361-26-016758", "ef.htm")], cik="0002041610")
        assert ps.latest_table_filing(subs, self.TODAY)["form"] == "10-K/A"

    def test_too_old_or_none(self):
        assert ps.latest_table_filing(_subs([("DEF 14A", "2025-04-01", "1", "d.htm")]), self.TODAY) is None
        assert ps.latest_table_filing(_subs([("DEFA14A", "2026-04-01", "1", "d.htm")]), self.TODAY) is None
        assert ps.latest_table_filing({}, self.TODAY) is None


TABLE = ["Berkshire Hathaway Inc.", "The Vanguard Group", "John C. Malone", "Warren E. Buffett",
         "Gregory B. Maffei", "Dodge & Cox"]


@pytest.mark.parametrize("aliases, expected", [
    (["BERKSHIRE HATHAWAY INC"], True),
    (["Warren Buffett"], True),
    (["BUFFETT WARREN E"], True),                         # EDGAR's reversed spelling
    (["VANGUARD GROUP INC"], True),
    (["Vanguard Capital Management LLC"], True),          # generous: a shared distinctive word
    (["DODGE & COX"], True),
    (["Apollo Investment Fund Iv Lp"], False),
    (["Starz Acquisition LLC", "Old QVC Group, Inc."], False),
    (["Loral Space & Communications Ltd"], False),
    (["Cna Financial Corp"], False),
    (["Capital Research Global Investors"], False),       # only stop words in common with nothing
    (["Liberty Media Corp"], False),                     # "c/o Liberty Media" was cut from Malone's row
    ([], False), ([""], False),
])
def test_named_in_table(aliases, expected):
    assert named_in_table(aliases, TABLE) is expected


def test_a_cell_naming_two_holders_matches_either():
    # Bank of America's 2026 proxy: one row for the group
    table = ["Warren E. Buffett/Berkshire Hathaway Inc.", "The Vanguard Group"]
    assert named_in_table(["BERKSHIRE HATHAWAY INC"], table)
    assert named_in_table(["BUFFETT WARREN E"], table)


def test_a_family_group_matches_the_familys_listed_member():
    # Lennar: the Miller family's 13D group (MP Alpha, the Miller Charitable Fund)
    # and the table's "Stuart Miller" (70.2 % of the votes)
    persons = ["MP ALPHA HOLDINGS LLLP", "THE MILLER CHARITABLE FUND, LLLP", "LMM FAMILY CORP"]
    assert named_in_table(persons, ["Stuart Miller", "The Vanguard Group", "BlackRock, Inc."])
