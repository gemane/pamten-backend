from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routers import entities, persons, locations, relationships, search, sources
from app.scraper import router as scraper_router
from app.auth import router as auth_router

app = FastAPI(
    title=settings.APP_NAME,
    description="A platform for mapping corporate ownership hierarchies worldwide.",
    version="0.1.0",
    debug=settings.DEBUG
)

# CORS – allow all origins for now (tighten in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
app.include_router(auth_router.router)


@app.get("/", tags=["Health"])
def root():
    return {
        "message": "Ownership Platform API",
        "status": "running",
        "version": "0.1.0",
        "docs": "/docs"
    }


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok"}
