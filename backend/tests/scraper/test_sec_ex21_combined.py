"""One 10-K filed by several registrants: AEP and seven subsidiaries, Duke
Energy and seven, Eversource and three. The one Exhibit 21 is the group's;
each co-registrant keeps only the branch the list draws under its own row."""
from unittest.mock import patch

from app.scraper import sec_ex21 as ex
from app.scraper.sec_ex21 import co_registrants, fetch_subsidiaries, parse_exhibit, registrants_part

HEADER = """<SEC-HEADER>0000072741-26-000010.hdr.sgml : 20260212
<TYPE>10-K
<FILER>
<COMPANY-DATA>
<CONFORMED-NAME>EVERSOURCE ENERGY
<CIK>0000072741
</COMPANY-DATA>
</FILER>
<FILER>
<COMPANY-DATA>
<CONFORMED-NAME>NSTAR ELECTRIC CO
<CIK>0000013372
</COMPANY-DATA>
</FILER>
<FILER>
<COMPANY-DATA>
<CONFORMED-NAME>PUBLIC SERVICE CO OF NEW HAMPSHIRE
<CIK>0000315256
</COMPANY-DATA>
</FILER>
"""
EVERSOURCE, NSTAR, PSNH = "72741", "13372", "315256"
FILERS = [(EVERSOURCE, "EVERSOURCE ENERGY"), (NSTAR, "NSTAR ELECTRIC CO"),
          (PSNH, "PUBLIC SERVICE CO OF NEW HAMPSHIRE")]


# an unrelated branch, so that every list draws a tree the parser trusts
# (two levels, three indented rows)
ZETA = [("Zeta Holdings LLC", 0), ("Zeta One LLC", 1), ("Zeta Two LLC", 2), ("Zeta Three LLC", 1)]


def _tree_list(rows):
    """An indented list: (name, level)."""
    rows = rows + ZETA
    return ("<table><tr><td>Name</td><td>State of Incorporation</td></tr>" + "".join(
        f'<tr><td style="padding-left:{12 * lvl}pt">{n}</td><td>Massachusetts</td></tr>' for n, lvl in rows)
            + "</table>")


GROUP = _tree_list([("NSTAR Electric Company", 0), ("Harbor Electric Energy Company", 1),
                    ("Harbor Sub LLC", 2), ("Public Service Company of New Hampshire", 0),
                    ("PSNH Funding LLC 3", 1), ("Aquarion Company", 0), ("Aquarion Water Company", 1)])


def _part(html, cik, filers=FILERS, registrant=None):
    return registrants_part(html, None, parse_exhibit(html, registrant), filers, cik, registrant)


class TestCoRegistrants:
    def test_every_filer_of_the_header(self):
        with patch.object(ex, "_get_text", return_value=HEADER) as get:
            assert co_registrants("13372", "0000072741-26-000010") == FILERS
        assert get.call_args[0][0] == ("https://www.sec.gov/Archives/edgar/data/13372/000007274126000010/"
                                       "0000072741-26-000010.hdr.sgml")

    def test_an_unreadable_header_is_no_combined_filing(self):
        with patch.object(ex, "_get_text", side_effect=RuntimeError("404")):
            assert co_registrants("13372", "0000072741-26-000010") == []


class TestRegistrantsPart:
    def test_the_filer_keeps_the_branch_under_its_own_row(self):
        subs, owner = _part(GROUP, NSTAR)
        assert owner == "EVERSOURCE ENERGY"
        assert [(s["name"], s.get("parent"), s.get("parent_basis")) for s in subs] == [
            ("Harbor Electric Energy Company", None, "indent"),          # directly under the filer
            ("Harbor Sub LLC", "Harbor Electric Energy Company", "indent")]

    def test_a_filer_without_a_branch_keeps_nothing(self):
        html = _tree_list([("NSTAR Electric Company", 0), ("Public Service Company of New Hampshire", 0),
                           ("PSNH Funding LLC 3", 1), ("Aquarion Company", 0)])
        assert _part(html, NSTAR) == ([], "EVERSOURCE ENERGY")

    def test_the_group_parent_reads_the_whole_list(self):
        subs, owner = _part(GROUP, EVERSOURCE, registrant="Eversource Energy")
        assert owner is None and len(subs) == 7 + len(ZETA)

    def test_a_filing_of_one_company(self):
        subs = parse_exhibit(GROUP)
        assert _part(GROUP, NSTAR, filers=[(NSTAR, "NSTAR ELECTRIC CO")]) == (subs, None)

    def test_every_registrant_listed_says_nothing_about_whose_list_it_is(self):
        # Brixmor lists itself and its operating partnership
        filers = [("1", "Brixmor Property Group Inc."), ("2", "Brixmor Operating Partnership LP")]
        html = _tree_list([("Brixmor Property Group Inc.", 0), ("Brixmor Operating Partnership LP", 0),
                           ("Brixmor LLC", 0)])
        assert len(_part(html, "2", filers)[0]) == 3 + len(ZETA) and _part(html, "2", filers)[1] is None

    def test_a_filer_the_list_does_not_name_reads_it_as_before(self):
        # OneMain Finance: the list is OneMain Holdings', neither is a row
        filers = [("1", "OneMain Holdings, Inc."), ("2", "ONEMAIN FINANCE CORP")]
        html = _tree_list([("AGFC Capital Trust I", 0), ("CommoLoCo, Inc.", 0)])
        assert _part(html, "2", filers) == (parse_exhibit(html), None)

    def test_the_operating_partnership_is_not_the_reit(self):
        # Hudson Pacific Properties, Inc. (the filer) lists "…, L.P." — another
        # legal form, another company: the list is the REIT's own
        filers = [("1", "Hudson Pacific Properties, Inc."), ("2", "Hudson Pacific Properties, L.P.")]
        html = _tree_list([("Hudson Pacific Properties, L.P.", 0), ("HPP Sub LLC", 1)])
        subs, owner = _part(html, "1", filers)
        assert owner is None and [s["name"] for s in subs][:2] == ["Hudson Pacific Properties, L.P.", "HPP Sub LLC"]
        subs, owner = _part(html, "2", filers)
        assert owner == "Hudson Pacific Properties, Inc." and [s["name"] for s in subs] == ["HPP Sub LLC"]

    def test_the_filer_by_its_listed_name_its_legal_form_spelled_out_and_without_its_dba(self):
        filers = [("1", "Northwest Natural Holding Co"), ("2", "NORTHWEST NATURAL GAS CO")]
        html = _tree_list([("Northwest Natural Gas Company (dba NW Natural)", 0), ("NWN Gas Reserves LLC", 1),
                           ("NW Natural Water Company, LLC", 0)])
        assert _part(html, "2", filers) == (
            [{"name": "NWN Gas Reserves LLC", "jurisdiction": "Massachusetts", "parent_basis": "indent",
              "read_from": "table"}],
            "Northwest Natural Holding Co")

    def test_the_filer_by_the_name_the_graph_gives_it(self):
        filers = [("1", "Alpha Holdings Corp"), ("2", "BETA OPCO")]
        html = _tree_list([("Beta Operating Company", 0), ("Gamma LLC", 1)])
        assert _part(html, "2", filers, registrant="Beta Operating Company")[0][0]["name"] == "Gamma LLC"


def test_fetch_reads_the_filers_part_and_says_whose_list_it_is():
    cands = [{"url": "https://x/ex21.htm", "form": "10-K", "filing_date": "2026-02-12",
              "accession": "0000072741-26-000010"}]
    with patch.object(ex, "annual_filings", return_value=[("10-K", "0000072741-26-000010", "2026-02-12", "")]), \
         patch.object(ex, "exhibit_candidates", return_value=cands), \
         patch.object(ex, "_get_text", side_effect=lambda u: HEADER if u.endswith(".hdr.sgml") else GROUP):
        got = fetch_subsidiaries(NSTAR, "NSTAR Electric Company")
    assert got["group_of"] == "EVERSOURCE ENERGY"
    assert [s["name"] for s in got["subsidiaries"]] == ["Harbor Electric Energy Company", "Harbor Sub LLC"]


def test_the_history_reads_the_filers_part_too():
    cands = [{"url": "https://x/ex21.htm", "form": "10-K", "filing_date": "2026-02-12",
              "accession": "0000072741-26-000010"}]
    with patch.object(ex, "annual_filings", return_value=[("10-K", "0000072741-26-000010", "2026-02-12", "")]), \
         patch.object(ex, "exhibit_candidates", return_value=cands), \
         patch.object(ex, "_get_text", side_effect=lambda u: HEADER if u.endswith(".hdr.sgml") else GROUP):
        history = ex.fetch_subsidiary_history(NSTAR)
    assert history[0]["names"] == {"harbor electric energy", "harbor sub"}


# ── One list per registrant (findings item 78) ───────────────────────────────
# Measured 2026-11-08: 43 of the 6,360 10-Ks of 2026 carry several subsidiary
# lists, every one a list per registrant named in its heading. Real exhibits:
# Kilroy's 21.1 (the REIT's, with "Kilroy Realty, L.P." as its first row) and
# 21.2 (the partnership's own); Exelon's 21.1 (the group's, ComEd a row in it),
# 21.2 (ComEd's own 4) and 21.3 (PECO's own 8).
from pathlib import Path

import pytest

FX = Path(__file__).parent / "fixtures"
KILROY_ACC, EXELON_ACC = "0001628280-26-007051", "0001109357-26-000018"
KILROY_FILERS = [("1025996", "KILROY REALTY CORP"), ("1493976", "Kilroy Realty, L.P.")]
EXELON_FILERS = [("1109357", "EXELON CORP"), ("9466", "BALTIMORE GAS & ELECTRIC CO"),
                 ("22606", "COMMONWEALTH EDISON Co"), ("1135971", "PEPCO HOLDINGS LLC"),
                 ("78100", "PECO ENERGY CO"), ("8192", "ATLANTIC CITY ELECTRIC CO")]


def _cands(acc, *files):
    return [{"url": f"https://www.sec.gov/x/{f}", "form": "10-K", "filing_date": "2026-02-10",
             "accession": acc, "period": None} for f in files]


def _doc(url):
    return (FX / url.rsplit("/", 1)[1]).read_text()


def _fetch_counting(cik, names, calls):
    with patch("app.scraper.sec_ex21.annual_filings",
               return_value=[("10-K", KILROY_ACC, "2026-02-10", "2025-12-31")]), \
         patch("app.scraper.sec_ex21.exhibit_candidates",
               return_value=_cands(KILROY_ACC, "kilroy_ex21_1.htm", "kilroy_ex21_2.htm")):
        return fetch_subsidiaries(cik, names)


def _fetch(cik, names, acc, files, filers):
    with patch("app.scraper.sec_ex21.annual_filings", return_value=[("10-K", acc, "2026-02-10", "2025-12-31")]), \
         patch("app.scraper.sec_ex21.exhibit_candidates", return_value=_cands(acc, *files)), \
         patch("app.scraper.sec_ex21.co_registrants", return_value=filers), \
         patch("app.scraper.sec_ex21._get_text", side_effect=_doc):
        return fetch_subsidiaries(cik, names)


class TestOwnListFirst:
    def test_the_partnership_reads_its_own_list_not_the_reits(self):
        got = _fetch("1493976", ["Kilroy Realty, L.P."], KILROY_ACC,
                     ("kilroy_ex21_1.htm", "kilroy_ex21_2.htm"), KILROY_FILERS)
        assert got["url"].endswith("kilroy_ex21_2.htm") and len(got["subsidiaries"]) == 77
        assert "group_of" not in got

    def test_the_reit_still_reads_its_own(self):
        got = _fetch("1025996", ["Kilroy Realty Corporation"], KILROY_ACC,
                     ("kilroy_ex21_2.htm", "kilroy_ex21_1.htm"), KILROY_FILERS)    # order does not matter
        assert got["url"].endswith("kilroy_ex21_1.htm") and len(got["subsidiaries"]) == 78  # the L.P. a row of it

    def test_a_filing_of_the_partnership_alone_still_finds_its_own(self):
        # SL Green's partnership files its own 10-K with both lists: the header names it alone
        got = _fetch("1493976", ["Kilroy Realty, L.P."], KILROY_ACC,
                     ("kilroy_ex21_1.htm", "kilroy_ex21_2.htm"), [("1493976", "Kilroy Realty, L.P.")])
        assert got["url"].endswith("kilroy_ex21_2.htm")

    def test_the_header_name_is_enough_without_the_nodes_names(self):
        # the history and legal_identity call without a registrant name; the
        # partnership's own filing names it alone in the header
        with patch("app.scraper.sec_ex21.co_registrants", return_value=[("1493976", "Kilroy Realty, L.P.")]), \
             patch("app.scraper.sec_ex21._get_text", side_effect=_doc):
            got = ex._parse_first(_cands(KILROY_ACC, "kilroy_ex21_1.htm", "kilroy_ex21_2.htm"), "1493976")
        assert got[1]["url"].endswith("kilroy_ex21_2.htm") and len(got[0]) == 77

    def test_a_utility_reads_its_own_of_eight(self):
        got = _fetch("22606", ["Commonwealth Edison Company"], EXELON_ACC,
                     ("exelon_ex21_1.htm", "exelon_ex21_3.htm", "exelon_ex21_2.htm"), EXELON_FILERS)
        assert got["url"].endswith("exelon_ex21_2.htm")
        assert [s["name"] for s in got["subsidiaries"]][:2] == ["Commonwealth Edison Company of Indiana, Inc.",
                                                                "ComEd Financing III"]

    def test_another_registrants_list_is_never_the_filers(self):
        # Atlantic City Electric has no list of its own here: the group's names it
        # as a row (its branch is read from it, as before), PECO's and ComEd's are
        # theirs — never ACE's
        with patch("app.scraper.sec_ex21.co_registrants", return_value=EXELON_FILERS), \
             patch("app.scraper.sec_ex21._get_text", side_effect=_doc):
            order = ex.own_list_first(_cands(EXELON_ACC, "exelon_ex21_3.htm", "exelon_ex21_2.htm",
                                             "exelon_ex21_1.htm"), "8192", ["Atlantic City Electric Company"])
        assert [m["url"].rsplit("/", 1)[1] for m in order] == ["exelon_ex21_1.htm"]

    def test_a_list_of_another_registrant_without_the_filer_is_dropped(self):
        # Blackstone's (TE) fund's list, read from the other fund's CIK
        filers = [("1", "Kilroy Realty Corp"), ("2", "Kilroy Realty, L.P.")]
        with patch("app.scraper.sec_ex21.co_registrants", return_value=filers + [("3", "Other Fund L.P.")]), \
             patch("app.scraper.sec_ex21._get_text", side_effect=_doc):
            order = ex.own_list_first(_cands(KILROY_ACC, "kilroy_ex21_2.htm", "kilroy_ex21_1.htm"), "3",
                                      ["Other Fund L.P."])
        # the L.P.'s list names neither the fund nor carries it: dropped; the
        # REIT's names the REIT and has no row for the fund: dropped too
        assert order == []

    def test_the_registration_statement_reads_its_own_too(self):
        with patch("app.scraper.sec_ex21._all_filings", return_value=[("S-1", KILROY_ACC, "2026-02-10", "", "")]), \
             patch("app.scraper.sec_ex21.exhibit_candidates",
                   return_value=_cands(KILROY_ACC, "kilroy_ex21_1.htm", "kilroy_ex21_2.htm")), \
             patch("app.scraper.sec_ex21.co_registrants", return_value=KILROY_FILERS), \
             patch("app.scraper.sec_ex21._get_text", side_effect=_doc):
            got = ex.list_from_registration("1493976", ["Kilroy Realty, L.P."])
        assert got["url"].endswith("kilroy_ex21_2.htm")

    def test_an_earlier_filings_list_is_read_the_same_way(self):
        with patch("app.scraper.sec_ex21.resolve_reference", return_value=[("10-K", KILROY_ACC, "2026-02-10")]), \
             patch("app.scraper.sec_ex21.exhibit_candidates",
                   return_value=_cands(KILROY_ACC, "kilroy_ex21_1.htm", "kilroy_ex21_2.htm")), \
             patch("app.scraper.sec_ex21.co_registrants", return_value=KILROY_FILERS), \
             patch("app.scraper.sec_ex21._get_text", side_effect=_doc):
            got = ex.list_from_earlier_filing("1493976", "Exhibit 21.1 to our Form 10-K filed on February 10, 2026",
                                              "", ["Kilroy Realty, L.P."])
        assert got["url"].endswith("kilroy_ex21_2.htm")

    def test_an_own_list_that_lists_the_filer_is_not_read_as_another_registrants(self):
        # "BRIXMOR OPERATING PARTNERSHIP LP AND SUBSIDIARIES" carries the
        # partnership itself among its 500 rows (the head of the real exhibit
        # and that row): the combined-filing rule took it for the REIT's list
        # and kept the partnership's branch of it — nothing. Its heading says whose.
        filers = [("1581068", "Brixmor Property Group Inc."), ("1630031", "Brixmor Operating Partnership LP")]
        got = _fetch("1630031", ["Brixmor Operating Partnership LP"], "0001581068-26-000007",
                     ("brixmor_op_ex21_2_head.htm",), filers)
        assert [s["name"] for s in got["subsidiaries"]] == [
            "Arapahoe Crossings, L.P.", "Berkshire Crossing Shopping Center, LLC", "Bradley Financing LLC",
            "Bradley Financing Partnership", "Bradley Operating LLC"]
        assert "group_of" not in got

    def test_each_candidate_is_fetched_once(self):
        calls = []
        with patch("app.scraper.sec_ex21.co_registrants", return_value=KILROY_FILERS), \
             patch("app.scraper.sec_ex21._get_text", side_effect=lambda u: calls.append(u) or _doc(u)):
            got = _fetch_counting("1493976", ["Kilroy Realty, L.P."], calls)
        assert got["url"].endswith("kilroy_ex21_2.htm")
        assert sorted(calls) == sorted(set(calls))


@pytest.mark.parametrize("text,name,says", [
    ("Subsidiaries of Kilroy Realty, L.P.", "Kilroy Realty, L.P.", True),
    ("Subsidiaries of Kilroy Realty, L.P.", "KILROY REALTY CORP", False),        # the legal form decides
    ("SUBSIDIARIES OF KILROY REALTY CORPORATION", "KILROY REALTY CORP", True),
    ("SUBSIDIARIES - BALTIMORE GAS AND ELECTRIC COMPANY", "BALTIMORE GAS & ELECTRIC CO", True),
    ("SUBSIDIARIES - DELMARVA POWER & LIGHT COMPANY", "DELMARVA POWER & LIGHT CO /DE/", True),
    ("Subsidiaries of VICI Properties L.P.", "VICI Properties L.P.", True),
    ("SUBSIDIARIES OF SOTHERLY HOTELS L.P.", "SOTHERLY HOTELS LP", True),          # dotted letters are one word
    ("Blackstone Private Equity Strategies Fund (TE) L.P.", "Blackstone Private Equity Strategies Fund L.P.", False),
    ("Subsidiaries of Kilroy Realty, L.P.", "Kilroy Realty", False),             # no legal form: too loose
])
def test_a_heading_names_a_registrant_whole(text, name, says):
    assert ex._says(ex._words(text), name) is says
