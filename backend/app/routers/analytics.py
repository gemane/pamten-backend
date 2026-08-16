"""
Usage measurement: what people look for, and what they do not find.

Read `app/analytics.py` first — it holds the privacy design, which is the reason
this endpoint takes so little. There is no user id in the request body, none is
read from the token, and the client is not asked for one.

The write endpoint is public because the searches worth knowing about include the
ones made signed-out, and requiring an account would bias the answer toward the
handful of people who have one. Public and unauthenticated also means it is
trivially floodable, so: an allow-list for every key, hard caps on the free-text
field, and the same salted-IP-hash rate limiter the flag endpoint uses — held in
memory for abuse control, never stored, never shown.

Reading is admin-only. Aggregates are not personal data about the searcher, but
free text people typed is not something to hand out either.
"""
import hashlib
import logging
import time
from collections import defaultdict
from threading import Lock
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app import analytics
from app.auth.dependencies import require_admin
from app.config import settings
from app.database import db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["Analytics"])

RATE_WINDOW = 3600          # seconds
EVENT_RATE_LIMIT = 240      # events per window per fingerprint — a busy session,
                            # not a flood: settled searches and deliberate clicks
                            # only, so a real user produces a few dozen an hour.

_lock = Lock()
_events: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _fingerprint(ip: str) -> str:
    """Salted hash of the client IP — abuse control only, never stored or shown."""
    return hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode()).hexdigest()[:32]


def _check_rate_limit(key: str) -> None:
    now = time.time()
    with _lock:
        recent = [t for t in _events[key] if now - t < RATE_WINDOW]
        _events[key] = recent
        if len(recent) >= EVENT_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many events.")
        _events[key].append(now)


class AnalyticsEvent(BaseModel):
    """One settled search, or one interaction. Never a keystroke."""

    kind: Literal["search", "usage"]
    # search
    query: Optional[str] = Field(None, max_length=120)
    country: Optional[str] = Field(None, pattern="^[A-Za-z]{2}$")
    outcome: Optional[Literal["selected", "zero", "abandoned"]] = None
    rank: Optional[int] = Field(None, ge=0, le=analytics.MAX_RANK)
    # usage
    event: Optional[str] = Field(None, max_length=64)


@router.post("/event", status_code=204)
def record_event(body: AnalyticsEvent, request: Request):
    """Record one aggregate event. Public, rate-limited, best-effort.

    Returns 204 whatever happens downstream: measurement must never be able to
    break, slow or reveal anything about the thing it is measuring. A rejected
    event is logged here and forgotten, not surfaced to the user.
    """
    if not settings.ANALYTICS_ENABLED:
        return Response(status_code=204)

    _check_rate_limit(_fingerprint(_client_ip(request)))

    try:
        if body.kind == "search":
            if not body.query or not body.outcome:
                raise ValueError("a search event needs a query and an outcome")
            analytics.record_search(body.query, body.country, body.outcome)
            # Rank is counted separately from the query — the question it answers
            # is about the ranking as a whole, not about one search.
            if body.outcome == "selected" and body.rank is not None:
                analytics.record_rank(body.rank)
        else:
            if not body.event:
                raise ValueError("a usage event needs an event name")
            analytics.record_usage(body.event)
    except ValueError as exc:
        # An unknown key is a client bug or an attempt to widen the key space.
        log.info("analytics event rejected: %s", exc)
    except Exception:  # noqa: BLE001 - never let measurement break a user action
        log.exception("analytics event failed")
    return Response(status_code=204)


def _page(vtype: str, order_by: str, response: Response, skip: int, limit: int) -> list[dict]:
    """One page of counters, biggest first, with the full total in a header —
    the same shape the flag queue uses."""
    with db.get_session() as session:
        rows = session.run(
            f"MATCH (r:{vtype}) RETURN r AS row ORDER BY r.{order_by} DESC "
            f"SKIP {int(skip)} LIMIT {int(limit)}"
        )
        out = [dict(rec["row"]) for rec in rows]
        rec = session.run(f"MATCH (r:{vtype}) RETURN count(r) AS n").single()
    response.headers["X-Total-Count"] = str((rec["n"] if rec else 0) or 0)
    return out


@router.get("/searches")
def list_searches(response: Response,
                  skip: int = Query(0, ge=0, le=100_000),
                  limit: int = Query(100, ge=1, le=500),
                  _: dict = Depends(require_admin)):
    """What people searched for, most-searched first.

    The `zero_results` column is the interesting one: it is a ranked list of what
    users wanted and the graph could not answer — which register or country to
    add next, in demand order.
    """
    return _page("SearchDemand", "searches", response, skip, limit)


@router.get("/usage")
def list_usage(response: Response,
               skip: int = Query(0, ge=0, le=100_000),
               limit: int = Query(200, ge=1, le=500),
               _: dict = Depends(require_admin)):
    """Feature counters and clicked-result positions, most-used first."""
    return _page("UsageCounter", "count", response, skip, limit)


@router.get("/endpoints")
def list_endpoints(response: Response,
                   skip: int = Query(0, ge=0, le=100_000),
                   limit: int = Query(200, ge=1, le=500),
                   _: dict = Depends(require_admin)):
    """Request counts by route, status class and latency bucket — where the app is
    slow or failing, keyed on the route template so no company id is ever a key."""
    return _page("EndpointStat", "count", response, skip, limit)
