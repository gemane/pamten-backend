"""Reading a 13D/G cover page and text (app/scraper/sec_13dg_text.py): the issuer check, the
stake arithmetic, joint filings, type codes, the date of event, the final report.

Part of test_sec_edgar.py until 2026-11-25, when sec_edgar.py was split into modules."""

import pytest

from app.scraper.sec_lookup import _ticker_normalize
from tests.scraper.sec_statement_fakes import _fixture, _serve
# the module, not the classes: a test class imported by name would be collected here a second time
from tests.scraper import test_sec_13dg


class TestIssuerVerification:
    """The wrong-subject bug: EDGAR's index metadata can name the wrong company.

    Embraer's agent filed the Eve Holding 13D/A with EMBRAER S.A. as SUBJECT
    COMPANY, so the index page, the SGML header and the accession prefix all
    agreed on the wrong company — and the graph gained "Embraer Aircraft
    Holding owns 83% of Embraer" (the real statement: 83% of Eve Holding).
    The cover page of the document is the only field that told the truth, so
    that is what gets verified now, for every filing an edge would come from.

    Cover snippets below are from the real filings.
    """

    EVE_COVER = ("SCHEDULE 13D Under the Securities Exchange Act of 1934 "
                 "(Amendment No. 2)* Eve Holding, Inc. (Name of Issuer) "
                 "Common Stock (Title of Class of Securities)")
    ABI_COVER = ("SCHEDULE 13D/A Anheuser-Busch InBev SA/NV (Name of Issuer) "
                 "Ordinary Shares (Title of Class of Securities)")
    TXT_COVER = ("SCHEDULE 13G EMBRAER S.A. ------------------------------ "
                 "(Name of Issuer)")

    def test_extracts_the_issuer_from_the_cover(self):
        from app.scraper.sec_13dg_text import _parse_issuer_from_text
        assert _parse_issuer_from_text(self.EVE_COVER) == "Eve Holding, Inc"

    def test_boilerplate_before_the_name_is_trimmed(self):
        # The capture reaches back into "…Act of 1934 (Amendment No. 2)*"; only
        # the company name may survive.
        from app.scraper.sec_13dg_text import _parse_issuer_from_text
        got = _parse_issuer_from_text(self.EVE_COVER)
        assert "1934" not in got and "Amendment" not in got

    def test_text_format_underline_rules_are_trimmed(self):
        from app.scraper.sec_13dg_text import _parse_issuer_from_text
        assert _parse_issuer_from_text(self.TXT_COVER) == "EMBRAER S.A"

    def test_a_coverless_document_yields_none(self):
        from app.scraper.sec_13dg_text import _parse_issuer_from_text
        assert _parse_issuer_from_text("no cover page here at all") is None

    def test_the_wrong_issuer_is_a_mismatch(self):
        # The bug, distilled: a filing about Eve Holding must not survive a
        # scrape of Embraer.
        from app.scraper.sec_13dg_text import _issuer_matches, _parse_issuer_from_text
        issuer = _parse_issuer_from_text(self.EVE_COVER)
        assert _issuer_matches("Embraer", issuer) is False

    def test_legal_form_variants_agree(self):
        from app.scraper.sec_13dg_text import _issuer_matches
        assert _issuer_matches("Embraer", "EMBRAER S.A")
        assert _issuer_matches("Anheuser-Busch InBev", "Anheuser-Busch InBev SA/NV")
        assert _issuer_matches("Alphabet", "Alphabet Inc.")

    def test_diacritics_do_not_reject_a_real_owner(self):
        # "Nestlé" must tokenize to "nestle", not split on the é into a stump —
        # otherwise every legitimate filing about an accented-name company is
        # silently thrown away. This is a loss-of-owners bug, the exact harm
        # the verification must never cause.
        from app.scraper.sec_13dg_text import _issuer_matches
        assert _issuer_matches("Nestlé", "Nestle S.A.") is True
        assert _issuer_matches("Nestle", "Nestlé S.A.") is True

    def test_a_renamed_company_keeps_its_old_filings(self):
        # EDGAR keeps the CIK through a rename; covers from before it carry the
        # old name. The caller passes current + former names, and agreement with
        # ANY of them keeps the filing.
        from app.scraper.sec_13dg_text import _issuer_matches
        assert _issuer_matches(["Meta Platforms", "Facebook Inc"],
                               "Facebook, Inc.") is True
        assert _issuer_matches(["Meta Platforms", "Facebook Inc"],
                               "Eve Holding, Inc") is False

    def test_former_names_are_consulted_in_the_scan(self):
        # End to end: a filing whose cover names the OLD name survives a scrape
        # under the new one, because fetch_former_names is folded in.
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer

        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SC 13G"/><content type="text/xml">
            <filing-href>https://x.test/old-index.htm</filing-href>
            <filing-date>2021-02-01</filing-date>
            <accession-number>0001104659-21-000001</accession-number>
          </content></entry>
        </feed>"""
        old_index = ('<span class="companyName">Vanguard Group Inc (Filed by)'
                     '</span> <a href="x">CIK=0000102909</a>'
                     '<table><tr><td><a href="/Archives/edgar/data/1/old.htm">doc</a>'
                     '</td><td>SC 13G</td></tr></table>')
        old_doc = ("SCHEDULE 13G Facebook, Inc. (Name of Issuer) "
                   "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11: 7.0% ")
        pages = {
            None: atom,
            "https://x.test/old-index.htm": old_index,
            "https://www.sec.gov/Archives/edgar/data/1/old.htm": old_doc,
        }
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names",
                          return_value=["Facebook Inc"]) as former:
            results = sec_13dg.fetch_ownership_filings("Meta Platforms", "1326801")

        former.assert_called_once_with("1326801")
        assert [r["investor_name"] for r in results] == ["Vanguard Group Inc"]
        assert results[0]["stake_percent"] == 7.0
        # a legacy cover page: the numbers came out of the form's numbered
        # rows in its text — the SEC's labels, the filer's figure
        assert results[0]["read_from"] == "form"

    def test_no_issuer_is_not_a_mismatch(self):
        # Old text filings may not parse. A positive mismatch is the only safe
        # ground to throw a filing away.
        from app.scraper.sec_13dg_text import _issuer_matches
        assert _issuer_matches("Embraer", None) is True

    def test_pure_legal_noise_never_decides(self):
        # A name that normalizes to nothing ("The Company Inc") must not veto —
        # there is no identity in it to disagree with. Both directions: as the
        # filing's issuer, and as the scraped company's own name.
        from app.scraper.sec_13dg_text import _issuer_matches
        assert _issuer_matches("Embraer", "The Company Inc.") is True
        assert _issuer_matches("The Group Inc", "Eve Holding, Inc") is True
        assert _issuer_matches(["The Group Inc", "Holdings Co"], "Eve Holding, Inc") is True

    def test_a_mismatched_filing_produces_no_investor(self):
        # End to end through fetch_ownership_filings: the Eve filing arrives via
        # the Atom feed exactly as the real one did (agent accession prefix, so
        # the outbound pre-filter does not catch it) and must be dropped at the
        # document stage; the BlackRock filing survives with its stake parsed.
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client

        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SC 13D/A"/><content type="text/xml">
            <filing-href>https://x.test/eve-index.htm</filing-href>
            <filing-date>2024-09-05</filing-date>
            <accession-number>0001292814-24-003347</accession-number>
          </content></entry>
          <entry><category term="SC 13G"/><content type="text/xml">
            <filing-href>https://x.test/blk-index.htm</filing-href>
            <filing-date>2023-02-01</filing-date>
            <accession-number>0001306550-23-008505</accession-number>
          </content></entry>
          <entry><category term="SC 13D"/><content type="text/xml">
            <filing-href>https://x.test/self-index.htm</filing-href>
            <filing-date>2025-01-01</filing-date>
            <accession-number>0001292814-25-000001</accession-number>
          </content></entry>
        </feed>"""
        eve_index = ('<span class="companyName">Embraer Aircraft Holding, Inc. (Filed by)'
                     '</span> <a href="x">CIK=0001926968</a>'
                     '<table><tr><td><a href="/Archives/edgar/data/1/eve.htm">doc</a>'
                     '</td><td>SC 13D/A</td></tr></table>')
        blk_index = ('<span class="companyName">BlackRock Inc. (Filed by)'
                     '</span> <a href="x">CIK=0001364742</a>'
                     '<table><tr><td><a href="/Archives/edgar/data/1/blk.htm">doc</a>'
                     '</td><td>SC 13G</td></tr></table>')
        # The company filing about itself, through an agent: the accession
        # prefix is the agent's CIK so the pre-filter misses it, and its cover
        # names the company itself so the issuer check passes it. Only the
        # investor-CIK guard stands between this and "Embraer owns Embraer" —
        # and that guard compares zero-padded CIKs against an UNPADDED
        # company_cik, which never matched until it was normalised.
        self_index = ('<span class="companyName">EMBRAER S.A. (Filed by)'
                      '</span> <a href="x">CIK=0001355444</a>'
                      '<table><tr><td><a href="/Archives/edgar/data/1/self.htm">doc</a>'
                      '</td><td>SC 13D</td></tr></table>')
        blk_doc = (self.TXT_COVER +
                   " PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11: 5.7% "
                   " TYPE OF REPORTING PERSON  CO ")

        pages = {
            "https://x.test/eve-index.htm": eve_index,
            "https://x.test/blk-index.htm": blk_index,
            "https://x.test/self-index.htm": self_index,
            "https://www.sec.gov/Archives/edgar/data/1/self.htm":
                self.TXT_COVER + " PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11: 34.6% ",
            "https://www.sec.gov/Archives/edgar/data/1/eve.htm":
                self.EVE_COVER + " PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11: 83.0% ",
            "https://www.sec.gov/Archives/edgar/data/1/blk.htm": blk_doc,
        }


        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)):
            results = sec_13dg.fetch_ownership_filings("Embraer", "1355444")

        names = [r["investor_name"] for r in results]
        assert "Embraer Aircraft Holding, Inc." not in str(names), \
            "the Eve Holding filing leaked through as an owner of Embraer"
        assert names == ["Blackrock Inc."], \
            "the company's own agent-filed 13D leaked through as an owner of itself"
        assert results[0]["stake_percent"] == 5.7
        assert results[0]["is_individual"] is False


class TestBeneficialOwnershipVsRealStake:
    """AB InBev summed to 109.9% because "beneficial ownership" is about power,
    not property: every member of a voting group reports the WHOLE group's
    shares in row 11, so summing row-13 percentages counts the same shares once
    per member. Altria and the Stichting each reported the same billion shares.

    The cover page's power rows tell the truth. Numbers below are the real ones
    from Altria's SC 13D/A (Amendment No. 6, September 2024).
    """

    ALTRIA = ("Sole Voting Power 0 Shared Voting Power 1,020,598,157 "
              "Sole Dispositive Power 159,121,937 Shared Dispositive Power 0 "
              "Percent of Class Represented by Amount in Row 11 51.7% "
              "based on a total of 1,975,913,221 Voting Shares issued and outstanding.")
    LONE = ("Sole Voting Power 31,554,913 Shared Voting Power 0 "
            "Sole Dispositive Power 32,416,315 Shared Dispositive Power 0 "
            "Percent of Class Represented by Amount in Row 11 5.7%")
    JOINT_ONLY = ("Sole Voting Power 0 Shared Voting Power 1,033,081,237 "
                  "Sole Dispositive Power 0 Shared Dispositive Power 771,096,582 "
                  "Percent of Class Represented by Amount in Row 11 52.3% "
                  "based on a total of 1,975,913,221 shares issued and outstanding.")

    def test_the_power_rows_are_read(self):
        from app.scraper.sec_13dg_text import _parse_power_rows
        rows = _parse_power_rows(self.ALTRIA)
        assert rows == {"sole_voting": 0, "shared_voting": 1020598157,
                        "sole_dispositive": 159121937, "shared_dispositive": 0}

    def test_a_missing_row_is_absent_not_zero(self):
        # "the filing did not say" must not become a genuine 0% stake.
        from app.scraper.sec_13dg_text import _parse_power_rows
        assert "sole_dispositive" not in _parse_power_rows("Sole Voting Power 5,000")

    def test_the_denominator_comes_from_the_filing(self):
        from app.scraper.sec_13dg_text import _shares_outstanding
        assert _shares_outstanding(self.ALTRIA) == 1975913221
        assert _shares_outstanding("no denominator here") is None

    def test_a_group_member_gets_its_own_stake_and_the_bloc_as_voting(self):
        # The heart of it: Altria's real holding is 159.1M/1.976B = 8.05%, and
        # the 51.7% is the Voting Agreement bloc, not Altria's shares.
        from app.scraper.sec_13dg_text import _own_stake_and_voting
        stake, voting = _own_stake_and_voting(self.ALTRIA, 51.7)
        assert stake == pytest.approx(8.05, abs=0.01)
        assert voting == 51.7

    def test_a_lone_filer_is_untouched(self):
        # No group, no bloc: the common case must not change at all.
        from app.scraper.sec_13dg_text import _own_stake_and_voting
        assert _own_stake_and_voting(self.LONE, 5.7) == (5.7, None)

    def test_a_purely_joint_holder_gets_no_invented_stake(self):
        # BRC can dispose of nothing alone — its shares sit in the Stichting it
        # co-owns with EPS. 0.0 would read as "owns nothing", the opposite of
        # the truth, so the stake is unknown and only the bloc is stated.
        from app.scraper.sec_13dg_text import _own_stake_and_voting
        stake, voting = _own_stake_and_voting(self.JOINT_ONLY, 52.3)
        assert stake is None
        assert voting == 52.3

    def test_a_lone_custodian_with_all_shared_power_keeps_its_stake(self):
        # State Street over News Corporation: sole 0, everything shared (held
        # for client accounts), aggregate 8.6M = 4.7% of class. A lone filer,
        # so 4.7% is its real stake, not a phantom bloc. This is the bug the
        # in_group flag fixes — before it, the stake was nulled.
        from app.scraper.sec_13dg_text import _split_stake
        rows = {"sole_voting": 0, "shared_voting": 6878465,
                "sole_dispositive": 0, "shared_dispositive": 8593355}
        assert _split_stake(rows, 182981979, 4.7, in_group=False) == (4.7, None)

    def test_the_same_rows_in_a_group_still_lose_the_stake(self):
        # Identical row shape, but with co-filers → a genuine bloc member whose
        # reported shares are the whole group's. Must stay null + bloc.
        from app.scraper.sec_13dg_text import _split_stake
        rows = {"sole_voting": 0, "shared_voting": 6878465,
                "sole_dispositive": 0, "shared_dispositive": 8593355}
        assert _split_stake(rows, 182981979, 4.7, in_group=True) == (None, 4.7)

    def test_stake_from_person_reads_the_co_filer_count(self):
        # One reporting person → lone custodian → real stake; two → group.
        from app.scraper.sec_13dg_xml import _stake_from_person
        person = {"name": "State Street Corp", "sole_voting": 0,
                  "shared_voting": 6878465, "sole_dispositive": 0,
                  "shared_dispositive": 8593355, "aggregate": 8600153,
                  "percent": 4.7}
        lone = {"comment_text": "based on a total of 182,981,979 shares "
                "issued and outstanding", "persons": [person]}
        assert _stake_from_person(lone, person) == (4.7, None)
        grouped = {**lone, "persons": [person, {"name": "Co-Filer LLC"}]}
        assert _stake_from_person(grouped, person) == (None, 4.7)

    def test_no_denominator_means_no_invented_stake(self):
        # The bloc is real but unquantifiable per member; keeping the bloc
        # figure as the stake is exactly what produced 109.9%.
        from app.scraper.sec_13dg_text import _own_stake_and_voting
        text = self.ALTRIA.split("based on")[0]
        assert _own_stake_and_voting(text, 51.7) == (None, 51.7)

    def test_the_sums_stop_exceeding_the_company(self):
        # The regression this exists to prevent, in one assertion: the three
        # real AB InBev filings can no longer add up to more than 100%.
        from app.scraper.sec_13dg_text import _own_stake_and_voting
        bevco = ("Sole Voting Power 0 Shared Voting Power 102,862,718 "
                 "Sole Dispositive Power 102,862,718 Shared Dispositive Power 0 "
                 "Percent of Class Represented by Amount in Row 11 5.9%")
        stakes = [_own_stake_and_voting(t, p)[0] for t, p in
                  ((self.ALTRIA, 51.7), (self.JOINT_ONLY, 52.3), (bevco, 5.9))]
        assert sum(s for s in stakes if s is not None) < 100

    def test_the_bloc_reaches_the_scrape_result(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer

        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SC 13D/A"/><content type="text/xml">
            <filing-href>https://x.test/i.htm</filing-href>
            <filing-date>2024-09-27</filing-date>
            <accession-number>0001193125-24-230346</accession-number>
          </content></entry>
        </feed>"""
        index = ('<span class="companyName">Altria Group, Inc. (Filed by)'
                 '</span> <a href="x">CIK=0000764180</a>'
                 '<table><tr><td><a href="/Archives/edgar/data/1/d.htm">doc</a>'
                 '</td><td>SC 13D/A</td></tr></table>')
        doc = "Anheuser-Busch InBev SA/NV (Name of Issuer) " + self.ALTRIA
        pages = {None: atom, "https://x.test/i.htm": index,
                 "https://www.sec.gov/Archives/edgar/data/1/d.htm": doc}

        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Anheuser-Busch InBev", "1668717")

        assert len(res) == 1
        assert res[0]["stake_percent"] == pytest.approx(8.05, abs=0.01)
        assert res[0]["voting_power_pct"] == 51.7

    def test_the_date_of_event_reaches_the_scrape_result(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SC 13D/A"/><content type="text/xml">
            <filing-href>https://x.test/i.htm</filing-href>
            <filing-date>2024-09-27</filing-date>
            <accession-number>0001193125-24-230346</accession-number>
          </content></entry>
        </feed>"""
        index = ('<span class="companyName">Altria Group, Inc. (Filed by)'
                 '</span> <a href="x">CIK=0000764180</a>'
                 '<table><tr><td><a href="/Archives/edgar/data/1/d.htm">doc</a>'
                 '</td><td>SC 13D/A</td></tr></table>')
        doc = ("Anheuser-Busch InBev SA/NV (Name of Issuer) September 25, 2024 "
               "(Date of Event Which Requires Filing of this Statement) " + self.ALTRIA)
        pages = {None: atom, "https://x.test/i.htm": index,
                 "https://www.sec.gov/Archives/edgar/data/1/d.htm": doc}
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Anheuser-Busch InBev", "1668717")
        assert res[0]["event_date"] == "2024-09-25"
        assert res[0]["file_date"] == "2024-09-27"          # said two days later


class TestClassTitleFromCoverPages:
    def test_the_title_is_read_off_a_cover(self):
        from app.scraper.sec_13dg_text import _parse_class_title_from_text
        assert _parse_class_title_from_text(
            "Anheuser-Busch InBev SA/NV (Name of Issuer) "
            "Ordinary Shares, without nominal value (Title of Class of Securities)"
        ) == "Ordinary Shares, without nominal value"

    def test_the_issuer_line_is_not_swallowed_into_it(self):
        from app.scraper.sec_13dg_text import _parse_class_title_from_text
        got = _parse_class_title_from_text(
            "SCHEDULE 13D Grupo Televisa (Name of Issuer) "
            "Series A Shares (Title of Class of Securities)")
        assert got == "Series A Shares"

    def test_a_cover_without_the_label_yields_nothing(self):
        from app.scraper.sec_13dg_text import _parse_class_title_from_text
        assert _parse_class_title_from_text("no class label anywhere here") is None


class TestTheFinalReport:
    """Findings item 35: a 13D/G below 5 % is the filer's last report — Item 5,
    "Ownership of 5 Percent or Less of a Class" — not a sale. It was written as
    a current holding (BlackRock, Inc. → Tesla 4.96 %, 13G/A 2026-10-27)."""

    TESLA_XML = ("https://www.sec.gov/Archives/edgar/data/1318605/"
                 "000201238326003852/primary_doc.xml")

    def test_a_13g_that_ticks_item_5(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        d = _parse_13dg_xml(_fixture("13ga_blackrock_tesla_final.xml"))
        assert d["final_report"] is True and d["event_date"] == "2026-09-30"
        assert d["persons"][0]["percent"] == 4.96

    def test_an_empty_item_5_is_no_final_report(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        assert _parse_13dg_xml(_fixture("13g_vanguard.xml"))["final_report"] is None

    def test_a_13d_states_the_day_it_ceased_in_item_5e(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        ceased = _fixture("13d_tactical.xml").replace(
            "<item5/>", "<item5><date5PercentOwnership>03/15/2026</date5PercentOwnership></item5>")
        assert _parse_13dg_xml(ceased)["final_report"] == "2026-03-15"
        # Bevco on Life Time, 4.4 %: "Not applicable." — no day, not final
        open_ = _fixture("13d_tactical.xml").replace(
            "<item5/>", "<item5><date5PercentOwnership>Not applicable.</date5PercentOwnership></item5>")
        assert _parse_13dg_xml(open_)["final_report"] is None

    @pytest.mark.parametrize("item5, final", [
        # the five spellings of the real text covers (2009–2020)
        ("check the following [X].", True),
        ("check the following: ý", True),                                     # Fir Tree, Wingdings
        ("check the following x .", True),                                     # Columbia Wanger
        ("check the following [_].", None),                                    # Citadel, 4.2 % unticked
        ("check the following [ ].", None),
        ("check the following.", None),                                        # Barclays 2009: no mark
    ])
    def test_the_box_of_a_text_cover(self, item5, final):
        from app.scraper.sec_13dg_text import _parse_final_report_from_text
        text = ("<p>Item 5. Ownership of Five Percent or Less of a Class</p><p>If this statement is "
                "being filed to report the fact that as of the date hereof the reporting person has "
                f"ceased to be the beneficial owner of more than five percent of the class of securities, "
                f"{item5}</p><p>Item 6. Ownership of More than Five Percent on Behalf of Another Person</p>")
        assert _parse_final_report_from_text(text) is final

    def test_a_text_cover_that_says_it_outright(self):
        # T. Rowe Price's covers: no box, the fact stated
        from app.scraper.sec_13dg_text import _parse_final_report_from_text
        assert _parse_final_report_from_text(
            "Item 5: Ownership of Five Percent or Less of a Class This statement is being filed to report "
            "the fact that, as of the date of this report, the reporting person(s) has (have) ceased to be "
            "the beneficial owner of more than five percent of the class of securities. Item 6: …") is True

    def test_the_day_a_final_report_speaks_for(self):
        from app.scraper.sec_13dg_text import _final_since
        assert _final_since(True, "2026-09-30", "2026-10-27") == "2026-09-30"
        assert _final_since(True, None, "2026-10-27") == "2026-10-27"
        assert _final_since("2026-03-15", "2026-03-20", "2026-03-25") == "2026-03-15"
        assert _final_since(None, "2026-09-30", "2026-10-27") is None

    def test_the_owners_reader_carries_it(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = test_sec_13dg.TestAnExitIsNotAZeroPercentHolding._atom(None, ("0002012383-26-003852", "2026-10-27"))
        with patch.object(sec_client, "_get_text",
                          side_effect=_serve(atom, {self.TESLA_XML: _fixture("13ga_blackrock_tesla_final.xml")})), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]), \
             patch.object(sec_filer, "fetch_filer_name", return_value="BlackRock, Inc."):
            res = sec_13dg.fetch_ownership_filings("Tesla, Inc.", "0001318605")
        assert [(r["stake_percent"], r["until"], r["final_report"]) for r in res] == [(4.96, None, "2026-09-30")]


class TestANegligibleHoldingIsNotZero:
    """Six Apple directors hold 1,139 shares each — 0.0000076% of ~15bn. Stored
    as 0.0 that reads as "owns nothing", which is both false and
    indistinguishable from a filer who has actually exited."""

    def test_a_real_but_tiny_holding_has_no_percentage(self):
        from app.scraper.sec_13dg_text import _pct_of
        assert _pct_of(1139, 14_935_826_000) is None
        assert _pct_of(250, 734_000_000) is None

    def test_a_holding_above_the_floor_keeps_its_number(self):
        from app.scraper.sec_13dg_text import _pct_of
        assert _pct_of(159121937, 1965328900) == 8.0965
        assert _pct_of(1, 1_000_000) == 0.0001      # exactly at four decimals

    def test_holding_nothing_really_is_zero(self):
        """The exit rule reads a zero as "has left"; a genuine zero must stay 0.0
        or `not pct and not voting` stops firing and exits go unrecorded."""
        from app.scraper.sec_13dg_text import _pct_of
        assert _pct_of(0, 1_000_000) == 0.0

    def test_no_denominator_no_percentage(self):
        from app.scraper.sec_13dg_text import _pct_of
        assert _pct_of(1139, None) is None
        assert _pct_of(None, 1_000_000) is None

    def test_every_stake_computation_goes_through_it(self):
        """Three sites divided shares by shares-outstanding, each rounding to 4
        decimals on its own — the sibling-path shape this codebase keeps paying
        for. They share the helper now, so the floor rule cannot drift apart."""
        import inspect
        import re
        from app.scraper import (sec_13dg, sec_13dg_text, sec_13dg_xml, sec_13f, sec_edgar, sec_holdings,
                                 sec_insiders, sec_shares)
        # every module that reads a stake (sec_edgar.py was one file until 2026-11-25)
        src = "".join(inspect.getsource(m) for m in (sec_13dg, sec_13dg_text, sec_13dg_xml, sec_13f, sec_edgar,
                                                      sec_holdings, sec_insiders, sec_shares))
        helper = inspect.getsource(sec_13dg_text._pct_of)
        elsewhere = src.replace(helper, "")
        assert not re.search(r"round\([^)]*/[^)]*\*\s*100,\s*4\)", elsewhere), \
            "a stake is rounded outside _pct_of — the floor rule will drift"
        # the call sites: the two 13D/G cover rules, the newest-denominator
        # restatement, and `stake_for`, which every Form 4 holding goes through
        assert elsewhere.count("_pct_of(") == 4


class TestJointFilingCoverPages:
    """A joint 13D/G carries one cover page per reporting person, and the field
    parsers take the first match in the document. Embraer's 2009 13G put Júlio
    Bozano (IN, 10.4%) on page one and his holding company Cia. Bozano (CO,
    9.2%) on page two; the filer of record is Cia. Bozano, which was therefore
    minted as a Person owning 10.4%. Text below is the real filing, condensed.
    """

    BOZANO = (
        "EMBRAER — EMPRESA BRASILEIRA DE AERONÁUTICA S.A. (Name of Issuer) "
        "Common Shares (Title of Class of Securities) 29081M102 (CUSIP Number) "
        "CUSIP No. 29081M102 1 NAME OF REPORTING PERSON Júlio Rafael de Aragão Bozano "
        "S.S. or I.R.S. Identification No. of Above Person Not Applicable "
        "2 CHECK THE APPROPRIATE BOX IF A MEMBER OF A GROUP (a) o (b) þ 3 SEC USE ONLY "
        "4 CITIZENSHIP OR PLACE OF ORGANIZATION Brazil 5 SOLE VOTING POWER -0- "
        "6 SHARED VOTING POWER 75,133,609 7 SOLE DISPOSITIVE POWER -0- "
        "8 SHARED DISPOSITIVE POWER 75,133,609 "
        "9 AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON 75,133,609 "
        "11 PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 9 10.4% 12 TYPE OF REPORTING PERSON IN "
        "2 CUSIP No. 29081M102 1 NAME OF REPORTING PERSON Cia. Bozano "
        "S.S. or I.R.S. Identification No. of Above Person Not Applicable "
        "2 CHECK THE APPROPRIATE BOX IF A MEMBER OF A GROUP (a) o (b) þ 3 SEC USE ONLY "
        "4 CITIZENSHIP OR PLACE OF ORGANIZATION Brazil 5 SOLE VOTING POWER -0- "
        "6 SHARED VOTING POWER 66,236,689 7 SOLE DISPOSITIVE POWER -0- "
        "8 SHARED DISPOSITIVE POWER 66,236,689 "
        "9 AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON 66,236,689 "
        "11 PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 9 9.2% 12 TYPE OF REPORTING PERSON CO "
        "3 Item 1. Item 1(a). Name of Issuer: Embraer — Empresa Brasileira de Aeronáutica S.A. "
        "Item 2(a). Name of Person(s) Filing: Júlio Rafael de Aragão Bozano Cia. Bozano "
        "Júlio Rafael de Aragão Bozano owns 99.66% of Cia. Bozano. "
        "Cia. Bozano is a corporation (sociedade anônima) organized under the laws of Brazil. "
        "based on a total of 722,766,139 shares issued and outstanding."
    )

    def test_the_filer_of_record_gets_its_own_page(self):
        from app.scraper import sec_13dg_text
        page = sec_13dg_text._cover_page_for(self.BOZANO, "Cia. Bozano")
        assert sec_13dg_text._parse_reporter_type_from_text(page) is False
        assert sec_13dg_text._parse_percent_from_text(page) == 9.2
        assert sec_13dg_text._parse_aggregate_from_text(page) == 66_236_689
        assert sec_13dg_text._parse_power_rows(page)["shared_dispositive"] == 66_236_689

    def test_the_individual_on_page_one_is_still_read_as_one(self):
        from app.scraper import sec_13dg_text
        page = sec_13dg_text._cover_page_for(self.BOZANO, "Júlio Rafael de Aragão Bozano")
        assert sec_13dg_text._parse_reporter_type_from_text(page) is True
        assert sec_13dg_text._parse_percent_from_text(page) == 10.4

    def test_the_shared_header_and_footnote_are_read_from_the_whole_filing(self):
        # The class title precedes page one and the outstanding-shares footnote
        # follows the last page; neither belongs to a page, so a filer on an
        # earlier page would lose the denominator if only its page were read.
        from app.scraper import sec_13dg_text
        page = sec_13dg_text._cover_page_for(self.BOZANO, "Júlio Rafael de Aragão Bozano")
        assert sec_13dg_text._shares_outstanding(page) is None
        assert sec_13dg_text._shares_outstanding(self.BOZANO) == 722_766_139
        assert sec_13dg_text._parse_class_title_from_text(self.BOZANO) == "Common Shares"
        # A 13G, so the two Bozano pages are a family, not a bloc.
        assert sec_13dg_text._own_stake_and_voting(page, 10.4, document=self.BOZANO,
                                               in_group=False) == (10.4, None)

    def test_a_lone_filer_document_is_returned_whole(self):
        from app.scraper import sec_13dg_text
        lone = ("Apple Inc (Name of Issuer) NAME OF REPORTING PERSON The Vanguard Group "
                "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11 8.1% TYPE OF REPORTING PERSON IA")
        assert sec_13dg_text._cover_page_for(lone, "Vanguard Group") == sec_13dg_text._plain_text(lone)
        assert sec_13dg_text._cover_page_for(self.BOZANO, None) == sec_13dg_text._plain_text(self.BOZANO)

    def test_a_filer_matching_no_page_keeps_the_old_first_page_reading(self):
        from app.scraper import sec_13dg_text
        page = sec_13dg_text._cover_page_for(self.BOZANO, "Someone Else Entirely")
        assert page == sec_13dg_text._plain_text(self.BOZANO)
        assert sec_13dg_text._parse_percent_from_text(page) == 10.4

    def test_end_to_end_the_holding_company_is_a_company_with_its_own_stake(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SC 13G"/><content type="text/xml">
            <filing-href>https://x.test/i.htm</filing-href>
            <filing-date>2009-04-07</filing-date>
            <accession-number>0000950123-09-006192</accession-number>
          </content></entry>
        </feed>"""
        index = ('<span class="companyName">Cia. Bozano (Filed by)</span>'
                 '<a href="x">CIK=0001461009</a>'
                 '<table><tr><td><a href="/Archives/edgar/data/1355444/y01449sc13g.htm">d</a></td>'
                 '<td>SC 13G</td></tr></table>')
        pages = {"https://x.test/i.htm": index,
                 "https://www.sec.gov/Archives/edgar/data/1355444/y01449sc13g.htm": self.BOZANO}
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("EMBRAER S.A.", "1355444")
        assert len(res) == 1
        row = res[0]
        assert row["investor_name"] == "Cia. Bozano"
        assert row["is_individual"] is False
        assert row["stake_percent"] == 9.2
        assert row["shares"] == 66_236_689
        assert row["shares_outstanding"] == 722_766_139
        assert row["share_class"] == "Common Shares"


class TestOldCoverPagesWriteZeroInWords:
    """Pre-2024 covers write zero as "NONE", "-0-" or "—0—", and a filer with
    nothing in a row sometimes leaves the cell empty. The row parser skipped
    those and took the next digits it saw — the NEXT ROW'S NUMBER. Berkshire
    Hathaway's Activision 13G stored 8 shares, Citadel's Tesla 13G 6 shares.
    Texts are the real covers, condensed.
    """

    BERKSHIRE = ("5 SOLE VOTING POWER NONE 6 SHARED VOTING POWER 52,717,075 shares of Common Stock "
                 "7 SOLE DISPOSITIVE POWER NONE 8 SHARED DISPOSITIVE POWER 52,717,075 shares of Common Stock "
                 "9 AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON 52,717,075 shares of Common Stock "
                 "11 PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 9 6.7%")
    DEER = ("5. SOLE VOTING POWER 3,328,253* 6. SHARED VOTING POWER —0— "
            "7. SOLE DISPOSITIVE POWER 3,328,253* 8. SHARED DISPOSITIVE POWER —0— "
            "9. AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON 3,328,253*")
    CITADEL = ("5. SOLE VOTING POWER 0 6. SHARED VOTING POWER 7,675,822 shares "
               "7. SOLE DISPOSITIVE POWER 0 8. SHARED DISPOSITIVE POWER See Row 6 above. "
               "9. AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON See Row 6 above.")
    EMPTY = ("5 SOLE VOTING POWER 6 SHARED VOTING POWER 1,234,567 "
             "7 SOLE DISPOSITIVE POWER 8 SHARED DISPOSITIVE POWER 1,234,567")

    def test_none_is_zero_not_the_next_rows_number(self):
        from app.scraper.sec_13dg_text import _parse_power_rows
        from app.scraper.sec_13dg_xml import _shares_held
        rows = _parse_power_rows(self.BERKSHIRE)
        assert rows == {"sole_voting": 0, "shared_voting": 52_717_075,
                        "sole_dispositive": 0, "shared_dispositive": 52_717_075}
        assert _shares_held(rows, 52_717_075) == 52_717_075

    def test_dashed_zero_and_the_bozano_number_of_minus_zero_minus(self):
        from app.scraper.sec_13dg_text import _parse_power_rows
        assert _parse_power_rows(self.DEER) == {"sole_voting": 3_328_253, "shared_voting": 0,
                                                "sole_dispositive": 3_328_253, "shared_dispositive": 0}
        bozano = "5 SOLE VOTING POWER NUMBER OF -0- SHARES 6 SHARED VOTING POWER BENEFICIALLY OWNED BY 75,133,609"
        assert _parse_power_rows(bozano) == {"sole_voting": 0, "shared_voting": 75_133_609}

    def test_a_reference_to_another_row_copies_that_row(self):
        from app.scraper.sec_13dg_text import _parse_power_rows
        from app.scraper.sec_13dg_xml import _shares_held
        rows = _parse_power_rows(self.CITADEL)
        assert rows["shared_dispositive"] == 7_675_822
        assert _shares_held(rows, None) == 7_675_822

    def test_an_empty_cell_is_absent_not_a_count(self):
        from app.scraper.sec_13dg_text import _parse_power_rows
        assert _parse_power_rows(self.EMPTY) == {"shared_voting": 1_234_567,
                                                 "shared_dispositive": 1_234_567}


class TestTypeCodes:
    """Findings item 21 (c): HC is a holding company or a control person."""

    def test_the_codes(self):
        from app.scraper.sec_13dg_text import _individual_by_code
        assert _individual_by_code("IN") is True
        assert _individual_by_code("IA") is False
        assert _individual_by_code("HC") is None             # the name decides
        assert _individual_by_code("HC, CO") is False        # a definite code beside it decides
        assert _individual_by_code("hc") is None
        assert _individual_by_code(None) is None and _individual_by_code("") is None

    def test_a_cover_page_hc_says_nothing_either_way(self):
        from app.scraper import sec_13dg_text
        assert sec_13dg_text._parse_reporter_type_from_text("12 TYPE OF REPORTING PERSON HC ") is None
        assert sec_13dg_text._parse_reporter_type_from_text("12 TYPE OF REPORTING PERSON HC, CO ") is False
        # the prose after the box is not a second code
        assert sec_13dg_text._parse_reporter_type_from_text(
            "12 TYPE OF REPORTING PERSON CO in the aggregate") is False

    def test_dondero_on_nexpoints_13d_is_left_to_the_name(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_13dg_xml, sec_client, sec_filer
        xml = {"filer_cik": "0001228922", "issuer_cik": "0001620393", "issuer_name": "NexPoint",
               "schedule": "13D", "persons": [
                   {"name": "DONDERO JAMES D", "cik": "0001228922", "type_code": "HC", "percent": 20.0},
                   {"name": "DONDERO NANCY MARIE", "cik": None, "type_code": "IN", "percent": 1.0}]}
        atom = test_sec_13dg.TestStructuredScrapeEndToEnd.ATOM.replace("SCHEDULE 13G", "SCHEDULE 13D/A")
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, {})), \
             patch.object(sec_13dg_xml, "_fetch_13dg_xml", return_value=xml), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("NexPoint", "1620393")
        assert res[0]["investor_name"].lower() == "dondero james d"
        assert res[0]["is_individual"] is None
        assert [g["type_code"] for g in res[0]["group_members"]] == ["IN"]


class TestThirteenGCoFilersAreAFamilyNotABloc:
    """Seven of the eleven pre-2024 13G rows on dev read "stake unknown, bloc
    X%": the text path assumed every filer might be in a group, and a 13G
    family (Berkshire + Buffett + subsidiaries; Sequoia's four funds) reports
    sole dispositive 0 exactly like a 13D bloc member. A 13G is passive by
    definition, so its co-filers never make a bloc; a 13D's still do.
    """

    def test_the_rule(self):
        from app.scraper.sec_13dg_text import _co_filers_form_a_bloc
        assert _co_filers_form_a_bloc("SC 13D", 3) is True
        assert _co_filers_form_a_bloc("SCHEDULE 13D/A", 2) is True
        assert _co_filers_form_a_bloc("SC 13G", 10) is False
        assert _co_filers_form_a_bloc("SCHEDULE 13G/A", 4) is False
        assert _co_filers_form_a_bloc("SC 13D", 1) is False
        assert _co_filers_form_a_bloc("SC 13D", 0) is True, "unknown count stays conservative"

    def test_a_13g_family_member_keeps_its_reported_stake(self):
        from app.scraper.sec_13dg_xml import _stake_from_person
        person = {"name": "Berkshire Hathaway Inc", "sole_voting": 0,
                  "shared_voting": 52_717_075, "sole_dispositive": 0,
                  "shared_dispositive": 52_717_075, "aggregate": 52_717_075, "percent": 6.7}
        family = {"comment_text": "", "schedule": "13G",
                  "persons": [person, {"name": "Warren E. Buffett"}, {"name": "National Indemnity Co"}]}
        assert _stake_from_person(family, person) == (6.7, None)
        group = {**family, "schedule": "13D"}
        assert _stake_from_person(group, person) == (None, 6.7)

    def test_the_parsed_xml_says_which_schedule_it_is(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        assert _parse_13dg_xml(_fixture("13g_vanguard.xml"))["schedule"] == "13G"
        assert _parse_13dg_xml(_fixture("13ga_wellington.xml"))["schedule"] == "13G"

    def test_end_to_end_berkshire_over_activision(self):
        # Ten cover pages, zero written as NONE. Stored before: stake None,
        # "bloc" 6.7%, 8 shares.
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SC 13G"/><content type="text/xml">
            <filing-href>https://x.test/i.htm</filing-href>
            <filing-date>2023-02-14</filing-date>
            <accession-number>0001193125-23-038333</accession-number>
          </content></entry>
        </feed>"""
        index = ('<span class="companyName">BERKSHIRE HATHAWAY INC (Filed by)</span>'
                 '<a href="x">CIK=0001067983</a>'
                 '<table><tr><td><a href="/Archives/edgar/data/718877/d415358dsc13g.htm">d</a></td>'
                 '<td>SC 13G</td></tr></table>')
        page = ("CUSIP No. 00507V109 1 NAMES OF REPORTING PERSONS {name} 2 CHECK THE APPROPRIATE BOX "
                "IF A MEMBER OF A GROUP 4 CITIZENSHIP OR PLACE OF ORGANIZATION Delaware "
                + TestOldCoverPagesWriteZeroInWords.BERKSHIRE.replace("52,717,075", "{n}").replace("6.7%", "{pct}")
                + " 12 TYPE OF REPORTING PERSON {code} ")
        # The subsidiary "Berkshire Hathaway International Insurance Ltd." shares
        # both significant tokens with the filer and once outscored the parent.
        doc = ("Activision Blizzard, Inc. (Name of Issuer) Common Stock, par value $0.000001 "
               "(Title of Class of Securities) "
               + page.format(name="Warren E. Buffett", n="52,717,075", pct="6.7%", code="IN")
               + page.format(name="Berkshire Hathaway Inc.", n="52,717,075", pct="6.7%", code="HC, CO")
               + page.format(name="National Indemnity Company", n="42,000,000", pct="5.3%", code="IC, CO")
               + page.format(name="Berkshire Hathaway International Insurance Ltd.", n="368,000", pct="0.1%", code="IC, CO")
               + "based on a total of 782,626,000 shares issued and outstanding.")
        pages = {"https://x.test/i.htm": index,
                 "https://www.sec.gov/Archives/edgar/data/718877/d415358dsc13g.htm": doc}
        with patch.object(sec_client, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Activision Blizzard, Inc.", "718877")
        assert len(res) == 1
        row = res[0]
        assert row["is_individual"] is False
        assert row["stake_percent"] == 6.7 and row["voting_power_pct"] is None
        assert row["shares"] == 52_717_075
        assert row["shares_outstanding"] == 782_626_000


class TestDateOfEvent:
    """The day a cover states the position as of. The count and the
    percentage are true on that day; the filing date is when it was said."""

    def test_both_written_forms(self):
        from app.scraper.sec_13dg_text import _event_date
        assert _event_date("02/07/2025") == "2025-02-07"
        assert _event_date("3/1/2020") == "2020-03-01"
        assert _event_date("March 10, 2020") == "2020-03-10"
        assert _event_date("Sept. 3 2019") is None                 # not a month we read: no guess
        assert _event_date("Sep. 3, 2019") is None
        assert _event_date("September 3 2019") == "2019-09-03"

    def test_nothing_is_guessed(self):
        from app.scraper.sec_13dg_text import _event_date
        for bad in (None, "", "02/30/2025", "2025-02-07", "13/01/2025", "Q1 2025"):
            assert _event_date(bad) is None

    def test_the_structured_schedules_carry_it(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        assert _parse_13dg_xml(_fixture("13d_altria_abinbev.xml"))["event_date"] == "2025-02-07"
        # a 13G names it differently, and for a passive filer it is a quarter-end
        assert _parse_13dg_xml(_fixture("13g_vanguard.xml"))["event_date"] == "2026-03-31"

    def test_an_html_cover_carries_it(self):
        from app.scraper.sec_13dg_text import _parse_event_date_from_text
        cover = ("<p>Ordinary Shares, without nominal value</p><p>(Title of Class of Securities)</p>"
                 "<p>March&nbsp;10, 2020</p><p>(Date of Event Which Requires Filing of this Statement)</p>")
        assert _parse_event_date_from_text(cover) == "2020-03-10"
        assert _parse_event_date_from_text("12/31/2019 (Date of Event which Requires Filing "
                                           "of this Statement)") == "2019-12-31"
        assert _parse_event_date_from_text("no cover here") is None

    def test_the_filer_side_holding_carries_it(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg_xml, sec_holdings
        xml = sec_13dg_xml._parse_13dg_xml(_fixture("13g_vanguard.xml"))
        with patch.object(sec_13dg_xml, "_fetch_13dg_xml", return_value=xml):
            parsed = sec_holdings._parse_holding_filing("0000102909", "0000000000-26-000001")
        assert parsed["event_date"] == "2026-03-31"


class TestWhenAHoldingBegan:
    """The scrape reads each holder's NEWEST filing; its date written as a
    stated start made a holder since 2005, first scraped in 2026, "since
    2026". An original schedule states the start; an amendment bounds it."""

    def test_the_rule(self):
        from app.scraper.sec_13dg_text import _stake_start
        assert _stake_start("SCHEDULE 13G", "2026-03-31", "2026-04-20") == ("2026-03-31", None)
        assert _stake_start("SC 13D", None, "2019-05-02") == ("2019-05-02", None)
        assert _stake_start("SCHEDULE 13G/A", "2025-12-31", "2026-02-10") == ("2025-12-31", "amendment")
        assert _stake_start("SC 13D/A", None, "2020-03-10") == ("2020-03-10", "amendment")
        assert _stake_start("SC 13D/A", None, None) == (None, None)

    def test_the_scrape_result_carries_it(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SCHEDULE 13G"/><content type="text/xml">
            <filing-href>https://x.test/i.htm</filing-href>
            <filing-date>2026-04-29</filing-date>
            <accession-number>0002100119-26-000139</accession-number>
          </content></entry>
        </feed>"""
        url = ("https://www.sec.gov/Archives/edgar/data/320193/"
               "000210011926000139/primary_doc.xml")
        with patch.object(sec_client, "_get_text",
                          side_effect=_serve(atom, {url: _fixture("13g_vanguard.xml")})), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Apple Inc", "0000320193")
        assert (res[0]["since"], res[0]["since_basis"]) == ("2026-03-31", None)


class TestDottedAndGermanFormsInTheIssuerCheck:
    """Findings items 12 and 13: the issuer check's tokens and the ticker key."""

    def test_tokens(self):
        from app.scraper.sec_13dg_text import _significant_tokens
        assert _significant_tokens("Blackstone L.P.") == {"blackstone"}
        assert _significant_tokens("Henkel AG & Co. KGaA") == {"henkel"}
        assert _significant_tokens("KG Mobility Corp") == {"kg", "mobility"}

    def test_ticker_key(self):
        assert _ticker_normalize("Siemens Beteiligungen GmbH & Co. KG") == "siemens beteiligungen"
        assert _ticker_normalize("Merck KGaA") == "merck"


class TestAVotingPactOrAJointFiling:
    """Findings item 103 (the user's rule of 2026-11-11): a joint 13D is a
    voting group only when it names an agreement over the votes; the rest —
    a parent with its subsidiaries, a family with its trusts — file together
    as Rule 13d-1(k) allows and are one holding. Of the 44 joint 13Ds behind
    dev's 27 groups, two name one: AB InBev's and the New York Times'."""

    def test_an_agreement_named_makes_the_co_filers_a_pact(self):
        from app.scraper.sec_13dg_text import _is_voting_pact
        brc = "<item6>The Reporting Persons are parties to the Voting Agreement</item6><memberOfGroup>a</memberOfGroup>"
        nyt = "the 1997 Trust … a Stockholders' Agreement … group within the meaning of Rule 13d-5"
        trust = "the shares are held in a voting trust"
        for text in (brc, nyt, trust):
            assert _is_voting_pact("SCHEDULE 13D/A", 8, text) is True, text

    def test_a_family_without_an_agreement_is_a_joint_filing(self):
        from app.scraper.sec_13dg_text import _is_voting_pact
        # Berkshire on DaVita: (a) ticked, but no agreement over the votes
        berkshire = "<memberOfGroup>a</memberOfGroup> National Indemnity Company is a wholly-owned subsidiary"
        assert _is_voting_pact("SCHEDULE 13D/A", 8, berkshire) is False

    def test_only_a_13d_with_co_filers_can_be_a_pact(self):
        from app.scraper.sec_13dg_text import _is_voting_pact
        assert _is_voting_pact("SCHEDULE 13G/A", 4, "a Voting Agreement") is False
        assert _is_voting_pact("SCHEDULE 13D", 1, "a Voting Agreement") is False

    def test_everyone_disclaiming_a_group_is_no_pact(self):
        # News Corp's Murdoch family trust names a stockholders agreement and
        # ticks (b) for every reporting person
        from app.scraper.sec_13dg_text import _group_boxes, _is_voting_pact
        xml = "a Stockholders Agreement" + "<memberOfGroup>b</memberOfGroup>" * 4
        assert _group_boxes(xml) == ["b"] * 4 and _is_voting_pact("SCHEDULE 13D", 4, xml) is False
        text = ("2. CHECK THE APPROPRIATE BOX IF A MEMBER OF A GROUP (a) ☐ (b) ☒ ... "
                "2. Check the Appropriate Box if a Member of a Group (a) [X] (b) [ ] a Voting Agreement")
        assert _group_boxes(text) == ["b", "a"] and _is_voting_pact("SC 13D", 2, text) is True

    def test_the_statement_says_which_one_it_is(self):
        # the record carries the flag the writer draws by; the stake split stays
        from app.scraper import sec_13dg_xml
        raw = _fixture("13da_brc_abinbev_2026.xml")
        assert sec_13dg_xml._parse_13dg_xml(raw)["raw_text"] == raw
