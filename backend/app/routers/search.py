from fastapi import APIRouter, Query, HTTPException
from app.database import db

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("/")
def search(q: str = Query(..., min_length=2)):
    # Full text search across Entity and Person nodes
    query = """
        CALL db.index.fulltext.queryNodes(
            'namesIndex', $q
        )
        YIELD node, score
        RETURN node, score, labels(node) as labels
        ORDER BY score DESC
        LIMIT 20
    """

    with db.get_session() as session:
        result = session.run(query, q=q + "*")
        return [
            {
                "node": dict(record["node"]),
                "score": record["score"],
                "type": record["labels"][0]
            }
            for record in result
        ]


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
            "operations": [dict(l) for l in record["operations"] if l],
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
