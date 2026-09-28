"""`/health` answers for the API's ability to SERVE, not for the process being up.

Render's check, the compose healthcheck and an outside uptime check all point
here; a 200 from a process that cannot reach the database made every one of
them lie while every real request failed."""
from unittest.mock import patch


def test_ok_when_the_database_answers(client):
    with patch("app.db.arcadedb.run_sql", return_value=[{"n": 12}]) as probe:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert "version" in body
    # bounded: a hung database must read as down, not as a hang
    assert probe.call_args.kwargs["timeout"] == 4.0


def test_503_when_the_database_does_not(client):
    with patch("app.db.arcadedb.run_sql", side_effect=RuntimeError("ArcadeDB command failed [503]")):
        r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded" and body["database"] == "unreachable"
    assert body["detail"] == "RuntimeError"
    assert "version" in body, "the version is reported either way — it says what is deployed"


def test_the_root_page_does_not_probe(client):
    with patch("app.db.arcadedb.run_sql", side_effect=AssertionError("root must not touch the DB")):
        assert client.get("/").status_code == 200
