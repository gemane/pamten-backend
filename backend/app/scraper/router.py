from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from app.config import settings
from app.scraper.runner import run_scrape, run_scrape_sec_edgar, run_scrape_all
from app.auth.dependencies import require_admin
from app.database import db

router = APIRouter(prefix="/scraper", tags=["Scraper"])


class ScrapeRequest(BaseModel):
    query: str = Field(..., min_length=2, description="Company or brand name to search on Wikidata")
    depth: int = Field(2, ge=0, le=3, description="How many subsidiary levels to follow (0–3)")


# ── Master status ─────────────────────────────────────────────────────────────

@router.get("/status")
def scraper_status():
    """Check whether the master scraper switch is enabled."""
    return {
        "enabled":              settings.SCRAPER_ENABLED,
        "sec_edgar_enabled":    settings.SCRAPER_SEC_EDGAR_ENABLED,
    }


# ── Wikidata endpoints ────────────────────────────────────────────────────────

@router.post("/run")
def scraper_run(body: ScrapeRequest, _: dict = Depends(require_admin)):
    """
    Trigger a Wikidata scrape for a company name.
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


# ── SEC EDGAR endpoints ───────────────────────────────────────────────────────

@router.get("/sec-edgar/status")
def sec_edgar_status():
    """Check whether SEC EDGAR scraping is enabled (both master and per-source flags)."""
    return {
        "enabled": settings.SCRAPER_ENABLED and settings.SCRAPER_SEC_EDGAR_ENABLED,
        "master_switch":     settings.SCRAPER_ENABLED,
        "sec_edgar_switch":  settings.SCRAPER_SEC_EDGAR_ENABLED,
    }


@router.post("/sec-edgar/run")
def sec_edgar_run(
    company: str = Query(..., min_length=2, description="Company name to look up on SEC EDGAR"),
    _: dict = Depends(require_admin),
):
    """
    Scrape SEC EDGAR for ownership filings and executive data for one company.
    Requires SCRAPER_ENABLED=true AND SCRAPER_SEC_EDGAR_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    if not settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise HTTPException(status_code=403,
            detail="SEC EDGAR scraper is disabled. Set SCRAPER_SEC_EDGAR_ENABLED=true.")
    try:
        result = run_scrape_sec_edgar(company)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SEC EDGAR scrape failed: {e}")


# ── Run-all endpoint ──────────────────────────────────────────────────────────

@router.post("/run-all")
def scraper_run_all(
    company: str = Query(..., min_length=2, description="Company name to scrape across all enabled sources"),
    depth:   int = Query(2, ge=0, le=3,    description="Wikidata subsidiary depth (0–3)"),
    _: dict = Depends(require_admin),
):
    """
    Run all enabled scrapers (Wikidata + SEC EDGAR) for a company name.
    Disabled scrapers are skipped and reported with status 'disabled'.
    Requires SCRAPER_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    try:
        result = run_scrape_all(company, depth)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Run-all failed: {e}")


# ── Purge endpoint ────────────────────────────────────────────────────────────

@router.delete("/company")
def purge_company(
    name: str = Query(..., min_length=2, description="Exact company name to delete"),
    _: dict = Depends(require_admin),
):
    """
    Delete a company entity and all its relationships from the graph, then
    remove any nodes that are left with no remaining relationships (orphans).
    Admin only. Useful for cleaning up test scrapes.
    """
    with db.get_session() as session:
        # Check it exists first
        rec = session.run(
            "MATCH (e:Entity {name: $name}) RETURN e.id AS id LIMIT 1",
            name=name,
        ).single()
        if not rec:
            raise HTTPException(status_code=404, detail=f"Company '{name}' not found")

        # Detach-delete the entity and all its relationships
        session.run(
            "MATCH (e:Entity {name: $name}) DETACH DELETE e",
            name=name,
        )

        # Remove orphaned Person and Entity nodes (no remaining relationships)
        orphan_result = session.run(
            """
            MATCH (n)
            WHERE (n:Person OR n:Entity) AND NOT (n)--()
            WITH n, n.name AS orphan_name
            DETACH DELETE n
            RETURN count(*) AS removed, collect(orphan_name) AS names
            """
        ).single()
        orphans_removed = orphan_result["removed"] if orphan_result else 0
        orphan_names    = orphan_result["names"]   if orphan_result else []

    return {
        "status":          "deleted",
        "company":         name,
        "orphans_removed": orphans_removed,
        "orphans":         orphan_names,
    }
