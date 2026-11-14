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


# ── a preferred class beside the common stock (findings item 116) ───────────

def test_a_preferred_class_beside_common_stock_is_not_the_stake():
    """QXO's 2026 proxy: Jacobs Private Equity II holds 35.7 % of the common —
    which counts the shares its preferred converts into — and 90.0 % of the
    Convertible Preferred. The 90 was stored, with no class, and summed with
    Vanguard's 7.8 to 102.3 %."""
    big = _big(_table("qxo_def14a_2026.html"))
    assert big["Jacobs Private Equity II, LLC"]["stake_pct"] == 35.7
    assert big["Jacobs Private Equity II, LLC"]["share_class"] == "Common Stock"
    assert big["The Vanguard Group"] == {**big["The Vanguard Group"], "stake_pct": 7.8,
                                         "share_class": "Common Stock"}


def test_lensar_north_run_holds_its_common_percent_not_all_of_the_series_a():
    big = _big(_table("lensar_def14a_2026.html"))
    assert big["North Run Capital, LP"]["stake_pct"] == 54.7
    assert big["North Run Capital, LP"]["share_class"] == "Common Stock"
    assert big["BlackRock, Inc."]["share_class"] == "Common Stock"


def test_a_row_that_states_a_preferred_only_keeps_it_with_its_class():
    """Security Benefit Life holds 100 % of Franklin BSP's Series H and no
    common: the preferred is all the row states."""
    preferred = "Series H Convertible Preferred Stock Percent of Class"
    assert holder_stake([(preferred, 100.0)], several_classes=True) == \
        {"stake_pct": 100.0, "share_class": "Series H", "voting_power_pct": None}
    assert holder_stake([("Shares of Convertible Preferred Stock Percentage of Class", 90.0)],
                        several_classes=True)["share_class"] == "Convertible Preferred Stock"


def test_a_common_column_that_mentions_the_preferred_is_still_common():
    columns = [("Common Stock, with the shares the Series A Preferred Stock converts into %", 30.0),
               ("Series A Preferred Stock %", 100.0)]
    assert holder_stake(columns, several_classes=True)["stake_pct"] == 30.0


def test_two_classes_of_common_stock_still_take_the_larger():
    columns = [("% of Class A Common Stock", 13.2), ("% of Shares of Class B Common Stock", 40.4)]
    assert holder_stake(columns, several_classes=True)["stake_pct"] == 40.4      # Better Home's Garg


def _html(header: list[str], *rows: list[str]) -> str:
    cells = "".join(f"<td>{h}</td>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<html><body><p>as of March 2, 2026</p><table><tr>{cells}</tr>{body}</table></body></html>"


def test_a_percent_column_that_names_its_own_security_keeps_it():
    """Two percent columns side by side: the preferred's header must not take
    "Common Stock" from the column left of it."""
    table = read_holder_table(_html(
        ["Name of Beneficial Owner", "Shares Beneficially Owned", "Common Stock (%)", "Preferred Stock (%)"],
        ["Alpha Holdings LLC", "1,000", "30.0%", "100.0%"],
        ["Beta Insurance Company", "50", "—", "100.0%"]))
    rows = {r["name"]: r for r in table["rows"]}
    assert (rows["Alpha Holdings LLC"]["stake_pct"], rows["Alpha Holdings LLC"]["share_class"]) == (30.0, "Common Stock")
    assert (rows["Beta Insurance Company"]["stake_pct"], rows["Beta Insurance Company"]["share_class"]) == \
        (100.0, "Preferred Stock")


def test_a_total_over_every_class_is_not_the_preferred_left_of_it():
    """Victory Capital's 2026 proxy: "# of Preferred Shares | # of Capital
    Shares | % of Total Capital Stock" — Amundi holds 4.9 % of the common and
    27.8 % of the capital stock. The total must not read as the preferred."""
    table = read_holder_table(_html(
        ["Name of Beneficial Owner", "# of Common Stock Shares", "% of Total Common Stock", "# of Preferred Shares",
         "# of Capital Shares", "% of Total Capital Stock"],
        ["Amundi Parties", "3,293,471", "4.9%", "19,742,300", "23,035,771", "27.8%"],
        ["BlackRock, Inc.", "7,000,000", "10.8%", "—", "7,000,000", "8.2%"]))
    rows = {r["name"]: (r["stake_pct"], r["share_class"]) for r in table["rows"]}
    assert rows == {"Amundi Parties": (27.8, "Capital Stock"), "BlackRock, Inc.": (10.8, "Common Stock")}


def test_a_holder_of_a_voting_preferred_holds_its_share_of_the_voting_stock():
    """Arbor Realty's 2026 proxy: Arbor Commercial Mortgage, 1.3 % of the
    common, 64.8 % of the Special Voting Preferred, 6.2 % of the voting stock."""
    table = read_holder_table(_html(
        ["Name and Address", "Shares of Common Stock Beneficially Owned Percentage",
         "Shares of Special Voting Preferred Stock Beneficially Owned Percentage",
         "Total Shares of Voting Stock Beneficially Owned Percentage"],
        ["Arbor Commercial Mortgage, LLC", "1.3%", "64.8%", "6.2%"],
        ["John Natalone", "—", "23.1%", "1.9%"]))
    rows = {r["name"]: (r["stake_pct"], r["share_class"]) for r in table["rows"]}
    assert rows == {"Arbor Commercial Mortgage, LLC": (6.2, "Voting Stock"), "John Natalone": (1.9, "Voting Stock")}


def test_a_one_class_table_that_says_common_stock_names_no_class():
    table = read_holder_table(_html(
        ["Name of Beneficial Owner", "Shares of Common Stock Beneficially Owned", "Percent of Class"],
        ["Alpha Holdings LLC", "1,000", "30.0%"]))
    assert table["rows"][0]["stake_pct"] == 30.0 and table["rows"][0]["share_class"] is None


def test_a_one_class_table_names_no_class():
    """Nearly every table: "Common Stock | Percent of Class". Naming it would
    part these holders from the 13D/G holders of the same, unnamed class."""
    assert holder_stake([("Shares of Common Stock Percent of Class", 7.0)])["share_class"] is None
    assert _big(_table("davita_def14a_2026.html"))["BlackRock, Inc."]["share_class"] is None


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


# ── a holder that left after the table (findings item 116) ──────────────────

MINIMED = "0002062583"


def _submissions(*filings):
    return {"filings": {"recent": {"form": [f for f, _, _ in filings], "filingDate": [d for _, d, _ in filings],
                                   "accessionNumber": [a for _, _, a in filings]}}}


def _since(submissions, feed=(), day="2026-08-21"):
    from app.scraper import sec_edgar
    from app.scraper.sec_writer import _statements_since
    with patch.object(sec_edgar, "_ownership_feed_entries", return_value=iter(feed)) as asked:
        out = _statements_since(MINIMED, day, submissions)
    return out, asked.called


def test_the_statements_since_the_table_come_from_the_submissions_already_read():
    """MiniMed's submissions list the filings it is the subject of beside its
    own: no feed request, and nothing filed on or before the proxy's day."""
    subs = _submissions(("SCHEDULE 13G", "2026-11-06", "0000315066-26-002944"), ("4", "2026-10-30", "0002111492-26-000008"),
                        ("SCHEDULE 13G/A", "2026-10-16", "0001999371-26-022971"),
                        ("SCHEDULE 13G", "2026-09-30", "0002062583-26-000001"),      # its own, about another company
                        ("DEF 14A", "2026-08-21", "0001140361-26-034021"), ("SCHEDULE 13G", "2026-08-21", "0000000001-26-000001"),
                        ("SCHEDULE 13G", "2026-02-10", "0001613103-26-000001"))
    out, asked = _since(subs)
    assert [(e["file_date"], e["form_type"], e["accession"]) for e in out] == [
        ("2026-11-06", "SCHEDULE 13G", "0000315066-26-002944"), ("2026-10-16", "SCHEDULE 13G/A", "0001999371-26-022971")]
    assert out[1]["index_url"].endswith("/2062583/000199937126022971/0001999371-26-022971-index.htm")
    assert not asked


def test_a_recent_list_that_does_not_reach_the_table_falls_back_to_the_feed():
    """A bank's thousand newest filings span days: the list cannot say what
    was filed since a table of last spring."""
    subs = _submissions(("424B2", "2026-11-13", "x-1"), ("424B2", "2026-11-12", "x-2"))
    feed = [{"file_date": d, "accession": a, "form_type": "SCHEDULE 13G/A", "index_url": "u"}
            for d, a in (("2026-10-16", "mdt"), ("2026-08-21", "same-day"), ("2026-05-01", "older"))]
    out, asked = _since(subs, feed)
    assert [e["accession"] for e in out] == ["mdt"] and asked


def test_no_submissions_means_the_feed():
    out, asked = _since(None, [{"file_date": "2026-10-16", "accession": "mdt", "form_type": "SCHEDULE 13G/A",
                                "index_url": "u"}])
    assert [e["accession"] for e in out] == ["mdt"] and asked


@pytest.mark.parametrize("a, b, same", [
    ("Medtronic plc", "MEDTRONIC PLC", True),
    ("Warren E. Buffett", "BUFFETT WARREN E", True),
    ("The Vanguard Group", "VANGUARD GROUP INC", True),
    ("BlackRock, Inc.", "BlackRock Portfolio Management LLC", False),     # one shared word is not the holder
    ("The Vanguard Group", "Vanguard Capital Management", False),         # the filer its exit made way for
    ("Capital World Investors", "Capital Research Global Investors", False),
    ("", "", False),
])
def test_an_exit_is_matched_to_a_row_by_the_whole_name(a, b, same):
    from app.scraper.sec_writer import _same_holder
    assert _same_holder(a, b) is same
