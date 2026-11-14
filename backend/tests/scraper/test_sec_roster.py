"""The directors and officers the proxy names whom the insider window misses
(findings item 115). The owner tables are cut from EDGAR's "Ownership Reports
from" pages of Berkshire Hathaway and Tesla (2026-11-14); the proxy names are
those of Berkshire's DEF 14A of 2026-03-13 and Tesla's of 2026-10-19."""
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.scraper import sec_edgar, sec_roster
from app.scraper.sec_roster import has_seat, match_owner, parse_owners, person_parts, roster_insiders

FIXTURES = Path(__file__).parent / "fixtures"
BERKSHIRE = parse_owners((FIXTURES / "own_disp_berkshire.html").read_text())
TESLA = parse_owners((FIXTURES / "own_disp_tesla.html").read_text())


# ── the owner list ──────────────────────────────────────────────────────────

def test_the_owner_table_gives_every_reporting_owner_with_cik_and_type():
    assert len(BERKSHIRE) == 23 and len(TESLA) == 34
    by_name = {o["name"]: o for o in BERKSHIRE}
    assert by_name["CHENAULT KENNETH I"] == {"name": "CHENAULT KENNETH I", "cik": "0001180524", "type": "director"}
    assert by_name["Jain Ajit"]["type"] == "director, officer: Vice Chairman"
    # the departed are listed too: the list alone is no roster
    assert {"GATES WILLIAM H III", "GOTTESMAN DAVID S", "MUNGER CHARLES T"} <= set(by_name)


def test_a_page_without_the_owner_table_gives_no_owners():
    assert parse_owners("<html><body><table><tr><td>Home</td></tr></table></body></html>") == []


# ── a proxy's name → the reporting owner ────────────────────────────────────

@pytest.mark.parametrize("name, owners, cik", [
    ("Kenneth I. Chenault", BERKSHIRE, "0001180524"),
    ("Howard G. Buffett", BERKSHIRE, "0001253718"),          # BUFFETT HOWARD, not Warren or Susan
    ("Susan A. Buffett", BERKSHIRE, "0001891497"),           # not BUFFETT SUSAN T, her mother
    ("Thomas S. Murphy, Jr.", BERKSHIRE, "0001402372"),      # not MURPHY THOMAS S, his father
    ("Stephen B. Burke", BERKSHIRE, "0001203694"),
    ("Robyn Denholm", TESLA, "0001242782"),
    ("Joe Gebbia", TESLA, "0001834171"),                     # Gebbia Joseph — a nickname
    ("JB Straubel", TESLA, "0001494727"),                    # Straubel Jeffrey B — initials
    ("Ira Ehrenpreis", TESLA, "0001412598"),
    ("Kathleen Wilson-Thompson", TESLA, "0001331680"),
])
def test_a_proxy_name_finds_its_reporting_owner(name, owners, cik):
    assert match_owner(name, owners)["cik"] == cik


@pytest.mark.parametrize("name, owners", [
    ("Wallace R. Weitz", BERKSHIRE),          # no filing under Berkshire at all
    ("Thomas S. Murphy", BERKSHIRE),          # father or son: both are owners, and the proxy does not say
    ("Susan Buffett", BERKSHIRE),             # two of the name, no initial to tell them apart
    ("Tom Zhu", TESLA),                       # Zhu Xiaotong: a name EDGAR does not know him by
    ("Alice Kroeger", TESLA),                 # a surname alone is not the departed Kroeger Harald
    ("John Musk", TESLA),                     # nor a shared first letter (Musk Elon, Musk Kimbal)
    ("Jamie Buss", TESLA),
    ("Cher", TESLA),
])
def test_a_seat_is_not_given_on_a_guess(name, owners):
    assert match_owner(name, owners) is None


def test_father_and_son_are_told_apart_by_a_stated_suffix_only():
    assert match_owner("Thomas S. Murphy, Sr.", BERKSHIRE) is None        # EDGAR's row of the father carries none
    owners = [{"name": "MURPHY THOMAS S SR", "cik": "1", "type": "director"},
              {"name": "MURPHY THOMAS S JR", "cik": "2", "type": "director"}]
    assert match_owner("Thomas S. Murphy, Sr.", owners)["cik"] == "1"
    assert match_owner("Thomas S. Murphy, Jr.", owners)["cik"] == "2"
    assert match_owner("Thomas S. Murphy", owners) is None


def test_one_owner_of_the_name_is_taken_whatever_the_suffix():
    """A proxy often drops the "Jr." EDGAR carries; with nobody else of the
    name among the owners there is nothing to confuse."""
    assert match_owner("David H. Morton", TESLA)["cik"] == "0001476070"     # MORTON DAVID H JR


def test_name_parts_cut_marks_titles_and_degrees():
    assert person_parts("Thomas S. Murphy, Jr.") == {"given": ["thomas", "s"], "surname": "murphy",
                                                     "suffix": "jr", "initials": False}
    assert person_parts("David R. Little, Chairman of the Board, President and CEO")["surname"] == "little"
    assert person_parts("William J. Link, Ph.D.")["surname"] == "link"
    assert person_parts("Brad Jacobs †")["surname"] == "jacobs"
    assert person_parts("JB Straubel")["initials"] is True
    assert person_parts("Al Gore")["initials"] is False
    assert person_parts("Cher") is None


def test_the_window_already_lists_a_person_generously():
    window = [{"name": "Xiaotong Zhu"}, {"name": "Warren E Buffett"}, {"name": "Thomas S Jr Murphy"}]
    assert has_seat("Warren E. Buffett", window)
    assert has_seat("Thomas S. Murphy, Jr.", window)
    assert not has_seat("Tom Zhu", window)               # no shared initial: looked up, and then not matched
    assert not has_seat("Howard G. Buffett", window)
    assert has_seat("Cher", window)                      # nothing to look up


# ── the records ─────────────────────────────────────────────────────────────

FILING = {"form": "DEF 14A", "date": "2026-03-13", "url": "https://www.sec.gov/Archives/edgar/data/1067983/x/def14a.htm"}
FORM3 = {"name": "Kenneth I Chenault", "title": "", "role": "Director", "shares_owned": 100.0, "share_class": "Class B",
         "holdings": {"Class B": 100.0}, "blocks": [{"x": 1}], "issuer_cik": "0001067983", "person_cik": "0001180524",
         "document_type": "3", "period_of_report": "2020-05-02", "former": False,
         "source_url": "https://www.sec.gov/form3-index.htm", "source_date": "2020-05-12"}


def _roster(people, window, form3=None, owners=BERKSHIRE, filing=FILING):
    with patch("app.scraper.proxy_statement.latest_table_filing", return_value=filing), \
         patch.object(sec_edgar, "_get_text", return_value="<html></html>"), \
         patch.object(sec_roster, "roster_people", return_value=people), \
         patch.object(sec_roster, "issuer_owners", return_value=owners) as asked, \
         patch.object(sec_roster, "own_form3", side_effect=lambda pc, ic: dict(form3) if form3 and pc == form3["person_cik"] else None) as f3:
        out = roster_insiders("0001067983", window, {"cik": "1067983"})
    return out, asked, f3


def test_a_proxy_named_director_outside_the_window_gets_the_seat_from_the_form_3():
    out, _, _ = _roster(["Warren E. Buffett", "Kenneth I. Chenault", "Wallace R. Weitz"],
                        [{"name": "Warren E Buffett", "person_cik": "0000315090"}], FORM3)
    assert [r["name"] for r in out] == ["Kenneth I Chenault"]              # Buffett has a seat, Weitz no filing
    seat = out[0]
    assert (seat["role"], seat["person_cik"], seat["since"], seat["until"]) == ("Director", "0001180524", "2020-05-02", None)
    assert seat["source_url"] == "https://www.sec.gov/form3-index.htm" and seat["roster"] == FILING["url"]
    # a Form 3's count is as old as the seat: no holding is written from it
    assert (seat["shares_owned"], seat["holdings"], seat["blocks"]) == (None, {}, [])


def test_without_a_form_3_the_owner_list_and_the_proxy_state_the_seat():
    """Howard G. Buffett: a director since 1993, before electronic filing."""
    out, _, _ = _roster(["Howard G. Buffett"], [])
    assert len(out) == 1
    seat = out[0]
    assert (seat["name"], seat["role"], seat["person_cik"], seat["since"]) == ("Howard Buffett", "Director", "0001253718", None)
    assert (seat["source_url"], seat["source_date"]) == (FILING["url"], "2026-03-13")
    assert seat["former"] is False and seat["holdings"] == {}






def test_an_officer_gets_no_seat_from_the_proxy():
    """The table lists last year's named executive officers, and they include
    the departed: Molson Coors' 2026 proxy names Gavin Hattersley, who retired
    as chief executive in 2025. Only an outside director is taken."""
    owners = [{"name": "Hattersley Gavin", "cik": "0000000005", "type": "director, officer: President and CEO"},
              {"name": "Smith Jane", "cik": "0000000007", "type": "officer: Chief Financial Officer"},
              {"name": "GATES WILLIAM H III", "cik": "0000902012", "type": "10 percent owner"},
              {"name": "Doe John", "cik": "0000000009", "type": "director, 10 percent owner"}]
    out, _, _ = _roster(["Gavin Hattersley", "Jane Smith", "William H. Gates III", "John Doe"], [], owners=owners)
    assert [(r["name"], r["role"]) for r in out] == [("John Doe", "Director")]


def test_the_newest_form_3_decides_over_the_owner_lists_old_type():
    """JB Straubel: "officer: Chief Technology Officer" in the owner list (2010),
    a director by his Form 3 of 2023."""
    form3 = {**FORM3, "name": "Jeffrey B Straubel", "person_cik": "0001494727", "period_of_report": "2023-05-16"}
    out, _, _ = _roster(["JB Straubel"], [], form3, owners=TESLA)
    assert [(r["name"], r["role"], r["since"]) for r in out] == [("Jeffrey B Straubel", "Director", "2023-05-16")]


def test_a_joint_form_3_dates_the_seat_and_the_owner_list_names_the_person():
    """Innovex: the Form 3 of its director Will Donnell is a joint one, parsed
    for its first reporting owner, the fund he sits for — "Amberjack Capital
    Partners, L.P." must not become the record's name."""
    owners = [{"name": "Donnell William P.", "cik": "0002026040", "type": "director"}]
    joint = {**FORM3, "name": "Amberjack Capital Partners, L.P.", "person_cik": "0002026035",
             "period_of_report": "2024-09-06", "source_url": "https://www.sec.gov/joint-index.htm", "role": "Director"}
    with patch.object(sec_roster, "own_form3", return_value=dict(joint)), \
         patch("app.scraper.proxy_statement.latest_table_filing", return_value=FILING), \
         patch.object(sec_edgar, "_get_text", return_value="<html></html>"), \
         patch.object(sec_roster, "roster_people", return_value=["William Donnell"]), \
         patch.object(sec_roster, "issuer_owners", return_value=owners):
        out = roster_insiders("0001042893", [], {"cik": "1042893"})
    assert [(r["name"], r["person_cik"], r["role"], r["since"], r["source_url"]) for r in out] == \
        [("William P Donnell", "0002026040", "Director", "2024-09-06", "https://www.sec.gov/joint-index.htm")]


@pytest.mark.parametrize("change", [{"role": "CFO"}, {"former": True}])
def test_a_form_3_that_states_an_officer_or_a_former_seat_gives_none(change):
    out, _, _ = _roster(["Kenneth I. Chenault"], [], {**FORM3, **change})
    assert out == []


@pytest.mark.parametrize("owner_type, outside", [
    ("director", True), ("director, 10 percent owner", True), ("Director", True),
    ("director, officer: Vice Chairman", False), ("officer: See Remarks", False), ("10 percent owner", False), ("", False),
])
def test_an_outside_director_is_a_director_who_is_no_officer(owner_type, outside):
    assert sec_roster.is_outside_director(owner_type) is outside


# ── silence must be the rule, or the person's own ───────────────────────────

POOL_OWNERS = [{"name": "SABATER CARLOS A", "cik": "0000000101", "type": "director"},
               {"name": "WOOD KATHERINE", "cik": "0000000102", "type": "director"}]
POOL_PEOPLE = ["Ann Able", "Bob Baker", "Carl Cook", "Dora Dean", "Ed Eaton"]
POOL_WINDOW = [{"name": n, "role": "Director", "person_cik": str(i).zfill(10)} for i, n in enumerate(POOL_PEOPLE, 1)]


def _pool(people, form3s, window=POOL_WINDOW):
    seated = [e["name"] for e in window]                 # the proxy names who sits, and the people asked about
    with patch("app.scraper.proxy_statement.latest_table_filing", return_value=FILING), \
         patch.object(sec_edgar, "_get_text", return_value="<html></html>"), \
         patch.object(sec_roster, "roster_people", return_value=seated + people), \
         patch.object(sec_roster, "issuer_owners", return_value=POOL_OWNERS), \
         patch.object(sec_roster, "own_form3", side_effect=lambda pc, ic: dict(form3s[pc]) if pc in form3s else None):
        return [r["name"] for r in roster_insiders("0000945841", window, {"cik": "945841"})]


def test_on_a_board_that_files_every_year_a_director_the_window_lacks_has_left():
    """Pool's 2026 proxy names Carlos Sabater; his last grant is of May 2025
    and every other director filed one in May 2026: he left at the meeting
    the proxy was written for. 13 of 33 on dev were such."""
    sabater = {**FORM3, "name": "Carlos A Sabater", "person_cik": "0000000101", "silent_since": False}
    assert _pool(["Carlos A. Sabater"], {"0000000101": sabater}) == []


def test_a_director_silent_since_their_form_3_is_seated_on_any_board():
    """A shareholder's nominee gets no grants to file for: LifeStance's
    Katherine Wood has filed nothing but Form 3s."""
    wood = {**FORM3, "name": "Katherine Wood", "person_cik": "0000000102", "silent_since": True}
    sabater = {**FORM3, "name": "Carlos A Sabater", "person_cik": "0000000101", "silent_since": False}
    assert _pool(["Carlos A. Sabater", "Katherine Wood"], {"0000000101": sabater, "0000000102": wood}) == ["Katherine Wood"]


def test_a_board_is_quiet_when_the_window_lacks_as_many_directors_as_it_holds():
    sabater = {**FORM3, "name": "Carlos A Sabater", "person_cik": "0000000101", "silent_since": False}
    wood = {**FORM3, "name": "Katherine Wood", "person_cik": "0000000102", "silent_since": False}
    both = {"0000000101": sabater, "0000000102": wood}
    people = ["Carlos A. Sabater", "Katherine Wood"]
    assert _pool(people, both, window=POOL_WINDOW[:2]) == ["Carlos A Sabater", "Katherine Wood"]      # 2 lacking, 2 held
    assert _pool(people, both, window=POOL_WINDOW[:3]) == []                                         # 2 lacking, 3 held


def test_only_sitting_directors_of_the_proxy_count_as_held():
    people = ["Ann Able", "Bob Baker", "Carl Cook", "Dora Dean", "Ed Eaton"]
    window = [{"name": "Ann Able", "role": "Director"}, {"name": "Bob Baker", "role": "CEO"},
              {"name": "Carl Cook", "role": "Director", "former": True}, {"name": "Dora Dean", "role": "Director", "until": "2026-01-01"},
              {"name": "Zed Zimmer", "role": "Director"}]                    # not in the proxy
    assert sec_roster.seated_directors(people, window) == 1


def test_an_owner_the_window_lists_under_another_spelling_is_not_written_twice():
    out, _, _ = _roster(["Kenneth I. Chenault"], [{"name": "K. Irvine Chenault-Smith", "person_cik": "0001180524"}], FORM3)
    assert out == []


def test_nobody_missing_asks_edgar_for_nothing_more():
    out, asked, _ = _roster(["Warren E. Buffett"], [{"name": "Warren E Buffett"}])
    assert out == [] and not asked.called


def test_no_table_filing_no_roster():
    out, asked, _ = _roster(["Kenneth I. Chenault"], [], filing=None)
    assert out == [] and not asked.called


def test_an_unreadable_owner_list_marks_the_insiders_section_and_writes_nobody():
    with patch("app.scraper.proxy_statement.latest_table_filing", return_value=FILING), \
         patch.object(sec_edgar, "_get_text", return_value="<html></html>"), \
         patch.object(sec_roster, "roster_people", return_value=["Kenneth I. Chenault"]), \
         patch.object(sec_roster, "issuer_owners", side_effect=httpx.ConnectError("down")), \
         patch.object(sec_edgar, "_section_failed") as failed:
        assert roster_insiders("0001067983", [], {"cik": "1067983"}) == []
    assert failed.call_args.args[0] == sec_edgar.SECTION_INSIDERS


def test_the_lookups_are_capped():
    people = [f"Person{i} Chenault" for i in range(50)]
    owners = [{"name": f"CHENAULT PERSON{i}", "cik": str(i).zfill(10), "type": "director"} for i in range(50)]
    out, _, f3 = _roster(people, [], owners=owners)
    assert len(out) == sec_roster.MAX_ROSTER_LOOKUPS == f3.call_count


# ── the person's own Form 3 ─────────────────────────────────────────────────

def _form3_xml(issuer_cik, period, director=True, other=""):
    return f"""<?xml version="1.0"?><ownershipDocument><documentType>3</documentType>
<periodOfReport>{period}</periodOfReport><issuer><issuerCik>{issuer_cik}</issuerCik></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0001180524</rptOwnerCik><rptOwnerName>CHENAULT KENNETH I</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>{1 if director else 0}</isDirector><isOfficer>0</isOfficer>
<isOther>{0 if director else 1}</isOther><otherText>{other}</otherText></reportingOwnerRelationship></reportingOwner></ownershipDocument>"""


def _own(filings, pages):
    subs = {"filings": {"recent": {"form": [f for f, _, _ in filings], "accessionNumber": [a for _, a, _ in filings],
                                   "primaryDocument": ["xslF345X02/doc.xml"] * len(filings),
                                   "filingDate": [d for _, _, d in filings]}}}

    def text(url, params=None):
        acc = url.split("/")[-2]
        if acc not in pages:
            raise httpx.ConnectError("no such filing")
        return pages[acc]
    with patch.object(sec_edgar, "_submissions", return_value=subs), \
         patch.object(sec_edgar, "_get_text", side_effect=text) as fetched:
        out = sec_roster.own_form3("0001180524", "0001067983")
    return out, [c.args[0].split("/")[-2] for c in fetched.call_args_list]


def test_the_persons_own_form_3_about_the_issuer_dates_the_seat():
    """Only Form 3s are opened — the Form 4s about other companies that bury
    it are not — and the one about another issuer is passed over."""
    out, opened = _own([("4", "0001-26-000009", "2026-08-07"), ("4", "0001-26-000008", "2026-06-29"),
                        ("3", "0001-20-000700", "2020-12-10"), ("3", "0001-20-000500", "2020-05-12")],
                       {"000120000700": _form3_xml("0001559720", "2020-12-09"),          # Airbnb
                        "000120000500": _form3_xml("0001067983", "2020-05-02")})
    assert opened == ["000120000700", "000120000500"]
    assert (out["name"], out["role"], out["period_of_report"], out["source_date"]) == \
        ("Kenneth I Chenault", "Director", "2020-05-02", "2020-05-12")
    assert out["source_url"].endswith("/1067983/000120000500/0001-20-000500-index.htm")


def test_a_form_4_or_5_filed_after_the_form_3_ends_the_silence():
    form3 = {"000120000500": _form3_xml("0001067983", "2020-05-02")}
    quiet, _ = _own([("3", "0001-20-000500", "2020-05-12"), ("4", "0001-19-000001", "2019-03-01")], form3)
    assert quiet["silent_since"] is True                                   # the Form 4 is older
    for later in ("4", "4/A", "5"):
        out, opened = _own([(later, "0001-26-000009", "2026-08-07"), ("3", "0001-20-000500", "2020-05-12")], form3)
        assert out["silent_since"] is False and opened == ["000120000500"]     # counted from the index, not opened


def test_no_form_3_about_the_issuer_gives_none():
    out, _ = _own([("3", "0001-20-000700", "2020-12-10")], {"000120000700": _form3_xml("0001559720", "2020-12-09")})
    assert out is None


def test_an_unreadable_form_3_is_passed_over():
    out, opened = _own([("3", "0001-21-000001", "2021-01-01"), ("3", "0001-20-000500", "2020-05-12")],
                       {"000120000500": _form3_xml("0001067983", "2020-05-02")})
    assert opened == ["000121000001", "000120000500"] and out["period_of_report"] == "2020-05-02"


def test_the_form_3s_opened_are_capped():
    filings = [("3", f"0001-10-{i:06d}", "2010-01-01") for i in range(40)]
    out, opened = _own(filings, {a.replace("-", ""): _form3_xml("0000000042", "2010-01-01") for _, a, _ in filings})
    assert out is None and len(opened) == sec_roster.MAX_PERSON_FORM3


# ── the scrape ──────────────────────────────────────────────────────────────

def test_the_people_a_proxy_names_leave_the_companies_out():
    rows = [{"name": "BlackRock, Inc."}, {"name": "Kenneth I. Chenault"}, {"name": "The Vanguard Group"},
            {"name": "Cher"}, {"name": "Thomas S. Murphy, Jr."}, {"name": "Harbor Lights Entertainment LLC"}]
    with patch("app.scraper.proxy_statement.read_holder_rows", return_value=rows):
        people = sec_roster.roster_people("<html></html>")
    assert "Kenneth I. Chenault" in people and "Thomas S. Murphy, Jr." in people
    assert "BlackRock, Inc." not in people and "Cher" not in people
    assert "Harbor Lights Entertainment LLC" not in people
