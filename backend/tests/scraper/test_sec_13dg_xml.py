"""Reading a structured 13D/G statement (app/scraper/sec_13dg_xml.py): the era, the reporting
persons, the shares held and voted, group members.

Part of test_sec_edgar.py until 2026-11-25, when sec_edgar.py was split into modules."""

import pytest

from tests.scraper.sec_statement_fakes import _fixture, _serve


class TestStructuredFilings:
    """The SEC's Dec-2024 modernization made 13D/G machine-readable, which is
    where the group membership behind AB InBev's voting bloc comes from — no
    Item 6 prose. Two schedules, two sets of tag names for the same facts."""

    def test_reads_the_13g_spelling(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        d = _parse_13dg_xml(_fixture("13g_vanguard.xml"))
        assert d["issuer_cik"] == "0000320193"
        assert d["issuer_name"] == "Apple Inc"
        assert len(d["persons"]) == 1
        assert d["persons"][0]["percent"] == 7.48
        assert d["persons"][0]["type_code"] == "IA"

    def test_reads_the_13d_spelling(self):
        # issuerCIK / percentOfClass / reportingPersonInfo — a 13G-only reader
        # returns nothing at all here, which is what used to happen.
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        d = _parse_13dg_xml(_fixture("13d_tactical.xml"))
        assert d["issuer_cik"] == "0002041208"
        assert d["persons"][0]["percent"] == 11.7
        assert [p["name"] for p in d["persons"]] == [
            "Blue Bird Capital Enterprises LLC", "Justus Parmar"]

    def test_every_group_member_gets_its_own_numbers(self):
        # Wellington's four blocks are NOT identical: the fourth reports 5.1 and
        # 28,489,718 where the others report 5.4 and 28,990,296. A lookup that
        # searches the whole document instead of the person's own subtree hands
        # every member the first block's figures.
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        d = _parse_13dg_xml(_fixture("13ga_wellington.xml"))
        assert [p["percent"] for p in d["persons"]] == [5.4, 5.4, 5.4, 5.1]
        assert d["persons"][3]["shared_voting"] == 28489718
        assert d["persons"][0]["shared_voting"] == 28990296
        # `percent` and `shared_voting` are what pin the scoping here — they are
        # the two fields these four real blocks disagree on. All four report
        # sole voting and sole dispositive of 0, so those columns would look
        # identical however they were read; the mechanism is shared, so proving
        # it on these two proves it for the row.

    def test_the_items_percentage_does_not_leak_in(self):
        # The same tag reappears further down the document with a different
        # value (5.36). No person may be given it.
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        d = _parse_13dg_xml(_fixture("13ga_wellington.xml"))
        assert 5.36 not in [p["percent"] for p in d["persons"]]

    def test_the_security_the_percentages_measure_is_captured(self):
        # Without this, 22.3% of one share class and 9.7% of another look
        # addable — which is how Grupo Televisa reached 115.9% of itself.
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        assert _parse_13dg_xml(_fixture("13g_vanguard.xml"))["class_title"] == "Common Stock"
        assert _parse_13dg_xml(_fixture("13d_tactical.xml"))["class_title"] == \
            "Common Shares, par value $0.0001 per share"

    def test_decimal_and_integer_share_counts_both_parse(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        d13d = _parse_13dg_xml(_fixture("13d_tactical.xml"))   # "1598232.00"
        d13g = _parse_13dg_xml(_fixture("13g_vanguard.xml"))   # "145321305"
        assert d13d["persons"][0]["sole_dispositive"] == 1598232
        assert d13g["persons"][0]["sole_voting"] == 145321305

    def test_an_absent_row_stays_absent(self):
        # None and 0 mean different things to _split_stake; conflating them
        # invents a 0% stake.
        from app.scraper.sec_13dg_xml import _xml_num
        import xml.etree.ElementTree as ET
        el = ET.fromstring("<a><soleVotingPower>0</soleVotingPower></a>")
        assert _xml_num(el, "soleVotingPower") == 0
        assert _xml_num(el, "soleDispositivePower") is None

    def test_junk_is_not_a_filing(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        assert _parse_13dg_xml("<html>404</html>") is None
        assert _parse_13dg_xml("not xml at all <<<") is None


class TestEraDetection:
    def test_modern_form_names_have_xml(self):
        from app.scraper.sec_13dg_xml import _is_structured
        assert _is_structured("SCHEDULE 13D") is True
        assert _is_structured("SCHEDULE 13G/A") is True

    def test_legacy_form_names_do_not(self):
        from app.scraper.sec_13dg_xml import _is_structured
        assert _is_structured("SC 13D") is False
        assert _is_structured("SC 13G/A") is False

    def test_other_schedules_are_not_mistaken_for_13s(self):
        # "SCHEDULE" alone would catch SCHEDULE TO-I and friends.
        from app.scraper.sec_13dg_xml import _is_structured
        assert _is_structured("SCHEDULE TO-I") is False
        assert _is_structured("SCHEDULE 14A") is False


class TestXmlIssuerVerification:
    """Two tiers. The CIK is strong but agent-typed — the Embraer mis-file came
    from that same keyboard — while the name can be years out of date."""

    def test_the_matching_cik_is_enough(self):
        from app.scraper.sec_13dg_xml import _xml_issuer_matches
        xml = {"issuer_cik": "0000320193", "issuer_name": "Apple Inc"}
        assert _xml_issuer_matches(xml, "320193", ["Apple Inc"]) is True

    def test_a_stale_name_with_the_right_cik_survives(self):
        # Wellington's real filing: correct CIK, "The NASDAQ OMX Group, Inc."
        from app.scraper.sec_13dg_xml import _xml_issuer_matches
        xml = {"issuer_cik": "0001120193", "issuer_name": "The NASDAQ OMX Group, Inc."}
        assert _xml_issuer_matches(xml, "1120193", ["Nasdaq, Inc."]) is True

    def test_the_matching_cik_carries_a_name_that_shares_nothing(self):
        # The case the CIK tier exists for: a rename with no word in common.
        # Alphabet's CIK still carries filings that say "Google Inc." — token
        # overlap is zero, so only the CIK keeps this real owner.
        from app.scraper.sec_13dg_xml import _xml_issuer_matches
        xml = {"issuer_cik": "0001652044", "issuer_name": "Google Inc."}
        assert _xml_issuer_matches(xml, "1652044", ["Alphabet Inc."]) is True

    def test_a_wrong_cik_with_a_matching_name_survives(self):
        # The mirror case: the agent typed the wrong CIK but the name is ours.
        from app.scraper.sec_13dg_xml import _xml_issuer_matches
        xml = {"issuer_cik": "0009999999", "issuer_name": "Embraer S.A."}
        assert _xml_issuer_matches(xml, "1355444", ["Embraer"]) is True

    def test_a_filing_about_another_company_is_dropped(self):
        # Eve Holding, in structured form: both tiers disagree.
        from app.scraper.sec_13dg_xml import _xml_issuer_matches
        xml = {"issuer_cik": "0001823652", "issuer_name": "Eve Holding, Inc."}
        assert _xml_issuer_matches(xml, "1355444", ["Embraer"]) is False

    def test_no_stated_issuer_is_not_a_mismatch(self):
        from app.scraper.sec_13dg_xml import _xml_issuer_matches
        assert _xml_issuer_matches({"issuer_cik": None}, "1355444", ["Embraer"]) is True


class TestStakeFromStructuredFilings:
    def test_a_stated_denominator_is_preferred_over_a_derived_one(self):
        # The two must disagree or the test proves nothing. Stated 1,250,000
        # gives 8.0%; deriving from 500,000 at 50% would give 1,000,000 and
        # therefore 10.0%. `percentOfClass` is often two significant figures,
        # so the stated figure is the better one whenever the filer gives it.
        from app.scraper.sec_13dg_xml import _stake_from_person
        person = {"name": "X", "sole_voting": 0, "shared_voting": 500000,
                  "sole_dispositive": 100000, "shared_dispositive": 0,
                  "aggregate": 500000, "percent": 50.0}
        # A group member (co-filer present): its individual 100,000 of a stated
        # 1,250,000 is 8.0%, and the 50% is the bloc.
        xml = {"comment_text": "based on a total of 1,250,000 shares issued and outstanding",
               "persons": [person, {"name": "Co-Filer LLC"}]}
        stake, voting = _stake_from_person(xml, person)
        assert stake == 8.0, "the derived denominator was used despite a stated one"
        assert voting == 50.0

    def test_derivation_is_the_fallback_when_nothing_is_stated(self):
        from app.scraper.sec_13dg_xml import _stake_from_person
        person = {"name": "X", "sole_voting": 0, "shared_voting": 1020598157,
                  "sole_dispositive": 159121937, "shared_dispositive": 0,
                  "aggregate": 1020598157, "percent": 51.7}
        # Altria in the AB InBev Voting Agreement — a group member, so its own
        # 159M is derived against the bloc's total.
        xml = {"comment_text": "", "persons": [person, {"name": "Co-Filer LLC"}]}
        stake, voting = _stake_from_person(xml, person)
        assert stake == pytest.approx(8.06, abs=0.01)   # Altria's real holding
        assert voting == 51.7

    def test_a_zero_percent_amendment_does_not_divide_by_zero(self):
        # A 13G/A reporting 0% is a filer announcing it has exited; common.
        from app.scraper.sec_13dg_xml import _derive_total
        assert _derive_total(0, 0.0) is None
        assert _derive_total(1000, 0.0) is None
        assert _derive_total(None, 5.0) is None

    def test_a_lone_filer_keeps_its_reported_percentage(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml, _stake_from_person
        d = _parse_13dg_xml(_fixture("13g_vanguard.xml"))
        stake, voting = _stake_from_person(d, d["persons"][0])
        assert stake == 7.48 and voting is None

    def test_a_lone_party_to_a_voting_agreement_gets_its_own_stake(self):
        # Altria's 2025 13D/A, the structured kind, names ONE reporting person
        # — so it read as a lone custodian and row 13's 51.9% (the Voting
        # Agreement bloc) became Altria's stake: AB InBev's "majority owner".
        # Altria disposes of 159,121,937 shares of its own — 8.1% — and shares
        # the vote over 1,020,598,157.
        from app.scraper.sec_13dg_xml import _parse_13dg_xml, _stake_from_person
        d = _parse_13dg_xml(_fixture("13d_altria_abinbev.xml"))
        assert len(d["persons"]) == 1 and d["schedule"] == "13D"
        stake, voting = _stake_from_person(d, d["persons"][0])
        assert stake == pytest.approx(8.1, abs=0.02)
        assert voting == 51.9

    def test_a_lone_custodian_and_a_lone_owner_stay_as_they_were(self):
        from app.scraper.sec_13dg_text import _split_stake
        total = 1_000_000
        # custodian: nothing it can sell alone, everything shared → its percent
        assert _split_stake({"sole_voting": 0, "shared_voting": 50_000, "sole_dispositive": 0,
                             "shared_dispositive": 60_000}, total, 6.0) == (6.0, None)
        # some sole, the rest disposed of jointly, votes no more than it disposes of → its percent
        assert _split_stake({"sole_voting": 0, "shared_voting": 60_000, "sole_dispositive": 10_000,
                             "shared_dispositive": 50_000}, total, 6.0) == (6.0, None)
        # can sell nothing alone, votes a little more than it disposes of
        # jointly: still a custodian's shape, not an owner in an agreement
        assert _split_stake({"sole_voting": 0, "shared_voting": 70_000, "sole_dispositive": 0,
                             "shared_dispositive": 60_000}, total, 7.0) == (7.0, None)
        # the boundary: votes exactly what it disposes of is no agreement
        assert _split_stake({"shared_voting": 60_000, "sole_dispositive": 60_000,
                             "shared_dispositive": 0}, total, 6.0) == (6.0, None)

    def test_a_lone_agreement_party_counts_what_it_disposes_of_jointly_too(self):
        # votes 500k, can sell 100k alone and 50k jointly → owns 150k = 15%; 50% is the bloc
        from app.scraper.sec_13dg_text import _split_stake
        rows = {"sole_voting": 0, "shared_voting": 500_000, "sole_dispositive": 100_000,
                "shared_dispositive": 50_000}
        assert _split_stake(rows, 1_000_000, 50.0) == (15.0, 50.0)
        # no denominator: the bloc is certain, the stake is not invented
        assert _split_stake(rows, None, 50.0) == (None, 50.0)


class TestPersonSelection:
    def test_the_filer_is_picked_out_of_the_group(self):
        from app.scraper.sec_13dg_xml import _select_person
        xml = {"persons": [{"name": "A", "cik": "0000000111"},
                           {"name": "B", "cik": "0000000222"}]}
        assert _select_person(xml, "222")["name"] == "B"

    def test_a_block_of_its_own_cik_is_used_when_none_is_the_filer(self):
        from app.scraper.sec_13dg_xml import _select_person
        xml = {"persons": [{"name": "A", "cik": "0000000111"}, {"name": "B", "cik": None}]}
        assert _select_person(xml, "999") == {"name": "A", "cik": "0000000111"}

    def test_cik_less_blocks_pick_the_one_edgar_names_the_filer(self):
        # Berkshire's 13Gs: no block carries a CIK and Warren E. Buffett comes
        # first. The first block on Berkshire's CIK made a Person "Warren E.
        # Buffett" sec:0001067983 with twelve of Berkshire's stakes (item 21).
        from app.scraper.sec_13dg_xml import _select_person
        xml = {"persons": [{"name": "Warren E. Buffett", "cik": None},
                           {"name": "Berkshire Hathaway Inc.", "cik": None}]}
        picked = _select_person(xml, "0001067983", lambda cik: "BERKSHIRE HATHAWAY INC")
        assert picked == {"name": "Berkshire Hathaway Inc.", "cik": "0001067983"}
        assert xml["persons"][0]["cik"] is None            # never the filer's CIK on another name

    def test_a_person_filer_is_matched_in_edgar_word_order(self):
        from app.scraper.sec_13dg_xml import _select_person
        xml = {"persons": [{"name": "Berkshire Hathaway Inc.", "cik": None},
                           {"name": "Warren E. Buffett", "cik": None}]}
        picked = _select_person(xml, "0000315090", lambda cik: "BUFFETT WARREN E")
        assert picked["name"] == "Warren E. Buffett" and picked["cik"] == "0000315090"

    def test_no_block_named_as_the_filer_skips_the_filing(self):
        from app.scraper.sec_13dg_xml import _select_person
        xml = {"persons": [{"name": "A Fund", "cik": None}, {"name": "B Fund", "cik": None}]}
        assert _select_person(xml, "999", lambda cik: "SOMEONE ELSE LLC") is None
        assert _select_person(xml, "999") is None          # no name to match by
        assert all(p["cik"] is None for p in xml["persons"])

    def test_a_lone_cik_less_block_is_the_filer(self):
        from app.scraper.sec_13dg_xml import _select_person
        asked = []
        xml = {"persons": [{"name": "Vanguard Capital Management", "cik": None}]}
        picked = _select_person(xml, "0000102909", asked.append)
        assert picked["cik"] == "0000102909" and asked == []

    def test_the_name_is_not_fetched_when_a_cik_matches(self):
        from app.scraper.sec_13dg_xml import _select_person
        asked = []
        xml = {"persons": [{"name": "A", "cik": None}, {"name": "B", "cik": "0000000222"}]}
        assert _select_person(xml, "222", asked.append)["name"] == "B" and asked == []

    def test_the_filer_name_ignores_legal_form_and_word_order(self):
        from app.scraper.sec_13dg_text import _same_filer_name
        assert _same_filer_name("Berkshire Hathaway Inc.", "BERKSHIRE HATHAWAY INC")
        assert _same_filer_name("Warren E. Buffett", "BUFFETT WARREN E")
        assert not _same_filer_name("National Indemnity Company", "BERKSHIRE HATHAWAY INC")
        assert not _same_filer_name("Berkshire Hathaway International Insurance Ltd.",
                                    "BERKSHIRE HATHAWAY INC")

    def test_no_persons_means_no_selection(self):
        from app.scraper.sec_13dg_xml import _select_person
        assert _select_person({"persons": []}, "1") is None


class TestGroupMembersFromSgml:
    def test_the_header_lines_are_read_one_per_member(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg_xml, sec_client
        header = ("<pre>GROUP MEMBERS:\t\tJORGE PAULO LEMANN\n"
                  "GROUP MEMBERS:\t\tSTICHTING ANHEUSER-BUSCH INBEV\n"
                  "GROUP MEMBERS:\t\tRAYVAX SOCIETE D INVESTISSEMENTS S.A.\n"
                  "SUBJECT COMPANY:\nCOMPANY CONFORMED NAME: Anheuser-Busch InBev SA/NV\n</pre>")
        with patch.object(sec_client, "_get_text", return_value=header):
            got = sec_13dg_xml._sgml_group_members("1668717", "0001193125-24-230284")
        assert [g["name"] for g in got] == [
            "JORGE PAULO LEMANN", "STICHTING ANHEUSER-BUSCH INBEV",
            "RAYVAX SOCIETE D INVESTISSEMENTS S.A."]
        assert all(g["source"] == "sgml" and g["cik"] is None for g in got)

    def test_the_rest_of_the_header_is_not_swallowed(self):
        # The header is line-oriented. Flattening whitespace first makes a
        # single match run to the end of the document and return the whole
        # header as one enormous "name" — which is what happened.
        from unittest.mock import patch
        from app.scraper import sec_13dg_xml, sec_client
        header = "GROUP MEMBERS:\tA CORP\nSUBJECT COMPANY:\tSOMETHING ELSE\n"
        with patch.object(sec_client, "_get_text", return_value=header):
            got = sec_13dg_xml._sgml_group_members("1", "0000000000-00-000000")
        assert [g["name"] for g in got] == ["A CORP"]

    def test_a_missing_header_is_not_fatal(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg_xml, sec_client
        with patch.object(sec_client, "_get_text", side_effect=Exception("404")):
            assert sec_13dg_xml._sgml_group_members("1", "0000000000-00-000000") == []


class TestShareCountsAreKept:
    """A count is what the filing states; a percentage is a division we perform
    against a denominator that moves. Bevco's 5.9% went stale purely because AB
    InBev issued more shares — its holding never changed."""

    def test_a_lone_filer_keeps_its_holding(self):
        from app.scraper.sec_13dg_xml import _shares_held
        assert _shares_held({"sole_dispositive": 1099168953}, None) == 1099168953

    def test_dispositive_power_is_what_counts_not_voting(self):
        # What the filer can sell is what it owns. Altria votes a billion shares
        # and can dispose of 159 million.
        from app.scraper.sec_13dg_xml import _shares_held
        rows = {"sole_voting": 0, "shared_voting": 1020598157,
                "sole_dispositive": 159121937, "shared_dispositive": 0}
        assert _shares_held(rows, 1020598157) == 159121937

    def test_a_purely_joint_holder_reports_what_it_disposes_of_jointly(self):
        # BRC can sell nothing alone, but the Stichting it co-owns holds
        # 771,096,582 — a real number, where stake_percent has to say None.
        from app.scraper.sec_13dg_xml import _shares_held
        rows = {"sole_dispositive": 0, "shared_dispositive": 771096582}
        assert _shares_held(rows, 1033081237) == 771096582

    def test_the_aggregate_is_the_last_resort(self):
        from app.scraper.sec_13dg_xml import _shares_held
        assert _shares_held({}, 5000) == 5000
        assert _shares_held({}, None) is None

    def test_the_counts_reach_the_scrape_result(self):
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
        assert res[0]["shares"] == 1099168953
        # 7.48% of what? The stake is checkable only if both numbers survive.
        assert res[0]["stake_percent"] == 7.48

    def test_a_filing_without_counts_stores_none_rather_than_zero(self):
        # Absent is not zero: a nil holding and an unstated one are different
        # facts, and a 0 here would read as "sold out".
        from app.scraper.sec_13dg_xml import _shares_held
        assert _shares_held({"sole_dispositive": 0, "shared_dispositive": 0}, None) is None


class TestTheBlocsOwnCount:
    """`voting_power_pct` has a numerator too — row 11. Like the percentage it
    belongs to the GROUP and is repeated verbatim by every member, so it may
    never be summed across owners."""

    def test_a_bloc_reports_its_count(self):
        from app.scraper.sec_13dg_xml import _shares_voted
        assert _shares_voted(1020598157, 51.7) == 1020598157

    def test_a_lone_filer_has_no_bloc_count(self):
        # No bloc, no number — not zero, and not its own holding restated.
        from app.scraper.sec_13dg_xml import _shares_voted
        assert _shares_voted(32416315, None) is None

    def test_row_eleven_is_read_off_a_cover(self):
        from app.scraper.sec_13dg_text import _parse_aggregate_from_text
        assert _parse_aggregate_from_text(
            "11. Aggregate Amount Beneficially Owned by Each Reporting Person "
            "1,020,598,157 12. Check if") == 1020598157

    def test_a_cover_without_row_eleven_yields_nothing(self):
        from app.scraper.sec_13dg_text import _parse_aggregate_from_text
        assert _parse_aggregate_from_text("no such row here") is None

    def test_every_member_reports_the_same_number(self):
        # The property that makes summing wrong. Wellington's four blocks each
        # carry the same aggregate; adding them would quadruple the bloc.
        from app.scraper.sec_13dg_xml import _parse_13dg_xml
        d = _parse_13dg_xml(_fixture("13ga_wellington.xml"))
        aggregates = [p["aggregate"] for p in d["persons"][:3]]
        assert len(set(aggregates)) == 1


class TestALoneFilersHoldingIsBothRows:
    """SoftBank's 13G/A on Alibaba: 3,788,048 shares it can sell alone and
    627,002,296 it sells through its subsidiaries — 630,790,344, 3.3 %. The
    count kept only the sole row, which made SoftBank a 0.02 % holder as soon
    as the count was divided again (the newest-denominator restatement)."""

    def test_the_real_filing(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml, _shares_held, _stake_from_person
        d = _parse_13dg_xml(_fixture("13ga_softbank_alibaba.xml"))
        assert len(d["persons"]) == 1 and d["schedule"] == "13G"
        p = d["persons"][0]
        rows = {k: p[k] for k in ("sole_voting", "shared_voting", "sole_dispositive",
                                  "shared_dispositive") if p.get(k) is not None}
        assert _shares_held(rows, p["aggregate"], in_group=False) == 630_790_344
        stake, voting = _stake_from_person(d, p)
        assert stake == 3.3 and voting is None

    def test_alone_both_rows_in_a_bloc_the_old_rule(self):
        from app.scraper.sec_13dg_xml import _shares_held
        rows = {"sole_dispositive": 100, "shared_dispositive": 900}
        assert _shares_held(rows, 1000, in_group=False) == 1000
        assert _shares_held(rows, 1000, in_group=True) == 100        # a member's shared rows are the group's
        # Altria alone: nothing shared to add
        assert _shares_held({"sole_dispositive": 159121937, "shared_dispositive": 0},
                            1020598157, in_group=False) == 159121937
        # nothing stated: still the aggregate, still not zero
        assert _shares_held({}, 5000, in_group=False) == 5000
        assert _shares_held({"sole_dispositive": 0, "shared_dispositive": 0}, None, in_group=False) is None


class TestTheSameSharesInBothRows:
    """Musk's Tesla 13G/A (amendment 17, June 2026) lists its 699,580,882
    shares as sole AND shared power, voting and dispositive alike, with the
    aggregate at the same 699,580,882. The lone-filer rule added the two rows
    and stored 1,399,161,764 — twice the holding, 35 % of Tesla by count
    beside the filed 19.9 %. The aggregate is the union of the power rows and
    caps the sum."""

    def test_the_real_filing(self):
        from app.scraper.sec_13dg_xml import _parse_13dg_xml, _shares_held, _stake_from_person
        d = _parse_13dg_xml(_fixture("13ga_musk_tesla.xml"))
        assert len(d["persons"]) == 1 and d["schedule"] == "13G"
        p = d["persons"][0]
        assert p["aggregate"] == 699_580_882
        assert p["sole_dispositive"] == p["shared_dispositive"] == 699_580_882
        rows = {k: p[k] for k in ("sole_voting", "shared_voting", "sole_dispositive",
                                  "shared_dispositive") if p.get(k) is not None}
        assert _shares_held(rows, p["aggregate"], in_group=False) == 699_580_882
        stake, voting = _stake_from_person(d, p)
        assert stake == 19.9 and voting is None

    def test_the_aggregate_caps_the_sum_only_when_the_rows_overlap(self):
        from app.scraper.sec_13dg_xml import _shares_held
        # overlapping rows: a sum above the aggregate is the aggregate
        assert _shares_held({"sole_dispositive": 100, "shared_dispositive": 60}, 120, in_group=False) == 120
        # disjoint rows (SoftBank's shape): the sum IS the aggregate
        assert _shares_held({"sole_dispositive": 100, "shared_dispositive": 900}, 1000, in_group=False) == 1000
        # an aggregate above the sum is no cap: dispositive power is the holding
        assert _shares_held({"sole_dispositive": 100, "shared_dispositive": 100}, 500, in_group=False) == 200
        # nothing to cap against
        assert _shares_held({"sole_dispositive": 100, "shared_dispositive": 100}, None, in_group=False) == 200
        # a bloc member is untouched: sole where it has any
        assert _shares_held({"sole_dispositive": 100, "shared_dispositive": 100}, 100, in_group=True) == 100

    def test_the_scrape_result_carries_the_whole_holding(self):
        from unittest.mock import patch
        from app.scraper import sec_13dg, sec_client, sec_filer
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><category term="SCHEDULE 13G/A"/><content type="text/xml">
            <filing-href>https://x.test/i.htm</filing-href>
            <filing-date>2025-05-15</filing-date>
            <accession-number>0000950170-25-071825</accession-number>
          </content></entry>
        </feed>"""
        url = ("https://www.sec.gov/Archives/edgar/data/1577552/"
               "000095017025071825/primary_doc.xml")
        with patch.object(sec_client, "_get_text",
                          side_effect=_serve(atom, {url: _fixture("13ga_softbank_alibaba.xml")})), \
             patch.object(sec_filer, "fetch_former_names", return_value=[]):
            res = sec_13dg.fetch_ownership_filings("Alibaba Group Holding Ltd", "0001577552")
        assert res and res[0]["shares"] == 630_790_344
        assert res[0]["stake_percent"] == 3.3
