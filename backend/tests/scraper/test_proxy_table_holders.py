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
    from app.scraper import sec_13dg
    from app.scraper.sec_writer import _statements_since
    with patch.object(sec_13dg, "_ownership_feed_entries", return_value=iter(feed)) as asked:
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


# ── a row of another security, a class named per row (findings item 117) ────

def _named(table: dict) -> dict:
    return {r["name"]: (r["stake_pct"], r["share_class"]) for r in table["rows"] if (r["stake_pct"] or 0) >= 5}


def test_malibu_units_of_the_operating_company_are_not_class_a():
    """"LLC Units Beneficially Owned" spans its own "Number | %": the percent
    took "Class A" from the group left of it, and the issuer's own 98.6 % of
    the LLC read as 98.6 % of its Class A stock."""
    named = _named(_table("malibu_boats_def14a_2026.html"))
    assert named["Malibu Boats, Inc."] == (98.6, "Llc Units")
    assert named["Pzena Investment Management LLC"] == (8.8, "Class A")
    assert _big(_table("malibu_boats_def14a_2026.html"))["Pzena Investment Management LLC"]["voting_power_pct"] == 8.6


def test_penguin_a_group_the_reader_has_no_word_for_is_another_security():
    """"Issued CPS" over "Amount | Percentage of Shares Beneficially Owned",
    beside "Common Stock" over the same pair: SK Telecom holds all of the
    convertible preferred and none of the common."""
    named = _named(_table("penguin_solutions_def14a_2025.html"))
    assert named["SK Telecom Co., Ltd."] == (100.0, "Issued CPS")
    assert named["BlackRock, Inc."] == (14.4, "Common Stock")


def test_restaurant_brands_exchangeable_units_are_not_the_common_shares():
    named = _named(_table("restaurant_brands_def14a_2026.html"))
    assert named["3G Funds"] == (90.7, "Partnership Exchangeable Units")
    assert named["Capital World Investors"] == (12.7, "Common Shares")
    # Pershing Square holds both: the common is its stake
    assert named["Pershing Square Funds"] == (6.7, "Common Shares")


def test_universal_display_the_class_stands_on_a_row_above_its_holders():
    """"Title of Class" is a column whose cell — "Common Stock", later "Series
    A Nonconvertible Preferred Stock" — stands alone above a group of rows."""
    named = _named(_table("universal_display_def14a_2026.html"))
    assert named["American Biomimetics Corporation"] == (100.0, "Series A")
    assert named["BlackRock, Inc."] == (8.6, "Common Stock")


def test_liberty_global_one_holder_over_three_class_rows():
    """Each holder takes three rows — Class A, B, C — and the name cell goes on
    with "c/o Liberty Global Ltd.", the address, a title. They read as holders
    of their own: "c/o Liberty Global Ltd." 67.8 %, "Clarendon House" 13.6 %,
    "Chairman, Chief Executive Officer &" 22.2 %."""
    table = _table("liberty_global_def14a_2026.html")
    named = _named(table)
    assert named["John C. Malone"] == (67.8, "Class B")
    assert named["Michael T. Fries"] == (22.2, "Class B")
    assert named["ACR Alpine Capital Research, LLC"] == (13.5, "Class A")
    assert named["Dimensional Fund Advisors LP"] == (5.0, "Class A")       # the row after ACR: its own holder
    names = {r["name"] for r in table["rows"]}
    assert not {n for n in names if n.startswith(("c/o", "Clarendon", "Chairman", "Executive", "Director"))}


def test_novagold_a_nominee_table_is_no_ownership_table():
    """"Name, Province … | Age | Independence | … | 2025 AGM Votes in Favor (%)
    | Meets Share Ownership Guidelines": every director had 85–99 %."""
    table = _table("novagold_def14a_2026.html")
    assert max(r["stake_pct"] or 0 for r in table["rows"]) < 30
    assert _named(table)["Paulson & Co. Inc."] == (9.24, None)
    assert not [r["name"] for r in table["rows"] if "Dorward-King" in r["name"]]


def _grouped(groups: list[tuple[str, int]], header: list[str], *rows: list[str]) -> str:
    """A table with a row of spanning headings over the header row."""
    top = "".join(f'<td colspan="{span}">{text}</td>' for text, span in groups)
    cells = "".join(f"<td>{h}</td>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<html><body><p>as of March 2, 2026</p><table><tr>{top}</tr><tr>{cells}</tr>{body}</table></body></html>"


def test_a_holder_of_the_stock_and_of_another_security_holds_the_stock():
    table = read_holder_table(_grouped(
        [("", 1), ("Common Stock", 2), ("Issued CPS", 2)],
        ["Name of Beneficial Owner", "Amount Beneficially Owned", "Percentage of Shares Beneficially Owned",
         "Amount Beneficially Owned", "Percentage of Shares Beneficially Owned"],
        ["Alpha Holdings LLC", "1,000", "30.0%", "200", "100.0%"],
        ["Beta Capital LP", "500", "15.0%", "—", "—"]))
    assert _named(table) == {"Alpha Holdings LLC": (30.0, "Common Stock"), "Beta Capital LP": (15.0, "Common Stock")}


def test_a_heading_that_names_nothing_still_lends_the_class_left_of_the_percent():
    """Molson Coors' shape under one spanning heading: "Beneficial Ownership"
    over "Class A Shares | Percent of class" names no security, so the percent
    is of the shares column left of it — inside its own group."""
    table = read_holder_table(_grouped(
        [("", 1), ("Beneficial Ownership", 4)],
        ["Name of Beneficial Owner", "Class A Shares", "Percent of class (%)", "Class B Shares", "Percent of class (%)"],
        ["Alpha Holdings LLC", "1,000", "30.0%", "5,000", "60.0%"]))
    assert _named(table) == {"Alpha Holdings LLC": (60.0, "Class B")}


def test_a_one_group_table_is_not_another_security():
    """One spanning heading the reader has no word for, and no security named
    beside it: a one-class table, as nearly every table is."""
    table = read_holder_table(_grouped(
        [("", 1), ("Shares Beneficially Owned", 2)],
        ["Name of Beneficial Owner", "Number", "Percent"],
        ["Alpha Holdings LLC", "1,000", "30.0%"]))
    assert _named(table) == {"Alpha Holdings LLC": (30.0, None)}


def test_two_holders_of_two_classes_in_a_class_column_stay_two_holders():
    """Lennar's 2026 proxy: GAMCO (Class B) stands right above The Vanguard
    Group (Class A). A class the holder above has no figure for does not make
    the next NAME that holder's second row."""
    table = read_holder_table(_html(
        ["Name", "Title of Class", "Amount and Nature of Beneficial Ownership", "Percent Of Class"],
        ["GAMCO Investors, Inc. One Corporate Center Rye, NY 10580", "Class B Common Stock", "1,724,170", "5.5%"],
        ["The Vanguard Group 100 Vanguard Blvd. Malvern, PA 19355", "Class A Common Stock", "27,368,807", "12.6%"]))
    assert sorted(v for v in _named(table).values()) == [(5.5, "Class B"), (12.6, "Class A")]


def test_a_row_without_a_name_is_the_holders_next_class():
    """Forward Air's 2026 proxy: the common on the holder's row, the Series B
    Preferred on a row with no name — 14.3 % of the common is the stake, not
    the preferred's 36.4 %."""
    table = read_holder_table(_html(
        ["Name and Address of Beneficial Owner", "Class of Shares or Units", "Total", "Percent of Class"],
        ["Ridgemont Group", "Company Common Stock", "5,109,278", "14.3 %"],
        ["", "Company Series B Preferred Units", "3,185,799", "36.4 %"],
        ["BlackRock, Inc.", "Company Common Stock", "4,000,000", "10.4 %"],
        ["", "Company Series B Preferred Units", "—", "—"]))
    assert _named(table) == {"Ridgemont Group": (14.3, "Common Stock"), "BlackRock, Inc.": (10.4, "Common Stock")}


def test_a_class_heading_over_the_percent_columns_is_no_class_column():
    """Universal Health Services: "Title of Class" spans a column per class."""
    table = read_holder_table(_grouped(
        [("", 1), ("Title of Class", 4)],
        ["Name and Address of Beneficial Owner", "Class A Common Stock Shares", "Class A Common Stock %",
         "Class C Common Stock Shares", "Class C Common Stock %"],
        ["Alan B. Miller", "5,112,384", "77.8%", "661,688", "100%"],
        ["BlackRock, Inc.", "400,000", "7.7%", "—", "—"]))
    assert _named(table) == {"Alan B. Miller": (100.0, "Class C"), "BlackRock, Inc.": (7.7, "Class A")}


def test_a_one_word_class_cell_and_a_section_heading_with_a_figure():
    """Bancroft Fund names its classes "Common" and "Preferred"; United States
    Antimony's "More than 5% owners:" heading stood in the class column and
    read as a percent column, which hid the class column."""
    html = ("<html><body><p>as of March 2, 2026</p><table>"
            "<tr><td>Title of Class</td><td>Name and Address of Beneficial Owner</td>"
            "<td>Amount of Shares</td><td>Percent of Class</td></tr>"
            "<tr><td>More than 5% owners:</td></tr>"
            "<tr><td>Common</td><td>Saba Capital Management, L.P.</td><td>361,294</td><td>6.2%</td></tr>"
            "<tr><td>Preferred</td><td>Americo Investment Advisors Inc.</td><td>60,000</td><td>5.5%</td></tr>"
            "</table></body></html>")
    assert _named(read_holder_table(html)) == {"Saba Capital Management, L.P.": (6.2, "Common Stock"),
                                               "Americo Investment Advisors Inc.": (5.5, "Preferred Stock")}


WHIRLPOOL_DIRECTORS = (
    "<html><body><p>The table shows the beneficial ownership of our common stock as of February 9, 2026, by "
    "each director and executive officer.</p><table>"
    "<tr><td>Name</td><td>Shares Beneficially Owned (1)</td><td>Deferred Stock Units (2)</td>"
    "<td>Shares Under Exercisable Options (3)</td><td>Total (4)</td><td>{percent}</td></tr>"
    "<tr><td>Samuel R. Allen</td><td>22,156</td><td>—</td><td>—</td><td>22,156</td><td>*</td></tr>"
    "<tr><td>Marc R. Bitzer</td><td>301,442</td><td>—</td><td>472,619</td><td>774,061</td><td>1.37%</td></tr>"
    "</table></body></html>")


@pytest.mark.parametrize("percent", [
    "Percentage (* Less than 1%)",          # Whirlpool's DEF 14A of 2026-03-10
    "Percent of Class (*less than 1.0 %)",
    "% of Class (if more than 1%)",
])
def test_a_note_with_a_figure_beside_the_percent_heading_keeps_the_column(percent):
    """Findings item 142: every header stating a figure was refused as a
    percent column (the rule meant for "More than 5% owners:"), so Whirlpool's
    director table read no row and no record date — and the statements pass,
    which begins at the earlier of filing day and record date, lost the 13D
    Appaloosa filed between the two."""
    html = WHIRLPOOL_DIRECTORS.format(percent=percent)
    assert [r["name"] for r in read_holder_rows(html)] == ["Samuel R. Allen", "Marc R. Bitzer"]
    table = read_holder_table(html)
    assert table["as_of"] == "2026-02-09"
    assert [(r["name"], r["stake_pct"]) for r in table["rows"]] == [("Marc R. Bitzer", 1.37)]


@pytest.mark.parametrize("text, heads_a_percent_column", [
    ("Percent of Class", True),
    ("%", True),
    ("Percentage (* Less than 1%)", True),
    ("More than 5% owners:", False),          # a section heading (United States Antimony)
    ("5% Stockholders", False),
    ("Owners of 5 % or more", False),         # the figure and its sign apart
    ("2025 AGM Votes in Favor (%)", False),   # a vote count (NovaGold)
    ("Shares Beneficially Owned", False),
])
def test_what_heads_a_percent_column(text, heads_a_percent_column):
    from app.scraper.proxy_statement import _is_percent_header
    assert _is_percent_header(text) is heads_a_percent_column


@pytest.mark.parametrize("columns, stake, named", [
    # units of the operating company beside the stock: the stock is the stake
    ([("Class A Common Stock %", 6.7), ("LLC Units Beneficially Owned %", 98.6)], 6.7, "Class A"),
    ([("Common Shares % of Class", 6.7), ("% of Class of Partnership Exchangeable Units", 3.6)], 6.7, "Common Shares"),
    # … and all a row states when it states nothing else
    ([("% of Class of Partnership Exchangeable Units", 90.7)], 90.7, "Partnership Exchangeable Units"),
    ([("Other security: Issued CPS (2) | Percentage of Shares Beneficially Owned", 100.0)], 100.0, "Issued CPS"),
    ([("Common Stock Percentage", 14.4),
      ("Other security: Issued CPS | Percentage of Shares Beneficially Owned", 100.0)], 14.4, "Common Stock"),
    # units that ARE the issuer's equity stay a class of their own
    ([("Common Units Percent of Class", 30.0), ("Preferred Units Percent of Class", 80.0)], 30.0, "Common Units"),
])
def test_the_stake_is_a_percent_of_the_stock(columns, stake, named):
    got = holder_stake(columns, several_classes=True)
    assert (got["stake_pct"], got["share_class"]) == (stake, named)


def test_two_headings_that_name_no_security_are_one_class():
    """"Before the Offering" and "After the Offering" over "Number | Percent"
    each: two groups, no security named anywhere — not two other securities."""
    table = read_holder_table(_grouped(
        [("", 1), ("Shares Beneficially Owned Before the Offering", 2),
         ("Shares Beneficially Owned After the Offering", 2)],
        ["Name of Beneficial Owner", "Number", "Percent", "Number", "Percent"],
        ["Alpha Holdings LLC", "1,000", "30.0%", "1,000", "24.0%"]))
    assert _named(table) == {"Alpha Holdings LLC": (30.0, None)}


def test_the_class_cell_above_the_first_holders_is_no_heading_of_the_percent_column():
    """Universal Display's "Common Stock" stands in the header rows, in the
    class column: lent to the percent column as its heading it would follow
    every later class ("Series A … Preferred Stock Common Stock Percentage")."""
    from bs4 import BeautifulSoup
    from app.scraper.proxy_statement import _holder_rows
    soup = BeautifulSoup((FIXTURES / "universal_display_def14a_2026.html").read_text(), "lxml")
    rows = {r["name"]: r for r in _holder_rows(soup.find("table"))}
    [(heading, pct)] = rows["American Biomimetics Corporation"]["columns"]
    assert pct == 100.0 and heading.startswith("Series A") and "Common" not in heading


def test_a_section_heading_in_the_class_column_is_no_class_and_ends_no_holder():
    """United States Antimony: "Directors and Executive Officers" stands in
    the class column between two groups of rows. Read as a class it made the
    next holder's row the second row of the holder above the heading — Edward
    Robinson's 18.1 % of the Series C became 0.6 % of the common."""
    html = ("<html><body><p>as of March 2, 2026</p><table>"
            "<tr><td>Title of Class of Stock</td><td>Name and Address of Beneficial Owner</td>"
            "<td>Amount and Nature of Beneficial Ownership</td><td>Percent of Class</td></tr>"
            "<tr><td>Series C Preferred</td><td>Edward Robinson</td><td>32,203</td><td>18.1%</td></tr>"
            "<tr><td>Directors and Executive Officers</td></tr>"
            "<tr><td>Common Stock</td><td>Dr. Blaise Aguirre</td><td>8,879,854</td><td>6.0%</td></tr>"
            "</table></body></html>")
    assert _named(read_holder_table(html)) == {"Edward Robinson": (18.1, "Series C"),
                                               "Dr. Blaise Aguirre": (6.0, "Common Stock")}


def test_a_section_heading_does_not_become_the_class_of_the_rows_below_it():
    """The class stands once above its holders (Universal Display's shape) and
    a section heading follows in the same column: the holders below it are
    still holders of that class, and of no class named after the heading."""
    html = ("<html><body><p>as of March 2, 2026</p><table>"
            "<tr><td>Title of Class</td><td>Name of Beneficial Owner</td>"
            "<td>Number of Shares Beneficially Owned</td><td>Percentage Ownership</td></tr>"
            "<tr><td>Common Stock</td></tr>"
            "<tr><td></td><td>Alpha Holdings LLC</td><td>1,000</td><td>30.0%</td></tr>"
            "<tr><td>Directors and Executive Officers</td></tr>"
            "<tr><td></td><td>Beta Capital LP</td><td>200</td><td>6.0%</td></tr>"
            "</table></body></html>")
    assert _named(read_holder_table(html)) == {"Alpha Holdings LLC": (30.0, None), "Beta Capital LP": (6.0, None)}


# ── layouts the header alone does not explain (findings items 139, 143, 144, 150) ──

def test_a_voting_power_column_headed_without_percent_is_read():
    """findings item 139: "Voting Power" / "Total Voting Power" say percent
    only beside the figures ("30.3 | %")."""
    liberty = {r["name"]: r["voting_power_pct"] for r in _table("liberty_global_def14a_2026.html")["rows"]}
    assert liberty["John C. Malone"] == 30.3 and liberty["Michael T. Fries"] == 9.9
    assert liberty["ACR Alpine Capital Research, LLC"] == 7.78
    brands = {r["name"]: r["voting_power_pct"] for r in _table("restaurant_brands_def14a_2026.html")["rows"]}
    assert brands["3G Funds"] == 21.7 and brands["Capital World Investors"] == 9.7
    penguin = {r["name"]: r["voting_power_pct"] for r in _table("penguin_solutions_def14a_2025.html")["rows"]}
    assert penguin["SK Telecom Co., Ltd."] == 10.4 and penguin["BlackRock, Inc."] == 12.9
    # the class percents stay what they were
    assert _named(_table("liberty_global_def14a_2026.html"))["John C. Malone"] == (67.8, "Class B")


def _rows_html(*rows: str) -> str:
    return "<table>" + "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row.split("|")) + "</tr>" for row in rows) + "</table>"


def test_a_voting_power_column_of_vote_counts_is_no_percent_column():
    """No "%" in the header and none beside the figures: the votes held."""
    table = read_holder_table(_rows_html("Name of Beneficial Owner|Shares Beneficially Owned|Percent of Class|Voting Power",
                                    "Alpha Holdings LLC|1,000,000|12.5%|10", "Beta Partners LP|500,000|6.3%|5"))
    assert [(r["name"], r["stake_pct"], r["voting_power_pct"]) for r in table["rows"]] == [
        ("Alpha Holdings LLC", 12.5, None), ("Beta Partners LP", 6.3, None)]


def test_whirlpool_a_table_that_opens_each_row_with_a_date():
    """findings item 143: "Schedule 13G Filed On | Name and Address of
    Beneficial Owner | Shares Beneficially Owned | Percent of Class" — the
    date stood where the reader looked for the name, so no row was a data row."""
    table = _table("whirlpool_def14a_2026.html")
    assert table["as_of"] == "2025-12-31"
    assert {n: r["stake_pct"] for n, r in _big(table).items()} == {
        "BlackRock, Inc.": 11.01, "The Vanguard Group Inc.": 9.73, "Appaloosa LP": 6.92,
        "PRIMECAP Management Company": 6.75, "FMR LLC": 5.66}


def test_onespan_no_header_over_the_percent_and_the_name_above_the_address():
    """findings items 144 + 150: the header is "Name and Address of Beneficial
    Owner | Amount and Nature of Beneficial Ownership" and nothing stands over
    "10.0%"; each 5 % holder is named alone on a row, the figures stand on the
    address row below it."""
    table = _table("onespan_def14a_2026.html")
    assert table["as_of"] == "2026-04-08"
    big = _big(table)
    assert {n: r["stake_pct"] for n, r in big.items()} == {
        "BlackRock, Inc.": 10.0, "Entities affiliated with Legal & General Investment Management Limited": 5.6,
        "Ameriprise Financial, Inc.": 5.5}
    assert big["Entities affiliated with Legal & General Investment Management Limited"]["group"] is True
    names = [r["name"] for r in table["rows"]]
    assert "Marc Boroditsky" in read_names("onespan_def14a_2026.html")      # the directors below, one row each
    assert not [n for n in names if n[0].isdigit() or n.startswith("One ")]


def read_names(fixture: str) -> list[str]:
    return [r["name"] for r in read_holder_rows((FIXTURES / fixture).read_text())]


@pytest.mark.parametrize("fixture, holders", [
    # "BlackRock, Inc." / "50 Hudson Yards | 6,085,591 (1) | 13.87%" / "New York, NY 10001"
    ("dime_community_def14a_2026.html", {
        "BlackRock, Inc.": 13.87, "Wellington Management Group LLP": 9.72, "The Vanguard Group": 6.66,
        "Dimensional Fund Advisors LP": 5.31, "T. Rowe Price Investment Management, Inc.": 5.14}),
    # name and street in one cell, the figures beside "Malvern, PA 19355"; State
    # Street's block has four lines: the name, "State Street Financial Center",
    # the street, the city (CIK 0001410636)
    ("state_street_block_def14a.html", {
        "The Vanguard Group": 12.3, "BlackRock, Inc.": 10.1, "State Street Corporation": 5.7}),
    # a heading the reader does not know ("Beneficial Ownership of 5% or
    # more:"), then "Orbis … Limited (2)" / "Orbis House, 25 Front Street" /
    # "Hamilton, Bermuda HM11 | 13,424,844 | 11.7 | %" (CIK 0001852244)
    ("orbis_block_def14a.html", {"Orbis Investment Management Limited": 11.7, "BlackRock, Inc.": 9.2}),
])
def test_a_holder_named_above_its_address_row_keeps_its_name(fixture, holders):
    """findings item 150: the name row has no figure and was dropped; the
    holder was written as "50 Hudson Yards", "Malvern, PA 19355", "Hamilton,
    Bermuda HM11"."""
    assert {n: r["stake_pct"] for n, r in _big(_table(fixture)).items()} == holders


HEAD = "Name and Address of Beneficial Owner|Shares Beneficially Owned|Percent of Class"


@pytest.mark.parametrize("rows, names", [
    # a heading, then holders whose cell begins with the name and goes on with the address
    (["Significant Shareholders", "The Vanguard Group 100 Vanguard Blvd. Malvern, PA 19355|7,286,181|11.06%",
      "FMR LLC 245 Summer Street Boston, MA 02210|5,575,253|8.46%"], ["The Vanguard Group", "FMR LLC"]),
    # a fund's ticker alone on a row, then its holder with "c/o" inside the cell
    (["RSF", "Patrick W. Galley c/o 360 South Rosemary Avenue|214,920|5.60%"], ["Patrick W. Galley"]),
    # a number in front of a name with a legal form is no street
    (["Principal Stockholders", "59 North Capital Management, LP|1,000,000|5.3%",
      "1607 Capital Partners, LLC|900,000|6.99%"], ["59 North Capital Management, LP", "1607 Capital Partners, LLC"]),
    # a heading the reader does not know, then the name, then the address row with the figures
    (["Principal Holders", "The Vanguard Group", "100 Vanguard Blvd.|7,286,181|11.06%"], ["The Vanguard Group"]),
    # … and a street word inside such a name does not make it one
    (["Principal Stockholders", "100 Park Avenue Partners, LLC|1,000,000|5.3%"], ["100 Park Avenue Partners, LLC"]),
    # a known heading above an address row names nobody
    (["5% Stockholders", "50 Hudson Yards, New York, NY 10001|3,720,220|10.0%"], ["50 Hudson Yards, New York, NY 10001"]),
])
def test_what_stands_above_a_row_is_taken_for_its_name_only_over_an_address(rows, names):
    assert [r["name"] for r in read_holder_table(_rows_html(HEAD, *rows))["rows"]] == names


@pytest.mark.parametrize("rows, names", [
    # Ovintiv 2026: the table numbers its holders (findings item 151)
    (["1 The Vanguard Group (1)|26,000,000|10.3%", "2 BlackRock, Inc. (2)|21,000,000|8.3%",
      "3 FMR LLC (3)|18,000,000|7.3%"], ["The Vanguard Group", "BlackRock, Inc.", "FMR LLC"]),
    (["1. The Vanguard Group|26,000,000|10.3%", "2) BlackRock, Inc.|21,000,000|8.3%"],
     ["The Vanguard Group", "BlackRock, Inc."]),
    # … beside rows without one
    (["1 The Vanguard Group|26,000,000|10.3%", "2 BlackRock, Inc.|21,000,000|8.3%", "Jane Doe|100|1.2%"],
     ["The Vanguard Group", "BlackRock, Inc.", "Jane Doe"]),
    # one row beginning with a number is a name
    (["1 Main Street Capital, LP|1,000,000|5.3%", "FMR LLC|900,000|6.99%"], ["1 Main Street Capital, LP", "FMR LLC"]),
    # numbers that do not count from 1 are names
    (["2 Sigma Partners, LP|1,000,000|5.3%", "3 Arrows Capital, LLC|900,000|6.99%"],
     ["2 Sigma Partners, LP", "3 Arrows Capital, LLC"]),
    # … nor those that count in another order
    (["2 Sigma Partners, LP|1,000,000|5.3%", "1 Main Street Capital, LP|900,000|6.99%"],
     ["2 Sigma Partners, LP", "1 Main Street Capital, LP"]),
    (["59 North Capital Management, LP|1,000,000|5.3%", "65 Equity Partners|900,000|9.22%"],
     ["59 North Capital Management, LP", "65 Equity Partners"]),
    (["3M Company|1,000,000|5.3%", "1st Source Bank|900,000|9.22%"], ["3M Company", "1st Source Bank"]),
])
def test_the_numbers_of_a_numbered_table_are_no_part_of_the_names(rows, names):
    table = read_holder_table(_rows_html(HEAD, *rows))["rows"]
    assert [r["name"] for r in table] == names
    assert [r["names"] for r in table] == [[n] for n in names]   # what the writer looks the holder up by


@pytest.mark.parametrize("cell, group", [
    ("John C. Malone c/o Liberty Media Corporation", False),   # an address, the stake is his own (item 152)
    ("Patrick W. Galley c/o 360 South Rosemary Avenue", False),
    ("Warren E. Buffett and affiliated entities", True),
    ("Entities affiliated with Legal & General", True),
    ("FMR LLC", False),
])
def test_a_care_of_address_makes_no_group(cell, group):
    row = read_holder_table(_rows_html(HEAD, f"{cell}|1,000,000|5.3%"))["rows"][0]
    assert row["group"] is group


def test_two_percent_columns_under_no_header_are_not_guessed():
    """findings item 144: one unheaded column is the percent of the class; with
    two, only a header could say which is which."""
    head = "Name of Beneficial Owner|Amount and Nature of Beneficial Ownership"
    one = read_holder_table(_rows_html(head, "Alpha Holdings LLC|1,000,000|12.5%", "Beta Partners LP|500,000|6.3%"))
    assert [(r["name"], r["stake_pct"]) for r in one["rows"]] == [("Alpha Holdings LLC", 12.5), ("Beta Partners LP", 6.3)]
    two = read_holder_table(_rows_html(head, "Alpha Holdings LLC|1,000,000|12.5%|40.0%", "Beta Partners LP|500,000|6.3%|20.0%"))
    assert two["rows"] == []
    single = read_holder_table(_rows_html(head, "Alpha Holdings LLC|1,000,000|12.5%", "Beta Partners LP|500,000|—"))
    assert single["rows"] == []                                  # stated once: not a column yet


def test_a_percent_under_a_header_that_names_no_percent_is_not_guessed():
    """findings item 144: the unheaded column is one under NO header cell; a
    figure under "Change" is that column's, whatever it says."""
    table = read_holder_table(_rows_html(
        "Name of Beneficial Owner|Amount and Nature of Beneficial Ownership|Change",
        "Alpha Holdings LLC|1,000,000|12.5%", "Beta Partners LP|500,000|6.3%"))
    assert table["rows"] == []
