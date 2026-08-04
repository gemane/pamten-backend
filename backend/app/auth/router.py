import logging
import uuid
from datetime import timedelta
from fastapi import APIRouter, HTTPException, Depends, Request, BackgroundTasks
from pydantic import BaseModel, EmailStr, field_validator
from app.config import settings
from app.database import db
from app.auth.security import (
    hash_password, verify_password, create_access_token,
    create_purpose_token, verify_purpose_token, password_hash_fingerprint, TokenError,
    generate_totp_secret, totp_provisioning_uri, verify_totp,
    generate_recovery_codes, hash_recovery_code,
)
from app.auth.dependencies import get_current_user, require_admin
from app.auth.password_policy import is_common_password
from app.auth.rate_limit import check_rate_limit, record_attempt, clear_attempts
from app.notifications.email import send_verification_email, send_password_reset_email

MIN_PASSWORD_LENGTH = 8
VERIFY_EMAIL_PURPOSE = "verify_email"
RESET_PASSWORD_PURPOSE = "pwd_reset"


def _validate_password(password: str) -> None:
    """Enforce the password policy (length + common-password blocklist) for user-chosen
    passwords on register/reset. Raises HTTPException(400) with a user-facing message."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400,
                            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if is_common_password(password):
        raise HTTPException(status_code=400,
                            detail="This password is too common — please choose a less common one.")
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


def _safe_send(send_fn, *args) -> None:
    """Run an email send best-effort — the transport can be slow or blocked (e.g.
    Render blocks outbound SMTP), and that must never fail the user's request or
    leak whether an account exists. Failures are logged, not raised."""
    try:
        send_fn(*args)
    except Exception as exc:  # noqa: BLE001
        log.warning("email send failed (%s): %s", getattr(send_fn, "__name__", send_fn), exc)


def _issue_verification_email(background: BackgroundTasks, user_id: str, email: str) -> None:
    # Token minted synchronously (cheap); the actual send runs after the response
    # so a slow/blocked transport can't hang or 500 the endpoint.
    token = create_purpose_token(
        user_id, VERIFY_EMAIL_PURPOSE, timedelta(hours=settings.EMAIL_VERIFY_TTL_HOURS))
    background.add_task(_safe_send, send_verification_email, email, token)


def _issue_password_reset_email(background: BackgroundTasks, user_id: str, email: str,
                                password_hash: str) -> None:
    token = create_purpose_token(
        user_id, RESET_PASSWORD_PURPOSE, timedelta(minutes=settings.PASSWORD_RESET_TTL_MINUTES),
        extra={"ph": password_hash_fingerprint(password_hash)})
    background.add_task(_safe_send, send_password_reset_email, email, token)


def _token_response(user_id: str, email: str, role: str, email_verified: bool = False):
    token = create_access_token(
        {"sub": user_id, "email": email, "role": role, "email_verified": bool(email_verified)})
    return {"access_token": token, "token_type": "bearer", "email": email, "role": role,
            "email_verified": bool(email_verified)}


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
def register(data: RegisterRequest, background: BackgroundTasks):
    _validate_password(data.password)

    with db.get_session() as session:
        if session.run("MATCH (u:User {email: $e}) RETURN u", e=data.email).single():
            raise HTTPException(status_code=400, detail="Email already registered")

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
                created_at: toString(datetime())
            })
            """,
            id=user_id, email=data.email,
            hash=hash_password(data.password), role=role, verified=verified,
        )

    if verified:
        return _token_response(user_id, data.email, role, email_verified=True)

    _issue_verification_email(background, user_id, data.email)
    return {"message": "Account created. Check your email to verify your address before logging in.",
            "email": data.email, "verification_required": True}


@router.post("/login")
def login(data: LoginRequest, request: Request):
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

    return _token_response(user["id"], user["email"], user["role"],
                           email_verified=bool(user.get("email_verified")))


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    # `email_verified` is read from the token claim (added at token issue). A token
    # issued before that claim existed reports False until the next login refreshes it
    # — harmless and short-lived (it only gates the on-demand scrape UI).
    return {"id": user["sub"], "email": user["email"], "role": user["role"],
            "email_verified": bool(user.get("email_verified", False))}


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
def resend_verification(data: _EmailOnlyRequest, background: BackgroundTasks):
    """Re-send the verification link. Always returns 200 (never reveals whether
    the address exists or is already verified)."""
    _rate_limit_email(data.email)
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {email: $e}) RETURN u.id AS id, u.email_verified AS verified",
            e=data.email,
        ).single()
    if rec and not rec["verified"]:
        _issue_verification_email(background, rec["id"], data.email)
    return {"message": "If that account exists and is unverified, a verification email was sent."}


@router.post("/forgot-password")
def forgot_password(data: _EmailOnlyRequest, background: BackgroundTasks):
    """Send a password-reset link. Always returns 200 — no user enumeration."""
    _rate_limit_email(data.email)
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {email: $e}) RETURN u.id AS id, u.password_hash AS hash",
            e=data.email,
        ).single()
    if rec:
        _issue_password_reset_email(background, rec["id"], data.email, rec["hash"])
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
    return {"message": "Password updated. You can now log in."}


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
def mfa_verify(data: MfaVerifyRequest):
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
            return _token_response(u["id"], u["email"], u["role"],
                                   email_verified=bool(u.get("email_verified")))

        # Recovery code — single use, so consume it on success.
        digest = hash_recovery_code(data.code)
        remaining = list(u.get("recovery_code_hashes") or [])
        if digest in remaining:
            remaining.remove(digest)
            session.run("MATCH (u:User {id: $id}) SET u.recovery_code_hashes = $h",
                        id=u["id"], h=remaining)
            _clear_login_attempts(key)
            return _token_response(u["id"], u["email"], u["role"],
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
