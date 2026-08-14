"""Tests for the Nominatim geocoding service (HTTP layer mocked)."""

import httpx
import pytest
from unittest.mock import MagicMock, patch

from app.scraper import geocode
from app.config import settings


@pytest.fixture(autouse=True)
def _no_geocache(monkeypatch):
    """Take the durable cache out of these tests.

    They exercise the HTTP path with no database behind them, and `lookup` now
    lets a ConnectionError through on purpose — if the database is unreachable
    the answer has nowhere to go, so spending a Nominatim request would be
    waste. Stubbing it here keeps that production behaviour intact while these
    tests stay about the request and the parsing.
    """
    from app.scraper import geo_cache
    monkeypatch.setattr(geo_cache, "lookup", lambda q: None)
    monkeypatch.setattr(geo_cache, "store", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
    monkeypatch.setattr(settings, "GEOCODING_MIN_INTERVAL", 0.0)  # no real sleeping
    geocode._cache.clear()
    geocode._full_cache.clear()
    geocode.close_client()
    yield
    geocode._cache.clear()
    geocode._full_cache.clear()
    geocode.close_client()


def _resp(payload, status=200):
    r = MagicMock(status_code=status)
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _client(resp=None, exc=None):
    c = MagicMock()
    if exc is not None:
        c.get.side_effect = exc
    else:
        c.get.return_value = resp
    return c


ADDR = {"street": "1 Infinite Loop", "city": "Cupertino", "country": "US"}


def test_returns_lat_lng_on_match():
    c = _client(_resp([{"lat": "37.3318", "lon": "-122.0312"}]))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_address(ADDR) == (37.3318, -122.0312)


def test_geocode_full_reports_exact_for_street_level():
    # place_rank >= 26 → a street/building match ⇒ 'exact'
    c = _client(_resp([{"lat": "51.9", "lon": "-2.07", "place_rank": 30}]))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_full("1 Test St, Cheltenham, GL51 0TJ, GB") == ((51.9, -2.07), "exact")


def test_geocode_full_reports_approx_for_coarse_match():
    # a town/locality-level match (low place_rank) ⇒ 'approx'
    c = _client(_resp([{"lat": "51.9", "lon": "-2.07", "place_rank": 16}]))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_full("Cheltenham, GB") == ((51.9, -2.07), "approx")


def test_geocode_full_none_on_no_match_or_empty():
    c = _client(_resp([]))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_full("nowhere at all") is None
    assert geocode.geocode_full("") is None
    assert geocode.geocode_full(None) is None


def test_disabled_returns_none_without_calling_out(monkeypatch):
    monkeypatch.setattr(settings, "GEOCODING_ENABLED", False)
    c = _client(_resp([{"lat": "1", "lon": "2"}]))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_address(ADDR) is None
    c.get.assert_not_called()


def test_sparse_address_is_not_queried():
    c = _client(_resp([]))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_address({"street": "somewhere"}) is None  # no city/country
    c.get.assert_not_called()


def test_no_match_returns_none():
    c = _client(_resp([]))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_address(ADDR) is None


def test_network_error_returns_none():
    c = _client(exc=httpx.ConnectError("boom"))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_address(ADDR) is None


def test_unparseable_response_returns_none():
    c = _client(_resp([{"nope": 1}]))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_address(ADDR) is None


def test_result_is_cached_second_call_hits_no_http():
    c = _client(_resp([{"lat": "1.5", "lon": "2.5"}]))
    with patch.object(geocode, "_get_client", return_value=c):
        assert geocode.geocode_address(ADDR) == (1.5, 2.5)
        assert geocode.geocode_address(ADDR) == (1.5, 2.5)
    assert c.get.call_count == 1  # second call served from cache


def test_structured_query_params_and_user_agent_are_sent():
    c = _client(_resp([{"lat": "1", "lon": "2"}]))
    with patch.object(geocode, "_get_client", return_value=c):
        geocode.geocode_address(ADDR)
    _, kwargs = c.get.call_args
    params = kwargs["params"]
    assert params["city"] == "Cupertino"
    assert params["country"] == "US"
    assert params["format"] == "json" and params["limit"] == "1"
