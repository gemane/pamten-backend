"""Tests for the geocode backfill (DB + geocoder mocked).

backfill() geocodes Entities that carry an HQ address or city/country but no
coordinates. It used to run a second pass over Location nodes and copy their
coordinates onto the entities pointing at them; Location is gone and the Entity
holds its own HQ, so there is one pass and one place the coordinates land.
"""

from unittest.mock import patch

from app.scraper import geocode_backfill


def test_backfill_geocodes_entities_with_hq_but_no_coords():
    ent_rows = [{"id": "e1", "city": "Vienna", "country": "AT"}]
    commands = []

    with patch.object(geocode_backfill, "run_query", return_value=ent_rows), \
         patch.object(geocode_backfill, "run_command", side_effect=lambda s, p=None: commands.append((s, p))), \
         patch.object(geocode_backfill, "geocode_address", return_value=(48.2, 16.37)):
        result = geocode_backfill.backfill()

    assert result["entities_geocoded"] == 1
    assert result["geocoded"] == 1
    set_entity = [c for c in commands if "SET e.hq_lat" in c[0]]
    assert len(set_entity) == 1
    assert set_entity[0][1]["lat"] == 48.2 and set_entity[0][1]["lng"] == 16.37


def test_backfill_writes_only_to_the_entity():
    """Nothing should touch a Location node — the type no longer exists, and a
    stray write against a missing type is the kind of thing that fails silently
    against a real database."""
    ent_rows = [{"id": "e1", "city": "Vienna", "country": "AT"}]
    commands = []

    with patch.object(geocode_backfill, "run_query", return_value=ent_rows), \
         patch.object(geocode_backfill, "run_command", side_effect=lambda s, p=None: commands.append((s, p))), \
         patch.object(geocode_backfill, "geocode_address", return_value=(48.2, 16.37)):
        geocode_backfill.backfill()

    assert not any("Location" in c[0] for c in commands)


def test_backfill_prefers_a_full_address_over_city_and_country():
    """A street-level hit gives a real pin; city/country is only the fallback,
    and the precision is recorded so the map can show a circle instead."""
    ent_rows = [{"id": "e1", "hq_address": "1 A St, Berlin", "city": "Berlin", "country": "DE"}]
    commands = []

    with patch.object(geocode_backfill, "run_query", return_value=ent_rows), \
         patch.object(geocode_backfill, "run_command", side_effect=lambda s, p=None: commands.append((s, p))), \
         patch.object(geocode_backfill, "geocode_full", return_value=((52.5, 13.4), "exact")) as full, \
         patch.object(geocode_backfill, "geocode_address") as approx:
        geocode_backfill.backfill()

    full.assert_called_once()
    approx.assert_not_called()
    assert commands[0][1]["prec"] == "exact"


def test_backfill_skips_when_no_geocode_match():
    with patch.object(geocode_backfill, "run_query",
                      return_value=[{"id": "e1", "city": "Nowhere", "country": "XX"}]), \
         patch.object(geocode_backfill, "run_command") as cmd, \
         patch.object(geocode_backfill, "geocode_address", return_value=None):
        result = geocode_backfill.backfill()
    assert result["geocoded"] == 0
    cmd.assert_not_called()  # nothing written when geocoding fails


def test_backfill_passes_limit_into_the_query():
    with patch.object(geocode_backfill, "run_query", return_value=[]) as q, \
         patch.object(geocode_backfill, "run_command"):
        geocode_backfill.backfill(limit=25)
    assert all("LIMIT 25" in c.args[0] for c in q.call_args_list)
