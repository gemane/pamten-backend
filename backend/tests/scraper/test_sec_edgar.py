"""
Tests for sec_edgar.py.

Covers the pure helper functions (no HTTP) and the HTTP-calling functions
with mocked httpx responses. Specifically validates the bugs we hit and fixed
during development:
  - XSLT prefix stripping from primaryDocument
  - Using the ISSUER's CIK for Archives URL, not the filer's CIK
  - Name normalisation (SEC stores names as LAST FIRST)
  - company_tickers.json preferred over full-text search to avoid ambiguity
"""

import textwrap
from unittest.mock import patch, MagicMock

import pytest

from app.scraper.sec_edgar import (
    _normalize_sec_name,
    _title_to_role,
    _parse_form34_xml,
    _cik_int,
    _cik_from_accession,
    _filing_index_url,
    _ticker_normalize,
    _lookup_in_tickers,
    search_company,
    fetch_former_names,
    fetch_company_lei,
)


class TestFetchFormerNames:
    _SUBS = {"name": "Meta Platforms, Inc.", "formerNames": [
        {"name": "Facebook Inc", "from": "2005-05-06T00:00:00.000Z", "to": "2021-10-27T00:00:00.000Z"},
        {"name": "TheFacebook, Inc.", "from": "2004-01-01T00:00:00.000Z", "to": "2005-05-05T00:00:00.000Z"},
    ]}

    def test_returns_former_names_in_order(self):
        with patch("app.scraper.sec_edgar._get", return_value=self._SUBS):
            assert fetch_former_names("0001326801") == ["Facebook Inc", "TheFacebook, Inc."]

    def test_dedupes_case_insensitively(self):
        subs = {"formerNames": [{"name": "Square, Inc."}, {"name": "SQUARE, INC."}, {"name": ""}]}
        with patch("app.scraper.sec_edgar._get", return_value=subs):
            assert fetch_former_names("x") == ["Square, Inc."]

    def test_empty_or_missing(self):
        with patch("app.scraper.sec_edgar._get", return_value={"name": "Acme"}):
            assert fetch_former_names("x") == []

    def test_swallows_fetch_error(self):
        with patch("app.scraper.sec_edgar._get", side_effect=RuntimeError("404")):
            assert fetch_former_names("x") == []


class TestFetchCompanyLei:
    """The LEI from EDGAR submissions bridges a SEC entity to its GLEIF node."""

    def test_returns_reported_lei(self):
        with patch("app.scraper.sec_edgar._get",
                   return_value={"name": "X", "lei": "5493001KJTIIGC8Y1R12"}):
            assert fetch_company_lei("x") == "5493001KJTIIGC8Y1R12"

    def test_null_or_missing_lei(self):
        # Microsoft-style: field present but null, or absent entirely → None
        with patch("app.scraper.sec_edgar._get", return_value={"lei": None}):
            assert fetch_company_lei("x") is None
        with patch("app.scraper.sec_edgar._get", return_value={"name": "X"}):
            assert fetch_company_lei("x") is None

    def test_swallows_fetch_error(self):
        with patch("app.scraper.sec_edgar._get", side_effect=RuntimeError("404")):
            assert fetch_company_lei("x") is None


# ── Pure helpers ───────────────────────────────────────────────────────────────

class TestNormalizeSecName:
    """SEC stores individual names as 'LAST FIRST [MIDDLE]' — we flip them."""

    def test_two_word_name(self):
        assert _normalize_sec_name("MUSK ELON") == "Elon Musk"

    def test_three_word_name(self):
        assert _normalize_sec_name("COOK TIMOTHY D") == "Timothy D Cook"

    def test_already_one_word(self):
        # Graceful fallback: title-case it
        assert _normalize_sec_name("SATYA") == "Satya"

    def test_strips_trailing_punctuation(self):
        # Names sometimes have trailing periods or commas from SEC data
        assert _normalize_sec_name("MUSK, ELON.") == "Elon Musk"


class TestTitleToRole:
    """_title_to_role maps officer titles to canonical role strings."""

    def test_ceo(self):
        assert _title_to_role("Chief Executive Officer") == "CEO"

    def test_ceo_abbrev(self):
        assert _title_to_role("CEO") == "CEO"

    def test_cfo(self):
        assert _title_to_role("Chief Financial Officer") == "CFO"

    def test_cto(self):
        assert _title_to_role("Chief Technology Officer") == "CTO"

    def test_general_counsel(self):
        assert _title_to_role("General Counsel") == "General Counsel"

    def test_chairman(self):
        assert _title_to_role("Executive Chairman") == "Chairman"

    def test_president(self):
        assert _title_to_role("President") == "President"

    def test_vp_is_not_president(self):
        # "vice president" contains "president" but must NOT match
        role = _title_to_role("Vice President of Engineering")
        assert role != "President"

    def test_unknown_passthrough(self):
        # Non-standard titles are returned as-is
        assert _title_to_role("SVP Powertrain and Energy Eng.") == "SVP Powertrain and Energy Eng."

    def test_empty(self):
        assert _title_to_role("") == "Officer"


class TestCikHelpers:
    def test_cik_from_accession(self):
        assert _cik_from_accession("0001318605-22-000032") == "0001318605"

    def test_cik_int_strips_zeros(self):
        assert _cik_int("0001318605") == "1318605"

    def test_filing_index_url(self):
        # Readable EDGAR filing index page: /data/{cik-int}/{acc-nodash}/{acc}-index.htm
        assert _filing_index_url("0000320193", "0001104659-24-021466") == (
            "https://www.sec.gov/Archives/edgar/data/320193/"
            "000110465924021466/0001104659-24-021466-index.htm"
        )

    def test_filing_index_url_none_when_incomplete(self):
        assert _filing_index_url("", "0001104659-24-021466") is None
        assert _filing_index_url("320193", "") is None


class TestParseForm34Xml:
    """_parse_form34_xml extracts name/role from Form 3/4 XML."""

    def _make_xml(self, name: str, is_officer: str = "0", is_director: str = "0",
                   title: str = "") -> str:
        return textwrap.dedent(f"""
            <?xml version="1.0"?>
            <ownershipDocument>
              <reportingOwner>
                <reportingOwnerId>
                  <rptOwnerName>{name}</rptOwnerName>
                </reportingOwnerId>
                <reportingOwnerRelationship>
                  <isOfficer>{is_officer}</isOfficer>
                  <isDirector>{is_director}</isDirector>
                  <officerTitle>{title}</officerTitle>
                </reportingOwnerRelationship>
              </reportingOwner>
            </ownershipDocument>
        """).strip()

    def test_officer_with_title(self):
        xml = self._make_xml("MUSK ELON", is_officer="1", title="Chief Executive Officer")
        result = _parse_form34_xml(xml)
        assert result is not None
        assert result["name"] == "Elon Musk"
        assert result["role"] == "CEO"
        assert result["title"] == "Chief Executive Officer"

    def test_director(self):
        xml = self._make_xml("KIMBAL MUSK", is_director="1")
        result = _parse_form34_xml(xml)
        assert result is not None
        assert result["role"] == "Director"

    def test_neither_officer_nor_director_returns_none(self):
        # Pure investor (Form 4, non-affiliate) — should be skipped
        xml = self._make_xml("SOME FUND", is_officer="0", is_director="0")
        assert _parse_form34_xml(xml) is None

    def test_missing_reporting_owner_returns_none(self):
        xml = "<ownershipDocument><issuer/></ownershipDocument>"
        assert _parse_form34_xml(xml) is None

    def test_invalid_xml_returns_none(self):
        assert _parse_form34_xml("this is not xml") is None

    def test_xslt_rendered_html_returns_none(self):
        # When the XSLT-prefixed URL is fetched instead of raw XML, we get HTML
        html = "<html><body><p>Filing viewer</p></body></html>"
        assert _parse_form34_xml(html) is None


class TestXsltPrefixStripping:
    """
    Regression test for the XSLT-prefix bug.

    EDGAR's primaryDocument field sometimes contains a stylesheet prefix:
      'xslF345X06/tm2618092-2_4seq1.xml'
    Fetching that path returns an HTML-rendered view, not raw XML.
    The fix: take only the last path component.
    """

    def test_prefix_stripped(self):
        raw = "xslF345X06/tm2618092-2_4seq1.xml"
        fixed = raw.split("/")[-1] if "/" in raw else raw
        assert fixed == "tm2618092-2_4seq1.xml"

    def test_no_prefix_unchanged(self):
        raw = "form4.xml"
        fixed = raw.split("/")[-1] if "/" in raw else raw
        assert fixed == "form4.xml"


class TestTickerNormalize:
    def test_strips_inc(self):
        assert _ticker_normalize("Tesla, Inc.") == "tesla"

    def test_lowercases(self):
        assert _ticker_normalize("APPLE INC") == "apple"

    def test_passthrough(self):
        assert _ticker_normalize("Tesla") == "tesla"


class TestLookupInTickers:
    """_lookup_in_tickers searches a cached dict of EDGAR listed companies."""

    MOCK_TICKERS = {
        "0": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
        "1": {"cik_str": 789019,  "ticker": "MSFT", "title": "MICROSOFT CORP"},
        "2": {"cik_str": 320193,  "ticker": "AAPL", "title": "Apple Inc."},
    }

    def test_exact_match(self):
        with patch("app.scraper.sec_edgar._tickers_cache", self.MOCK_TICKERS):
            result = _lookup_in_tickers("Tesla")
        assert result is not None
        assert result["cik"] == "0001318605"
        assert result["name"] == "Tesla, Inc."

    def test_case_insensitive_match(self):
        with patch("app.scraper.sec_edgar._tickers_cache", self.MOCK_TICKERS):
            result = _lookup_in_tickers("microsoft")
        assert result is not None
        assert "MICROSOFT" in result["name"]

    def test_no_match_returns_none(self):
        with patch("app.scraper.sec_edgar._tickers_cache", self.MOCK_TICKERS):
            result = _lookup_in_tickers("Berkshire Hathaway")
        assert result is None

    def test_ambiguity_resolved_by_shortest_name(self):
        """
        Regression: searching "Apple" must NOT match "Apple Hospitality REIT".
        The tickers file always has the real Apple Inc., which normalises to
        'apple' (exact), while "Apple Hospitality REIT" normalises to something longer.
        Exact matches win; among them the shortest name is preferred.
        """
        tickers = {
            "0": {"cik_str": 320193,  "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 1418121, "ticker": "APLE", "title": "Apple Hospitality REIT, Inc."},
        }
        with patch("app.scraper.sec_edgar._tickers_cache", tickers):
            result = _lookup_in_tickers("Apple")
        assert result["name"] == "Apple Inc."


class TestSearchCompany:
    """search_company prefers tickers lookup and falls back to full-text search."""

    MOCK_TICKERS = {
        "0": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
    }

    def test_returns_from_tickers(self):
        with patch("app.scraper.sec_edgar._tickers_cache", self.MOCK_TICKERS):
            result = search_company("Tesla")
        assert result is not None
        assert result["cik"] == "0001318605"

    def test_full_text_fallback(self):
        """When tickers miss, falls back to EDGAR full-text search."""
        empty_tickers = {}
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "hits": {"hits": [{
                "_source": {
                    "display_names": ["PrivateCo  (CIK 0009999999)"],
                    "ciks":          ["9999999"],
                    "adsh":          "0009999999-22-000001",
                }
            }]}
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.scraper.sec_edgar._tickers_cache", empty_tickers), \
             patch("app.scraper.sec_edgar._get_client",
                   return_value=MagicMock(get=MagicMock(return_value=mock_response))):
            result = search_company("PrivateCo")

        assert result is not None
        assert result["cik"] == "0009999999"

    def test_returns_none_when_not_found(self):
        empty_tickers = {}
        mock_response = MagicMock()
        mock_response.json.return_value = {"hits": {"hits": []}}
        mock_response.raise_for_status = MagicMock()

        with patch("app.scraper.sec_edgar._tickers_cache", empty_tickers), \
             patch("app.scraper.sec_edgar._get_client",
                   return_value=MagicMock(get=MagicMock(return_value=mock_response))):
            result = search_company("NonExistentXYZ123")

        assert result is None


# ── Form 4 share-holding extraction ──────────────────────────────────────────

_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Fink Laurence D</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>500000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeHolding>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>510000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeHolding>
  </nonDerivativeTable>
</ownershipDocument>"""


def test_parse_form4_extracts_role_and_shares_owned():
    out = _parse_form34_xml(_FORM4_XML)
    assert out["role"] == "CEO"
    assert out["shares_owned"] == 510000.0   # largest sharesOwnedFollowingTransaction


def test_parse_form4_shares_none_when_absent():
    xml = """<ownershipDocument><reportingOwner>
      <reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
      <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
    </reportingOwner></ownershipDocument>"""
    out = _parse_form34_xml(xml)
    assert out["role"] == "Director" and out["shares_owned"] is None


# ── Person-centric insider holding (Option A) ────────────────────────────────

_SUBS = {"filings": {"recent": {
    "form": ["4"], "accessionNumber": ["0001059245-24-000123"],
    "primaryDocument": ["form4.xml"], "filingDate": ["2024-02-01"]}}}

def _form4(issuer_cik: str, shares: str) -> str:
    return (f"<x><issuerCik>{issuer_cik}</issuerCik>"
            f"<nonDerivativeTable><nonDerivativeTransaction><postTransactionAmounts>"
            f"<sharesOwnedFollowingTransaction><value>{shares}</value>"
            f"</sharesOwnedFollowingTransaction></postTransactionAmounts>"
            f"</nonDerivativeTransaction></nonDerivativeTable></x>")


def test_fetch_insider_holding_computes_stake_when_issuer_matches():
    from app.scraper.sec_edgar import fetch_insider_holding
    with patch("app.scraper.sec_edgar._lookup_person_cik", return_value="0001059245"), \
         patch("app.scraper.sec_edgar._get", return_value=_SUBS), \
         patch("app.scraper.sec_edgar._get_text", return_value=_form4("0001364742", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742", shares_outstanding=100000)
    assert out["shares_owned"] == 2000.0
    assert out["stake_percent"] == 2.0            # 2000 / 100000 * 100


def test_fetch_insider_holding_none_when_issuer_mismatch():
    from app.scraper.sec_edgar import fetch_insider_holding
    with patch("app.scraper.sec_edgar._lookup_person_cik", return_value="0001059245"), \
         patch("app.scraper.sec_edgar._get", return_value=_SUBS), \
         patch("app.scraper.sec_edgar._get_text", return_value=_form4("0000999999", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742", shares_outstanding=100000)
    assert out is None                            # Form 4 is about a different company


def test_fetch_insider_holding_none_when_no_cik():
    from app.scraper.sec_edgar import fetch_insider_holding
    with patch("app.scraper.sec_edgar._lookup_person_cik", return_value=None):
        assert fetch_insider_holding("Nobody", "0001364742") is None


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
        from app.scraper.sec_edgar import _parse_issuer_from_text
        assert _parse_issuer_from_text(self.EVE_COVER) == "Eve Holding, Inc"

    def test_boilerplate_before_the_name_is_trimmed(self):
        # The capture reaches back into "…Act of 1934 (Amendment No. 2)*"; only
        # the company name may survive.
        from app.scraper.sec_edgar import _parse_issuer_from_text
        got = _parse_issuer_from_text(self.EVE_COVER)
        assert "1934" not in got and "Amendment" not in got

    def test_text_format_underline_rules_are_trimmed(self):
        from app.scraper.sec_edgar import _parse_issuer_from_text
        assert _parse_issuer_from_text(self.TXT_COVER) == "EMBRAER S.A"

    def test_a_coverless_document_yields_none(self):
        from app.scraper.sec_edgar import _parse_issuer_from_text
        assert _parse_issuer_from_text("no cover page here at all") is None

    def test_the_wrong_issuer_is_a_mismatch(self):
        # The bug, distilled: a filing about Eve Holding must not survive a
        # scrape of Embraer.
        from app.scraper.sec_edgar import _issuer_matches, _parse_issuer_from_text
        issuer = _parse_issuer_from_text(self.EVE_COVER)
        assert _issuer_matches("Embraer", issuer) is False

    def test_legal_form_variants_agree(self):
        from app.scraper.sec_edgar import _issuer_matches
        assert _issuer_matches("Embraer", "EMBRAER S.A")
        assert _issuer_matches("Anheuser-Busch InBev", "Anheuser-Busch InBev SA/NV")
        assert _issuer_matches("Alphabet", "Alphabet Inc.")

    def test_diacritics_do_not_reject_a_real_owner(self):
        # "Nestlé" must tokenize to "nestle", not split on the é into a stump —
        # otherwise every legitimate filing about an accented-name company is
        # silently thrown away. This is a loss-of-owners bug, the exact harm
        # the verification must never cause.
        from app.scraper.sec_edgar import _issuer_matches
        assert _issuer_matches("Nestlé", "Nestle S.A.") is True
        assert _issuer_matches("Nestle", "Nestlé S.A.") is True

    def test_a_renamed_company_keeps_its_old_filings(self):
        # EDGAR keeps the CIK through a rename; covers from before it carry the
        # old name. The caller passes current + former names, and agreement with
        # ANY of them keeps the filing.
        from app.scraper.sec_edgar import _issuer_matches
        assert _issuer_matches(["Meta Platforms", "Facebook Inc"],
                               "Facebook, Inc.") is True
        assert _issuer_matches(["Meta Platforms", "Facebook Inc"],
                               "Eve Holding, Inc") is False

    def test_former_names_are_consulted_in_the_scan(self):
        # End to end: a filing whose cover names the OLD name survives a scrape
        # under the new one, because fetch_former_names is folded in.
        from unittest.mock import patch
        from app.scraper import sec_edgar

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
        with patch.object(sec_edgar, "_get_text",
                          side_effect=lambda url, params=None: pages.get(url, pages[None])), \
             patch.object(sec_edgar, "fetch_former_names",
                          return_value=["Facebook Inc"]) as former:
            results = sec_edgar.fetch_ownership_filings("Meta Platforms", "1326801")

        former.assert_called_once_with("1326801")
        assert [r["investor_name"] for r in results] == ["Vanguard Group Inc"]
        assert results[0]["stake_percent"] == 7.0

    def test_no_issuer_is_not_a_mismatch(self):
        # Old text filings may not parse. A positive mismatch is the only safe
        # ground to throw a filing away.
        from app.scraper.sec_edgar import _issuer_matches
        assert _issuer_matches("Embraer", None) is True

    def test_pure_legal_noise_never_decides(self):
        # A name that normalizes to nothing ("The Company Inc") must not veto —
        # there is no identity in it to disagree with. Both directions: as the
        # filing's issuer, and as the scraped company's own name.
        from app.scraper.sec_edgar import _issuer_matches
        assert _issuer_matches("Embraer", "The Company Inc.") is True
        assert _issuer_matches("The Group Inc", "Eve Holding, Inc") is True
        assert _issuer_matches(["The Group Inc", "Holdings Co"], "Eve Holding, Inc") is True

    def test_a_mismatched_filing_produces_no_investor(self):
        # End to end through fetch_ownership_filings: the Eve filing arrives via
        # the Atom feed exactly as the real one did (agent accession prefix, so
        # the outbound pre-filter does not catch it) and must be dropped at the
        # document stage; the BlackRock filing survives with its stake parsed.
        from unittest.mock import patch
        from app.scraper import sec_edgar

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
            None: atom,   # the browse call passes params, url is BROWSE_URL
            "https://x.test/eve-index.htm": eve_index,
            "https://x.test/blk-index.htm": blk_index,
            "https://x.test/self-index.htm": self_index,
            "https://www.sec.gov/Archives/edgar/data/1/self.htm":
                self.TXT_COVER + " PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11: 34.6% ",
            "https://www.sec.gov/Archives/edgar/data/1/eve.htm":
                self.EVE_COVER + " PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11: 83.0% ",
            "https://www.sec.gov/Archives/edgar/data/1/blk.htm": blk_doc,
        }

        def fake_get_text(url, params=None):
            return pages[url if url in pages else None]

        with patch.object(sec_edgar, "_get_text", side_effect=fake_get_text):
            results = sec_edgar.fetch_ownership_filings("Embraer", "1355444")

        names = [r["investor_name"] for r in results]
        assert "Embraer Aircraft Holding, Inc." not in str(names), \
            "the Eve Holding filing leaked through as an owner of Embraer"
        assert names == ["Blackrock Inc."], \
            "the company's own agent-filed 13D leaked through as an owner of itself"
        assert results[0]["stake_percent"] == 5.7
        assert results[0]["is_individual"] is False


class TestForm34IssuerVerification:
    """The wrong-subject bug's second door: a company's submissions feed lists
    Form 3/4s it FILED about other issuers, and parsing them put "EMBRAER S.A."
    into Embraer's executive list as a Director — isDirector meant director of
    Eve Holding. The XML names the issuer's CIK exactly, so unlike the 13D
    cover there is nothing fuzzy about the check."""

    def _xml(self, owner: str, issuer_cik: str | None) -> str:
        issuer = f"<issuer><issuerCik>{issuer_cik}</issuerCik></issuer>" if issuer_cik else ""
        return (f'<?xml version="1.0"?><ownershipDocument>{issuer}'
                f'<reportingOwner><reportingOwnerId>'
                f'<rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>'
                f'<reportingOwnerRelationship><isDirector>1</isDirector>'
                f'</reportingOwnerRelationship></reportingOwner>'
                f'</ownershipDocument>')

    def test_the_parser_reports_the_issuer(self):
        out = _parse_form34_xml(self._xml("EMBRAER S.A.", "0001823652"))
        assert out["issuer_cik"] == "0001823652"

    def test_a_form_about_another_issuer_is_dropped(self):
        # Embraer (CIK 1355444) scanning its feed must not keep a Form 4 whose
        # issuer is Eve Holding (1823652), whoever the reporting owner is.
        from unittest.mock import patch
        from app.scraper import sec_edgar

        submissions = {"filings": {"recent": {
            "form":            ["4", "4"],
            "accessionNumber": ["0001292814-24-000001", "0000000001-24-000002"],
            "primaryDocument": ["eve.xml", "own.xml"],
            "filingDate":      ["2024-09-05", "2024-09-06"],
        }}}
        docs = {
            "eve.xml": self._xml("EMBRAER S.A.", "0001823652"),      # about Eve
            "own.xml": self._xml("GOMES NETO FRANCISCO", "1355444"),  # about Embraer
        }

        def fake_get(url, params=None):
            return submissions

        def fake_get_text(url, params=None):
            return docs[url.rsplit("/", 1)[-1]]

        with patch.object(sec_edgar, "_get", side_effect=fake_get), \
             patch.object(sec_edgar, "_get_text", side_effect=fake_get_text):
            execs = sec_edgar.fetch_executives("1355444")

        names = [e["name"] for e in execs]
        assert "Embraer S.A." not in str(names) and "EMBRAER" not in str(names), \
            "the Eve Holding Form 4 leaked the company into its own executive list"
        assert len(execs) == 1 and "Francisco" in execs[0]["name"]

    def test_a_form_with_no_issuer_cik_is_kept(self):
        # Absent metadata is not a mismatch — same principle as the 13D check.
        out = _parse_form34_xml(self._xml("SOME PERSON", None))
        assert out is not None and out["issuer_cik"] is None

    def test_padding_does_not_defeat_the_comparison(self):
        # issuerCik may or may not be zero-padded; 1355444 == 0001355444.
        from unittest.mock import patch
        from app.scraper import sec_edgar

        submissions = {"filings": {"recent": {
            "form": ["4"], "accessionNumber": ["0000000001-24-000003"],
            "primaryDocument": ["p.xml"], "filingDate": ["2024-01-01"],
        }}}
        with patch.object(sec_edgar, "_get", return_value=submissions), \
             patch.object(sec_edgar, "_get_text",
                          return_value=self._xml("GOMES NETO FRANCISCO", "0001355444")):
            execs = sec_edgar.fetch_executives("1355444")
        assert len(execs) == 1


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
        from app.scraper.sec_edgar import _parse_power_rows
        rows = _parse_power_rows(self.ALTRIA)
        assert rows == {"sole_voting": 0, "shared_voting": 1020598157,
                        "sole_dispositive": 159121937, "shared_dispositive": 0}

    def test_a_missing_row_is_absent_not_zero(self):
        # "the filing did not say" must not become a genuine 0% stake.
        from app.scraper.sec_edgar import _parse_power_rows
        assert "sole_dispositive" not in _parse_power_rows("Sole Voting Power 5,000")

    def test_the_denominator_comes_from_the_filing(self):
        from app.scraper.sec_edgar import _shares_outstanding
        assert _shares_outstanding(self.ALTRIA) == 1975913221
        assert _shares_outstanding("no denominator here") is None

    def test_a_group_member_gets_its_own_stake_and_the_bloc_as_voting(self):
        # The heart of it: Altria's real holding is 159.1M/1.976B = 8.05%, and
        # the 51.7% is the Voting Agreement bloc, not Altria's shares.
        from app.scraper.sec_edgar import _own_stake_and_voting
        stake, voting = _own_stake_and_voting(self.ALTRIA, 51.7)
        assert stake == pytest.approx(8.05, abs=0.01)
        assert voting == 51.7

    def test_a_lone_filer_is_untouched(self):
        # No group, no bloc: the common case must not change at all.
        from app.scraper.sec_edgar import _own_stake_and_voting
        assert _own_stake_and_voting(self.LONE, 5.7) == (5.7, None)

    def test_a_purely_joint_holder_gets_no_invented_stake(self):
        # BRC can dispose of nothing alone — its shares sit in the Stichting it
        # co-owns with EPS. 0.0 would read as "owns nothing", the opposite of
        # the truth, so the stake is unknown and only the bloc is stated.
        from app.scraper.sec_edgar import _own_stake_and_voting
        stake, voting = _own_stake_and_voting(self.JOINT_ONLY, 52.3)
        assert stake is None
        assert voting == 52.3

    def test_no_denominator_means_no_invented_stake(self):
        # The bloc is real but unquantifiable per member; keeping the bloc
        # figure as the stake is exactly what produced 109.9%.
        from app.scraper.sec_edgar import _own_stake_and_voting
        text = self.ALTRIA.split("based on")[0]
        assert _own_stake_and_voting(text, 51.7) == (None, 51.7)

    def test_the_sums_stop_exceeding_the_company(self):
        # The regression this exists to prevent, in one assertion: the three
        # real AB InBev filings can no longer add up to more than 100%.
        from app.scraper.sec_edgar import _own_stake_and_voting
        bevco = ("Sole Voting Power 0 Shared Voting Power 102,862,718 "
                 "Sole Dispositive Power 102,862,718 Shared Dispositive Power 0 "
                 "Percent of Class Represented by Amount in Row 11 5.9%")
        stakes = [_own_stake_and_voting(t, p)[0] for t, p in
                  ((self.ALTRIA, 51.7), (self.JOINT_ONLY, 52.3), (bevco, 5.9))]
        assert sum(s for s in stakes if s is not None) < 100

    def test_the_bloc_reaches_the_scrape_result(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar

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

        with patch.object(sec_edgar, "_get_text",
                          side_effect=lambda url, params=None: pages.get(url, pages[None])), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Anheuser-Busch InBev", "1668717")

        assert len(res) == 1
        assert res[0]["stake_percent"] == pytest.approx(8.05, abs=0.01)
        assert res[0]["voting_power_pct"] == 51.7
