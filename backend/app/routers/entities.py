from fastapi import APIRouter, HTTPException, Depends, Query
from app.models.entity import EntityCreate, EntityResponse
from app.auth.dependencies import require_contributor
from app.database import db
from app.merged_ids import resolve_current_id
from app.models.person import KeepSeparateRequest
from datetime import datetime, timezone
from itertools import combinations
import uuid

router = APIRouter(prefix="/entities", tags=["Entities"])


@router.post("/", response_model=EntityResponse)
def create_entity(entity: EntityCreate, _: dict = Depends(require_contributor)):
    entity_id = str(uuid.uuid4())

    query = """
        CREATE (e:Entity {
            id: $id,
            name: $name,
            type: $type,
            country: $country,
            founded: $founded,
            revenue: $revenue,
            description: $description,
            verified: false
        })
        RETURN e
    """

    with db.get_session() as session:
        result = session.run(query,
            id=entity_id,
            **entity.model_dump()
        )
        record = result.single()
        if not record:
            raise HTTPException(status_code=500, detail="Failed to create entity")
        return {**dict(record["e"]), "id": entity_id}


@router.get("/countries")
def list_countries():
    """Return distinct country names with entity counts, sorted by count."""
    query = """
        MATCH (e:Entity)
        WHERE e.country IS NOT NULL AND e.country <> ''
        RETURN e.country AS country, count(e) AS cnt
        ORDER BY cnt DESC
    """
    with db.get_session() as session:
        result = session.run(query)
        return [{"country": r["country"], "count": r["cnt"]} for r in result]


#: Which country a company is counted under on the map.
#:
#: `jurisdiction` is where it is registered, `hq` where it is actually run. They
#: differ exactly where it is interesting — BARCLAYS CAPITAL (CAYMAN) LIMITED is
#: registered in KY and run from GB — which is why this is a choice rather than a
#: single "country" the map picks for you.
#:
#: `subdivision` is that same registration fact one level finer: the ISO 3166-2
#: legal jurisdiction GLEIF states, e.g. 'US-DE'. Only ~1% of records carry one and
#: only six countries use them at all, so these groups are sparse by nature and the
#: null group is most of the graph — a caller narrows to one country's rows ('US-')
#: rather than mapping the world by it.
#:
#: The property cannot be parameterised: ArcadeDB's Cypher will not accept
#: `e[$prop]`, so each basis is a literal query string. See the `@out.id` lesson in
#: app/scraper/maintenance.py for what that assumption costs — the query matches
#: nothing and reports success.
_BASIS_PROPERTY = {
    "jurisdiction": "e.country",
    "hq": "e.hq_country",
    "subdivision": "e.jurisdiction_code",
}


def _basis_property(basis: str) -> str:
    """The property for a basis, rejecting anything else.

    A typo must not quietly fall back to jurisdiction and render the wrong map
    with nothing to indicate it is wrong.
    """
    try:
        return _BASIS_PROPERTY[basis]
    except KeyError:
        raise HTTPException(
            status_code=422,
            detail=f"basis must be one of {', '.join(sorted(_BASIS_PROPERTY))}",
        ) from None


@router.get("/by-country")
def get_entities_by_country(basis: str = Query("jurisdiction", description="jurisdiction | hq | subdivision")):
    """Entity counts per country. Entity lists are fetched per-country on demand.

    Companies with no country for this basis come back as one group with
    `country: null` rather than being dropped. A tenth of the graph has no country
    at all — BlackRock and The Vanguard Group among them — and silently
    subtracting them leaves the map quietly wrong about how much it is showing.
    """
    prop = _basis_property(basis)
    # Secondary sort so equal counts return in a stable order. The frontend
    # re-sorts anyway, but an endpoint whose row order varies between identical
    # calls is a trap for anything else that reads it.
    placed = f"""
        MATCH (e:Entity)
        WHERE {prop} IS NOT NULL AND {prop} <> ''
        RETURN {prop} AS country, count(e) AS cnt
        ORDER BY cnt DESC, country ASC
    """
    unplaced = f"""
        MATCH (e:Entity)
        WHERE {prop} IS NULL OR {prop} = ''
        RETURN count(e) AS cnt
    """
    with db.get_session() as session:
        groups = [{"country": rec["country"], "count": rec["cnt"]} for rec in session.run(placed)]
        rec = session.run(unplaced).single()
        missing = (rec["cnt"] if rec else 0) or 0
        if missing:
            groups.append({"country": None, "count": missing})
    return groups


@router.get("/without-country")
def get_entities_without_country(
    basis: str = Query("jurisdiction", description="jurisdiction | hq | subdivision"),
    limit: int = Query(200, ge=1, le=500),
):
    """The companies behind the `country: null` group — the ones the map cannot place.

    A query endpoint rather than `/by-country/none`, which would be a magic path
    segment that a real country code could one day collide with.
    """
    prop = _basis_property(basis)
    query = f"""
        MATCH (e:Entity)
        WHERE {prop} IS NULL OR {prop} = ''
        RETURN e.id AS id, e.name AS name, e.type AS type
        ORDER BY e.name
        LIMIT $limit
    """
    with db.get_session() as session:
        result = session.run(query, limit=limit)
        return [{"id": r["id"], "name": r["name"], "type": r["type"]} for r in result]


@router.get("/by-country/{country}")
def get_entities_for_country(
    country: str,
    basis: str = Query("jurisdiction", description="jurisdiction | hq | subdivision"),
    limit: int = Query(200, ge=1, le=500),
):
    """Return up to `limit` entities for a specific country, ordered by name."""
    prop = _basis_property(basis)
    query = f"""
        MATCH (e:Entity)
        WHERE {prop} = $country
        RETURN e.id AS id, e.name AS name, e.type AS type
        ORDER BY e.name
        LIMIT $limit
    """
    with db.get_session() as session:
        result = session.run(query, country=country, limit=limit)
        return [{"id": r["id"], "name": r["name"], "type": r["type"]} for r in result]


@router.get("/{entity_id}", response_model=EntityResponse)
def get_entity(entity_id: str):
    query = """
        MATCH (e:Entity {id: $id})
        RETURN e
    """
    with db.get_session() as session:
        record = session.run(query, id=entity_id).single()
        if not record:
            # The id may belong to a node a merge folded away — follow the
            # forwarding address rather than 404 on a link that used to work.
            # Only ever on a miss: a live id must not be redirected.
            merged_into = resolve_current_id(session, entity_id)
            if merged_into:
                record = session.run(query, id=merged_into).single()
        if not record:
            raise HTTPException(status_code=404, detail="Entity not found")
        # The response carries the survivor's own id, so a caller can see the
        # canonical one and update what it stored.
        return dict(record["e"])


@router.get("/")
def list_entities(skip: int = Query(0, ge=0, le=100_000), limit: int = Query(20, ge=1, le=100)):
    query = """
        MATCH (e:Entity)
        RETURN e
        SKIP $skip LIMIT $limit
    """
    with db.get_session() as session:
        result = session.run(query, skip=skip, limit=limit)
        return [dict(record["e"]) for record in result]


@router.put("/{entity_id}", response_model=EntityResponse)
def update_entity(entity_id: str, entity: EntityCreate, _: dict = Depends(require_contributor)):
    query = """
        MATCH (e:Entity {id: $id})
        SET e += {
            name: $name,
            type: $type,
            country: $country,
            founded: $founded,
            revenue: $revenue,
            description: $description
        }
        RETURN e
    """
    with db.get_session() as session:
        result = session.run(query, id=entity_id, **entity.model_dump())
        record = result.single()
        if not record:
            raise HTTPException(status_code=404, detail="Entity not found")
        return dict(record["e"])


@router.delete("/{entity_id}")
def delete_entity(entity_id: str, _: dict = Depends(require_contributor)):
    query = """
        MATCH (e:Entity {id: $id})
        DETACH DELETE e
    """
    with db.get_session() as session:
        session.run(query, id=entity_id)
        return {"message": "Entity deleted"}


# ── Keep-separate and the merge log ───────────────────────────────────────────
#
# The entity twin of the person endpoints in routers/persons.py. Entities had
# neither, which was backwards: entity merges are the riskier of the two — they
# run automatically during scraping and destroyed the loser's data until #205 —
# yet a moderator could not say "these two are different companies", so a
# rejected candidate came back on every scan, and nothing recorded what merged.

@router.post("/keep-separate")
def keep_separate(data: KeepSeparateRequest, _: dict = Depends(require_contributor)):
    """Mark a group of entities as confirmed DIFFERENT companies.

    A NOT_DUPLICATE edge is stored between every pair, and the dedup scan checks
    them **per pair** rather than per group: a third same-named company must not
    drag a node someone explicitly separated into a destructive auto-merge.
    Reversible via DELETE /entities/keep-separate.
    """
    ids = sorted(set(data.ids))
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="Need at least two distinct ids")
    at = datetime.now(timezone.utc).isoformat()
    with db.get_session() as session:
        for a, b in combinations(ids, 2):
            session.run(
                "MATCH (a:Entity {id:$a}), (b:Entity {id:$b}) "
                "MERGE (a)-[r:NOT_DUPLICATE]->(b) SET r.at = $at",
                a=a, b=b, at=at)
    return {"message": "Marked as separate", "ids": ids}


@router.delete("/keep-separate")
def undo_keep_separate(data: KeepSeparateRequest, _: dict = Depends(require_contributor)):
    """Undo a keep-separate: the pair(s) can be suggested as duplicates again."""
    ids = sorted(set(data.ids))
    with db.get_session() as session:
        for a, b in combinations(ids, 2):
            session.run(
                "MATCH (a:Entity {id:$a})-[r:NOT_DUPLICATE]-(b:Entity {id:$b}) DELETE r",
                a=a, b=b)
    return {"message": "Keep-separate removed", "ids": ids}


@router.get("/kept-separate")
def list_kept_separate(_: dict = Depends(require_contributor)):
    """The pairs a human has confirmed are different companies."""
    with db.get_session() as session:
        pairs = [
            {"a_id": r.get("a_id"), "a_name": r.get("a_name"),
             "b_id": r.get("b_id"), "b_name": r.get("b_name"), "at": r.get("at")}
            for r in session.run(
                "MATCH (a:Entity)-[r:NOT_DUPLICATE]->(b:Entity) "
                "RETURN a.id AS a_id, a.name AS a_name, "
                "       b.id AS b_id, b.name AS b_name, r.at AS at")
        ]
    pairs.sort(key=lambda p: p["at"] or "", reverse=True)
    return {"count": len(pairs), "pairs": pairs}


@router.get("/merge-log")
def merge_log(limit: int = Query(200, ge=1, le=1000), _: dict = Depends(require_contributor)):
    """Recent entity merges, most recent first.

    Filtered on kind so this and the person log never show each other's entries.
    """
    with db.get_session() as session:
        entries = [
            {"id": r.get("id"), "keep_id": r.get("keep_id"), "keep_name": r.get("keep_name"),
             "dup_id": r.get("dup_id"), "dup_name": r.get("dup_name"),
             "at": r.get("at"), "count": r.get("count")}
            for r in session.run(
                "MATCH (ml:MergeLog) WHERE ml.kind = 'entity' "
                "RETURN ml.id AS id, ml.keep_id AS keep_id, ml.keep_name AS keep_name, "
                "       ml.dup_id AS dup_id, ml.dup_name AS dup_name, "
                "       ml.at AS at, ml.count AS count")
        ]
    entries.sort(key=lambda e: e["at"] or "", reverse=True)
    return {"count": len(entries), "entries": entries[:limit]}
