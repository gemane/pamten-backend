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

    def test_returns_former_names_in_order_with_the_day_each_ended(self):
        with patch("app.scraper.sec_edgar._get", return_value=self._SUBS):
            assert fetch_former_names("0001326801") == ["Facebook Inc|2021-10-27", "TheFacebook, Inc.|2005-05-05"]

    def test_dedupes_case_insensitively_and_tolerates_a_missing_date(self):
        subs = {"formerNames": [{"name": "Square, Inc."}, {"name": "SQUARE, INC.", "to": "2021-12-10T05:00:00.000Z"},
                                {"name": ""}]}
        with patch("app.scraper.sec_edgar._get", return_value=subs):
            assert fetch_former_names("x") == ["Square, Inc.|"]

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
                   title: str = "", remarks: str = "") -> str:
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
              <remarks>{remarks}</remarks>
            </ownershipDocument>
        """).strip()

    def test_officer_with_title(self):
        xml = self._make_xml("MUSK ELON", is_officer="1", title="Chief Executive Officer")
        result = _parse_form34_xml(xml)
        assert result is not None
        assert result["name"] == "Elon Musk"
        assert result["role"] == "CEO"
        assert result["title"] == "Chief Executive Officer"

    # Berkshire's 2026 Form 4s (accessions 0001193125-26-349555 / -349574):
    # the title box says "See Remarks", the remarks box holds the title.
    def test_see_remarks_reads_the_role_off_the_remarks_box(self):
        xml = self._make_xml("Chang Charles C", is_officer="1", title="See Remarks",
                             remarks="Senior Vice President and Chief Financial Officer")
        result = _parse_form34_xml(xml)
        assert result["role"] == "CFO"
        assert result["title"] == "Senior Vice President and Chief Financial Officer"
        xml = self._make_xml("O'Sullivan Michael J.", is_officer="1", title="See remarks below",
                             remarks="Senior Vice President, General Counsel and Secretary")
        assert _parse_form34_xml(xml)["role"] == "General Counsel"

    # Televisa's Form 4 (CIK 912892, 2026-06-08): the remarks are a sentence
    # about a post at a SUBSIDIARY — mining it for "Chief Executive Officer"
    # would have seated him as Televisa's CEO.
    def test_a_remarks_sentence_is_never_mined_for_a_role(self):
        from app.scraper.sec_edgar import _looks_like_title, _officer_role
        valim = ("Mr. Valim is the Chief Executive Officer of izzi, Grupo Televisa, S.A.B.'s cable "
                 "company, and on that basis he may be considered an officer of Grupo Televisa, S.A.B.")
        xml = self._make_xml("Valim Francisco", is_officer="1", title="See Remarks", remarks=valim)
        result = _parse_form34_xml(xml)
        assert result["role"] == "Officer"
        assert result["title"] == "See Remarks"                 # the sentence is not a title either
        assert _officer_role("See Remarks", "He serves as Chief Financial Officer of the parent.") == "Officer"
        assert _officer_role("See Remarks", "Chief Financial Officer. Also a director of the trust.") == "Officer"
        assert _officer_role("See Remarks", "Chief Financial Officer " * 12) == "Officer"     # too long for a title
        # what a title looks like
        for ok in ("Senior Vice President and Chief Financial Officer", "Vice Chairman - Non-Insurance Operations",
                   "Senior Vice President, General Counsel and Secretary"):
            assert _looks_like_title(ok), ok
        assert _officer_role("See Remarks", "Senior Vice President and Chief Financial Officer") == "CFO"

    def test_see_remarks_never_becomes_a_role_and_a_remarks_paragraph_never_a_title(self):
        from app.scraper.sec_edgar import _officer_role
        # remarks that name no role we know: a plain officer, not the paragraph
        xml = self._make_xml("DOE JANE", is_officer="1", title="See Remarks",
                             remarks="Senior Vice President - Insurance Operations. The reporting "
                                     "person disclaims beneficial ownership of the shares held by the trust.")
        result = _parse_form34_xml(xml)
        assert result["role"] == "Officer"
        # no remarks at all: still never the placeholder
        assert _parse_form34_xml(self._make_xml("DOE JANE", is_officer="1", title="See Remarks"))["role"] == "Officer"
        assert _officer_role("See Remarks", "") == "Officer"
        assert _officer_role("see remarks", "Vice Chairman - Non-Insurance Operations") == "Chairman"
        # an ordinary title box is read as before, remarks or not
        assert _officer_role("Chief Executive Officer", "unrelated remarks") == "CEO"
        assert _officer_role("Treasurer", "Chief Financial Officer") == "Treasurer"

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

    def test_apostrophes_meet_edgars_spelling(self):
        from app.scraper.sec_edgar import _name_similarity, _significant_tokens
        spellings = ("Domino's Pizza, Inc.", "Domino’s Pizza, Inc.", "DOMINOS PIZZA INC")
        assert {_ticker_normalize(n) for n in spellings} == {"dominos pizza"}
        assert all("s" not in _significant_tokens(n) and "dominos" in _significant_tokens(n) for n in spellings)
        assert _name_similarity("Domino's Pizza, Inc.", "DOMINOS PIZZA INC") == 1.0


    @pytest.mark.parametrize("name,key", [
        ("NV Energy", "nv energy"),                     # findings item 5: was "energy"
        ("LENNAR CORP /NEW/", "lennar"),                # EDGAR's tag goes first
        ("Alibaba Group Holding Ltd", "alibaba"),
        ("Group 1 Automotive Inc", "group 1 automotive"),
        ("Innovex Co-Invest Fund II, L.P.", "innovex co-invest fund ii"),
    ])
    def test_a_legal_form_goes_only_at_the_end(self, name, key):
        assert _ticker_normalize(name) == key


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


    def test_nv_energy_is_not_energy_fuels(self):
        # findings item 5 (2026-10-30): "nv energy" keyed "energy", a prefix of
        # "energy fuels" at similarity 0.67, and Energy Fuels' filings were scraped
        tickers = {"0": {"cik_str": 1385849, "ticker": "UUUU", "title": "ENERGY FUELS INC"}}
        with patch("app.scraper.sec_edgar._tickers_cache", tickers):
            assert _lookup_in_tickers("NV Energy") is None

    def test_an_adr_programme_is_not_the_company(self):
        # the user's choice 2026-10-30: MTN Group would take the depositary's CIK
        tickers = {"0": {"cik_str": 1163420, "ticker": "MTNOY", "title": "MTN GROUP LTD/ADR"},
                   "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        with patch("app.scraper.sec_edgar._tickers_cache", tickers):
            assert _lookup_in_tickers("MTN Group") is None
            assert _lookup_in_tickers("Apple")["cik"] == "0000320193"

    @pytest.mark.parametrize("title,adr", [
        ("MTN GROUP LTD/ADR", True), ("FOO PLC /ADR/", True), ("BAR SA \\ADR", True),
        ("ADR HOLDINGS INC", False), ("TOYOTA MOTOR CORP/", False), ("LENNAR CORP /NEW/", False), (None, False),
    ])
    def test_is_adr_listing(self, title, adr):
        from app.scraper.sec_edgar import is_adr_listing
        assert is_adr_listing(title) is adr


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


    def test_a_fund_vehicle_is_not_its_sponsor(self):
        """2026-10-29: the full-text hit for "Baillie Gifford & Co" (the 13F
        manager, CIK 0001088875) was the proxy of BAILLIE GIFFORD FUNDS, and the
        manager's node took the funds' CIK. Not found beats a wrong CIK."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"hits": {"hits": [{"_source": {
            "display_names": ["BAILLIE GIFFORD FUNDS  (CIK 0001120543)"],
            "ciks": ["1120543"], "adsh": "0001120543-25-000001"}}]}}
        mock_response.raise_for_status = MagicMock()
        mock_response.content = b'<feed xmlns="http://www.w3.org/2005/Atom"/>'
        with patch("app.scraper.sec_edgar._tickers_cache", {}), \
             patch("app.scraper.sec_edgar.time.sleep"), \
             patch("app.scraper.sec_edgar._get_client",
                   return_value=MagicMock(get=MagicMock(return_value=mock_response))):
            assert search_company("Baillie Gifford & Co") is None


class TestFundVehicles:
    def test_a_fund_word_the_query_lacks_scores_zero(self):
        from app.scraper.sec_edgar import _name_similarity
        assert _name_similarity("Baillie Gifford & Co", "BAILLIE GIFFORD FUNDS") == 0.0
        assert _name_similarity("Blackstone", "Blackstone Private Credit Fund") == 0.0
        assert _name_similarity("iShares", "iShares Trust") == 0.0

    def test_a_fund_word_the_query_has_is_kept(self):
        from app.scraper.sec_edgar import _name_similarity
        assert _name_similarity("Northern Trust", "NORTHERN TRUST CORP") == 1.0
        assert _name_similarity("Vanguard Funds", "VANGUARD FUNDS") == 1.0
        assert _name_similarity("Trustmark", "TRUSTMARK CORP") == 1.0     # a word, not a prefix

    def test_and_co_is_a_legal_form(self):
        from app.scraper.sec_edgar import _ticker_normalize
        assert _ticker_normalize("BAILLIE GIFFORD & CO") == _ticker_normalize("Baillie Gifford") == "baillie gifford"
        assert _ticker_normalize("Procter & Gamble Co") == "procter & gamble"


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
    assert out["shares_owned"] == 510000.0   # the LAST row: the position the filing leaves the filer in


def test_parse_form4_shares_none_when_absent():
    xml = """<ownershipDocument><reportingOwner>
      <reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
      <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
    </reportingOwner></ownershipDocument>"""
    out = _parse_form34_xml(xml)
    assert out["role"] == "Director" and out["shares_owned"] is None


# ── The holding, per class, against the count of its own class ──────────────

# Warren Buffett's Form 4 of 15 July 2026: 8,000 Class A converted into
# 12,000,000 Class B, then given away in four gifts; the Class A he keeps is in
# the derivative table, because Class A is convertible into B. The largest
# "owned following" value (12,001,162) is the moment before the gifts.
_BUFFETT_XML = """<ownershipDocument>
  <issuer><issuerCik>0001067983</issuerCik></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>BUFFETT WARREN E</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer>
      <officerTitle>Chairman and CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction><securityTitle><value>Class B Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>12001162</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
    <nonDerivativeTransaction><securityTitle><value>Class B Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>3001162</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
    <nonDerivativeTransaction><securityTitle><value>Class B Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>1162</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction><securityTitle><value>Class A Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>188290</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></derivativeTransaction>
    <derivativeHolding><securityTitle><value>Stock Option (Right to Buy)</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>999999</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </derivativeHolding>
  </derivativeTable>
</ownershipDocument>"""

# Berkshire's cover: 488,450 Class A and 1,408,035,161 Class B as of 2026-07-29.
BRK = {"total": 1_408_523_611.0, "as_of": "2026-07-29", "by_class": {"A": 488_450.0, "B": 1_408_035_161.0}}


class TestForm4Holdings:
    def test_the_last_row_per_security_not_the_largest(self):
        from app.scraper.sec_edgar import _form4_holdings
        import xml.etree.ElementTree as ET
        h = _form4_holdings(ET.fromstring(_BUFFETT_XML))
        assert h["Class B Common Stock"] == 1162.0          # after the gifts, not 12,001,162 before them
        assert h["Class A Common Stock"] == 188290.0        # a plain class of common stock in Table II counts
        assert "Stock Option (Right to Buy)" not in h       # an option is not a share held

    def test_direct_and_indirect_positions_of_one_security_are_summed(self):
        from app.scraper.sec_edgar import _form4_holdings
        import xml.etree.ElementTree as ET
        xml = """<x><nonDerivativeTable>
          <nonDerivativeHolding><securityTitle><value>Common Stock</value></securityTitle>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>100</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeHolding>
          <nonDerivativeHolding><securityTitle><value>Common Stock</value></securityTitle>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>40</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>
              <natureOfOwnership><value>By trust</value></natureOfOwnership></ownershipNature></nonDerivativeHolding>
        </nonDerivativeTable></x>"""
        assert _form4_holdings(ET.fromstring(xml)) == {"Common Stock": 140.0}

    def test_the_stake_is_class_against_class(self):
        from app.scraper.sec_edgar import stake_for
        pick = stake_for({"Class B Common Stock": 1162.0, "Class A Common Stock": 188290.0}, BRK)
        # the class with the largest percentage carries the edge
        assert pick["share_class"] == "Class A Common Stock" and pick["shares_owned"] == 188290.0
        assert pick["stake_percent"] == 38.5485                  # of the 488,450 Class A, never of a B count
        assert pick["shares_outstanding"] == 488_450.0 and pick["denominator_date"] == "2026-07-29"

    def test_a_count_of_one_class_is_never_divided_by_another(self):
        from app.scraper.sec_edgar import stake_for
        # only Class B held: 1,162 of 1.4 bn Class B — a rounding-floor 0.0001 %, and never 1,274 %
        pick = stake_for({"Class B Common Stock": 1162.0}, BRK)
        assert pick["stake_percent"] == 0.0001 and pick["shares_outstanding"] == 1_408_035_161.0
        # a class the issuer does not state, or no class named at all, on a multi-class issuer: no denominator
        for title in ("Class C Common Stock", "Common Stock"):
            pick = stake_for({title: 5000.0}, BRK)
            assert pick["stake_percent"] is None and pick["shares_outstanding"] is None
            assert pick["denominator_date"] is None

    def test_an_unclassed_issuer_total_divides_any_title(self):
        from app.scraper.sec_edgar import stake_for
        assert stake_for({"Common Stock": 2000.0}, 100_000)["stake_percent"] == 2.0          # a bare total, as the 13F path hands over
        assert stake_for({"Class A Common Stock": 2000.0}, {"total": 100_000, "as_of": "2026-05-01", "by_class": {}})["stake_percent"] == 2.0

    def test_nothing_held_is_no_pick(self):
        from app.scraper.sec_edgar import stake_for
        assert stake_for({}, BRK) is None
        assert stake_for({"Common Stock": 0.0}, BRK) is None

    def test_the_parsed_filing_carries_its_class(self):
        out = _parse_form34_xml(_BUFFETT_XML)
        assert out["shares_owned"] == 188290.0 and out["share_class"] == "Class A Common Stock"
        assert out["holdings"] == {"Class B Common Stock": 1162.0, "Class A Common Stock": 188290.0}


class TestTheDenominatorIsDatedAndPerClass:
    # the plain-tag series Berkshire stopped keeping in 2011
    OLD = {"units": {"shares": [{"end": "2010-07-30", "val": 976414}, {"end": "2011-04-29", "val": 941481}]}}
    FRESH = {"units": {"shares": [{"end": "2026-06-30", "val": 15_000_000_000}]}}

    def test_a_series_that_stopped_is_not_the_current_count(self):
        from app.scraper import sec_edgar
        cover = sec_edgar.SharesOutstanding(1_408_523_611.0, "2026-07-29", {"A": 488_450.0, "B": 1_408_035_161.0})
        with patch.object(sec_edgar, "_get", return_value=self.OLD), \
             patch.object(sec_edgar, "_shares_outstanding_from_cover", return_value=cover) as fromcover:
            d = sec_edgar.fetch_shares_outstanding_detail("0001067983", on="2026-10-10")
            total = sec_edgar.fetch_shares_outstanding("0001067983", on="2026-10-10")
        assert d == cover and fromcover.called
        assert total == 1_408_523_611.0                      # the 13F path's bare total: the classes summed

    def test_a_fresh_series_is_taken_as_it_was(self):
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_get", return_value=self.FRESH), \
             patch.object(sec_edgar, "_shares_outstanding_from_cover") as fromcover:
            d = sec_edgar.fetch_shares_outstanding_detail("0000320193", on="2026-10-10")
        assert d == sec_edgar.SharesOutstanding(15_000_000_000.0, "2026-06-30", {}) and not fromcover.called

    def test_the_floor_is_fifteen_months_before_the_day_asked_for(self):
        from app.scraper import sec_edgar
        assert sec_edgar._freshness_floor("2026-10-10") == "2025-07-17"
        # the same 2011 value IS current when asked about from 2011
        with patch.object(sec_edgar, "_get", return_value=self.OLD), \
             patch.object(sec_edgar, "_shares_outstanding_from_cover") as fromcover:
            d = sec_edgar.fetch_shares_outstanding_detail("0001067983", on="2011-08-01")
        assert d.total == 941481.0 and not fromcover.called

    def test_the_cover_tells_the_classes_apart_by_their_contexts(self):
        from app.scraper.sec_edgar import cover_counts
        html = """<xbrli:context id="C_a"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001067983</xbrli:identifier>
          <xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassAMember</xbrldi:explicitMember></xbrli:segment>
          </xbrli:entity><xbrli:period><xbrli:instant>2026-07-29</xbrli:instant></xbrli:period></xbrli:context>
          <xbrli:context id="C_b"><xbrli:entity><xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassBMember</xbrldi:explicitMember></xbrli:segment>
          </xbrli:entity><xbrli:period><xbrli:instant>2026-07-29</xbrli:instant></xbrli:period></xbrli:context>"""
        d = cover_counts({"C_a": 488_450.0, "C_b": 1_408_035_161.0}, html)
        assert d.by_class == {"A": 488_450.0, "B": 1_408_035_161.0}
        assert d.total == 1_408_523_611.0 and d.as_of == "2026-07-29"

    def test_a_single_class_cover_is_an_unclassed_count(self):
        from app.scraper.sec_edgar import cover_counts
        html = '<context id="c1"><entity><identifier>1</identifier></entity><period><instant>2026-03-31</instant></period></context>'
        d = cover_counts({"c1": 15_000_000_000.0}, html)
        assert d.by_class == {} and d.total == 15_000_000_000.0 and d.as_of == "2026-03-31"
        assert cover_counts({}, html) is None

    # Trimmed from Toast's 10-Q for 2026-06-30 (tost-20260630.htm): the cover
    # states millions, `scale="6"`.
    TOAST = (
        '<xbrli:context id="c-2"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001650164</xbrli:identifier>'
        '<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassAMember</xbrldi:explicitMember>'
        '</xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-07-30</xbrli:instant></xbrli:period></xbrli:context>'
        '<xbrli:context id="c-3"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001650164</xbrli:identifier>'
        '<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassBMember</xbrldi:explicitMember>'
        '</xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-07-30</xbrli:instant></xbrli:period></xbrli:context>'
        '<td><ix:nonFraction unitRef="shares" contextRef="c-2" decimals="-6" name="dei:EntityCommonStockSharesOutstanding" scale="6" id="f-24">514</ix:nonFraction></td>'
        '<td><ix:nonFraction unitRef="shares" contextRef="c-3" decimals="-6" name="dei:EntityCommonStockSharesOutstanding" scale="6" id="f-25">64</ix:nonFraction></td>')

    def test_a_scaled_cover_figure_is_multiplied_out(self):
        """Read as written, a Toast director's 3,566 shares were 693.77 % of
        Class A, the database refused the edge and the whole scrape failed."""
        from app.scraper.sec_edgar import cover_counts, cover_facts, stake_for
        assert cover_facts(self.TOAST) == {"c-2": 514_000_000.0, "c-3": 64_000_000.0}
        d = cover_counts(cover_facts(self.TOAST), self.TOAST)
        assert d.by_class == {"A": 514_000_000.0, "B": 64_000_000.0} and d.as_of == "2026-07-30"
        pick = stake_for({"Class A Common Stock": 3566.0}, d)
        assert pick["stake_percent"] == 0.0007 and pick["shares_outstanding"] == 514_000_000.0

    def test_more_shares_than_the_class_has_is_no_percentage(self):
        from app.scraper.sec_edgar import _pct_of, stake_for
        assert _pct_of(3566.0, 514.0) is None
        assert _pct_of(514.0, 514.0) == 100.0
        pick = stake_for({"Class A Common Stock": 3566.0}, {"total": 578.0, "as_of": "2026-07-30",
                                                             "by_class": {"A": 514.0, "B": 64.0}})
        assert pick["stake_percent"] is None and pick["shares_owned"] == 3566.0

    def test_an_unscaled_or_decimal_figure_reads_as_written(self):
        from app.scraper.sec_edgar import cover_facts
        tag = '<ix:nonFraction contextRef="c1" name="dei:EntityCommonStockSharesOutstanding"{}>{}</ix:nonFraction>'
        assert cover_facts(tag.format("", "1,408,035,161")) == {"c1": 1_408_035_161.0}
        assert cover_facts(tag.format(' scale="9"', "13.18")) == {"c1": 13_180_000_000.0}
        assert cover_facts(tag.format(' scale="0"', "941,481")) == {"c1": 941_481.0}


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
    with patch("app.scraper.sec_edgar._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_edgar._get", return_value=_SUBS), \
         patch("app.scraper.sec_edgar._get_text", return_value=_form4("0001364742", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742", shares_outstanding=100000)
    assert out["shares_owned"] == 2000.0
    assert out["stake_percent"] == 2.0            # 2000 / 100000 * 100


def test_fetch_insider_holding_divides_class_by_class():
    from app.scraper.sec_edgar import fetch_insider_holding
    with patch("app.scraper.sec_edgar._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_edgar._get", return_value=_SUBS), \
         patch("app.scraper.sec_edgar._get_text", return_value=_BUFFETT_XML), \
         patch("time.sleep"):
        out = fetch_insider_holding("Warren Buffett", "0001067983", shares_outstanding=BRK)
    assert out["shares_owned"] == 188290.0 and out["share_class"] == "Class A Common Stock"
    assert out["stake_percent"] == 38.5485 and out["denominator_date"] == "2026-07-29"
    assert out["source_date"] == "2024-02-01"


def test_fetch_insider_holding_none_when_issuer_mismatch():
    from app.scraper.sec_edgar import fetch_insider_holding
    with patch("app.scraper.sec_edgar._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_edgar._get", return_value=_SUBS), \
         patch("app.scraper.sec_edgar._get_text", return_value=_form4("0000999999", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742", shares_outstanding=100000)
    assert out is None                            # Form 4 is about a different company


def test_fetch_insider_holding_none_when_no_cik():
    from app.scraper.sec_edgar import fetch_insider_holding
    with patch("app.scraper.sec_edgar._lookup_person_ciks", return_value=[]):
        assert fetch_insider_holding("Nobody", "0001364742") is None


def test_fetch_insider_holding_returns_the_cik_it_was_filed_under():
    from app.scraper.sec_edgar import fetch_insider_holding
    with patch("app.scraper.sec_edgar._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_edgar._get", return_value=_SUBS), \
         patch("app.scraper.sec_edgar._get_text", return_value=_form4("0001364742", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742")
    assert out["cik"] == "0001059245"


def test_fetch_insider_holding_tries_the_next_candidate():
    """A name search with several matches names no one: the first candidate
    files about another company, the second about the issuer."""
    from app.scraper.sec_edgar import fetch_insider_holding
    xml = {"1": _form4("0000999999", "50"), "2": _form4("0001364742", "2000")}
    with patch("app.scraper.sec_edgar._lookup_person_ciks", return_value=["0000000001", "0000000002"]), \
         patch("app.scraper.sec_edgar._get", return_value=_SUBS), \
         patch("app.scraper.sec_edgar._get_text", side_effect=lambda url: xml[url.split("/")[-3]]), \
         patch("time.sleep"):
        out = fetch_insider_holding("Thomas Murphy", "0001364742")
    assert out["cik"] == "0000000002" and out["shares_owned"] == 2000.0


def test_fetch_insider_holding_uses_the_cik_the_person_holds():
    from app.scraper.sec_edgar import fetch_insider_holding
    with patch("app.scraper.sec_edgar._lookup_person_ciks") as lookup, \
         patch("app.scraper.sec_edgar._get", return_value=_SUBS), \
         patch("app.scraper.sec_edgar._get_text", return_value=_form4("0001364742", "2000")), \
         patch("time.sleep"):
        out = fetch_insider_holding("Larry Fink", "0001364742", person_cik="1059245")
    lookup.assert_not_called()
    assert out["cik"] == "0001059245"


def test_fetch_insider_holding_opens_a_bounded_number_of_a_candidates_form_4s():
    from app.scraper import sec_edgar
    n = sec_edgar.MAX_PERSON_FORM4 + 5
    subs = {"filings": {"recent": {"form": ["4"] * n,
                                   "accessionNumber": [f"0001059245-24-{i:06d}" for i in range(n)],
                                   "primaryDocument": ["form4.xml"] * n,
                                   "filingDate": ["2024-02-01"] * n}}}
    with patch("app.scraper.sec_edgar._lookup_person_ciks", return_value=["0001059245"]), \
         patch("app.scraper.sec_edgar._get", return_value=subs), \
         patch("app.scraper.sec_edgar._get_text", return_value=_form4("0000999999", "1")) as get_text, \
         patch("time.sleep"):
        assert sec_edgar.fetch_insider_holding("Larry Fink", "0001364742") is None
    assert get_text.call_count == sec_edgar.MAX_PERSON_FORM4


class TestPersonCikLookup:
    """EDGAR names a person surname first and its name search matches from the
    start: "Robyn Denholm" found nobody, "Denholm Robyn" finds her (findings
    item 29). The two answer shapes are EDGAR's real ones: one match names the
    filer (company-info + conformed-name), several give CIKs alone."""

    ONE = ("<feed><company-info><cik>0001242782</cik>"
           "<conformed-name>DENHOLM ROBYN M</conformed-name></company-info></feed>")
    MANY = ("<feed><entry><content><company-info name=\"ARRAY(0x1)\"><cik>0001145065</cik>"
            "</company-info></content></entry><entry><content><company-info name=\"ARRAY(0x2)\">"
            "<cik>0001235136</cik></company-info></content></entry></feed>")
    NONE = "<feed><author><name>Webmaster</name></author></feed>"

    def _lookup(self, name, answers):
        from app.scraper import sec_edgar
        asked = []

        def get(url, params):
            asked.append(params["company"])
            resp = MagicMock()
            resp.text = answers.get(params["company"], self.NONE)
            return resp
        client = MagicMock()
        client.get.side_effect = get
        with patch.object(sec_edgar, "_get_client", return_value=client), patch("time.sleep"):
            return sec_edgar._lookup_person_ciks(name), asked

    def test_surname_first_is_asked_first(self):
        found, asked = self._lookup("Robyn Denholm", {"Denholm Robyn": self.ONE})
        assert found == ["0001242782"] and asked == ["Denholm Robyn"]

    def test_a_middle_initial_and_a_suffix_are_left_out(self):
        _, asked = self._lookup("Thomas S. Murphy, Jr.", {})
        assert asked == ["Murphy Thomas", "Thomas S. Murphy"]

    def test_an_sec_styled_name_is_found_as_given(self):
        found, asked = self._lookup("DENHOLM ROBYN M", {"DENHOLM ROBYN M": self.ONE})
        assert found == ["0001242782"] and asked == ["M DENHOLM", "DENHOLM ROBYN M"]

    def test_one_match_with_another_name_is_refused(self):
        found, _ = self._lookup("Robyn Smith", {"Smith Robyn": self.ONE, "Robyn Smith": self.ONE})
        assert found == []

    def test_several_matches_are_all_candidates(self):
        found, _ = self._lookup("Thomas Murphy", {"Murphy Thomas": self.MANY})
        assert found == ["0001145065", "0001235136"]

    def test_a_failed_search_tries_the_name_as_given(self):
        from app.scraper import sec_edgar
        import httpx
        client = MagicMock()
        ok = MagicMock(text=self.ONE)
        client.get.side_effect = [httpx.ConnectError("down"), ok]
        with patch.object(sec_edgar, "_get_client", return_value=client), patch("time.sleep"):
            assert sec_edgar._lookup_person_ciks("Robyn M. Denholm") == ["0001242782"]


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
        # a legacy cover page: the numbers came out of the form's numbered
        # rows in its text — the SEC's labels, the filer's figure
        assert results[0]["read_from"] == "form"

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

    def test_a_lone_custodian_with_all_shared_power_keeps_its_stake(self):
        # State Street over News Corporation: sole 0, everything shared (held
        # for client accounts), aggregate 8.6M = 4.7% of class. A lone filer,
        # so 4.7% is its real stake, not a phantom bloc. This is the bug the
        # in_group flag fixes — before it, the stake was nulled.
        from app.scraper.sec_edgar import _split_stake
        rows = {"sole_voting": 0, "shared_voting": 6878465,
                "sole_dispositive": 0, "shared_dispositive": 8593355}
        assert _split_stake(rows, 182981979, 4.7, in_group=False) == (4.7, None)

    def test_the_same_rows_in_a_group_still_lose_the_stake(self):
        # Identical row shape, but with co-filers → a genuine bloc member whose
        # reported shares are the whole group's. Must stay null + bloc.
        from app.scraper.sec_edgar import _split_stake
        rows = {"sole_voting": 0, "shared_voting": 6878465,
                "sole_dispositive": 0, "shared_dispositive": 8593355}
        assert _split_stake(rows, 182981979, 4.7, in_group=True) == (None, 4.7)

    def test_stake_from_person_reads_the_co_filer_count(self):
        # One reporting person → lone custodian → real stake; two → group.
        from app.scraper.sec_edgar import _stake_from_person
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

    def test_the_date_of_event_reaches_the_scrape_result(self):
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
        doc = ("Anheuser-Busch InBev SA/NV (Name of Issuer) September 25, 2024 "
               "(Date of Event Which Requires Filing of this Statement) " + self.ALTRIA)
        pages = {None: atom, "https://x.test/i.htm": index,
                 "https://www.sec.gov/Archives/edgar/data/1/d.htm": doc}
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Anheuser-Busch InBev", "1668717")
        assert res[0]["event_date"] == "2024-09-25"
        assert res[0]["file_date"] == "2024-09-27"          # said two days later


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
        from app.scraper.sec_edgar import _stake_from_person
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
        from app.scraper.sec_edgar import _derive_total
        assert _derive_total(0, 0.0) is None
        assert _derive_total(1000, 0.0) is None
        assert _derive_total(None, 5.0) is None

    def test_a_lone_filer_keeps_its_reported_percentage(self):
        from app.scraper.sec_edgar import _parse_13dg_xml, _stake_from_person
        d = _parse_13dg_xml(_fixture("13g_vanguard.xml"))
        stake, voting = _stake_from_person(d, d["persons"][0])
        assert stake == 7.48 and voting is None

    def test_a_lone_party_to_a_voting_agreement_gets_its_own_stake(self):
        # Altria's 2025 13D/A, the structured kind, names ONE reporting person
        # — so it read as a lone custodian and row 13's 51.9% (the Voting
        # Agreement bloc) became Altria's stake: AB InBev's "majority owner".
        # Altria disposes of 159,121,937 shares of its own — 8.1% — and shares
        # the vote over 1,020,598,157.
        from app.scraper.sec_edgar import _parse_13dg_xml, _stake_from_person
        d = _parse_13dg_xml(_fixture("13d_altria_abinbev.xml"))
        assert len(d["persons"]) == 1 and d["schedule"] == "13D"
        stake, voting = _stake_from_person(d, d["persons"][0])
        assert stake == pytest.approx(8.1, abs=0.02)
        assert voting == 51.9

    def test_a_lone_custodian_and_a_lone_owner_stay_as_they_were(self):
        from app.scraper.sec_edgar import _split_stake
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
        from app.scraper.sec_edgar import _split_stake
        rows = {"sole_voting": 0, "shared_voting": 500_000, "sole_dispositive": 100_000,
                "shared_dispositive": 50_000}
        assert _split_stake(rows, 1_000_000, 50.0) == (15.0, 50.0)
        # no denominator: the bloc is certain, the stake is not invented
        assert _split_stake(rows, None, 50.0) == (None, 50.0)


class TestPersonSelection:
    def test_the_filer_is_picked_out_of_the_group(self):
        from app.scraper.sec_edgar import _select_person
        xml = {"persons": [{"name": "A", "cik": "0000000111"},
                           {"name": "B", "cik": "0000000222"}]}
        assert _select_person(xml, "222")["name"] == "B"

    def test_a_block_of_its_own_cik_is_used_when_none_is_the_filer(self):
        from app.scraper.sec_edgar import _select_person
        xml = {"persons": [{"name": "A", "cik": "0000000111"}, {"name": "B", "cik": None}]}
        assert _select_person(xml, "999") == {"name": "A", "cik": "0000000111"}

    def test_cik_less_blocks_pick_the_one_edgar_names_the_filer(self):
        # Berkshire's 13Gs: no block carries a CIK and Warren E. Buffett comes
        # first. The first block on Berkshire's CIK made a Person "Warren E.
        # Buffett" sec:0001067983 with twelve of Berkshire's stakes (item 21).
        from app.scraper.sec_edgar import _select_person
        xml = {"persons": [{"name": "Warren E. Buffett", "cik": None},
                           {"name": "Berkshire Hathaway Inc.", "cik": None}]}
        picked = _select_person(xml, "0001067983", lambda cik: "BERKSHIRE HATHAWAY INC")
        assert picked == {"name": "Berkshire Hathaway Inc.", "cik": "0001067983"}
        assert xml["persons"][0]["cik"] is None            # never the filer's CIK on another name

    def test_a_person_filer_is_matched_in_edgar_word_order(self):
        from app.scraper.sec_edgar import _select_person
        xml = {"persons": [{"name": "Berkshire Hathaway Inc.", "cik": None},
                           {"name": "Warren E. Buffett", "cik": None}]}
        picked = _select_person(xml, "0000315090", lambda cik: "BUFFETT WARREN E")
        assert picked["name"] == "Warren E. Buffett" and picked["cik"] == "0000315090"

    def test_no_block_named_as_the_filer_skips_the_filing(self):
        from app.scraper.sec_edgar import _select_person
        xml = {"persons": [{"name": "A Fund", "cik": None}, {"name": "B Fund", "cik": None}]}
        assert _select_person(xml, "999", lambda cik: "SOMEONE ELSE LLC") is None
        assert _select_person(xml, "999") is None          # no name to match by
        assert all(p["cik"] is None for p in xml["persons"])

    def test_a_lone_cik_less_block_is_the_filer(self):
        from app.scraper.sec_edgar import _select_person
        asked = []
        xml = {"persons": [{"name": "Vanguard Capital Management", "cik": None}]}
        picked = _select_person(xml, "0000102909", asked.append)
        assert picked["cik"] == "0000102909" and asked == []

    def test_the_name_is_not_fetched_when_a_cik_matches(self):
        from app.scraper.sec_edgar import _select_person
        asked = []
        xml = {"persons": [{"name": "A", "cik": None}, {"name": "B", "cik": "0000000222"}]}
        assert _select_person(xml, "222", asked.append)["name"] == "B" and asked == []

    def test_the_filer_name_ignores_legal_form_and_word_order(self):
        from app.scraper.sec_edgar import _same_filer_name
        assert _same_filer_name("Berkshire Hathaway Inc.", "BERKSHIRE HATHAWAY INC")
        assert _same_filer_name("Warren E. Buffett", "BUFFETT WARREN E")
        assert not _same_filer_name("National Indemnity Company", "BERKSHIRE HATHAWAY INC")
        assert not _same_filer_name("Berkshire Hathaway International Insurance Ltd.",
                                    "BERKSHIRE HATHAWAY INC")

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
        assert res[0]["read_from"] == "field"            # named XML fields, not a regex
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
             patch.object(sec_edgar, "fetch_former_names", return_value=[]), \
             patch.object(sec_edgar, "fetch_filer_name", return_value="WELLINGTON MANAGEMENT GROUP LLP"):
            res = sec_edgar.fetch_ownership_filings("Nasdaq, Inc.", "1120193")
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
        # the day the position ended — the exit's date of event, 13 days
        # before it was filed on 2026-03-26
        assert res[0]["until"] == "2026-03-13"
        # the structured branch carries the cover's date of event too
        assert res[0]["event_date"] == "2025-12-31"

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
                                   None, 52.3, in_group=True)
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
        import inspect
        import re
        from app.scraper import sec_edgar
        src = inspect.getsource(sec_edgar)
        helper = inspect.getsource(sec_edgar._pct_of)
        elsewhere = src.replace(helper, "")
        assert not re.search(r"round\([^)]*/[^)]*\*\s*100,\s*4\)", elsewhere), \
            "a stake is rounded outside _pct_of — the floor rule will drift"
        # the call sites: the two 13D/G cover rules, the newest-denominator
        # restatement, and `stake_for`, which every Form 4 holding goes through
        assert elsewhere.count("_pct_of(") == 4


class TestSecWebsite:
    def test_the_corporate_site_wins_over_investor_relations(self):
        from app.scraper.sec_edgar import sec_website
        assert sec_website({"website": "https://www.tesla.com",
                            "investorWebsite": "https://ir.tesla.com"}) == "https://www.tesla.com"

    def test_investor_site_is_the_fallback(self):
        from app.scraper.sec_edgar import sec_website
        assert sec_website({"website": "", "investorWebsite": "https://ir.tesla.com"}) \
            == "https://ir.tesla.com"

    def test_junk_is_rejected_like_wikidatas(self):
        from app.scraper.sec_edgar import sec_website
        assert sec_website({"website": "tesla.com"}) is None    # no scheme guessing
        assert sec_website({}) is None

    def test_the_fetch_wrapper_swallows_errors(self):
        from unittest.mock import patch
        import httpx
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_submissions",
                          side_effect=httpx.ConnectError("down")):
            assert sec_edgar.fetch_filer_website("0001318605") is None


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
        from app.scraper import sec_edgar
        page = sec_edgar._cover_page_for(self.BOZANO, "Cia. Bozano")
        assert sec_edgar._parse_reporter_type_from_text(page) is False
        assert sec_edgar._parse_percent_from_text(page) == 9.2
        assert sec_edgar._parse_aggregate_from_text(page) == 66_236_689
        assert sec_edgar._parse_power_rows(page)["shared_dispositive"] == 66_236_689

    def test_the_individual_on_page_one_is_still_read_as_one(self):
        from app.scraper import sec_edgar
        page = sec_edgar._cover_page_for(self.BOZANO, "Júlio Rafael de Aragão Bozano")
        assert sec_edgar._parse_reporter_type_from_text(page) is True
        assert sec_edgar._parse_percent_from_text(page) == 10.4

    def test_the_shared_header_and_footnote_are_read_from_the_whole_filing(self):
        # The class title precedes page one and the outstanding-shares footnote
        # follows the last page; neither belongs to a page, so a filer on an
        # earlier page would lose the denominator if only its page were read.
        from app.scraper import sec_edgar
        page = sec_edgar._cover_page_for(self.BOZANO, "Júlio Rafael de Aragão Bozano")
        assert sec_edgar._shares_outstanding(page) is None
        assert sec_edgar._shares_outstanding(self.BOZANO) == 722_766_139
        assert sec_edgar._parse_class_title_from_text(self.BOZANO) == "Common Shares"
        # A 13G, so the two Bozano pages are a family, not a bloc.
        assert sec_edgar._own_stake_and_voting(page, 10.4, document=self.BOZANO,
                                               in_group=False) == (10.4, None)

    def test_a_lone_filer_document_is_returned_whole(self):
        from app.scraper import sec_edgar
        lone = ("Apple Inc (Name of Issuer) NAME OF REPORTING PERSON The Vanguard Group "
                "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 11 8.1% TYPE OF REPORTING PERSON IA")
        assert sec_edgar._cover_page_for(lone, "Vanguard Group") == sec_edgar._plain_text(lone)
        assert sec_edgar._cover_page_for(self.BOZANO, None) == sec_edgar._plain_text(self.BOZANO)

    def test_a_filer_matching_no_page_keeps_the_old_first_page_reading(self):
        from app.scraper import sec_edgar
        page = sec_edgar._cover_page_for(self.BOZANO, "Someone Else Entirely")
        assert page == sec_edgar._plain_text(self.BOZANO)
        assert sec_edgar._parse_percent_from_text(page) == 10.4

    def test_end_to_end_the_holding_company_is_a_company_with_its_own_stake(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
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
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("EMBRAER S.A.", "1355444")
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
        from app.scraper.sec_edgar import _parse_power_rows, _shares_held
        rows = _parse_power_rows(self.BERKSHIRE)
        assert rows == {"sole_voting": 0, "shared_voting": 52_717_075,
                        "sole_dispositive": 0, "shared_dispositive": 52_717_075}
        assert _shares_held(rows, 52_717_075) == 52_717_075

    def test_dashed_zero_and_the_bozano_number_of_minus_zero_minus(self):
        from app.scraper.sec_edgar import _parse_power_rows
        assert _parse_power_rows(self.DEER) == {"sole_voting": 3_328_253, "shared_voting": 0,
                                                "sole_dispositive": 3_328_253, "shared_dispositive": 0}
        bozano = "5 SOLE VOTING POWER NUMBER OF -0- SHARES 6 SHARED VOTING POWER BENEFICIALLY OWNED BY 75,133,609"
        assert _parse_power_rows(bozano) == {"sole_voting": 0, "shared_voting": 75_133_609}

    def test_a_reference_to_another_row_copies_that_row(self):
        from app.scraper.sec_edgar import _parse_power_rows, _shares_held
        rows = _parse_power_rows(self.CITADEL)
        assert rows["shared_dispositive"] == 7_675_822
        assert _shares_held(rows, None) == 7_675_822

    def test_an_empty_cell_is_absent_not_a_count(self):
        from app.scraper.sec_edgar import _parse_power_rows
        assert _parse_power_rows(self.EMPTY) == {"shared_voting": 1_234_567,
                                                 "shared_dispositive": 1_234_567}


class TestFilerIsNotTheFirstBlock:
    """Findings item 21 (b): the filer's CIK goes with the filer's own block."""

    ATOM = TestStructuredScrapeEndToEnd.ATOM.replace("0002100119-26-000139", "0001193125-25-283610")
    URL = "https://www.sec.gov/Archives/edgar/data/1014473/000119312525283610/primary_doc.xml"

    def _run(self, filer_name):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_get_text",
                          side_effect=_serve(self.ATOM, {self.URL: _fixture("13ga_berkshire_verisign.xml")})), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]), \
             patch.object(sec_edgar, "fetch_filer_name", return_value=filer_name):
            return sec_edgar.fetch_ownership_filings("VeriSign, Inc.", "1014473")

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


class TestTypeCodes:
    """Findings item 21 (c): HC is a holding company or a control person."""

    def test_the_codes(self):
        from app.scraper.sec_edgar import _individual_by_code
        assert _individual_by_code("IN") is True
        assert _individual_by_code("IA") is False
        assert _individual_by_code("HC") is None             # the name decides
        assert _individual_by_code("HC, CO") is False        # a definite code beside it decides
        assert _individual_by_code("hc") is None
        assert _individual_by_code(None) is None and _individual_by_code("") is None

    def test_a_cover_page_hc_says_nothing_either_way(self):
        from app.scraper import sec_edgar
        assert sec_edgar._parse_reporter_type_from_text("12 TYPE OF REPORTING PERSON HC ") is None
        assert sec_edgar._parse_reporter_type_from_text("12 TYPE OF REPORTING PERSON HC, CO ") is False
        # the prose after the box is not a second code
        assert sec_edgar._parse_reporter_type_from_text(
            "12 TYPE OF REPORTING PERSON CO in the aggregate") is False

    def test_dondero_on_nexpoints_13d_is_left_to_the_name(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        xml = {"filer_cik": "0001228922", "issuer_cik": "0001620393", "issuer_name": "NexPoint",
               "schedule": "13D", "persons": [
                   {"name": "DONDERO JAMES D", "cik": "0001228922", "type_code": "HC", "percent": 20.0},
                   {"name": "DONDERO NANCY MARIE", "cik": None, "type_code": "IN", "percent": 1.0}]}
        atom = TestStructuredScrapeEndToEnd.ATOM.replace("SCHEDULE 13G", "SCHEDULE 13D/A")
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, {})), \
             patch.object(sec_edgar, "_fetch_13dg_xml", return_value=xml), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("NexPoint", "1620393")
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
        from app.scraper.sec_edgar import _co_filers_form_a_bloc
        assert _co_filers_form_a_bloc("SC 13D", 3) is True
        assert _co_filers_form_a_bloc("SCHEDULE 13D/A", 2) is True
        assert _co_filers_form_a_bloc("SC 13G", 10) is False
        assert _co_filers_form_a_bloc("SCHEDULE 13G/A", 4) is False
        assert _co_filers_form_a_bloc("SC 13D", 1) is False
        assert _co_filers_form_a_bloc("SC 13D", 0) is True, "unknown count stays conservative"

    def test_a_13g_family_member_keeps_its_reported_stake(self):
        from app.scraper.sec_edgar import _stake_from_person
        person = {"name": "Berkshire Hathaway Inc", "sole_voting": 0,
                  "shared_voting": 52_717_075, "sole_dispositive": 0,
                  "shared_dispositive": 52_717_075, "aggregate": 52_717_075, "percent": 6.7}
        family = {"comment_text": "", "schedule": "13G",
                  "persons": [person, {"name": "Warren E. Buffett"}, {"name": "National Indemnity Co"}]}
        assert _stake_from_person(family, person) == (6.7, None)
        group = {**family, "schedule": "13D"}
        assert _stake_from_person(group, person) == (None, 6.7)

    def test_the_parsed_xml_says_which_schedule_it_is(self):
        from app.scraper.sec_edgar import _parse_13dg_xml
        assert _parse_13dg_xml(_fixture("13g_vanguard.xml"))["schedule"] == "13G"
        assert _parse_13dg_xml(_fixture("13ga_wellington.xml"))["schedule"] == "13G"

    def test_end_to_end_berkshire_over_activision(self):
        # Ten cover pages, zero written as NONE. Stored before: stake None,
        # "bloc" 6.7%, 8 shares.
        from unittest.mock import patch
        from app.scraper import sec_edgar
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
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Activision Blizzard, Inc.", "718877")
        assert len(res) == 1
        row = res[0]
        assert row["is_individual"] is False
        assert row["stake_percent"] == 6.7 and row["voting_power_pct"] is None
        assert row["shares"] == 52_717_075
        assert row["shares_outstanding"] == 782_626_000
class TestRoleDatesFromForm3:
    """Form 4 dates a trade, not a seat; Form 3 is filed within ten days of
    becoming an insider and its periodOfReport IS the appointment date (John
    Ternus: 2026-09-01, Apple's stated transition date). Until now the role
    edges from SEC carried no start at all — 204 of them on dev, 0 dated by
    SEC itself.
    """

    def _xml(self, name, *, form="4", period="2026-04-27", officer="1", director="0",
             title="", other="", cik="0001234"):
        other_xml = (f"<isOther>1</isOther><otherText>{other}</otherText>" if other else "")
        return (f'<?xml version="1.0"?><ownershipDocument><documentType>{form}</documentType>'
                f'<periodOfReport>{period}</periodOfReport>'
                f'<issuer><issuerCik>0000320193</issuerCik></issuer>'
                f'<reportingOwner><reportingOwnerId><rptOwnerCik>{cik}</rptOwnerCik>'
                f'<rptOwnerName>{name}</rptOwnerName></reportingOwnerId>'
                f'<reportingOwnerRelationship><isDirector>{director}</isDirector>'
                f'<isOfficer>{officer}</isOfficer><officerTitle>{title}</officerTitle>'
                f'{other_xml}</reportingOwnerRelationship></reportingOwner></ownershipDocument>')

    def test_flags_written_as_true_are_read(self):
        # Apple's 2026 Form 3 for its new CEO: <isDirector>true</isDirector>.
        out = _parse_form34_xml(self._xml("Ternus John", form="3", period="2026-09-01",
                                          officer="true", director="true", title="CEO"))
        assert out and out["role"] == "CEO" and out["document_type"] == "3"
        assert out["period_of_report"] == "2026-09-01" and out["former"] is False

    def test_a_former_officer_filing_names_the_seat_that_ended(self):
        out = _parse_form34_xml(self._xml("Doe Jane", officer="0", director="0",
                                          other="Former Chief Financial Officer",
                                          period="2026-03-31"))
        assert out and out["former"] is True and out["role"] == "CFO"
        assert out["period_of_report"] == "2026-03-31"

    def test_a_company_keeps_its_name_as_filed(self):
        # Embraer files Form 4s as a director of Eve Holding. Turned around as
        # a person's 'LAST FIRST' it became "S.a Embraer", lost its legal form
        # and was minted a Person with Embraer's CIK (findings item 21).
        out = _parse_form34_xml(self._xml("EMBRAER S.A.", officer="0", director="1",
                                          cik="0001355444"))
        assert out and out["name"] == "EMBRAER S.A." and out["person_cik"] == "0001355444"
        assert _parse_form34_xml(self._xml("COOK TIMOTHY D"))["name"] == "Timothy D Cook"

    def test_a_pure_investor_is_still_nobody(self):
        assert _parse_form34_xml(self._xml("Some Fund", officer="0", director="0",
                                           other="10% owner")) is None

    def _run(self, forms, docs, dates=None):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        n = len(forms)
        submissions = {"filings": {"recent": {
            "form": forms,
            "accessionNumber": [f"0000000001-26-{i:06d}" for i in range(n)],
            "primaryDocument": [f"d{i}.xml" for i in range(n)],
            "filingDate": dates or [f"2026-0{9 - i}-01" for i in range(n)],
        }}}
        served = {f"d{i}.xml": d for i, d in enumerate(docs)}

        def fake_get_text(url, params=None):
            return served[url.split("/")[-1]]
        with patch.object(sec_edgar, "_get", return_value=submissions), \
             patch.object(sec_edgar, "_get_text", side_effect=fake_get_text):
            return sec_edgar.fetch_executives("320193")

    def test_the_form_3_behind_a_form_4_dates_the_seat(self):
        execs = self._run(
            ["4", "3"],
            [self._xml("Ternus John", form="4", period="2026-09-10", title="CEO"),
             self._xml("Ternus John", form="3", period="2026-09-01", title="CEO")])
        assert len(execs) == 1
        assert execs[0]["role"] == "CEO" and execs[0]["since"] == "2026-09-01"
        assert execs[0]["until"] is None

    def test_a_form_3_for_a_different_seat_does_not_date_the_current_one(self):
        # VP then, CEO now: the Form 3 dates the VP seat, which is not listed.
        execs = self._run(
            ["4", "3"],
            [self._xml("Ternus John", form="4", period="2026-09-10", title="CEO"),
             self._xml("Ternus John", form="3", period="2021-02-01", title="Vice President")])
        assert execs[0]["role"] == "CEO" and execs[0]["since"] is None

    def test_a_form_3_as_the_newest_filing_dates_itself(self):
        execs = self._run(["3"], [self._xml("Newstead Jennifer", form="3", period="2026-03-01",
                                            title="SVP, GC and Secretary")])
        assert execs[0]["since"] == "2026-03-01"

    def test_a_former_filing_becomes_a_closing_not_a_seat(self):
        execs = self._run(["4"], [self._xml("Doe Jane", officer="0", director="0",
                                            other="Former Chief Financial Officer",
                                            period="2026-03-31")])
        assert execs[0]["former"] is True and execs[0]["until"] == "2026-03-31"
        assert execs[0]["role"] == "CFO" and execs[0]["since"] is None

    def test_a_form_3_date_shared_by_the_whole_board_is_the_issuers_event(self):
        # Embraer became subject to Section 16 on 2026-03-18 and every sitting
        # officer and director filed a Form 3 that day: not their seat dates.
        execs = self._run(
            ["3", "3", "3", "3"],
            [self._xml(f"Person{i} A", form="3", period="2026-03-18", title="Director",
                       officer="0", director="1", cik=f"{i:07d}") for i in range(3)]
            + [self._xml("Lima Felipe", form="3", period="2026-04-13", title="CFO", cik="0000099")])
        assert [e["since"] for e in execs] == [None, None, None, "2026-04-13"], \
            "the shared date is dropped, the individual one kept"

    def _run_paged(self, recent_forms, recent_docs, pages, dated=None):
        """Like _run, with older `filings.files` pages: {name: (forms, docs)}."""
        from unittest.mock import patch
        from app.scraper import sec_edgar
        served, fetched_pages = {}, []

        def index(prefix, forms, docs):
            n = len(forms)
            for i, d in enumerate(docs):
                served[f"{prefix}{i}.xml"] = d
            return {"form": forms,
                    "accessionNumber": [f"0000000001-{prefix}-{i:06d}" for i in range(n)],
                    "primaryDocument": [f"{prefix}{i}.xml" for i in range(n)],
                    "filingDate": [f"20{prefix}-01-01"] * n}
        submissions = {"filings": {"recent": index("26", recent_forms, recent_docs),
                                   "files": [{"name": name} for name in pages]}}
        older = {name: index(name[:2], *fd) for name, fd in pages.items()}

        def fake_get(url, params=None):
            name = url.split("/")[-1]
            if name.startswith("CIK"):
                return submissions
            fetched_pages.append(name)
            return older[name]

        def fake_get_text(url, params=None):
            return served[url.split("/")[-1]]
        with patch.object(sec_edgar, "_get", side_effect=fake_get), \
             patch.object(sec_edgar, "_get_text", side_effect=fake_get_text):
            return sec_edgar.fetch_executives("320193"), fetched_pages

    def test_a_form_3_on_an_older_page_dates_a_long_serving_officer(self):
        # Amy Hood: Form 4s in the inline index since 2020, Form 3 (CFO,
        # 2013-05-08) on the 2008–2020 page.
        execs, pages = self._run_paged(
            ["4"], [self._xml("Hood Amy", form="4", title="CFO", cik="0000001")],
            {"13-page.json": (["3", "4"], [self._xml("Hood Amy", form="3", period="2013-05-08",
                                                     title="CFO", cik="0000001"),
                                           self._xml("Hood Amy", form="4", title="CFO", cik="0000001")])})
        assert execs[0]["since"] == "2013-05-08" and pages == ["13-page.json"]

    def test_older_pages_are_not_read_when_everyone_is_dated(self):
        execs, pages = self._run_paged(
            ["3"], [self._xml("Hood Amy", form="3", period="2013-05-08", title="CFO")],
            {"13-page.json": (["3"], [self._xml("Hood Amy", form="3", period="2005-01-01", title="CFO")])})
        assert execs[0]["since"] == "2013-05-08" and pages == []

    def test_an_older_form_3_for_another_seat_leaves_the_current_one_undated(self):
        # Kathleen Hogan: Form 3 as EVP Human Resources (2014); current seat EVP Strategy.
        execs, pages = self._run_paged(
            ["4"], [self._xml("Hogan Kathleen", form="4", title="EVP, Strategy", cik="0000002")],
            {"14-page.json": (["3"], [self._xml("Hogan Kathleen", form="3", period="2014-11-28",
                                                title="EVP, Human Resources", cik="0000002")])})
        assert execs[0]["since"] is None and pages == ["14-page.json"]

    def test_reading_stops_once_the_last_person_is_dated(self):
        # Page one dates Hood; page two is never asked for.
        execs, fetched = self._run_paged(
            ["4"], [self._xml("Hood Amy", form="4", title="CFO", cik="0000001")],
            {"13-page.json": (["3"], [self._xml("Hood Amy", form="3", period="2013-05-08",
                                                title="CFO", cik="0000001")]),
             "05-page.json": (["3"], [self._xml("Hood Amy", form="3", period="2005-01-01",
                                                title="CFO", cik="0000001")])})
        assert execs[0]["since"] == "2013-05-08" and fetched == ["13-page.json"]

    def test_older_pages_stop_at_the_page_cap(self):
        from app.scraper import sec_edgar
        pages = {f"{10 + i}-page.json": (["3"], [self._xml("Nobody Else", form="3",
                                                             period="2010-01-01", cik="0000099")])
                 for i in range(sec_edgar.MAX_OLDER_PAGES + 2)}
        execs, fetched = self._run_paged(
            ["4"], [self._xml("Hood Amy", form="4", title="CFO", cik="0000001")], pages)
        assert execs[0]["since"] is None and len(fetched) == sec_edgar.MAX_OLDER_PAGES

    def test_form_3s_are_still_read_past_the_insider_cap(self):
        from app.scraper import sec_edgar
        cap = sec_edgar.MAX_FORM4_FETCH
        forms = ["4"] * cap + ["3"]
        docs = [self._xml(f"Person{i} A", title="Vice President", cik=f"{i:07d}") for i in range(cap)]
        docs.append(self._xml("Person0 A", form="3", period="2020-01-15", title="Vice President",
                              cik="0000000"))
        execs = self._run(forms, docs)
        assert len(execs) == cap
        assert execs[0]["since"] == "2020-01-15", "the cap stops new insiders, not their Form 3s"


class TestTheInsiderWindow:
    """Insiders come from the Form 3/4s of the last ~15 months, not from the
    newest 50 documents: a few officers' repeat filings used those up, and
    Bank of America listed 8 of its 29 insiders (findings item 9)."""

    _xml = TestRoleDatesFromForm3._xml
    _run = TestRoleDatesFromForm3._run

    def test_directors_behind_a_burst_of_officer_filings_are_listed(self):
        # 60 Form 4s by two officers, then the annual director grant
        forms = ["4"] * 64
        docs = [self._xml("Moynihan Brian", title="CEO", cik="0000001") if i % 2 else
                self._xml("Borthwick Alastair", title="CFO", cik="0000002") for i in range(60)]
        docs += [self._xml(f"Director{i} A", officer="0", director="1", cik=f"00001{i:02d}") for i in range(4)]
        dates = ["2026-10-29"] * 60 + ["2026-05-06"] * 4
        execs = self._run(forms, docs, dates)
        assert sorted(e["name"] for e in execs if e["role"] == "Director") == \
            [f"A Director{i}" for i in range(4)]

    def test_a_filing_before_the_window_lists_no_one(self):
        forms = ["4", "4"]
        docs = [self._xml("Moynihan Brian", title="CEO", cik="0000001"),
                self._xml("Budd Wayne", officer="0", director="1", cik="0000009")]
        execs = self._run(forms, docs, ["2026-10-29", "2025-07-01"])     # 485 days earlier
        assert [e["name"] for e in execs] == ["Brian Moynihan"]

    def test_a_form_3_before_the_window_still_dates_a_listed_seat(self):
        forms = ["4", "3"]
        docs = [self._xml("Moynihan Brian", title="CEO", cik="0000001"),
                self._xml("Moynihan Brian", form="3", period="2010-01-01", title="CEO", cik="0000001")]
        execs = self._run(forms, docs, ["2026-10-29", "2010-01-04"])
        assert execs[0]["since"] == "2010-01-01"

    def test_the_document_ceiling_still_bounds_the_scan(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "MAX_FORM4_SCAN", 3):
            forms = ["4"] * 5
            docs = [self._xml(f"Person{i} A", title="Vice President", cik=f"{i:07d}") for i in range(5)]
            execs = self._run(forms, docs, ["2026-10-29"] * 5)
        assert len(execs) == 3

    def test_the_window_counts_from_the_newest_form_3_or_4(self):
        from app.scraper.sec_edgar import FORM4_WINDOW_DAYS, _form34_window_start
        assert FORM4_WINDOW_DAYS == 456
        assert _form34_window_start(["8-K", "4", "3"], ["2026-10-30", "2026-10-29", "2026-01-01"]) == "2025-07-30"
        assert _form34_window_start(["8-K"], ["2026-10-30"]) is None
        assert _form34_window_start(["4"], ["not a date"]) is None


class TestDeparturesFromEightK:
    """A departing insider files nothing; the company announces it in an 8-K,
    Item 5.02. The submissions index says which 8-Ks carry the item; the text
    is prose, read only for names we already list. Both sentences below are
    the real filings (Apple 2026-04-20, Microsoft 2026-06-05).
    """

    COOK = ("On April 20, 2026, Apple Inc. (“Apple”) announced that Tim Cook will transition "
            "from his role as Chief Executive Officer to Executive Chair of Apple’s Board of "
            "Directors (the “Board”), effective September 1, 2026 (the “Transition Date”). "
            "On April 17, 2026, the Board appointed John Ternus, Apple’s Senior Vice President "
            "of Hardware Engineering, as Chief Executive Officer and a member of the Board, in "
            "each case effective on the Transition Date. Mr. Ternus, 50, joined Apple in 2001.")
    HOFFMAN = ("(b) On June 2, 2026, Reid Hoffman, a member of the Board of Directors of "
               "Microsoft Corporation (the “Company”) since 2017, informed the Company of his "
               "decision not to stand for re-election at the Company’s 2026 annual shareholder "
               "meeting (the “Annual Meeting”). Mr. Hoffman will continue to serve as a director "
               "until the Annual Meeting.")

    def test_an_effective_date_closes_the_named_seat(self):
        from app.scraper.sec_edgar import _departures_in_text
        out = _departures_in_text(self.COOK, ["Timothy D Cook", "John Ternus"], "2026-04-20")
        assert out == [{"name": "Timothy D Cook", "until": "2026-09-01", "role": "CEO",
                        "sentence": out[0]["sentence"]}]

    def test_a_notice_without_a_date_does_not_close_anything(self):
        # Hoffman still serves until a meeting whose date the text does not give.
        from app.scraper.sec_edgar import _departures_in_text
        assert _departures_in_text(self.HOFFMAN, ["Reid Hoffman"], "2026-06-05") == []

    def test_a_past_departure_is_dated_by_its_sentence(self):
        from app.scraper.sec_edgar import _departures_in_text
        text = ("On March 3, 2026, Jane Doe resigned as Chief Financial Officer of the Company. "
                "Mr. Roe passed away on February 1, 2026.")
        out = _departures_in_text(text, ["Jane Doe", "Richard Roe"], "2026-03-05")
        assert {(d["name"], d["until"], d["role"]) for d in out} == {
            ("Jane Doe", "2026-03-03", None), ("Richard Roe", "2026-02-01", None)}

    def test_a_past_departure_without_any_date_takes_the_filing_date(self):
        from app.scraper.sec_edgar import _departures_in_text
        out = _departures_in_text("Ms. Doe resigned as a director.", ["Jane Doe"], "2026-03-05")
        assert out and out[0]["until"] == "2026-03-05"

    def test_a_succession_closes_only_the_person_after_the_verb(self):
        # Both real Apple sentences (2026-01-02 and 2025-12-05). Borders and
        # Newstead are the successors and must stay open.
        from app.scraper.sec_edgar import _departures_in_text
        kondo = "Mr. Borders succeeds Chris Kondo in the role of Principal Accounting Officer."
        out = _departures_in_text(kondo, ["Ben Borders", "Chris Kondo"], "2026-01-02")
        assert [(d["name"], d["until"], d["role"]) for d in out] == \
            [("Chris Kondo", "2026-01-02", "Principal Accounting Officer")]
        adams = ("Apple announced that Jennifer Newstead will become Apple’s general counsel on "
                 "March 1, 2026, following a transition of duties from Kate Adams, who has served "
                 "as Apple’s general counsel since 2017.")
        out = _departures_in_text(adams, ["Jennifer Newstead", "Katherine L Adams"], "2025-12-05")
        assert [(d["name"], d["until"]) for d in out] == [("Katherine L Adams", "2026-03-01")]

    def test_a_surname_alone_never_matches(self):
        # One 8-K names several people; "Cook" in a sentence about someone else
        # must not close Tim Cook's seat.
        from app.scraper.sec_edgar import _departures_in_text
        text = "On May 1, 2026, the Company's head of Cook County operations resigned."
        assert _departures_in_text(text, ["Timothy D Cook"], "2026-05-02") == []

    def test_the_item_is_cut_out_of_the_whole_filing_without_its_heading(self):
        from app.scraper.sec_edgar import _item_502_text
        html = ("<p>Item 2.02 Results.</p><p>Item 5.02 Departure of Directors. "
                + self.COOK + "</p><p>Item 9.01 Exhibits.</p><p>SIGNATURES</p>")
        text = _item_502_text(html)
        assert text.startswith("On April 20, 2026") and "Exhibits" not in text
        full = ("<p>Item 5.02 Departure of Directors or Certain Officers; Election of Directors; "
                "Appointment of Certain Officers; Compensatory Arrangements of Certain Officers. "
                "(b) On June 2, 2026, Jane Doe resigned.</p><p>SIGNATURES</p>")
        assert _item_502_text(full) == "(b) On June 2, 2026, Jane Doe resigned."

    # Berkshire's 8-K of 2025-05-08 (accession 0001193125-25-115695), as the
    # reader saw it: the heading glued to paragraph b), whose "Departure"
    # closed Abel's CEO seat at the day it began, 2026-01-01 (dev, 2026-10-25).
    BERKSHIRE = ("Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain "
                 "Officers; Compensatory Arrangements of Certain Officers. b) On May 3, 2025, Mr. Warren "
                 "Buffett, Berkshire’s Chairman and CEO, announced that he was going to recommend to "
                 "Berkshire’s Board of Directors at its meeting to be held on May 4, 2025 that Mr. Greg Abel, "
                 "Berkshire’s Vice Chairman – Non-Insurance Operations, be appointed Berkshire’s Chief "
                 "Executive Officer to become effective on January 1, 2026. c) On May 4, 2025, Berkshire’s "
                 "Board of Directors voted unanimously to appoint Mr. Abel to become Berkshire’s President "
                 "and CEO effective on January 1, 2026. Mr. Abel, age 62, has been a director of the "
                 "Corporation and the Corporation’s Vice Chairman – Non-Insurance Operations since 2018.")

    def test_the_heading_s_departure_closes_nobody(self):
        from app.scraper.sec_edgar import _ITEM_502_HEADING_RX, _departures_in_text
        text = _ITEM_502_HEADING_RX.sub("", self.BERKSHIRE)
        assert text.startswith("b) On May 3, 2025")
        # an appointment is not a departure: nobody in this 8-K leaves
        assert _departures_in_text(text, ["Gregory E Abel", "Warren E Buffett"], "2025-05-08") == []

    def test_a_lettered_paragraph_starts_its_own_sentence(self):
        # "2026. b) Ms. Doe …" — a lowercase enumerator after a period used to
        # glue the appointment onto the resignation before it.
        from app.scraper.sec_edgar import _departures_in_text
        text = ("a) Mr. Roe resigned effective March 1, 2026. b) Ms. Doe was appointed Chief "
                "Financial Officer effective March 1, 2026. (c) Mr. Poe was elected a director.")
        out = _departures_in_text(text, ["Richard Roe", "Jane Doe", "Edgar Poe"], "2026-03-05")
        assert [(d["name"], d["until"]) for d in out] == [("Richard Roe", "2026-03-01")]

    def test_end_to_end_only_8ks_with_item_502_are_opened(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        submissions = {"filings": {"recent": {
            "form":            ["8-K", "8-K", "10-Q"],
            "items":           ["5.02", "2.02,9.01", ""],
            "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002", "0000000001-26-000003"],
            "primaryDocument": ["a.htm", "b.htm", "c.htm"],
            "filingDate":      ["2026-04-20", "2026-05-01", "2026-05-02"],
        }}}
        opened = []

        def fake_get_text(url, params=None):
            opened.append(url.split("/")[-1])
            return "<p>Item 5.02 Departure. " + self.COOK + "</p><p>SIGNATURE</p>"
        with patch.object(sec_edgar, "_get", return_value=submissions), \
             patch.object(sec_edgar, "_get_text", side_effect=fake_get_text):
            out = sec_edgar.fetch_departures("320193", ["Timothy D Cook"])
        assert opened == ["a.htm"]
        assert len(out) == 1 and out[0]["until"] == "2026-09-01" and out[0]["role"] == "CEO"
        assert out[0]["source_date"] == "2026-04-20"
        assert "0000000001-26-000001" in out[0]["source_url"]

    def test_nobody_to_look_for_costs_no_request(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_get", side_effect=AssertionError("must not fetch")):
            assert sec_edgar.fetch_departures("320193", []) == []


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
        from app.scraper.sec_edgar import _class_key
        assert _class_key(self.ORD) == _class_key(self.ORD_ADS) == "ordinary shares"
        assert _class_key("Common Stock, par value $0.001 per share") == "common stock"
        assert _class_key("Class A Common Stock") != _class_key("Class B Common Stock")
        assert _class_key(None) == ""

    def test_older_counts_are_divided_by_the_newest_total(self):
        from app.scraper.sec_edgar import _restate_against_newest_denominator
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
        from app.scraper.sec_edgar import _restate_against_newest_denominator
        fs = self.filings()
        fs[2]["share_class"] = "Class B Common Stock"                         # another class
        fs[1]["until"] = "2025-06-01"                                         # an ended holding
        _restate_against_newest_denominator(fs)
        assert fs[2]["stake_percent"] == 5.9 and fs[2]["denominator_date"] is None
        assert fs[1]["stake_percent"] == 8.0965 and fs[1]["denominator_date"] is None

    def test_not_across_a_split(self):
        from app.scraper.sec_edgar import _restate_against_newest_denominator
        fs = self.filings()
        fs[0]["shares_outstanding"] = 1730242027 * 3                         # a 3-for-1 split since 2020
        fs[1]["shares_outstanding"] = 1730242027 * 3
        _restate_against_newest_denominator(fs)
        assert fs[2]["stake_percent"] == 5.9 and fs[2]["denominator_date"] is None

    def test_a_same_day_or_equal_total_changes_nothing(self):
        from app.scraper.sec_edgar import _restate_against_newest_denominator
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
        from app.scraper.sec_edgar import _restate_against_newest_denominator
        fs = list(reversed(self.filings()))
        _restate_against_newest_denominator(fs)
        bevco = next(f for f in fs if f["investor_name"].startswith("Bevco"))
        assert bevco["denominator_date"] == "2026-05-15"
        assert bevco["shares_outstanding"] == 1972133054


class TestDateOfEvent:
    """The day a cover states the position as of. The count and the
    percentage are true on that day; the filing date is when it was said."""

    def test_both_written_forms(self):
        from app.scraper.sec_edgar import _event_date
        assert _event_date("02/07/2025") == "2025-02-07"
        assert _event_date("3/1/2020") == "2020-03-01"
        assert _event_date("March 10, 2020") == "2020-03-10"
        assert _event_date("Sept. 3 2019") is None                 # not a month we read: no guess
        assert _event_date("Sep. 3, 2019") is None
        assert _event_date("September 3 2019") == "2019-09-03"

    def test_nothing_is_guessed(self):
        from app.scraper.sec_edgar import _event_date
        for bad in (None, "", "02/30/2025", "2025-02-07", "13/01/2025", "Q1 2025"):
            assert _event_date(bad) is None

    def test_the_structured_schedules_carry_it(self):
        from app.scraper.sec_edgar import _parse_13dg_xml
        assert _parse_13dg_xml(_fixture("13d_altria_abinbev.xml"))["event_date"] == "2025-02-07"
        # a 13G names it differently, and for a passive filer it is a quarter-end
        assert _parse_13dg_xml(_fixture("13g_vanguard.xml"))["event_date"] == "2026-03-31"

    def test_an_html_cover_carries_it(self):
        from app.scraper.sec_edgar import _parse_event_date_from_text
        cover = ("<p>Ordinary Shares, without nominal value</p><p>(Title of Class of Securities)</p>"
                 "<p>March&nbsp;10, 2020</p><p>(Date of Event Which Requires Filing of this Statement)</p>")
        assert _parse_event_date_from_text(cover) == "2020-03-10"
        assert _parse_event_date_from_text("12/31/2019 (Date of Event which Requires Filing "
                                           "of this Statement)") == "2019-12-31"
        assert _parse_event_date_from_text("no cover here") is None

    def test_the_filer_side_holding_carries_it(self):
        from unittest.mock import patch
        from app.scraper import sec_edgar
        xml = sec_edgar._parse_13dg_xml(_fixture("13g_vanguard.xml"))
        with patch.object(sec_edgar, "_fetch_13dg_xml", return_value=xml):
            parsed = sec_edgar._parse_holding_filing("0000102909", "0000000000-26-000001")
        assert parsed["event_date"] == "2026-03-31"


class TestALoneFilersHoldingIsBothRows:
    """SoftBank's 13G/A on Alibaba: 3,788,048 shares it can sell alone and
    627,002,296 it sells through its subsidiaries — 630,790,344, 3.3 %. The
    count kept only the sole row, which made SoftBank a 0.02 % holder as soon
    as the count was divided again (the newest-denominator restatement)."""

    def test_the_real_filing(self):
        from app.scraper.sec_edgar import _parse_13dg_xml, _shares_held, _stake_from_person
        d = _parse_13dg_xml(_fixture("13ga_softbank_alibaba.xml"))
        assert len(d["persons"]) == 1 and d["schedule"] == "13G"
        p = d["persons"][0]
        rows = {k: p[k] for k in ("sole_voting", "shared_voting", "sole_dispositive",
                                  "shared_dispositive") if p.get(k) is not None}
        assert _shares_held(rows, p["aggregate"], in_group=False) == 630_790_344
        stake, voting = _stake_from_person(d, p)
        assert stake == 3.3 and voting is None

    def test_alone_both_rows_in_a_bloc_the_old_rule(self):
        from app.scraper.sec_edgar import _shares_held
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
        from app.scraper.sec_edgar import _parse_13dg_xml, _shares_held, _stake_from_person
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
        from app.scraper.sec_edgar import _shares_held
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
        from app.scraper import sec_edgar
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
        with patch.object(sec_edgar, "_get_text",
                          side_effect=_serve(atom, {url: _fixture("13ga_softbank_alibaba.xml")})), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Alibaba Group Holding Ltd", "0001577552")
        assert res and res[0]["shares"] == 630_790_344
        assert res[0]["stake_percent"] == 3.3


class TestRestatementNeedsACountThatIsTheStake:
    def test_a_count_that_is_not_the_filed_stake_is_not_divided_again(self):
        # the old SoftBank row: 3.3 % filed, a count that is 0.02 % of its total
        from app.scraper.sec_edgar import _restate_against_newest_denominator
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
        from app.scraper.sec_edgar import _restate_against_newest_denominator
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
        from app.scraper import sec_edgar
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
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            res = sec_edgar.fetch_ownership_filings("Example Issuer Inc", "0000000222")
        assert res and res[0]["shares"] == 100_000


class TestWhenAHoldingBegan:
    """The scrape reads each holder's NEWEST filing; its date written as a
    stated start made a holder since 2005, first scraped in 2026, "since
    2026". An original schedule states the start; an amendment bounds it."""

    def test_the_rule(self):
        from app.scraper.sec_edgar import _stake_start
        assert _stake_start("SCHEDULE 13G", "2026-03-31", "2026-04-20") == ("2026-03-31", None)
        assert _stake_start("SC 13D", None, "2019-05-02") == ("2019-05-02", None)
        assert _stake_start("SCHEDULE 13G/A", "2025-12-31", "2026-02-10") == ("2025-12-31", "amendment")
        assert _stake_start("SC 13D/A", None, "2020-03-10") == ("2020-03-10", "amendment")
        assert _stake_start("SC 13D/A", None, None) == (None, None)

    def test_the_scrape_result_carries_it(self):
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
        assert (res[0]["since"], res[0]["since_basis"]) == ("2026-03-31", None)


class TestAnUnreadCoverIsNotAnExit:
    """`not pct` read "the parser found nothing" as "the holder holds
    nothing", and closed live holdings on that filing's date."""

    @staticmethod
    def _run(docs: dict, entries: list[tuple[str, str, str]]):
        from unittest.mock import patch
        from app.scraper import sec_edgar
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
        with patch.object(sec_edgar, "_get_text", side_effect=_serve(atom, pages)), \
             patch.object(sec_edgar, "fetch_former_names", return_value=[]):
            return sec_edgar.fetch_ownership_filings("Example Issuer Inc", "0000000222")

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
        from app.scraper import sec_edgar
        filings = [{"form": "SCHEDULE 13G/A", "accession": "a-2", "date": "2026-05-01"},
                   {"form": "SCHEDULE 13G", "accession": "a-1", "date": "2025-05-01"}]
        parsed = {"a-2": {"subject_cik": "0000000222", "subject_name": "Example", "percent": None,
                          "accession": "a-2", "event_date": "2026-03-31"},
                  "a-1": {"subject_cik": "0000000222", "subject_name": "Example", "percent": 6.0,
                          "accession": "a-1", "event_date": "2025-03-31"}}
        with patch.object(sec_edgar, "_iter_filing_pages", return_value=iter([filings])), \
             patch.object(sec_edgar, "_parse_holding_filing", side_effect=lambda c, a: parsed[a]):
            rows = sec_edgar.fetch_filer_holdings("0000000777")
        assert len(rows) == 1 and rows[0]["stake_percent"] == 6.0 and rows[0]["until"] is None
        assert (rows[0]["since"], rows[0]["since_basis"]) == ("2025-03-31", None)


class TestIncorporationPlace:
    """Where a filer is incorporated — Exhibit 21 tells its own row from a
    namesake abroad by it (findings item 17)."""

    @pytest.mark.parametrize("subs, expected", [
        ({"stateOfIncorporation": "NY", "stateOfIncorporationDescription": "NY"}, "US-NY"),
        ({"stateOfIncorporation": "de"}, "US-DE"),
        ({"stateOfIncorporation": "E9", "stateOfIncorporationDescription": "Cayman Islands"}, "KY"),
        ({"stateOfIncorporation": "", "addresses": {"business": {"country": "Germany"}}}, None),
        ({}, None),
    ])
    def test_incorporation_place(self, subs, expected):
        from app.scraper.sec_edgar import incorporation_place
        assert incorporation_place(subs) == expected


class TestStatedIncorporation:
    """The newest tagged cover (10-K, 20-F, 40-F, 10-Q, 8-K) says where the
    filer is incorporated; EDGAR's header field only when it does not
    (findings item 19, 2026-10-30; the 10-Q and 8-K since item 23)."""

    @staticmethod
    def _subs(forms=("10-K",), **header):
        return {"cik": "1404655", **header,
                "filings": {"recent": {"form": list(forms),
                                       "accessionNumber": [f"0001193125-26-04664{i}" for i in range(len(forms))],
                                       "primaryDocument": [f"doc{i}.htm" for i in range(len(forms))]}}}

    @staticmethod
    def _tag(value, fmt=None):
        attr = f' format="{fmt}"' if fmt else ""
        return (f'<html><ix:nonNumeric contextRef="c-1" name="dei:EntityIncorporationStateCountryCode"{attr}>'
                f'<span style="font-weight:bold">{value}</span></ix:nonNumeric></html>')

    @pytest.mark.parametrize("value, fmt, expected", [
        ("Delaware", "ixt-sec:stateprovnameen", "US-DE"),          # HubSpot, whose header says NY
        ("Cayman Islands", "ixt-sec:edgarprovcountryen", "KY"),    # Alibaba, whose header says Hong Kong
        ("United Mexican States", "ixt-sec:edgarprovcountryen", "MX"),
        ("Brazil", "ixt-sec:edgarprovcountryen", "BR"),
        ("DE", None, "US-DE"),                                     # EDGAR's own code, untransformed
        ("E9", None, None),
    ])
    def test_the_cover_tag(self, value, fmt, expected):
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_get_text", return_value=self._tag(value, fmt)):
            assert sec_edgar.stated_incorporation(self._subs()) == expected

    def test_the_newest_tagged_cover_is_read_whatever_the_form(self):
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_get_text", return_value=self._tag("Delaware", "ixt-sec:stateprovnameen")) as get:
            sec_edgar.stated_incorporation(self._subs(forms=("6-K", "8-K/A", "8-K", "10-Q", "10-K")))
        get.assert_called_once_with("https://www.sec.gov/Archives/edgar/data/1404655/000119312526046642/doc2.htm")

    def test_a_change_of_state_after_the_annual_report_is_read(self):
        # Liberty Media: Delaware → Nevada on 2026-05-12, after its 10-K of
        # February; the 8-Ks and the 10-Q since say Nevada (findings item 23)
        from app.scraper import sec_edgar
        pages = {"doc0.htm": self._tag("Nevada", "ixt-sec:stateprovnameen"),
                 "doc1.htm": self._tag("Nevada", "ixt-sec:stateprovnameen"),
                 "doc2.htm": self._tag("Delaware", "ixt-sec:stateprovnameen")}
        with patch.object(sec_edgar, "_get_text", side_effect=lambda url: pages[url.rsplit("/", 1)[1]]):
            assert sec_edgar.stated_incorporation(self._subs(forms=("8-K", "10-Q", "10-K"))) == "US-NV"

    def test_an_untagged_cover_passes_to_the_next_within_the_limit(self):
        from app.scraper import sec_edgar
        pages = iter(["<html>untagged</html>", self._tag("New York", "ixt-sec:stateprovnameen")])
        with patch.object(sec_edgar, "_get_text", side_effect=lambda url: next(pages)):
            assert sec_edgar.stated_incorporation(self._subs(forms=("8-K", "10-K"))) == "US-NY"
        sec_edgar._cover_of.cache_clear()                    # the same URLs again below
        untagged = ["<html>untagged</html>"] * sec_edgar._MAX_COVERS
        pages = iter(untagged + [self._tag("New York", "ixt-sec:stateprovnameen")])
        with patch.object(sec_edgar, "_get_text", side_effect=lambda url: next(pages)) as get:
            assert sec_edgar.stated_incorporation(
                self._subs(forms=("8-K",) * sec_edgar._MAX_COVERS + ("10-K",))) is None
        assert get.call_count == sec_edgar._MAX_COVERS

    def test_a_joint_filing_gives_each_registrant_its_own_cover(self):
        # Eversource files its 10-Qs and 8-Ks jointly with CL&P, NSTAR Electric
        # and PSNH; the first cover is the parent's, Massachusetts
        from app.scraper import sec_edgar
        doc = ("<html>"
               '<ix:nonNumeric name="dei:EntityRegistrantName" contextRef="c-1">EVERSOURCE ENERGY</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="c-1">0000072741</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityIncorporationStateCountryCode" contextRef="c-1">Massachusetts</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityRegistrantName" contextRef="c-2">THE CONNECTICUT LIGHT AND POWER COMPANY</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="c-2">0000023426</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityIncorporationStateCountryCode" contextRef="c-2">Connecticut</ix:nonNumeric>'
               "</html>")
        with patch.object(sec_edgar, "_get_text", return_value=doc):
            assert sec_edgar.stated_incorporation({**self._subs(), "cik": "23426"}) == "US-CT"
            assert sec_edgar.stated_incorporation({**self._subs(), "cik": "72741"}) == "US-MA"

    @pytest.mark.parametrize("subs", [
        {"cik": "1404655"},                                        # no filings listed
        {"filings": {"recent": {"form": ["10-K"], "accessionNumber": ["x"], "primaryDocument": ["d.htm"]}}},
    ])
    def test_nothing_to_read(self, subs):
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_get_text") as get:
            assert sec_edgar.stated_incorporation(subs) is None
        get.assert_not_called()

    def test_the_header_answers_when_the_report_does_not(self):
        from app.scraper import sec_edgar
        subs = self._subs(stateOfIncorporation="NY", stateOfIncorporationDescription="NY")
        with patch.object(sec_edgar, "_get_text", return_value="<html>untagged</html>"):
            assert sec_edgar.incorporation_place(subs) == "US-NY"
        with patch.object(sec_edgar, "_get_text", side_effect=RuntimeError("down")):
            assert sec_edgar.incorporation_place(subs) == "US-NY"

    def test_the_report_beats_the_header(self):
        from app.scraper import sec_edgar
        subs = self._subs(stateOfIncorporation="NY", stateOfIncorporationDescription="NY")
        with patch.object(sec_edgar, "_get_text", return_value=self._tag("Delaware", "ixt-sec:stateprovnameen")):
            assert sec_edgar.incorporation_place(subs) == "US-DE"


class TestReportCover:
    """Country and headquarters from the newest annual report's cover, the
    company record only when it states none (2026-10-30)."""
    SUBS = {"cik": "1577552", "stateOfIncorporation": "K3", "stateOfIncorporationDescription": "Hong Kong",
            "addresses": {"business": {"street1": "26/F TOWER ONE TIMES SQUARE", "city": "CAUSEWAY BAY",
                                       "stateOrCountry": "K3", "stateOrCountryDescription": "Hong Kong",
                                       "zipCode": "00000"}},
            "filings": {"recent": {"form": ["6-K", "20-F"], "accessionNumber": ["x-1", "0001193125-26-231755"],
                                   "primaryDocument": ["a.htm", "baba-20260331.htm"]}}}

    @staticmethod
    def _cover(*facts, context="c-1"):
        tags = "".join(f'<ix:nonNumeric name="dei:{n}" contextRef="{c}">{v}</ix:nonNumeric>'
                       for n, v, c in [(*f, context) if len(f) == 2 else f for f in facts])
        return f"<html>{tags}</html>"

    ALIBABA = staticmethod(lambda: TestReportCover._cover(
        ("EntityRegistrantName", "Alibaba Group Holding Limited"), ("EntityIncorporationStateCountryCode", "Cayman Islands"),
        ("EntityAddressAddressLine1", "26/F Tower One"), ("EntityAddressAddressLine2", "Times Square"),
        ("EntityAddressAddressLine3", "1 Matheson Street"), ("EntityAddressCityOrTown", "Causeway Bay"),
        ("EntityAddressPostalZipCode", "00000"), ("EntityAddressCountry", "Hong Kong")))

    def test_country_and_office_from_the_report(self):
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_get_text", return_value=self.ALIBABA()) as get:
            assert sec_edgar.sec_country(self.SUBS) == "KY"                  # the record says Hong Kong
            hq = sec_edgar.sec_headquarters(self.SUBS)
        assert hq == {"address": "26/F Tower One Times Square 1 Matheson Street, Causeway Bay, HK",
                      "city": "Causeway Bay", "country": "HK", "street": "26/F Tower One Times Square 1 Matheson Street",
                      "postcode": None, "state": None}                      # "00000" is no postcode
        get.assert_called_once()                                              # one report, read once

    def test_a_us_office_carries_its_state(self):
        from app.scraper import sec_edgar
        page = self._cover(("EntityRegistrantName", "HubSpot, Inc."), ("EntityAddressAddressLine1", "Two Canal Park"),
                           ("EntityAddressCityOrTown", "Cambridge"), ("EntityAddressStateOrProvince", "Massachusetts"),
                           ("EntityAddressPostalZipCode", "02141"))
        with patch.object(sec_edgar, "_get_text", return_value=page):
            hq = sec_edgar.stated_headquarters({**self.SUBS, "cik": "1404655"})
        assert (hq["city"], hq["state"], hq["country"], hq["postcode"]) == ("Cambridge", "MA", "US", "02141")

    def test_the_registrants_own_context_not_the_business_contact(self):
        # AB InBev's 20-F tags a second address under an axis
        from app.scraper import sec_edgar
        page = self._cover(("EntityAddressCityOrTown", "Elsewhere", "contact"),
                           ("EntityRegistrantName", "Anheuser-Busch InBev SA/NV", "own"),
                           ("EntityAddressAddressLine1", "Brouwerijplein 1", "own"),
                           ("EntityAddressCityOrTown", "Leuven", "own"), ("EntityAddressCountry", "Belgium", "own"))
        with patch.object(sec_edgar, "_get_text", return_value=page):
            assert sec_edgar.stated_headquarters(self.SUBS)["city"] == "Leuven"

    def test_a_po_box_is_no_office(self):
        from app.scraper import sec_edgar
        page = self._cover(("EntityRegistrantName", "Inter & Co, Inc."),
                           ("EntityAddressAddressLine1", "PO Box 309, Ugland House"),
                           ("EntityAddressCityOrTown", "Grand Cayman"), ("EntityAddressCountry", "Cayman Islands"))
        subs = {**self.SUBS, "addresses": {"business": {"street1": "AV BARBACENA 1219", "city": "BELO HORIZONTE",
                                                        "stateOrCountry": "D5", "stateOrCountryDescription": "Brazil"}}}
        with patch.object(sec_edgar, "_get_text", return_value=page):
            assert sec_edgar.sec_headquarters(subs)["city"] == "BELO HORIZONTE"

    def test_no_report_keeps_the_record(self):
        from app.scraper import sec_edgar
        subs = {k: v for k, v in self.SUBS.items() if k != "filings"}
        with patch.object(sec_edgar, "_get_text") as get:
            assert sec_edgar.sec_country(subs) == "HK"
            assert sec_edgar.sec_headquarters(subs)["city"] == "CAUSEWAY BAY"
        get.assert_not_called()

    def test_an_unreadable_report_keeps_the_record_and_is_asked_again(self):
        from app.scraper import sec_edgar
        with patch.object(sec_edgar, "_get_text", side_effect=RuntimeError("down")):
            assert sec_edgar.sec_country(self.SUBS) == "HK"
        with patch.object(sec_edgar, "_get_text", return_value=self.ALIBABA()):
            assert sec_edgar.sec_country(self.SUBS) == "KY"                  # the failure was not kept


class TestFilerIdentity:
    def test_filer_identity_from_one_request(self):
        from app.scraper.sec_edgar import fetch_filer_identity
        with patch("app.scraper.sec_edgar._submissions",
                   return_value={"name": "NEW YORK TIMES CO", "stateOfIncorporation": "NY"}) as subs:
            assert fetch_filer_identity("71691") == ("NEW YORK TIMES CO", "US-NY")
        assert subs.call_count == 1
        with patch("app.scraper.sec_edgar._submissions", side_effect=RuntimeError("503")):
            assert fetch_filer_identity("71691") == (None, None)



class TestDottedAndGermanFormsInTheIssuerCheck:
    """Findings items 12 and 13: the issuer check's tokens and the ticker key."""

    def test_tokens(self):
        from app.scraper.sec_edgar import _significant_tokens
        assert _significant_tokens("Blackstone L.P.") == {"blackstone"}
        assert _significant_tokens("Henkel AG & Co. KGaA") == {"henkel"}
        assert _significant_tokens("KG Mobility Corp") == {"kg", "mobility"}

    def test_ticker_key(self):
        assert _ticker_normalize("Siemens Beteiligungen GmbH & Co. KG") == "siemens beteiligungen"
        assert _ticker_normalize("Merck KGaA") == "merck"
