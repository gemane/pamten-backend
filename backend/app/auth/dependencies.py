import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.auth.security import decode_token

bearer = HTTPBearer(auto_error=False)


def _parse(credentials: HTTPAuthorizationCredentials):
    if not credentials:
        return None
    try:
        return decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    user = _parse(credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def get_current_user_optional(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    return _parse(credentials)


def require_verified(user: dict = Depends(get_current_user)):
    """Authenticated + email-verified, **any role** (viewer included) — the gate for
    user-triggered on-demand scraping. Trusts the `email_verified` token claim when
    present (added at token issue); for older tokens without it, reads the User node
    once. 403 if the account isn't verified."""
    if user.get("email_verified") is True:
        return user
    from app.database import db     # local import keeps this module import-light
    with db.get_session() as session:
        rec = session.run("MATCH (u:User {id: $id}) RETURN u.email_verified AS v",
                          id=user["sub"]).single()
    if not rec or not rec["v"]:
        raise HTTPException(status_code=403, detail="Email verification required")
    return user


def require_admin(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_contributor(user: dict = Depends(get_current_user)):
    if user.get("role") not in ("admin", "contributor"):
        raise HTTPException(status_code=403, detail="Contributor or admin access required")
    return user


def require_moderator(user: dict = Depends(get_current_user)):
    """Data-moderation actions (e.g. the verification flag queue). `admin`
    implies moderator; `contributor` / `viewer` do not."""
    if user.get("role") not in ("admin", "moderator"):
        raise HTTPException(status_code=403, detail="Moderator or admin access required")
    return user
