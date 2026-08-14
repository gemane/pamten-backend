"""Geocoding the company a scrape just fetched.

Why it hangs off the post-scrape hook rather than each runner: there is one
place that already knows what a scrape touched and what its target was, and one
place is easier to keep right than five.

Why only the target: Nominatim allows one request a second, and a depth-2 scrape
can touch hundreds of entities. The target costs at most two requests; the rest
belong to the batch pass, which nobody is waiting on.
"""
from unittest.mock import patch

import pytest

from app.config import settings
from app.scraper import graph_writer


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "SCRAPER_GEOCODE_ENABLED", True)
    monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)


@pytest.fixture
def geocoder():
    """Capture the ids handed to the geocoder."""
    calls: list[list[str]] = []
    result = {"geocoded": 1}

    def fake(ids, target="both"):
        calls.append(list(ids))
        return {**result, "passes": {}, "entities_total": len(ids),
                "entities_geocoded": result["geocoded"]}

    with patch("app.scraper.geocode_backfill.geocode_entities", side_effect=fake):
        yield calls


class TestWhatItGeocodes:
    def test_the_scrape_target(self, enabled, geocoder):
        out = graph_writer._geocode_after_scrape({"id": "e1", "depth": 2})
        assert geocoder == [["e1"]]
        assert out == {"geocoding": {"geocoded": 1}}

    def test_only_the_target(self, enabled, geocoder):
        # Not the whole touched-set: at one request a second, a depth-2 scrape
        # would keep the user waiting minutes.
        graph_writer._geocode_after_scrape({"id": "e1"})
        assert geocoder[0] == ["e1"]

    def test_nothing_when_the_scrape_had_no_target(self, enabled, geocoder):
        assert graph_writer._geocode_after_scrape(None) == {}
        assert graph_writer._geocode_after_scrape({}) == {}
        assert geocoder == []


class TestWhenItRuns:
    def test_off_when_the_scrape_geocode_flag_is_off(self, monkeypatch, geocoder):
        monkeypatch.setattr(settings, "SCRAPER_GEOCODE_ENABLED", False)
        monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
        assert graph_writer._geocode_after_scrape({"id": "e1"}) == {}
        assert geocoder == []

    def test_off_when_geocoding_itself_is_off(self, monkeypatch, geocoder):
        """GEOCODING_ENABLED is the master switch — the Nominatim usage policy
        hangs off it, so a scrape must not route around it."""
        monkeypatch.setattr(settings, "SCRAPER_GEOCODE_ENABLED", True)
        monkeypatch.setattr(settings, "GEOCODING_ENABLED", False)
        assert graph_writer._geocode_after_scrape({"id": "e1"}) == {}
        assert geocoder == []


class TestItCannotBreakAScrape:
    def test_a_geocoding_failure_is_reported_not_raised(self, enabled):
        with patch("app.scraper.geocode_backfill.geocode_entities",
                   side_effect=RuntimeError("nominatim down")):
            out = graph_writer._geocode_after_scrape({"id": "e1"})
        assert out["geocoding"]["status"] == "error"


class TestAMergedTarget:
    def test_follows_the_forwarding_address(self):
        """The scoped dedup runs first and can fold the target into a survivor.
        Geocoding the id that no longer exists would write coordinates nobody
        reads."""
        with patch("app.merged_ids.resolve_current_id", return_value="survivor"), \
             patch("app.database.db.get_session"):
            assert graph_writer._scrape_target_after({"id": "old"})["id"] == "survivor"

    def test_keeps_the_id_when_nothing_was_merged(self):
        with patch("app.merged_ids.resolve_current_id", return_value=None), \
             patch("app.database.db.get_session"):
            assert graph_writer._scrape_target_after({"id": "e1"})["id"] == "e1"

    def test_a_lookup_failure_leaves_the_target_alone(self):
        with patch("app.database.db.get_session", side_effect=RuntimeError("db down")):
            assert graph_writer._scrape_target_after({"id": "e1"})["id"] == "e1"
