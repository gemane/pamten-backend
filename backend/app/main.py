import asyncio
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app import analytics
from app.config import settings
from app.db.arcadedb import close_client
from app.db.schema import ensure_indexes
from app.scraper.geocode import close_client as close_geocode_client
from app.scraper.sec_edgar import close_client as close_sec_client
from app.routers import (entities, persons, relationships, search, sources, federation,
                         flags, stats, app_version, analytics as analytics_router)
from app.scraper import router as scraper_router
from app.scraper import sources as scraper_sources
from app.auth import router as auth_router

# Emit app INFO logs (scrape/import progress) — without this the root logger
# stays at WARNING and those lines are lost. No-op if handlers already exist.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# ...but don't let httpx log every ArcadeDB request (a bulk import makes thousands).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── Request counters ──────────────────────────────────────────────────────────
#
# How often each route is called, how often it fails, and how slow it is. Kept in
# memory and flushed on a timer: a database write per request would cost more than
# the requests being measured, and losing the last minute of counters on a restart
# costs nothing worth having.
#
# The key is the route TEMPLATE — `GET /entities/{id} 2xx <100ms`, never the path.
# A key per company id would be unbounded, and it would be a record of which
# companies were looked at, which is exactly what this design refuses to keep.
_ENDPOINT_FLUSH_SECONDS = 60
_endpoint_counts: dict[str, int] = defaultdict(int)
_endpoint_lock = Lock()


def _count_request(method: str, route: str, status: int, ms: float) -> None:
    key = f"{method} {route} {status // 100}xx {analytics.latency_bucket(ms)}"
    with _endpoint_lock:
        _endpoint_counts[key] += 1


def _drain_endpoint_counts() -> dict[str, int]:
    with _endpoint_lock:
        counts = dict(_endpoint_counts)
        _endpoint_counts.clear()
    return counts


async def _flush_endpoint_counts_forever() -> None:
    while True:
        await asyncio.sleep(_ENDPOINT_FLUSH_SECONDS)
        counts = _drain_endpoint_counts()
        if counts:
            # Off the event loop: it is a handful of small writes, but they are
            # blocking ones and this runs beside live requests.
            await asyncio.to_thread(analytics.flush_endpoint_stats, counts)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort schema/index bootstrap (idempotent, never fatal).
    ensure_indexes()
    # Provision the env-configured admin (after the schema exists), so a fresh DB
    # has an admin without the first-registrant-becomes-admin race.
    auth_router.bootstrap_admin()
    flusher = (asyncio.create_task(_flush_endpoint_counts_forever())
               if settings.ANALYTICS_ENABLED else None)
    yield
    if flusher:
        flusher.cancel()
        # One last flush, so a graceful shutdown does not throw the window away.
        counts = _drain_endpoint_counts()
        if counts:
            analytics.flush_endpoint_stats(counts)
    # Close pooled HTTP clients on shutdown.
    close_client()
    close_geocode_client()
    close_sec_client()


app = FastAPI(
    title=settings.APP_NAME,
    description="A platform for mapping corporate ownership hierarchies worldwide.",
    version="0.1.0",
    debug=settings.DEBUG,
    lifespan=lifespan,
)

# CORS – explicit allow-list via CORS_ORIGINS env var (comma-separated).
# allow_origins=["*"] with allow_credentials=True is rejected by browsers anyway,
# so an explicit list is required for authenticated cross-origin requests to work.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Response headers are invisible to browser JS unless named here. The graph
    # endpoints report truncation this way (see routers/relationships.py) rather
    # than in the body, which would break already-released clients.
    expose_headers=["X-Result-Truncated", "X-Total-Count"],
)


@app.middleware("http")
async def _measure_request(request: Request, call_next):
    """Count the request, once it is known which ROUTE served it.

    The route template is only in the scope after the app has routed, which is
    why this reads it afterwards rather than parsing the path itself. Anything
    unrouted (a 404, a probe) is counted as `unrouted` — one key, not one per
    URL somebody tried.
    """
    if not settings.ANALYTICS_ENABLED:
        return await call_next(request)

    started = time.perf_counter()
    response = await call_next(request)
    try:
        route = request.scope.get("route")
        template = getattr(route, "path", None) or "unrouted"
        _count_request(request.method, template, response.status_code,
                       (time.perf_counter() - started) * 1000)
    except Exception:  # noqa: BLE001 - measuring must never affect the response
        logging.getLogger(__name__).debug("request measurement failed", exc_info=True)
    return response

# ── API versioning ────────────────────────────────────────────────────────────
#
# Every router is mounted twice:
#
#   /v1/…   the canonical, documented API. New clients use this.
#   /…      the original unversioned paths, still served but hidden from the
#           OpenAPI schema and deprecated.
#
# The duplication exists because clients we don't deploy can pin a path. A mobile
# app runs whatever version the user last installed, and federation peers call
# `{base_url}/federation/export` (see routers/federation.py) — a hardcoded,
# unversioned path baked into every instance already running. Removing the legacy
# mounts would break those silently, so they stay until every known caller has
# moved. Add new endpoints to the routers as usual; both mounts pick them up.
#
# Health endpoints are deliberately NOT versioned — uptime monitoring and
# Render's health check target a stable URL that must never move.
API_V1_PREFIX = "/v1"

_ROUTERS = [
    entities.router,
    persons.router,
    relationships.router,
    search.router,
    stats.router,
    sources.router,
    federation.router,
    flags.router,
    analytics_router.router,
    scraper_router.router,
    scraper_sources.router,
    auth_router.router,
    app_version.router,
]

# Federation is on hold (see routers/federation.py). Leaving it unmounted rather
# than merely disabled means the routes 404 and never appear in the OpenAPI
# schema, so nothing advertises a capability that is switched off — and no
# environment variable can bring it back on its own.
if federation.FEDERATION_ON_HOLD:
    _ROUTERS = [r for r in _ROUTERS if r is not federation.router]

for _router in _ROUTERS:
    app.include_router(_router, prefix=API_V1_PREFIX)
    app.include_router(_router, include_in_schema=False)


@app.get("/", tags=["Health"])
def root():
    return {
        "message": "Owlgraph Ownership Platform API",
        "status": "running",
        "version": "0.1.0",
        "docs": "/docs",
        "licence": {
            "code": "MIT",
            "data": "ODbL v1.0",
            "data_url": "https://opendatacommons.org/licenses/odbl/1-0/",
            "attribution": "Data from Owlgraph, available under ODbL"
        }
    }


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok"}
