"""An issuer's 13D/G statements (app/scraper/sec_13dg.py): the scrape end to end, exits, the
newest denominator.

Part of test_sec_edgar.py until 2026-11-25, when sec_edgar.py was split into modules."""

import pytest

from tests.scraper.sec_statement_fakes import _fixture, _serve


class TestStructuredScrapeEndToEnd:
    """`fetch_ownership_filings` over the modern feed and the XML documents."""

    ATOM = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><category term="SCHEDULE 13G"/><content type="text/xml">
        <filing-href>https://x.test/i.htm</filing-href>
        <filing-date>2026-04-29</filing-date>
        <accession-number>0002100119-26-000139</accession-number>
      </content></entry>
    </feed>"""
    XML_URL = ("https://www.sec.gov/Archives/edgar/data/320193/"
               "000210011926000139/primary_doc.xml")

    def test_a_modern_filing_is_read_from_xml_without_the_index_page(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_13dg_text, sec_client, sec_filer
        pages = {self.XML_URL: _fixture("13g_vanguard.xml")}
        with patch.object(sec_client, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]), \
             patch.object(sec_13dg_text, "_fetch_filing_index") as index:
            res = sec_13dg.fetch_ownership_filings("Apple Inc", "0000320193")
        assert len(res) == 1
        assert res[0]["investor_name"] == "Vanguard Capital Management"
        assert res[0]["stake_percent"] == 7.48
        assert res[0]["is_individual"] is False          # IA is an entity
        assert res[0]["read_from"] == "field"            # named XML fields, not a regex
        index.assert_not_called(), "the XML path must not fetch the index page"

    def test_the_share_class_reaches_the_result(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        pages = {self.XML_URL: _fixture("13g_vanguard.xml")}
        with patch.object(sec_client, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Apple Inc", "0000320193")
        assert res[0]["share_class"] == "Common Stock"

    def test_the_provenance_link_is_still_the_readable_index(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        pages = {self.XML_URL: _fixture("13g_vanguard.xml")}
        with patch.object(sec_client, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Apple Inc", "0000320193")
        assert res[0]["source_url"].endswith("-index.htm")

    def test_the_other_group_members_come_back(self):
        # Wellington files for four entities; three are the group.
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = self.ATOM.replace("0002100119-26-000139", "0000902219-26-000292")
        url = ("https://www.sec.gov/Archives/edgar/data/1120193/"
               "000090221926000292/primary_doc.xml")
        with patch.object(sec_client, "_get_text",
                          side_effect=_serve(atom, {url: _fixture("13ga_wellington.xml")})), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]), \
             patch.object(sec_filer, "fetch_filer_name", return_value="WELLINGTON MANAGEMENT GROUP LLP"):
            res = sec_13dg.fetch_ownership_filings("Nasdaq, Inc.", "1120193")
        assert len(res) == 1
        assert res[0]["investor_name"].lower() == "wellington management group llp"
        assert res[0]["investor_cik"] == "0000902219"
        names = [g["name"] for g in res[0]["group_members"]]
        assert len(names) == 3
        assert "Wellington Management Company LLP" in names
        assert res[0]["investor_name"] not in names, "the filer is not its own group member"

    def test_a_modern_filing_whose_xml_is_missing_falls_back(self):
        # Coverage is not total even post-mandate; the HTML path must still run.
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        html = ('<span class="companyName">Someone Corp (Filed by)</span>'
                '<a href="x">CIK=0000000123</a>'
                '<table><tr><td><a href="/Archives/edgar/data/1/d.htm">d</a></td>'
                '<td>SC 13G</td></tr></table>')
        doc = ("Apple Inc (Name of Issuer) Common Stock (Title of Class of Securities) "
               "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11 6.0%")
        pages = {"https://x.test/i.htm": html,
                 "https://www.sec.gov/Archives/edgar/data/1/d.htm": doc}
        with patch.object(sec_client, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Apple Inc", "0000320193")
        assert len(res) == 1 and res[0]["stake_percent"] == 6.0
        # Pre-2024 filings have no XML, so the cover page is the only place the
        # class is stated — and it is stated on every one of them.
        assert res[0]["share_class"] == "Common Stock"

    def test_an_unverifiable_filing_is_dropped_not_admitted(self):
        # A modern index page offers no SC-13-typed .htm, so primary_url is
        # None and there is nothing to check the issuer against. Admitting it
        # is how the Eve Holding rows got in.
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        html = ('<span class="companyName">Someone Corp (Filed by)</span>'
                '<a href="x">CIK=0000000123</a>'
                '<table><tr><td><a href="/Archives/edgar/data/1/x.htm">x</a></td>'
                '<td>EX-99</td></tr></table>')
        pages = {"https://x.test/i.htm": html}
        with patch.object(sec_client, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Apple Inc", "0000320193")
        assert res == []

    def test_a_dropped_filing_does_not_burn_the_investors_slot(self):
        # Two filings by one investor: the first names the wrong issuer, the
        # second is correct. Claiming the CIK before verification lost the good
        # one — and the wrong-issuer filing is the newer, so it is seen first.
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SC 13D"/><content type="text/xml">
            <filing-href>https://x.test/bad.htm</filing-href>
            <filing-date>2024-06-01</filing-date>
            <accession-number>0000000999-24-000001</accession-number>
          </content></entry>
          <entry><category term="SC 13D"/><content type="text/xml">
            <filing-href>https://x.test/good.htm</filing-href>
            <filing-date>2024-01-01</filing-date>
            <accession-number>0000000999-24-000002</accession-number>
          </content></entry>
        </feed>"""
        idx = ('<span class="companyName">Acme Capital (Filed by)</span>'
               '<a href="x">CIK=0000000555</a>'
               '<table><tr><td><a href="/Archives/edgar/data/1/{d}.htm">d</a></td>'
               '<td>SC 13D</td></tr></table>')
        pages = {
            "https://x.test/bad.htm":  idx.format(d="bad"),
            "https://x.test/good.htm": idx.format(d="good"),
            "https://www.sec.gov/Archives/edgar/data/1/bad.htm":
                "Eve Holding, Inc. (Name of Issuer) PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11 83.0%",
            "https://www.sec.gov/Archives/edgar/data/1/good.htm":
                "Embraer S.A. (Name of Issuer) PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11 9.9%",
        }
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Embraer", "1355444")
        assert [r["stake_percent"] for r in res] == [9.9]

    def test_the_feed_asks_for_both_form_name_eras(self):
        # "SC 13" is a PREFIX match and the forms were renamed to "SCHEDULE 13G"
        # in Dec 2024 — asking for "SC 13" silently returned nothing newer than
        # early 2024. This is the one that made the scraper go blind.
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client
        seen = {}

        def capture(url, params=None):
            if params:
                seen.update(params)
                return "<feed xmlns='http://www.w3.org/2005/Atom'></feed>"
            raise AssertionError("no document should be fetched")

        with patch.object(sec_client, "_get_text", side_effect=capture):
            sec_13dg.fetch_ownership_filings("Apple Inc", "0000320193")
        assert seen["type"] == "SC", f"feed asked for {seen['type']!r}"


class TestAnExitIsNotAZeroPercentHolding:
    """The Vanguard Group's January-2026 realignment moved its holdings to
    subsidiaries that file separately, so its 13G/As report 0 in every power
    row. `fetch_filer_holdings` has always read a zero as an exit; this side
    wrote it as a live "owns 0.0%" edge — eighteen of them on the dev graph."""

    # Both real, both by The Vanguard Group (CIK 0000102909) about GCI Liberty:
    # 10.84% on 2026-01-07, then 0 in every power row on 2026-03-26.
    ZERO_XML = ("https://www.sec.gov/Archives/edgar/data/2057463/"
                "000010290926000394/primary_doc.xml")
    HELD_XML = ("https://www.sec.gov/Archives/edgar/data/2057463/"
                "000010290926000024/primary_doc.xml")

    def _atom(self, *entries: tuple[str, str]) -> str:
        rows = "".join(
            f'<entry><category term="SCHEDULE 13G/A"/><content type="text/xml">'
            f'<filing-href>https://x.test/{acc}.htm</filing-href>'
            f'<filing-date>{date}</filing-date>'
            f'<accession-number>{acc}</accession-number>'
            f'</content></entry>' for acc, date in entries)
        return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{rows}</feed>'

    def test_a_zero_filing_writes_no_holding(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = self._atom(("0000102909-26-000394", "2026-03-26"))
        pages = {self.ZERO_XML: _fixture("13ga_vanguard_exit.xml")}
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("GCI Liberty Inc", "0002057463")
        assert res == [], "a filer reporting nothing is not an owner of 0.0%"

    def test_an_older_holding_is_closed_with_the_exit_date(self):
        """The timeline: the position existed and then ended. Dropping the pair
        outright would lose that it was ever held."""
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = self._atom(("0000102909-26-000394", "2026-03-26"),   # the exit
                          ("0000102909-26-000024", "2026-01-07"))   # 10.84%
        pages = {self.ZERO_XML: _fixture("13ga_vanguard_exit.xml"),
                 self.HELD_XML: _fixture("13ga_vanguard_held.xml")}
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("GCI Liberty Inc", "0002057463")
        assert len(res) == 1
        assert res[0]["stake_percent"] == 10.84
        # the day the position ended — the exit's date of event, 13 days
        # before it was filed on 2026-03-26
        assert res[0]["until"] == "2026-03-13"
        # the structured branch carries the cover's date of event too
        assert res[0]["event_date"] == "2025-12-31"

    def test_the_caller_is_told_who_left(self):
        """Findings item 116: the table pass reads only the statements filed
        since an issuer's ownership table, so the older holding an exit closes
        is never reached — it must still learn who reported no position."""
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = self._atom(("0000102909-26-000394", "2026-03-26"))
        pages = {self.ZERO_XML: _fixture("13ga_vanguard_exit.xml")}
        exits: list[str] = []
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("GCI Liberty Inc", "0002057463", exits=exits)
        assert res == [] and len(exits) == 1 and "vanguard" in exits[0].lower()

    def test_given_filings_are_read_instead_of_the_feed(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        entries = [{"index_url": "https://x.test/held.htm", "form_type": "SCHEDULE 13G/A",
                    "file_date": "2026-01-07", "accession": "0000102909-26-000024"}]
        pages = {self.HELD_XML: _fixture("13ga_vanguard_held.xml")}
        with patch.object(sec_client, "_get_text", side_effect=_serve("no feed is asked for", pages)), \
             patch.object(sec_13dg, "_ownership_feed_entries") as feed, \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("GCI Liberty Inc", "0002057463", entries=entries)
        feed.assert_not_called()
        assert [r["stake_percent"] for r in res] == [10.84]

    def test_the_exit_is_scoped_to_the_cik_that_filed_it(self):
        """The realignment moved holdings from the parent to a subsidiary that
        files separately. The parent's zero must not suppress the subsidiary's
        real position — losing that would be worse than the 0% row was."""
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        apple_zero = ("https://www.sec.gov/Archives/edgar/data/320193/"
                      "000010290926000394/primary_doc.xml")
        atom = self._atom(("0000102909-26-000394", "2026-03-26"),      # parent, 0%
                          ("0002100119-26-000139", "2026-04-29"))      # subsidiary, 7.48%
        # The exit fixture names GCI Liberty, so serve it where the issuer
        # matches: this test is about CIK scoping, not issuer verification.
        pages = {apple_zero: _fixture("13ga_vanguard_exit.xml"),
                 ("https://www.sec.gov/Archives/edgar/data/320193/"
                  "000210011926000139/primary_doc.xml"): _fixture("13g_vanguard.xml")}
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Apple Inc", "0000320193")
        assert [(r["investor_name"], r["stake_percent"]) for r in res] == \
               [("Vanguard Capital Management", 7.48)]

    def test_a_live_holding_carries_no_until(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = self._atom(("0000102909-26-000024", "2026-01-07"))
        pages = {self.HELD_XML: _fixture("13ga_vanguard_held.xml")}
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("GCI Liberty Inc", "0002057463")
        assert res[0]["until"] is None

    def test_a_bloc_member_with_no_individual_stake_is_not_an_exit(self):
        """BRC can dispose of nothing alone — null stake beside a real 52.3%
        bloc. Reading that as "holds nothing" would delete the voting group the
        whole 13D model is built on."""
        from app.scraper.sec_13dg_text import _split_stake
        pct, voting = _split_stake({"sole_dispositive": 0, "shared_voting": 1_020_598_157},
                                   None, 52.3, in_group=True)
        assert pct is None and voting == 52.3


class TestFilerIsNotTheFirstBlock:
    """Findings item 21 (b): the filer's CIK goes with the filer's own block."""

    ATOM = TestStructuredScrapeEndToEnd.ATOM.replace("0002100119-26-000139", "0001193125-25-283610")
    URL = "https://www.sec.gov/Archives/edgar/data/1014473/000119312525283610/primary_doc.xml"

    def _run(self, filer_name):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        with patch.object(sec_client, "_get_text",
                          side_effect=_serve(self.ATOM, {self.URL: _fixture("13ga_berkshire_verisign.xml")})), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]), \
             patch.object(sec_filer, "fetch_filer_name", return_value=filer_name):
            return sec_13dg.fetch_ownership_filings("VeriSign, Inc.", "1014473")

    def test_berkshires_13g_is_berkshires_not_buffetts(self):
        res = self._run("BERKSHIRE HATHAWAY INC")
        assert len(res) == 1
        row = res[0]
        assert row["investor_name"] == "Berkshire Hathaway Inc."
        assert row["investor_cik"] == "0001067983"
        assert row["is_individual"] is None                # HC: the runner decides by the name
        members = {g["name"]: g["cik"] for g in row["group_members"]}
        assert members == {"Warren E. Buffett": None, "National Indemnity Company": None}

    def test_without_the_filers_name_nothing_is_written(self):
        assert self._run(None) == []


class TestNewestDenominator:
    """A stake is `shares / shares_outstanding`; only the count is the
    holder's. Bevco's last 13D/A on AB InBev (2020) says 102,862,718 of
    1,730,242,027 = 5.9%; the company's newest schedule (BRC, 2026) states
    1,972,133,054. The real figures from the three filings."""

    ORD = "Ordinary Shares, without nominal value"
    ORD_ADS = ("Ordinary Shares, without nominal value and American Depositary Shares, "
               "each of which represents one (1) Ordinary Share, without nominal value")

    def filings(self):
        return [
            {"investor_name": "Brc S.A R.L.", "file_date": "2026-05-15", "form_type": "SCHEDULE 13D/A",
             "stake_percent": None, "voting_power_pct": 52.3, "shares": 769317631,
             "shares_outstanding": 1972133054, "share_class": self.ORD_ADS, "until": None},
            {"investor_name": "Altria Group, Inc.", "file_date": "2025-02-07", "form_type": "SCHEDULE 13D/A",
             "stake_percent": 8.0965, "voting_power_pct": 51.9, "shares": 159121937,
             "shares_outstanding": 1965328900, "share_class": self.ORD, "until": None},
            {"investor_name": "Bevco Lux S.A.R.L.", "file_date": "2020-03-10", "form_type": "SC 13D/A",
             "stake_percent": 5.9, "voting_power_pct": None, "shares": 102862718,
             "shares_outstanding": 1730242027, "share_class": self.ORD, "until": None},
        ]

    def test_the_class_key_sees_through_the_wording(self):
        from app.scraper.sec_13dg import _class_key
        assert _class_key(self.ORD) == _class_key(self.ORD_ADS) == "ordinary shares"
        assert _class_key("Common Stock, par value $0.001 per share") == "common stock"
        assert _class_key("Class A Common Stock") != _class_key("Class B Common Stock")
        assert _class_key(None) == ""

    def test_older_counts_are_divided_by_the_newest_total(self):
        from app.scraper.sec_13dg import _restate_against_newest_denominator
        fs = self.filings()
        _restate_against_newest_denominator(fs)
        brc, altria, bevco = fs
        assert bevco["stake_percent"] == pytest.approx(5.216, abs=0.001)    # 102,862,718 / 1,972,133,054
        assert bevco["shares_outstanding"] == 1972133054
        assert bevco["denominator_date"] == "2026-05-15"
        assert bevco["ownership_type"] == "minority"
        assert altria["stake_percent"] == pytest.approx(8.0685, abs=0.001)
        assert altria["voting_power_pct"] == 51.9                           # the bloc is the bloc
        # the newest filing is its own denominator; a bloc-only row has no stake to restate
        assert brc["stake_percent"] is None and brc["denominator_date"] is None

    def test_what_is_left_alone(self):
        from app.scraper.sec_13dg import _restate_against_newest_denominator
        fs = self.filings()
        fs[2]["share_class"] = "Class B Common Stock"                         # another class
        fs[1]["until"] = "2025-06-01"                                         # an ended holding
        _restate_against_newest_denominator(fs)
        assert fs[2]["stake_percent"] == 5.9 and fs[2]["denominator_date"] is None
        assert fs[1]["stake_percent"] == 8.0965 and fs[1]["denominator_date"] is None

    def test_not_across_a_split(self):
        from app.scraper.sec_13dg import _restate_against_newest_denominator
        fs = self.filings()
        fs[0]["shares_outstanding"] = 1730242027 * 3                         # a 3-for-1 split since 2020
        fs[1]["shares_outstanding"] = 1730242027 * 3
        _restate_against_newest_denominator(fs)
        assert fs[2]["stake_percent"] == 5.9 and fs[2]["denominator_date"] is None

    def test_a_same_day_or_equal_total_changes_nothing(self):
        from app.scraper.sec_13dg import _restate_against_newest_denominator
        fs = self.filings()
        fs[2]["file_date"] = "2026-05-15"
        _restate_against_newest_denominator(fs)
        assert fs[2]["stake_percent"] == 5.9
        fs = self.filings()
        fs[2]["shares_outstanding"] = 1972133054
        _restate_against_newest_denominator(fs)
        assert fs[2]["stake_percent"] == 5.9 and fs[2]["denominator_date"] is None

    def test_the_newest_total_wins_whatever_the_order(self):
        # EDGAR lists newest first, but nothing should depend on that
        from app.scraper.sec_13dg import _restate_against_newest_denominator
        fs = list(reversed(self.filings()))
        _restate_against_newest_denominator(fs)
        bevco = next(f for f in fs if f["investor_name"].startswith("Bevco"))
        assert bevco["denominator_date"] == "2026-05-15"
        assert bevco["shares_outstanding"] == 1972133054


class TestRestatementNeedsACountThatIsTheStake:
    def test_a_count_that_is_not_the_filed_stake_is_not_divided_again(self):
        # the old SoftBank row: 3.3 % filed, a count that is 0.02 % of its total
        from app.scraper.sec_13dg import _restate_against_newest_denominator
        fs = [{"investor_name": "Newer Filer", "file_date": "2026-05-15", "stake_percent": 6.0,
               "shares": 1_200_000_000, "shares_outstanding": 20_000_000_000,
               "share_class": "Ordinary shares", "until": None, "form_type": "SCHEDULE 13G"},
              {"investor_name": "Softbank Group Corp", "file_date": "2025-05-15", "stake_percent": 3.3,
               "shares": 3_788_048, "shares_outstanding": 19_114_858_909,
               "share_class": "Ordinary shares", "until": None, "form_type": "SCHEDULE 13G/A"}]
        _restate_against_newest_denominator(fs)
        assert fs[1]["stake_percent"] == 3.3 and fs[1]["denominator_date"] is None
        # the same row with its whole holding is restated
        fs[1]["shares"] = 630_790_344
        _restate_against_newest_denominator(fs)
        assert fs[1]["stake_percent"] == pytest.approx(3.154, abs=0.001)
        assert fs[1]["denominator_date"] == "2026-05-15"

    def test_rounding_in_the_filed_percentage_is_no_mismatch(self):
        # Bevco: 102,862,718 / 1,730,242,027 = 5.945 %, filed as "5.9"
        from app.scraper.sec_13dg import _restate_against_newest_denominator
        fs = [{"investor_name": "New", "file_date": "2026-05-15", "stake_percent": None,
               "voting_power_pct": 52.3, "shares": 1, "shares_outstanding": 1972133054,
               "share_class": "Ordinary Shares", "until": None, "form_type": "SCHEDULE 13D/A"},
              {"investor_name": "Bevco", "file_date": "2020-03-10", "stake_percent": 5.9,
               "shares": 102862718, "shares_outstanding": 1730242027,
               "share_class": "Ordinary Shares", "until": None, "form_type": "SC 13D/A"}]
        _restate_against_newest_denominator(fs)
        assert fs[1]["denominator_date"] == "2026-05-15"

    def test_an_html_group_member_keeps_the_bloc_rule(self):
        # Two cover pages on a 13D = a bloc: the member's shared rows are the
        # group's, so its count is its sole row, not sole + shared.
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SC 13D/A"/><content type="text/xml">
            <filing-href>https://x.test/i.htm</filing-href>
            <filing-date>2019-03-01</filing-date>
            <accession-number>0000000000-19-000001</accession-number>
          </content></entry>
        </feed>"""
        index = ('<span class="companyName">Member Holdings (Filed by)</span> '
                 '<a href="x">CIK=0000000111</a>'
                 '<table><tr><td><a href="/Archives/edgar/data/1/d.htm">doc</a>'
                 '</td><td>SC 13D/A</td></tr></table>')
        page = ("Name of Reporting Persons {n} Sole Voting Power 0 Shared Voting Power 900,000 "
                "Sole Dispositive Power 100,000 Shared Dispositive Power 50,000 "
                "Aggregate Amount Beneficially Owned 900,000 "
                "Percent of Class Represented by Amount in Row 11 9.0% ")
        doc = ("Example Issuer Inc (Name of Issuer) based on a total of 10,000,000 shares issued "
               "and outstanding. " + page.format(n="Member Holdings") + page.format(n="Other Member"))
        pages = {None: atom, "https://x.test/i.htm": index,
                 "https://www.sec.gov/Archives/edgar/data/1/d.htm": doc}
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Example Issuer Inc", "0000000222")
        assert res and res[0]["shares"] == 100_000


class TestAnUnreadCoverIsNotAnExit:
    """`not pct` read "the parser found nothing" as "the holder holds
    nothing", and closed live holdings on that filing's date."""

    @staticmethod
    def _run(docs: dict, entries: list[tuple[str, str, str]]):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        rows = "".join(
            f'<entry><category term="{form}"/><content type="text/xml">'
            f'<filing-href>https://x.test/{acc}.htm</filing-href>'
            f'<filing-date>{date}</filing-date><accession-number>{acc}</accession-number>'
            f'</content></entry>' for acc, date, form in entries)
        atom = f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{rows}</feed>'
        pages = {None: atom}
        for acc, (doc, _) in docs.items():
            pages[f"https://x.test/{acc}.htm"] = (
                '<span class="companyName">Holder Fund LP (Filed by)</span> '
                '<a href="x">CIK=0000000777</a><table><tr><td>'
                f'<a href="/Archives/edgar/data/1/{acc}.htm">doc</a></td><td>SC 13G</td></tr></table>')
            pages[f"https://www.sec.gov/Archives/edgar/data/1/{acc}.htm"] = doc
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            return sec_13dg.fetch_ownership_filings("Example Issuer Inc", "0000000222")

    COVER = ("Example Issuer Inc (Name of Issuer) {date} (Date of Event Which Requires Filing "
             "of this Statement) Name of Reporting Persons Holder Fund LP "
             "Sole Voting Power 0 Shared Voting Power 0 Sole Dispositive Power {n} "
             "Shared Dispositive Power 0 {pct}")

    def test_a_cover_without_a_percentage_falls_back_to_the_older_filing(self):
        newest = self.COVER.format(date="March 1, 2026", n="600,000", pct="(no percent row here)")
        older = self.COVER.format(date="March 1, 2025", n="600,000",
                                  pct="Percent of Class Represented by Amount in Row 11 6.0%")
        res = self._run({"a2": (newest, 0), "a1": (older, 0)},
                        [("a2", "2026-03-05", "SC 13G/A"), ("a1", "2025-03-05", "SC 13G/A")])
        assert len(res) == 1 and res[0]["stake_percent"] == 6.0
        assert res[0]["until"] is None, "an unread cover closed a live holding"

    def test_a_stated_zero_is_still_an_exit_dated_by_the_oldest_zero(self):
        z2 = self.COVER.format(date="June 30, 2026", n="0",
                               pct="Percent of Class Represented by Amount in Row 11 0%")
        z1 = self.COVER.format(date="March 31, 2026", n="0",
                               pct="Percent of Class Represented by Amount in Row 11 0.0%")
        held = self.COVER.format(date="December 31, 2025", n="600,000",
                                 pct="Percent of Class Represented by Amount in Row 11 6.0%")
        res = self._run({"z2": (z2, 0), "z1": (z1, 0), "h": (held, 0)},
                        [("z2", "2026-07-10", "SC 13G/A"), ("z1", "2026-04-10", "SC 13G/A"),
                         ("h", "2026-01-10", "SC 13G/A")])
        assert len(res) == 1 and res[0]["stake_percent"] == 6.0
        assert res[0]["until"] == "2026-03-31"     # the first zero's date of event, not the later repeat

    def test_the_filer_side_skips_an_unread_percentage(self):
        from unittest.mock import patch
        from app.scraper import sec_holdings
        filings = [{"form": "SCHEDULE 13G/A", "accession": "a-2", "date": "2026-05-01"},
                   {"form": "SCHEDULE 13G", "accession": "a-1", "date": "2025-05-01"}]
        parsed = {"a-2": {"subject_cik": "0000000222", "subject_name": "Example", "percent": None,
                          "accession": "a-2", "event_date": "2026-03-31", "read_from": "field"},
                  "a-1": {"subject_cik": "0000000222", "subject_name": "Example", "percent": 6.0,
                          "stake": 6.0, "voting": None, "accession": "a-1", "event_date": "2025-03-31", "read_from": "field"}}
        with patch.object(sec_holdings, "_iter_filing_pages", return_value=iter([filings])), \
             patch.object(sec_holdings, "_parse_holding_filing", side_effect=lambda c, a, n=None: parsed[a]):
            rows = sec_holdings.fetch_filer_holdings("0000000777")
        assert len(rows) == 1 and rows[0]["stake_percent"] == 6.0 and rows[0]["until"] is None
        assert (rows[0]["since"], rows[0]["since_basis"]) == ("2025-03-31", None)
