from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.db.arcadedb import close_client
from app.db.schema import ensure_indexes
from app.routers import entities, persons, locations, relationships, search, sources
from app.scraper import router as scraper_router
from app.scraper import sources as scraper_sources
from app.auth import router as auth_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort schema/index bootstrap (idempotent, never fatal).
    ensure_indexes()
    yield
    # Close the pooled ArcadeDB connections on shutdown.
    close_client()


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
)

# Include all routers
app.include_router(entities.router)
app.include_router(persons.router)
app.include_router(locations.router)
app.include_router(relationships.router)
app.include_router(search.router)
app.include_router(sources.router)
app.include_router(scraper_router.router)
app.include_router(scraper_sources.router)
app.include_router(auth_router.router)


@app.get("/", tags=["Health"])
def root():
    return {
        "message": "Pamten Ownership Platform API",
        "status": "running",
        "version": "0.1.0",
        "docs": "/docs",
        "licence": {
            "code": "MIT",
            "data": "ODbL v1.0",
            "data_url": "https://opendatacommons.org/licenses/odbl/1-0/",
            "attribution": "Data from Pamten, available under ODbL"
        }
    }


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok"}
