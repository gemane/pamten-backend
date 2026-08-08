import logging
import uuid
from datetime import timedelta
from fastapi import APIRouter, HTTPException, Depends, Request, Response, BackgroundTasks
from pydantic import BaseModel, EmailStr, field_validator
from app.config import settings
from app.database import db
from app.auth import refresh as refresh_store
from app.auth.security import (
    hash_password, verify_password, create_access_token,
    create_purpose_token, verify_purpose_token, password_hash_fingerprint, TokenError,
    generate_totp_secret, totp_provisioning_uri, verify_totp,
    generate_recovery_codes, hash_recovery_code,
)
from app.auth.dependencies import get_current_user, require_admin
from app.auth.password_policy import is_common_password, password_policy_error
from app.notifications.i18n import DEFAULT_LANGUAGE, normalize as normalize_language
from app.auth.rate_limit import check_rate_limit, record_attempt, clear_attempts
from app.notifications.email import (
    send_verification_email, send_password_reset_email, send_account_exists_email,
)

# The user-password rules (min length, bcrypt byte cap, blocklist) live in
# app.auth.password_policy — one definition, shared with manage.py set-password.
# Admin accounts warrant a higher floor than regular users because they can do
# anything in the system. Applied as a startup warning rather than a hard error
# so a dev environment with a simple ADMIN_PASSWORD can still boot.
MIN_ADMIN_PASSWORD_LEN = 12
VERIFY_EMAIL_PURPOSE = "verify_email"
RESET_PASSWORD_PURPOSE = "pwd_reset"


def _validate_password(password: str) -> None:
    """Enforce the password policy on register / reset / change.

    The rules themselves live in ``app.auth.password_policy`` so the CLI applies
    exactly the same ones; this only turns a violation into an HTTP 400.
    """
    message = password_policy_error(password)
    if message:
        raise HTTPException(status_code=400, detail=message)


MFA_PENDING_PURPOSE = "mfa_pending"
MFA_PENDING_TTL_MINUTES = 5

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])


def bootstrap_admin() -> None:
    """Provision the ADMIN_EMAIL account as admin on startup, if configured.
    Idempotent (only creates when the email doesn't exist — never overwrites a
    later password change) and best-effort (never crashes startup). This removes
    the 'first person to hit /register becomes admin' race on a fresh DB."""
    email = (settings.ADMIN_EMAIL or "").strip().lower()
    password = settings.ADMIN_PASSWORD
    if not email or not password:
        return

    # Warn if ADMIN_PASSWORD is weak — we can't return a 400 here, but the
    # warning shows up in the Render log and CI output so operators notice early.
    # The length check is checked first; if it fires the common-password check
    # is redundant (any password < 12 chars should be changed anyway).
    if len(password) < MIN_ADMIN_PASSWORD_LEN:
        log.warning(
            "ADMIN_PASSWORD is only %d characters — use at least %d to protect "
            "the admin account (generate with: openssl rand -base64 16).",
            len(password), MIN_ADMIN_PASSWORD_LEN,
        )
    elif is_common_password(password):
        log.warning(
            "ADMIN_PASSWORD appears to be a very common password. "
            "Use a strong, unique password to protect the admin account.",
        )

    try:
        with db.get_session() as session:
            if session.run("MATCH (u:User {email: $e}) RETURN u", e=email).single():
                return  # already exists — leave it (respects password changes)
            session.run(
                """
                CREATE (u:User {
                    id: $id, email: $email, password_hash: $hash,
                    role: 'admin', email_verified: true,
                    created_at: toString(datetime())
                })
                """,
                id=str(uuid.uuid4()), email=email, hash=hash_password(password),
            )
        log.info("Bootstrapped admin user %s from ADMIN_EMAIL", email)
    except Exception as exc:  # noqa: BLE001 - never fail startup on this
        log.warning("Admin bootstrap skipped: %s", exc)

LOGIN_RATE_LIMIT = 5           # attempts
LOGIN_RATE_WINDOW = 15 * 60    # seconds

# Throttle outbound email (verification resend / password reset) per address, so
# the endpoints can't be used to spam an inbox. Applied before any user lookup so
# it never reveals whether an address exists.
EMAIL_SEND_LIMIT = 3
EMAIL_SEND_WINDOW = 15 * 60


def _rate_limit_email(email: str) -> None:
    key = f"email:{email}"
    check_rate_limit(key, EMAIL_SEND_LIMIT, EMAIL_SEND_WINDOW)
    record_attempt(key, EMAIL_SEND_WINDOW)


class _EmailPasswordRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class RegisterRequest(_EmailPasswordRequest):
    pass


class LoginRequest(_EmailPasswordRequest):
    pass


class _EmailOnlyRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class VerifyEmailRequest(BaseModel):
    token: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class DeleteAccountRequest(BaseModel):
    password: str


def _safe_send(send_fn, *args) -> None:
    """Run an email send best-effort — the transport can be slow or blocked (e.g.
    Render blocks outbound SMTP), and that must never fail the user's request or
    leak whether an account exists. Failures are logged, not raised."""
    try:
        send_fn(*args)
    except Exception as exc:  # noqa: BLE001
        log.warning("email send failed (%s): %s", getattr(send_fn, "__name__", send_fn), exc)


# The UI's current language, sent by the frontend on every request. Not
# Accept-Language: that reflects the browser's configured languages, which say
# nothing about the in-app language switcher — a German site in an English
# browser would otherwise email in English.
LANGUAGE_HEADER = "X-Owlgraph-Language"


def _request_language(request: Request) -> str:
    return normalize_language(request.headers.get(LANGUAGE_HEADER))


def _user_language(session, user_id: str, fallback: str) -> str:
    """The account owner's own language, for mail addressed to them.

    A password reset or an account-exists notice goes to the account holder,
    who may not be the person who triggered it — the requester could be a
    stranger probing for accounts. Their stored preference wins; the requester's
    language is only the fallback for accounts predating this field.
    """
    try:
        rec = session.run("MATCH (u:User {id: $id}) RETURN u.language AS lang",
                          id=user_id).single()
    except Exception:  # noqa: BLE001 — never block an email on this lookup
        return fallback
    return normalize_language(rec["lang"]) if rec and rec["lang"] else fallback


def _issue_verification_email(background: BackgroundTasks, user_id: str, email: str,
                              language: str = DEFAULT_LANGUAGE) -> None:
    # Token minted synchronously (cheap); the actual send runs after the response
    # so a slow/blocked transport can't hang or 500 the endpoint.
    token = create_purpose_token(
        user_id, VERIFY_EMAIL_PURPOSE, timedelta(hours=settings.EMAIL_VERIFY_TTL_HOURS))
    background.add_task(_safe_send, send_verification_email, email, token, language)


def _issue_password_reset_email(background: BackgroundTasks, user_id: str, email: str,
                                password_hash: str,
                                language: str = DEFAULT_LANGUAGE) -> None:
    token = create_purpose_token(
        user_id, RESET_PASSWORD_PURPOSE, timedelta(minutes=settings.PASSWORD_RESET_TTL_MINUTES),
        extra={"ph": password_hash_fingerprint(password_hash)})
    background.add_task(_safe_send, send_password_reset_email, email, token, language)


def _set_refresh_cookie(response: Response, raw: str) -> None:
    """Hand the refresh token to the browser as an httpOnly cookie.

    httpOnly keeps it out of reach of JavaScript, so an XSS bug cannot steal the
    long-lived credential — the short-lived access token in memory is all that is
    exposed. SameSite=Lax is sufficient because the frontend and API sit on one
    registrable domain (dev.owlgraph.org / api-dev.owlgraph.org), making their
    requests same-site even though they are cross-origin; see render.yaml.
    """
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=raw,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=settings.REFRESH_COOKIE_SECURE,
        samesite="lax",
        # Empty setting → host-only cookie (omit the attribute entirely).
        domain=settings.REFRESH_COOKIE_DOMAIN or None,
        path="/",
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Expire the cookie. The attributes must match those it was set with, or the
    browser treats it as a different cookie and leaves the original in place."""
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        httponly=True,
        secure=settings.REFRESH_COOKIE_SECURE,
        samesite="lax",
        domain=settings.REFRESH_COOKIE_DOMAIN or None,
        path="/",
    )


def _session_expired() -> HTTPException:
    """A 401 that also clears the refresh cookie.

    The Set-Cookie has to travel on the exception itself: headers written to the
    injected ``Response`` are discarded when an HTTPException is raised, because
    the error response is constructed fresh by the exception handler. Without
    this the browser keeps replaying a token that can never work again.
    """
    scratch = Response()
    _clear_refresh_cookie(scratch)
    return HTTPException(
        status_code=401,
        detail="Session expired. Please log in again.",
        headers={"set-cookie": scratch.headers["set-cookie"]},
    )


def _access_token_payload(user_id: str, email: str, role: str, email_verified: bool) -> dict:
    token = create_access_token(
        {"sub": user_id, "email": email, "role": role, "email_verified": bool(email_verified)})
    return {"access_token": token, "token_type": "bearer", "email": email, "role": role,
            "email_verified": bool(email_verified),
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60}


def _token_response(response: Response, user_id: str, email: str, role: str,
                    email_verified: bool = False):
    """Start a session: a short-lived access token in the body, a refresh token
    in an httpOnly cookie.

    Issuing the refresh token is best-effort — if the store is unreachable the
    caller still gets a usable access token rather than being unable to log in
    at all. The consequence is a session that ends when that token expires.
    """
    try:
        _set_refresh_cookie(response, refresh_store.issue(user_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not issue refresh token for %s: %s", user_id, exc)
    return _access_token_payload(user_id, email, role, email_verified)


def _login_rate_limit_key(request: Request, email: str) -> str:
    client_ip = request.client.host if request.client else "unknown"
    return f"login:{client_ip}:{email}"


def _check_login_rate_limit(key: str) -> None:
    check_rate_limit(key, LOGIN_RATE_LIMIT, LOGIN_RATE_WINDOW)


def _record_login_failure(key: str) -> None:
    record_attempt(key, LOGIN_RATE_WINDOW)


def _clear_login_attempts(key: str) -> None:
    clear_attempts(key)


@router.post("/register")
def register(data: RegisterRequest, background: BackgroundTasks, response: Response,
             request: Request):
    _validate_password(data.password)
    language = _request_language(request)

    # Generic response used for both new and duplicate registrations — never
    # reveals whether the address already has an account (email enumeration).
    _REGISTER_OK = {
        "message": "If this email is new, a verification link has been sent. Check your inbox.",
        "verification_required": True,
    }

    with db.get_session() as session:
        rec_existing = session.run(
                "MATCH (u:User {email: $e}) RETURN u.id AS id", e=data.email).single()
        if rec_existing:
            # Address already registered — notify the owner silently and return
            # the same generic response so the caller learns nothing.
            owner_language = _user_language(session, rec_existing["id"], language)
            background.add_task(_safe_send, send_account_exists_email, data.email,
                                owner_language)
            return {**_REGISTER_OK, "email": data.email}

        # When an admin is provisioned from env (ADMIN_EMAIL), self-registration
        # NEVER grants admin — closing the "first registrant becomes admin" hole.
        # Otherwise fall back to the legacy bootstrap: the first user is admin.
        if settings.ADMIN_EMAIL:
            role = "viewer"
        else:
            count = session.run("MATCH (u:User) RETURN count(u) AS n").single()["n"]
            role = "admin" if count == 0 else "viewer"
        user_id = str(uuid.uuid4())
        # The legacy first-user admin is auto-verified so a fresh DB with no SMTP
        # configured still yields a usable admin; everyone else must verify.
        verified = role == "admin"

        session.run(
            """
            CREATE (u:User {
                id: $id, email: $email, password_hash: $hash,
                role: $role, email_verified: $verified,
                language: $language,
                created_at: toString(datetime())
            })
            """,
            id=user_id, email=data.email,
            hash=hash_password(data.password), role=role, verified=verified,
            language=language,
        )

    if verified:
        return _token_response(response, user_id, data.email, role, email_verified=True)

    _issue_verification_email(background, user_id, data.email, language)
    return {**_REGISTER_OK, "email": data.email}


@router.post("/login")
def login(data: LoginRequest, request: Request, response: Response):
    rate_limit_key = _login_rate_limit_key(request, data.email)
    _check_login_rate_limit(rate_limit_key)

    with db.get_session() as session:
        rec = session.run("MATCH (u:User {email: $e}) RETURN u", e=data.email).single()
        if not rec:
            _record_login_failure(rate_limit_key)
            raise HTTPException(status_code=401, detail="Invalid email or password")
        user = dict(rec["u"])

    if not verify_password(data.password, user["password_hash"]):
        _record_login_failure(rate_limit_key)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    _clear_login_attempts(rate_limit_key)

    # Block login until the email is verified. `email_verified` is absent on
    # accounts created before this feature — treat missing as unverified, but the
    # env admin (bootstrap) and legacy first-user admin are stamped verified.
    if settings.REQUIRE_EMAIL_VERIFICATION and not user.get("email_verified"):
        raise HTTPException(
            status_code=403,
            detail={"code": "email_not_verified",
                    "message": "Please verify your email before logging in."})

    # Second factor: password alone isn't enough once MFA is on — hand back a
    # short-lived pending token the client exchanges (with a TOTP / recovery code)
    # at /auth/mfa/verify for the real access token.
    if user.get("mfa_enabled"):
        pending = create_purpose_token(user["id"], MFA_PENDING_PURPOSE,
                                       timedelta(minutes=MFA_PENDING_TTL_MINUTES))
        return {"mfa_required": True, "mfa_token": pending}

    return _token_response(response, user["id"], user["email"], user["role"],
                           email_verified=bool(user.get("email_verified")))


@router.post("/refresh")
def refresh_session(request: Request, response: Response):
    """Trade the refresh cookie for a new access token, rotating the cookie.

    Unauthenticated by design: it is called precisely when the access token has
    expired, so requiring one would make it useless. The cookie is the credential.

    Every refresh re-reads the account rather than trusting the old token's
    claims, so a role change, a revoked verification, or a deleted account takes
    effect within one access-token lifetime instead of lingering for the life of
    the session.
    """
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        user_id, new_raw = refresh_store.rotate(raw)
    except refresh_store.RefreshError:
        raise _session_expired()

    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {id: $id}) RETURN u.email AS email, u.role AS role, "
            "u.email_verified AS verified", id=user_id,
        ).single()
    if not rec:
        # Account deleted while the session was alive — retire the successor we
        # just minted rather than leaving a valid token for a missing user.
        refresh_store.revoke(new_raw)
        raise _session_expired()

    _set_refresh_cookie(response, new_raw)
    return _access_token_payload(user_id, rec["email"], rec["role"],
                                 bool(rec["verified"]))


@router.post("/logout")
def logout(request: Request, response: Response):
    """End this session. Unauthenticated for the same reason as /refresh — an
    expired access token must not prevent logging out — and idempotent, so a
    stale or absent cookie still reports success."""
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if raw:
        refresh_store.revoke(raw)
    _clear_refresh_cookie(response)
    return {"message": "Logged out."}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    """The signed-in user.

    `email_verified` normally comes from the token claim added at issue time. When
    the claim is absent — a token minted before it existed — fall back to the User
    node instead of reporting False. Reporting False was described as harmless
    because it "only gates the on-demand scrape UI", but the effect is that the
    scrape option silently disappears for a verified user with no explanation,
    which is indistinguishable from the feature being broken. Same fallback
    require_verified already does; costs one indexed read on legacy tokens only.
    """
    verified = user.get("email_verified")
    if verified is None:
        with db.get_session() as session:
            rec = session.run(
                "MATCH (u:User {id: $id}) RETURN u.email_verified AS v", id=user["sub"],
            ).single()
        verified = bool(rec["v"]) if rec else False
    return {"id": user["sub"], "email": user["email"], "role": user["role"],
            "email_verified": bool(verified)}


@router.post("/verify-email")
def verify_email(data: VerifyEmailRequest):
    try:
        claims = verify_purpose_token(data.token, VERIFY_EMAIL_PURPOSE)
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {id: $id}) SET u.email_verified = true "
            "RETURN u.email AS email", id=claims["sub"],
        ).single()
        if not rec:
            raise HTTPException(status_code=400, detail="Invalid link")
    return {"message": "Email verified. You can now log in.", "email": rec["email"]}


@router.post("/resend-verification")
def resend_verification(data: _EmailOnlyRequest, background: BackgroundTasks,
                        request: Request):
    """Re-send the verification link. Always returns 200 (never reveals whether
    the address exists or is already verified)."""
    _rate_limit_email(data.email)
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {email: $e}) RETURN u.id AS id, u.email_verified AS verified",
            e=data.email,
        ).single()
        language = (_user_language(session, rec["id"], _request_language(request))
                    if rec else DEFAULT_LANGUAGE)
    if rec and not rec["verified"]:
        _issue_verification_email(background, rec["id"], data.email, language)
    return {"message": "If that account exists and is unverified, a verification email was sent."}


@router.post("/forgot-password")
def forgot_password(data: _EmailOnlyRequest, background: BackgroundTasks,
                    request: Request):
    """Send a password-reset link. Always returns 200 — no user enumeration."""
    _rate_limit_email(data.email)
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {email: $e}) RETURN u.id AS id, u.password_hash AS hash",
            e=data.email,
        ).single()
        language = (_user_language(session, rec["id"], _request_language(request))
                    if rec else DEFAULT_LANGUAGE)
    if rec:
        _issue_password_reset_email(background, rec["id"], data.email, rec["hash"], language)
    return {"message": "If that account exists, a password-reset email was sent."}


@router.post("/reset-password")
def reset_password(data: ResetPasswordRequest):
    _validate_password(data.new_password)
    try:
        claims = verify_purpose_token(data.token, RESET_PASSWORD_PURPOSE)
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {id: $id}) RETURN u.password_hash AS hash", id=claims["sub"],
        ).single()
        if not rec:
            raise HTTPException(status_code=400, detail="Invalid link")
        # Self-invalidation: the token embedded a fingerprint of the password hash
        # at issue time. If it no longer matches, the link was already used or a
        # newer reset was requested — reject it.
        if claims.get("ph") != password_hash_fingerprint(rec["hash"]):
            raise HTTPException(status_code=400, detail="This reset link is no longer valid.")
        # Resetting the password also proves email ownership → mark verified.
        session.run(
            "MATCH (u:User {id: $id}) SET u.password_hash = $hash, u.email_verified = true",
            id=claims["sub"], hash=hash_password(data.new_password),
        )
    # A reset is the "I lost control of this account" path, so every existing
    # session ends here — including one an attacker may be holding.
    refresh_store.revoke_all_for_user(claims["sub"])
    return {"message": "Password updated. You can now log in."}


@router.post("/change-password")
def change_password(data: ChangePasswordRequest, response: Response,
                    user: dict = Depends(get_current_user)):
    """Change your own password, proving ownership with the current one.

    This is the only self-service route for a signed-in user: /forgot-password
    depends on email delivery, which is unavailable wherever outbound SMTP is
    blocked (Render), so without this an account whose password is known but
    unwanted could never be rotated from the UI.

    Other sessions are signed out, then *this* one is re-established — a password
    change should evict anyone else holding the old one without logging the user
    out of the browser they just used.

    Already-issued access tokens are the exception: they are stateless and
    checked against nothing, so another session stays usable until its token
    expires (ACCESS_TOKEN_EXPIRE_MINUTES). Bounding that window is the reason
    the access-token TTL is short.
    """
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {id: $id}) RETURN u.password_hash AS hash", id=user["sub"],
        ).single()
        if not rec:
            raise HTTPException(status_code=404, detail="User not found")
        # Check the current password before the policy check on the new one, so a
        # caller without the current password learns nothing about the policy.
        if not verify_password(data.current_password, rec["hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        _validate_password(data.new_password)
        if verify_password(data.new_password, rec["hash"]):
            raise HTTPException(status_code=400,
                                detail="New password must be different from the current one")
        session.run(
            "MATCH (u:User {id: $id}) SET u.password_hash = $hash",
            id=user["sub"], hash=hash_password(data.new_password),
        )
    refresh_store.revoke_all_for_user(user["sub"])
    try:
        _set_refresh_cookie(response, refresh_store.issue(user["sub"]))
    except Exception as exc:  # noqa: BLE001 — the password change itself succeeded
        log.warning("could not re-issue session after password change: %s", exc)
    return {"message": "Password updated."}


def _purge_user_rate_limits(email: str, user_id: str) -> None:
    """Drop the RateLimit rows keyed to a deleted account.

    Keys are built in a few shapes (see the helpers above): ``user:<id>``,
    ``mfa:<id>``, ``email:<addr>`` and ``login:<ip>:<addr>``. The login key
    embeds the client IP, so that one has to be matched by suffix. Best-effort —
    a leftover counter must never keep the deletion itself from completing.
    """
    from app.db.arcadedb import run_sql

    for query, params in (
        ("DELETE FROM RateLimit WHERE key = :k", {"k": f"user:{user_id}"}),
        ("DELETE FROM RateLimit WHERE key = :k", {"k": f"mfa:{user_id}"}),
        ("DELETE FROM RateLimit WHERE key = :k", {"k": f"email:{email}"}),
        ("DELETE FROM RateLimit WHERE key LIKE :k", {"k": f"login:%:{email}"}),
    ):
        try:
            run_sql(query, params)
        except Exception as exc:  # noqa: BLE001 - never block the deletion
            log.warning("rate-limit purge failed on account deletion: %s", exc)


@router.delete("/me")
def delete_own_account(data: DeleteAccountRequest, response: Response,
                       user: dict = Depends(get_current_user)):
    """Delete your own account, permanently.

    Required by both app stores for any app that lets users create an account,
    and the mechanism behind a GDPR erasure request. Re-authenticates with the
    password so a stolen access token alone can't destroy an account.

    What goes: the User node (with it the password hash, TOTP secret and recovery
    codes), the account's refresh tokens, and its rate-limit counters. Flags the
    user filed are *anonymised*, not deleted — the reports are about companies,
    not about the reporter, and dropping them would silently rewrite moderation
    history; only the link back to the person is severed.
    """
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {id: $id}) RETURN u.password_hash AS hash, u.email AS email, "
            "u.role AS role",
            id=user["sub"],
        ).single()
        if not rec:
            raise HTTPException(status_code=404, detail="User not found")
        if not verify_password(data.password, rec["hash"]):
            raise HTTPException(status_code=400, detail="Password is incorrect")

        email = (rec["email"] or "").strip().lower()

        # The env-provisioned admin is recreated by bootstrap_admin() on the next
        # startup, so "deleting" it would quietly undo itself — refuse rather than
        # promise an erasure that won't hold.
        if settings.ADMIN_EMAIL and email == settings.ADMIN_EMAIL.strip().lower():
            raise HTTPException(
                status_code=400,
                detail="This account is provisioned from ADMIN_EMAIL and would be recreated on "
                       "the next restart. Unset ADMIN_EMAIL first, then delete it.",
            )

        # Don't let the instance end up with nobody who can administer it.
        if rec["role"] == "admin":
            others = session.run(
                "MATCH (u:User) WHERE u.role = 'admin' AND u.id <> $id RETURN count(u) AS n",
                id=user["sub"],
            ).single()
            if not others or (others["n"] or 0) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="You are the only admin. Promote another user to admin before "
                           "deleting this account.",
                )

        session.run(
            "MATCH (f:Flag {reporter_id: $id}) "
            "SET f.reporter_kind = 'deleted', f.reporter_id = '', f.reporter_fp = ''",
            id=user["sub"],
        )
        session.run("MATCH (u:User {id: $id}) DELETE u", id=user["sub"])

    refresh_store.delete_all_for_user(user["sub"])
    _clear_refresh_cookie(response)
    _purge_user_rate_limits(email, user["sub"])
    log.info("Account deleted (self-service): %s", email)
    return {"message": "Your account has been deleted."}


# ── Two-factor auth (TOTP) ────────────────────────────────────────────────────

class MfaCodeRequest(BaseModel):
    code: str


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str


def _load_user(session, user_id: str) -> dict | None:
    rec = session.run("MATCH (u:User {id: $id}) RETURN u", id=user_id).single()
    return dict(rec["u"]) if rec else None


@router.get("/mfa/status")
def mfa_status(user: dict = Depends(get_current_user)):
    with db.get_session() as session:
        u = _load_user(session, user["sub"])
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return {"mfa_enabled": bool(u.get("mfa_enabled"))}


@router.post("/mfa/setup")
def mfa_setup(user: dict = Depends(get_current_user)):
    """Start enrolment: stash a pending secret, return the otpauth URI + secret so
    the client can show a QR / manual key. Not active until /mfa/enable confirms a
    code."""
    secret = generate_totp_secret()
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {id: $id}) SET u.mfa_pending_secret = $s RETURN u.email AS email",
            id=user["sub"], s=secret,
        ).single()
        if not rec:
            raise HTTPException(status_code=404, detail="User not found")
    return {"secret": secret, "otpauth_uri": totp_provisioning_uri(secret, rec["email"])}


@router.post("/mfa/enable")
def mfa_enable(data: MfaCodeRequest, user: dict = Depends(get_current_user)):
    """Confirm enrolment with a code from the authenticator, then return the
    one-time recovery codes (shown once)."""
    with db.get_session() as session:
        u = _load_user(session, user["sub"])
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        if u.get("mfa_enabled"):
            raise HTTPException(status_code=400, detail="Two-factor auth is already enabled")
        pending = u.get("mfa_pending_secret")
        if not pending:
            raise HTTPException(status_code=400, detail="Start setup first")
        if not verify_totp(pending, data.code):
            raise HTTPException(status_code=400, detail="That code isn't valid. Try again.")
        codes = generate_recovery_codes()
        session.run(
            "MATCH (u:User {id: $id}) SET u.mfa_enabled = true, u.totp_secret = $s, "
            "u.mfa_pending_secret = '', u.recovery_code_hashes = $h",
            id=user["sub"], s=pending, h=[hash_recovery_code(c) for c in codes],
        )
    return {"enabled": True, "recovery_codes": codes}


@router.post("/mfa/disable")
def mfa_disable(data: MfaCodeRequest, user: dict = Depends(get_current_user)):
    """Turn MFA off — requires a current authenticator or recovery code, so a
    hijacked session alone can't remove the second factor."""
    with db.get_session() as session:
        u = _load_user(session, user["sub"])
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        if not u.get("mfa_enabled"):
            return {"enabled": False}
        ok = (verify_totp(u.get("totp_secret") or "", data.code)
              or hash_recovery_code(data.code) in (u.get("recovery_code_hashes") or []))
        if not ok:
            raise HTTPException(status_code=400, detail="That code isn't valid.")
        session.run(
            "MATCH (u:User {id: $id}) SET u.mfa_enabled = false, u.totp_secret = '', "
            "u.mfa_pending_secret = '', u.recovery_code_hashes = $empty",
            id=user["sub"], empty=[],
        )
    return {"enabled": False}


@router.post("/mfa/verify")
def mfa_verify(data: MfaVerifyRequest, response: Response):
    """Exchange the login-issued pending token + a TOTP (or recovery) code for the
    real access token. Rate-limited per account against code brute-force."""
    try:
        claims = verify_purpose_token(data.mfa_token, MFA_PENDING_PURPOSE)
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    key = f"mfa:{claims['sub']}"
    _check_login_rate_limit(key)
    with db.get_session() as session:
        u = _load_user(session, claims["sub"])
        if not u or not u.get("mfa_enabled"):
            raise HTTPException(status_code=400, detail="Invalid request")

        if verify_totp(u.get("totp_secret") or "", data.code):
            _clear_login_attempts(key)
            return _token_response(response, u["id"], u["email"], u["role"],
                                   email_verified=bool(u.get("email_verified")))

        # Recovery code — single use, so consume it on success.
        digest = hash_recovery_code(data.code)
        remaining = list(u.get("recovery_code_hashes") or [])
        if digest in remaining:
            remaining.remove(digest)
            session.run("MATCH (u:User {id: $id}) SET u.recovery_code_hashes = $h",
                        id=u["id"], h=remaining)
            _clear_login_attempts(key)
            return _token_response(response, u["id"], u["email"], u["role"],
                                   email_verified=bool(u.get("email_verified")))

    _record_login_failure(key)
    raise HTTPException(status_code=401, detail="Invalid code")


class RoleRequest(BaseModel):
    role: str


@router.get("/users")
def list_users(_: dict = Depends(require_admin)):
    with db.get_session() as session:
        result = session.run(
            "MATCH (u:User) RETURN u.id AS id, u.email AS email, u.role AS role, "
            "u.email_verified AS email_verified, u.created_at AS created_at ORDER BY u.created_at"
        )
        return [{"id": r["id"], "email": r["email"], "role": r["role"],
                 "email_verified": bool(r["email_verified"]), "created_at": r["created_at"]}
                for r in result]


@router.patch("/users/{user_id}/role")
def update_user_role(user_id: str, data: RoleRequest, _: dict = Depends(require_admin)):
    if data.role not in ("admin", "moderator", "contributor", "viewer"):
        raise HTTPException(status_code=400, detail="Role must be admin, moderator, contributor, or viewer")
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {id: $id}) SET u.role = $role RETURN u.id AS id",
            id=user_id, role=data.role,
        ).single()
        if not rec:
            raise HTTPException(status_code=404, detail="User not found")
    return {"message": "Role updated"}


@router.delete("/users/{user_id}")
def delete_user(user_id: str, current: dict = Depends(require_admin)):
    if current["sub"] == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    with db.get_session() as session:
        session.run("MATCH (u:User {id: $id}) DELETE u", id=user_id)
    return {"message": "User deleted"}
