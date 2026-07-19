"""
End-to-end tests for the auth API. The ArcadeDB layer is faked (fake_db),
but security.py (bcrypt, JWT) and dependencies.py (role guards) run for real.
"""

from app.auth.security import hash_password


# ── Registration ───────────────────────────────────────────────────────────────

def test_register_first_user_becomes_admin(client, fake_db):
    fake_db.queue([], [{"n": 0}], [])  # no existing user, count=0, create
    r = client.post("/auth/register", json={"email": "boss@x.com", "password": "password123"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_register_second_user_is_viewer(client, fake_db):
    fake_db.queue([], [{"n": 3}], [])  # existing users present
    r = client.post("/auth/register", json={"email": "new@x.com", "password": "password123"})
    assert r.status_code == 200
    assert r.json()["role"] == "viewer"


def test_register_duplicate_email_rejected(client, fake_db):
    fake_db.queue([{"u": {"id": "1"}}])  # existing user found
    r = client.post("/auth/register", json={"email": "dupe@x.com", "password": "password123"})
    assert r.status_code == 400
    assert "already registered" in r.json()["detail"].lower()


def test_register_short_password_rejected(client, fake_db):
    r = client.post("/auth/register", json={"email": "a@x.com", "password": "short"})
    assert r.status_code == 400


def test_register_invalid_email_rejected(client, fake_db):
    r = client.post("/auth/register", json={"email": "not-an-email", "password": "password123"})
    assert r.status_code == 422  # EmailStr validation


def test_register_normalizes_email_to_lowercase(client, fake_db):
    fake_db.queue([], [{"n": 0}], [])
    r = client.post("/auth/register", json={"email": "Test@X.COM", "password": "password123"})
    assert r.status_code == 200
    # the existence-check query must have received the normalized email
    assert fake_db.calls[0][1]["e"] == "test@x.com"


# ── Login ──────────────────────────────────────────────────────────────────────

def _user_row(password="password123", role="viewer"):
    return [{"u": {
        "id": "u1", "email": "user@x.com", "role": role,
        "password_hash": hash_password(password),
    }}]


def test_login_success_returns_token(client, fake_db):
    fake_db.queue(_user_row())
    r = client.post("/auth/login", json={"email": "user@x.com", "password": "password123"})
    assert r.status_code == 200
    assert r.json()["access_token"]
    assert r.json()["role"] == "viewer"


def test_login_wrong_password_rejected(client, fake_db):
    fake_db.queue(_user_row(password="rightpass"))
    r = client.post("/auth/login", json={"email": "user@x.com", "password": "wrongpass"})
    assert r.status_code == 401


def test_login_unknown_email_rejected(client, fake_db):
    fake_db.queue([])  # no user
    r = client.post("/auth/login", json={"email": "ghost@x.com", "password": "password123"})
    assert r.status_code == 401


def test_login_rate_limited_after_repeated_failures(client, fake_db):
    for _ in range(5):
        fake_db.queue([])  # user not found each time
        r = client.post("/auth/login", json={"email": "target@x.com", "password": "password123"})
        assert r.status_code == 401
    # 6th attempt within the window is blocked before touching the DB
    r = client.post("/auth/login", json={"email": "target@x.com", "password": "password123"})
    assert r.status_code == 429


# ── /auth/me and role guards ────────────────────────────────────────────────────

def test_me_requires_authentication(client):
    assert client.get("/auth/me").status_code == 401


def test_me_rejects_garbage_token(client):
    r = client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


def test_me_returns_identity_for_valid_token(client, make_token):
    tok = make_token(role="contributor", sub="u9", email="me@x.com")
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json() == {"id": "u9", "email": "me@x.com", "role": "contributor"}


def test_admin_endpoint_rejects_anonymous(client):
    assert client.get("/auth/users").status_code == 401


def test_admin_endpoint_rejects_viewer(client, make_token):
    tok = make_token(role="viewer")
    assert client.get("/auth/users", headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_admin_endpoint_allows_admin(client, fake_db, make_token):
    fake_db.queue([{"id": "u1", "email": "a@x.com", "role": "admin", "created_at": "2026"}])
    tok = make_token(role="admin")
    r = client.get("/auth/users", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()[0]["email"] == "a@x.com"


# ── Admin user management guards ────────────────────────────────────────────────

def test_update_role_rejects_invalid_role(client, fake_db, make_token):
    tok = make_token(role="admin", sub="admin-1")
    r = client.patch("/auth/users/u2/role", json={"role": "superuser"},
                     headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400


def test_admin_cannot_delete_own_account(client, make_token):
    tok = make_token(role="admin", sub="admin-1")
    r = client.delete("/auth/users/admin-1", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400


def test_admin_can_delete_other_account(client, fake_db, make_token):
    tok = make_token(role="admin", sub="admin-1")
    r = client.delete("/auth/users/other-2", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_update_role_accepts_moderator(client, fake_db, make_token):
    tok = make_token(role="admin", sub="admin-1")
    fake_db.queue([{"id": "u2"}])
    r = client.patch("/auth/users/u2/role", json={"role": "moderator"},
                     headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


# ── require_moderator guard ─────────────────────────────────────────────────────

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from app.auth.dependencies import require_moderator  # noqa: E402


def test_require_moderator_allows_moderator_and_admin():
    assert require_moderator({"role": "moderator"})["role"] == "moderator"
    assert require_moderator({"role": "admin"})["role"] == "admin"


@pytest.mark.parametrize("role", ["contributor", "viewer"])
def test_require_moderator_rejects_lower_roles(role):
    with pytest.raises(HTTPException) as exc:
        require_moderator({"role": role})
    assert exc.value.status_code == 403
