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
                 founder="http://www.wikidata.org/entity/Q317521", founderLabel="Elon Musk",
                 founderStart="2002-03-14"),
            _row(itemLabel="SpaceX",
                 chair="http://www.wikidata.org/entity/Q317521", chairLabel="Elon Musk"),
            _row(itemLabel="SpaceX",
                 board="http://www.wikidata.org/entity/Q123", boardLabel="Some Director",
                 boardStart="2015-01-01", boardEnd="2020-06-30"),
        ]
        result = _aggregate("Q1", rows)
        officers = {(o["label"], o["role"]): o for o in result["officers"]}
        assert ("Elon Musk", "Founder") in officers
        assert ("Elon Musk", "Chairman") in officers
        assert ("Some Director", "Board Member") in officers
        # Position start/end dates (P580/P582) are captured — they feed the timeline.
        assert officers[("Elon Musk", "Founder")]["since"] == "2002-03-14"
        assert officers[("Elon Musk", "Chairman")]["since"] is None
        board = officers[("Some Director", "Board Member")]
        assert board["since"] == "2015-01-01" and board["until"] == "2020-06-30"

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

    def test_retries_on_429_then_succeeds(self):
        # A rate-limit (429) must back off + retry, not fail the scrape.
        throttled = MagicMock(status_code=429, headers={})
        ok = self._mock_response([{"id": "Q1", "label": "Apple Inc."}])
        ok.status_code = 200
        with patch("httpx.get", side_effect=[throttled, ok]) as get, \
             patch("time.sleep"):
            out = search_entity("Apple")
        assert out == [{"id": "Q1", "label": "Apple Inc."}]
        assert get.call_count == 2                       # retried once after the 429

    def test_retries_on_502_gateway_then_succeeds(self):
        # A transient Bad Gateway (502) from Wikidata's proxy must back off + retry too.
        gateway = MagicMock(status_code=502, headers={})
        ok = self._mock_response([{"id": "Q1", "label": "Apple Inc."}])
        ok.status_code = 200
        with patch("httpx.get", side_effect=[gateway, ok]) as get, \
             patch("time.sleep"):
            out = search_entity("Apple")
        assert out == [{"id": "Q1", "label": "Apple Inc."}]
        assert get.call_count == 2                        # retried once after the 502

    def test_gives_up_after_max_retries(self):
        import httpx
        throttled = MagicMock(status_code=429, headers={})
        throttled.raise_for_status.side_effect = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=throttled)
        with patch("httpx.get", return_value=throttled) as get, patch("time.sleep"):
            with __import__("pytest").raises(httpx.HTTPStatusError):
                search_entity("Apple")
        assert get.call_count == 5                        # initial + _MAX_RETRIES

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
        assert "Owlgraph" in headers["User-Agent"]

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

    def test_flaky_employees_query_does_not_abort_the_scrape(self):
        import httpx
        # core/people/relations succeed; the 4th (employees) request 502s. The
        # company data must still come back — just without the employees field.
        good = self._sparql_response([{"itemLabel": {"value": "Apple Inc."}}])
        boom = httpx.HTTPError("502 Bad Gateway")
        with patch("httpx.get", side_effect=[good, good, good, boom]), \
             patch("time.sleep"):
            result = fetch_company_data("Q1")
        assert result is not None
        assert result["name"] == "Apple Inc."
        assert result["employees"] is None

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


# ── External-identifier bridge (P1278 LEI / P5531 CIK) ────────────────────────
#
# A GLEIF entity and its Wikidata counterpart share nothing a merge can key on:
# GLEIF supplies lei_id, Wikidata supplies wikidata_id, and the dedup only calls a
# group "definitive" when two members carry the SAME identifier. That is why
# Microsoft existed twice — one node with the ownership graph, one with the
# executives. SEC's own LEI field is null for most operating companies, so
# Wikidata's P1278 is the bridge that actually exists.
#
# SPARQL can't run in CI, so the mapping and the normalisation are what get
# covered here; the query itself is verified by a live re-scrape.

from app.scraper.wikidata import normalize_lei, normalize_cik  # noqa: E402


class TestNormalizeLei:
    def test_accepts_a_valid_lei(self):
        assert normalize_lei("INR2EJN1ERAN0W5ZP974") == "INR2EJN1ERAN0W5ZP974"

    def test_upper_cases_and_strips(self):
        assert normalize_lei("  inr2ejn1eran0w5zp974 ") == "INR2EJN1ERAN0W5ZP974"

    def test_rejects_wrong_length(self):
        # Wikidata is crowd-edited; a truncated code must not become a merge key.
        assert normalize_lei("INR2EJN1ERAN0W5ZP97") is None      # 19
        assert normalize_lei("INR2EJN1ERAN0W5ZP9744") is None    # 21

    def test_rejects_non_alphanumeric(self):
        assert normalize_lei("INR2EJN1ERAN-W5ZP974") is None
        assert normalize_lei("see LEI register") is None

    def test_rejects_empty(self):
        assert normalize_lei(None) is None
        assert normalize_lei("") is None
        assert normalize_lei("   ") is None


class TestNormalizeCik:
    def test_pads_to_ten_digits(self):
        # EDGAR always stores the padded form; an unpadded value would silently
        # fail to match a SEC-sourced node.
        assert normalize_cik("789019") == "0000789019"

    def test_leaves_an_already_padded_value_alone(self):
        assert normalize_cik("0000789019") == "0000789019"

    def test_rejects_non_numeric(self):
        assert normalize_cik("CIK789019") is None
        assert normalize_cik("n/a") is None

    def test_rejects_overlong(self):
        assert normalize_cik("12345678901") is None

    def test_rejects_empty(self):
        assert normalize_cik(None) is None
        assert normalize_cik("") is None


class TestIdentifierAggregation:
    def test_extracts_lei_and_cik(self):
        row = _row(itemLabel="Microsoft", lei="INR2EJN1ERAN0W5ZP974", cik="0000789019")
        result = _aggregate("Q2283", [row])
        assert result["lei"] == "INR2EJN1ERAN0W5ZP974"
        assert result["sec_cik"] == "0000789019"

    def test_identifiers_default_to_none(self):
        result = _aggregate("Q1", [APPLE_ROW])
        assert result["lei"] is None
        assert result["sec_cik"] is None

    def test_reads_identifiers_from_a_later_row(self):
        # They are OPTIONAL joins, so the first row can carry the label while a
        # later one carries the identifier. Reading them only in the set-once
        # name block would drop them.
        rows = [_row(itemLabel="Microsoft"),
                _row(lei="INR2EJN1ERAN0W5ZP974", cik="789019")]
        result = _aggregate("Q2283", rows)
        assert result["lei"] == "INR2EJN1ERAN0W5ZP974"
        assert result["sec_cik"] == "0000789019"

    def test_a_malformed_identifier_is_dropped_not_stored(self):
        rows = [_row(itemLabel="Dodgy Co", lei="NOT-A-LEI", cik="abc")]
        result = _aggregate("Q9", rows)
        assert result["lei"] is None
        assert result["sec_cik"] is None

    def test_the_core_query_requests_both_properties(self):
        # Guards the SPARQL itself, which no test can execute.
        from app.scraper.wikidata import _sparql
        import inspect
        src = inspect.getsource(_sparql)
        assert "wdt:P1278" in src, "LEI property missing from the core query"
        assert "wdt:P5531" in src, "CIK property missing from the core query"


class TestNormalizeUrl:
    """This string becomes an <a href> in the panel — crowd-edited values get
    no trust, and rejection beats repair (a guessed scheme asserts something
    the source did not say)."""

    def test_plain_http_and_https_pass(self):
        from app.scraper.wikidata import normalize_url
        assert normalize_url("https://www.apple.com/") == "https://www.apple.com/"
        assert normalize_url("http://example.test") == "http://example.test"

    def test_whitespace_is_stripped_but_inner_spaces_reject(self):
        from app.scraper.wikidata import normalize_url
        assert normalize_url("  https://a.test  ") == "https://a.test"
        assert normalize_url("https://a b.test") is None

    def test_everything_else_is_rejected_not_repaired(self):
        from app.scraper.wikidata import normalize_url
        assert normalize_url("javascript:alert(1)") is None
        assert normalize_url("apple.com") is None            # no scheme guessing
        assert normalize_url("ftp://files.test") is None
        assert normalize_url("") is None
        assert normalize_url(None) is None


class TestWebsiteAggregation:
    def test_p856_flows_through_normalised(self):
        row = {**APPLE_ROW, "website": {"value": "https://www.apple.com/"}}
        assert _aggregate("Q1", [row])["website"] == "https://www.apple.com/"

    def test_a_junk_p856_yields_none_not_a_link(self):
        row = {**APPLE_ROW, "website": {"value": "javascript:alert(1)"}}
        assert _aggregate("Q1", [row])["website"] is None

    def test_absent_p856_is_none(self):
        assert _aggregate("Q1", [APPLE_ROW])["website"] is None
