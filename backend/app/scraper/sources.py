"""
Per-source scraper toggles, stored as ScraperSource nodes in ArcadeDB.
These are independent of the master SCRAPER_ENABLED env flag.
"""
from fastapi import APIRouter, HTTPException, Depends
from app.database import db
from app.auth.dependencies import require_admin

router = APIRouter(prefix="/scraper/sources", tags=["Scraper"])

KNOWN_SOURCES = {
    "wikidata":         "Wikidata — structured corporate data via SPARQL",
    "sec_edgar":        "SEC EDGAR — legally required US ownership filings (SC 13D/13G, Form 3/4)",
    "open_corporates":  "OpenCorporates — official company registers from 200+ jurisdictions",
    "gleif":            "GLEIF — Global LEI Foundation legal-entity identifiers (CC0)",
    "uk_psc":           "UK PSC — Companies House persons-with-significant-control register (CC0)",
}


def _ensure_sources():
    """Create default ScraperSource nodes if they don't exist."""
    with db.get_session() as session:
        for name, description in KNOWN_SOURCES.items():
            session.run(
                """
                MERGE (s:ScraperSource {name: $name})
                ON CREATE SET s.enabled = true, s.description = $desc
                """,
                name=name, desc=description,
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
            "MATCH (s:ScraperSource) RETURN s.name AS name, s.enabled AS enabled, s.description AS description"
        )
        return [
            {"name": r["name"], "enabled": bool(r["enabled"]), "description": r["description"]}
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
