import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.db.arcadedb import close_client
from app.db.schema import ensure_indexes
from app.scraper.geocode import close_client as close_geocode_client
from app.scraper.sec_edgar import close_client as close_sec_client
from app.routers import entities, persons, locations, relationships, search, sources, federation, flags, stats
from app.scraper import router as scraper_router
from app.scraper import sources as scraper_sources
from app.auth import router as auth_router

# Emit app INFO logs (scrape/import progress) — without this the root logger
# stays at WARNING and those lines are lost. No-op if handlers already exist.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# ...but don't let httpx log every ArcadeDB request (a bulk import makes thousands).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort schema/index bootstrap (idempotent, never fatal).
    ensure_indexes()
    # Provision the env-configured admin (after the schema exists), so a fresh DB
    # has an admin without the first-registrant-becomes-admin race.
    auth_router.bootstrap_admin()
    yield
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
    expose_headers=["X-Result-Truncated"],
)

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
    locations.router,
    relationships.router,
    search.router,
    stats.router,
    sources.router,
    federation.router,
    flags.router,
    scraper_router.router,
    scraper_sources.router,
    auth_router.router,
]

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
