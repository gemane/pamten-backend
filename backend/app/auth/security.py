import hashlib
from datetime import datetime, timedelta, timezone
import bcrypt
import jwt
from app.config import settings

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
