"""Geocoding a named set of entities, against a real ArcadeDB.

The id filter is inlined into the Cypher rather than parameterised, because
ArcadeDB will not take a list parameter — the exact class of assumption that
passes against a mock and matches nothing against the real database. So it is
tested here.
"""
from unittest.mock import patch

import pytest

from app.scraper import geocode_backfill

pytestmark = pytest.mark.integration


def _entity(it_db, eid, name, **props):
    parts = [f"id:'{eid}'", f"name:'{name}'", "type:'company'"]
    parts += [f"{k}:'{v}'" for k, v in props.items()]
    it_db.run_command(f"CREATE (:Entity {{{', '.join(parts)}}})")


def _coords(it_db, eid):
    rows = it_db.run_query(f"MATCH (e:Entity {{id:'{eid}'}}) "
                           "RETURN e.hq_lat AS hq, e.reg_lat AS reg")
    return rows[0] if rows else {}


@pytest.fixture
def geocoder():
    """Deterministic coordinates, so the tests never touch Nominatim."""
    with patch.object(geocode_backfill, "geocode_full", return_value=((1.0, 2.0), "exact")), \
         patch.object(geocode_backfill, "geocode_address", return_value=(3.0, 4.0)):
        yield


def test_geocodes_only_the_named_entities(it_db, geocoder):
    _entity(it_db, "e1", "Target Co", hq_address="1 A St, Berlin", address="2 B St, Berlin")
    _entity(it_db, "e2", "Bystander Ltd", hq_address="9 Z St, Berlin", address="9 Z St, Berlin")

    geocode_backfill.geocode_entities(["e1"])

    assert _coords(it_db, "e1")["hq"] == 1.0
    # The whole point of the scope: a scrape must not quietly geocode the graph.
    assert _coords(it_db, "e2")["hq"] is None


def test_fills_both_places_for_one_entity(it_db, geocoder):
    _entity(it_db, "e1", "Target Co", hq_address="1 A St, Berlin", address="2 B St, Berlin")
    geocode_backfill.geocode_entities(["e1"])
    row = _coords(it_db, "e1")
    assert row["hq"] == 1.0 and row["reg"] == 1.0


def test_an_empty_id_list_geocodes_nothing(it_db, geocoder):
    _entity(it_db, "e1", "Target Co", hq_address="1 A St, Berlin")
    assert geocode_backfill.geocode_entities([])["geocoded"] == 0
    assert _coords(it_db, "e1")["hq"] is None


def test_an_unknown_id_is_harmless(it_db, geocoder):
    assert geocode_backfill.geocode_entities(["nope"])["geocoded"] == 0


def test_it_skips_what_is_already_placed(it_db, geocoder):
    """Resumability, per place: an entity with a headquarters pin but no
    registered one must still get the registered pass."""
    _entity(it_db, "e1", "Half Placed", hq_address="1 A St", address="2 B St")
    it_db.run_command("MATCH (e:Entity {id:'e1'}) SET e.hq_lat = 55.0, e.hq_lng = 55.0")

    res = geocode_backfill.geocode_entities(["e1"])

    row = _coords(it_db, "e1")
    assert row["hq"] == 55.0        # untouched
    assert row["reg"] == 1.0        # filled
    assert res["passes"]["hq"]["total"] == 0
