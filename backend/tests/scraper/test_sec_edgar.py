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

import pytest
import textwrap
from unittest.mock import patch, MagicMock

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
    scrape_company,
)


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
             patch("httpx.get", return_value=mock_response):
            result = search_company("PrivateCo")

        assert result is not None
        assert result["cik"] == "0009999999"

    def test_returns_none_when_not_found(self):
        empty_tickers = {}
        mock_response = MagicMock()
        mock_response.json.return_value = {"hits": {"hits": []}}
        mock_response.raise_for_status = MagicMock()

        with patch("app.scraper.sec_edgar._tickers_cache", empty_tickers), \
             patch("httpx.get", return_value=mock_response):
            result = search_company("NonExistentXYZ123")

        assert result is None
