"""
End-to-end tests for the auth API. The ArcadeDB layer is faked (fake_db),
but security.py (bcrypt, JWT) and dependencies.py (role guards) run for real.
"""

from app.auth.security import hash_password


# ── Registration ───────────────────────────────────────────────────────────────

def test_register_first_user_becomes_admin(client, fake_db):
    fake_db.queue([], [{"n": 0}], [])  # no existing user, count=0, create
    r = client.post("/auth/register", json={"email": "boss@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_register_second_user_is_viewer_and_must_verify(client, fake_db):
    fake_db.queue([], [{"n": 3}], [])  # existing users present
    r = client.post("/auth/register", json={"email": "new@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    # A non-admin registrant gets no token — they must verify their email first.
    body = r.json()
    assert body["verification_required"] is True
    assert "access_token" not in body


def test_register_never_admin_when_env_admin_configured(client, fake_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "boss@example.com")
    fake_db.queue([], [])  # dup-check empty, then create (no count query on this path)
    r = client.post("/auth/register", json={"email": "first@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    # even as the very first user — no self-promotion, and must verify
    assert r.json()["verification_required"] is True


class TestBootstrapAdmin:
    def test_creates_admin_when_missing(self, fake_db, monkeypatch):
        from app.config import settings
        from app.auth.router import bootstrap_admin
        monkeypatch.setattr(settings, "ADMIN_EMAIL", "Boss@example.com")
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "Zt9mQ2vLp4rK")
        fake_db.queue([], [])   # not found, then create
        bootstrap_admin()
        creates = [c for c in fake_db.calls if "CREATE (u:User" in c[0]]
        assert len(creates) == 1
        assert creates[0][1]["email"] == "boss@example.com"   # normalized to lowercase
        assert "role: 'admin'" in creates[0][0]

    def test_skips_when_admin_already_exists(self, fake_db, monkeypatch):
        from app.config import settings
        from app.auth.router import bootstrap_admin
        monkeypatch.setattr(settings, "ADMIN_EMAIL", "boss@example.com")
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "Zt9mQ2vLp4rK")
        fake_db.queue([{"u": {"id": "1"}}])   # already exists
        bootstrap_admin()
        assert not any("CREATE (u:User" in c[0] for c in fake_db.calls)   # no overwrite

    def test_noop_when_unconfigured(self, fake_db, monkeypatch):
        from app.config import settings
        from app.auth.router import bootstrap_admin
        monkeypatch.setattr(settings, "ADMIN_EMAIL", None)
        bootstrap_admin()
        assert fake_db.calls == []

    def test_warns_when_admin_password_is_short(self, fake_db, monkeypatch, caplog):
        import logging
        from app.config import settings
        from app.auth.router import bootstrap_admin
        monkeypatch.setattr(settings, "ADMIN_EMAIL", "boss@example.com")
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "tooshort")  # 8 chars, < 12
        fake_db.queue([{"u": {"id": "1"}}])  # admin already exists — skip create
        with caplog.at_level(logging.WARNING, logger="app.auth.router"):
            bootstrap_admin()
        assert any(
            "ADMIN_PASSWORD" in r.message and "characters" in r.message
            for r in caplog.records
        ), "Expected a short-password warning in the log"

    def test_warns_when_admin_password_is_common(self, fake_db, monkeypatch, caplog):
        import logging
        from app.config import settings
        from app.auth import router as auth_r
        monkeypatch.setattr(settings, "ADMIN_EMAIL", "boss@example.com")
        # Password is long enough (≥12) but force is_common_password to return True.
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "NotShortButCommon!")
        monkeypatch.setattr(auth_r, "is_common_password", lambda _: True)
        fake_db.queue([{"u": {"id": "1"}}])  # already exists — skip create
        with caplog.at_level(logging.WARNING, logger="app.auth.router"):
            auth_r.bootstrap_admin()
        assert any(
            "common" in r.message.lower()
            for r in caplog.records
        ), "Expected a common-password warning in the log"


def test_register_duplicate_email_returns_generic_response(client, fake_db):
    # Duplicate registration must NOT reveal that the address exists (no 400 / "already
    # registered") — the response is indistinguishable from a successful new registration.
    fake_db.queue([{"u": {"id": "1"}}])  # existing user found
    with patch.object(auth_router, "send_account_exists_email") as send:
        r = client.post("/auth/register", json={"email": "dupe@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    body = r.json()
    assert body["verification_required"] is True
    assert "access_token" not in body
    # The account-exists notification is sent to the owner silently.
    send.assert_called_once_with("dupe@example.com")


def test_register_short_password_rejected(client, fake_db):
    r = client.post("/auth/register", json={"email": "a@example.com", "password": "short"})
    assert r.status_code == 400


def test_register_rejects_password_exceeding_bcrypt_byte_limit(client, fake_db):
    # 73 ASCII chars = 73 UTF-8 bytes — one over the 72-byte bcrypt limit.
    # Previously this was silently truncated; now it is explicitly rejected.
    r = client.post("/auth/register", json={"email": "a@example.com",
                                            "password": "a" * 73})
    assert r.status_code == 400
    assert "72" in r.json()["detail"]


def test_register_rejects_password_long_in_bytes_not_chars(client, fake_db):
    # 25 × '€' = 25 chars but 75 UTF-8 bytes (€ is 3 bytes) — over the limit.
    r = client.post("/auth/register", json={"email": "a@example.com",
                                            "password": "€" * 25})
    assert r.status_code == 400


def test_register_accepts_password_at_byte_limit(client, fake_db):
    # Exactly 72 ASCII chars = 72 bytes — should be accepted.
    fake_db.queue([], [{"n": 0}], [])
    r = client.post("/auth/register", json={"email": "a@example.com",
                                            "password": "a" * 72})
    assert r.status_code == 200


def test_register_rejects_common_password(client, fake_db):
    # A long-enough but very common password is refused by the blocklist.
    r = client.post("/auth/register", json={"email": "a@example.com", "password": "password123"})
    assert r.status_code == 400
    assert "too common" in r.json()["detail"].lower()


def test_register_invalid_email_rejected(client, fake_db):
    r = client.post("/auth/register", json={"email": "not-an-email", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 422  # EmailStr validation


def test_register_normalizes_email_to_lowercase(client, fake_db):
    fake_db.queue([], [{"n": 0}], [])
    r = client.post("/auth/register", json={"email": "Test@EXAMPLE.COM", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    # the existence-check query must have received the normalized email
    assert fake_db.calls[0][1]["e"] == "test@example.com"


# ── Login ──────────────────────────────────────────────────────────────────────

def _user_row(password="Zt9mQ2vLp4rK", role="viewer", email_verified=True):
    return [{"u": {
        "id": "u1", "email": "user@example.com", "role": role,
        "password_hash": hash_password(password), "email_verified": email_verified,
    }}]


def test_login_success_returns_token(client, fake_db):
    fake_db.queue(_user_row())
    r = client.post("/auth/login", json={"email": "user@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    assert r.json()["access_token"]
    assert r.json()["role"] == "viewer"


def test_login_wrong_password_rejected(client, fake_db):
    fake_db.queue(_user_row(password="rightpass"))
    r = client.post("/auth/login", json={"email": "user@example.com", "password": "wrongpass"})
    assert r.status_code == 401


def test_login_blocked_until_email_verified(client, fake_db):
    fake_db.queue(_user_row(email_verified=False))
    r = client.post("/auth/login", json={"email": "user@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "email_not_verified"


def test_login_allowed_unverified_when_requirement_disabled(client, fake_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", False)
    fake_db.queue(_user_row(email_verified=False))
    r = client.post("/auth/login", json={"email": "user@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_login_unknown_email_rejected(client, fake_db):
    fake_db.queue([])  # no user
    r = client.post("/auth/login", json={"email": "ghost@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 401


def test_login_rate_limited_after_repeated_failures(client, fake_db):
    for _ in range(5):
        fake_db.queue([])  # user not found each time
        r = client.post("/auth/login", json={"email": "target@example.com", "password": "Zt9mQ2vLp4rK"})
        assert r.status_code == 401
    # 6th attempt within the window is blocked before touching the DB
    r = client.post("/auth/login", json={"email": "target@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 429


# ── /auth/me and role guards ────────────────────────────────────────────────────

def test_me_requires_authentication(client):
    assert client.get("/auth/me").status_code == 401


def test_me_rejects_garbage_token(client):
    r = client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


def test_me_returns_identity_for_valid_token(client, make_token):
    tok = make_token(role="contributor", sub="u9", email="me@example.com")
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json() == {"id": "u9", "email": "me@example.com", "role": "contributor",
                        "email_verified": False}


# ── require_verified (any role, must be email-verified) ─────────────────────────

def test_require_verified_accepts_token_claim():
    from app.auth.dependencies import require_verified
    u = {"sub": "u1", "email": "a@example.com", "role": "viewer", "email_verified": True}
    assert require_verified(u) is u   # trusts the claim, no DB read


def test_require_verified_db_fallback_accepts_verified(fake_db):
    from app.auth.dependencies import require_verified
    fake_db.queue([{"v": True}])      # token lacks the claim → read the User node
    u = {"sub": "u1", "email": "a@example.com", "role": "viewer"}
    assert require_verified(u) is u


def test_require_verified_rejects_unverified(fake_db):
    import pytest
    from fastapi import HTTPException
    from app.auth.dependencies import require_verified
    fake_db.queue([{"v": False}])
    with pytest.raises(HTTPException) as ei:
        require_verified({"sub": "u1", "email": "a@example.com", "role": "viewer"})
    assert ei.value.status_code == 403


def test_ensure_endpoint_requires_auth(client):
    assert client.post("/scraper/ensure", json={"query": "Acme"}).status_code == 401


def test_ensure_endpoint_rejects_unverified(client, make_token, fake_db):
    fake_db.queue([{"v": False}])     # DB fallback says unverified
    tok = make_token(role="viewer")
    r = client.post("/scraper/ensure", json={"query": "Acme"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_admin_endpoint_rejects_anonymous(client):
    assert client.get("/auth/users").status_code == 401


def test_admin_endpoint_rejects_viewer(client, make_token):
    tok = make_token(role="viewer")
    assert client.get("/auth/users", headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_admin_endpoint_allows_admin(client, fake_db, make_token):
    fake_db.queue([{"id": "u1", "email": "a@example.com", "role": "admin",
                    "email_verified": True, "created_at": "2026"}])
    tok = make_token(role="admin")
    r = client.get("/auth/users", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()[0]["email"] == "a@example.com"


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


# ── Email verification + password reset ──────────────────────────────────────

from unittest.mock import patch  # noqa: E402
from app.auth.security import (  # noqa: E402
    create_purpose_token, password_hash_fingerprint,
)
from app.auth import router as auth_router  # noqa: E402
from datetime import timedelta  # noqa: E402


def test_register_sends_verification_email(client, fake_db):
    fake_db.queue([], [{"n": 3}], [])  # dup empty, count=3 -> viewer, create
    with patch.object(auth_router, "send_verification_email") as send:
        r = client.post("/auth/register", json={"email": "new@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    send.assert_called_once()
    to, token = send.call_args.args[0], send.call_args.args[1]
    assert to == "new@example.com"
    # the emailed token is a valid verify-email token
    claims = auth_router.verify_purpose_token(token, auth_router.VERIFY_EMAIL_PURPOSE)
    assert claims["sub"]


def test_verify_email_marks_verified(client, fake_db):
    token = create_purpose_token("u1", auth_router.VERIFY_EMAIL_PURPOSE, timedelta(hours=1))
    fake_db.queue([{"email": "new@example.com"}])   # the UPDATE ... RETURN email
    r = client.post("/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    assert "UPDATE" in fake_db.calls[0][0] or "SET u.email_verified = true" in fake_db.calls[0][0]


def test_verify_email_rejects_non_verify_token(client, make_token):
    # an access token has no verify_email purpose -> rejected
    r = client.post("/auth/verify-email", json={"token": make_token()})
    assert r.status_code == 400


def test_resend_verification_is_always_200_and_silent_for_unknown(client, fake_db):
    fake_db.queue([])   # no such user
    with patch.object(auth_router, "send_verification_email") as send:
        r = client.post("/auth/resend-verification", json={"email": "ghost@example.com"})
    assert r.status_code == 200
    send.assert_not_called()   # nothing sent, but no hint that the user is missing


def test_forgot_password_no_enumeration_but_sends_when_present(client, fake_db):
    # unknown email -> 200, no email
    fake_db.queue([])
    with patch.object(auth_router, "send_password_reset_email") as send:
        r = client.post("/auth/forgot-password", json={"email": "ghost@example.com"})
    assert r.status_code == 200 and not send.called
    # known email -> 200, email sent with a reset token
    fake_db.queue([{"id": "u1", "hash": hash_password("oldpassword")}])
    with patch.object(auth_router, "send_password_reset_email") as send:
        r = client.post("/auth/forgot-password", json={"email": "real@example.com"})
    assert r.status_code == 200 and send.called


def test_reset_password_updates_hash_then_self_invalidates(client, fake_db):
    old_hash = hash_password("oldpassword")
    token = create_purpose_token(
        "u1", auth_router.RESET_PASSWORD_PURPOSE, timedelta(minutes=30),
        extra={"ph": password_hash_fingerprint(old_hash)})

    # first use: lookup returns the old hash (fingerprint matches) -> update
    fake_db.queue([{"hash": old_hash}], [])
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "brandnewpass"})
    assert r.status_code == 200

    # reuse: the password hash has since changed, so the token's fingerprint no
    # longer matches -> rejected (single-use in practice)
    fake_db.queue([{"hash": hash_password("brandnewpass")}])
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "another12"})
    assert r.status_code == 400


def test_reset_password_short_password_rejected(client):
    token = create_purpose_token("u1", auth_router.RESET_PASSWORD_PURPOSE, timedelta(minutes=30),
                                 extra={"ph": "x"})
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "short"})
    assert r.status_code == 400


def test_reset_password_rejects_too_long_password(client):
    token = create_purpose_token("u1", auth_router.RESET_PASSWORD_PURPOSE, timedelta(minutes=30),
                                 extra={"ph": "x"})
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "a" * 73})
    assert r.status_code == 400
    assert "72" in r.json()["detail"]


def test_reset_password_rejects_common_password(client):
    token = create_purpose_token("u1", auth_router.RESET_PASSWORD_PURPOSE, timedelta(minutes=30),
                                 extra={"ph": "x"})
    r = client.post("/auth/reset-password", json={"token": token, "new_password": "password123"})
    assert r.status_code == 400
    assert "too common" in r.json()["detail"].lower()


def test_forgot_password_survives_email_send_failure(client, fake_db):
    # A blocked/failing transport (e.g. Render blocks SMTP) must not 500 or hang —
    # the send is best-effort in a background task.
    fake_db.queue([{"id": "u1", "hash": hash_password("oldpassword")}])
    with patch.object(auth_router, "send_password_reset_email", side_effect=RuntimeError("smtp blocked")):
        r = client.post("/auth/forgot-password", json={"email": "real@example.com"})
    assert r.status_code == 200


def test_register_survives_email_send_failure(client, fake_db):
    fake_db.queue([], [{"n": 3}], [])  # dup empty, count=3 -> viewer, create
    with patch.object(auth_router, "send_verification_email", side_effect=RuntimeError("smtp blocked")):
        r = client.post("/auth/register", json={"email": "new@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200 and r.json()["verification_required"] is True


def test_email_send_endpoints_are_rate_limited(client, fake_db):
    for _ in range(3):
        fake_db.queue([])
        assert client.post("/auth/forgot-password", json={"email": "spam@example.com"}).status_code == 200
    # 4th within the window is throttled
    assert client.post("/auth/forgot-password", json={"email": "spam@example.com"}).status_code == 429


# ── Two-factor auth (TOTP) ────────────────────────────────────────────────────

def _mfa_user_row(**over):
    u = {"id": "u1", "email": "user@example.com", "role": "viewer",
         "password_hash": hash_password("Zt9mQ2vLp4rK"), "email_verified": True,
         "mfa_enabled": True, "totp_secret": "SECRET", "recovery_code_hashes": []}
    u.update(over)
    return [{"u": u}]


def test_login_with_mfa_returns_pending_token_not_access(client, fake_db):
    fake_db.queue(_mfa_user_row())
    r = client.post("/auth/login", json={"email": "user@example.com", "password": "Zt9mQ2vLp4rK"})
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_required"] is True and body["mfa_token"]
    assert "access_token" not in body


def test_mfa_setup_returns_secret_and_uri(client, fake_db, make_token):
    tok = make_token(sub="u1")
    fake_db.queue([{"email": "user@example.com"}])   # the SET ... RETURN email
    r = client.post("/auth/mfa/setup", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["secret"] and r.json()["otpauth_uri"].startswith("otpauth://totp/")


def test_mfa_enable_confirms_code_and_returns_recovery_codes(client, fake_db, make_token):
    tok = make_token(sub="u1")
    fake_db.queue(_mfa_user_row(mfa_enabled=False, mfa_pending_secret="PENDING"), [])
    with patch.object(auth_router, "verify_totp", return_value=True):
        r = client.post("/auth/mfa/enable", json={"code": "123456"},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["enabled"] is True and len(r.json()["recovery_codes"]) == 10


def test_mfa_enable_rejects_bad_code(client, fake_db, make_token):
    tok = make_token(sub="u1")
    fake_db.queue(_mfa_user_row(mfa_enabled=False, mfa_pending_secret="PENDING"))
    with patch.object(auth_router, "verify_totp", return_value=False):
        r = client.post("/auth/mfa/enable", json={"code": "000000"},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400


def test_mfa_verify_with_totp_returns_access_token(client, fake_db):
    from app.auth.security import create_purpose_token
    from datetime import timedelta
    mfa_token = create_purpose_token("u1", auth_router.MFA_PENDING_PURPOSE, timedelta(minutes=5))
    fake_db.queue(_mfa_user_row())
    with patch.object(auth_router, "verify_totp", return_value=True):
        r = client.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "123456"})
    assert r.status_code == 200 and r.json()["access_token"]


def test_mfa_verify_consumes_a_recovery_code(client, fake_db):
    from app.auth.security import create_purpose_token, hash_recovery_code
    from datetime import timedelta
    mfa_token = create_purpose_token("u1", auth_router.MFA_PENDING_PURPOSE, timedelta(minutes=5))
    good = hash_recovery_code("aaaaa-bbbbb")
    # lookup returns the user (TOTP will fail via patch), then the SET consuming the code
    fake_db.queue(_mfa_user_row(recovery_code_hashes=[good]), [])
    with patch.object(auth_router, "verify_totp", return_value=False):
        r = client.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "aaaaa-bbbbb"})
    assert r.status_code == 200 and r.json()["access_token"]
    # the consuming UPDATE dropped the used hash
    set_call = [c for c in fake_db.calls if "recovery_code_hashes = $h" in c[0]][-1]
    assert set_call[1]["h"] == []


def test_mfa_verify_rejects_bad_code(client, fake_db):
    from app.auth.security import create_purpose_token
    from datetime import timedelta
    mfa_token = create_purpose_token("u1", auth_router.MFA_PENDING_PURPOSE, timedelta(minutes=5))
    fake_db.queue(_mfa_user_row(recovery_code_hashes=[]))
    with patch.object(auth_router, "verify_totp", return_value=False):
        r = client.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "999999"})
    assert r.status_code == 401


def test_mfa_verify_rejects_non_mfa_token(client, make_token):
    r = client.post("/auth/mfa/verify", json={"mfa_token": make_token(), "code": "123456"})
    assert r.status_code == 400


def test_mfa_disable_requires_a_valid_code(client, fake_db, make_token):
    tok = make_token(sub="u1")
    fake_db.queue(_mfa_user_row())
    with patch.object(auth_router, "verify_totp", return_value=False):
        r = client.post("/auth/mfa/disable", json={"code": "000000"},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400


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
