"""Undo for a person merge: the picture a merge leaves behind, and the split
that reads it (findings item 123).

A merge is lossy by design — the duplicate's seats and holdings are folded into
the survivor's, blank fields are filled, the claims re-keyed, the node deleted —
and until this module the log kept two ids and two names. A merge that turns
out wrong (two namesakes born in one month; found out years later, perhaps by
the person) could then only be repaired by hand. So every person merge now
stores three pictures in a `MergeSnapshot` row: the kept person BEFORE, the
duplicate BEFORE, and the survivor right AFTER.

`split` puts the two back with a three-way rule, so that what was written
since the merge is not thrown away:

- what the merge CREATED on the survivor (an edge or a claim that is in AFTER
  and not in BEFORE) goes back to the duplicate, in the state it has now;
- what the merge CHANGED on the survivor (a field it filled, a seat it folded
  an earlier start into) gets its BEFORE value back — only where it still has
  the value the merge gave it;
- what was written SINCE (not in AFTER) stays with the survivor and is named in
  the report: nothing says whose it is.

Edges are followed by their record id (`@rid`), which a rename of the node and a
property update both leave alone; claims by what they assert (kind, direction,
other end, source, role or filing family), since their key hashes the ids.

The pictures are kept for good (user, 2026-11-16): a wrong merge must stay
repairable whenever it is found. They hold nothing the merged person does not
hold anyway.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

TYPE = "MergeSnapshot"
#: the edge types a person carries; anything else on a Person is not a merge's business
EDGE_TYPES = ("OWNS", "HAS_ROLE", "RELATED_TO", "MEMBER_OF", "NOT_DUPLICATE")
_NODE_TYPES = ("Person", "Entity", "VotingGroup")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RID = re.compile(r"^#\d+:\d+$")
#: what tells one edge between the same two nodes from another
_EDGE_KEY = ("role", "since", "until", "source_id", "filing_type", "relation_type")


def _sql(command: str, params: dict | None = None) -> list[dict]:
    from app.db.arcadedb import run_sql
    return run_sql(command, params or {}) or []


def _props(row: dict) -> dict:
    """A record's own properties: no `@rid`/`@type`, and nothing that is null."""
    return {k: v for k, v in row.items() if not k.startswith("@") and v is not None}


def _safe(name: str) -> str:
    if not _NAME.match(name):
        raise ValueError(f"not a property name: {name!r}")
    return name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── the picture ──────────────────────────────────────────────────────────────

def picture(person_id: str) -> dict | None:
    """Everything a merge can touch on one person, read through SQL (Cypher
    coerces a datetime-looking string): the node, its edges with the node at
    the other end, its claims and stake rows, the stored references to it and
    the ids already forwarded to it. None when there is no such person."""
    from app.merged_ids import REFERENCE_COLUMNS

    rows = _sql("SELECT FROM Person WHERE id = :id", {"id": person_id})
    if not rows:
        return None
    node = rows[0]
    rid = node["@rid"]
    raw = [e for e in _sql("SELECT expand(bothE()) FROM Person WHERE id = :id", {"id": person_id})
           if e.get("@type") in EDGE_TYPES and e.get("@in") != e.get("@out")]
    others = sorted({e["@in"] if e["@out"] == rid else e["@out"] for e in raw})
    ends: dict[str, tuple] = {}
    if others:
        for label in _NODE_TYPES:
            for r in _sql(f"SELECT @rid AS rid, id FROM {label} WHERE @rid IN :rids", {"rids": others}):
                ends[str(r["rid"])] = (r["id"], label)
    edges = []
    for e in raw:
        out = e["@out"] == rid
        other = ends.get(e["@in"] if out else e["@out"])
        if not other:
            continue                                   # an end outside the three node types
        edges.append({"rid": e["@rid"], "type": e["@type"], "out": out,
                      "other": other[0], "other_type": other[1], "props": _props(e)})
    refs = [[table, col, str(r["rid"])]
            for table, cols in REFERENCE_COLUMNS for col in cols
            for r in _sql(f"SELECT @rid AS rid FROM {table} WHERE {col} = :id", {"id": person_id})]
    return {
        "id": person_id, "rid": rid, "node": _props(node), "edges": edges,
        "claims": [_props(c) for c in _sql("SELECT FROM Claim WHERE from_id = :id OR to_id = :id",
                                           {"id": person_id})],
        "stakes": [_props(s) for s in _sql("SELECT FROM StakeHistory WHERE from_id = :id OR to_id = :id",
                                           {"id": person_id})],
        "refs": refs,
        "forwards": [r["old_id"] for r in _sql("SELECT old_id FROM MergedId WHERE new_id = :id",
                                               {"id": person_id})],
    }


def record(snapshot_id: str, keep: dict, dup: dict, survivor_id: str, reason: str | None) -> bool:
    """Store the merge's three pictures. Best-effort, like the forwarding row:
    the nodes are merged by now, so a failure here must not fail the merge — it
    leaves a merge that cannot be undone, and says so in the log."""
    try:
        after = picture(survivor_id)
        if after is None:
            raise ValueError(f"survivor {survivor_id} not found")
        _sql(f"INSERT INTO {TYPE} SET id = :id, kind = 'person', keep_id = :keep, dup_id = :dup, "
             "survivor_id = :survivor, keep_name = :keep_name, dup_name = :dup_name, at = :at, "
             "reason = :reason, payload = :payload",
             {"id": snapshot_id, "keep": keep["id"], "dup": dup["id"], "survivor": survivor_id,
              "keep_name": keep["node"].get("full_name"), "dup_name": dup["node"].get("full_name"),
              "at": _now(), "reason": reason,
              "payload": json.dumps({"keep": keep, "dup": dup, "after": after}, default=str)})
        return True
    except Exception as exc:  # noqa: BLE001 — the merge itself already happened
        log.error("merge %s <- %s has NO undo picture: %s", keep.get("id"), dup.get("id"), exc)
        return False


def snapshots(limit: int = 200, open_only: bool = False) -> list[dict]:
    """The stored merges, newest first, without their pictures."""
    rows = _sql(f"SELECT id, kind, keep_id, dup_id, survivor_id, keep_name, dup_name, at, reason, "
                f"undone_at FROM {TYPE}")
    rows = [_props(r) for r in rows if not (open_only and r.get("undone_at"))]
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return rows[:limit]


def forget(person_id: str) -> int:
    """Delete every picture that shows this person — a person deleted from the
    graph is not kept in the undo's store (an erasure must reach it), and a
    merge one of whose two is gone cannot be split anyway."""
    rows = _sql(f"SELECT id FROM {TYPE} WHERE keep_id = :id OR dup_id = :id OR survivor_id = :id",
                {"id": person_id})
    for r in rows:
        _sql(f"DELETE FROM {TYPE} WHERE id = :id", {"id": r["id"]})
        _sql("UPDATE MergeLog REMOVE snapshot_id WHERE snapshot_id = :id", {"id": r["id"]})
    return len(rows)


# ── the split ────────────────────────────────────────────────────────────────

def _fwd(node_id: str) -> str:
    from app.merged_ids import canonical_id
    return canonical_id(node_id) or node_id


def _edge_key(e: dict, other: str | None = None) -> tuple:
    return (e["type"], e["out"], other or e["other"], *(e["props"].get(k) for k in _EDGE_KEY))


def _restore(current: dict, before: dict, after: dict, skip: tuple = ()) -> tuple[dict, list]:
    """The three-way rule for one record: the properties to set and to remove so
    that what the merge changed (after ≠ before) reads as before — where it
    still reads as the merge left it."""
    sets, removes = {}, []
    for name in set(before) | set(after):
        if name in skip or after.get(name) == before.get(name) or current.get(name) != after.get(name):
            continue
        if before.get(name) is None:
            removes.append(name)
        else:
            sets[name] = before[name]
    return sets, removes


def _write(table: str, where: str, params: dict, sets: dict, removes: list) -> None:
    if sets:
        _sql(f"UPDATE {table} SET " + ", ".join(f"{_safe(k)} = :set_{k}" for k in sets) + f" WHERE {where}",
             {**params, **{f"set_{k}": v for k, v in sets.items()}})
    for name in removes:
        _sql(f"UPDATE {table} REMOVE {_safe(name)} WHERE {where}", params)


def _insert(table: str, props: dict) -> None:
    _sql(f"INSERT INTO {table} SET " + ", ".join(f"{_safe(k)} = :{k}" for k in props), props)


def _create_edge(person_id: str, e: dict, other: str) -> bool:
    """Draw the edge `e` between a person and the node at its other end."""
    from app.db.arcadedb import run_command
    if e["type"] not in EDGE_TYPES or e["other_type"] not in _NODE_TYPES:
        return False
    if not _sql(f"SELECT id FROM {e['other_type']} WHERE id = :id", {"id": other}):
        return False                                           # the other end is gone
    props = {k: v for k, v in e["props"].items() if v is not None}
    body = ("{" + ", ".join(f"{_safe(k)}: $p_{k}" for k in props) + "}") if props else ""
    a, b = ("(p)", "(x)") if e["out"] else ("(x)", "(p)")
    run_command(f"MATCH (p:Person {{id: $pid}}), (x:{e['other_type']} {{id: $other}}) "
                f"CREATE {a}-[:{e['type']} {body}]->{b}",
                {"pid": person_id, "other": other, **{f"p_{k}": v for k, v in props.items()}})
    return True


def _edge_label(e: dict) -> str:
    arrow = "->" if e["out"] else "<-"
    return f"{e['type']} {arrow} {e['other']}" + (f" ({e['props']['role']})" if e["props"].get("role") else "")


def _claim_ident(c: dict, self_id: str) -> tuple:
    """What a claim asserts, whatever id its person has at the moment."""
    from app.claims import _key_discriminator
    out = c.get("from_id") == self_id
    other = c.get("to_id") if out else c.get("from_id")
    return (c.get("kind"), out, _fwd(other or ""), c.get("source_id"),
            _key_discriminator(c.get("kind"), c.get("role"), c.get("filing_type")))


def _locate_survivor(snap: dict, after: dict) -> dict:
    """The survivor's picture as it is now. It may have been renamed since (a
    hard id learned later: the same record under another id), or be a person an
    undo of a LATER merge wrote again (the same id, a new record) — but while
    it is merged into somebody else, that merge is undone first."""
    survivor_id = snap["survivor_id"]
    current = picture(survivor_id)
    if current is not None:
        return current
    onward = [r["id"] for r in _sql(f"SELECT id, undone_at FROM {TYPE} WHERE dup_id = :id", {"id": survivor_id})
              if not r.get("undone_at")]
    if onward:
        raise ValueError(f"{survivor_id} was merged into another person since — undo that merge first ({onward[0]})")
    current = picture(_fwd(survivor_id))
    if current is None:
        raise ValueError(f"the merged person {survivor_id} no longer exists")
    if current["rid"] != after["rid"]:
        raise ValueError(f"{survivor_id} was merged into {current['id']} since, and that merge left no picture")
    return current


def split(snapshot_id: str) -> dict:
    """Undo one person merge. Returns what was done and what was left alone;
    raises ValueError when the merge is unknown, already undone, or cannot be
    undone as things stand."""
    from app.claims import migrate_claims, rekey_claim
    from app.db.arcadedb import run_command
    from app.merged_ids import REFERENCE_COLUMNS, invalidate_forwarding_cache, repoint_references

    rows = _sql(f"SELECT FROM {TYPE} WHERE id = :id", {"id": snapshot_id})
    if not rows:
        raise ValueError("no such merge")
    snap = rows[0]
    if snap.get("undone_at"):
        raise ValueError("this merge was already undone")
    pictures = json.loads(snap["payload"])
    before, dup, after = pictures["keep"], pictures["dup"], pictures["after"]
    keep_id, dup_id = snap["keep_id"], snap["dup_id"]
    invalidate_forwarding_cache()
    current = _locate_survivor(snap, after)
    survivor = current["id"]
    if survivor != dup_id and _sql("SELECT id FROM Person WHERE id = :id", {"id": dup_id}):
        raise ValueError(f"a person {dup_id} exists again")
    report = {"snapshot_id": snapshot_id, "fields_restored": [], "edges_moved": 0, "edges_restored": 0,
              "edges_recreated": 0, "claims_moved": 0, "claims_recreated": 0, "stakes_moved": 0,
              "references_moved": 0, "written_since": [], "not_restored": []}

    # 1. the survivor's own fields: what the merge filled reads as before
    sets, removes = _restore(current["node"], before["node"], after["node"], skip=("id",))
    _write("Person", "id = :id", {"id": survivor}, sets, removes)
    report["fields_restored"] = sorted([*sets, *removes])

    # 2. the id the merge gave the survivor goes back with the hard id it came from
    if snap["survivor_id"] != keep_id and survivor == snap["survivor_id"]:
        if _sql("SELECT id FROM Person WHERE id = :id", {"id": keep_id}):
            raise ValueError(f"a person {keep_id} exists again")
        _sql("UPDATE Person SET id = :new WHERE id = :old", {"new": keep_id, "old": survivor})
        _sql("DELETE FROM MergedId WHERE old_id = :id", {"id": keep_id})
        _sql("UPDATE MergedId SET new_id = :new WHERE new_id = :old", {"new": keep_id, "old": survivor})
        migrate_claims(survivor, keep_id)
        repoint_references(survivor, keep_id)
        survivor = keep_id

    # 3. the duplicate is a person again, under its own id
    _sql("DELETE FROM MergedId WHERE old_id = :id", {"id": dup_id})
    invalidate_forwarding_cache()
    _insert("Person", dup["node"])

    # 4. edges: by record id — or, on a person an undo wrote again (every
    # record id new), by what the edge says
    # (an undone snapshot that had this person as its duplicate, later than
    # this merge — a record id itself proves nothing, the store reuses them)
    same_record = not any(r.get("undone_at") and str(r.get("at") or "") > str(snap.get("at") or "")
                          for r in _sql(f"SELECT at, undone_at FROM {TYPE} WHERE dup_id = :id",
                                        {"id": snap["survivor_id"]}))
    was = {e["rid"]: e for e in before["edges"]}
    pool = list(current["edges"])

    def live(made: dict) -> dict | None:
        """The survivor's edge that `made` (an edge of the AFTER picture) is now."""
        if same_record:
            hit = next((c for c in pool if c["rid"] == made["rid"] and c["type"] == made["type"]
                        and c["out"] == made["out"]), None)
        else:
            key = _edge_key(made, _fwd(made["other"]))
            hit = next((c for c in pool if _edge_key(c, _fwd(c["other"])) == key), None)
        if hit:
            pool.remove(hit)
        return hit

    unpaired = list(dup["edges"])
    for made in after["edges"]:
        now = live(made)
        kept = was.get(made["rid"])
        if kept is None:
            # created by the merge: the duplicate's edge, carried over as it was
            key = _edge_key(made, _fwd(made["other"]))
            twin = next((d for d in unpaired if _edge_key(d, _fwd(d["other"])) == key), None)
            if now and _RID.match(now["rid"]) and _create_edge(dup_id, now, now["other"]):
                _sql(f"DELETE FROM {now['type']} WHERE @rid = :rid", {"rid": now["rid"]})
                report["edges_moved"] += 1
                if twin:
                    unpaired.remove(twin)
            elif twin and same_record:
                unpaired.remove(twin)              # deleted since the merge: not drawn again
        elif now and _RID.match(now["rid"]):
            sets, removes = _restore(now["props"], kept["props"], made["props"])
            if sets or removes:
                _write(now["type"], "@rid = :rid", {"rid": now["rid"]}, sets, removes)
                report["edges_restored"] += 1
    for d in unpaired:                         # folded into a survivor's edge, or not carried
        if _create_edge(dup_id, d, _fwd(d["other"])):
            report["edges_recreated"] += 1
        else:
            report["not_restored"].append(_edge_label(d))
    report["written_since"] += [_edge_label(e) for e in pool]

    # 5. claims, by what they assert
    kept_claims = {_claim_ident(c, keep_id): c for c in before["claims"]}
    dup_claims = {_claim_ident(c, dup_id): c for c in dup["claims"]}
    made_claims = {_claim_ident(c, snap["survivor_id"]): c for c in after["claims"]}
    seen = set()
    for c in _sql("SELECT FROM Claim WHERE from_id = :id OR to_id = :id", {"id": survivor}):
        ident = _claim_ident(c, survivor)
        seen.add(ident)
        end = "from_id" if c.get("from_id") == survivor else "to_id"
        if ident not in made_claims:
            report["written_since"].append(f"claim {c.get('kind')} {c.get('source_id')} "
                                           f"{c.get('from_id')} -> {c.get('to_id')}")
        elif ident not in kept_claims:
            rekey_claim(c, **{end: dup_id})                    # the duplicate's, as it is now
            report["claims_moved"] += 1
        elif ident in dup_claims:
            # one assertion both made: folded into one row — two again
            sets, removes = _restore(_props(c), kept_claims[ident], made_claims[ident],
                                     skip=("claim_key", "from_id", "to_id"))
            _write("Claim", "claim_key = :key", {"key": c["claim_key"]}, sets, removes)
            _insert("Claim", dup_claims[ident])
            report["claims_recreated"] += 1
    for ident, c in dup_claims.items():
        if ident not in made_claims and ident not in seen:     # dropped by the merge (a self-loop)
            _insert("Claim", c)
            report["claims_recreated"] += 1

    # 6. the stake statements the duplicate's claims had overwritten
    for row in dup["stakes"]:
        for col in ("from_id", "to_id"):
            if row.get(col) != dup_id:
                continue
            rest = {k: v for k, v in row.items() if k != col}
            where = " AND ".join(f"{_safe(k)} = :w_{k}" for k in rest)
            moved = _sql(f"UPDATE StakeHistory SET {col} = :dup WHERE {col} = :survivor"
                         + (f" AND {where}" if where else ""),
                         {"dup": dup_id, "survivor": survivor, **{f"w_{k}": v for k, v in rest.items()}})
            report["stakes_moved"] += sum(int(r.get("count") or 0) for r in moved if isinstance(r, dict))

    # 7. flags, pins, suppressions and run-log rows that named the duplicate
    allowed = {(table, col) for table, cols in REFERENCE_COLUMNS for col in cols}
    for table, col, rid in dup["refs"]:
        if (table, col) in allowed and _RID.match(rid):
            moved = _sql(f"UPDATE {table} SET {col} = :dup WHERE @rid = :rid AND {col} = :survivor",
                         {"dup": dup_id, "rid": rid, "survivor": survivor})
            report["references_moved"] += sum(int(r.get("count") or 0) for r in moved if isinstance(r, dict))

    # 8. ids that forwarded to the duplicate before do so again
    if dup["forwards"]:
        _sql("UPDATE MergedId SET new_id = :dup WHERE new_id = :survivor AND old_id IN :ids",
             {"dup": dup_id, "survivor": survivor, "ids": dup["forwards"]})

    # 9. two people, on the record: the scan leaves the pair alone from now on
    at = _now()
    run_command("MATCH (a:Person {id: $a}), (b:Person {id: $b}) MERGE (a)-[r:NOT_DUPLICATE]->(b) SET r.at = $at",
                {"a": survivor, "b": dup_id, "at": at})
    _sql(f"UPDATE {TYPE} SET undone_at = :at WHERE id = :id", {"at": at, "id": snapshot_id})
    _sql("UPDATE MergeLog SET undone_at = :at WHERE snapshot_id = :id", {"at": at, "id": snapshot_id})
    invalidate_forwarding_cache()
    report.update(kept_id=survivor, restored_id=dup_id)
    log.info("merge %s undone: %s and %s are two persons again", snapshot_id, survivor, dup_id)
    return report
