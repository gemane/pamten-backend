"""One passive statement, one holder: newest wins across filer CIKs (findings item 93).

Berkshire Hathaway's 13G/As on Sirius XM name the same eleven reporting persons —
Warren E. Buffett, Berkshire, National Indemnity, GEICO, the pension trusts — each
with the whole block. EDGAR filed the Feb-2025 amendment under Buffett's CIK and
the Nov-2025 one under Berkshire's, and the reader, deduplicating by the filer's
CIK alone, kept both: Berkshire 37.1 % AND Buffett 34.6 % of the same shares.
The fixtures are the two real filings.
"""
import re
from unittest.mock import patch

from app.scraper import sec_edgar
from tests.scraper.test_sec_ownership_feed_paging import _Feed, _entry, _fixture, _page

ISSUER = "0000908937"              # Sirius XM Holdings Inc.
NEWER = "0001193125-25-283617"     # filed under Berkshire's CIK, as of 2025-09-30, 37.1 %
OLDER = "0000950170-25-021169"     # filed under Buffett's CIK, as of 2024-12-31, 34.6 %
FILER_NAMES = {"0001067983": "BERKSHIRE HATHAWAY INC", "0000315090": "BUFFETT WARREN E",
               "0000999999": "SOMEONE ELSE HOLDINGS LLC"}


def _xml_url(acc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/908937/{acc.replace('-', '')}/primary_doc.xml"


BERKSHIRE = _fixture("13ga_berkshire_siriusxm_2025.xml")
BUFFETT = _fixture("13ga_buffett_siriusxm_2025.xml")


def _zeroed(xml: str) -> str:
    """The same filing reporting nothing held — a group exit."""
    for tag in ("reportingPersonBeneficiallyOwnedAggregateNumberOfShares", "classPercent",
                "soleVotingPower", "sharedVotingPower", "soleDispositivePower",
                "sharedDispositivePower"):
        xml = re.sub(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>0</{tag}>", xml)
    return xml


def _stranger(xml: str) -> str:
    """Buffett's filing as if a party named on no other schedule had filed it."""
    xml = xml.replace("0000315090", "0000999999")
    xml = re.sub(r"<reportingPersonName>[^<]*</reportingPersonName>",
                 "<reportingPersonName>Someone Else Holdings LLC</reportingPersonName>", xml)
    return xml


def _run(docs: dict, entries: list[str]):
    feed = _Feed([_page(entries)], docs)
    with patch.object(sec_edgar, "_get_text", side_effect=feed), \
         patch.object(sec_edgar, "fetch_former_names", return_value=[]), \
         patch.object(sec_edgar, "fetch_filer_name", side_effect=FILER_NAMES.get):
        return sec_edgar.fetch_ownership_filings("Sirius XM Holdings Inc.", ISSUER)


def _both(newer_xml=BERKSHIRE, older_xml=BUFFETT):
    return _run({_xml_url(NEWER): newer_xml, _xml_url(OLDER): older_xml},
                [_entry(NEWER, "2025-11-14"), _entry(OLDER, "2025-02-14")])


def test_the_older_amendment_under_another_reporting_persons_cik_is_the_same_statement():
    res = _both()
    assert [(r["investor_cik"], r["stake_percent"]) for r in res] == [("0001067983", 37.1)]


def test_the_newest_filing_wins_whichever_cik_it_is_filed_under():
    """The rule is the feed's order, not a preference for the company."""
    res = _run({_xml_url(NEWER): BERKSHIRE, _xml_url(OLDER): BUFFETT},
               [_entry(OLDER, "2025-11-14"), _entry(NEWER, "2025-02-14")])
    assert [r["investor_cik"] for r in res] == ["0000315090"]


def test_a_filer_named_on_no_newer_schedule_stays_a_holder():
    res = _both(older_xml=_stranger(BUFFETT))
    assert sorted(r["investor_cik"] for r in res) == ["0000999999", "0001067983"]


def test_a_13d_group_member_filing_under_its_own_cik_is_not_skipped():
    """In a bloc every member reports shares of its own: not a repetition."""
    with patch.object(sec_edgar, "_co_filers_form_a_bloc", return_value=True):
        res = _both()
    assert sorted(r["investor_cik"] for r in res) == ["0000315090", "0001067983"]


def test_a_newer_group_exit_closes_the_holding_filed_under_another_cik():
    res = _both(newer_xml=_zeroed(BERKSHIRE))
    assert [(r["investor_cik"], r["until"]) for r in res] == [("0000315090", "2025-09-30")]


def test_the_skipped_filer_is_not_read_again_from_an_even_older_filing():
    """Once the statement is taken, every older filing of that filer repeats it too."""
    oldest = "0000950170-24-000001"
    res = _run({_xml_url(NEWER): BERKSHIRE, _xml_url(OLDER): BUFFETT, _xml_url(oldest): BUFFETT},
               [_entry(NEWER, "2025-11-14"), _entry(OLDER, "2025-02-14"),
                _entry(oldest, "2024-02-14")])
    assert [r["investor_cik"] for r in res] == ["0001067983"]


def test_cover_page_names_reads_one_name_per_cover_page():
    text = ("<p>1. NAMES OF REPORTING PERSONS Warren E. Buffett 2. CHECK THE APPROPRIATE BOX</p>"
            "<p>1. Names of Reporting Persons Berkshire Hathaway Inc. "
            "I.R.S. Identification Nos. 47-0813844</p>")
    assert sec_edgar._cover_page_names(text) == ["Warren E. Buffett", "Berkshire Hathaway Inc"]


def test_named_in_matches_edgars_word_order():
    assert sec_edgar._named_in("BUFFETT WARREN E", ["Berkshire Hathaway Inc.", "Warren E. Buffett"])
    assert not sec_edgar._named_in("BUFFETT HOWARD G", ["Warren E. Buffett"])
    assert not sec_edgar._named_in(None, ["Warren E. Buffett"])



def test_the_newer_filers_own_name_under_another_cik_is_another_company():
    """BlackRock's old parent and the new BlackRock, Inc. share a name, not a statement:
    the old parent's holding is `close_reported_by_parent`'s to end, not this rule's."""
    namesake = _stranger(BUFFETT).replace("Someone Else Holdings LLC", "Berkshire Hathaway Inc.")
    names = dict(FILER_NAMES, **{"0000999999": "BERKSHIRE HATHAWAY INC"})
    feed = _Feed([_page([_entry(NEWER, "2025-11-14"), _entry(OLDER, "2025-02-14")])],
                 {_xml_url(NEWER): BERKSHIRE, _xml_url(OLDER): namesake})
    with patch.object(sec_edgar, "_get_text", side_effect=feed), \
         patch.object(sec_edgar, "fetch_former_names", return_value=[]), \
         patch.object(sec_edgar, "fetch_filer_name", side_effect=names.get):
        res = sec_edgar.fetch_ownership_filings("Sirius XM Holdings Inc.", ISSUER)
    assert sorted(r["investor_cik"] for r in res) == ["0000999999", "0001067983"]
