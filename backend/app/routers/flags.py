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
from app.models.flag import FlagCreate, FlagStatusUpdate, FlagTargetKind, PinRequest

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
               category: Optional[str] = None, group: bool = False,
               limit: int = Query(100, ge=1, le=500),
               _: dict = Depends(require_moderator)):
    """The moderation queue — newest first, with optional filters. Moderator only.

    With `group=true` the flags are collapsed to one row per target+category
    (so many reports of the same thing show as a single row with a `count` and
    the member `flag_ids`), ordered by count then recency."""
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
    # When grouping we scan a wider window so a group's count is accurate, then
    # collapse in Python (ArcadeDB's Cypher aggregation/collect is unreliable).
    fetch = 1000 if group else int(limit)
    with db.get_session() as session:
        rows = session.run(
            f"MATCH (f:Flag) {where} RETURN f.id AS id, f.target_kind AS target_kind, "
            f"f.category AS category, f.note AS note, f.status AS status, "
            f"f.reporter_kind AS reporter_kind, f.from_id AS from_id, f.to_id AS to_id, "
            f"f.role AS role, f.node_id AS node_id, f.created_at AS created_at, "
            f"f.updated_at AS updated_at ORDER BY f.created_at DESC LIMIT {fetch}",
            **params,
        )
        # Read columns explicitly — dict(rec) on a whole ArcadeDB _Record raises.
        flags = [
            {"id": r["id"], "target_kind": r["target_kind"], "category": r["category"],
             "note": r["note"], "status": r["status"], "reporter_kind": r["reporter_kind"],
             "from_id": r["from_id"], "to_id": r["to_id"], "role": r["role"],
             "node_id": r["node_id"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
            for r in rows
        ]

    if not group:
        return flags

    groups: dict[tuple, dict] = {}
    for f in flags:
        key = (f["target_kind"], f["from_id"], f["to_id"], f["role"], f["node_id"], f["category"])
        g = groups.get(key)
        if g is None:
            g = {"target_kind": f["target_kind"], "from_id": f["from_id"], "to_id": f["to_id"],
                 "role": f["role"], "node_id": f["node_id"], "category": f["category"],
                 "count": 0, "flag_ids": [], "note": "", "created_at": ""}
            groups[key] = g
        g["count"] += 1
        g["flag_ids"].append(f["id"])
        if f.get("note") and not g["note"]:     # surface a representative note
            g["note"] = f["note"]
        if (f.get("created_at") or "") > g["created_at"]:
            g["created_at"] = f["created_at"]
    return sorted(groups.values(), key=lambda x: (x["count"], x["created_at"]), reverse=True)


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


@router.delete("/{flag_id}")
def delete_flag(flag_id: str, _: dict = Depends(require_moderator)):
    """Remove a flag entirely (spam, a test, or a duplicate). Moderator only. Any
    Suppression/Pin it produced is a separate record and is left untouched."""
    with db.get_session() as session:
        exists = session.run("MATCH (f:Flag {id:$id}) RETURN f.id AS id", id=flag_id).single()
        if not exists:
            raise HTTPException(status_code=404, detail="Flag not found")
        session.run("MATCH (f:Flag {id:$id}) DETACH DELETE f", id=flag_id)
    return {"id": flag_id, "status": "deleted"}


# ── Suppression (Phase-B resolution) ─────────────────────────────────────────
# Suppressing an edge flag deletes the wrong edge now AND records a Suppression
# override keyed by the edge's natural key, so read endpoints (app.suppressions)
# keep it hidden even if a re-scrape recreates it. See docs/verification.md.

@router.get("/suppressions")
def list_suppressions(_: dict = Depends(require_moderator)):
    """Active suppression overrides, newest first. Moderator only."""
    with db.get_session() as session:
        rows = session.run(
            "MATCH (s:Suppression) RETURN s.id AS id, s.target_kind AS target_kind, "
            "s.from_id AS from_id, s.to_id AS to_id, s.role AS role, "
            "s.flag_id AS flag_id, s.created_at AS created_at ORDER BY s.created_at DESC"
        )
        return [
            {"id": r["id"], "target_kind": r["target_kind"], "from_id": r["from_id"],
             "to_id": r["to_id"], "role": r["role"], "flag_id": r["flag_id"],
             "created_at": r["created_at"]}
            for r in rows
        ]


@router.delete("/suppressions/{suppression_id}")
def remove_suppression(suppression_id: str, _: dict = Depends(require_moderator)):
    """Un-suppress — drop the override (the edge won't reappear until a re-scrape
    recreates it). Moderator only."""
    with db.get_session() as session:
        exists = session.run(
            "MATCH (s:Suppression {id:$id}) RETURN s.id AS id", id=suppression_id).single()
        if not exists:
            raise HTTPException(status_code=404, detail="Suppression not found")
        session.run("MATCH (s:Suppression {id:$id}) DELETE s", id=suppression_id)
    return {"id": suppression_id, "status": "removed"}


@router.post("/{flag_id}/suppress")
def suppress_flag(flag_id: str, _: dict = Depends(require_moderator)):
    """Resolve a flag by suppressing its target, recorded as a re-scrape-surviving
    override; the flag becomes resolved. An **edge** flag (owns/role) also deletes
    the active edge now; a **node** flag (entity/person) is a pure read-time hide
    (not deleted, so un-suppress restores it). Moderator only."""
    with db.get_session() as session:
        flag = session.run(
            "MATCH (f:Flag {id:$id}) RETURN f.target_kind AS tk, f.from_id AS from_id, "
            "f.to_id AS to_id, f.role AS role, f.node_id AS node_id", id=flag_id).single()
        if not flag:
            raise HTTPException(status_code=404, detail="Flag not found")
        tk = flag["tk"]
        now = _now_iso()

        if tk in ("entity", "person"):
            node_id = flag["node_id"]
            existing = session.run(
                "MATCH (s:Suppression) WHERE s.target_kind=$tk AND s.node_id=$nid "
                "RETURN s.id AS id LIMIT 1", tk=tk, nid=node_id).single()
            sup_id = existing["id"] if existing else str(uuid.uuid4())
            if not existing:
                session.run(
                    "CREATE (s:Suppression {id:$id, target_kind:$tk, node_id:$nid, "
                    "from_id:'', to_id:'', role:'', flag_id:$fid, created_at:$now})",
                    id=sup_id, tk=tk, nid=node_id, fid=flag_id, now=now)
        elif tk in ("owns", "role"):
            from_id, to_id, role = flag["from_id"], flag["to_id"], flag.get("role") or ""
            existing = session.run(
                "MATCH (s:Suppression) WHERE s.target_kind=$tk AND s.from_id=$f "
                "AND s.to_id=$t AND s.role=$role RETURN s.id AS id LIMIT 1",
                tk=tk, f=from_id, t=to_id, role=role).single()
            sup_id = existing["id"] if existing else str(uuid.uuid4())
            if not existing:
                session.run(
                    "CREATE (s:Suppression {id:$id, target_kind:$tk, from_id:$f, to_id:$t, "
                    "role:$role, node_id:'', flag_id:$fid, created_at:$now})",
                    id=sup_id, tk=tk, f=from_id, t=to_id, role=role, fid=flag_id, now=now)
            # Delete the active edge now (anchored on indexed ids).
            etype = "OWNS" if tk == "owns" else "HAS_ROLE"
            if tk == "role":
                session.run(
                    f"MATCH (a {{id:$f}})-[r:{etype}]->(b {{id:$t}}) "
                    f"WHERE r.role=$role AND r.until IS NULL DELETE r",
                    f=from_id, t=to_id, role=role)
            else:
                session.run(
                    f"MATCH (a {{id:$f}})-[r:{etype}]->(b {{id:$t}}) WHERE r.until IS NULL DELETE r",
                    f=from_id, t=to_id)
        else:
            raise HTTPException(status_code=400, detail=f"Cannot suppress target_kind '{tk}'")

        # Resolve every open/reviewing flag on this same target — suppressing it
        # moots all reports about it (clears the whole queue group in one action).
        if tk in ("entity", "person"):
            session.run(
                "MATCH (f:Flag) WHERE f.target_kind=$tk AND f.node_id=$nid "
                "AND (f.status='open' OR f.status='reviewing') "
                "SET f.status='resolved', f.updated_at=$now",
                tk=tk, nid=node_id, now=now)
        else:
            session.run(
                "MATCH (f:Flag) WHERE f.target_kind=$tk AND f.from_id=$f AND f.to_id=$t "
                "AND f.role=$role AND (f.status='open' OR f.status='reviewing') "
                "SET f.status='resolved', f.updated_at=$now",
                tk=tk, f=from_id, t=to_id, role=role, now=now)
    return {"id": sup_id, "flag_id": flag_id, "status": "suppressed"}


# ── Pin (Phase-B resolution: corrected OWNS value) ───────────────────────────
# Pinning records a corrected stake %/ownership type for an OWNS edge as a
# read-time override (app.pins) — it does NOT mutate the scraped edge, so the fix
# stands across re-scrapes and un-pinning cleanly reverts to the scraped value.

@router.get("/pins")
def list_pins(_: dict = Depends(require_moderator)):
    """Active pin overrides, newest first. Moderator only."""
    with db.get_session() as session:
        rows = session.run(
            "MATCH (p:Pin) RETURN p.id AS id, p.from_id AS from_id, p.to_id AS to_id, "
            "p.stake_percent AS stake_percent, p.ownership_type AS ownership_type, "
            "p.flag_id AS flag_id, p.created_at AS created_at ORDER BY p.created_at DESC"
        )
        return [
            {"id": r["id"], "from_id": r["from_id"], "to_id": r["to_id"],
             "stake_percent": r["stake_percent"], "ownership_type": r["ownership_type"],
             "flag_id": r["flag_id"], "created_at": r["created_at"]}
            for r in rows
        ]


@router.delete("/pins/{pin_id}")
def remove_pin(pin_id: str, _: dict = Depends(require_moderator)):
    """Un-pin — drop the override; reads fall back to the scraped value. Moderator only."""
    with db.get_session() as session:
        exists = session.run("MATCH (p:Pin {id:$id}) RETURN p.id AS id", id=pin_id).single()
        if not exists:
            raise HTTPException(status_code=404, detail="Pin not found")
        session.run("MATCH (p:Pin {id:$id}) DELETE p", id=pin_id)
    return {"id": pin_id, "status": "removed"}


@router.post("/{flag_id}/pin")
def pin_flag(flag_id: str, data: PinRequest, _: dict = Depends(require_moderator)):
    """Resolve an OWNS edge flag by pinning a corrected stake %/ownership type as a
    read-time override; the flag becomes resolved. Moderator only."""
    with db.get_session() as session:
        flag = session.run(
            "MATCH (f:Flag {id:$id}) RETURN f.target_kind AS tk, f.from_id AS from_id, "
            "f.to_id AS to_id", id=flag_id).single()
        if not flag:
            raise HTTPException(status_code=404, detail="Flag not found")
        if flag["tk"] != "owns":
            raise HTTPException(status_code=400, detail="Only OWNS edge flags can be pinned")
        from_id, to_id = flag["from_id"], flag["to_id"]

        # Merge onto an existing pin for the same edge (keep fields not re-supplied).
        existing = session.run(
            "MATCH (p:Pin) WHERE p.from_id=$f AND p.to_id=$t "
            "RETURN p.id AS id, p.stake_percent AS stake, p.ownership_type AS otype LIMIT 1",
            f=from_id, t=to_id).single()
        stake = data.stake_percent if data.stake_percent is not None else (existing["stake"] if existing else None)
        otype = data.ownership_type if data.ownership_type else (existing["otype"] if existing else None)
        now = _now_iso()
        if existing:
            pin_id = existing["id"]
            session.run(
                "MATCH (p:Pin {id:$id}) SET p.stake_percent=$stake, p.ownership_type=$otype, "
                "p.flag_id=$fid, p.updated_at=$now",
                id=pin_id, stake=stake, otype=otype, fid=flag_id, now=now)
        else:
            pin_id = str(uuid.uuid4())
            session.run(
                "CREATE (p:Pin {id:$id, target_kind:'owns', from_id:$f, to_id:$t, "
                "stake_percent:$stake, ownership_type:$otype, flag_id:$fid, "
                "created_at:$now, updated_at:$now})",
                id=pin_id, f=from_id, t=to_id, stake=stake, otype=otype, fid=flag_id, now=now)

        # Resolve every open/reviewing flag on this OWNS edge (clears the group).
        session.run(
            "MATCH (f:Flag) WHERE f.target_kind='owns' AND f.from_id=$f AND f.to_id=$t "
            "AND (f.status='open' OR f.status='reviewing') "
            "SET f.status='resolved', f.updated_at=$now",
            f=from_id, t=to_id, now=now)
    return {"id": pin_id, "flag_id": flag_id, "status": "pinned",
            "stake_percent": stake, "ownership_type": otype}
