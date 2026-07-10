"""Tests for the geocode backfill (DB + geocoder mocked)."""

from unittest.mock import patch

from app.scraper import geocode_backfill


def test_backfill_geocodes_and_updates_missing_locations():
    rows = [
        {"id": "l1", "street": "1 A St", "city": "Berlin", "state": None, "zip": None, "country": "DE"},
        {"id": "l2", "street": None, "city": "Paris", "state": None, "zip": None, "country": "FR"},
    ]
    commands = []

    def fake_command(stmt, params=None):
        commands.append((stmt, params))

    with patch.object(geocode_backfill, "run_query", return_value=rows), \
         patch.object(geocode_backfill, "run_command", side_effect=fake_command), \
         patch.object(geocode_backfill, "geocode_address", return_value=(52.5, 13.4)):
        result = geocode_backfill.backfill()

    assert result == {"total": 2, "geocoded": 2, "skipped": 0}
    # each location → one SET on the Location + one denormalize onto entities
    set_location = [c for c in commands if "SET l.latitude" in c[0]]
    denorm = [c for c in commands if "e.hq_lat" in c[0]]
    assert len(set_location) == 2
    assert len(denorm) == 2
    assert set_location[0][1]["lat"] == 52.5 and set_location[0][1]["lng"] == 13.4


def test_backfill_skips_locations_without_a_match():
    rows = [{"id": "l1", "street": None, "city": "Nowhere", "state": None, "zip": None, "country": "XX"}]
    with patch.object(geocode_backfill, "run_query", return_value=rows), \
         patch.object(geocode_backfill, "run_command") as cmd, \
         patch.object(geocode_backfill, "geocode_address", return_value=None):
        result = geocode_backfill.backfill()
    assert result == {"total": 1, "geocoded": 0, "skipped": 1}
    cmd.assert_not_called()  # nothing written when geocoding fails


def test_backfill_passes_limit_into_query():
    with patch.object(geocode_backfill, "run_query", return_value=[]) as q, \
         patch.object(geocode_backfill, "run_command"):
        geocode_backfill.backfill(limit=25)
    assert "LIMIT 25" in q.call_args.args[0]
