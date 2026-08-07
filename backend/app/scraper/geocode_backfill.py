"""
Backfill geocoding for entities that have an address but no coordinates.

Idempotent and resumable: only entities with a NULL hq_lat are selected, so a
re-run picks up what is still missing (or what failed last time). Rate limiting
and caching are handled by the geocoding service.

There used to be a second pass over Location nodes, whose coordinates were then
copied onto the entities pointing at them. Location is gone and the Entity holds
its own HQ, so the copy step went with it.
"""
import logging

from app.db.arcadedb import run_query, run_command
from app.scraper.geocode import geocode_address, geocode_full

log = logging.getLogger(__name__)


def backfill(limit: int | None = None) -> dict:
    """Geocode entities lacking coordinates. Returns a summary dict."""
    # Entities carry HQ directly (hq_address / hq_city / hq_country / hq_lat).
    # Geocode those with an address or city/country but no coordinates — an HQ
    # Wikidata had no P625 for, or a SEC/BODS entity with an address.
    ent_query = """
        MATCH (e:Entity)
        WHERE e.hq_lat IS NULL AND (e.hq_address IS NOT NULL OR e.hq_city IS NOT NULL
                                    OR e.hq_country IS NOT NULL)
        RETURN e.id AS id, e.hq_address AS hq_address, e.hq_city AS city,
               e.hq_country AS country
    """
    if limit is not None:
        ent_query += f"\n        LIMIT {int(limit)}"

    ent_rows = run_query(ent_query)
    ent_geocoded = 0
    for r in ent_rows:
        # Prefer the full HQ address for a street-level pin; fall back to city/country
        # (approximate). Store which precision we got so the map shows a pin vs a circle.
        coord = precision = None
        hit = geocode_full(r.get("hq_address")) if r.get("hq_address") else None
        if hit:
            coord, precision = hit
        if not coord and (r.get("city") or r.get("country")):
            coord = geocode_address({"city": r.get("city"), "country": r.get("country")})
            precision = "approx"
        if not coord:
            continue
        lat, lng = coord
        run_command(
            "MATCH (e:Entity {id: $id}) SET e.hq_lat = $lat, e.hq_lng = $lng, "
            "e.hq_geo_precision = $prec",
            {"id": r["id"], "lat": lat, "lng": lng, "prec": precision},
        )
        ent_geocoded += 1

    result = {
        "entities_total":    len(ent_rows),
        "entities_geocoded": ent_geocoded,
        "geocoded":          ent_geocoded,
    }
    log.info("Geocode backfill: %s", result)
    return result
