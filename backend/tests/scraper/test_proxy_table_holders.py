"""The holders an issuer's own ownership table adds (findings item 86): the
reader's classes, record date and names, and the writer's rules that need no
database. The fixtures are cut from the real filings: Skydance's 10-K/A of
2026-04-24 (Harbor Lights Entertainment, 100 % of Class A, files no 13D/G),
Molson Coors' and Better Home & Finance's 2026 proxies."""
from pathlib import Path
from unittest.mock import patch

import pytest

from app.scraper.proxy_statement import (holder_names, holder_stake, read_holder_rows, read_holder_table,
                                         table_as_of)

FIXTURES = Path(__file__).parent / "fixtures" / "proxy_table"


def _table(name: str) -> dict:
    return read_holder_table((FIXTURES / name).read_text())


def _big(table: dict) -> dict:
    return {r["name"]: r for r in table["rows"] if (r["stake_pct"] or 0) >= 5}


# ── the reader ──────────────────────────────────────────────────────────────

def test_skydance_10ka_names_harbor_lights_with_all_of_class_a():
    """Its header row "Name of Beneficial Owner | Shares | % | Shares | %" was
    taken for the first data row ("%" looked like a figure), so the table read
    as no table at all."""
    table = _table("skydance_10ka_2026.html")
    big = _big(table)
    assert table["as_of"] == "2026-02-20"
    assert big["Harbor Lights Entertainment, Inc."]["stake_pct"] == 100.0
    assert big["Harbor Lights Entertainment, Inc."]["share_class"] == "Class A"
    assert big["David Ellison"]["share_class"] == "Class A"       # the largest of his two classes
    assert big["Gerald Cardinale"] == {**big["Gerald Cardinale"], "stake_pct": 13.32, "share_class": "Class B"}


def test_the_absence_reader_sees_the_skydance_table_too():
    names = [r["name"] for r in read_holder_rows((FIXTURES / "skydance_10ka_2026.html").read_text())]
    assert "Harbor Lights Entertainment, Inc." in names


def test_molson_coors_class_comes_from_the_shares_column_left_of_the_percent():
    """"Class A Shares | Percent of class (%)": the percent header names no class."""
    table = _table("molson_coors_def14a_2026.html")
    big = _big(table)
    assert big["Pentland Securities Inc."]["share_class"] == "Class A"
    assert big["The Vanguard Group"]["share_class"] == "Class B"
    assert table["as_of"] is None                   # the proxy states no date near the table


def test_better_home_takes_the_largest_class_and_marks_the_group_rows():
    big = _big(_table("better_home_def14a_2026.html"))
    assert (big["Vishal Garg"]["stake_pct"], big["Vishal Garg"]["share_class"]) == (40.4, "Class B")
    assert big["BHFHC Distribution Trust"]["share_class"] == "Class C"
    spencer = next(r for r in big.values() if "Spencer" in r["name"])
    assert spencer["names"] == ["Vance Spencer"] and spencer["group"] is True
    assert big["Vishal Garg"]["group"] is False


@pytest.mark.parametrize("cell, names", [
    ("GAMCO Investors, Inc. One Corporate Center Rye, NY 10580-1435", ["GAMCO Investors, Inc."]),
    ("MUFG 4-5, Marunouchi 1-chome Chiyoda-ku, Tokyo 100-8330, Japan", ["MUFG"]),
    ("Warren E. Buffett/Berkshire Hathaway Inc.", ["Warren E. Buffett", "Berkshire Hathaway Inc."]),
    ("Entities affiliated with BlackRock, Inc. (3)", ["BlackRock, Inc."]),
    ("Entities and Persons Affiliated with Vance Spencer", ["Vance Spencer"]),
    ("Warren E. Buffett and affiliated entities", ["Warren E. Buffett"]),
    ("SB Energy Global Holdings One Ltd.", ["SB Energy Global Holdings One Ltd."]),
    ("4280661 Canada Inc.", ["4280661 Canada Inc."]),
])
def test_holder_names(cell, names):
    assert holder_names(cell) == names


@pytest.mark.parametrize("header, before, day", [
    ("Shares Beneficially Owned as of April 2, 2026", "", "2026-04-02"),
    ("Name", "the following table sets forth, as of March 1, 2026, the beneficial", "2026-03-01"),
    ("Name", "as of Feb. 27 2026", "2026-02-27"),
    ("Name", "as of the record date", None),
])
def test_table_as_of(header, before, day):
    assert table_as_of(header, before) == day


def test_holder_stake_keeps_the_voting_power_apart():
    stake = holder_stake([("Percent of Series (%)", 7.8), ("Voting Power (%)", 3.9)])
    assert stake == {"stake_pct": 7.8, "share_class": None, "voting_power_pct": 3.9}
    assert holder_stake([("% of Class A Common Stock", 13.2), ("% of Shares of Class B Common Stock", 40.4)]) == \
        {"stake_pct": 40.4, "share_class": "Class B", "voting_power_pct": None}


# ── the writer's rules ──────────────────────────────────────────────────────

def test_a_block_several_rows_report_is_written_on_its_one_company():
    from app.scraper.sec_writer import _shared_blocks
    rows = [r for r in _table("skydance_10ka_2026.html")["rows"] if (r["stake_pct"] or 0) >= 5]
    assert _shared_blocks(rows) == {"David Ellison"}           # Harbor Lights holds it


def test_a_block_several_companies_share_is_written_on_none():
    from app.scraper.sec_writer import _shared_blocks
    rows = [r for r in _table("molson_coors_def14a_2026.html")["rows"] if (r["stake_pct"] or 0) >= 5]
    assert _shared_blocks(rows) == {"Adolph Coors Company LLC", "Adolph Coors Jr. Trust",
                                    "Pentland Securities Inc.", "4280661 Canada Inc."}


def test_equal_stakes_that_fit_in_the_class_are_two_holders():
    from app.scraper.sec_writer import _shared_blocks
    rows = [{"name": "A Inc.", "names": ["A Inc."], "stake_pct": 30.0, "share_class": None},
            {"name": "B Inc.", "names": ["B Inc."], "stake_pct": 30.0, "share_class": None}]
    assert _shared_blocks(rows) == set()


def test_a_registered_fund_is_recognised_by_what_it_files():
    from app.scraper.sec_writer import is_registered_fund
    assert is_registered_fund({"filings": {"recent": {"form": ["DEF 14A", "N-CSR", "485BPOS"]}}})
    assert not is_registered_fund({"filings": {"recent": {"form": ["DEF 14A", "10-K", "8-K"]}}})


def test_a_funds_table_writes_nothing():
    from app.scraper.sec_writer import write_table_holders
    with patch("app.db.arcadedb.run_sql") as sql:
        out = write_table_holders("fund", {"form": "DEF 14A", "date": "2025-09-09", "url": "u"},
                                  {"as_of": None, "rows": [{"name": "Individual Investor", "names": ["Individual Investor"],
                                                            "stake_pct": 46.2}]}, "sec-edgar",
                                  {"filings": {"recent": {"form": ["N-CSR"]}}})
    assert out["fund"] is True and out["written"] == 0
    sql.assert_not_called()


@pytest.mark.parametrize("claims, stated", [
    ([{"source_id": "s", "filing_type": "DEF 14A", "stake_percent": 100.0}], True),
    ([{"source_id": "s", "filing_type": "10-K/A", "stake_percent": 13.32}], True),
    # the voting-power claim proxy_write records states no stake
    ([{"source_id": "s", "filing_type": "DEF 14A", "stake_percent": None, "voting_power_pct": 40.0}], False),
    ([{"source_id": "s", "filing_type": "DEF 14A", "stake_percent": 9.0, "until": "2026-04-01"}], False),
    ([{"source_id": "s", "filing_type": "Form 4", "stake_percent": 9.0}], False),
])
def test_the_form4_reader_yields_to_a_table_stake(claims, stated):
    from app.scraper.sec_writer import stated_by_13dg
    with patch("app.scraper.sec_writer.claims_for", return_value=claims):
        assert stated_by_13dg("o", "i", "s") is stated


def test_a_10ka_table_claim_shares_the_proxy_family():
    from app.claims import filing_family
    assert filing_family("10-K/A") == filing_family("DEF 14A") == "def14a"
    assert filing_family("10-K XBRL") != "def14a"


def test_the_writer_knows_every_form_the_reader_reads():
    from app.scraper.proxy_statement import TABLE_FORMS
    from app.scraper.sec_writer import TABLE_FILING_TYPES
    assert set(TABLE_FORMS) == TABLE_FILING_TYPES


@pytest.mark.parametrize("header, cls", [
    # "class of" is the column's title, not a class (LENSAR, Forward Air read "Class OF")
    ("Title or Class of Securities Series A Convertible Preferred Stock (2) Percentage of", "Series A"),
    ("Class of Shares or Units Percent of Class", None),
    # a number after "class" is a footnote mark (Baxter, Equifax, Quantum read "Class 1")
    ("PERCENT OF CLASS 1 (%)", None),
    ("Beneficial Ownership of Common Stock % of Class 1", None),
    ("Percent of Class 2", None),
    ("% of Class 1 Common Stock", "Class 1"),              # a numbered class names its security
    ("% of Class A Common Stock", "Class A"),
    ("Percent of class (%)", None),
])
def test_a_header_word_after_class_is_not_always_a_class(header, cls):
    assert holder_stake([(header, 7.0)])["share_class"] == cls
