"""
Per-source scraper toggles, stored as ScraperSource nodes in ArcadeDB.
These are independent of the master SCRAPER_ENABLED env flag.
"""
from fastapi import APIRouter, HTTPException, Depends
from app.database import db
from app.auth.dependencies import require_admin

router = APIRouter(prefix="/scraper/sources", tags=["Scraper"])

# Each source has a KIND: "instant" (query-driven, per-company — run on demand by the
# search-triggered scrape) or "bulk" (whole-dataset scheduled import — GLEIF/UK PSC,
# never triggered by on-demand search). New sources of either kind are added here.
KNOWN_SOURCES = {
    "wikidata":        {"kind": "instant",
                        "description": "Wikidata — structured corporate data via SPARQL"},
    "sec_edgar":       {"kind": "instant",
                        "description": "SEC EDGAR — legally required US ownership filings (SC 13D/13G, Form 3/4)"},
    "open_corporates": {"kind": "instant",
                        "description": "OpenCorporates — official company registers from 200+ jurisdictions"},
    "bods_gleif":      {"kind": "bulk",
                        "description": "GLEIF – Global Legal Entity Identifier "
                                       "(corporate ownership, worldwide, CC0)"},
    "bods_uk_psc":     {"kind": "bulk",
                        "description": "UK People with Significant Control Register "
                                       "(beneficial ownership, UK companies, CC0)"},
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
        return [
            {"name": r["name"], "enabled": bool(r["enabled"]), "description": r["description"],
             # `kind` may be null on rows created before this field existed → fall back to
             # the declared kind (or "instant" for anything unknown).
             "kind": r["kind"] or KNOWN_SOURCES.get(r["name"], {}).get("kind", "instant")}
            for r in records
        ]


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
