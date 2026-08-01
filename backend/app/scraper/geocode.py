"""
Geocoding via Nominatim (OpenStreetMap).

Turns a street/city/country address into (latitude, longitude). Best-effort:
returns None on any problem (disabled, no match, network/parse error) so a
caller never has to guard it.

Nominatim usage policy (https://operations.osmfoundation.org/policies/nominatim/)
is respected: a descriptive User-Agent with a contact, at most one request per
second (GEOCODING_MIN_INTERVAL), and results are cached so the same address is
never requested twice in a process. Persisted coordinates on Location nodes act
as the durable cache, so backfills only ever geocode what is still missing.
"""
import logging
import threading
import time

import httpx

from app.config import settings

log = logging.getLogger(__name__)

Coord = tuple[float, float]  # (latitude, longitude)

_client: httpx.Client | None = None
_client_lock = threading.Lock()
_last_request = 0.0
_rate_lock = threading.Lock()
_cache: dict[tuple, Coord | None] = {}


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                contact = f" ({settings.GEOCODING_CONTACT})" if settings.GEOCODING_CONTACT else ""
                _client = httpx.Client(
                    timeout=httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0),
                    headers={"User-Agent": f"{settings.GEOCODING_USER_AGENT}{contact}"},
                )
    return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def _throttle() -> None:
    """Block until at least GEOCODING_MIN_INTERVAL has passed since the last call."""
    global _last_request
    with _rate_lock:
        wait = settings.GEOCODING_MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def geocode_address(address: dict) -> Coord | None:
    """
    Geocode a {street, city, state, zip, country} address to (lat, lng).

    Returns None when geocoding is disabled, the address is too sparse to be
    meaningful, or no match/an error occurs.
    """
    if not settings.GEOCODING_ENABLED:
        return None

    params = {
        "street":     (address.get("street") or "").strip(),
        "city":       (address.get("city") or "").strip(),
        "state":      (address.get("state") or "").strip(),
        "postalcode": (address.get("zip") or "").strip(),
        "country":    (address.get("country") or "").strip(),
    }
    params = {k: v for k, v in params.items() if v}
    # Need at least a city or country to have any chance of a useful result.
    if not (params.get("city") or params.get("country")):
        return None

    key = tuple(sorted(params.items()))
    if key in _cache:
        return _cache[key]

    result = _query({**params, "format": "json", "limit": "1"})
    _cache[key] = result
    return result


# Nominatim place_rank at/above which a match is a specific address (street/building),
# not just a locality — used to flag whether a full-address geocode is exact.
_STREET_LEVEL_RANK = 26
_full_cache: dict[str, tuple[Coord, str] | None] = {}


def geocode_full(query: str) -> tuple[Coord, str] | None:
    """Free-text geocode a FULL address → ((lat, lng), precision) where precision is
    'exact' for a street/building-level match or 'approx' for a coarser (town+) one.
    None when disabled, empty, or no match — the caller then falls back to city geocoding."""
    if not settings.GEOCODING_ENABLED or not (query or "").strip():
        return None
    q = query.strip()
    if q in _full_cache:
        return _full_cache[q]
    _throttle()
    result: tuple[Coord, str] | None = None
    try:
        resp = _get_client().get(settings.NOMINATIM_URL,
                                 params={"q": q, "format": "json", "limit": "1"})
        resp.raise_for_status()
        data = resp.json()
        if data:
            coord = (float(data[0]["lat"]), float(data[0]["lon"]))
            rank = int(data[0].get("place_rank") or 0)
            result = (coord, "exact" if rank >= _STREET_LEVEL_RANK else "approx")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("Geocoding (full) failed (%s): %s", q, exc)
    _full_cache[q] = result
    return result


def _query(params: dict) -> Coord | None:
    _throttle()
    try:
        resp = _get_client().get(settings.NOMINATIM_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Geocoding request failed (%s): %s", params, exc)
        return None

    if not data:
        return None
    try:
        return (float(data[0]["lat"]), float(data[0]["lon"]))
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        log.warning("Geocoding response unparseable (%s): %s", params, exc)
        return None
