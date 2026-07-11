from fastapi import APIRouter, Query, HTTPException
from app.database import db

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("/")
def search(q: str = Query(..., min_length=2), country: str | None = Query(default=None)):
    q_lower = q.lower()

    # Run Entity and Person queries separately — ArcadeDB UNION + LIMIT is unreliable.
    if country:
        entity_cypher = """
            MATCH (n:Entity)
            WHERE toLower(n.name) CONTAINS $q AND n.country = $country
            RETURN n AS node, 1.0 AS score, 'Entity' AS type
            LIMIT 20
        """
        entity_params: dict = {"q": q_lower, "country": country}
    else:
        entity_cypher = """
            MATCH (n:Entity)
            WHERE toLower(n.name) CONTAINS $q
               OR toLower(coalesce(n.description, '')) CONTAINS $q
               OR any(alias IN coalesce(n.aliases, []) WHERE toLower(alias) CONTAINS $q)
            RETURN n AS node, 1.0 AS score, 'Entity' AS type
            LIMIT 20
        """
        entity_params = {"q": q_lower}

    person_cypher = """
        MATCH (n:Person)
        WHERE toLower(n.full_name) CONTAINS $q
        RETURN n AS node, 1.0 AS score, 'Person' AS type
        LIMIT 10
    """

    results = []
    with db.get_session() as session:
        for record in session.run(entity_cypher, **entity_params):
            results.append({
                "node":  dict(record["node"]),
                "score": record["score"],
                "type":  record["type"],
            })
        if not country:
            for record in session.run(person_cypher, q=q_lower):
                results.append({
                    "node":  dict(record["node"]),
                    "score": record["score"],
                    "type":  record["type"],
                })

    return results[:20]


@router.get("/entity/{entity_id}/full-profile")
def get_full_profile(entity_id: str):
    # Everything about an entity in one call
    query = """
        MATCH (e:Entity {id: $id})
        OPTIONAL MATCH (e)-[:HEADQUARTERED_IN]->(hq:Location)
        OPTIONAL MATCH (e)-[:OPERATES_IN]->(ops:Location)
        OPTIONAL MATCH (owner)-[owns_r:OWNS]->(e) WHERE owns_r.until IS NULL
        OPTIONAL MATCH (e)-[sub_r:OWNS]->(subsidiary) WHERE sub_r.until IS NULL
        OPTIONAL MATCH (p:Person)-[role_r:HAS_ROLE]->(e) WHERE role_r.until IS NULL
        RETURN e,
               hq,
               collect(DISTINCT ops) as operations,
               collect(DISTINCT {owner: owner, rel: owns_r}) as owners,
               collect(DISTINCT {entity: subsidiary, rel: sub_r}) as subsidiaries,
               collect(DISTINCT {person: p, role: role_r}) as executives
    """

    with db.get_session() as session:
        result = session.run(query, id=entity_id)
        record = result.single()
        if not record:
            raise HTTPException(status_code=404, detail="Entity not found")

        return {
            "entity": dict(record["e"]),
            "headquarters": dict(record["hq"]) if record["hq"] else None,
            "operations": [dict(loc) for loc in record["operations"] if loc],
            "owners": [
                {
                    "owner": dict(o["owner"]),
                    "relationship": dict(o["rel"])
                }
                for o in record["owners"] if o["owner"]
            ],
            "subsidiaries": [
                {
                    "entity": dict(s["entity"]),
                    "relationship": dict(s["rel"])
                }
                for s in record["subsidiaries"] if s["entity"]
            ],
            "executives": [
                {
                    "person": dict(ex["person"]),
                    "role": dict(ex["role"])
                }
                for ex in record["executives"] if ex["person"]
            ]
        }


@router.get("/geographic")
def search_by_country(country: str, region: str = None):
    # Find all entities in a country or region
    query = """
        MATCH (e:Entity)-[:HEADQUARTERED_IN]->(l:Location)
        WHERE l.country = $country
        RETURN e, l
        ORDER BY e.name
    """

    with db.get_session() as session:
        result = session.run(query, country=country)
        return [
            {
                "entity": dict(record["e"]),
                "location": dict(record["l"])
            }
            for record in result
        ]
