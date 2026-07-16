from fastapi import APIRouter, HTTPException, Depends, Query
from app.models.person import PersonCreate, PersonResponse, PersonMergeRequest
from app.auth.dependencies import require_contributor
from app.database import db
from collections import defaultdict
import re
import uuid

router = APIRouter(prefix="/persons", tags=["Persons"])

_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "sir", "prof", "mx", "madam", "hon", "rev"}


def _name_key(full_name: str | None) -> tuple:
    """Order/case/honorific-insensitive token set — 'Page Lawrence' == 'Lawrence Page'."""
    toks = [t for t in re.findall(r"[a-z0-9]+", (full_name or "").lower()) if t not in _HONORIFICS]
    return tuple(sorted(toks))


def _norm_place(place: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (place or "").lower())


@router.get("/duplicates")
def find_duplicate_persons(_: dict = Depends(require_contributor)):
    """
    Suggest likely-duplicate person nodes for review (does NOT merge). Signals:
      - same name token set (catches SEC "Last First" order + honorific/spelling)
      - same birth date + place (links the same person across different name
        spellings, e.g. "Larry Page" / "Lawrence Page")
      - sharing a connected company (corroboration for common names)

    Confidence: high = share birth date+place OR a company; medium = distinctive
    name match (3+ tokens); low = common 2-token name match with no corroboration.
    Feed a group's members into POST /persons/merge to resolve it.
    """
    with db.get_session() as session:
        persons = [
            {"id": r.get("id"), "full_name": r.get("full_name"), "wikidata_id": r.get("wikidata_id"),
             "birth_date": r.get("birth_date"), "birth_place": r.get("birth_place")}
            for r in session.run("""
                MATCH (p:Person)
                RETURN p.id AS id, p.full_name AS full_name, p.wikidata_id AS wikidata_id,
                       p.birth_date AS birth_date, p.birth_place AS birth_place
            """)
        ]

        by_name: dict[tuple, list] = defaultdict(list)
        by_birth: dict[tuple, list] = defaultdict(list)
        for p in persons:
            if (nk := _name_key(p["full_name"])):
                by_name[nk].append(p)
            if p["birth_date"] and p["birth_place"]:
                by_birth[(p["birth_date"], _norm_place(p["birth_place"]))].append(p)

        groups: list[dict] = []
        seen: set[frozenset] = set()

        def _entities(pid: str) -> set:
            rec = session.run(
                "MATCH (x:Person {id:$id})-[]-(e:Entity) RETURN collect(DISTINCT e.id) AS ids",
                id=pid).single()
            return set(rec.get("ids") or [])

        def _emit(members: list, base_reason: str):
            ids = frozenset(m["id"] for m in members)
            if len(ids) < 2 or ids in seen:
                return
            seen.add(ids)
            ent = [_entities(m["id"]) for m in members]
            shared_entity = any(ent[i] & ent[j] for i in range(len(ent)) for j in range(i + 1, len(ent)))

            # Birth-date signal (place may be missing — BODS/PSC give date only).
            present_dates = [m["birth_date"] for m in members if m["birth_date"]]
            shared_birth   = len(set(present_dates)) == 1 and len(present_dates) >= 2
            conflict_birth = len(set(present_dates)) >= 2
            distinctive = len(_name_key(members[0]["full_name"])) >= 3

            reasons = [base_reason]
            if shared_entity:
                reasons.append("share a company")
            if shared_birth and "birth" not in base_reason:
                reasons.append("same birth date")

            # Conflicting birth dates on a same-name group ⇒ almost certainly two
            # different people — flag as likely-distinct, don't suggest a merge.
            likely_distinct = conflict_birth and not (shared_entity or shared_birth)
            if shared_entity or shared_birth:
                confidence = "high"
            elif likely_distinct:
                confidence = "low"
                reasons.append("but DIFFERENT birth dates — likely distinct people")
            else:
                confidence = "medium" if distinctive else "low"

            # keep: prefer a Wikidata node, then the most-connected, then shortest name
            idx = sorted(range(len(members)),
                         key=lambda i: (0 if members[i]["wikidata_id"] else 1, -len(ent[i]),
                                        len(members[i]["full_name"] or "")))
            groups.append({
                "confidence": confidence,
                "likely_distinct": likely_distinct,
                "reason": ", ".join(reasons),
                "suggested_keep_id": members[idx[0]]["id"],
                "members": [{**m, "connected": len(ent[i])} for i, m in enumerate(members)],
            })

        for members in by_birth.values():
            _emit(members, "same birth date + place")
        for members in by_name.values():
            _emit(members, "same name (order/spelling/title)")

    rank = {"high": 0, "medium": 1, "low": 2}
    groups.sort(key=lambda g: (rank[g["confidence"]], g["reason"]))
    return {"count": len(groups), "groups": groups}


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
    BIO_COALESCE = ["wikidata_id", "sec_cik", "birth_date", "death_date", "birth_place", "wikipedia_url"]

    with db.get_session() as session:
        keep_rec = session.run("MATCH (p:Person {id:$id}) RETURN p", id=keep).single()
        if not keep_rec:
            raise HTTPException(status_code=404, detail="Person to keep not found")
        keep_node = dict(keep_rec["p"])
        dup_rec = session.run("MATCH (p:Person {id:$id}) RETURN p", id=dup).single()
        if not dup_rec:
            raise HTTPException(status_code=404, detail="Duplicate person not found")
        dup_node = dict(dup_rec["p"])

        # The duplicate's name is an alias of the kept person (e.g. SEC's
        # "Page Lawrence" becomes an alias of "Larry Page"), so it's still
        # findable. Union: kept aliases + dup's full_name + dup's aliases,
        # dropping the kept person's own name and case-insensitive dupes.
        keep_full = (keep_node.get("full_name") or "").strip()
        alias_union: list[str] = []
        seen_alias = {keep_full.lower()}
        for a in (keep_node.get("alias") or []) + [dup_node.get("full_name")] + (dup_node.get("alias") or []):
            a = (a or "").strip()
            if a and a.lower() not in seen_alias:
                seen_alias.add(a.lower())
                alias_union.append(a)

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
                keep.alias         = $alias,
                keep.nationalities = CASE WHEN size(COALESCE(keep.nationalities, [])) > 0 THEN keep.nationalities ELSE $nationalities END
        """, keep=keep,
             description=dup_node.get("description") or "",
             nationality=dup_node.get("nationality") or "",
             alias=alias_union,
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
            birth_place: $birth_place,
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
            birth_place: $birth_place,
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
