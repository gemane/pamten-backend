"""What a person's page can say about who this is (findings item 124, step 7
and the user's "how sure" step):

- **the entries it was made of** — the merges that folded other records into
  this person, each with how sure it was (`merge_quality`), and the weakest of
  them as the person's own;
- **the others of the name and birth month** — persons the dedupe did NOT
  merge with this one though they carry the same name and were born the same
  month: namesakes, or the same person where nothing links the entries. Under
  a common name the page says so instead of guessing.
"""
from __future__ import annotations

from app import merge_quality

#: entries listed; the count says how many there are
MAX_MERGED = 50
MAX_NAMESAKES = 10
#: persons fetched by the name before the list is cut
MAX_CANDIDATES = 400


def _own_ids(session, person_id: str) -> list[str]:
    """The id, and every id that forwards to it (a merged entry's, an id it
    carried before it learned a better one)."""
    rows = session.run("MATCH (m:MergedId) WHERE m.new_id = $id RETURN m.old_id AS old", id=person_id)
    return [person_id, *sorted({r.get("old") for r in rows if r.get("old")})]


def merged_entries(session, person_id: str) -> list[dict]:
    """The merges this person was made of, newest first: {"name", "at",
    "reason", "basis", "tier"}; undone ones are left out."""
    seen, out = set(), []
    for pid in _own_ids(session, person_id):
        for r in session.run(
                "MATCH (ml:MergeLog) WHERE ml.keep_id = $id AND (ml.kind IS NULL OR ml.kind = 'person') "
                "RETURN ml.id AS id, ml.dup_name AS name, ml.at AS at, ml.reason AS reason, "
                "ml.undone_at AS undone", id=pid):
            if r.get("undone") or r.get("id") in seen:
                continue
            seen.add(r.get("id"))
            out.append({"name": r.get("name"), "at": r.get("at"), "reason": r.get("reason"),
                        **merge_quality.of(r.get("reason"))})
    out.sort(key=lambda e: e["at"] or "", reverse=True)
    return out


def namesakes(session, person: dict, guard) -> dict | None:
    """The other persons of this name and birth month: {"count", "entries":
    [{"id", "full_name", "birth_date", "companies"}]} — None when the person
    states no birth month (a name alone is shared by too many to list)."""
    from app.routers.persons import (_PERSON_RETURN, _birth_parts, _name_key, _name_lookup_variants,
                                     _person_row, births_compare)
    birth = person.get("birth_date")
    if len(_birth_parts(birth)) < 2:
        return None
    keys = {k for name in [person.get("full_name"), *(person.get("aliases") or [])] if (k := _name_key(name))}
    names = {n for n in [person.get("full_name"), *(person.get("aliases") or [])] if n}
    names |= {v for n in list(names) for v in _name_lookup_variants(n)}
    found: dict[str, dict] = {}
    for name in names:
        for clause in ("p.full_name = $n", "$n IN p.aliases"):
            for r in session.run(f"MATCH (p:Person) WHERE {clause} {_PERSON_RETURN} LIMIT {MAX_CANDIDATES}", n=name):
                found.setdefault(r.get("id"), _person_row(r))
    same = [p for pid, p in found.items()
            if pid != person.get("id") and not guard.node(pid)
            and keys & {k for name in [p["full_name"], *p["aliases"]] if (k := _name_key(name))}
            and births_compare(birth, p["birth_date"]) == "same"]
    same.sort(key=lambda p: (p["full_name"] or "", p["id"]))
    entries = []
    for p in same[:MAX_NAMESAKES]:
        companies = [r.get("name") for r in session.run(
            "MATCH (x:Person {id: $id})-[]-(e:Entity) RETURN DISTINCT e.name AS name, e.id AS id LIMIT 4", id=p["id"])
            if r.get("name") and not guard.node(r.get("id"))]
        entries.append({"id": p["id"], "full_name": p["full_name"], "birth_date": p["birth_date"],
                        "companies": companies[:3]})
    return {"count": len(same), "entries": entries}


def identity(session, person: dict, guard) -> dict:
    """{"merged": {"count", "tier", "entries"}, "namesakes": {…} | None}."""
    merged = merged_entries(session, person.get("id"))
    return {"merged": {"count": len(merged), "tier": merge_quality.weakest(e["tier"] for e in merged),
                       "entries": [{k: e[k] for k in ("name", "at", "basis", "tier")} for e in merged[:MAX_MERGED]]},
            "namesakes": namesakes(session, person, guard)}
