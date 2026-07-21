"""
Tests for wikidata.py — SPARQL aggregation and HTTP helpers.

Strategy: test the pure aggregation functions directly (no mocking needed),
and mock httpx for the HTTP-calling functions.
"""

import pytest
from unittest.mock import patch, MagicMock

from app.scraper.wikidata import (
    _v, _qid, _parse_point, _aggregate, _fetch_person_details,
    search_entity, fetch_company_data,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _row(**kwargs) -> dict:
    """Build a minimal SPARQL result row with typed literals/URIs."""
    return {k: {"value": v} for k, v in kwargs.items() if v is not None}


APPLE_ROW = _row(
    itemLabel="Apple Inc.",
    itemDescription="American technology company",
    altLabel="Apple",
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

    def test_parses_employees_and_as_of_year(self):
        # employees come from a separate query row (no itemLabel)
        emp_row = _row(employees="164000", employeesAsOf="2022-01-01T00:00:00Z")
        result = _aggregate("Q1", [APPLE_ROW, emp_row])
        assert result["employees"] == 164000
        assert result["employees_as_of"] == 2022

    def test_employees_none_when_absent(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert result["employees"] is None
        assert result["employees_as_of"] is None

    def test_employees_without_as_of_qualifier(self):
        result = _aggregate("Q1", [APPLE_ROW, _row(employees="5000")])
        assert result["employees"] == 5000
        assert result["employees_as_of"] is None

    def test_extracts_instance_qids(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert "Q4830453" in result["instances"]

    def test_extracts_subsidiary(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert len(result["subsidiaries"]) == 1
        sub = result["subsidiaries"][0]
        assert sub["qid"] == "Q312"
        assert sub["name"] == "Apple Records"

    def test_dual_listed_company_multiple_countries_and_hqs(self):
        # Unilever-style: two domiciles (UK + NL) and two HQs.
        rows = [
            _row(itemLabel="Unilever", countryCode="GB",
                 hqLabel="London", hqCountryCode="GB", hqCoord="Point(-0.12 51.5)"),
            _row(itemLabel="Unilever", countryCode="NL",
                 hqLabel="Rotterdam", hqCountryCode="NL", hqCoord="Point(4.48 51.92)"),
        ]
        r = _aggregate("Q1", rows)
        assert r["country"] == "GB"                       # primary
        assert r["countries"] == ["GB", "NL"]             # both domiciles, primary first
        # Primary HQ's city and country agree (no "Rotterdam, GB" mismatch)
        assert (r["hq_city"], r["hq_country"]) == ("London", "GB")
        assert set(r["hq_locations"]) == {"London|GB", "Rotterdam|NL"}

    def test_primary_hq_prefers_one_with_a_resolved_country(self):
        # Unilever's real case: an HQ that's an office building (coords but no
        # country) must NOT become the primary and inherit a mismatched country.
        rows = [
            _row(itemLabel="Unilever", countryCode="GB",
                 hqLabel="Rotterdam", hqCoord="Point(4.48 51.92)"),   # coords, no country
            _row(itemLabel="Unilever", countryCode="NL",
                 hqLabel="London", hqCountryCode="GB", hqCoord="Point(-0.12 51.5)"),
        ]
        r = _aggregate("Q1", rows)
        assert (r["hq_city"], r["hq_country"]) == ("London", "GB")  # never "Rotterdam, GB"

    def test_single_country_company_has_singleton_countries_list(self):
        r = _aggregate("Q1", [APPLE_ROW])
        assert r["country"] == "US"
        assert r["countries"] == ["US"]

    def test_hq_country_never_falls_back_to_a_mismatched_domicile(self):
        # HQ in NL but company domiciled in GB → hq_country must be NL, not GB.
        rows = [_row(itemLabel="X", countryCode="GB",
                     hqLabel="Rotterdam", hqCountryCode="NL", hqCoord="Point(4.48 51.92)")]
        r = _aggregate("Q1", rows)
        assert r["hq_city"] == "Rotterdam"
        assert r["hq_country"] == "NL"

    def test_extracts_ceo(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert len(result["ceos"]) == 1
        ceo = result["ceos"][0]
        assert ceo["qid"] == "Q88"
        assert ceo["label"] == "Tim Cook"
        assert ceo["nationality"] == "US"
        assert ceo["since"] == "2011-08-24"
        assert ceo["until"] is None

    def test_extracts_founder_chair_board_as_officers(self):
        rows = [
            _row(itemLabel="SpaceX",
                 founder="http://www.wikidata.org/entity/Q317521", founderLabel="Elon Musk"),
            _row(itemLabel="SpaceX",
                 chair="http://www.wikidata.org/entity/Q317521", chairLabel="Elon Musk"),
            _row(itemLabel="SpaceX",
                 board="http://www.wikidata.org/entity/Q123", boardLabel="Some Director"),
        ]
        result = _aggregate("Q1", rows)
        officers = {(o["label"], o["role"]) for o in result["officers"]}
        assert ("Elon Musk", "Founder") in officers
        assert ("Elon Musk", "Chairman") in officers
        assert ("Some Director", "Board Member") in officers

    def test_extracts_owned_by_with_instances(self):
        rows = [_row(
            itemLabel="SpaceX",
            owner="http://www.wikidata.org/entity/Q317521", ownerLabel="Elon Musk",
            ownerInstance="http://www.wikidata.org/entity/Q5",  # human
        )]
        result = _aggregate("Q1", rows)
        assert len(result["owners"]) == 1
        owner = result["owners"][0]
        assert owner["qid"] == "Q317521"
        assert owner["label"] == "Elon Musk"
        assert "Q5" in owner["instances"]

    def test_officers_and_owners_empty_when_absent(self):
        result = _aggregate("Q1", [APPLE_ROW])  # APPLE_ROW has no founder/owner
        assert result["officers"] == []
        assert result["owners"] == []

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
        assert isinstance(result["aliases"], list)
        assert isinstance(result["instances"], list)
        assert isinstance(result["subsidiaries"], list)
        assert isinstance(result["parents"], list)
        assert isinstance(result["ceos"], list)

    def test_collects_aliases(self):
        row1 = _row(itemLabel="Apple Inc.", altLabel="Apple")
        row2 = _row(itemLabel="Apple Inc.", altLabel="AAPL")
        result = _aggregate("Q1", [row1, row2])
        assert set(result["aliases"]) == {"Apple", "AAPL"}

    def test_deduplicates_aliases(self):
        rows = [_row(itemLabel="X", altLabel="Foo"), _row(itemLabel="X", altLabel="Foo")]
        result = _aggregate("Q1", rows)
        assert result["aliases"].count("Foo") == 1

    def test_no_aliases_returns_empty_list(self):
        result = _aggregate("Q1", [_row(itemLabel="X")])
        assert result["aliases"] == []

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
        # One polite sleep before each targeted query (core, people, relations,
        # employees).
        with patch("httpx.get", return_value=self._sparql_response([])), \
             patch("time.sleep") as mock_sleep:
            fetch_company_data("Q1")
        assert mock_sleep.call_count == 4
        mock_sleep.assert_called_with(0.4)


# ── _fetch_person_details ─────────────────────────────────────────────────────

class TestFetchPersonDetails:
    def _resp(self, bindings: list) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"results": {"bindings": bindings}}
        return resp

    def test_empty_qids_makes_no_request(self):
        with patch("httpx.get") as mock_get:
            out = _fetch_person_details(set())
        assert out == {}
        mock_get.assert_not_called()

    def test_parses_birth_death_nationalities_and_aliases(self):
        rows = [{
            "person":     {"value": "http://www.wikidata.org/entity/Q317521"},
            "birth":      {"value": "1971-06-28T00:00:00Z"},
            "death":      {"value": ""},
            "birthPlace": {"value": "Pretoria"},
            "nats":       {"value": "US|CA|ZA"},
            "aliases":    {"value": "Elon|Technoking"},
            "instances":  {"value": "http://www.wikidata.org/entity/Q5"},  # human
        }]
        with patch("httpx.get", return_value=self._resp(rows)), patch("time.sleep"):
            out = _fetch_person_details({"Q317521"})
        d = out["Q317521"]
        assert d["birth_date"] == "1971-06-28"       # timestamp truncated to date
        assert d["death_date"] is None                # empty string → None
        assert d["birth_place"] == "Pretoria"
        assert d["nationalities"] == ["US", "CA", "ZA"]
        assert d["aliases"] == ["Elon", "Technoking"]
        assert d["is_human"] is True                  # instance-of Q5

    def test_non_human_flagged(self):
        # a company (P31 present, no Q5) wrongly appearing in a person slot
        rows = [{"person":    {"value": "http://www.wikidata.org/entity/Q312"},
                 "instances": {"value": "http://www.wikidata.org/entity/Q4830453"}}]
        with patch("httpx.get", return_value=self._resp(rows)), patch("time.sleep"):
            out = _fetch_person_details({"Q312"})
        assert out["Q312"]["is_human"] is False

    def test_person_with_no_detail_yields_unknown_human(self):
        rows = [{"person": {"value": "http://www.wikidata.org/entity/Q1"}}]
        with patch("httpx.get", return_value=self._resp(rows)), patch("time.sleep"):
            out = _fetch_person_details({"Q1"})
        assert out["Q1"] == {
            "birth_date": None, "death_date": None, "birth_place": None,
            "nationalities": [], "aliases": [], "is_human": None,   # no P31 → unknown
        }

    def test_query_includes_place_of_birth_and_instance(self):
        with patch("httpx.get", return_value=self._resp([])) as mock_get, \
             patch("time.sleep"):
            _fetch_person_details({"Q42"})
        query = mock_get.call_args.kwargs["params"]["query"]
        assert "wdt:P19" in query and "birthPlace" in query
        assert "wdt:P31" in query and "instances" in query

    def test_embeds_all_qids_as_values(self):
        with patch("httpx.get", return_value=self._resp([])) as mock_get, \
             patch("time.sleep"):
            _fetch_person_details({"Q42", "Q88"})
        query = mock_get.call_args.kwargs["params"]["query"]
        assert "wd:Q42" in query and "wd:Q88" in query


# ── fetch_company_data person enrichment ──────────────────────────────────────

class TestFetchCompanyDataEnrichesPeople:
    def test_ceo_founder_owner_get_person_detail_merged(self):
        rows = [_row(
            itemLabel="Tesla, Inc.",
            ceo="http://www.wikidata.org/entity/Q317521",
            ceoLabel="Elon Musk",
            founder="http://www.wikidata.org/entity/Q317521",
            founderLabel="Elon Musk",
        )]
        detail = {"Q317521": {
            "birth_date": "1971-06-28", "death_date": None,
            "nationalities": ["US", "CA"], "aliases": ["Elon"],
        }}
        with patch("app.scraper.wikidata._sparql", return_value=rows), \
             patch("app.scraper.wikidata._fetch_person_details", return_value=detail) as fp:
            result = fetch_company_data("Q478214")

        # the person qid was passed to the detail fetch
        assert "Q317521" in fp.call_args.args[0]
        ceo = result["ceos"][0]
        assert ceo["birth_date"] == "1971-06-28"
        assert ceo["nationalities"] == ["US", "CA"]
        assert ceo["aliases"] == ["Elon"]
        founder = result["officers"][0]
        assert founder["birth_date"] == "1971-06-28"

    def test_no_people_skips_detail_fetch(self):
        rows = [_row(itemLabel="Widget Co")]
        with patch("app.scraper.wikidata._sparql", return_value=rows), \
             patch("app.scraper.wikidata._fetch_person_details") as fp:
            result = fetch_company_data("Q1")
        # empty person set → helper returns {} without an HTTP call; still called
        # with an empty set, or skipped — either way no enrichment error.
        assert result["ceos"] == []
        if fp.called:
            assert fp.call_args.args[0] == set()
