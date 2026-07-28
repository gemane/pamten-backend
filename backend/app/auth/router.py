import logging
import time
import uuid
import threading
from datetime import timedelta
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, EmailStr, field_validator
from app.config import settings
from app.database import db
from app.auth.security import (
    hash_password, verify_password, create_access_token,
    create_purpose_token, verify_purpose_token, password_hash_fingerprint, TokenError,
)
from app.auth.dependencies import get_current_user, require_admin
from app.notifications.email import send_verification_email, send_password_reset_email

MIN_PASSWORD_LENGTH = 8
VERIFY_EMAIL_PURPOSE = "verify_email"
RESET_PASSWORD_PURPOSE = "pwd_reset"

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

_login_attempts: dict[str, list[float]] = defaultdict(list)
_login_attempts_lock = threading.Lock()

# Throttle outbound email (verification resend / password reset) per address, so
# the endpoints can't be used to spam an inbox. Applied before any user lookup so
# it never reveals whether an address exists.
EMAIL_SEND_LIMIT = 3
EMAIL_SEND_WINDOW = 15 * 60
_email_send_attempts: dict[str, list[float]] = defaultdict(list)
_email_send_lock = threading.Lock()


def _rate_limit_email(email: str) -> None:
    now = time.time()
    with _email_send_lock:
        attempts = [t for t in _email_send_attempts[email] if now - t < EMAIL_SEND_WINDOW]
        _email_send_attempts[email] = attempts
        if len(attempts) >= EMAIL_SEND_LIMIT:
            raise HTTPException(status_code=429, detail="Too many requests. Try again later.")
        _email_send_attempts[email].append(now)


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


def _issue_verification_email(user_id: str, email: str) -> None:
    token = create_purpose_token(
        user_id, VERIFY_EMAIL_PURPOSE, timedelta(hours=settings.EMAIL_VERIFY_TTL_HOURS))
    send_verification_email(email, token)


def _issue_password_reset_email(user_id: str, email: str, password_hash: str) -> None:
    token = create_purpose_token(
        user_id, RESET_PASSWORD_PURPOSE, timedelta(minutes=settings.PASSWORD_RESET_TTL_MINUTES),
        extra={"ph": password_hash_fingerprint(password_hash)})
    send_password_reset_email(email, token)


def _token_response(user_id: str, email: str, role: str):
    token = create_access_token({"sub": user_id, "email": email, "role": role})
    return {"access_token": token, "token_type": "bearer", "email": email, "role": role}


def _login_rate_limit_key(request: Request, email: str) -> str:
    client_ip = request.client.host if request.client else "unknown"
    return f"{client_ip}:{email}"


def _check_login_rate_limit(key: str) -> None:
    now = time.time()
    with _login_attempts_lock:
        attempts = [t for t in _login_attempts[key] if now - t < LOGIN_RATE_WINDOW]
        _login_attempts[key] = attempts
        if len(attempts) >= LOGIN_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")


def _record_login_failure(key: str) -> None:
    with _login_attempts_lock:
        _login_attempts[key].append(time.time())


def _clear_login_attempts(key: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(key, None)


@router.post("/register")
def register(data: RegisterRequest):
    if len(data.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400,
                            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

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
        return _token_response(user_id, data.email, role)

    _issue_verification_email(user_id, data.email)
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

    return _token_response(user["id"], user["email"], user["role"])


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"id": user["sub"], "email": user["email"], "role": user["role"]}


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
def resend_verification(data: _EmailOnlyRequest):
    """Re-send the verification link. Always returns 200 (never reveals whether
    the address exists or is already verified)."""
    _rate_limit_email(data.email)
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {email: $e}) RETURN u.id AS id, u.email_verified AS verified",
            e=data.email,
        ).single()
    if rec and not rec["verified"]:
        _issue_verification_email(rec["id"], data.email)
    return {"message": "If that account exists and is unverified, a verification email was sent."}


@router.post("/forgot-password")
def forgot_password(data: _EmailOnlyRequest):
    """Send a password-reset link. Always returns 200 — no user enumeration."""
    _rate_limit_email(data.email)
    with db.get_session() as session:
        rec = session.run(
            "MATCH (u:User {email: $e}) RETURN u.id AS id, u.password_hash AS hash",
            e=data.email,
        ).single()
    if rec:
        _issue_password_reset_email(rec["id"], data.email, rec["hash"])
    return {"message": "If that account exists, a password-reset email was sent."}


@router.post("/reset-password")
def reset_password(data: ResetPasswordRequest):
    if len(data.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400,
                            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
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
