"""
Tests for wikidata.py — SPARQL aggregation and HTTP helpers.

Strategy: test the pure aggregation functions directly (no mocking needed),
and mock httpx for the HTTP-calling functions.
"""

import pytest
from unittest.mock import patch, MagicMock

from app.scraper.wikidata import _v, _qid, _parse_point, _aggregate, search_entity, fetch_company_data


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _row(**kwargs) -> dict:
    """Build a minimal SPARQL result row with typed literals/URIs."""
    return {k: {"value": v} for k, v in kwargs.items() if v is not None}


APPLE_ROW = _row(
    itemLabel="Apple Inc.",
    itemDescription="American technology company",
    instance="http://www.wikidata.org/entity/Q4830453",
    countryCode="US",
    founded="1976-04-01T00:00:00Z",
    revenue="394328000000",
    subsidiary="http://www.wikidata.org/entity/Q312",
    subsidiaryLabel="Apple Records",
    subsidiaryInstance="http://www.wikidata.org/entity/Q4830453",
    ceo="http://www.wikidata.org/entity/Q88",
    ceoLabel="Tim Cook",
    ceoDescription="American business executive",
    ceoNationalityCode="US",
    ceoStart="2011-08-24",
)


# ── _v ────────────────────────────────────────────────────────────────────────

class TestV:
    def test_returns_value_when_key_present(self):
        row = {"name": {"value": "Apple"}}
        assert _v(row, "name") == "Apple"

    def test_returns_none_when_key_missing(self):
        assert _v({}, "missing") is None

    def test_returns_none_when_value_key_absent(self):
        assert _v({"name": {}}, "name") is None


# ── _qid ─────────────────────────────────────────────────────────────────────

class TestQid:
    def test_extracts_qid_from_full_uri(self):
        assert _qid("http://www.wikidata.org/entity/Q312") == "Q312"

    def test_returns_none_for_none_input(self):
        assert _qid(None) is None

    def test_handles_trailing_slash(self):
        assert _qid("http://www.wikidata.org/entity/Q312/") == "Q312"

    def test_returns_bare_qid_unchanged(self):
        assert _qid("Q999") == "Q999"


# ── _parse_point ───────────────────────────────────────────────────────────────

class TestParsePoint:
    def test_parses_point_swapping_lon_lat(self):
        # WKT is Point(longitude latitude); we return (lat, lng)
        assert _parse_point("Point(-122.03 37.33)") == (37.33, -122.03)

    def test_parses_positive_and_integer_coords(self):
        assert _parse_point("Point(13 52)") == (52.0, 13.0)

    def test_returns_none_for_none(self):
        assert _parse_point(None) is None

    def test_returns_none_for_garbage(self):
        assert _parse_point("somewhere") is None


# ── _aggregate ────────────────────────────────────────────────────────────────

class TestAggregate:
    def test_returns_none_for_empty_rows(self):
        assert _aggregate("Q1", []) is None

    def test_extracts_basic_fields(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert result["name"] == "Apple Inc."
        assert result["description"] == "American technology company"
        assert result["country"] == "US"
        assert result["qid"] == "Q1"

    def test_parses_founded_year(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert result["founded"] == 1976

    def test_parses_revenue_as_float(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert result["revenue"] == pytest.approx(394328000000.0)

    def test_extracts_instance_qids(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert "Q4830453" in result["instances"]

    def test_extracts_subsidiary(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert len(result["subsidiaries"]) == 1
        sub = result["subsidiaries"][0]
        assert sub["qid"] == "Q312"
        assert sub["name"] == "Apple Records"

    def test_extracts_ceo(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert len(result["ceos"]) == 1
        ceo = result["ceos"][0]
        assert ceo["qid"] == "Q88"
        assert ceo["label"] == "Tim Cook"
        assert ceo["nationality"] == "US"
        assert ceo["since"] == "2011-08-24"
        assert ceo["until"] is None

    def test_deduplicates_subsidiaries_across_rows(self):
        rows = [APPLE_ROW, APPLE_ROW]  # same subsidiary in two rows
        result = _aggregate("Q1", rows)
        assert len(result["subsidiaries"]) == 1

    def test_deduplicates_ceos_by_qid_and_since(self):
        rows = [APPLE_ROW, APPLE_ROW]
        result = _aggregate("Q1", rows)
        assert len(result["ceos"]) == 1

    def test_multiple_ceo_tenures_are_kept_separately(self):
        cook = _row(
            itemLabel="Apple Inc.", ceo="http://www.wikidata.org/entity/Q88",
            ceoLabel="Tim Cook", ceoStart="2011-08-24",
        )
        jobs = _row(
            itemLabel="Apple Inc.", ceo="http://www.wikidata.org/entity/Q19837",
            ceoLabel="Steve Jobs", ceoStart="1997-09-16", ceoEnd="2011-08-24",
        )
        result = _aggregate("Q1", [cook, jobs])
        assert len(result["ceos"]) == 2

    def test_multiple_parents_collected(self):
        row1 = _row(itemLabel="Sub", parent="http://www.wikidata.org/entity/Q1")
        row2 = _row(itemLabel="Sub", parent="http://www.wikidata.org/entity/Q2")
        result = _aggregate("Q99", [row1, row2])
        assert set(result["parents"]) == {"Q1", "Q2"}

    def test_returns_lists_not_sets(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert isinstance(result["instances"], list)
        assert isinstance(result["subsidiaries"], list)
        assert isinstance(result["parents"], list)
        assert isinstance(result["ceos"], list)

    def test_malformed_founded_date_leaves_founded_none(self):
        row = _row(itemLabel="X", founded="not-a-date")
        result = _aggregate("Q1", [row])
        assert result["founded"] is None

    def test_malformed_revenue_leaves_revenue_none(self):
        row = _row(itemLabel="X", revenue="N/A")
        result = _aggregate("Q1", [row])
        assert result["revenue"] is None

    def test_basic_fields_set_only_on_first_row(self):
        row1 = _row(itemLabel="First Name", countryCode="US")
        row2 = _row(itemLabel="Second Name", countryCode="DE")
        result = _aggregate("Q1", [row1, row2])
        assert result["name"] == "First Name"
        assert result["country"] == "US"

    def test_extracts_hq_coordinates_city_and_country(self):
        row = _row(
            itemLabel="Apple Inc.",
            hqCoord="Point(-122.0312 37.3318)",
            hqLabel="Cupertino",
            hqCountryCode="US",
        )
        result = _aggregate("Q1", [row])
        assert result["hq_lat"] == pytest.approx(37.3318)
        assert result["hq_lng"] == pytest.approx(-122.0312)
        assert result["hq_city"] == "Cupertino"
        assert result["hq_country"] == "US"

    def test_falls_back_to_item_coordinate_when_no_hq(self):
        row = _row(itemLabel="X", itemCoord="Point(13.4 52.5)", countryCode="DE")
        result = _aggregate("Q1", [row])
        assert result["hq_lat"] == pytest.approx(52.5)
        assert result["hq_lng"] == pytest.approx(13.4)
        assert result["hq_country"] == "DE"  # falls back to item country

    def test_hq_prefers_hq_coord_over_item_coord(self):
        row = _row(
            itemLabel="X",
            itemCoord="Point(0 0)",
            hqCoord="Point(2 48)",
            hqLabel="Paris",
        )
        result = _aggregate("Q1", [row])
        assert (result["hq_lat"], result["hq_lng"]) == (48.0, 2.0)

    def test_no_coordinates_leaves_hq_none(self):
        result = _aggregate("Q1", [_row(itemLabel="X")])
        assert result["hq_lat"] is None
        assert result["hq_city"] is None

    def test_subsidiary_instances_accumulated_across_rows(self):
        row1 = _row(
            itemLabel="Parent",
            subsidiary="http://www.wikidata.org/entity/Q312",
            subsidiaryLabel="Sub",
            subsidiaryInstance="http://www.wikidata.org/entity/Q4830453",
        )
        row2 = _row(
            itemLabel="Parent",
            subsidiary="http://www.wikidata.org/entity/Q312",
            subsidiaryLabel="Sub",
            subsidiaryInstance="http://www.wikidata.org/entity/Q783794",
        )
        result = _aggregate("Q1", [row1, row2])
        assert len(result["subsidiaries"]) == 1
        assert len(result["subsidiaries"][0]["instances"]) == 2


# ── search_entity ─────────────────────────────────────────────────────────────

class TestSearchEntity:
    def _mock_response(self, results: list) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"search": results}
        return resp

    def test_returns_search_results(self):
        results = [{"id": "Q1", "label": "Apple Inc.", "description": "tech co"}]
        with patch("httpx.get", return_value=self._mock_response(results)), \
             patch("time.sleep"):
            out = search_entity("Apple")
        assert out == results

    def test_returns_empty_list_when_no_results(self):
        with patch("httpx.get", return_value=self._mock_response([])), \
             patch("time.sleep"):
            out = search_entity("zzznomatch")
        assert out == []

    def test_passes_query_and_language_params(self):
        with patch("httpx.get", return_value=self._mock_response([])) as mock_get, \
             patch("time.sleep"):
            search_entity("Tesla", limit=3)
        params = mock_get.call_args.kwargs["params"]
        assert params["search"] == "Tesla"
        assert params["language"] == "en"
        assert params["limit"] == 3

    def test_sends_user_agent_header(self):
        with patch("httpx.get", return_value=self._mock_response([])) as mock_get, \
             patch("time.sleep"):
            search_entity("x")
        headers = mock_get.call_args.kwargs["headers"]
        assert "User-Agent" in headers
        assert "Pamten" in headers["User-Agent"]

    def test_sleeps_after_request(self):
        with patch("httpx.get", return_value=self._mock_response([])), \
             patch("time.sleep") as mock_sleep:
            search_entity("x")
        mock_sleep.assert_called_once()


# ── fetch_company_data ────────────────────────────────────────────────────────

class TestFetchCompanyData:
    def _sparql_response(self, bindings: list) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"results": {"bindings": bindings}}
        return resp

    def test_returns_none_when_no_bindings(self):
        with patch("httpx.get", return_value=self._sparql_response([])), \
             patch("time.sleep"):
            result = fetch_company_data("Q9999")
        assert result is None

    def test_returns_aggregated_dict_on_match(self):
        row = {"itemLabel": {"value": "Apple Inc."}}
        with patch("httpx.get", return_value=self._sparql_response([row])), \
             patch("time.sleep"):
            result = fetch_company_data("Q1")
        assert result is not None
        assert result["name"] == "Apple Inc."
        assert result["qid"] == "Q1"

    def test_sends_format_json_param(self):
        with patch("httpx.get", return_value=self._sparql_response([])) as mock_get, \
             patch("time.sleep"):
            fetch_company_data("Q1")
        params = mock_get.call_args.kwargs["params"]
        assert params["format"] == "json"
        assert "query" in params

    def test_qid_is_embedded_in_sparql_query(self):
        with patch("httpx.get", return_value=self._sparql_response([])) as mock_get, \
             patch("time.sleep"):
            fetch_company_data("Q380")
        query = mock_get.call_args.kwargs["params"]["query"]
        assert "Q380" in query

    def test_sleeps_before_request(self):
        with patch("httpx.get", return_value=self._sparql_response([])), \
             patch("time.sleep") as mock_sleep:
            fetch_company_data("Q1")
        mock_sleep.assert_called_once()
