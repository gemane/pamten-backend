import time
import uuid
import threading
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, EmailStr, field_validator
from app.database import db
from app.auth.security import hash_password, verify_password, create_access_token
from app.auth.dependencies import get_current_user, require_admin

router = APIRouter(prefix="/auth", tags=["Auth"])

LOGIN_RATE_LIMIT = 5           # attempts
LOGIN_RATE_WINDOW = 15 * 60    # seconds

_login_attempts: dict[str, list[float]] = defaultdict(list)
_login_attempts_lock = threading.Lock()


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
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    with db.get_session() as session:
        if session.run("MATCH (u:User {email: $e}) RETURN u", e=data.email).single():
            raise HTTPException(status_code=400, detail="Email already registered")

        count = session.run("MATCH (u:User) RETURN count(u) AS n").single()["n"]
        role = "admin" if count == 0 else "viewer"
        user_id = str(uuid.uuid4())

        session.run(
            """
            CREATE (u:User {
                id: $id, email: $email, password_hash: $hash,
                role: $role, created_at: toString(datetime())
            })
            """,
            id=user_id, email=data.email,
            hash=hash_password(data.password), role=role,
        )

    return _token_response(user_id, data.email, role)


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
    return _token_response(user["id"], user["email"], user["role"])


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"id": user["sub"], "email": user["email"], "role": user["role"]}


class RoleRequest(BaseModel):
    role: str


@router.get("/users")
def list_users(_: dict = Depends(require_admin)):
    with db.get_session() as session:
        result = session.run(
            "MATCH (u:User) RETURN u.id AS id, u.email AS email, u.role AS role, u.created_at AS created_at ORDER BY u.created_at"
        )
        return [{"id": r["id"], "email": r["email"], "role": r["role"], "created_at": r["created_at"]} for r in result]


@router.patch("/users/{user_id}/role")
def update_user_role(user_id: str, data: RoleRequest, _: dict = Depends(require_admin)):
    if data.role not in ("admin", "contributor", "viewer"):
        raise HTTPException(status_code=400, detail="Role must be admin, contributor, or viewer")
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
