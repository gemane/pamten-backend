from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from app.config import settings
from app.scraper.runner import run_scrape
from app.auth.dependencies import require_admin

router = APIRouter(prefix="/scraper", tags=["Scraper"])


class ScrapeRequest(BaseModel):
    query: str = Field(..., min_length=2, description="Company or brand name to search on Wikidata")
    depth: int = Field(2, ge=0, le=3, description="How many subsidiary levels to follow (0–3)")


@router.get("/status")
def scraper_status():
    """Check whether the scraper is enabled."""
    return {"enabled": settings.SCRAPER_ENABLED}


@router.post("/run")
def scraper_run(body: ScrapeRequest, _: dict = Depends(require_admin)):
    """
    Trigger a scrape for a company name.
    Requires SCRAPER_ENABLED=true in the environment.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable.",
        )
    try:
        result = run_scrape(body.query, body.depth)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape failed: {e}")
