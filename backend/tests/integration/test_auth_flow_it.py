"""
Real-ArcadeDB end-to-end for email verification + password reset: register a
viewer, confirm login is blocked until verified, verify via the emailed token,
then log in; then reset the password and confirm the old one stops working.

The email transport is patched to capture the token the app would have sent
(console/SMTP never actually fires). Skipped unless ARCADEDB_IT_URL is set.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture
def client(it_db, monkeypatch):
    # ADMIN_EMAIL set (no password) => registrants are unverified viewers, and
    # startup bootstrap is a no-op. Verification is required.
    from app.config import settings
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "admin@pamten.local")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", True)
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_register_verify_login_then_reset(client):
    captured: dict[str, str] = {}
    with patch("app.auth.router.send_verification_email",
               side_effect=lambda to, token: captured.update(verify=token)):
        r = client.post("/auth/register", json={"email": "jane@example.com", "password": "password123"})
    assert r.status_code == 200 and r.json()["verification_required"] is True

    # login blocked until verified
    r = client.post("/auth/login", json={"email": "jane@example.com", "password": "password123"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "email_not_verified"

    # verify via the emailed token, then login works
    r = client.post("/auth/verify-email", json={"token": captured["verify"]})
    assert r.status_code == 200
    r = client.post("/auth/login", json={"email": "jane@example.com", "password": "password123"})
    assert r.status_code == 200 and r.json()["access_token"]

    # forgot -> reset with the emailed token
    with patch("app.auth.router.send_password_reset_email",
               side_effect=lambda to, token: captured.update(reset=token)):
        r = client.post("/auth/forgot-password", json={"email": "jane@example.com"})
    assert r.status_code == 200
    r = client.post("/auth/reset-password",
                    json={"token": captured["reset"], "new_password": "brandnewpass"})
    assert r.status_code == 200

    # old password no longer works, new one does
    assert client.post("/auth/login",
                       json={"email": "jane@example.com", "password": "password123"}).status_code == 401
    assert client.post("/auth/login",
                       json={"email": "jane@example.com", "password": "brandnewpass"}).status_code == 200

    # the reset link is single-use: replaying it now fails
    r = client.post("/auth/reset-password",
                    json={"token": captured["reset"], "new_password": "yetanother1"})
    assert r.status_code == 400
