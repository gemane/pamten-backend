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


def _serve(atom: str, pages: dict):
    """A fake `_get_text` that 404s on anything it was not given.

    The old idiom fell back to the Atom feed for an unknown URL, so a code path
    that started requesting a new document (say `primary_doc.xml`) would be
    handed a feed, fail to parse it, quietly take the fallback branch, and the
    test would pass for the wrong reason. Unknown URLs now raise the way EDGAR
    does, which is what makes these tests able to say the legacy path was used.
    """
    import httpx

    def _get_text(url, params=None):
        if params is not None:      # the browse call is the only one with params
            return atom
        if url not in pages:
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", url),
                response=httpx.Response(404))
        return pages[url]
    return _get_text


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
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
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
            "https://x.test/eve-index.htm": eve_index,
            "https://x.test/blk-index.htm": blk_index,
            "https://x.test/self-index.htm": self_index,
            "https://www.sec.gov/Archives/edgar/data/1/self.htm":
                self.TXT_COVER + " PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11: 34.6% ",
            "https://www.sec.gov/Archives/edgar/data/1/eve.htm":
                self.EVE_COVER + " PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11: 83.0% ",
            "https://www.sec.gov/Archives/edgar/data/1/blk.htm": blk_doc,
        }


        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)):
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

        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Anheuser-Busch InBev", "1668717")

        assert len(res) == 1
        assert res[0]["stake_percent"] == pytest.approx(8.05, abs=0.01)
        assert res[0]["voting_power_pct"] == 51.7


def _fixture(name: str) -> str:
    """A real filing, trimmed to what the parser reads.

    Trimmed, never invented: the 13D/13G schema split was invisible for exactly
    as long as the fixtures were written from imagination.
    """
    import pathlib
    return (pathlib.Path(__file__).parent / "fixtures" / name).read_text()


class TestStructuredFilings:
    """The SEC's Dec-2024 modernization made 13D/G machine-readable, which is
    where the group membership behind AB InBev's voting bloc comes from — no
    Item 6 prose. Two schedules, two sets of tag names for the same facts."""

    def test_reads_the_13g_spelling(self):
        from app.scraper.sec_edgar import _parse_13dg_xml
        d = _parse_13dg_xml(_fixture("13g_vanguard.xml"))
        assert d["issuer_cik"] == "0000320193"
        assert d["issuer_name"] == "Apple Inc"
        assert len(d["persons"]) == 1
        assert d["persons"][0]["percent"] == 7.48
        assert d["persons"][0]["type_code"] == "IA"

    def test_reads_the_13d_spelling(self):
        # issuerCIK / percentOfClass / reportingPersonInfo — a 13G-only reader
        # returns nothing at all here, which is what used to happen.
        from app.scraper.sec_edgar import _parse_13dg_xml
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
        from app.scraper.sec_edgar import _parse_13dg_xml
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
        from app.scraper.sec_edgar import _parse_13dg_xml
        d = _parse_13dg_xml(_fixture("13ga_wellington.xml"))
        assert 5.36 not in [p["percent"] for p in d["persons"]]

    def test_the_security_the_percentages_measure_is_captured(self):
        # Without this, 22.3% of one share class and 9.7% of another look
        # addable — which is how Grupo Televisa reached 115.9% of itself.
        from app.scraper.sec_edgar import _parse_13dg_xml
        assert _parse_13dg_xml(_fixture("13g_vanguard.xml"))["class_title"] == "Common Stock"
        assert _parse_13dg_xml(_fixture("13d_tactical.xml"))["class_title"] == \
            "Common Shares, par value $0.0001 per share"

    def test_decimal_and_integer_share_counts_both_parse(self):
        from app.scraper.sec_edgar import _parse_13dg_xml
        d13d = _parse_13dg_xml(_fixture("13d_tactical.xml"))   # "1598232.00"
        d13g = _parse_13dg_xml(_fixture("13g_vanguard.xml"))   # "145321305"
        assert d13d["persons"][0]["sole_dispositive"] == 1598232
        assert d13g["persons"][0]["sole_voting"] == 145321305

    def test_an_absent_row_stays_absent(self):
        # None and 0 mean different things to _split_stake; conflating them
        # invents a 0% stake.
        from app.scraper.sec_edgar import _xml_num
        import xml.etree.ElementTree as ET
        el = ET.fromstring("<a><soleVotingPower>0</soleVotingPower></a>")
        assert _xml_num(el, "soleVotingPower") == 0
        assert _xml_num(el, "soleDispositivePower") is None

    def test_junk_is_not_a_filing(self):
        from app.scraper.sec_edgar import _parse_13dg_xml
        assert _parse_13dg_xml("<html>404</html>") is None
        assert _parse_13dg_xml("not xml at all <<<") is None


class TestClassTitleFromCoverPages:
    def test_the_title_is_read_off_a_cover(self):
        from app.scraper.sec_edgar import _parse_class_title_from_text
        assert _parse_class_title_from_text(
            "Anheuser-Busch InBev SA/NV (Name of Issuer) "
            "Ordinary Shares, without nominal value (Title of Class of Securities)"
        ) == "Ordinary Shares, without nominal value"

    def test_the_issuer_line_is_not_swallowed_into_it(self):
        from app.scraper.sec_edgar import _parse_class_title_from_text
        got = _parse_class_title_from_text(
            "SCHEDULE 13D Grupo Televisa (Name of Issuer) "
            "Series A Shares (Title of Class of Securities)")
        assert got == "Series A Shares"

    def test_a_cover_without_the_label_yields_nothing(self):
        from app.scraper.sec_edgar import _parse_class_title_from_text
        assert _parse_class_title_from_text("no class label anywhere here") is None


class TestEraDetection:
    def test_modern_form_names_have_xml(self):
        from app.scraper.sec_edgar import _is_structured
        assert _is_structured("SCHEDULE 13D") is True
        assert _is_structured("SCHEDULE 13G/A") is True

    def test_legacy_form_names_do_not(self):
        from app.scraper.sec_edgar import _is_structured
        assert _is_structured("SC 13D") is False
        assert _is_structured("SC 13G/A") is False

    def test_other_schedules_are_not_mistaken_for_13s(self):
        # "SCHEDULE" alone would catch SCHEDULE TO-I and friends.
        from app.scraper.sec_edgar import _is_structured
        assert _is_structured("SCHEDULE TO-I") is False
        assert _is_structured("SCHEDULE 14A") is False


class TestXmlIssuerVerification:
    """Two tiers. The CIK is strong but agent-typed — the Embraer mis-file came
    from that same keyboard — while the name can be years out of date."""

    def test_the_matching_cik_is_enough(self):
        from app.scraper.sec_edgar import _xml_issuer_matches
        xml = {"issuer_cik": "0000320193", "issuer_name": "Apple Inc"}
        assert _xml_issuer_matches(xml, "320193", ["Apple Inc"]) is True

    def test_a_stale_name_with_the_right_cik_survives(self):
        # Wellington's real filing: correct CIK, "The NASDAQ OMX Group, Inc."
        from app.scraper.sec_edgar import _xml_issuer_matches
        xml = {"issuer_cik": "0001120193", "issuer_name": "The NASDAQ OMX Group, Inc."}
        assert _xml_issuer_matches(xml, "1120193", ["Nasdaq, Inc."]) is True

    def test_the_matching_cik_carries_a_name_that_shares_nothing(self):
        # The case the CIK tier exists for: a rename with no word in common.
        # Alphabet's CIK still carries filings that say "Google Inc." — token
        # overlap is zero, so only the CIK keeps this real owner.
        from app.scraper.sec_edgar import _xml_issuer_matches
        xml = {"issuer_cik": "0001652044", "issuer_name": "Google Inc."}
        assert _xml_issuer_matches(xml, "1652044", ["Alphabet Inc."]) is True

    def test_a_wrong_cik_with_a_matching_name_survives(self):
        # The mirror case: the agent typed the wrong CIK but the name is ours.
        from app.scraper.sec_edgar import _xml_issuer_matches
        xml = {"issuer_cik": "0009999999", "issuer_name": "Embraer S.A."}
        assert _xml_issuer_matches(xml, "1355444", ["Embraer"]) is True

    def test_a_filing_about_another_company_is_dropped(self):
        # Eve Holding, in structured form: both tiers disagree.
        from app.scraper.sec_edgar import _xml_issuer_matches
        xml = {"issuer_cik": "0001823652", "issuer_name": "Eve Holding, Inc."}
        assert _xml_issuer_matches(xml, "1355444", ["Embraer"]) is False

    def test_no_stated_issuer_is_not_a_mismatch(self):
        from app.scraper.sec_edgar import _xml_issuer_matches
        assert _xml_issuer_matches({"issuer_cik": None}, "1355444", ["Embraer"]) is True


class TestStakeFromStructuredFilings:
    def test_a_stated_denominator_is_preferred_over_a_derived_one(self):
        # The two must disagree or the test proves nothing. Stated 1,250,000
        # gives 8.0%; deriving from 500,000 at 50% would give 1,000,000 and
        # therefore 10.0%. `percentOfClass` is often two significant figures,
        # so the stated figure is the better one whenever the filer gives it.
        from app.scraper.sec_edgar import _stake_from_person
        xml = {"comment_text": "based on a total of 1,250,000 shares issued and outstanding"}
        person = {"name": "X", "sole_voting": 0, "shared_voting": 500000,
                  "sole_dispositive": 100000, "shared_dispositive": 0,
                  "aggregate": 500000, "percent": 50.0}
        stake, voting = _stake_from_person(xml, person)
        assert stake == 8.0, "the derived denominator was used despite a stated one"
        assert voting == 50.0

    def test_derivation_is_the_fallback_when_nothing_is_stated(self):
        from app.scraper.sec_edgar import _stake_from_person
        person = {"name": "X", "sole_voting": 0, "shared_voting": 1020598157,
                  "sole_dispositive": 159121937, "shared_dispositive": 0,
                  "aggregate": 1020598157, "percent": 51.7}
        stake, voting = _stake_from_person({"comment_text": ""}, person)
        assert stake == pytest.approx(8.06, abs=0.01)   # Altria's real holding
        assert voting == 51.7

    def test_a_zero_percent_amendment_does_not_divide_by_zero(self):
        # A 13G/A reporting 0% is a filer announcing it has exited; common.
        from app.scraper.sec_edgar import _derive_total
        assert _derive_total(0, 0.0) is None
        assert _derive_total(1000, 0.0) is None
        assert _derive_total(None, 5.0) is None

    def test_a_lone_filer_keeps_its_reported_percentage(self):
        from app.scraper.sec_edgar import _parse_13dg_xml, _stake_from_person
        d = _parse_13dg_xml(_fixture("13g_vanguard.xml"))
        stake, voting = _stake_from_person(d, d["persons"][0])
        assert stake == 7.48 and voting is None


class TestPersonSelection:
    def test_the_filer_is_picked_out_of_the_group(self):
        from app.scraper.sec_edgar import _select_person
        xml = {"persons": [{"name": "A", "cik": "0000000111"},
                           {"name": "B", "cik": "0000000222"}]}
        assert _select_person(xml, "222")["name"] == "B"

    def test_the_first_block_is_used_when_no_cik_matches(self):
        # 13G person blocks carry no CIK at all.
        from app.scraper.sec_edgar import _select_person
        xml = {"persons": [{"name": "A", "cik": None}, {"name": "B", "cik": None}]}
        assert _select_person(xml, "999")["name"] == "A"

    def test_no_persons_means_no_selection(self):
        from app.scraper.sec_edgar import _select_person
        assert _select_person({"persons": []}, "1") is None


class TestGroupMembersFromSgml:
    def test_the_header_lines_are_read_one_per_member(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        header = ("<pre>GROUP MEMBERS:\t\tJORGE PAULO LEMANN\n"
                  "GROUP MEMBERS:\t\tSTICHTING ANHEUSER-BUSCH INBEV\n"
                  "GROUP MEMBERS:\t\tRAYVAX SOCIETE D INVESTISSEMENTS S.A.\n"
                  "SUBJECT COMPANY:\nCOMPANY CONFORMED NAME: Anheuser-Busch InBev SA/NV\n</pre>")
        with patch.object(sec_edgar, "_get_text", return_value=header):
            got = sec_edgar._sgml_group_members("1668717", "0001193125-24-230284")
        assert [g["name"] for g in got] == [
            "JORGE PAULO LEMANN", "STICHTING ANHEUSER-BUSCH INBEV",
            "RAYVAX SOCIETE D INVESTISSEMENTS S.A."]
        assert all(g["source"] == "sgml" and g["cik"] is None for g in got)

    def test_the_rest_of_the_header_is_not_swallowed(self):
        # The header is line-oriented. Flattening whitespace first makes a
        # single match run to the end of the document and return the whole
        # header as one enormous "name" — which is what happened.
        from unittest.mock import patch
        from app.scraper import sec_edgar
        header = "GROUP MEMBERS:\tA CORP\nSUBJECT COMPANY:\tSOMETHING ELSE\n"
        with patch.object(sec_edgar, "_get_text", return_value=header):
            got = sec_edgar._sgml_group_members("1", "0000000000-00-000000")
        assert [g["name"] for g in got] == ["A CORP"]

    def test_a_missing_header_is_not_fatal(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_get_text", side_effect=Exception("404")):
            assert sec_edgar._sgml_group_members("1", "0000000000-00-000000") == []


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
        from app.scraper import sec_edgar
        pages = {self.XML_URL: _fixture("13g_vanguard.xml")}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]), \
             patch.object(sec_edgar, "_fetch_filing_index") as index:
            res = sec_edgar.fetch_ownership_filings("Apple Inc", "0000320193")
        assert len(res) == 1
        assert res[0]["investor_name"] == "Vanguard Capital Management"
        assert res[0]["stake_percent"] == 7.48
        assert res[0]["is_individual"] is False          # IA is an entity
        index.assert_not_called(), "the XML path must not fetch the index page"

    def test_the_share_class_reaches_the_result(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        pages = {self.XML_URL: _fixture("13g_vanguard.xml")}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Apple Inc", "0000320193")
        assert res[0]["share_class"] == "Common Stock"

    def test_the_provenance_link_is_still_the_readable_index(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        pages = {self.XML_URL: _fixture("13g_vanguard.xml")}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Apple Inc", "0000320193")
        assert res[0]["source_url"].endswith("-index.htm")

    def test_the_other_group_members_come_back(self):
        # Wellington files for four entities; three are the group.
        from unittest.mock import patch
        from app.scraper import sec_edgar
        atom = self.ATOM.replace("0002100119-26-000139", "0000902219-26-000292")
        url = ("https://www.sec.gov/Archives/edgar/data/1120193/"
               "000090221926000292/primary_doc.xml")
        with patch.object(sec_edgar, "_get_text",
                          side_effect=_serve(atom, {url: _fixture("13ga_wellington.xml")})), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Nasdaq, Inc.", "1120193")
        assert len(res) == 1
        names = [g["name"] for g in res[0]["group_members"]]
        assert len(names) == 3
        assert "Wellington Management Company LLP" in names
        assert res[0]["investor_name"] not in names, "the filer is not its own group member"

    def test_a_modern_filing_whose_xml_is_missing_falls_back(self):
        # Coverage is not total even post-mandate; the HTML path must still run.
        from unittest.mock import patch
        from app.scraper import sec_edgar
        html = ('<span class="companyName">Someone Corp (Filed by)</span>'
                '<a href="x">CIK=0000000123</a>'
                '<table><tr><td><a href="/Archives/edgar/data/1/d.htm">d</a></td>'
                '<td>SC 13G</td></tr></table>')
        doc = ("Apple Inc (Name of Issuer) Common Stock (Title of Class of Securities) "
               "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11 6.0%")
        pages = {"https://x.test/i.htm": html,
                 "https://www.sec.gov/Archives/edgar/data/1/d.htm": doc}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Apple Inc", "0000320193")
        assert len(res) == 1 and res[0]["stake_percent"] == 6.0
        # Pre-2024 filings have no XML, so the cover page is the only place the
        # class is stated — and it is stated on every one of them.
        assert res[0]["share_class"] == "Common Stock"

    def test_an_unverifiable_filing_is_dropped_not_admitted(self):
        # A modern index page offers no SC-13-typed .htm, so primary_url is
        # None and there is nothing to check the issuer against. Admitting it
        # is how the Eve Holding rows got in.
        from unittest.mock import patch
        from app.scraper import sec_edgar
        html = ('<span class="companyName">Someone Corp (Filed by)</span>'
                '<a href="x">CIK=0000000123</a>'
                '<table><tr><td><a href="/Archives/edgar/data/1/x.htm">x</a></td>'
                '<td>EX-99</td></tr></table>')
        pages = {"https://x.test/i.htm": html}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(self.ATOM, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Apple Inc", "0000320193")
        assert res == []

    def test_a_dropped_filing_does_not_burn_the_investors_slot(self):
        # Two filings by one investor: the first names the wrong issuer, the
        # second is correct. Claiming the CIK before verification lost the good
        # one — and the wrong-issuer filing is the newer, so it is seen first.
        from unittest.mock import patch
        from app.scraper import sec_edgar
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
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Embraer", "1355444")
        assert [r["stake_percent"] for r in res] == [9.9]

    def test_the_feed_asks_for_both_form_name_eras(self):
        # "SC 13" is a PREFIX match and the forms were renamed to "SCHEDULE 13G"
        # in Dec 2024 — asking for "SC 13" silently returned nothing newer than
        # early 2024. This is the one that made the scraper go blind.
        from unittest.mock import patch
        from app.scraper import sec_edgar
        seen = {}

        def capture(url, params=None):
            if params:
                seen.update(params)
                return "<feed xmlns='http://www.w3.org/2005/Atom'></feed>"
            raise AssertionError("no document should be fetched")

        with patch.object(sec_edgar, "_get_text", side_effect=capture):
            sec_edgar.fetch_ownership_filings("Apple Inc", "0000320193")
        assert seen["type"] == "SC", f"feed asked for {seen['type']!r}"


class TestShareCountsAreKept:
    """A count is what the filing states; a percentage is a division we perform
    against a denominator that moves. Bevco's 5.9% went stale purely because AB
    InBev issued more shares — its holding never changed."""

    def test_a_lone_filer_keeps_its_holding(self):
        from app.scraper.sec_edgar import _shares_held
        assert _shares_held({"sole_dispositive": 1099168953}, None) == 1099168953

    def test_dispositive_power_is_what_counts_not_voting(self):
        # What the filer can sell is what it owns. Altria votes a billion shares
        # and can dispose of 159 million.
        from app.scraper.sec_edgar import _shares_held
        rows = {"sole_voting": 0, "shared_voting": 1020598157,
                "sole_dispositive": 159121937, "shared_dispositive": 0}
        assert _shares_held(rows, 1020598157) == 159121937

    def test_a_purely_joint_holder_reports_what_it_disposes_of_jointly(self):
        # BRC can sell nothing alone, but the Stichting it co-owns holds
        # 771,096,582 — a real number, where stake_percent has to say None.
        from app.scraper.sec_edgar import _shares_held
        rows = {"sole_dispositive": 0, "shared_dispositive": 771096582}
        assert _shares_held(rows, 1033081237) == 771096582

    def test_the_aggregate_is_the_last_resort(self):
        from app.scraper.sec_edgar import _shares_held
        assert _shares_held({}, 5000) == 5000
        assert _shares_held({}, None) is None

    def test_the_counts_reach_the_scrape_result(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
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
        with patch.object(sec_edgar, "_get_text",
                          side_effect=_serve(atom, {url: _fixture("13g_vanguard.xml")})), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Apple Inc", "0000320193")
        assert res[0]["shares"] == 1099168953
        # 7.48% of what? The stake is checkable only if both numbers survive.
        assert res[0]["stake_percent"] == 7.48

    def test_a_filing_without_counts_stores_none_rather_than_zero(self):
        # Absent is not zero: a nil holding and an unstated one are different
        # facts, and a 0 here would read as "sold out".
        from app.scraper.sec_edgar import _shares_held
        assert _shares_held({"sole_dispositive": 0, "shared_dispositive": 0}, None) is None


class TestTheBlocsOwnCount:
    """`voting_power_pct` has a numerator too — row 11. Like the percentage it
    belongs to the GROUP and is repeated verbatim by every member, so it may
    never be summed across owners."""

    def test_a_bloc_reports_its_count(self):
        from app.scraper.sec_edgar import _shares_voted
        assert _shares_voted(1020598157, 51.7) == 1020598157

    def test_a_lone_filer_has_no_bloc_count(self):
        # No bloc, no number — not zero, and not its own holding restated.
        from app.scraper.sec_edgar import _shares_voted
        assert _shares_voted(32416315, None) is None

    def test_row_eleven_is_read_off_a_cover(self):
        from app.scraper.sec_edgar import _parse_aggregate_from_text
        assert _parse_aggregate_from_text(
            "11. Aggregate Amount Beneficially Owned by Each Reporting Person "
            "1,020,598,157 12. Check if") == 1020598157

    def test_a_cover_without_row_eleven_yields_nothing(self):
        from app.scraper.sec_edgar import _parse_aggregate_from_text
        assert _parse_aggregate_from_text("no such row here") is None

    def test_every_member_reports_the_same_number(self):
        # The property that makes summing wrong. Wellington's four blocks each
        # carry the same aggregate; adding them would quadruple the bloc.
        from app.scraper.sec_edgar import _parse_13dg_xml
        d = _parse_13dg_xml(_fixture("13ga_wellington.xml"))
        aggregates = [p["aggregate"] for p in d["persons"][:3]]
        assert len(set(aggregates)) == 1


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
        from app.scraper import sec_edgar
        atom = self._atom(("0000102909-26-000394", "2026-03-26"))
        pages = {self.ZERO_XML: _fixture("13ga_vanguard_exit.xml")}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("GCI Liberty Inc", "0002057463")
        assert res == [], "a filer reporting nothing is not an owner of 0.0%"

    def test_an_older_holding_is_closed_with_the_exit_date(self):
        """The timeline: the position existed and then ended. Dropping the pair
        outright would lose that it was ever held."""
        from unittest.mock import patch
        from app.scraper import sec_edgar
        atom = self._atom(("0000102909-26-000394", "2026-03-26"),   # the exit
                          ("0000102909-26-000024", "2026-01-07"))   # 10.84%
        pages = {self.ZERO_XML: _fixture("13ga_vanguard_exit.xml"),
                 self.HELD_XML: _fixture("13ga_vanguard_held.xml")}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("GCI Liberty Inc", "0002057463")
        assert len(res) == 1
        assert res[0]["stake_percent"] == 10.84
        assert res[0]["until"] == "2026-03-26"

    def test_the_exit_is_scoped_to_the_cik_that_filed_it(self):
        """The realignment moved holdings from the parent to a subsidiary that
        files separately. The parent's zero must not suppress the subsidiary's
        real position — losing that would be worse than the 0% row was."""
        from unittest.mock import patch
        from app.scraper import sec_edgar
        apple_zero = ("https://www.sec.gov/Archives/edgar/data/320193/"
                      "000010290926000394/primary_doc.xml")
        atom = self._atom(("0000102909-26-000394", "2026-03-26"),      # parent, 0%
                          ("0002100119-26-000139", "2026-04-29"))      # subsidiary, 7.48%
        # The exit fixture names GCI Liberty, so serve it where the issuer
        # matches: this test is about CIK scoping, not issuer verification.
        pages = {apple_zero: _fixture("13ga_vanguard_exit.xml"),
                 ("https://www.sec.gov/Archives/edgar/data/320193/"
                  "000210011926000139/primary_doc.xml"): _fixture("13g_vanguard.xml")}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Apple Inc", "0000320193")
        assert [(r["investor_name"], r["stake_percent"]) for r in res] == \
               [("Vanguard Capital Management", 7.48)]

    def test_a_live_holding_carries_no_until(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        atom = self._atom(("0000102909-26-000024", "2026-01-07"))
        pages = {self.HELD_XML: _fixture("13ga_vanguard_held.xml")}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("GCI Liberty Inc", "0002057463")
        assert res[0]["until"] is None

    def test_a_bloc_member_with_no_individual_stake_is_not_an_exit(self):
        """BRC can dispose of nothing alone — null stake beside a real 52.3%
        bloc. Reading that as "holds nothing" would delete the voting group the
        whole 13D model is built on."""
        from app.scraper.sec_edgar import _split_stake
        pct, voting = _split_stake({"sole_dispositive": 0, "shared_voting": 1_020_598_157},
                                   None, 52.3)
        assert pct is None and voting == 52.3


class TestANegligibleHoldingIsNotZero:
    """Six Apple directors hold 1,139 shares each — 0.0000076% of ~15bn. Stored
    as 0.0 that reads as "owns nothing", which is both false and
    indistinguishable from a filer who has actually exited."""

    def test_a_real_but_tiny_holding_has_no_percentage(self):
        from app.scraper.sec_edgar import _pct_of
        assert _pct_of(1139, 14_935_826_000) is None
        assert _pct_of(250, 734_000_000) is None

    def test_a_holding_above_the_floor_keeps_its_number(self):
        from app.scraper.sec_edgar import _pct_of
        assert _pct_of(159121937, 1965328900) == 8.0965
        assert _pct_of(1, 1_000_000) == 0.0001      # exactly at four decimals

    def test_holding_nothing_really_is_zero(self):
        """The exit rule reads a zero as "has left"; a genuine zero must stay 0.0
        or `not pct and not voting` stops firing and exits go unrecorded."""
        from app.scraper.sec_edgar import _pct_of
        assert _pct_of(0, 1_000_000) == 0.0

    def test_no_denominator_no_percentage(self):
        from app.scraper.sec_edgar import _pct_of
        assert _pct_of(1139, None) is None
        assert _pct_of(None, 1_000_000) is None

    def test_every_stake_computation_goes_through_it(self):
        """Three sites divided shares by shares-outstanding, each rounding to 4
        decimals on its own — the sibling-path shape this codebase keeps paying
        for. They share the helper now, so the floor rule cannot drift apart."""
        import inspect, re
        from app.scraper import sec_edgar
        src = inspect.getsource(sec_edgar)
        helper = inspect.getsource(sec_edgar._pct_of)
        elsewhere = src.replace(helper, "")
        assert not re.search(r"round\([^)]*/[^)]*\*\s*100,\s*4\)", elsewhere), \
            "a stake is rounded outside _pct_of — the floor rule will drift"
        assert elsewhere.count("_pct_of(") == 3      # the three call sites
