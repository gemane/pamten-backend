import uuid
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.database import db
from app.auth.security import hash_password, verify_password, create_access_token
from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["Auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


def _token_response(user_id: str, email: str, role: str):
    token = create_access_token({"sub": user_id, "email": email, "role": role})
    return {"access_token": token, "token_type": "bearer", "email": email, "role": role}


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
def login(data: LoginRequest):
    with db.get_session() as session:
        rec = session.run("MATCH (u:User {email: $e}) RETURN u", e=data.email).single()
        if not rec:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        user = dict(rec["u"])

    if not verify_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return _token_response(user["id"], user["email"], user["role"])


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"id": user["sub"], "email": user["email"], "role": user["role"]}
