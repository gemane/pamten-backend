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

    keep, dup = data.keep_id, data.dup_id
    OWNS_PROPS = ["stake_percent", "voting_power_pct", "ownership_type", "since", "until",
                  "value_usd", "source_id", "credibility_score", "source_url", "source_date",
                  "last_scraped_at"]
    ROLE_PROPS = ["role", "since", "until", "source_id", "credibility_score",
                  "source_url", "source_date", "last_scraped_at"]
    BIO_COALESCE = ["wikidata_id", "sec_cik", "birth_date", "death_date", "wikipedia_url"]

    with db.get_session() as session:
        if not session.run("MATCH (p:Person {id:$id}) RETURN p.id AS id", id=keep).single():
            raise HTTPException(status_code=404, detail="Person to keep not found")
        dup_rec = session.run("MATCH (p:Person {id:$id}) RETURN p", id=dup).single()
        if not dup_rec:
            raise HTTPException(status_code=404, detail="Duplicate person not found")
        dup_node = dict(dup_rec["p"])

        # Read the duplicate's edges into Python, then write them onto the kept
        # person with bound $params. We deliberately avoid Cypher that reads a
        # second edge/node's properties (properties(r), COALESCE(a.x, b.x)) — that
        # silently misbehaves on the production ArcadeDB version. Only proven
        # patterns are used: plain CREATE/MERGE with $params, SET x=$param, and
        # COALESCE(existing, $param).
        def _edges(rel: str, props: list[str], out: bool) -> list[dict]:
            pat = f"(dup:Person {{id:$dup}})-[r:{rel}]->(x)" if out else f"(x)-[r:{rel}]->(dup:Person {{id:$dup}})"
            cols = ", ".join(f"r.{p} AS {p}" for p in props)
            q = f"MATCH {pat} RETURN x.id AS target{',' if cols else ''} {cols}"
            return [{**{p: rec.get(p) for p in props}, "target": rec.get("target")}
                    for rec in session.run(q, dup=dup)]

        # OWNS → fold onto the kept person's existing edge to the same target.
        owns_set = ", ".join(f"nr.{p} = COALESCE(nr.{p}, ${p})" for p in OWNS_PROPS)
        for e in _edges("OWNS", OWNS_PROPS, out=True):
            session.run(
                f"MATCH (keep:Person {{id:$keep}}), (x {{id:$target}}) "
                f"MERGE (keep)-[nr:OWNS]->(x) SET {owns_set}",
                keep=keep, target=e["target"], **{p: e[p] for p in OWNS_PROPS})

        # HAS_ROLE → create (distinct tenures are deduped on display).
        role_set = ", ".join(f"nr.{p} = ${p}" for p in ROLE_PROPS)
        for e in _edges("HAS_ROLE", ROLE_PROPS, out=True):
            session.run(
                f"MATCH (keep:Person {{id:$keep}}), (x {{id:$target}}) "
                f"CREATE (keep)-[nr:HAS_ROLE]->(x) SET {role_set}",
                keep=keep, target=e["target"], **{p: e[p] for p in ROLE_PROPS})

        # RELATED_TO (both directions) → fold onto keep's edge.
        for e in _edges("RELATED_TO", ["relation", "source_id"], out=True):
            session.run(
                "MATCH (keep:Person {id:$keep}), (x {id:$target}) "
                "MERGE (keep)-[nr:RELATED_TO]->(x) "
                "SET nr.relation = COALESCE(nr.relation, $relation), nr.source_id = COALESCE(nr.source_id, $source_id)",
                keep=keep, target=e["target"], relation=e["relation"], source_id=e["source_id"])
        for e in _edges("RELATED_TO", ["relation", "source_id"], out=False):
            session.run(
                "MATCH (keep:Person {id:$keep}), (x {id:$target}) "
                "MERGE (x)-[nr:RELATED_TO]->(keep) "
                "SET nr.relation = COALESCE(nr.relation, $relation), nr.source_id = COALESCE(nr.source_id, $source_id)",
                keep=keep, target=e["target"], relation=e["relation"], source_id=e["source_id"])

        # Backfill blank bio fields on the kept person from the duplicate's values.
        bio_set = ", ".join(f"keep.{p} = COALESCE(keep.{p}, ${p})" for p in BIO_COALESCE)
        session.run(f"""
            MATCH (keep:Person {{id:$keep}})
            SET {bio_set},
                keep.description   = CASE WHEN COALESCE(keep.description, '') = '' THEN $description ELSE keep.description END,
                keep.nationality   = CASE WHEN COALESCE(keep.nationality, '') = '' THEN $nationality ELSE keep.nationality END,
                keep.alias         = CASE WHEN size(COALESCE(keep.alias, [])) > 0 THEN keep.alias ELSE $alias END,
                keep.nationalities = CASE WHEN size(COALESCE(keep.nationalities, [])) > 0 THEN keep.nationalities ELSE $nationalities END
        """, keep=keep,
             description=dup_node.get("description") or "",
             nationality=dup_node.get("nationality") or "",
             alias=dup_node.get("alias") or [],
             nationalities=dup_node.get("nationalities") or [],
             **{p: dup_node.get(p) for p in BIO_COALESCE})

        session.run("MATCH (dup:Person {id:$dup}) DETACH DELETE dup", dup=dup)

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
