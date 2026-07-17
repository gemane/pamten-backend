"""Tests for the geocode backfill (DB + geocoder mocked).

backfill() geocodes Location nodes (address → coords) AND Entities that carry
an HQ city/country directly but no coordinates. run_query is called once per
group (locations, then entities).
"""

from unittest.mock import patch

from app.scraper import geocode_backfill


def test_backfill_geocodes_and_updates_missing_locations():
    loc_rows = [
        {"id": "l1", "street": "1 A St", "city": "Berlin", "state": None, "zip": None, "country": "DE"},
        {"id": "l2", "street": None, "city": "Paris", "state": None, "zip": None, "country": "FR"},
    ]
    commands = []

    with patch.object(geocode_backfill, "run_query", side_effect=[loc_rows, []]), \
         patch.object(geocode_backfill, "run_command", side_effect=lambda s, p=None: commands.append((s, p))), \
         patch.object(geocode_backfill, "geocode_address", return_value=(52.5, 13.4)):
        result = geocode_backfill.backfill()

    assert result["locations_geocoded"] == 2
    assert result["entities_geocoded"] == 0
    assert result["geocoded"] == 2
    # each location → one SET on the Location + one denormalize onto entities
    assert len([c for c in commands if "SET l.latitude" in c[0]]) == 2
    assert len([c for c in commands if "e.hq_lat" in c[0]]) == 2


def test_backfill_geocodes_entities_with_hq_but_no_coords():
    ent_rows = [{"id": "e1", "city": "Vienna", "country": "AT"}]
    commands = []

    with patch.object(geocode_backfill, "run_query", side_effect=[[], ent_rows]), \
         patch.object(geocode_backfill, "run_command", side_effect=lambda s, p=None: commands.append((s, p))), \
         patch.object(geocode_backfill, "geocode_address", return_value=(48.2, 16.37)):
        result = geocode_backfill.backfill()

    assert result["entities_geocoded"] == 1
    assert result["geocoded"] == 1
    set_entity = [c for c in commands if "SET e.hq_lat" in c[0]]
    assert len(set_entity) == 1
    assert set_entity[0][1]["lat"] == 48.2 and set_entity[0][1]["lng"] == 16.37


def test_backfill_skips_when_no_geocode_match():
    with patch.object(geocode_backfill, "run_query",
                      side_effect=[[{"id": "l1", "street": None, "city": "Nowhere",
                                     "state": None, "zip": None, "country": "XX"}], []]), \
         patch.object(geocode_backfill, "run_command") as cmd, \
         patch.object(geocode_backfill, "geocode_address", return_value=None):
        result = geocode_backfill.backfill()
    assert result["geocoded"] == 0
    cmd.assert_not_called()  # nothing written when geocoding fails


def test_backfill_passes_limit_into_both_queries():
    with patch.object(geocode_backfill, "run_query", return_value=[]) as q, \
         patch.object(geocode_backfill, "run_command"):
        geocode_backfill.backfill(limit=25)
    assert all("LIMIT 25" in c.args[0] for c in q.call_args_list)   # locations + entities
