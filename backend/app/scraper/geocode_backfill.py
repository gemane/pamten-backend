"""
Backfill geocoding for Location nodes that have an address but no coordinates.

Idempotent and resumable: only nodes with a NULL latitude are selected, so a
re-run just picks up what is still missing (or what failed last time). Rate
limiting and caching are handled by the geocoding service. Coordinates are also
denormalized onto any Entity that points at the location, so map pins work
without re-scraping.
"""
import logging

from app.db.arcadedb import run_query, run_command
from app.scraper.geocode import geocode_address

log = logging.getLogger(__name__)


def backfill(limit: int | None = None) -> dict:
    """Geocode Location nodes lacking coordinates. Returns a summary dict."""
    query = """
        MATCH (l:Location)
        WHERE l.latitude IS NULL AND (l.city IS NOT NULL OR l.country IS NOT NULL)
        RETURN l.id AS id, l.street AS street, l.city AS city,
               l.state AS state, l.zip AS zip, l.country AS country
    """
    if limit is not None:
        query += f"\n        LIMIT {int(limit)}"

    rows = run_query(query)
    geocoded = 0
    for r in rows:
        address = {
            "street":  r.get("street"),
            "city":    r.get("city"),
            "state":   r.get("state"),
            "zip":     r.get("zip"),
            "country": r.get("country"),
        }
        coord = geocode_address(address)
        if not coord:
            continue
        lat, lng = coord
        run_command(
            "MATCH (l:Location {id: $id}) SET l.latitude = $lat, l.longitude = $lng",
            {"id": r["id"], "lat": lat, "lng": lng},
        )
        # Denormalize onto entities linked to this location (any Entity->Location edge).
        run_command(
            """
            MATCH (e:Entity)-->(l:Location {id: $id})
            SET e.hq_lat     = COALESCE(e.hq_lat, $lat),
                e.hq_lng     = COALESCE(e.hq_lng, $lng),
                e.hq_city    = COALESCE(e.hq_city, l.city),
                e.hq_country = COALESCE(e.hq_country, l.country)
            """,
            {"id": r["id"], "lat": lat, "lng": lng},
        )
        geocoded += 1

    # Entities carry HQ directly (hq_city/hq_country/hq_lat) rather than via a
    # Location node — the Wikidata scraper denormalizes it. Geocode those with a
    # city/country but no coordinates (an HQ Wikidata had no P625 for, or a
    # SEC/BODS entity with an address).
    ent_query = """
        MATCH (e:Entity)
        WHERE e.hq_lat IS NULL AND (e.hq_city IS NOT NULL OR e.hq_country IS NOT NULL)
        RETURN e.id AS id, e.hq_city AS city, e.hq_country AS country
    """
    if limit is not None:
        ent_query += f"\n        LIMIT {int(limit)}"

    ent_rows = run_query(ent_query)
    ent_geocoded = 0
    for r in ent_rows:
        coord = geocode_address({"city": r.get("city"), "country": r.get("country")})
        if not coord:
            continue
        lat, lng = coord
        run_command(
            "MATCH (e:Entity {id: $id}) SET e.hq_lat = $lat, e.hq_lng = $lng",
            {"id": r["id"], "lat": lat, "lng": lng},
        )
        ent_geocoded += 1

    result = {
        "locations_total":    len(rows),
        "locations_geocoded": geocoded,
        "entities_total":     len(ent_rows),
        "entities_geocoded":  ent_geocoded,
        "geocoded":           geocoded + ent_geocoded,
    }
    log.info("Geocode backfill: %s", result)
    return result
