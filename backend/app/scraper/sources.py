"""
Per-source scraper toggles, stored as ScraperSource nodes in ArcadeDB.
These are independent of the master SCRAPER_ENABLED env flag.
"""
from fastapi import APIRouter, HTTPException, Depends
from app.database import db
from app.auth.dependencies import require_admin

router = APIRouter(prefix="/scraper/sources", tags=["Scraper"])

# The catalogue of everything this instance can draw on. One entry per source, and
# the single definition of its metadata: `runner.py` reads the label/url/credibility
# from here rather than keeping its own copies, so the public catalogue and the
# provenance stamped onto scraped data can never disagree.
#
# KIND — "instant" (query-driven, per-company, run on demand by the search-triggered
# scrape) or "bulk" (whole-dataset import — GLEIF/UK PSC, never triggered by search).
#
# CREDIBILITY — 0-100, the tie-breaker when two sources claim different things about
# the same relationship (see app/claims.py). `quality` is the band it belongs to,
# which is what the UI shows: a bare "98" says nothing without "legally mandated".
#
# ⚠️ `enabled` (a per-source toggle, stored on the ScraperSource node) governs whether
# a source RUNS — it says nothing about whether its data is already in the graph. Both
# bulk sources are toggled off while their data is loaded and in active use. Do not
# present the toggle as "this source is not used".
KNOWN_SOURCES = {
    "wikidata": {
        "kind": "instant",
        "label": "Wikidata",
        "url": "https://www.wikidata.org",
        "credibility": 80,
        "quality": "community",
        "description": "Wikidata — structured corporate data via SPARQL",
        "region": "Global",
        "coverage": "Descriptions, people, logos and websites for globally notable "
                    "companies — the soft layer no register carries",
    },
    "sec_edgar": {
        "kind": "instant",
        "label": "SEC EDGAR",
        "url": "https://www.sec.gov/edgar",
        "credibility": 98,
        "quality": "statutory",
        "description": "SEC EDGAR — legally required US ownership filings (SC 13D/13G, Form 3/4)",
        "region": "US",
        "coverage": "Ownership of US-listed companies — 13D/G control stakes, 13F "
                    "institutional holdings, Form 3/4 insider filings",
    },
    "open_corporates": {
        "kind": "instant",
        "label": "OpenCorporates",
        "url": "https://opencorporates.com",
        "credibility": 85,
        "quality": "aggregated",
        "description": "OpenCorporates — official company registers from 200+ jurisdictions",
        "region": "200+ jurisdictions",
        "coverage": "Company registrations and register numbers, looked up on demand "
                    "for jurisdictions this platform does not read directly",
    },
    "bods_gleif": {
        "kind": "bulk",
        "label": "GLEIF",
        "url": "https://www.gleif.org",
        "credibility": 92,
        "quality": "official",
        "description": "GLEIF – Global Legal Entity Identifier "
                       "(corporate ownership, worldwide, CC0)",
        "region": "Global",
        "coverage": "Legal entities worldwide that hold an LEI, and their accounting "
                    "consolidation relationships (who consolidates whom)",
    },
    "bods_uk_psc": {
        "kind": "bulk",
        "label": "UK PSC",
        "url": "https://www.gov.uk/government/publications/persons-with-significant-control-register",
        "credibility": 97,
        "quality": "statutory",
        "description": "UK People with Significant Control Register "
                       "(beneficial ownership, UK companies, CC0)",
        "region": "GB",
        "coverage": "Beneficial owners — the persons and companies with significant "
                    "control over UK-registered companies",
    },
}


def _ensure_sources():
    """Create default ScraperSource nodes if they don't exist."""
    with db.get_session() as session:
        for name, meta in KNOWN_SOURCES.items():
            session.run(
                """
                MERGE (s:ScraperSource {name: $name})
                ON CREATE SET s.enabled = true, s.description = $desc,
                              s.data_mode = 'full'
                SET s.kind = $kind
                """,
                name=name, desc=meta["description"], kind=meta["kind"],
            )


def get_source_enabled(name: str) -> bool:
    _ensure_sources()
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:ScraperSource {name: $name}) RETURN s.enabled AS enabled",
            name=name,
        ).single()
        return bool(rec["enabled"]) if rec else False


@router.get("")
def list_sources():
    _ensure_sources()
    with db.get_session() as session:
        records = session.run(
            "MATCH (s:ScraperSource) RETURN s.name AS name, s.enabled AS enabled, "
            "s.description AS description, s.kind AS kind, s.data_mode AS data_mode"
        )
        rows = []
        for r in records:
            meta = KNOWN_SOURCES.get(r["name"], {})
            rows.append({
                "name": r["name"],
                "enabled": bool(r["enabled"]),
                "description": r["description"],
                # `kind` may be null on rows created before this field existed → fall
                # back to the declared kind (or "instant" for anything unknown).
                "kind": r["kind"] or meta.get("kind", "instant"),
                # Catalogue metadata, served to everyone: this endpoint is public, and
                # where the data comes from and how far to trust it is the case for the
                # whole platform rather than something to keep behind a login.
                "label": meta.get("label", r["name"]),
                "url": meta.get("url"),
                "credibility": meta.get("credibility"),
                "quality": meta.get("quality"),
                # What the source is authoritative for, and where — catalogue-
                # served like label/url, so edits apply without touching the
                # DB node (whose description is ON CREATE only).
                "region": meta.get("region"),
                "coverage": meta.get("coverage"),
                # `full` draws edges; `claims_only` records claims and enriches
                # entities but never draws structure. Null on pre-field rows
                # reads as full — the mode is an opt-in restriction.
                "data_mode": r["data_mode"] or "full",
            })
        return rows


_DATA_MODES = ("full", "claims_only")

# data_mode per Source-NODE id, cached briefly: the bulk importers consult
# this once per edge, and a 60-second lag on an admin mode flip is invisible
# while a per-edge point read at GLEIF scale is not. Same pattern as the
# stats cache.
_MODE_CACHE: dict = {"at": 0.0, "by_source_id": {}}
_MODE_CACHE_TTL = 60.0


def _mode_by_source_id() -> dict:
    """Source-node id → data_mode, for every catalogue source.

    Edges carry the Source NODE's uuid, not the catalogue slug; the bridge is
    the label (Source.name == KNOWN_SOURCES[slug]["label"]). Unknown labels
    (a peer's federated source, say) read as full — restriction is opt-in."""
    import time
    now = time.monotonic()
    if now - _MODE_CACHE["at"] < _MODE_CACHE_TTL:
        return _MODE_CACHE["by_source_id"]
    by_id: dict = {}
    try:
        label_to_mode = {}
        with db.get_session() as session:
            for r in session.run("MATCH (s:ScraperSource) RETURN s.name AS name, "
                                 "s.data_mode AS data_mode"):
                meta = KNOWN_SOURCES.get(r["name"])
                if meta:
                    label_to_mode[meta["label"]] = r["data_mode"] or "full"
            for r in session.run("MATCH (s:Source) RETURN s.id AS id, s.name AS name"):
                mode = label_to_mode.get(r["name"])
                if mode:
                    by_id[r["id"]] = mode
    except Exception:  # noqa: BLE001 - fail open, and CACHE the failure:
        # a mocked suite (or a flaky DB) must produce one failed read per TTL,
        # not one per edge — repeated failed reads are how the ArcadeDB
        # brute-force lockout gets triggered from tests.
        by_id = {}
    _MODE_CACHE["at"] = now
    _MODE_CACHE["by_source_id"] = by_id
    return by_id


def edge_writes_suppressed(source_id: str | None) -> bool:
    """True when the source that owns ``source_id`` is in claims-only mode.

    Consulted by every edge writer AFTER the claim is recorded: a claims-only
    source may assert (provenance kept, corroboration visible) but not draw
    structure. Fail-open on any error — a mode lookup must never break a
    scrape."""
    if not source_id:
        return False
    try:
        return _mode_by_source_id().get(source_id) == "claims_only"
    except Exception:  # noqa: BLE001 - never break a write over a mode read
        return False


@router.patch("/{name}/mode")
def set_source_mode(name: str, mode: str, _: dict = Depends(require_admin)):
    """Set a source's data mode: ``full`` draws edges, ``claims_only``
    records claims and enriches entities but never draws structure.

    Does NOT touch existing edges — removing what a source already drew is
    the explicitly destructive POST /{name}/sweep-edges. Reverting is
    mode=full plus a re-scrape/re-import."""
    if name not in KNOWN_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {name}")
    if mode not in _DATA_MODES:
        raise HTTPException(status_code=422,
                            detail=f"mode must be one of {_DATA_MODES}")
    _ensure_sources()
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:ScraperSource {name: $name}) SET s.data_mode = $mode "
            "RETURN s.data_mode AS data_mode",
            name=name, mode=mode).single()
        if not rec:
            raise HTTPException(status_code=404, detail="Source not found")
    _MODE_CACHE["at"] = 0.0   # a mode flip should not wait out the cache
    return {"name": name, "data_mode": rec["data_mode"]}


@router.post("/{name}/sweep-edges")
def sweep_source_edges(name: str, confirm: str, _: dict = Depends(require_admin)):
    """Delete every EDGE this source drew — nodes and claims stay.

    The retro half of claims-only: flipping the mode stops new structure,
    this removes the old. Destructive, so the wipe-source retype guard,
    HTTP-shaped: ``confirm`` must equal the source name exactly."""
    if name not in KNOWN_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {name}")
    if confirm != name:
        raise HTTPException(status_code=422,
                            detail="confirm must equal the source name exactly")
    from app.scraper.maintenance import wipe_source_edges
    label = KNOWN_SOURCES[name]["label"]
    try:
        result = wipe_source_edges(label)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"name": name, "source": label, **result}


@router.patch("/{name}/toggle")
def toggle_source(name: str, _: dict = Depends(require_admin)):
    if name not in KNOWN_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {name}")
    _ensure_sources()
    with db.get_session() as session:
        rec = session.run(
            """
            MATCH (s:ScraperSource {name: $name})
            SET s.enabled = NOT s.enabled
            RETURN s.enabled AS enabled
            """,
            name=name,
        ).single()
        if not rec:
            raise HTTPException(status_code=404, detail="Source not found")
        return {"name": name, "enabled": bool(rec["enabled"])}
