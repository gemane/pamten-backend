import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from app.config import settings
from app.scraper.runner import (
    run_scrape, run_scrape_sec_edgar, run_scrape_all, run_scrape_open_corporates,
    run_import_bods_gleif, run_import_bods_uk_psc,
)
from app.auth.dependencies import require_admin, require_contributor
from app.scraper import maintenance, proxy_write
from app.scraper.run_log import record_run, list_runs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scraper", tags=["Scraper"])


class ScrapeRequest(BaseModel):
    query: str = Field(..., min_length=2, description="Company or brand name to search on Wikidata")
    depth: int = Field(2, ge=0, le=3, description="How many subsidiary levels to follow (0–3)")


# ── Master status ─────────────────────────────────────────────────────────────

@router.get("/status")
def scraper_status():
    """Check whether the master scraper switch is enabled."""
    return {
        "enabled":                    settings.SCRAPER_ENABLED,
        "wikidata_enabled":           settings.SCRAPER_WIKIDATA_ENABLED,
        "sec_edgar_enabled":          settings.SCRAPER_SEC_EDGAR_ENABLED,
        "open_corporates_enabled":    settings.SCRAPER_OPENCORPORATES_ENABLED,
        "bods_gleif_enabled":         settings.SCRAPER_BODS_GLEIF_ENABLED,
        "bods_uk_psc_enabled":        settings.SCRAPER_BODS_UK_PSC_ENABLED,
        "geocoding_enabled":          settings.GEOCODING_ENABLED,
        "autodedup_enabled":          settings.SCRAPER_AUTODEDUP_ENABLED,
    }


@router.get("/runs")
def scraper_runs(
    limit: int = Query(50, ge=1, le=500, description="Max run records to return"),
    _: dict = Depends(require_contributor),
):
    """Recent scrape runs (newest first) — what ran, when, node counts, and failures."""
    runs = list_runs(limit)
    return {"count": len(runs), "runs": runs}


# ── Wikidata endpoints ────────────────────────────────────────────────────────

@router.post("/run")
def scraper_run(body: ScrapeRequest, _: dict = Depends(require_contributor)):
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
        with record_run("wikidata", body.query) as run:
            result = run_scrape(body.query, body.depth)
            run["total"] = result.get("total", 0)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("Wikidata scrape failed (query=%r)", body.query)
        raise HTTPException(status_code=500, detail="Scrape failed. Check server logs for details.")


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
    _: dict = Depends(require_contributor),
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
        with record_run("sec_edgar", company) as run:
            result = run_scrape_sec_edgar(company)
            run["total"] = result.get("total", 0)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("SEC EDGAR scrape failed (company=%r)", company)
        raise HTTPException(status_code=500, detail="SEC EDGAR scrape failed. Check server logs for details.")


# ── Run-all endpoint ──────────────────────────────────────────────────────────

@router.post("/run-all")
def scraper_run_all(
    company: str = Query(..., min_length=2, description="Company name to scrape across all enabled sources"),
    depth:   int = Query(2, ge=0, le=3,    description="Wikidata subsidiary depth (0–3)"),
    _: dict = Depends(require_contributor),
):
    """
    Run all enabled scrapers (Wikidata + SEC EDGAR + OpenCorporates) for a company name.
    Disabled scrapers are skipped and reported with status 'disabled'.
    Requires SCRAPER_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    try:
        with record_run("all", company) as run:
            result = run_scrape_all(company, depth)
            run["total"] = sum(
                (v or {}).get("total", 0)
                for v in (result.get("results") or {}).values() if isinstance(v, dict))
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("Run-all scrape failed (company=%r)", company)
        raise HTTPException(status_code=500, detail="Run-all failed. Check server logs for details.")


# ── OpenCorporates endpoints ──────────────────────────────────────────────────

@router.get("/open-corporates/status")
def open_corporates_status():
    """Check whether OpenCorporates scraping is enabled (both master and per-source flags)."""
    return {
        "enabled":                    settings.SCRAPER_ENABLED and settings.SCRAPER_OPENCORPORATES_ENABLED,
        "master_switch":              settings.SCRAPER_ENABLED,
        "open_corporates_switch":     settings.SCRAPER_OPENCORPORATES_ENABLED,
    }


@router.post("/open-corporates/run")
def open_corporates_run(
    company: str = Query(..., min_length=2, description="Company name to look up on OpenCorporates"),
    _: dict = Depends(require_contributor),
):
    """
    Scrape OpenCorporates for company registration details and officers.
    Requires SCRAPER_ENABLED=true AND SCRAPER_OPENCORPORATES_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    if not settings.SCRAPER_OPENCORPORATES_ENABLED:
        raise HTTPException(status_code=403,
            detail="OpenCorporates scraper is disabled. Set SCRAPER_OPENCORPORATES_ENABLED=true.")
    try:
        with record_run("open_corporates", company) as run:
            result = run_scrape_open_corporates(company)
            run["total"] = result.get("total", 0)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("OpenCorporates scrape failed (company=%r)", company)
        raise HTTPException(status_code=500, detail="OpenCorporates scrape failed. Check server logs for details.")


# ── Purge endpoint ────────────────────────────────────────────────────────────

@router.delete("/company")
def purge_company(
    name: str = Query(..., min_length=2, description="Exact company name to delete"),
    _: dict = Depends(require_admin),
):
    """Delete a company entity and all its relationships, then orphans. Admin only."""
    try:
        return maintenance.purge_company(name)
    except maintenance.CompanyNotFound:
        raise HTTPException(status_code=404, detail=f"Company '{name}' not found")


# ── Deduplication endpoint ─────────────────────────────────────────────────────

@router.post("/deduplicate-edges")
def deduplicate_owns_edges(_: dict = Depends(require_admin)):
    """Collapse duplicate active OWNS edges, keeping the most informative. Admin only."""
    return maintenance.deduplicate_owns_edges()


# ── Person deduplication endpoint ──────────────────────────────────────────────

@router.post("/deduplicate-persons")
def deduplicate_person_nodes(_: dict = Depends(require_admin)):
    """Merge reversed-name Person duplicates and migrate their edges. Admin only."""
    return maintenance.deduplicate_person_nodes()


# ── Entity deduplication endpoint ──────────────────────────────────────────────

@router.post("/deduplicate-entities")
def deduplicate_entities(_: dict = Depends(require_admin)):
    """Merge Entity duplicates sharing an LEI / Companies House number (heals the
    old recordId-keyed BODS doubling) and migrate their edges. Admin only."""
    return maintenance.deduplicate_entities()


# ── Geocode endpoint ───────────────────────────────────────────────────────────

@router.post("/geocode")
def geocode_backfill_run(
    limit: int | None = Query(None, ge=1, description="Max nodes to geocode this run"),
    _: dict = Depends(require_contributor),
):
    """
    Backfill HQ coordinates via Nominatim for Location nodes and Entities that
    have a city/country but no coordinates. Gated by GEOCODING_ENABLED (env), so
    it never hits Nominatim unless deliberately turned on.
    """
    if not settings.GEOCODING_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Geocoding is disabled. Set GEOCODING_ENABLED=true in the environment to enable.",
        )
    from app.scraper.geocode_backfill import backfill
    return {"status": "ok", **backfill(limit=limit)}


# ── Proxy statement endpoints ───────────────────────────────────────────────────

@router.post("/proxy-statement/run")
def proxy_statement_run(
    company: str = Query(..., min_length=2,
                         description="Company name to search for on EDGAR"),
    _: dict = Depends(require_contributor),
):
    """
    Parse the most recent DEF 14A proxy statement for a company and return
    per-person voting power percentages from the beneficial ownership table.
    Read-only — does not write to the database.
    """
    from app.scraper.proxy_statement import fetch_proxy_ownership
    return fetch_proxy_ownership(company)


@router.post("/proxy-statement/write")
def proxy_statement_write(
    company: str = Query(..., min_length=2,
                         description="Company name to search for on EDGAR"),
    entity_id: str | None = Query(
        None,
        description="DB entity ID of the target company (overrides name lookup). "
                    "Use this when the EDGAR name differs from the DB name, "
                    "e.g. company=Alphabet&entity_id=<google-uuid>",
    ),
    _: dict = Depends(require_contributor),
):
    """Fetch the latest DEF 14A and write voting_power_pct onto OWNS edges."""
    return proxy_write.write_proxy_ownership(company, entity_id)


# ── Ownership-type migration endpoint ────────────────────────────────────────

@router.post("/migrate-ownership-types")
def migrate_ownership_types(_: dict = Depends(require_admin)):
    """One-time migration deriving canonical ownership_type values. Admin only."""
    return maintenance.migrate_ownership_types()


# ── BODS endpoints ────────────────────────────────────────────────────────────

def _validate_bods_local_file(local_file: str | None) -> str | None:
    """
    Restrict local_file to .zip/.json files inside BODS_DATA_DIR.

    The importer opens whatever path it is handed, so without this check any
    contributor could read arbitrary server files into the graph. resolve()
    follows symlinks, so a link pointing outside the data dir is rejected too.
    """
    if local_file is None:
        return None
    data_dir = Path(settings.BODS_DATA_DIR).resolve()
    path = Path(local_file).resolve()
    if path.suffix.lower() not in (".zip", ".json"):
        raise HTTPException(status_code=400, detail="local_file must be a .zip or .json file")
    if not path.is_relative_to(data_dir):
        raise HTTPException(
            status_code=400,
            detail=f"local_file must be inside the data directory ({settings.BODS_DATA_DIR})",
        )
    if not path.is_file():
        raise HTTPException(status_code=400, detail="local_file not found")
    return str(path)

@router.get("/bods/status")
def bods_status():
    """Check enabled status for both BODS sources (GLEIF and UK PSC)."""
    return {
        "gleif_enabled":      settings.SCRAPER_ENABLED and settings.SCRAPER_BODS_GLEIF_ENABLED,
        "uk_psc_enabled":     settings.SCRAPER_ENABLED and settings.SCRAPER_BODS_UK_PSC_ENABLED,
        "master_switch":      settings.SCRAPER_ENABLED,
        "bods_gleif_switch":  settings.SCRAPER_BODS_GLEIF_ENABLED,
        "bods_uk_psc_switch": settings.SCRAPER_BODS_UK_PSC_ENABLED,
    }


@router.post("/bods/gleif/run")
def bods_gleif_run(
    limit: int | None = Query(
        None, ge=1,
        description="Max entity statements to process. Omit for the full ~5 M-entity dataset.",
    ),
    filter_jurisdiction: str | None = Query(
        None, min_length=2, max_length=2,
        description="ISO alpha-2 country code to restrict entity imports, e.g. 'DE'.",
    ),
    local_file: str | None = Query(
        None,
        description="Path to a pre-downloaded .zip or .json file. "
                    "Skips the ~1.1 GB download when given.",
    ),
    _: dict = Depends(require_contributor),
):
    """
    Import the GLEIF BODS dataset (CC0) into the graph.
    Downloads ~1.1 GB if no local_file is given; allow 10–30 min for the full dataset.
    Requires SCRAPER_ENABLED=true AND SCRAPER_BODS_GLEIF_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    if not settings.SCRAPER_BODS_GLEIF_ENABLED:
        raise HTTPException(status_code=403,
            detail="GLEIF scraper is disabled. Set SCRAPER_BODS_GLEIF_ENABLED=true.")
    local_file = _validate_bods_local_file(local_file)
    try:
        return run_import_bods_gleif(
            limit=limit,
            filter_jurisdiction=filter_jurisdiction,
            local_file=local_file,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("GLEIF BODS import failed")
        raise HTTPException(status_code=500, detail="GLEIF import failed. Check server logs for details.")


@router.post("/bods/uk-psc/run")
def bods_uk_psc_run(
    limit: int | None = Query(
        None, ge=1,
        description="Max entity statements to process. Omit for the full ~8 M-entity dataset.",
    ),
    local_file: str | None = Query(
        None,
        description="Path to a pre-downloaded .zip or .json file. "
                    "Skips the ~3.3 GB download when given.",
    ),
    _: dict = Depends(require_contributor),
):
    """
    Import the UK PSC BODS dataset (CC0) into the graph.
    Downloads ~3.3 GB if no local_file is given; allow 30–90 min for the full dataset.
    Requires SCRAPER_ENABLED=true AND SCRAPER_BODS_UK_PSC_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    if not settings.SCRAPER_BODS_UK_PSC_ENABLED:
        raise HTTPException(status_code=403,
            detail="UK PSC scraper is disabled. Set SCRAPER_BODS_UK_PSC_ENABLED=true.")
    local_file = _validate_bods_local_file(local_file)
    try:
        return run_import_bods_uk_psc(limit=limit, local_file=local_file)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("UK PSC BODS import failed")
        raise HTTPException(status_code=500, detail="UK PSC import failed. Check server logs for details.")


@router.post("/bods/run-all")
def bods_run_all(
    limit: int | None = Query(None, ge=1, description="Max entity statements per source."),
    _: dict = Depends(require_contributor),
):
    """
    Run both GLEIF and UK PSC imports if their respective flags are enabled.
    Disabled sources are skipped and reported with status 'disabled'.
    Requires SCRAPER_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")

    results: dict = {}

    if settings.SCRAPER_BODS_GLEIF_ENABLED:
        try:
            results["gleif"] = run_import_bods_gleif(limit=limit)
        except PermissionError as e:
            results["gleif"] = {"status": "disabled", "detail": str(e)}
        except Exception:
            logger.exception("GLEIF BODS import failed (run-all)")
            results["gleif"] = {"status": "error", "detail": "Import failed. Check server logs for details."}
    else:
        results["gleif"] = {"status": "disabled"}

    if settings.SCRAPER_BODS_UK_PSC_ENABLED:
        try:
            results["uk_psc"] = run_import_bods_uk_psc(limit=limit)
        except PermissionError as e:
            results["uk_psc"] = {"status": "disabled", "detail": str(e)}
        except Exception:
            logger.exception("UK PSC BODS import failed (run-all)")
            results["uk_psc"] = {"status": "error", "detail": "Import failed. Check server logs for details."}
    else:
        results["uk_psc"] = {"status": "disabled"}

    return {"status": "ok", "results": results}
