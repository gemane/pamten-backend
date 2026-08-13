"""
Backfill geocoding for entities that have an address but no coordinates.

Two independent passes, because a company has **two** places and they are not the
same question:

* **headquarters** — where it is actually run. `hq_address` → `hq_lat/hq_lng`.
* **registered office** — the address on the register, which for an offshore
  company is its agent's door: BARCLAYS CAPITAL (CAYMAN) is "c/o Maples
  Corporate Services Limited, Ugland House, Grand Cayman" and is run from
  London. `address` → `reg_lat/reg_lng`.

The registered pass exists because the map's Registered/Headquarters switch had
nothing to draw under Registered — the addresses were being imported and stored
all along, and only the geocoder was ever pointed at the HQ fields.

Idempotent and resumable: each pass selects on its own NULL coordinate, so a
re-run picks up what is still missing (or what failed last time). Rate limiting
and caching are handled by the geocoding service.
"""
import logging

from app.db.arcadedb import run_query, run_command
from app.scraper.geocode import geocode_address, geocode_full

log = logging.getLogger(__name__)

HQ = "hq"
REGISTERED = "registered"

#: What each pass reads and writes. Keeping them as data rather than two
#: near-identical functions means a fix to one cannot silently miss the other.
_PASSES = {
    HQ: {
        "lat": "hq_lat", "lng": "hq_lng", "precision": "hq_geo_precision",
        # The full HQ address first (street-level), then city/country (approximate).
        "full": "hq_address", "city": "hq_city", "country": "hq_country",
    },
    REGISTERED: {
        "lat": "reg_lat", "lng": "reg_lng", "precision": "reg_geo_precision",
        # `address` is the human-readable legal address; `registered_address` is
        # the normalised lowercase form kept for dedup, which geocodes worse.
        #
        # NO fallback: the address resolves or there is no pin. There is no
        # registered *city* column, so the only coarser thing available is the
        # country — and geocoding a bare country returns its centroid. The first
        # run of this pass put 51 American companies in a field in Kansas and 39
        # British ones in the Irish Sea, all claiming to be registered offices.
        # An absent pin says "we do not know"; a centroid says something false.
        "full": "address", "city": None, "country": None,
    },
}


def _run_pass(name: str, limit: int | None) -> dict:
    f = _PASSES[name]
    sources = [f["full"], f["city"], f["country"]]
    have_any = " OR ".join(f"e.{c} IS NOT NULL" for c in sources if c)

    query = f"""
        MATCH (e:Entity)
        WHERE e.{f['lat']} IS NULL AND ({have_any})
        RETURN e.id AS id, e.{f['full']} AS full,
               {f"e.{f['city']} AS city," if f['city'] else "null AS city,"}
               {f"e.{f['country']} AS country" if f['country'] else "null AS country"}
    """
    if limit is not None:
        query += f"\n        LIMIT {int(limit)}"

    rows = run_query(query)
    geocoded = 0
    for r in rows:
        # Prefer the full address for a street-level pin; fall back to
        # city/country (approximate). Store which precision we got so the map can
        # show a pin rather than implying a building it does not know.
        coord = precision = None
        hit = geocode_full(r.get("full")) if r.get("full") else None
        if hit:
            coord, precision = hit
        # Only a pass that declares a coarse source may fall back to one. Gating
        # on the row instead would quietly reinstate the fallback the moment a
        # caller (or a test) passed a country along with the address.
        coarse_ok = bool(f["city"] or f["country"])
        if not coord and coarse_ok and (r.get("city") or r.get("country")):
            coord = geocode_address({"city": r.get("city"), "country": r.get("country")})
            precision = "approx"
        if not coord:
            continue
        lat, lng = coord
        run_command(
            f"MATCH (e:Entity {{id: $id}}) SET e.{f['lat']} = $lat, e.{f['lng']} = $lng, "
            f"e.{f['precision']} = $prec",
            {"id": r["id"], "lat": lat, "lng": lng, "prec": precision},
        )
        geocoded += 1

    return {"total": len(rows), "geocoded": geocoded}


def backfill(limit: int | None = None, target: str = "both") -> dict:
    """Geocode entities lacking coordinates. Returns a summary dict.

    `target` is 'hq', 'registered' or 'both'. Both by default: a fresh import
    needs each, and forgetting the registered pass is invisible until someone
    switches the map to Registered and finds the pins gone.
    """
    if target not in (HQ, REGISTERED, "both"):
        raise ValueError(f"target must be hq, registered or both — got {target!r}")

    names = [HQ, REGISTERED] if target == "both" else [target]
    passes = {n: _run_pass(n, limit) for n in names}

    result = {
        "passes": passes,
        # Flat totals, kept because callers and logs already read these names.
        "entities_total":    sum(p["total"] for p in passes.values()),
        "entities_geocoded": sum(p["geocoded"] for p in passes.values()),
        "geocoded":          sum(p["geocoded"] for p in passes.values()),
    }
    log.info("Geocode backfill: %s", result)
    return result
