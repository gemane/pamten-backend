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
