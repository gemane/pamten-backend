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

**Structured, not free text.** Nominatim takes street/city/postcode/country as
separate fields, and the sources hand them over that way — GLEIF has
AddressLines/City/PostalCode/Country, SEC EDGAR has street1/city/zipCode. Asking
a gazetteer to work out which comma-separated piece was the city is a problem we
were creating for ourselves, and the hand-written rules that did it encoded one
country's conventions. The parts are used when we have them; a row that only ever
stored the assembled string still gets asked plainly, unmodified.
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
        # The parts first; the assembled string only for rows that have no parts.
        "street": "hq_street", "postcode": "hq_postcode",
        "full": "hq_address", "city": "hq_city", "country": "hq_country",
    },
    REGISTERED: {
        "lat": "reg_lat", "lng": "reg_lng", "precision": "reg_geo_precision",
        "street": "reg_street", "postcode": "reg_postcode",
        # `address` is the human-readable legal address; `registered_address` is
        # the normalised lowercase form kept for dedup, which geocodes worse.
        #
        # NO country fallback: the address resolves or there is no pin. Geocoding
        # a bare country returns its centroid, and the first run of this pass put
        # 51 American companies in a field in Kansas and 39 British ones in the
        # Irish Sea, all captioned as registered offices. An absent pin says "we
        # do not know"; a centroid says something false.
        "full": "address", "city": "reg_city", "country": None,
    },
}


def _run_pass(name: str, limit: int | None, ids: list[str] | None = None) -> dict:
    f = _PASSES[name]
    cols = {k: f.get(k) for k in ("street", "city", "postcode", "country", "full")}
    have_any = " OR ".join(f"e.{c} IS NOT NULL" for c in cols.values() if c)
    # Scoped to the ids a scrape just touched, when given. Inlined rather than
    # parameterised: ArcadeDB's Cypher will not take a list parameter (see the
    # gotchas in maintenance.py), and the ids are internally generated.
    scope = ""
    if ids is not None:
        quoted = ", ".join("'" + i.replace("'", "") + "'" for i in ids)
        scope = f" AND e.id IN [{quoted}]"

    selected = ", ".join(f"e.{col} AS {alias}" if col else f"null AS {alias}"
                         for alias, col in cols.items())
    query = f"""
        MATCH (e:Entity)
        WHERE e.{f['lat']} IS NULL AND ({have_any}){scope}
        RETURN e.id AS id, {selected}
    """
    if limit is not None:
        query += f"\n        LIMIT {int(limit)}"

    rows = run_query(query)
    geocoded = 0
    for r in rows:
        coord, precision = _locate(r, coarse_country=bool(f["country"]))
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


def _locate(row: dict, *, coarse_country: bool) -> tuple[tuple[float, float] | None, str | None]:
    """Coordinates for one row, best available first.

    1. **The parts**, structured — street, city, postcode, country as separate
       fields, which is how the sources gave them and how Nominatim wants them.
    2. **The assembled string**, unmodified, for rows that predate the parts or
       come from a source that only ever had one. Asked plainly: the rules that
       used to rewrite it encoded one country's conventions and guessed at the
       rest.
    3. **City and country**, coarse, and only for a pass that allows it — the
       registered pass does not, because a bare country is a centroid.
    """
    if row.get("street") or row.get("postcode"):
        coord = geocode_address({"street": row.get("street"), "city": row.get("city"),
                                 "zip": row.get("postcode"), "country": row.get("country")})
        if coord:
            # Structured queries carry no place_rank in the same way; a street
            # given in full is a street-level answer.
            return coord, ("exact" if row.get("street") else "approx")

    if row.get("full"):
        hit = geocode_full(row["full"])
        if hit:
            return hit

    if coarse_country and (row.get("city") or row.get("country")):
        coord = geocode_address({"city": row.get("city"), "country": row.get("country")})
        if coord:
            return coord, "approx"

    return None, None


def geocode_entities(ids: list[str], target: str = "both") -> dict:
    """Geocode a specific set of entities — the ones a scrape just touched.

    Same two passes as the batch backfill, so there is one definition of what a
    company's two places are and how they are filled. Bounded by the caller:
    Nominatim allows one request a second, and a scrape a user is waiting on can
    afford a couple, not a couple of hundred.
    """
    if not ids:
        return {"passes": {}, "entities_total": 0, "entities_geocoded": 0, "geocoded": 0}
    names = [HQ, REGISTERED] if target == "both" else [target]
    passes = {n: _run_pass(n, None, ids) for n in names}
    return {
        "passes": passes,
        "entities_total":    sum(p["total"] for p in passes.values()),
        "entities_geocoded": sum(p["geocoded"] for p in passes.values()),
        "geocoded":          sum(p["geocoded"] for p in passes.values()),
    }


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
