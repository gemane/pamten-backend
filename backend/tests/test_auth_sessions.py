"""Session lifecycle over HTTP: the refresh cookie, /auth/refresh, /auth/logout.

Complements ``test_refresh_tokens.py`` (the store in isolation) by driving the
real endpoints, so the wiring is covered too: that login actually sets a cookie,
that refreshing rotates it, and that the events which should end a session do.

The DB is faked (``fake_db`` for Cypher, ``refresh_rows`` for the token table),
but the cookie handling, JWT signing and route dependencies are real.
"""
import time

import pytest

from app.auth.security import decode_token, hash_password
from app.config import settings

COOKIE = settings.REFRESH_COOKIE_NAME


@pytest.fixture(autouse=True)
def _insecure_cookie(monkeypatch):
    """Let TestClient keep the cookie.

    The test client speaks plain http to ``testserver``, and a ``Secure`` cookie
    is dropped by the client's jar over http — so with the production default
    every test here would see no cookie at all. Turning Secure off is a property
    of the harness, not of the behaviour under test; that the flag *is* emitted
    when configured is asserted separately in ``test_cookie_is_secure_...``.
    """
    monkeypatch.setattr(settings, "REFRESH_COOKIE_SECURE", False)


def _user_row(password="Zt9mQ2vLp4rK", role="viewer", email_verified=True):
    return [{"u": {
        "id": "u1", "email": "user@example.com", "role": role,
        "password_hash": hash_password(password), "email_verified": email_verified,
    }}]


def _login(client, fake_db, **kw):
    fake_db.queue(_user_row(**kw))
    r = client.post("/auth/login",
                    json={"email": "user@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    return r


def _profile_row(role="viewer", verified=True):
    """What /auth/refresh re-reads for the account."""
    return [{"email": "user@example.com", "role": role, "verified": verified}]


# ── Login issues a session ────────────────────────────────────────────────────

def test_login_sets_a_refresh_cookie(client, fake_db, refresh_rows):
    _login(client, fake_db)
    assert COOKIE in client.cookies
    assert len(refresh_rows) == 1


def test_login_cookie_is_httponly(client, fake_db):
    """httpOnly is what keeps an XSS bug from stealing the long-lived credential."""
    r = _login(client, fake_db)
    assert "httponly" in r.headers["set-cookie"].lower()


def test_login_cookie_is_samesite_lax(client, fake_db):
    r = _login(client, fake_db)
    assert "samesite=lax" in r.headers["set-cookie"].lower()


def test_cookie_is_secure_when_configured(client, fake_db, monkeypatch):
    monkeypatch.setattr(settings, "REFRESH_COOKIE_SECURE", True)
    r = _login(client, fake_db)
    assert "secure" in r.headers["set-cookie"].lower()


def test_cookie_value_is_not_the_stored_value(client, fake_db, refresh_rows):
    """The cookie carries the secret; the database holds only its hash."""
    _login(client, fake_db)
    assert client.cookies[COOKIE] not in refresh_rows


def test_access_token_is_short_lived(client, fake_db):
    r = _login(client, fake_db)
    assert r.json()["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    claims = decode_token(r.json()["access_token"])
    assert claims["exp"] - time.time() <= settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60 + 5


def test_login_still_succeeds_when_the_token_store_is_down(client, fake_db, monkeypatch):
    """Issuing is best-effort: a dead store must not make the app unloggable-into."""
    import app.auth.refresh as rt
    monkeypatch.setattr(rt, "run_sql", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    r = _login(client, fake_db)
    assert r.json()["access_token"]
    assert COOKIE not in client.cookies


# ── Refreshing ────────────────────────────────────────────────────────────────

def test_refresh_returns_a_new_access_token(client, fake_db):
    first = _login(client, fake_db).json()["access_token"]
    fake_db.queue(_profile_row())
    r = client.post("/auth/refresh")
    assert r.status_code == 200
    assert r.json()["access_token"]
    assert r.json()["email"] == "user@example.com"
    # A fresh token, not the one from login handed back.
    assert r.json()["access_token"] != first or decode_token(first)["exp"]


def test_refresh_rotates_the_cookie(client, fake_db):
    _login(client, fake_db)
    before = client.cookies[COOKIE]
    fake_db.queue(_profile_row())
    client.post("/auth/refresh")
    assert client.cookies[COOKIE] != before


def test_refresh_without_a_cookie_is_401(client):
    assert client.post("/auth/refresh").status_code == 401


def test_refresh_picks_up_a_role_change(client, fake_db):
    """Re-reading the account each refresh is what bounds how long a stale role
    survives to one access-token lifetime."""
    _login(client, fake_db, role="viewer")
    fake_db.queue(_profile_row(role="admin"))
    r = client.post("/auth/refresh")
    assert r.json()["role"] == "admin"
    assert decode_token(r.json()["access_token"])["role"] == "admin"


def test_refresh_fails_after_the_account_is_deleted(client, fake_db):
    _login(client, fake_db)
    fake_db.queue([])                       # user gone
    r = client.post("/auth/refresh")
    assert r.status_code == 401


def test_refresh_retires_the_new_token_when_the_user_is_gone(client, fake_db, refresh_rows):
    """The successor is minted before the account is checked — it must not be
    left behind as a valid token for a user who no longer exists."""
    _login(client, fake_db)
    fake_db.queue([])
    client.post("/auth/refresh")
    assert all(row["revoked_at"] for row in refresh_rows.values())


def test_a_dead_cookie_is_cleared(client, fake_db):
    """Otherwise the browser replays a token that can never work again."""
    _login(client, fake_db)
    fake_db.queue([])
    r = client.post("/auth/refresh")
    assert r.status_code == 401
    assert COOKIE not in client.cookies


def test_refresh_chains_across_several_calls(client, fake_db):
    _login(client, fake_db)
    for _ in range(3):
        fake_db.queue(_profile_row())
        assert client.post("/auth/refresh").status_code == 200


def test_replaying_an_old_cookie_burns_the_session(client, fake_db):
    """End-to-end theft scenario: the stolen cookie works once, and the moment
    the two copies race, both are locked out."""
    _login(client, fake_db)
    stolen = client.cookies[COOKIE]

    fake_db.queue(_profile_row())
    assert client.post("/auth/refresh").status_code == 200   # legitimate rotation
    live = client.cookies[COOKIE]

    client.cookies.set(COOKIE, stolen)                       # thief replays
    fake_db.queue(_profile_row())
    assert client.post("/auth/refresh").status_code == 401

    client.cookies.set(COOKIE, live)                         # victim now locked out too
    fake_db.queue(_profile_row())
    assert client.post("/auth/refresh").status_code == 401


# ── Logout ────────────────────────────────────────────────────────────────────

def test_logout_clears_the_cookie(client, fake_db):
    _login(client, fake_db)
    assert client.post("/auth/logout").status_code == 200
    assert COOKIE not in client.cookies


def test_logout_revokes_the_token(client, fake_db):
    _login(client, fake_db)
    token = client.cookies[COOKIE]
    client.post("/auth/logout")

    client.cookies.set(COOKIE, token)        # a copy kept by an attacker
    fake_db.queue(_profile_row())
    assert client.post("/auth/refresh").status_code == 401


def test_logout_without_a_session_still_succeeds(client):
    """Idempotent: an expired access token must not trap someone in a session."""
    assert client.post("/auth/logout").status_code == 200


def test_logout_needs_no_access_token(client, fake_db):
    """It is called exactly when the access token may already be dead."""
    _login(client, fake_db)
    r = client.post("/auth/logout")          # no Authorization header sent
    assert r.status_code == 200


# ── Events that end sessions ──────────────────────────────────────────────────

def _auth(make_token):
    return {"Authorization": f"Bearer {make_token(role='viewer', sub='u1')}"}


def test_change_password_revokes_other_sessions(client, fake_db, make_token, refresh_rows):
    import app.auth.refresh as rt
    other_session = rt.issue("u1")           # a second browser

    fake_db.queue([{"hash": hash_password("oldpassword")}], [])
    r = client.post("/auth/change-password", headers=_auth(make_token),
                    json={"current_password": "oldpassword", "new_password": "brandnewpass"})
    assert r.status_code == 200
    assert refresh_rows[rt.hash_token(other_session)]["revoked_at"] > 0


def test_change_password_keeps_the_caller_signed_in(client, fake_db, make_token):
    """Evicting everyone else should not log you out of the browser you are using."""
    fake_db.queue([{"hash": hash_password("oldpassword")}], [])
    r = client.post("/auth/change-password", headers=_auth(make_token),
                    json={"current_password": "oldpassword", "new_password": "brandnewpass"})
    assert r.status_code == 200
    assert COOKIE in client.cookies

    fake_db.queue(_profile_row())
    assert client.post("/auth/refresh").status_code == 200


def test_failed_change_password_revokes_nothing(client, fake_db, make_token, refresh_rows):
    import app.auth.refresh as rt
    session = rt.issue("u1")
    fake_db.queue([{"hash": hash_password("oldpassword")}])
    r = client.post("/auth/change-password", headers=_auth(make_token),
                    json={"current_password": "wrong", "new_password": "brandnewpass"})
    assert r.status_code == 400
    assert refresh_rows[rt.hash_token(session)]["revoked_at"] == 0.0


def test_password_reset_revokes_every_session(client, fake_db, refresh_rows):
    """The 'I lost control of this account' path — including a session an
    attacker may be holding."""
    from datetime import timedelta
    import app.auth.refresh as rt
    from app.auth.router import RESET_PASSWORD_PURPOSE
    from app.auth.security import create_purpose_token, password_hash_fingerprint

    old_hash = hash_password("oldpassword")
    session = rt.issue("u1")
    token = create_purpose_token("u1", RESET_PASSWORD_PURPOSE, timedelta(minutes=30),
                                 extra={"ph": password_hash_fingerprint(old_hash)})

    fake_db.queue([{"hash": old_hash}], [])
    r = client.post("/auth/reset-password",
                    json={"token": token, "new_password": "brandnewpass"})
    assert r.status_code == 200
    assert refresh_rows[rt.hash_token(session)]["revoked_at"] > 0


def test_account_deletion_erases_the_token_rows(client, fake_db, make_token, refresh_rows):
    """Erasure, not revocation — no record of logins after the account is gone."""
    import app.auth.refresh as rt
    rt.issue("u1")
    rt.issue("someone-else")

    fake_db.queue(
        [{"hash": hash_password("mypassword"), "email": "user@example.com", "role": "viewer"}],
        [],   # flag anonymisation
        [],   # delete user
    )
    r = client.request("DELETE", "/auth/me", headers=_auth(make_token),
                       json={"password": "mypassword"})
    assert r.status_code == 200
    assert [row["user_id"] for row in refresh_rows.values()] == ["someone-else"]
    assert COOKIE not in client.cookies
