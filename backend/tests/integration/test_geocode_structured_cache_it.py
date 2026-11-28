"""
Structured geocoding through the durable cache, against a real ArcadeDB: the
answer lands in the GeoCache table, and a fresh process (the in-memory dict
gone, as after a rebuild or a restart) is answered from it without a request.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.scraper import geocode

pytestmark = pytest.mark.integration


def test_a_second_process_is_answered_from_the_table(it_db, monkeypatch):
    from app import objectstore
    monkeypatch.setattr(objectstore, "enabled", lambda: False)     # the table only
    monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
    monkeypatch.setattr(settings, "GEOCODING_MIN_INTERVAL", 0.0)
    geocode._cache.clear()
    resp = MagicMock(status_code=200)
    resp.json.return_value = [{"lat": "51.5054", "lon": "-0.0235"}]
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    addr = {"street": "1 Churchill Place", "city": "London", "country": "GB"}
    with patch.object(geocode, "_get_client", return_value=client):
        assert geocode.geocode_address(addr) == (51.5054, -0.0235)
        assert client.get.call_count == 1
        geocode._cache.clear()                       # a new process
        assert geocode.geocode_address(addr) == (51.5054, -0.0235)
        assert client.get.call_count == 1, "asked Nominatim again for a known address"
    row = it_db.run_command("MATCH (g:GeoCache) RETURN g.query AS q, g.precision AS p")[0]
    assert row["q"] == "structured:city=London|country=GB|street=1 Churchill Place"
    assert row["p"] == "structured"


def test_a_pass_after_a_rebuild_reads_its_answers_ahead(it_db, monkeypatch):
    """The read-ahead (`geo_cache.warm`) against a real ArcadeDB: the table is
    empty as after a rebuild, the bucket knows every address but one; the pass
    places all of them, asks Nominatim for the one, and the table holds each
    address once afterwards."""
    import json
    from datetime import datetime, timezone
    from app import objectstore
    from app.scraper import geo_cache, geocode_backfill
    monkeypatch.setattr(settings, "GEOCODING_ENABLED", True)
    monkeypatch.setattr(settings, "GEOCODING_MIN_INTERVAL", 0.0)
    geocode._cache.clear(); geocode._full_cache.clear(); geo_cache.cool()
    known = [f"{n} Known St, Berlin" for n in range(1, 13)]
    for n, addr in enumerate(known, 1):
        it_db.run_command(f"CREATE (:Entity {{id:'k{n}', name:'Known {n}', type:'company', hq_address:'{addr}'}})")
    it_db.run_command("CREATE (:Entity {id:'new', name:'New Co', type:'company', hq_address:'1 Unknown Way, Graz'})")
    now = datetime.now(timezone.utc).isoformat()
    bucket = {geo_cache.s3_key(a): json.dumps({"query": a, "lat": 52.0 + i, "lng": 13.0, "precision": "exact",
                                               "checked_at": now}).encode() for i, a in enumerate(known)}
    asked_bucket, put = [], {}
    monkeypatch.setattr(objectstore, "enabled", lambda: True)
    monkeypatch.setattr(objectstore, "get", lambda key: (asked_bucket.append(key), bucket.get(key))[1])
    monkeypatch.setattr(objectstore, "put", lambda key, blob, **kw: put.update({key: blob}) or True)
    resp = MagicMock(status_code=200)
    resp.json.return_value = [{"lat": "47.07", "lon": "15.44", "place_rank": 30}]
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    with patch.object(geocode, "_get_client", return_value=client):
        result = geocode_backfill.backfill(target="hq")
    assert result["passes"]["hq"] == {"total": 13, "geocoded": 13}
    assert client.get.call_count == 1                                  # the one address nobody knew
    assert client.get.call_args.kwargs["params"]["q"] == "1 Unknown Way, Graz"
    assert geo_cache.s3_key("1 Unknown Way, Graz") in put               # … and its answer is shared
    rows = it_db.run_command("MATCH (e:Entity) RETURN e.id AS id, e.hq_lat AS lat ORDER BY e.id")
    assert {r["id"]: r["lat"] for r in rows}["k1"] == 52.0 and all(r["lat"] is not None for r in rows)
    cached = [r["q"] for r in it_db.run_command("MATCH (g:GeoCache) RETURN g.query AS q")]
    assert sorted(cached) == sorted(known + ["1 Unknown Way, Graz"])    # each once: no doubled row
    assert geo_cache.warmed(known[0]) is None                           # dropped with the pass
    # every known address was read from the bucket once — none twice
    assert sorted(k for k in asked_bucket if k in bucket) == sorted(bucket)
