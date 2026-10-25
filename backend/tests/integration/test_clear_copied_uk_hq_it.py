"""Real-ArcadeDB test for maintenance.clear_copied_uk_headquarters: the UK
importers copied the registered office into hq_* until 2026-10-18; the heal
clears those fields (the geocoded pin too), moves the parts under reg_* where
nothing stands there, fills the display `address`, and leaves a headquarters
another source stated alone. Pins the paged id-index walk and the sqlscript
UPDATE-by-rid with null and parameter assignments mixed in one statement.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration

OFFICE = "MARSTON'S HOUSE, BREWERY ROAD, WOLVERHAMPTON, ENGLAND, WV1 4JT"


def _seed(it_db):
    # a company both importers touched: hq_* is the registered office, geocoded
    it_db.run_command(
        "CREATE (:Entity {id:'gb-coh:00078439', name:'Carlsberg', address:$o, "
        "hq_address:$o, hq_city:'WOLVERHAMPTON', hq_country:'GB', hq_street:\"MARSTON'S HOUSE, BREWERY ROAD\", "
        "hq_postcode:'WV1 4JT', hq_lat:52.58, hq_lng:-2.12, hq_geo_precision:'street', "
        "reg_street:\"MARSTON'S HOUSE, BREWERY ROAD\", reg_city:'WOLVERHAMPTON', reg_postcode:'WV1 4JT'})", {"o": OFFICE})
    # a corporate PSC owner from the PSC import: parts only under hq_*, no pin yet
    it_db.run_command(
        "CREATE (:Entity {id:'chpsc:07434180:x', name:'Robert Hitchins Limited', "
        "address:'The Manor, Cheltenham, GL51 0TJ', hq_address:'The Manor, Cheltenham, GL51 0TJ', "
        "hq_city:'Cheltenham', hq_country:'GB', hq_street:'The Manor', hq_postcode:'GL51 0TJ'})")
    # a merged node whose headquarters GLEIF stated
    it_db.run_command(
        "CREATE (:Entity {id:'gb-coh:00041424', name:'Unilever Plc', address:'Port Sunlight, Wirral, CH62 4ZD, GB', "
        "address_normalized:'port sunlight wirral ch62 4zd gb', hq_address:'Unilever House, 100 Victoria Embankment, London', "
        "hq_city:'London', hq_country:'GB', hq_lat:51.51, hq_lng:-0.10})")
    # not a UK node: never walked
    it_db.run_command(
        "CREATE (:Entity {id:'lei:X', name:'Elsewhere AG', address:'Same', hq_address:'Same'})")


def _node(it_db, nid):
    rows = it_db.run_command(
        "MATCH (e:Entity {id:$id}) RETURN e.address AS a, e.hq_address AS ha, "
        "e.hq_city AS hc, e.hq_country AS hk, e.hq_street AS hs, e.hq_postcode AS hp, e.hq_lat AS hlat, "
        "e.hq_lng AS hlng, e.hq_geo_precision AS hprec, e.reg_street AS rs, e.reg_city AS rc, "
        "e.reg_postcode AS rp, e.reg_lat AS rlat, e.reg_lng AS rlng, e.reg_geo_precision AS rprec", {"id": nid})
    return rows[0]


def test_the_copied_headquarters_is_cleared_and_kept_under_registered(it_db):
    from app.scraper.maintenance import clear_copied_uk_headquarters
    _seed(it_db)
    res = clear_copied_uk_headquarters(apply=False)
    assert res == {"nodes": 3, "copied": 2, "pins_cleared": 1, "applied": False}
    assert _node(it_db, "gb-coh:00078439")["ha"] == OFFICE, "a dry run wrote"

    res = clear_copied_uk_headquarters(apply=True)
    assert res == {"nodes": 3, "copied": 2, "pins_cleared": 1, "applied": True}

    c = _node(it_db, "gb-coh:00078439")
    assert (c["ha"], c["hc"], c["hk"], c["hs"], c["hp"], c["hlat"], c["hlng"], c["hprec"]) == (None,) * 8
    assert (c["rs"], c["rc"], c["rp"]) == ("MARSTON'S HOUSE, BREWERY ROAD", "WOLVERHAMPTON", "WV1 4JT")
    # the pin is NOT moved under Registered: it may have been another source's HQ point
    assert (c["rlat"], c["rlng"], c["rprec"]) == (None, None, None)
    assert c["a"] == OFFICE                                                  # the panel's display address

    o = _node(it_db, "chpsc:07434180:x")
    assert (o["ha"], o["hc"], o["hs"], o["hp"]) == (None,) * 4
    assert (o["rs"], o["rc"], o["rp"], o["rlat"]) == ("The Manor", "Cheltenham", "GL51 0TJ", None)
    assert o["a"] == "The Manor, Cheltenham, GL51 0TJ"

    u = _node(it_db, "gb-coh:00041424")
    assert u["ha"] == "Unilever House, 100 Victoria Embankment, London" and u["hlat"] == 51.51
    assert u["a"] == "Port Sunlight, Wirral, CH62 4ZD, GB"

    assert _node(it_db, "lei:X")["ha"] == "Same"
    # idempotent: nothing left to clear
    assert clear_copied_uk_headquarters(apply=True)["copied"] == 0
