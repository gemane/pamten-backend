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
            [{"name": "NWN Gas Reserves LLC", "jurisdiction": "Massachusetts", "parent_basis": "indent"}],
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
