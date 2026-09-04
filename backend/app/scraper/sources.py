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
                ON CREATE SET s.enabled = true, s.description = $desc
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
            "s.description AS description, s.kind AS kind"
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
            })
        return rows


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
