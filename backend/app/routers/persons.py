from fastapi import APIRouter, HTTPException, Depends
from app.models.person import PersonCreate, PersonResponse
from app.auth.dependencies import require_contributor
from app.database import db
import uuid

router = APIRouter(prefix="/persons", tags=["Persons"])


@router.post("/", response_model=PersonResponse)
def create_person(person: PersonCreate, _: dict = Depends(require_contributor)):
    person_id = str(uuid.uuid4())
    full_name = f"{person.first_name} {person.last_name}"

    query = """
        CREATE (p:Person {
            id: $id,
            first_name: $first_name,
            last_name: $last_name,
            full_name: $full_name,
            alias: $alias,
            nationality: $nationality,
            nationalities: $nationalities,
            birth_date: $birth_date,
            death_date: $death_date,
            description: $description,
            wikipedia_url: $wikipedia_url,
            verified: false
        })
        RETURN p
    """

    with db.get_session() as session:
        result = session.run(query,
            id=person_id,
            full_name=full_name,
            **person.model_dump()
        )
        record = result.single()
        if not record:
            raise HTTPException(status_code=500, detail="Failed to create person")
        return dict(record["p"])


@router.get("/{person_id}", response_model=PersonResponse)
def get_person(person_id: str):
    query = """
        MATCH (p:Person {id: $id})
        RETURN p
    """
    with db.get_session() as session:
        result = session.run(query, id=person_id)
        record = result.single()
        if not record:
            raise HTTPException(status_code=404, detail="Person not found")
        return dict(record["p"])


@router.get("/")
def list_persons(skip: int = 0, limit: int = 20):
    query = """
        MATCH (p:Person)
        RETURN p
        SKIP $skip LIMIT $limit
    """
    with db.get_session() as session:
        result = session.run(query, skip=skip, limit=limit)
        return [dict(record["p"]) for record in result]


@router.put("/{person_id}", response_model=PersonResponse)
def update_person(person_id: str, person: PersonCreate, _: dict = Depends(require_contributor)):
    full_name = f"{person.first_name} {person.last_name}"
    query = """
        MATCH (p:Person {id: $id})
        SET p += {
            first_name: $first_name,
            last_name: $last_name,
            full_name: $full_name,
            alias: $alias,
            nationality: $nationality,
            nationalities: $nationalities,
            birth_date: $birth_date,
            death_date: $death_date,
            description: $description,
            wikipedia_url: $wikipedia_url
        }
        RETURN p
    """
    with db.get_session() as session:
        result = session.run(query,
            id=person_id,
            full_name=full_name,
            **person.model_dump()
        )
        record = result.single()
        if not record:
            raise HTTPException(status_code=404, detail="Person not found")
        return dict(record["p"])


@router.delete("/{person_id}")
def delete_person(person_id: str, _: dict = Depends(require_contributor)):
    query = """
        MATCH (p:Person {id: $id})
        DETACH DELETE p
    """
    with db.get_session() as session:
        session.run(query, id=person_id)
        return {"message": "Person deleted"}
