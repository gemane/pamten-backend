"""
Real-ArcadeDB end-to-end for TOTP two-factor auth: a verified user enables MFA,
then must exchange a login-issued pending token + a TOTP code (or a one-time
recovery code) for the access token. Exercises the real SET writes (secret,
bound-list recovery hashes) the mocked suite can't validate.

Skipped unless ARCADEDB_IT_URL is set.
"""
import base64
import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.hashes import SHA1
from cryptography.hazmat.primitives.twofactor.totp import TOTP
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _code(secret_b32: str) -> str:
    key = base64.b32decode(secret_b32)
    return TOTP(key, 6, SHA1(), 30).generate(int(time.time())).decode()


@pytest.fixture
def client(it_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "admin@pamten.local")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", True)
    from app.main import app
    with TestClient(app) as c:
        yield c


def _register_verified_user(client) -> str:
    """Register + verify a user, return a bearer access token."""
    captured: dict[str, str] = {}
    with patch("app.auth.router.send_verification_email",
               side_effect=lambda to, token: captured.update(v=token)):
        client.post("/auth/register", json={"email": "mfa@example.com", "password": "password123"})
    client.post("/auth/verify-email", json={"token": captured["v"]})
    r = client.post("/auth/login", json={"email": "mfa@example.com", "password": "password123"})
    return r.json()["access_token"]


def test_enable_then_login_requires_totp_and_recovery_code(client):
    token = _register_verified_user(client)
    auth = {"Authorization": f"Bearer {token}"}

    # enrol
    secret = client.post("/auth/mfa/setup", headers=auth).json()["secret"]
    enable = client.post("/auth/mfa/enable", json={"code": _code(secret)}, headers=auth)
    assert enable.status_code == 200 and enable.json()["enabled"] is True
    recovery = enable.json()["recovery_codes"]
    assert len(recovery) == 10

    # login now returns a pending token, not an access token
    r = client.post("/auth/login", json={"email": "mfa@example.com", "password": "password123"})
    assert r.json().get("mfa_required") is True
    mfa_token = r.json()["mfa_token"]

    # exchange it with a fresh TOTP code
    r = client.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": _code(secret)})
    assert r.status_code == 200 and r.json()["access_token"]

    # a recovery code works once...
    mfa_token = client.post("/auth/login", json={"email": "mfa@example.com", "password": "password123"}).json()["mfa_token"]
    r = client.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": recovery[0]})
    assert r.status_code == 200 and r.json()["access_token"]

    # ...and only once (it was consumed)
    mfa_token = client.post("/auth/login", json={"email": "mfa@example.com", "password": "password123"}).json()["mfa_token"]
    r = client.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": recovery[0]})
    assert r.status_code == 401


def test_disable_turns_mfa_off(client):
    token = _register_verified_user(client)
    auth = {"Authorization": f"Bearer {token}"}
    secret = client.post("/auth/mfa/setup", headers=auth).json()["secret"]
    client.post("/auth/mfa/enable", json={"code": _code(secret)}, headers=auth)

    r = client.post("/auth/mfa/disable", json={"code": _code(secret)}, headers=auth)
    assert r.status_code == 200 and r.json()["enabled"] is False

    # login is back to a straight access token
    r = client.post("/auth/login", json={"email": "mfa@example.com", "password": "password123"})
    assert r.status_code == 200 and r.json()["access_token"]
    assert "mfa_required" not in r.json()
