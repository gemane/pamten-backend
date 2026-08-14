"""The durable address → coordinate cache.

Its job is to stop the same address being asked about twice, on a service that
allows one request a second. 36% of the registered addresses in the dev graph
are duplicates — 24 companies share one registered agent's building — so the
cache is what makes the cost proportional to distinct addresses.

Two behaviours carry the weight, and both are about *not* asking again: a cached
miss is a real answer (230 dev addresses resolve to nothing), and the cache must
never be able to break geocoding by being unavailable.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.scraper import geo_cache


@pytest.fixture
def store():
    """An in-memory stand-in for the GeoCache rows."""
    rows: list[dict] = []

    def fake_query(sql, params=None):
        if "count(g)" in sql:
            if "lat IS NOT NULL" in sql:
                return [{"n": len([r for r in rows if r.get("lat") is not None])}]
            return [{"n": len(rows)}]
        return [r for r in rows if r["query"] == (params or {}).get("q")][:1]

    def fake_command(sql, params=None):
        p = params or {}
        if sql.lstrip().startswith("CREATE"):
            rows.append(dict(p, query=p["q"], lat=p.get("lat"), lng=p.get("lng"),
                             precision=p.get("prec"), checked_at=p.get("now")))
        else:
            for r in rows:
                if r["query"] == p["q"]:
                    r.update(lat=p.get("lat"), lng=p.get("lng"),
                             precision=p.get("prec"), checked_at=p.get("now"))
        return []

    with patch.object(geo_cache, "run_query", side_effect=fake_query), \
         patch.object(geo_cache, "run_command", side_effect=fake_command):
        yield rows


class TestHits:
    def test_an_address_it_has_never_seen_is_not_cached(self):
        with patch.object(geo_cache, "run_query", return_value=[]):
            assert geo_cache.lookup("1 High St, London") is None

    def test_a_stored_coordinate_comes_back(self, store):
        geo_cache.store("1 High St, London", (51.5, -0.1), "exact")
        assert geo_cache.lookup("1 High St, London") == ((51.5, -0.1), "exact")

    def test_storing_twice_updates_rather_than_duplicates(self, store):
        geo_cache.store("1 High St", (51.5, -0.1), "approx")
        geo_cache.store("1 High St", (51.6, -0.2), "exact")
        assert len(store) == 1
        assert geo_cache.lookup("1 High St") == ((51.6, -0.2), "exact")


class TestMisses:
    def test_a_cached_miss_is_an_answer_not_a_gap(self, store):
        """The distinction the whole module turns on. `None` means "never asked";
        `(None, None)` means "asked, and OpenStreetMap does not have it" — and the
        caller must not spend a request re-asking."""
        geo_cache.store("Nowhere At All", None, None)
        assert geo_cache.lookup("Nowhere At All") == (None, None)

    def test_a_stale_miss_is_retried(self, store):
        # OpenStreetMap gains addresses continually, so a permanent "no" would be
        # a lie. Past the TTL the answer reverts to "never asked".
        geo_cache.store("Newly Mapped Place", None, None)
        old = (datetime.now(timezone.utc)
               - timedelta(days=geo_cache._MISS_TTL_DAYS + 1)).isoformat()
        store[0]["checked_at"] = old
        assert geo_cache.lookup("Newly Mapped Place") is None

    def test_an_unparseable_timestamp_is_treated_as_stale(self, store):
        geo_cache.store("Odd Row", None, None)
        store[0]["checked_at"] = "not a date"
        assert geo_cache.lookup("Odd Row") is None


class TestAnUnreachableDatabase:
    """It is NOT treated as "not cached", which was the first design and was wrong.

    If the database is unreachable the caller cannot store the answer either — not
    on the entity, not in the cache — so carrying on would spend a request from a
    rate-limited free service to produce a coordinate that goes nowhere, and would
    hide the outage behind a slow, silently useless run.
    """

    def test_a_read_propagates(self):
        with patch.object(geo_cache, "run_query",
                          side_effect=ConnectionError("ArcadeDB unreachable")):
            with pytest.raises(ConnectionError):
                geo_cache.lookup("1 High St")

    def test_a_write_propagates(self):
        with patch.object(geo_cache, "run_command",
                          side_effect=ConnectionError("ArcadeDB unreachable")):
            with pytest.raises(ConnectionError):
                geo_cache.store("1 High St", (51.5, -0.1), "exact")

    def test_no_request_is_spent_when_the_database_is_down(self, monkeypatch):
        """The point, stated where it can regress: Nominatim must not be called
        at all if the answer has nowhere to go."""
        from app.config import settings
        from app.scraper import geocode

        monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
        geocode._full_cache.clear()
        monkeypatch.setattr(geo_cache, "run_query",
                            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))

        def explode():
            raise AssertionError("Nominatim was called with a dead database")
        monkeypatch.setattr(geocode, "_get_client", lambda: explode())

        with pytest.raises(ConnectionError):
            geocode.geocode_full("1 High St, London")


class TestSurvivableCacheProblems:
    def test_a_type_that_does_not_exist_reads_as_not_cached(self):
        # Verified against ArcadeDB 26.7.3: MATCH on an unknown type returns an
        # empty result rather than an error, so a database predating GeoCache
        # needs no special handling — and none is written.
        with patch.object(geo_cache, "run_query", return_value=[]):
            assert geo_cache.lookup("1 High St") is None

    def test_a_losing_race_to_create_is_swallowed(self):
        # Two workers geocoding the same address: one loses to the UNIQUE index.
        # The coordinate is already paid for and still reaches the entity; only
        # the cache entry is lost.
        with patch.object(geo_cache, "run_query", return_value=[]), \
             patch.object(geo_cache, "run_command", side_effect=RuntimeError("duplicate key")):
            geo_cache.store("1 High St", (51.5, -0.1), "exact")   # must not raise

    def test_an_empty_query_is_ignored(self):
        assert geo_cache.lookup("") is None
        geo_cache.store("", (1.0, 2.0), "exact")   # must not raise


class TestItIsUsedByTheGeocoder:
    def test_a_cached_address_costs_no_request(self, monkeypatch, store):
        """The point of the exercise. 24 companies share one Wilmington address;
        the second through twenty-fourth must not each spend a second of
        Nominatim's budget."""
        from app.config import settings
        from app.scraper import geocode

        monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
        monkeypatch.setattr(geocode, "_throttle", lambda: None)
        geocode._full_cache.clear()
        geo_cache.store("251 Little Falls Drive, Wilmington, US", (39.7, -75.5), "exact")

        def explode():
            raise AssertionError("Nominatim was called for a cached address")
        monkeypatch.setattr(geocode, "_get_client", lambda: explode())

        assert geocode.geocode_full("251 Little Falls Drive, Wilmington, US") \
            == ((39.7, -75.5), "exact")

    def test_a_cached_miss_costs_no_request_either(self, monkeypatch, store):
        from app.config import settings
        from app.scraper import geocode

        monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
        monkeypatch.setattr(geocode, "_throttle", lambda: None)
        geocode._full_cache.clear()
        geo_cache.store("Somewhere OSM Lacks", None, None)

        def explode():
            raise AssertionError("Nominatim was called for a cached miss")
        monkeypatch.setattr(geocode, "_get_client", lambda: explode())

        assert geocode.geocode_full("Somewhere OSM Lacks") is None
