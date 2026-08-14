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


def test_the_structured_pass_runs_against_a_real_database(it_db):
    """The parts are new columns, referenced in both the WHERE and the RETURN.

    ArcadeDB is schemaless, so rows written before they existed simply have no
    such property — a mocked session cannot tell you whether the query still
    matches those rows, or errors, or silently returns nothing.
    """
    _entity(it_db, "with_parts", "Has Parts", reg_street="251 Little Falls Drive",
            reg_city="Wilmington", reg_postcode="19808")
    _entity(it_db, "string_only", "String Only", address="1 High St, London, GB")

    calls: list[dict] = []
    with patch.object(geocode_backfill, "geocode_address",
                      side_effect=lambda a: calls.append(a) or (1.0, 2.0)), \
         patch.object(geocode_backfill, "geocode_full", return_value=((3.0, 4.0), "exact")):
        res = geocode_backfill.backfill(target="registered")

    # Both rows are found — the one with parts and the one without.
    assert res["passes"]["registered"]["geocoded"] == 2
    assert calls == [{"street": "251 Little Falls Drive", "city": "Wilmington",
                      "zip": "19808", "country": None}]

    rows = it_db.run_query("MATCH (e:Entity) WHERE e.reg_lat IS NOT NULL "
                           "RETURN e.id AS id, e.reg_lat AS lat ORDER BY e.id")
    assert {r["id"]: r["lat"] for r in rows} == {"string_only": 3.0, "with_parts": 1.0}
