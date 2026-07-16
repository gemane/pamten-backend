from fastapi import APIRouter, HTTPException, Depends, Query
from app.models.person import PersonCreate, PersonResponse, PersonMergeRequest
from app.auth.dependencies import require_contributor
from app.database import db
import uuid

router = APIRouter(prefix="/persons", tags=["Persons"])


@router.post("/merge")
def merge_persons(data: PersonMergeRequest, _: dict = Depends(require_contributor)):
    """
    Fold a duplicate person into the one to keep, then delete the duplicate.

    Different scrapers spell the same person differently — e.g. Wikidata's
    "Larry Page" vs SEC EDGAR's last-first "Page Lawrence" — and can't be
    auto-matched, so they land as two nodes. This re-homes the duplicate's
    relationships (OWNS / HAS_ROLE / RELATED_TO, with their properties) onto the
    kept person, backfills any blank bio fields, then DETACH DELETEs the dup.
    """
    if data.keep_id == data.dup_id:
        raise HTTPException(status_code=400, detail="keep_id and dup_id must differ")

    params = {"keep": data.keep_id, "dup": data.dup_id}
    with db.get_session() as session:
        if not session.run("MATCH (p:Person {id:$id}) RETURN p.id AS id", id=data.keep_id).single():
            raise HTTPException(status_code=404, detail="Person to keep not found")
        if not session.run("MATCH (p:Person {id:$id}) RETURN p.id AS id", id=data.dup_id).single():
            raise HTTPException(status_code=404, detail="Duplicate person not found")

        # Re-home the duplicate's relationships onto the kept person, preserving
        # each edge's properties. (Display-level de-dup already collapses any
        # resulting same-target duplicates in the profile endpoints.)
        session.run("""
            MATCH (keep:Person {id:$keep}), (dup:Person {id:$dup})-[r:OWNS]->(x)
            CREATE (keep)-[nr:OWNS]->(x) SET nr += properties(r)
        """, **params)
        session.run("""
            MATCH (keep:Person {id:$keep}), (dup:Person {id:$dup})-[r:HAS_ROLE]->(x)
            CREATE (keep)-[nr:HAS_ROLE]->(x) SET nr += properties(r)
        """, **params)
        session.run("""
            MATCH (keep:Person {id:$keep}), (dup:Person {id:$dup})-[r:RELATED_TO]->(x)
            CREATE (keep)-[nr:RELATED_TO]->(x) SET nr += properties(r)
        """, **params)
        session.run("""
            MATCH (keep:Person {id:$keep}), (x)-[r:RELATED_TO]->(dup:Person {id:$dup})
            CREATE (x)-[nr:RELATED_TO]->(keep) SET nr += properties(r)
        """, **params)

        # Backfill blank bio fields on the kept person from the duplicate.
        session.run("""
            MATCH (keep:Person {id:$keep}), (dup:Person {id:$dup})
            SET keep.wikidata_id   = COALESCE(keep.wikidata_id, dup.wikidata_id),
                keep.sec_cik       = COALESCE(keep.sec_cik, dup.sec_cik),
                keep.birth_date    = COALESCE(keep.birth_date, dup.birth_date),
                keep.death_date    = COALESCE(keep.death_date, dup.death_date),
                keep.wikipedia_url = COALESCE(keep.wikipedia_url, dup.wikipedia_url),
                keep.description   = CASE WHEN COALESCE(keep.description, '') = '' THEN dup.description ELSE keep.description END,
                keep.nationality   = CASE WHEN COALESCE(keep.nationality, '') = '' THEN dup.nationality ELSE keep.nationality END,
                keep.alias         = CASE WHEN size(COALESCE(keep.alias, [])) > 0 THEN keep.alias ELSE dup.alias END,
                keep.nationalities = CASE WHEN size(COALESCE(keep.nationalities, [])) > 0 THEN keep.nationalities ELSE dup.nationalities END
        """, **params)

        session.run("MATCH (dup:Person {id:$dup}) DETACH DELETE dup", **params)

    return {"message": "Persons merged", "keep_id": data.keep_id, "removed_id": data.dup_id}


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
def list_persons(skip: int = Query(0, ge=0, le=100_000), limit: int = Query(20, ge=1, le=100)):
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
