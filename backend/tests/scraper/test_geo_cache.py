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
            if "{query: $q}" in sql:
                return [{"n": len([r for r in rows if r["query"] == (params or {}).get("q")])}]
            return [{"n": len(rows)}]
        return [r for r in rows if r["query"] == (params or {}).get("q")][:1]

    def fake_command(sql, params=None):
        p = params or {}
        if "DELETE g" in sql:
            rows[:] = [r for r in rows if r["query"] != p["q"]]
            return []
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


# ── the shared layer ────────────────────────────────────────────────────────

from app import objectstore  # noqa: E402
from app.config import settings  # noqa: E402
from tests.test_objectstore import FakeS3  # noqa: E402


@pytest.fixture
def shared():
    s3 = FakeS3()
    with patch.object(settings, "OBJECT_STORE_BUCKET", "b"), \
         patch.object(settings, "OBJECT_STORE_ENDPOINT", "e"), \
         patch.object(objectstore, "_get_client", lambda: s3):
        yield s3


class TestSharedLayer:
    ADDR = "1 CHURCHILL PLACE, LONDON, E14 5HP, GB"

    def test_a_store_writes_the_bucket_too(self, store, shared):
        geo_cache.store(self.ADDR, (51.5, -0.01), "exact")
        key = geo_cache.s3_key(self.ADDR)
        assert key.startswith("geocode/") and len(key) == len("geocode/") + 64
        row = __import__("json").loads(shared.objects[key])
        assert row["query"] == self.ADDR and row["lat"] == 51.5 and row["precision"] == "exact"

    def test_a_table_miss_is_answered_from_the_bucket_and_refills_the_table(self, store, shared):
        # The rebuild case: the table is empty, the bucket remembers.
        geo_cache.store(self.ADDR, (51.5, -0.01), "exact")
        store.clear()
        assert geo_cache.lookup(self.ADDR) == ((51.5, -0.01), "exact")
        assert [r["query"] for r in store] == [self.ADDR], "copied back into the table"
        shared.calls.clear()
        geo_cache.lookup(self.ADDR)
        assert not any(c.startswith("get") for c in shared.calls), "second read is local"

    def test_a_cached_miss_survives_the_rebuild_too(self, store, shared):
        # Misses are the expensive part — 829 of 892 dev addresses — and were
        # re-asked after every rebuild.
        geo_cache.store("PO BOX 2271, CAPE TOWN, ZA", None, None)
        store.clear()
        assert geo_cache.lookup("PO BOX 2271, CAPE TOWN, ZA") == (None, None)

    def test_a_stale_bucket_miss_is_retried(self, store, shared):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        shared.objects[geo_cache.s3_key(self.ADDR)] = __import__("json").dumps(
            {"query": self.ADDR, "lat": None, "lng": None, "precision": None, "checked_at": old}).encode()
        assert geo_cache.lookup(self.ADDR) is None
        assert store == [], "nothing stale is copied back"

    def test_the_table_wins_when_it_has_an_answer(self, store, shared):
        geo_cache.store(self.ADDR, (1.0, 1.0), "approx")
        shared.calls.clear()
        assert geo_cache.lookup(self.ADDR) == ((1.0, 1.0), "approx")
        assert shared.calls == [], "no bucket round-trip for a local hit"

    def test_a_digest_collision_or_bad_object_is_ignored(self, store, shared):
        shared.objects[geo_cache.s3_key(self.ADDR)] = b'{"query": "SOMEWHERE ELSE", "lat": 1, "lng": 1}'
        assert geo_cache.lookup(self.ADDR) is None
        shared.objects[geo_cache.s3_key(self.ADDR)] = b"not json"
        assert geo_cache.lookup(self.ADDR) is None

    def test_off_without_the_store(self, store):
        with patch.object(settings, "OBJECT_STORE_BUCKET", ""):
            geo_cache.store(self.ADDR, (1.0, 1.0), "exact")
            store.clear()
            assert geo_cache.lookup(self.ADDR) is None


class TestForget:
    ADDR = "12 HOME STREET, SMALLTOWN, GB"

    def test_removes_the_row_and_the_object(self, store, shared):
        geo_cache.store(self.ADDR, (1.0, 2.0), "exact")
        assert geo_cache.s3_key(self.ADDR) in shared.objects
        r = geo_cache.forget(self.ADDR)
        assert r == {"rows": 1, "object": True}
        assert geo_cache.s3_key(self.ADDR) not in shared.objects
        # and it does not come back from the bucket on the next lookup
        store.clear()
        assert geo_cache.lookup(self.ADDR) is None

    def test_forgetting_an_unknown_address_is_quiet(self, store, shared):
        assert geo_cache.forget("NOWHERE") == {"rows": 0, "object": True}


class TestWarm:
    """`warm` reads many cached answers ahead, several at a time (the user,
    2026-11-28): a pass after an import asks for thousands of addresses whose
    answers are all in the bucket, at ~180 ms a read — 16 minutes of the
    2026-11-27 rebuild's step 3 for 4,010 addresses and six Nominatim requests."""

    @pytest.fixture(autouse=True)
    def _cool(self):
        geo_cache.cool()
        yield
        geo_cache.cool()

    @staticmethod
    def _obj(query, lat=51.5, lng=-0.1, checked=None):
        import json
        return json.dumps({"query": query, "lat": lat, "lng": lng, "precision": "exact" if lat else None,
                           "checked_at": checked or datetime.now(timezone.utc).isoformat()}).encode()

    def _bucket(self, objects):
        got = []

        def get(key):
            got.append(key)
            return objects.get(key)
        return got, patch.multiple(geo_cache.objectstore, enabled=lambda: True, get=get)

    def test_the_lookups_after_it_ask_nothing(self, store):
        geo_cache.store("in the table", (48.2, 16.4), "exact")
        got, bucket = self._bucket({geo_cache.s3_key("in the bucket"): self._obj("in the bucket")})
        with bucket:
            assert geo_cache.warm(["in the table", "in the bucket", "nowhere", "in the bucket", ""]) == 2
            # each once, the table's own not at all
            assert sorted(got) == sorted([geo_cache.s3_key("in the bucket"), geo_cache.s3_key("nowhere")])
            with patch.object(geo_cache, "run_query", side_effect=AssertionError("asked the table")), \
                 patch.object(geo_cache.objectstore, "get", side_effect=AssertionError("asked the bucket")):
                assert geo_cache.lookup("in the table") == ((48.2, 16.4), "exact")
                assert geo_cache.lookup("in the bucket") == ((51.5, -0.1), "exact")
        assert geo_cache.warmed("nowhere") is None and geo_cache.warmed(None) is None

    def test_an_answer_from_the_bucket_refills_the_table_with_one_request(self, store):
        commands = []
        _, bucket = self._bucket({geo_cache.s3_key("q"): self._obj("q")})
        real = geo_cache.run_command.side_effect
        with bucket, patch.object(geo_cache, "run_command", side_effect=lambda s, p=None: (commands.append(s), real(s, p))[1]):
            geo_cache.warm(["q"])
        assert len(commands) == 1 and commands[0].lstrip().startswith("CREATE")
        assert [(r["query"], r["lat"]) for r in store] == [("q", 51.5)]

    def test_what_is_found_nowhere_is_left_to_the_caller(self, store):
        _, bucket = self._bucket({})
        with bucket:
            assert geo_cache.warm(["nowhere"]) == 0
            assert geo_cache.lookup("nowhere") is None       # "not cached": Nominatim's turn
        assert store == []

    def test_a_fresh_miss_is_an_answer_and_a_stale_one_is_not(self, store):
        old = (datetime.now(timezone.utc) - timedelta(days=geo_cache._MISS_TTL_DAYS + 1)).isoformat()
        _, bucket = self._bucket({geo_cache.s3_key("fresh"): self._obj("fresh", None, None),
                                  geo_cache.s3_key("stale"): self._obj("stale", None, None, old)})
        with bucket:
            assert geo_cache.warm(["fresh", "stale"]) == 1
        assert geo_cache.warmed("fresh") == (None, None) and geo_cache.warmed("stale") is None
        assert [r["query"] for r in store] == ["fresh"]      # a stale miss is not copied back

    def test_a_newer_answer_replaces_the_one_read_ahead(self, store):
        geo_cache.store("q", None, None)
        geo_cache.warm(["q"])
        assert geo_cache.warmed("q") == (None, None)
        geo_cache.store("q", (1.0, 2.0), "exact")
        assert geo_cache.lookup("q") == ((1.0, 2.0), "exact")

    def test_cool_drops_what_was_read(self, store):
        geo_cache.store("q", (1.0, 2.0), "exact")
        geo_cache.warm(["q"])
        geo_cache.cool()
        assert geo_cache.warmed("q") is None

    def test_the_reads_run_side_by_side(self, store):
        import threading
        together = threading.Barrier(3, timeout=5)        # three reads must be in flight at once

        def get(key):
            together.wait()
            return None
        with patch.multiple(geo_cache.objectstore, enabled=lambda: True, get=get):
            assert geo_cache.warm(["a", "b", "c"]) == 0     # a sequential reader would time out

    def test_an_unreachable_database_is_still_said(self):
        with patch.object(geo_cache, "run_query", side_effect=ConnectionError("down")), \
             pytest.raises(ConnectionError):
            geo_cache.warm(["a", "b"])
