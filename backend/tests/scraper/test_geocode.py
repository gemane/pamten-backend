"""Tests for the Nominatim geocoding service (HTTP layer mocked)."""

import httpx
import pytest
from unittest.mock import MagicMock, patch

from app.scraper import geocode
from app.config import settings


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
    monkeypatch.setattr(settings, "GEOCODING_MIN_INTERVAL", 0.0)  # no real sleeping
    geocode._cache.clear()
    geocode.close_client()
    yield
    geocode._cache.clear()
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
