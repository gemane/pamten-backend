"""`/health` against a real ArcadeDB: the probe's query must be one the server
accepts, or every deployment would report itself down."""
import pytest

pytestmark = pytest.mark.integration


def test_health_is_ok_against_a_real_database(it_db, client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json()["database"] == "ok"


def test_health_is_503_when_the_database_is_gone(it_db, client, monkeypatch):
    from app.config import settings
    from app.db import arcadedb
    monkeypatch.setattr(settings, "ARCADEDB_URL", "http://127.0.0.1:9")   # nothing listens
    arcadedb.close_client()
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["database"] == "unreachable"
    arcadedb.close_client()
