"""When the shares a 13D/G statement is about ceased to exist (findings item 99).

Fixtures are the real EDGAR documents: Sprint's 25-NSE (2020-04-01, Common
Stock, 12d2-2(a)(3)) and its Form 15 (2020-04-13, common stock + notes, "one"
holder); Foundation Medicine's Form 15 ("Two"); Texaco's ("Fewer than 300");
WesBanco's (a preferred series only); a 12d2-2(b) delisting; Airspan's
warrants; a SPAC's units."""
import pathlib
from unittest.mock import patch

import pytest

from app.scraper import sec_class_end as ce

FIX = pathlib.Path(__file__).parent / "fixtures" / "class_end"


def _read(name):
    return (FIX / name).read_text()


class TestFields:
    def test_sprint_25nse_is_a_ceased_common_class(self):
        cls, rule = ce.form25_fields(_read("sprint_25nse_2020.xml"))
        assert (cls, rule) == ("Common Stock", "17 CFR 240.12d2-2(a)(3)")
        assert ce.is_equity_class(cls) and ce.class_ceased(rule)

    def test_a_delisting_for_the_listing_rules_is_no_end(self):
        cls, rule = ce.form25_fields(_read("rule_b_25nse.xml"))
        assert ce.is_equity_class(cls) and not ce.class_ceased(rule)

    @pytest.mark.parametrize("name", ["airspan_warrants_25nse_2023.xml", "spac_units_25nse.xml"])
    def test_warrants_and_spac_units_are_no_equity_class(self, name):
        cls, _ = ce.form25_fields(_read(name))
        assert not ce.is_equity_class(cls)

    def test_foundation_medicine_form15_two_holders_of_common(self):
        cls, holders = ce.form15_fields(_read("foundation_medicine_15_12b_2018.htm"))
        assert cls == "Common Stock, par value $0.01 per share"
        assert ce.is_equity_class(cls) and ce.few_holders(holders)

    def test_sprints_form15_names_the_common_stock_beside_its_notes(self):
        cls, holders = ce.form15_fields(_read("sprint_15_12b_notes_2020.htm"))
        assert cls.startswith("Sprint Corporation: Common Stock")
        assert ce.is_equity_class(cls) and ce.few_holders(holders)

    def test_texaco_fewer_than_300_is_going_dark_not_an_end(self):
        cls, holders = ce.form15_fields(_read("texaco_15_12b_2001.txt"))
        assert ce.is_equity_class(cls) and not ce.few_holders(holders)

    def test_wesbancos_preferred_series_is_no_equity_class(self):
        cls, holders = ce.form15_fields(_read("wesbanco_15_12g_preferred_2025.htm"))
        assert not ce.is_equity_class(cls) and ce.few_holders(holders)


@pytest.mark.parametrize("text, expected", [
    ("Common Stock, par value $0.01 per share", True),
    ("Class A Ordinary Shares, Warrants, Units", True),                      # a SPAC wound up: the shares too
    ("Common Stock, Warrant", True),
    ("American Depositary Shares, each representing two Class A ordinary shares", True),
    ("Common Units representing limited partner interests", True),
    ("Class A Ordinary Share", True),                                        # Perfect Corp's 25-NSE, singular
    ("Common Stock; Depositary Shares, each representing a 1/40th interest in a share of Preferred Stock", True),
    ("6.875% Notes due November 15, 2028 of Sprint Capital Corporation", False),
    ("9.75% Series A Fixed-to-Floating Rate Cumulative Redeemable Perpetual Preferred Units", False),
    ("Series A Preference Stock Purchase Rights", False),
    ("Warrants to Purchase Common Stock (expiring December 12, 2018)", False),
    ("Plan Interests under the Resource America, Inc. Investment Savings Plan", False),
    ("Guaranteed senior debt securities of Credit Suisse (USA), Inc.", False),
    ("", False), (None, False),
])
def test_is_equity_class(text, expected):
    assert ce.is_equity_class(text) is expected


@pytest.mark.parametrize("text, expected", [
    ("as of the certification or notice date: one", True),
    ("as of the certification or notice date: Two Pursuant", True),
    ("as of the certification or notice date: -0- .", True),
    ("as of the certification or notice date:  None", True),
    ("as of the certification or notice date: 1* * On September 10", True),
    ("as of the certification or notice date: 2", True),
    ("as of the certification or notice date: 3", False),
    ("as of the certification or notice date: Fewer than 300.", False),
    ("as of the certification or notice date: approximately 150", False),
    ("", False), (None, False),
])
def test_few_holders(text, expected):
    assert ce.few_holders(text) is expected


@pytest.mark.parametrize("rule, expected", [
    ("17 CFR 240.12d2-2(a)(3)", True), ("17 CFR 240.12d2-2(a)(1)", True),
    ("17 CFR 240.12d2-2 (a)(2)", True), ("17 CFR 240.12d2-2(b)", False),
    ("17 CFR 240.12d2-2(c)", False), (None, False),
])
def test_class_ceased(rule, expected):
    assert ce.class_ceased(rule) is expected


def _subs(rows):
    """A submissions document from (form, date, accession, primary document) rows."""
    return {"filings": {"recent": {"form": [r[0] for r in rows], "filingDate": [r[1] for r in rows],
                                   "accessionNumber": [r[2] for r in rows],
                                   "primaryDocument": [r[3] for r in rows]}}}


SPRINT_25 = ("25-NSE", "2020-04-01", "0000876661-20-000282", "xslF25X02/primary_doc.xml")
SPRINT_15 = ("15-12B", "2020-04-13", "0001193125-20-105344", "d866456d1512b.htm")


#: accession folder → the fixture its form reads (a 25's primary_doc.xml, a 15's document)
DOCS = {"000087666120000282": "sprint_25nse_2020.xml", "000119312520105344": "sprint_15_12b_notes_2020.htm",
        "000000000000000001": "texaco_15_12b_2001.txt", "000000000000000002": "rule_b_25nse.xml",
        "000000000000000003": "airspan_warrants_25nse_2023.xml",
        "000000000000000004": "wesbanco_15_12g_preferred_2025.htm"}


def _docs(url):
    folder = url.rsplit("/", 2)[-2]
    if folder in DOCS:
        return _read(DOCS[folder])
    raise AssertionError(f"unexpected request {url}")


class TestClassEnds:
    def _run(self, rows):
        with patch.object(ce.sec_edgar, "_get_text", side_effect=_docs) as get:
            return ce.class_ends("0000101830", _subs(rows)), get

    def test_sprint_ends_on_its_25nse_then_its_form15(self):
        ends, get = self._run([("10-Q", "2020-02-01", "x-1", "q.htm"), SPRINT_15, SPRINT_25])
        assert [(e["date"], e["form"]) for e in ends] == [("2020-04-01", "25-NSE"), ("2020-04-13", "15-12B")]
        assert ends[0]["class"] == "Common Stock"
        assert ends[0]["url"] == ("https://www.sec.gov/Archives/edgar/data/101830/000087666120000282/"
                                  "0000876661-20-000282-index.htm")
        # the 25-NSE is read from its XML, not the rendered xsl path the list names
        assert any(c.args[0].endswith("/000087666120000282/primary_doc.xml") for c in get.call_args_list)

    @pytest.mark.parametrize("row", [
        ("15-12B", "2001-10-10", "0000000000-00-000001", "f.txt"),        # "fewer than 300": going dark
        ("25-NSE", "2019-01-30", "0000000000-00-000002", "x.xml"),        # 12d2-2(b): listing rules
        ("25-NSE", "2023-08-14", "0000000000-00-000003", "x.xml"),        # warrants
        ("15-12G", "2025-11-24", "0000000000-00-000004", "d.htm"),        # a preferred series, "None"
    ])
    def test_what_is_no_end(self, row):
        ends, get = self._run([row])
        assert ends == [] and get.call_count == 1

    def test_a_successor_issuer_within_30_days_is_no_end(self):
        ends, _ = self._run([SPRINT_25, ("8-K12B", "2020-04-20", "x-2", "k.htm")])
        assert ends == []

    def test_a_successor_issuer_long_after_is_another_matter(self):
        ends, _ = self._run([SPRINT_25, ("8-K12B", "2020-06-20", "x-2", "k.htm")])
        assert [e["date"] for e in ends] == ["2020-04-01"]

    def test_a_13dg_filed_after_the_grace_keeps_the_issuer_held(self):
        ends, _ = self._run([SPRINT_25, ("SCHEDULE 13G/A", "2021-05-15", "x-3", "g.htm")])
        assert ends == []

    @pytest.mark.parametrize("form, day", [("SC 13G/A", "2020-05-20"),       # an exit filing within 60 days
                                           ("SC 13D", "2019-12-01")])        # before the end
    def test_a_13dg_within_the_grace_or_before_does_not(self, form, day):
        ends, _ = self._run([SPRINT_25, (form, day, "x-3", "g.htm")])
        assert [e["date"] for e in ends] == ["2020-04-01"]

    @pytest.mark.parametrize("day, until", [
        ("2025-12-09", "2026-02-20"),     # Akero: the annual 13G/As of February are exits
        ("2020-04-01", "2021-02-20"),     # an end in spring: the next February
        ("2026-01-10", "2026-03-11"),     # 60 days reach past this February
        ("2026-02-20", "2027-02-20"),
    ])
    def test_the_grace_runs_to_the_next_february_20_at_least(self, day, until):
        assert ce._grace_until(day) == until

    def test_a_february_exit_after_a_december_end_is_an_exit(self):
        late = ("25-NSE", "2025-12-09", "0000876661-20-000282", "x.xml")
        ends, _ = self._run([late, ("SCHEDULE 13G/A", "2026-02-17", "x-3", "g.htm")])
        assert [e["date"] for e in ends] == ["2025-12-09"]
        ends, _ = self._run([late, ("SCHEDULE 13G/A", "2026-03-02", "x-3", "g.htm")])
        assert ends == []

    @pytest.mark.parametrize("form", ["SCHEDULE 13G", "SC 13D"])
    def test_a_new_statement_after_the_end_is_a_new_holder(self, form):
        ends, _ = self._run([SPRINT_25, (form, "2020-04-20", "x-3", "g.htm")])
        assert ends == []

    @pytest.mark.parametrize("form", ["N-CEN", "N-CSR", "NPORT-P", "485BPOS"])
    def test_a_fund_trust_is_skipped_without_a_request(self, form):
        with patch.object(ce.sec_edgar, "_get_text") as get:
            assert ce.class_ends("0001100663", _subs([SPRINT_25, (form, "2026-01-01", "x-4", "n.htm")])) == []
        get.assert_not_called()

    def test_an_issuer_filed_form_25_is_not_read(self):
        with patch.object(ce.sec_edgar, "_get_text") as get:
            assert ce.class_ends("1", _subs([("25", "2020-04-01", "0000000000-20-000009", "x.xml")])) == []
        get.assert_not_called()

    def test_an_unreadable_form_is_skipped(self):
        with patch.object(ce.sec_edgar, "_get_text", side_effect=RuntimeError("boom")):
            assert ce.class_ends("0000101830", _subs([SPRINT_25])) == []

    def test_no_filings_no_request(self):
        with patch.object(ce.sec_edgar, "_get_text") as get:
            assert ce.class_ends("1", _subs([])) == []
            assert ce.class_ends("1", {}) == []
        get.assert_not_called()

    def test_the_submissions_are_read_when_not_given(self):
        with patch.object(ce.sec_edgar, "_submissions", return_value=_subs([SPRINT_25])) as subs, \
             patch.object(ce.sec_edgar, "_get_text", side_effect=_docs):
            assert len(ce.class_ends("0000101830")) == 1
        subs.assert_called_once_with("0000101830")
