from fastapi import APIRouter, HTTPException
from app.models.location import LocationCreate, LocationResponse
from app.database import db
import uuid

router = APIRouter(prefix="/locations", tags=["Locations"])


@router.post("/", response_model=LocationResponse)
def create_location(location: LocationCreate):
    location_id = str(uuid.uuid4())

    query = """
        CREATE (l:Location {
            id: $id,
            street: $street,
            city: $city,
            state: $state,
            zip: $zip,
            country: $country,
            country_full: $country_full,
            region: $region,
            latitude: $latitude,
            longitude: $longitude,
            verified: false
        })
        RETURN l
    """

    with db.get_session() as session:
        result = session.run(query,
            id=location_id,
            **location.model_dump()
        )
        record = result.single()
        if not record:
            raise HTTPException(status_code=500, detail="Failed to create location")
        return {**dict(record["l"]), "id": location_id}


@router.get("/{location_id}", response_model=LocationResponse)
def get_location(location_id: str):
    query = """
        MATCH (l:Location {id: $id})
        RETURN l
    """
    with db.get_session() as session:
        result = session.run(query, id=location_id)
        record = result.single()
        if not record:
            raise HTTPException(status_code=404, detail="Location not found")
        return dict(record["l"])


@router.post("/{entity_id}/headquartered-in/{location_id}")
def set_headquarters(entity_id: str, location_id: str):
    query = """
        MATCH (e:Entity {id: $entity_id})
        MATCH (l:Location {id: $location_id})
        MERGE (e)-[:HEADQUARTERED_IN]->(l)
        RETURN e, l
    """
    with db.get_session() as session:
        result = session.run(query,
            entity_id=entity_id,
            location_id=location_id
        )
        if not result.single():
            raise HTTPException(status_code=404, detail="Entity or Location not found")
        return {"message": "Headquarters set successfully"}


@router.post("/{entity_id}/registered-in/{location_id}")
def set_registered_in(entity_id: str, location_id: str):
    query = """
        MATCH (e:Entity {id: $entity_id})
        MATCH (l:Location {id: $location_id})
        MERGE (e)-[:REGISTERED_IN]->(l)
        RETURN e, l
    """
    with db.get_session() as session:
        result = session.run(query,
            entity_id=entity_id,
            location_id=location_id
        )
        if not result.single():
            raise HTTPException(status_code=404, detail="Entity or Location not found")
        return {"message": "Registration location set successfully"}


@router.post("/{entity_id}/operates-in/{location_id}")
def set_operates_in(entity_id: str, location_id: str):
    query = """
        MATCH (e:Entity {id: $entity_id})
        MATCH (l:Location {id: $location_id})
        MERGE (e)-[:OPERATES_IN]->(l)
        RETURN e, l
    """
    with db.get_session() as session:
        result = session.run(query,
            entity_id=entity_id,
            location_id=location_id
        )
        if not result.single():
            raise HTTPException(status_code=404, detail="Entity or Location not found")
        return {"message": "Operations location set successfully"}
