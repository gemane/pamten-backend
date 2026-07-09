"""
Tests for the CRUD routers: auth enforcement on writes, pagination caps,
and basic read/write behaviour. ArcadeDB is faked; auth runs for real.
"""

import pytest


def auth(make_token, role="contributor"):
    return {"Authorization": f"Bearer {make_token(role=role)}"}


# ── Write endpoints require a contributor ───────────────────────────────────────

WRITE_CASES = [
    ("post", "/entities/", {"name": "Acme", "type": "company"}),
    ("post", "/persons/", {"first_name": "Ada", "last_name": "Lovelace"}),
    ("post", "/sources/", {"name": "SEC", "credibility_score": 90, "type": "register"}),
    ("post", "/relationships/owns", {"owner_id": "a", "owned_id": "b"}),
    ("post", "/locations/", {"country": "US"}),
]


@pytest.mark.parametrize("method,path,body", WRITE_CASES)
def test_write_requires_authentication(client, method, path, body):
    r = getattr(client, method)(path, json=body)
    assert r.status_code == 401


@pytest.mark.parametrize("method,path,body", WRITE_CASES)
def test_write_rejects_viewer_role(client, make_token, method, path, body):
    r = getattr(client, method)(path, json=body, headers=auth(make_token, "viewer"))
    assert r.status_code == 403


# ── A contributor gets past the guard (into the DB layer) ───────────────────────

def test_create_entity_succeeds_for_contributor(client, fake_db, make_token):
    fake_db.queue([{"e": {"name": "Acme", "type": "company", "verified": False}}])
    r = client.post("/entities/", json={"name": "Acme", "type": "company"},
                    headers=auth(make_token, "contributor"))
    assert r.status_code == 200
    assert r.json()["name"] == "Acme"


def test_create_source_succeeds_for_admin(client, fake_db, make_token):
    fake_db.queue([{"s": {"name": "SEC", "credibility_score": 90, "type": "register"}}])
    r = client.post("/sources/", json={"name": "SEC", "credibility_score": 90, "type": "register"},
                    headers=auth(make_token, "admin"))
    assert r.status_code == 200
    assert r.json()["name"] == "SEC"


# ── Read endpoints are public ───────────────────────────────────────────────────

def test_get_entity_is_public(client, fake_db):
    fake_db.queue([{"e": {"id": "e1", "name": "Acme", "type": "company", "verified": True}}])
    r = client.get("/entities/e1")
    assert r.status_code == 200
    assert r.json()["name"] == "Acme"


def test_get_missing_entity_returns_404(client, fake_db):
    fake_db.queue([])  # not found
    assert client.get("/entities/nope").status_code == 404


def test_list_entities_is_public(client, fake_db):
    fake_db.queue([{"e": {"id": "e1", "name": "A", "type": "company", "verified": True}}])
    r = client.get("/entities/")
    assert r.status_code == 200
    assert len(r.json()) == 1


# ── Pagination caps ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/entities/", "/persons/", "/sources/"])
def test_pagination_limit_ceiling_enforced(client, path):
    # limit above the Query(le=100) ceiling is rejected before the handler runs
    assert client.get(path, params={"limit": 999999999}).status_code == 422


@pytest.mark.parametrize("path", ["/entities/", "/persons/", "/sources/"])
def test_pagination_negative_skip_rejected(client, path):
    assert client.get(path, params={"skip": -5}).status_code == 422


def test_by_country_limit_ceiling_enforced(client):
    assert client.get("/entities/by-country/US", params={"limit": 10_000}).status_code == 422
