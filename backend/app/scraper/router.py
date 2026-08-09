import logging
import threading
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from app.config import settings
from app.scraper.runner import (
    run_scrape, run_scrape_sec_edgar, run_scrape_all, run_scrape_open_corporates,
)  # noqa: F401 - importing runner also registers the built-in scrapers in the registry
from app.auth.dependencies import (
    require_admin, require_contributor, require_verified, get_current_user_optional,
)
from app.scraper import maintenance, proxy_write
from app.scraper.run_log import record_run, list_runs
from app.scraper.scraper_registry import get as _get_scraper, registered as _registered_scrapers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scraper", tags=["Scraper"])


class ScrapeRequest(BaseModel):
    query: str = Field(..., min_length=2, description="Company or brand name to search on Wikidata")
    depth: int = Field(2, ge=0, le=3, description="How many subsidiary levels to follow (0–3)")


class EnsureRequest(BaseModel):
    query: str = Field(..., min_length=2, description="Company name to ensure is present + fresh")
    depth: int = Field(1, ge=0, le=3, description="Ownership depth to reach (phase 1 = 1, idle phase 2 = 2)")
    force: bool = Field(False, description="Re-scrape even if the company is fresh (< TTL)")


# ── On-demand enrichment (any verified user) ──────────────────────────────────

@router.post("/ensure")
def scraper_ensure(body: EnsureRequest, _: dict = Depends(require_verified)):
    """Ensure a company is in the graph and fresh, scraping the enabled **instant**
    sources (Wikidata, SEC EDGAR, OpenCorporates — never bulk/GLEIF) only when it's
    absent, never on-demand-scraped, stale (> TTL days), the caller forces it, or a
    deeper pass is requested. Open to any authenticated + email-verified user. Degrades
    to a DB-only response when the master switch is off. See app/scraper/ondemand.py."""
    from app.scraper.ondemand import ensure_scrape
    try:
        return ensure_scrape(body.query, body.depth, body.force)
    except Exception:
        logger.exception("ensure-scrape failed (query=%r, depth=%s, force=%s)",
                         body.query, body.depth, body.force)
        raise HTTPException(status_code=500, detail="On-demand scrape failed. Check server logs.")


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


# Roles allowed to see *why* a run failed. Everyone else still sees that it did.
_RUN_DETAIL_ROLES = ("admin", "contributor")


@router.get("/runs")
def scraper_runs(
    limit: int = Query(50, ge=1, le=500, description="Max run records to return"),
    user: dict | None = Depends(get_current_user_optional),
):
    """Recent scrape runs (newest first) — what ran, when, node counts, and failures.

    Public, because what the platform ingests is exactly the kind of thing an
    ownership-transparency project should be transparent about. What ran, against
    which company, when, and how many nodes came of it are all publishable.

    The ``error`` field is not. It carries raw exception text, which can include
    internal URLs, database errors, or a credential embedded in a failing request
    URL — so it is stripped for anyone outside _RUN_DETAIL_ROLES. They still get
    ``status: "failed"``; they just don't get the stack's opinion about why.

    Redaction lives here rather than in ``list_runs()`` so the run log stays a
    plain data accessor with no notion of who is asking.
    """
    runs = list_runs(limit)
    if (user or {}).get("role") not in _RUN_DETAIL_ROLES:
        runs = [{k: v for k, v in run.items() if k != "error"} for run in runs]
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


# ── Generic per-source endpoints (registry-driven) ────────────────────────────
# Any registered scraper is reachable here with NO per-scraper router code — a new
# scraper just registers a ScraperSpec (see app/scraper/scraper_registry.py). The
# named endpoints above are kept for the built-ins the current frontend calls.

@router.get("/registry")
def scraper_registry():
    """List the registered scrapers and whether each is currently runnable
    (master switch AND the scraper's own enabled predicate)."""
    return {
        "master_switch": settings.SCRAPER_ENABLED,
        "scrapers": [
            {"name": s.name, "enabled": settings.SCRAPER_ENABLED and s.enabled()}
            for s in _registered_scrapers()
        ],
    }


@router.get("/source/{name}/status")
def scraper_source_status(name: str):
    """Enabled state for one registered scraper by name."""
    spec = _get_scraper(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No scraper named {name!r}")
    return {
        "name": name,
        "enabled": settings.SCRAPER_ENABLED and spec.enabled(),
        "master_switch": settings.SCRAPER_ENABLED,
    }


@router.post("/source/{name}/run")
def scraper_source_run(
    name: str,
    company: str = Query(..., min_length=2, description="Company name to scrape"),
    depth:   int = Query(2, ge=0, le=3, description="Subsidiary depth (0–3; ignored by sources that don't traverse)"),
    _: dict = Depends(require_contributor),
):
    """Run one registered scraper by name — the generic entry point for every
    scraper, current and future. Requires SCRAPER_ENABLED plus the scraper's own
    switch/source toggle."""
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403, detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    spec = _get_scraper(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No scraper named {name!r}")
    if not spec.enabled():
        raise HTTPException(status_code=403, detail=f"{name} scraper is disabled (check its switch / source toggle).")
    try:
        with record_run(name, company) as run:
            result = spec.run(company, depth)
            run["total"] = result.get("total", 0) if isinstance(result, dict) else 0
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("%s scrape failed (company=%r)", name, company)
        raise HTTPException(status_code=500, detail=f"{name} scrape failed. Check server logs for details.")


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

@router.get("/duplicate-edges/count")
def count_duplicate_owns_edges(_: dict = Depends(require_admin)):
    """How many duplicate active OWNS edges exist (read-only observability):
    {active_edges, distinct_pairs, duplicate_pairs, redundant_edges}. Admin only."""
    return maintenance.count_duplicate_owns_edges()


@router.get("/duplicate-entities/name-count")
def count_duplicate_entity_names(_: dict = Depends(require_admin)):
    """How many same-name entity duplicate groups exist — the same company under
    different identifiers (e.g. two GLEIF LEIs) that the id-based dedup can't see.
    Read-only. Admin only."""
    return maintenance.count_duplicate_entity_names()


@router.get("/duplicate-entities/name-candidates")
def list_duplicate_entity_names(limit: int = 100, min_confidence: str | None = None,
                                _: dict = Depends(require_admin)):
    """The biggest same-name duplicate groups, each tagged with a confidence that
    the members are the same company (definitive/high/medium/low), with members
    (id/country/lei/address) for review. `min_confidence` filters the list.
    Admin only."""
    return {"candidates": maintenance.find_duplicate_entity_names(limit, min_confidence)}


@router.post("/deduplicate-edges")
def deduplicate_owns_edges(_: dict = Depends(require_admin)):
    """Collapse duplicate active OWNS edges, keeping the largest stake. Admin only."""
    return maintenance.deduplicate_owns_edges()


@router.post("/mark-shortcuts")
def mark_ownership_shortcuts(
    limit: int | None = Query(None, ge=1, description="Max parents to process; omit for all"),
    _: dict = Depends(require_admin),
):
    """Flag GLEIF ultimate-parent edges that duplicate a path already in the graph,
    so the renderer can omit them without losing companies whose only link is a
    shortcut. Re-run after every import — a delta can turn a redundant edge into a
    load-bearing one. Admin only."""
    return maintenance.mark_ownership_shortcuts(limit=limit)


# ── Person deduplication endpoint ──────────────────────────────────────────────

@router.post("/deduplicate-persons")
def deduplicate_person_nodes(_: dict = Depends(require_admin)):
    """Merge reversed-name Person duplicates and migrate their edges. Admin only."""
    return maintenance.deduplicate_person_nodes()


# ── Entity deduplication endpoint ──────────────────────────────────────────────

_dedup_lock = threading.Lock()
_dedup_running = False


def _dedup_entities_job(strategy: str) -> None:
    """Run the entity dedup in a background thread, logged as a ScrapeRun so
    progress/outcome shows up in GET /scraper/runs like any scrape."""
    global _dedup_running
    try:
        with record_run("deduplicate-entities", strategy) as out:
            if strategy == "merge":
                out["total"] = maintenance.deduplicate_entities(limit=None)["entities_merged"]
            else:  # "bulk" — fast delete-redundant heal (the practical one at scale)
                out["total"] = maintenance.deduplicate_entities_bulk()["entities_removed"]
    except Exception:  # noqa: BLE001 - record_run already logged 'failed'
        logger.exception("deduplicate-entities job failed")
    finally:
        with _dedup_lock:
            _dedup_running = False


@router.post("/deduplicate-entities")
def deduplicate_entities(background: bool = True, strategy: str = "bulk", limit: int = 300,
                         _: dict = Depends(require_admin)):
    """Heal the old recordId-keyed BODS doubling: collapse Entity duplicates that
    share an LEI / Companies House number. Admin only.

    Runs **in the background** by default (returns immediately; poll
    `GET /scraper/runs`, source `deduplicate-entities`) because at full-GLEIF
    scale even the grouping scan exceeds the request timeout. Strategies:
    `bulk` (default) keeps one node per id and deletes the rest (fast, drops the
    losers' edges — survivor already carries the import's); `merge` migrates the
    losers' edges first (correct but only finishes on small data). Pass
    `background=false` to run the bounded-batch merge synchronously (`limit`
    groups, returns `remaining`)."""
    if not background:
        return maintenance.deduplicate_entities(limit=limit)

    global _dedup_running
    with _dedup_lock:
        if _dedup_running:
            return {"status": "already_running",
                    "message": "A deduplicate-entities job is already in progress; poll GET /scraper/runs."}
        _dedup_running = True
    threading.Thread(target=_dedup_entities_job, args=(strategy,), daemon=True).start()
    return {"status": "started", "strategy": strategy,
            "message": "Deduplicating entities in the background; poll GET /scraper/runs (source=deduplicate-entities)."}


# ── Geocode endpoint ───────────────────────────────────────────────────────────

@router.post("/geocode")
def geocode_backfill_run(
    limit: int | None = Query(None, ge=1, description="Max nodes to geocode this run"),
    _: dict = Depends(require_contributor),
):
    """
    Backfill HQ coordinates via Nominatim for entities that have an address or
    a city/country but no coordinates. Gated by GEOCODING_ENABLED (env), so
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


@router.post("/backfill-entity-sources")
def backfill_entity_sources(_: dict = Depends(require_admin)):
    """One-time backfill stamping source_id on Wikidata/SEC entities that predate
    the fix — restores the source panel for pure owners (e.g. a government or a
    large fund with no inbound owners). Idempotent. Admin only."""
    return maintenance.backfill_entity_sources()


@router.post("/flag-nominees")
def flag_nominees(_: dict = Depends(require_admin)):
    """Flag nominee/custodian entities (holders of record — '… Nominees Limited',
    custodians, Cede & Co) by name, so they don't masquerade as beneficial owners.
    Name-derived + idempotent; backfills nodes imported before the flag. Admin only."""
    return maintenance.flag_nominee_entities()


@router.get("/ownership-quality")
def ownership_quality(limit: int = 100, _: dict = Depends(require_admin)):
    """Ownership data-quality report: self-loop OWNS edges (A owns A — treasury/
    error) and circular ownership pairs (A↔B). Read-only. Admin only."""
    return {
        **maintenance.count_self_loop_owns(),
        "cross_holdings": maintenance.find_cross_holdings(limit),
    }


# ── Bulk-import status ────────────────────────────────────────────────────────

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


# Bulk datasets (GLEIF golden copy + Companies House PSC / register) are imported
# from the CLI — manage.py gleif-lei-cdf / gleif-rr / gleif-succession / ch-psc /
# ch-company-data — not over HTTP: the source files are multi-GB local batch loads
# run in a tmux session on the server, not URL fetches triggered from the web app.
