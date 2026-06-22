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


def require_admin(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_contributor(user: dict = Depends(get_current_user)):
    if user.get("role") not in ("admin", "contributor"):
        raise HTTPException(status_code=403, detail="Contributor or admin access required")
    return user
