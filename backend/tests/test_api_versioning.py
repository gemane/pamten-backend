"""
Tests for the /v1 API prefix.

Every router is served twice — under /v1 (canonical, documented) and at the
original unversioned path (deprecated, hidden from the schema). The legacy mount
is not cosmetic: a released mobile app pins whatever path it shipped with, so
dropping it breaks callers we don't deploy. These tests are what should fail if
someone "tidies up" by deleting one of the two mounts.

They used /federation/export as their example until federation was put on hold
and its router unmounted. Deliberately re-pointed at auth-gated routers that are always mounted: a
versioning test must not fail because an unrelated feature was parked, and the
example has to answer 401 without touching the database.
"""
from app.main import API_V1_PREFIX


def test_prefix_is_v1():
    assert API_V1_PREFIX == "/v1"


def _documented_paths(client) -> dict:
    return client.get("/openapi.json").json()["paths"]


def test_every_documented_api_path_is_versioned(client):
    paths = _documented_paths(client)
    unversioned = [p for p in paths if not p.startswith("/v1/")]
    # Health endpoints stay unversioned on purpose — uptime monitoring and
    # Render's health check point at a URL that must never move.
    assert sorted(unversioned) == ["/", "/health"]


def test_the_schema_documents_the_real_api(client):
    paths = _documented_paths(client)
    for expected in ("/v1/search/", "/v1/auth/login", "/v1/entities/", "/v1/flags"):
        assert expected in paths, f"{expected} missing from the OpenAPI schema"


def test_legacy_unversioned_paths_still_serve(client):
    # 401 (not 404) proves the route exists and the auth dependency ran — no DB
    # needed. A missing route would 404 before any dependency is evaluated.
    for legacy in ("/auth/users", "/flags/"):
        assert client.get(legacy).status_code != 404, f"{legacy} stopped being served"


def test_versioned_and_legacy_paths_behave_the_same(client):
    for path in ("/auth/users", "/flags/"):
        assert client.get(path).status_code == client.get(f"/v1{path}").status_code


def test_legacy_paths_are_hidden_from_the_schema(client):
    paths = _documented_paths(client)
    # Present and working, but not advertised — new clients should pick /v1.
    assert "/auth/users" not in paths
    assert "/flags" not in paths


def test_health_is_not_served_under_v1(client):
    assert client.get("/health").status_code == 200
    assert client.get("/v1/health").status_code == 404


def test_unknown_paths_still_404_under_the_prefix(client):
    assert client.get("/v1/definitely-not-a-route").status_code == 404
