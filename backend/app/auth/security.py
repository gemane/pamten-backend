import base64
import hashlib
import os
import secrets
import time as _time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import bcrypt
import jwt
from cryptography.hazmat.primitives.hashes import SHA1
from cryptography.hazmat.primitives.twofactor import InvalidToken
from cryptography.hazmat.primitives.twofactor.totp import TOTP

from app.config import settings

# TOTP parameters (RFC 6238) — the defaults every authenticator app assumes.
_TOTP_DIGITS = 6
_TOTP_PERIOD = 30
_TOTP_SECRET_BYTES = 20          # 160-bit shared secret
_TOTP_SKEW_STEPS = 1             # accept the adjacent 30s windows (clock drift)
_MFA_ISSUER = "Owlgraph"
_RECOVERY_CODE_COUNT = 10

# bcrypt only considers the first 72 bytes of a password; bcrypt >= 4.1 raises
# if given more, so truncate to match (passlib truncated internally too). This
# keeps the $2b$ hash format, so hashes created by the previous passlib+bcrypt
# stack still verify.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8")[:_BCRYPT_MAX_BYTES],
            hashed.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # Malformed/empty stored hash — treat as a failed auth, never raise.
        return False


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])


# ── Purpose-scoped, self-contained links (email verify / password reset) ──────
# These are signed JWTs — no server-side token store. A ``purpose`` claim keeps
# an access token from being usable as a reset link and vice-versa.

class TokenError(Exception):
    """Raised when a purpose token is invalid, expired, or the wrong purpose."""


def password_hash_fingerprint(password_hash: str) -> str:
    """Short digest of the current password hash. Embedding it in a reset token
    makes the token self-invalidating: once the password changes the hash changes,
    so an old (or already-used) reset link no longer matches and is rejected."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def create_purpose_token(sub: str, purpose: str, ttl: timedelta, extra: dict | None = None) -> str:
    payload = {"sub": sub, "purpose": purpose,
               "exp": datetime.now(timezone.utc) + ttl}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def verify_purpose_token(token: str, purpose: str) -> dict:
    """Decode and check a purpose token. Raises TokenError on expiry, bad
    signature, or a purpose mismatch. Returns the claims on success."""
    try:
        claims = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Link has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Invalid link") from exc
    if claims.get("purpose") != purpose:
        raise TokenError("Invalid link")
    return claims


# ── TOTP two-factor auth (RFC 6238) + recovery codes ──────────────────────────

def generate_totp_secret() -> str:
    """A fresh base32 TOTP shared secret (what authenticator apps store)."""
    return base64.b32encode(os.urandom(_TOTP_SECRET_BYTES)).decode("ascii")


def totp_provisioning_uri(secret_b32: str, account: str) -> str:
    """otpauth:// URI for a QR code — scanned by Google Authenticator/Authy. The
    label is `Issuer:Account` with the colon separator kept literal (as apps
    expect) and each part percent-encoded."""
    label = f"{quote(_MFA_ISSUER)}:{quote(account)}"
    return (f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(_MFA_ISSUER)}"
            f"&algorithm=SHA1&digits={_TOTP_DIGITS}&period={_TOTP_PERIOD}")


def _totp(secret_b32: str) -> TOTP:
    key = base64.b32decode(secret_b32)
    return TOTP(key, _TOTP_DIGITS, SHA1(), _TOTP_PERIOD)


def verify_totp(secret_b32: str, code: str) -> bool:
    """True if `code` is valid for the secret now (± one step for clock drift)."""
    code = (code or "").strip()
    if not code.isdigit():
        return False
    try:
        totp = _totp(secret_b32)
    except (ValueError, TypeError):
        return False
    now = int(_time.time())
    for step in range(-_TOTP_SKEW_STEPS, _TOTP_SKEW_STEPS + 1):
        try:
            totp.verify(code.encode("ascii"), now + step * _TOTP_PERIOD)
            return True
        except InvalidToken:
            continue
    return False


def generate_recovery_codes(n: int = _RECOVERY_CODE_COUNT) -> list[str]:
    """One-time backup codes shown once at enrolment (formatted xxxxx-xxxxx)."""
    codes = []
    for _ in range(n):
        raw = secrets.token_hex(5)   # 10 hex chars
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    """Stable digest for storing/matching a recovery code (case/format-insensitive)."""
    normalized = (code or "").strip().lower().replace("-", "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
