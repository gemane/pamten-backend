"""
Verification flags API — readers report a node/edge that looks wrong; moderators
work the queue. Phase A: capture + surface only (no resolution). See
docs/verification.md.
"""
import hashlib
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.config import settings
from app.database import db
from app.auth.dependencies import get_current_user_optional, require_moderator
from app.models.flag import FlagCreate, FlagStatusUpdate, FlagTargetKind

router = APIRouter(prefix="/flags", tags=["Verification"])

# In-memory sliding-window rate limit (mirrors the login limiter). Anonymous
# reporters are capped tightly; signed-in users get a higher ceiling.
ANON_RATE_LIMIT = 2            # flags per window for an anonymous fingerprint
USER_RATE_LIMIT = 20          # flags per window for a logged-in user
RATE_WINDOW = 60 * 60         # seconds (1 hour)

_flag_events: dict[str, list[float]] = defaultdict(list)
_flag_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_ip(request: Request) -> str:
    """Real client IP: first hop of X-Forwarded-For (set by Render's proxy), else
    the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _fingerprint(ip: str) -> str:
    """Salted hash of the client IP — for abuse control only, never displayed."""
    return hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode()).hexdigest()[:32]


def _check_rate_limit(key: str, limit: int) -> None:
    now = time.time()
    with _flag_lock:
        events = [t for t in _flag_events[key] if now - t < RATE_WINDOW]
        _flag_events[key] = events
        if len(events) >= limit:
            raise HTTPException(status_code=429, detail="Too many reports. Try again later.")
        events.append(now)


def _target_clause(data) -> tuple[str, dict]:
    """WHERE fragment + params identifying a flag's target (node or edge)."""
    if data.target_kind in (FlagTargetKind.owns, FlagTargetKind.role):
        clause = "f.target_kind = $tk AND f.from_id = $from_id AND f.to_id = $to_id"
        params = {"tk": data.target_kind.value, "from_id": data.from_id, "to_id": data.to_id}
        if data.target_kind == FlagTargetKind.role:
            clause += " AND f.role = $role"
            params["role"] = data.role
        return clause, params
    return "f.target_kind = $tk AND f.node_id = $node_id", {
        "tk": data.target_kind.value, "node_id": data.node_id}


@router.post("")
def create_flag(data: FlagCreate, request: Request,
                user: Optional[dict] = Depends(get_current_user_optional)):
    """File a report that a node/edge looks wrong. Open to everyone; signing in
    only raises the rate ceiling and records the reporter."""
    fp = _fingerprint(_client_ip(request))
    if user:
        _check_rate_limit(f"user:{user['sub']}", USER_RATE_LIMIT)
        reporter_kind, reporter_id = "user", user["sub"]
    else:
        _check_rate_limit(f"anon:{fp}", ANON_RATE_LIMIT)
        reporter_kind, reporter_id = "anon", ""

    clause, params = _target_clause(data)
    with db.get_session() as session:
        # Duplicate-collapse: same reporter, same target + category, still active.
        existing = session.run(
            f"MATCH (f:Flag) WHERE {clause} AND f.category = $cat AND f.reporter_fp = $fp "
            f"AND (f.status = 'open' OR f.status = 'reviewing') RETURN f.id AS id LIMIT 1",
            cat=data.category.value, fp=fp, **params,
        ).single()
        if existing:
            return {"id": existing["id"], "status": "duplicate", "message": "Already reported."}

        flag_id = str(uuid.uuid4())
        now = _now_iso()
        session.run(
            "CREATE (f:Flag {id:$id, target_kind:$tk, category:$cat, note:$note, status:'open', "
            "reporter_kind:$rk, reporter_id:$rid, reporter_fp:$fp, "
            "from_id:$from_id, to_id:$to_id, role:$role, node_id:$node_id, "
            "created_at:$now, updated_at:$now})",
            id=flag_id, tk=data.target_kind.value, cat=data.category.value,
            note=(data.note or "")[:1000], rk=reporter_kind, rid=reporter_id, fp=fp,
            from_id=data.from_id or "", to_id=data.to_id or "",
            role=data.role or "", node_id=data.node_id or "", now=now,
        )
    return {"id": flag_id, "status": "open"}


@router.get("")
def list_flags(status: Optional[str] = None, target_kind: Optional[str] = None,
               category: Optional[str] = None, limit: int = Query(100, ge=1, le=500),
               _: dict = Depends(require_moderator)):
    """The moderation queue — newest first, with optional filters. Moderator only."""
    clauses, params = [], {}
    if status:
        clauses.append("f.status = $status")
        params["status"] = status
    if target_kind:
        clauses.append("f.target_kind = $tk")
        params["tk"] = target_kind
    if category:
        clauses.append("f.category = $cat")
        params["cat"] = category
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.get_session() as session:
        rows = session.run(
            f"MATCH (f:Flag) {where} RETURN f.id AS id, f.target_kind AS target_kind, "
            f"f.category AS category, f.note AS note, f.status AS status, "
            f"f.reporter_kind AS reporter_kind, f.from_id AS from_id, f.to_id AS to_id, "
            f"f.role AS role, f.node_id AS node_id, f.created_at AS created_at, "
            f"f.updated_at AS updated_at ORDER BY f.created_at DESC LIMIT {int(limit)}",
            **params,
        )
        # Read columns explicitly — dict(rec) on a whole ArcadeDB _Record raises.
        return [
            {"id": r["id"], "target_kind": r["target_kind"], "category": r["category"],
             "note": r["note"], "status": r["status"], "reporter_kind": r["reporter_kind"],
             "from_id": r["from_id"], "to_id": r["to_id"], "role": r["role"],
             "node_id": r["node_id"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
            for r in rows
        ]


@router.get("/summary")
def flag_summary(node_id: Optional[str] = None, from_id: Optional[str] = None,
                 to_id: Optional[str] = None, role: Optional[str] = None):
    """Open-flag count for one target — powers the "disputed" badge. Public."""
    if node_id:
        clause, params = "f.node_id = $node_id", {"node_id": node_id}
    elif from_id and to_id:
        clause, params = "f.from_id = $from_id AND f.to_id = $to_id", {"from_id": from_id, "to_id": to_id}
        if role:
            clause += " AND f.role = $role"
            params["role"] = role
    else:
        raise HTTPException(status_code=400, detail="Provide node_id, or from_id and to_id")
    with db.get_session() as session:
        rec = session.run(
            f"MATCH (f:Flag) WHERE {clause} AND f.status = 'open' RETURN count(f) AS n",
            **params,
        ).single()
        return {"open": (rec["n"] if rec else 0) or 0}


@router.patch("/{flag_id}")
def update_flag_status(flag_id: str, data: FlagStatusUpdate,
                       _: dict = Depends(require_moderator)):
    """Move a flag through triage (open ⇄ reviewing, → rejected). Moderator only."""
    with db.get_session() as session:
        rec = session.run(
            "MATCH (f:Flag {id:$id}) SET f.status = $st, f.updated_at = $now RETURN f.id AS id",
            id=flag_id, st=data.status.value, now=_now_iso(),
        ).single()
        if not rec:
            raise HTTPException(status_code=404, detail="Flag not found")
    return {"id": flag_id, "status": data.status.value}
