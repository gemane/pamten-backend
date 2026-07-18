from fastapi import APIRouter, HTTPException, Depends, Query
from app.models.person import PersonCreate, PersonResponse, PersonMergeRequest
from app.auth.dependencies import require_contributor
from app.database import db
from collections import defaultdict
import re
import uuid

router = APIRouter(prefix="/persons", tags=["Persons"])

_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "sir", "prof", "mx", "madam", "hon", "rev"}

# Common given-name ↔ legal-name pairs, so "Bob Smith" links to "Robert Smith".
# Deliberately small; the same-initial + fuzzy check below catches spelling
# variants (Larry/Laurence) that a static map can't enumerate.
_NICKNAMES = {
    "bob": "robert", "bobby": "robert", "rob": "robert", "bill": "william",
    "billy": "william", "will": "william", "dick": "richard", "rick": "richard",
    "rich": "richard", "jim": "james", "jimmy": "james", "joe": "joseph",
    "joey": "joseph", "larry": "lawrence", "tom": "thomas", "tommy": "thomas",
    "tony": "anthony", "mike": "michael", "mickey": "michael", "dave": "david",
    "steve": "stephen", "chris": "christopher", "ed": "edward", "eddie": "edward",
    "ted": "theodore", "fred": "frederick", "gene": "eugene", "hank": "henry",
    "jack": "john", "johnny": "john", "sam": "samuel", "ben": "benjamin",
    "dan": "daniel", "danny": "daniel", "matt": "matthew", "nick": "nicholas",
    "greg": "gregory", "jeff": "jeffrey", "ron": "ronald", "don": "donald",
    "andy": "andrew", "charlie": "charles", "chuck": "charles", "al": "albert",
    "betty": "elizabeth", "liz": "elizabeth", "beth": "elizabeth", "kate": "katherine",
    "katie": "katherine", "peggy": "margaret", "meg": "margaret",
}


def _name_key(full_name: str | None) -> tuple:
    """Order/case/honorific-insensitive token set — 'Page Lawrence' == 'Lawrence Page'."""
    toks = [t for t in re.findall(r"[a-z0-9]+", (full_name or "").lower()) if t not in _HONORIFICS]
    return tuple(sorted(toks))


def _norm_place(place: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (place or "").lower())


def _first_token(name: str | None) -> str:
    m = re.findall(r"[a-z0-9]+", (name or "").lower())
    return m[0] if m else ""


def _surname_key(last_name: str | None, full_name: str | None) -> str:
    """Normalised surname — the parsed last_name if present, else the final
    (honorific-stripped) token of the full name, in name order (not sorted)."""
    if last_name and last_name.strip():
        return _norm_place(last_name)
    toks = [t for t in re.findall(r"[a-z0-9]+", (full_name or "").lower()) if t not in _HONORIFICS]
    return toks[-1] if toks else ""


def _first_compatible(a: str | None, b: str | None) -> bool:
    """
    True if two given names plausibly denote the same person — an exact match, a
    known nickname/legal-name pair (Bob↔Robert), a prefix (Dave↔David), or a
    shared two-letter stem (Larry↔Laurence). Intentionally lenient: it only ever
    fires as a *review* suggestion alongside a shared company and surname.
    """
    a, b = _first_token(a), _first_token(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if _NICKNAMES.get(a, a) == _NICKNAMES.get(b, b):     # Bob ↔ Robert (cross-initial)
        return True
    if a.startswith(b) or b.startswith(a):               # Dave ↔ David, Ed ↔ Edward
        return True
    if len(a) >= 2 and a[:2] == b[:2]:                   # Larry ↔ Laurence, Steve ↔ Stephen
        return True
    return False


@router.get("/duplicates")
def find_duplicate_persons(_: dict = Depends(require_contributor)):
    """List likely-duplicate person groups for review (does NOT merge)."""
    groups = scan_duplicate_groups()
    return {"count": len(groups), "groups": groups}


def scan_duplicate_groups() -> list[dict]:
    """
    Suggest likely-duplicate person nodes for review (does NOT merge). Signals:
      - same name token set, across a person's full name AND every Wikidata
        alias (catches SEC "Last First" order + honorific/spelling, e.g. SEC's
        "Gates William H Iii" vs the "Bill Gates" node's "William H. Gates III"
        alias)
      - same birth date + place (links the same person across different name
        spellings, e.g. "Larry Page" / "Lawrence Page")
      - same surname + a shared company + a compatible given name — catches
        nickname/legal-name variants a static map can't, e.g. SEC's "Laurence
        Fink" vs Wikidata's "Larry Fink", both tied to BlackRock
      - sharing a connected company (corroboration for common names)

    Confidence: high = share birth date+place OR a company on a same-name match;
    medium = distinctive name match (3+ tokens) OR a surname+company name variant;
    low = common 2-token name match with no corroboration.
    Feed a group's members into POST /persons/merge to resolve it.
    """
    with db.get_session() as session:
        persons = [
            {"id": r.get("id"), "full_name": r.get("full_name"), "wikidata_id": r.get("wikidata_id"),
             "birth_date": r.get("birth_date"), "birth_place": r.get("birth_place"),
             "first_name": r.get("first_name"), "last_name": r.get("last_name"),
             "alias": r.get("alias") or []}
            for r in session.run("""
                MATCH (p:Person)
                RETURN p.id AS id, p.full_name AS full_name, p.wikidata_id AS wikidata_id,
                       p.birth_date AS birth_date, p.birth_place AS birth_place,
                       p.first_name AS first_name, p.last_name AS last_name,
                       p.alias AS alias
            """)
        ]

        by_name: dict[tuple, list] = defaultdict(list)
        by_birth: dict[tuple, list] = defaultdict(list)
        by_surname: dict[str, list] = defaultdict(list)
        for p in persons:
            # Index under the full name AND every alias, so a node whose full
            # name is one variant links to one recorded only as another person's
            # alias (SEC "Gates William H Iii" ↔ "Bill Gates" / "William H. Gates III").
            name_keys = {nk for name in [p["full_name"], *p["alias"]] if (nk := _name_key(name))}
            for nk in name_keys:
                by_name[nk].append(p)
            if p["birth_date"] and p["birth_place"]:
                by_birth[(p["birth_date"], _norm_place(p["birth_place"]))].append(p)
            if (surname := _surname_key(p["last_name"], p["full_name"])):
                by_surname[surname].append(p)

        groups: list[dict] = []
        seen: set[frozenset] = set()
        _ent_cache: dict[str, set] = {}

        def _entities(pid: str) -> set:
            if pid not in _ent_cache:
                rec = session.run(
                    "MATCH (x:Person {id:$id})-[]-(e:Entity) RETURN collect(DISTINCT e.id) AS ids",
                    id=pid).single()
                _ent_cache[pid] = set(rec.get("ids") or [])
            return _ent_cache[pid]

        def _emit(members: list, base_reason: str, variant: bool = False, match_key: tuple | None = None):
            ids = frozenset(m["id"] for m in members)
            if len(ids) < 2 or ids in seen:
                return
            ent = [_entities(m["id"]) for m in members]
            shared_entity = any(ent[i] & ent[j] for i in range(len(ent)) for j in range(i + 1, len(ent)))
            # A name-variant guess (different given names) is only worth surfacing
            # when a shared company corroborates it — otherwise it's just two people
            # who happen to share a surname.
            if variant and not shared_entity:
                return
            seen.add(ids)

            # Birth-date signal (place may be missing — BODS/PSC give date only).
            present_dates = [m["birth_date"] for m in members if m["birth_date"]]
            shared_birth   = len(set(present_dates)) == 1 and len(present_dates) >= 2
            conflict_birth = len(set(present_dates)) >= 2
            # judge distinctiveness on the token set that actually matched (an alias
            # like "William H. Gates III" is distinctive even if a full name is not)
            distinctive = len(match_key if match_key is not None else _name_key(members[0]["full_name"])) >= 3

            reasons = [base_reason]
            if shared_entity and "company" not in base_reason:
                reasons.append("share a company")
            if shared_birth and "birth" not in base_reason:
                reasons.append("same birth date")

            # Conflicting birth dates on a same-name group ⇒ almost certainly two
            # different people — flag as likely-distinct, don't suggest a merge.
            likely_distinct = conflict_birth and not (shared_entity or shared_birth)
            if variant:
                # different given names ⇒ needs review even with the shared company,
                # unless a matching birth date settles it
                confidence = "high" if shared_birth else "medium"
            elif shared_entity or shared_birth:
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
        for nk, members in by_name.items():
            _emit(members, "same name/alias (order/spelling/title)", match_key=nk)
        # Surname + shared company + compatible given name — catches nickname and
        # legal-name variants (Larry/Laurence Fink) that no name-token or birth
        # signal links. Only pairs with differing given names reach here; identical
        # names are already handled by the name-token pass above.
        for members in by_surname.values():
            if len(members) < 2:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    if _name_key(a["full_name"]) == _name_key(b["full_name"]):
                        continue
                    if _first_compatible(a["first_name"] or a["full_name"],
                                         b["first_name"] or b["full_name"]):
                        _emit([a, b], "same surname + shared company (name variant)", variant=True)

    rank = {"high": 0, "medium": 1, "low": 2}
    groups.sort(key=lambda g: (rank[g["confidence"]], g["reason"]))
    return groups


def deduplicate_high_confidence(apply: bool = True) -> dict:
    """
    Scan for duplicate persons and auto-merge only HIGH-confidence, non-distinct
    groups — a matching name/alias token set backed by a shared company or birth
    date. Medium/low groups (surname/company name variants, conflicting-birth
    pairs) are returned untouched under `needs_review` for a human to resolve via
    POST /persons/merge. Pass apply=False for a dry run (report only).
    """
    merged: list[dict] = []
    needs_review: list[dict] = []
    for g in scan_duplicate_groups():
        if g["confidence"] != "high" or g.get("likely_distinct"):
            needs_review.append(g)
            continue
        keep = g["suggested_keep_id"]
        keep_name = next((m["full_name"] for m in g["members"] if m["id"] == keep), keep)
        done: list[str] = []
        for m in g["members"]:
            if m["id"] == keep:
                continue
            if apply:
                try:
                    merge_person_records(keep, m["id"])
                except ValueError:
                    continue  # node vanished (already merged in a prior group)
            done.append(m["full_name"])
        if done:
            merged.append({"keep_id": keep, "keep_name": keep_name, "merged": done})
    return {
        "applied": apply,
        "merged": merged,
        "merged_count": sum(len(x["merged"]) for x in merged),
        "needs_review": needs_review,
        "review_count": len(needs_review),
    }


@router.post("/deduplicate")
def deduplicate_persons(
    apply: bool = Query(True, description="Merge high-confidence groups; false = dry-run report only"),
    _: dict = Depends(require_contributor),
):
    """
    Auto-merge high-confidence duplicate persons and report what was merged plus
    the medium/low groups left for manual review. Runs after each scrape when
    SCRAPER_AUTODEDUP_ENABLED is set; also callable directly from the UI.
    """
    return deduplicate_high_confidence(apply=apply)


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
    try:
        merge_person_records(data.keep_id, data.dup_id)
    except ValueError as exc:
        # keep==dup is a bad request; a missing node is a 404
        status = 400 if "differ" in str(exc) else 404
        raise HTTPException(status_code=status, detail=str(exc))
    return {"message": "Persons merged", "keep_id": data.keep_id, "removed_id": data.dup_id}


def merge_person_records(keep: str, dup: str) -> None:
    """
    Fold person `dup` into person `keep` (see merge_persons). Raises ValueError if
    the ids are equal or either node is missing — callers map that to HTTP status.
    """
    if keep == dup:
        raise ValueError("keep_id and dup_id must differ")

    OWNS_PROPS = ["stake_percent", "voting_power_pct", "ownership_type", "since", "until",
                  "value_usd", "source_id", "credibility_score", "source_url", "source_date",
                  "last_scraped_at"]
    ROLE_PROPS = ["role", "since", "until", "source_id", "credibility_score",
                  "source_url", "source_date", "last_scraped_at"]
    BIO_COALESCE = ["wikidata_id", "sec_cik", "birth_date", "death_date", "birth_place", "wikipedia_url"]

    with db.get_session() as session:
        keep_rec = session.run("MATCH (p:Person {id:$id}) RETURN p", id=keep).single()
        if not keep_rec:
            raise ValueError("Person to keep not found")
        keep_node = dict(keep_rec["p"])
        dup_rec = session.run("MATCH (p:Person {id:$id}) RETURN p", id=dup).single()
        if not dup_rec:
            raise ValueError("Duplicate person not found")
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
