"""
Tests for open_corporates.py.

All HTTP calls are mocked — tests never touch the real API.
Specifically validates the 401 PermissionError fix: previously a 401
was silently swallowed and returned no_results; now it raises PermissionError
with an actionable message.
"""

import pytest
from unittest.mock import patch, MagicMock
import httpx

from app.scraper.open_corporates import (
    search_company,
    fetch_company_details,
    fetch_officers,
    scrape_company,
    _params,
)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.json.return_value = json_data
    if status_code >= 400:
        mock.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=mock
        )
    else:
        mock.raise_for_status = MagicMock()
    return mock


# ── Params helper ──────────────────────────────────────────────────────────────

class TestParams:
    def test_no_api_key(self):
        with patch("app.scraper.open_corporates._api_key", return_value=""):
            p = _params()
        assert "api_token" not in p
        assert p["format"] == "json"

    def test_with_api_key(self):
        with patch("app.scraper.open_corporates._api_key", return_value="mytoken"):
            p = _params()
        assert p["api_token"] == "mytoken"


# ── 401 handling (regression for the silent no_results bug) ───────────────────

class TestAuthError:
    """A 401 must raise PermissionError, not return None silently."""

    def test_search_raises_on_401(self):
        mock = _mock_response({}, status_code=401)
        with patch("httpx.get", return_value=mock):
            with pytest.raises(PermissionError, match="API token"):
                search_company("Tesla")

    def test_details_raises_on_401(self):
        mock = _mock_response({}, status_code=401)
        with patch("httpx.get", return_value=mock):
            with pytest.raises(PermissionError, match="API token"):
                fetch_company_details("us_de", "1234567")

    def test_officers_raises_on_401(self):
        mock = _mock_response({}, status_code=401)
        with patch("httpx.get", return_value=mock):
            with pytest.raises(PermissionError, match="API token"):
                fetch_officers("us_de", "1234567")

    def test_200_with_error_payload_raises_runtime_error(self):
        """Some OC errors come back as HTTP 200 with an error key in the body."""
        mock = _mock_response({"error": {"message": "Invalid Api Token."}})
        with patch("httpx.get", return_value=mock):
            with pytest.raises(RuntimeError, match="Invalid Api Token"):
                search_company("Tesla")


# ── search_company ─────────────────────────────────────────────────────────────

class TestSearchCompany:
    SEARCH_RESPONSE = {
        "results": {
            "companies": [
                {"company": {
                    "name": "Tesla, Inc.",
                    "jurisdiction_code": "us_de",
                    "company_number": "4554982",
                }}
            ]
        }
    }

    def test_returns_best_match(self):
        mock = _mock_response(self.SEARCH_RESPONSE)
        with patch("httpx.get", return_value=mock):
            result = search_company("Tesla")
        assert result["name"] == "Tesla, Inc."
        assert result["jurisdiction_code"] == "us_de"
        assert result["company_number"] == "4554982"

    def test_returns_none_when_empty(self):
        mock = _mock_response({"results": {"companies": []}})
        with patch("httpx.get", return_value=mock):
            result = search_company("NonExistentXYZ")
        assert result is None

    def test_http_error_returns_none(self):
        mock = _mock_response({}, status_code=500)
        with patch("httpx.get", return_value=mock):
            result = search_company("Tesla")
        assert result is None


# ── fetch_company_details ──────────────────────────────────────────────────────

class TestFetchCompanyDetails:
    DETAILS_RESPONSE = {
        "results": {
            "company": {
                "registered_address": {
                    "street_address": "1 Tesla Road",
                    "locality":       "Austin",
                    "country":        "United States",
                    "postal_code":    "78725",
                },
                "incorporation_date": "2003-07-01",
                "company_type":       "DOMESTIC STOCK COMPANY",
                "current_status":     "Active",
            }
        }
    }

    def test_extracts_address(self):
        mock = _mock_response(self.DETAILS_RESPONSE)
        with patch("httpx.get", return_value=mock):
            details = fetch_company_details("us_de", "4554982")
        assert details["registered_address"]["city"] == "Austin"
        assert details["registered_address"]["country"] == "United States"
        assert details["registered_address"]["street"] == "1 Tesla Road"
        assert details["registered_address"]["zip"] == "78725"

    def test_extracts_metadata(self):
        mock = _mock_response(self.DETAILS_RESPONSE)
        with patch("httpx.get", return_value=mock):
            details = fetch_company_details("us_de", "4554982")
        assert details["incorporation_date"] == "2003-07-01"
        assert details["status"] == "Active"

    def test_http_error_returns_empty_dict(self):
        mock = _mock_response({}, status_code=500)
        with patch("httpx.get", return_value=mock):
            details = fetch_company_details("us_de", "4554982")
        assert details == {}


# ── fetch_officers ─────────────────────────────────────────────────────────────

class TestFetchOfficers:
    OFFICERS_RESPONSE = {
        "results": {
            "officers": [
                {"officer": {"name": "Elon Musk",  "position": "Chief Executive Officer",
                             "start_date": "2008-10-01", "end_date": None}},
                {"officer": {"name": "Zachary Kirkhorn", "position": "Chief Financial Officer",
                             "start_date": "2019-05-07", "end_date": None}},
                {"officer": {"name": "Elon Musk",  "position": "Director",
                             "start_date": "2004-01-01", "end_date": None}},  # duplicate
            ]
        }
    }

    def test_returns_list(self):
        mock = _mock_response(self.OFFICERS_RESPONSE)
        with patch("httpx.get", return_value=mock):
            officers = fetch_officers("us_de", "4554982")
        assert len(officers) == 2  # Elon Musk deduplicated

    def test_officer_fields(self):
        mock = _mock_response(self.OFFICERS_RESPONSE)
        with patch("httpx.get", return_value=mock):
            officers = fetch_officers("us_de", "4554982")
        elon = officers[0]
        assert elon["name"] == "Elon Musk"
        assert elon["role"] == "Chief Executive Officer"
        assert elon["start_date"] == "2008-10-01"

    def test_http_error_returns_empty_list(self):
        mock = _mock_response({}, status_code=500)
        with patch("httpx.get", return_value=mock):
            officers = fetch_officers("us_de", "4554982")
        assert officers == []


# ── scrape_company (integration of the three calls) ───────────────────────────

class TestScrapeCompany:
    def _make_mocks(self):
        search  = _mock_response({"results": {"companies": [{"company": {
            "name": "Tesla, Inc.", "jurisdiction_code": "us_de", "company_number": "4554982",
        }}]}})
        details = _mock_response({"results": {"company": {
            "registered_address": {"street_address": "1 Tesla Road", "locality": "Austin",
                                   "country": "United States", "postal_code": "78725"},
            "incorporation_date": "2003-07-01", "company_type": "DOMESTIC STOCK COMPANY",
            "current_status": "Active",
        }}})
        officers = _mock_response({"results": {"officers": [
            {"officer": {"name": "Elon Musk", "position": "CEO",
                         "start_date": "2008-10-01", "end_date": None}},
        ]}})
        return [search, details, officers]

    def test_full_scrape_returns_structured_dict(self):
        with patch("httpx.get", side_effect=self._make_mocks()):
            result = scrape_company("Tesla")
        assert result is not None
        assert result["name"] == "Tesla, Inc."
        assert result["jurisdiction_code"] == "us_de"
        assert result["incorporation_date"] == "2003-07-01"
        assert len(result["officers"]) == 1
        assert result["officers"][0]["name"] == "Elon Musk"

    def test_returns_none_when_not_found(self):
        not_found = _mock_response({"results": {"companies": []}})
        with patch("httpx.get", return_value=not_found):
            result = scrape_company("NonExistentXYZ")
        assert result is None


class TestStampRegistration:
    """jurisdiction + number IS OpenCorporates' identity model; it used to be
    fetched and dropped after building the source URL."""

    @staticmethod
    def _stamp(fake_db, jurisdiction, number):
        from app.scraper.runner import _stamp_registration
        _stamp_registration("e1", jurisdiction, number)
        assert len(fake_db.calls) == 1
        cypher, params = fake_db.calls[0]
        # fill-if-missing on EVERY field: this scraper resolves by name, so the
        # node may already carry a register's own statement of the same fact
        for field in ("registration_number", "companies_house_id", "register_id"):
            assert f"e.{field} = COALESCE(e.{field}," in cypher, field
        return params

    def test_gb_becomes_companies_house_id(self, fake_db):
        params = self._stamp(fake_db, "gb", "07524813")
        assert params["number"] == "07524813"
        assert params["ch"] == "07524813"
        assert params["rid"] is None

    def test_sole_register_country_becomes_register_id(self, fake_db):
        from app.scraper.gleif_reference import sole_register_for_country
        code = sole_register_for_country("PA")
        assert code, "Panama left the sole-register list — pick another fixture country"
        params = self._stamp(fake_db, "pa", "155692169")
        assert params["rid"] == f"{code}:155692169"
        assert params["ch"] is None

    def test_sub_national_keeps_the_number_only(self, fake_db):
        # "us_de" is Delaware's register, not a US-wide one — the ISO-2 prefix
        # cannot pick between state namespaces.
        params = self._stamp(fake_db, "us_de", "3112015")
        assert params["number"] == "3112015"
        assert params["ch"] is None and params["rid"] is None

    def test_multi_register_country_keeps_the_number_only(self, fake_db):
        params = self._stamp(fake_db, "de", "HRB12345")
        assert params["ch"] is None and params["rid"] is None

    def test_a_subdivision_of_a_sole_register_country_is_not_national(self, fake_db):
        # Hypothetical "pa_colon": a sub-national register's numbers must not be
        # stamped into the NATIONAL register's namespace — that is a false-merge
        # generator, and exactly what the sub_national guard exists to prevent.
        params = self._stamp(fake_db, "pa_colon", "155692169")
        assert params["rid"] is None and params["ch"] is None

    def test_missing_parts_write_nothing(self, fake_db):
        from app.scraper.runner import _stamp_registration
        _stamp_registration("e1", None, "123")
        _stamp_registration("e1", "de", None)
        assert fake_db.calls == []
