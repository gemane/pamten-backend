"""Graph-maintenance services: purge, dedup, and one-time migrations.

Pure orchestration over the DB layer, extracted from the scraper router so
the endpoints stay thin. Behaviour is unchanged from the previous inline
implementations.
"""
import logging
import uuid
from datetime import datetime, timezone

from app.database import db
from app.db.arcadedb import run_query, run_command, run_sql, run_sqlscript
from app.scraper.mapper import derive_ownership_type as _derive_ownership_type
from app.merged_ids import record_merge_sql

log = logging.getLogger(__name__)


class CompanyNotFound(Exception):
    """Raised by purge_company when the named entity does not exist."""


def purge_company(name: str) -> dict:
    """
    Delete a company entity and all its relationships from the graph, then
    remove any nodes that are left with no remaining relationships (orphans).
    Admin only. Useful for cleaning up test scrapes.
    """
    with db.get_session() as session:
        # Check it exists first
        rec = session.run(
            "MATCH (e:Entity {name: $name}) RETURN e.id AS id LIMIT 1",
            name=name,
        ).single()
        if not rec:
            raise CompanyNotFound(f"Company '{name}' not found")

        # Detach-delete the entity and all its relationships
        session.run(
            "MATCH (e:Entity {name: $name}) DETACH DELETE e",
            name=name,
        )

        # Remove orphaned Person and Entity nodes (no remaining relationships)
        orphan_result = session.run(
            """
            MATCH (n)
            WHERE (n:Person OR n:Entity) AND NOT (n)--()
            WITH n, n.name AS orphan_name
            DETACH DELETE n
            RETURN count(*) AS removed, collect(orphan_name) AS names
            """
        ).single()
        orphans_removed = orphan_result["removed"] if orphan_result else 0
        orphan_names    = orphan_result["names"]   if orphan_result else []

    return {
        "status":          "deleted",
        "company":         name,
        "orphans_removed": orphans_removed,
        "orphans":         orphan_names,
    }


_OWNS_PAGE = 20000


def _owns_pairs_with_rids() -> dict[tuple, list[tuple]]:
    """Group active OWNS edges by their (owner, target) vertex pair, returning
    {(out_rid, in_rid): [(edge_rid, stake_percent, direct_or_indirect), ...]}.

    Pages through the edges by @rid ordering and groups in Python, so there's NO
    server-side GROUP BY — a global `GROUP BY a.id, b.id` over the ~700k OWNS
    edges blows the dev DB's query heap (OutOfMemoryError). @out/@in are the
    endpoint vertex rids; @rid identifies the edge for a precise delete.
    """
    pairs: dict[tuple, list[tuple]] = {}
    last: str | None = None
    while True:
        where = "WHERE until IS NULL" + (f" AND @rid > {last}" if last else "")
        rows = run_sql(
            f"SELECT @rid AS rid, @out AS o, @in AS i, stake_percent AS st, "
            f"direct_or_indirect AS doi FROM OWNS {where} ORDER BY @rid LIMIT {_OWNS_PAGE}"
        )
        if not rows:
            break
        for r in rows:
            pairs.setdefault((r["o"], r["i"]), []).append((r["rid"], r.get("st"), r.get("doi")))
        last = rows[-1]["rid"]
        if len(rows) < _OWNS_PAGE:
            break
    return pairs


def count_duplicate_owns_edges() -> dict:
    """Report duplicate active OWNS edges without changing anything — a
    duplicate is a second+ edge between the same (owner, target) pair (e.g. from
    a multi-interest BODS relationship statement). Admin/observability."""
    pairs = _owns_pairs_with_rids()
    dup_pairs = sum(1 for v in pairs.values() if len(v) > 1)
    redundant = sum(len(v) - 1 for v in pairs.values() if len(v) > 1)
    return {
        "active_edges": sum(len(v) for v in pairs.values()),
        "distinct_pairs": len(pairs),
        "duplicate_pairs": dup_pairs,
        "redundant_edges": redundant,
    }


def deduplicate_owns_edges(batch_size: int = 2000) -> dict:
    """
    For every (owner → target) pair with more than one active OWNS edge, keep one
    and delete the rest by @rid. Admin only.

    Survivor priority: largest stake first, then — among edges with an equal/absent
    stake — the one carrying a `direct_or_indirect` marker, and finally a `direct`
    marker over an `indirect` one. The first keeps the GLEIF RR-CDF direct/ultimate
    edge (stakeless but flagged) over a flagless duplicate from the BODS import, so
    auto-running dedup after an RR import can't silently drop the direct/indirect
    signal.

    The last tie-break matters because the two are not interchangeable: keeping the
    `indirect` twin of a pair that also has a `direct` edge labels a direct holding
    as indirect, and the graph then hides it as an ownership shortcut — the company
    disappears despite a perfectly good direct edge. Without this the winner was
    whichever came first in @rid order.

    Deleting by @rid preserves the kept edge's full provenance (unlike a
    delete-all-then-recreate, which drops properties), and the delete is batched
    in one sqlscript per `batch_size` edges so each request stays under the DB
    proxy timeout.
    """
    pairs = _owns_pairs_with_rids()
    to_delete: list[str] = []
    dup_pairs = 0
    for edges in pairs.values():
        if len(edges) < 2:
            continue
        dup_pairs += 1
        # keep the largest stake (None treated as -1); tie-break on having a
        # direct_or_indirect marker, then on that marker being 'direct'; delete the rest
        edges_sorted = sorted(
            edges,
            key=lambda e: (e[1] if e[1] is not None else -1,
                           1 if e[2] else 0,
                           1 if e[2] == "direct" else 0),
            reverse=True,
        )
        to_delete.extend(rid for rid, _, _ in edges_sorted[1:])

    deleted = 0
    for i in range(0, len(to_delete), batch_size):
        chunk = to_delete[i:i + batch_size]
        # `DELETE FROM <rid>` is direct record access; `DELETE FROM OWNS WHERE
        # @rid = <rid>` scans the whole (700k-edge) type per statement instead.
        run_sqlscript(";".join(f"DELETE FROM {rid}" for rid in chunk))
        deleted += len(chunk)

    return {"duplicates_removed": deleted, "pairs_cleaned": dup_pairs}


# ── Cross-source duplicate detection (same company, different identifiers) ─────
#
# The GLEIF/CH importers key each entity on its LEI / Companies House id, so the
# same real-world company recorded under two LEIs
# — e.g. BlackRock, Inc. as both 549300… and 529900… — becomes two Entity nodes.
# The id-based dedup (deduplicate_entities, by shared LEI/CH id) can't see these
# because the ids differ. What they share is a `name_normalized`. This DETECTS
# such groups for review (it does not merge — same normalized name isn't always
# the same company, so a human decides).

# name_normalized is lowercase letters / digits / spaces (see
# mapper.normalize_entity_name).
_NAME_SHARD_CHARSET = "0123456789abcdefghijklmnopqrstuvwxyz "


def _duplicate_name_groups() -> list[tuple[str, int]]:
    """(name_normalized, member_count) for every normalized name shared by >1
    Entity. Server-side GROUP BY sharded by name prefix — split deeper on the
    query-heap cap — so it never loads the whole Entity set or trips
    OutOfMemoryError (a single global GROUP BY over millions of names does)."""
    found: list[tuple[str, int]] = []

    def _collect(prefix: str) -> None:
        # Voting groups are excluded: two blocs over one company normalise to
        # nearly the same name by construction, and they are identified by their
        # rosters (see _upsert_voting_group), never by name.
        q = ("SELECT FROM (SELECT name_normalized AS k, count(*) AS c FROM Entity "
             "WHERE name_normalized >= :lo AND name_normalized < :hi "
             "AND type <> 'voting_group' "
             "GROUP BY name_normalized) WHERE c > 1")
        try:
            rows = run_sql(q, {"lo": prefix, "hi": prefix + "￿"})
        except RuntimeError as exc:
            if not any(m in str(exc) for m in _GROUP_LIMIT_MARKERS):
                raise
            for ch in _NAME_SHARD_CHARSET:
                _collect(prefix + ch)
            return
        found.extend((r["k"], int(r["c"])) for r in rows if r.get("k"))

    _collect("")
    return found


def count_duplicate_entity_names() -> dict:
    """How many same-name duplicate groups exist (observability / post-import):
    {duplicate_name_groups, redundant_nodes}."""
    groups = _duplicate_name_groups()
    return {
        "duplicate_name_groups": len(groups),
        "redundant_nodes": sum(c - 1 for _, c in groups),
    }


_CONFIDENCE_RANK = {"definitive": 0, "high": 1, "medium": 2, "low": 3}


def _group_confidence(members: list[dict]) -> str:
    """How sure are we the same-name members are the SAME company?
      definitive — they share a wikidata_id / sec_cik / companies_house_id
                   (a hard external identifier ⇒ same entity).
      high       — same registered_address (GLEIF registered office).
      medium     — same country AND same founded year.
      low        — name only (differing address/country ⇒ probably different).
    """
    def _shared(field: str) -> bool:
        vals = [m.get(field) for m in members if m.get(field)]
        return len(vals) >= 2 and len(set(vals)) < len(vals)

    if any(_shared(f) for f in ("lei_id", "wikidata_id", "sec_cik", "companies_house_id")):
        return "definitive"
    if _shared("registered_address"):
        return "high"
    countries = {m.get("country") for m in members if m.get("country")}
    founded = {m.get("founded") for m in members if m.get("founded")}
    if len(countries) == 1 and len(founded) == 1 and (countries or founded):
        return "medium"
    return "low"


def find_duplicate_entity_names(limit: int = 100, min_confidence: str | None = None) -> list[dict]:
    """The biggest same-name duplicate groups for review, each tagged with a
    confidence that the members are the SAME company (see _group_confidence), so
    a true duplicate (two LEIs, same registered address / shared hard id) is told
    apart from a coincidental name clash. `min_confidence` filters the list
    (definitive > high > medium > low)."""
    cutoff = _CONFIDENCE_RANK.get(min_confidence, len(_CONFIDENCE_RANK))
    groups = sorted(_duplicate_name_groups(), key=lambda g: -g[1])
    out = []
    for name_norm, cnt in groups:
        members = run_sql(
            "SELECT id, name, country, founded, lei_id, companies_house_id, "
            "sec_cik, wikidata_id, registered_address FROM Entity "
            "WHERE name_normalized = :nn LIMIT 25", {"nn": name_norm})
        members = [{k: v for k, v in m.items() if not k.startswith("@")} for m in members]
        confidence = _group_confidence(members)
        if _CONFIDENCE_RANK[confidence] > cutoff:
            continue
        out.append({
            "name_normalized": name_norm,
            "count": cnt,
            "confidence": confidence,
            "members": members,
        })
        if len(out) >= limit:
            break
    return out


def _migrate_person_edges(dead_id: str, keep_id: str) -> int:
    """Move all OWNS / HAS_ROLE / RELATED_TO edges from dead_id → keep_id.

    This was the recreate block the entity path's "add it to all three blocks"
    docstring did not know about: it named 6 of 25 OWNS properties, and it runs
    automatically after every scrape via auto-dedup — so every person merge
    silently stripped provenance, counts and voting data from the surviving
    edges. Property lists now come from ``edge_schema``, same as the entity
    path, so both carry whatever the schema carries.
    """
    from app.scraper.edge_schema import (OWNS_PROPS, ROLE_PROPS, RELATED_TO_PROPS,
                                         edge_return_clause, edge_create_clause,
                                         edge_params)
    migrated = 0

    for e in run_query(
        f"""MATCH (p:Person {{id: $pid}})-[r:OWNS]->(t:Entity)
            RETURN t.id AS tid, {edge_return_clause('r', OWNS_PROPS)}""",
        {"pid": dead_id},
    ):
        if run_query(
            "MATCH (p:Person {id: $pid})-[r:OWNS]->(t:Entity {id: $tid}) "
            "WHERE r.until IS NULL RETURN r LIMIT 1",
            {"pid": keep_id, "tid": e["tid"]},
        ):
            continue
        run_command(
            f"""MATCH (p:Person {{id: $pid}}), (t:Entity {{id: $tid}})
                CREATE (p)-[:OWNS {{{edge_create_clause(OWNS_PROPS)}}}]->(t)""",
            {"pid": keep_id, "tid": e["tid"], **edge_params(e, OWNS_PROPS)},
        )
        migrated += 1

    for e in run_query(
        f"""MATCH (p:Person {{id: $pid}})-[r:HAS_ROLE]->(t:Entity)
            RETURN t.id AS tid, {edge_return_clause('r', ROLE_PROPS)}""",
        {"pid": dead_id},
    ):
        if run_query(
            "MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(t:Entity {id: $tid}) "
            "WHERE r.role = $role AND r.until IS NULL RETURN r LIMIT 1",
            {"pid": keep_id, "tid": e["tid"], "role": e.get("role")},
        ):
            continue
        run_command(
            f"""MATCH (p:Person {{id: $pid}}), (t:Entity {{id: $tid}})
                CREATE (p)-[:HAS_ROLE {{{edge_create_clause(ROLE_PROPS)}}}]->(t)""",
            {"pid": keep_id, "tid": e["tid"], **edge_params(e, ROLE_PROPS)},
        )
        migrated += 1

    # People are group members too — Lemann, Sicupira and Telles all are — and
    # a person-merge used to sever them from their bloc exactly as the entity
    # path once did.
    for e in run_query(
        f"""MATCH (p:Person {{id: $pid}})-[r:RELATED_TO]->(t:Entity)
            RETURN t.id AS tid, {edge_return_clause('r', RELATED_TO_PROPS)}""",
        {"pid": dead_id},
    ):
        if run_query(
            "MATCH (p:Person {id: $pid})-[r:RELATED_TO]->(t:Entity {id: $tid}) "
            "WHERE r.relation = $relation RETURN r LIMIT 1",
            {"pid": keep_id, "tid": e["tid"], "relation": e.get("relation")},
        ):
            continue
        run_command(
            f"""MATCH (p:Person {{id: $pid}}), (t:Entity {{id: $tid}})
                CREATE (p)-[:RELATED_TO {{{edge_create_clause(RELATED_TO_PROPS)}}}]->(t)""",
            {"pid": keep_id, "tid": e["tid"], **edge_params(e, RELATED_TO_PROPS)},
        )
        migrated += 1

    from app.claims import migrate_claims
    migrate_claims(dead_id, keep_id)

    return migrated

def deduplicate_person_nodes() -> dict:
    """
    Merge Person node pairs whose 2-word names are each other's reversal
    (e.g. 'Brin Sergey' ↔ 'Sergey Brin').  Keeps the richer node
    (prefer wikidata_id, then more edges, then alphabetically first name),
    migrates all edges from the dead node, then deletes it.  Admin only.
    """
    # Fetch all Person nodes with a 2-word full_name
    persons = run_query(
        "MATCH (p:Person) RETURN p.id AS id, p.full_name AS name, p.wikidata_id AS wid"
    )

    # Build a lookup: normalised name → node
    by_name: dict[str, dict] = {}
    for p in persons:
        name = (p.get("name") or "").strip()
        if name:
            by_name[name.lower()] = p

    merged: list[dict] = []
    visited: set[str] = set()

    for p in persons:
        name = (p.get("name") or "").strip()
        parts = name.split()
        if len(parts) != 2:
            continue
        pid = p["id"]
        if pid in visited:
            continue

        reversed_name = f"{parts[1]} {parts[0]}"
        other = by_name.get(reversed_name.lower())
        if not other or other["id"] == pid or other["id"] in visited:
            continue

        # Decide which to keep: prefer wikidata_id, then pick the one with
        # more natural "First Last" order (first word title-cased, second too)
        p_has_wiki   = bool(p.get("wid"))
        oth_has_wiki = bool(other.get("wid"))

        if p_has_wiki and not oth_has_wiki:
            keep, dead = p, other
        elif oth_has_wiki and not p_has_wiki:
            keep, dead = other, p
        else:
            # Both or neither have wikidata — keep the more "natural" name
            # (prefer First Last over Last First: first word should be shorter
            # for EDGAR LAST FIRST format, but simplest heuristic is alphabetical)
            keep, dead = (p, other) if p["name"] < other["name"] else (other, p)

        migrated = _migrate_person_edges(dead["id"], keep["id"])

        # Delete the dead node
        run_command("MATCH (p:Person {id: $pid}) DETACH DELETE p", {"pid": dead["id"]})

        visited.add(pid)
        visited.add(other["id"])
        merged.append({
            "kept":     keep["name"],
            "deleted":  dead["name"],
            "edges_migrated": migrated,
        })

    return {"pairs_merged": len(merged), "detail": merged}


# Properties that must NOT move from the loser to the survivor on a merge.
# Everything else is carried across when the survivor has no value of its own, so
# a field added by a future scraper survives merges without anyone remembering to
# update this module.
_MERGE_SKIP_PROPS = frozenset({
    "id",                # the survivor's identity — the whole point of choosing it
    "name",              # survivor won on name_credibility; its name stands
    "name_normalized",   # derived from name
    "search_text",       # rebuilt below from the merged name + description + aliases
    "name_credibility",  # handled explicitly (max)
    "verified",          # handled explicitly (either)
    "aliases", "countries", "hq_locations",   # lists — unioned, not replaced
})


def _merge_entity_props(keep: dict, dead: dict) -> dict:
    """The properties to write onto ``keep`` when folding ``dead`` into it.

    Fill a gap, never clobber: the survivor was chosen deliberately, so its own
    values win. Without this the merge kept only edges, and the loser's
    identifiers and descriptive fields were simply deleted — merging a GLEIF node
    with its Wikidata twin lost `wikidata_id`, `sec_cik`, the description, the
    revenue and the employee count. Losing `wikidata_id` also un-marked the
    company as notable for search ranking and removed the key a later Wikidata
    scrape resolves on.
    """
    out: dict = {}

    for key, value in dead.items():
        if key.startswith("@") or key in _MERGE_SKIP_PROPS:
            continue
        if value in (None, "", []):
            continue
        current = keep.get(key)
        if current in (None, "", []):
            out[key] = value

    # Lists: union, keeping the survivor's order first. The loser's NAME becomes
    # an alias so the company stays findable under it (SEC's "Page Lawrence" ->
    # "Larry Page" does the same for persons).
    for key, extra in (
        ("aliases", [dead.get("name")]),
        ("countries", []),
        ("hq_locations", []),
    ):
        merged: list = []
        seen: set = set()
        for value in list(keep.get(key) or []) + list(dead.get(key) or []) + extra:
            value = (value or "").strip() if isinstance(value, str) else value
            if not value:
                continue
            marker = value.lower() if isinstance(value, str) else value
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(value)
        # Don't list the survivor's own name as an alias of itself.
        if key == "aliases":
            own = (keep.get("name") or "").strip().lower()
            merged = [a for a in merged if a.lower() != own]
        if merged != list(keep.get(key) or []):
            out[key] = merged

    out["name_credibility"] = max(keep.get("name_credibility") or 0,
                                  dead.get("name_credibility") or 0)
    if keep.get("verified") or dead.get("verified"):
        out["verified"] = True

    # Keep the FULL_TEXT column consistent with the merged aliases, or the company
    # stops being findable under a name it just absorbed.
    name = keep.get("name") or ""
    description = out.get("description", keep.get("description")) or ""
    aliases = out.get("aliases", keep.get("aliases")) or []
    out["search_text"] = " ".join(p for p in (name, description, " ".join(aliases)) if p).strip()
    return out


def _coalesce_entity_props(dead_id: str, keep_id: str) -> int:
    """Copy the loser's data onto the survivor before it is deleted.

    Returns the number of properties written. Best-effort: the edges are already
    migrated by the time this runs, so a failure must not abort the merge and
    leave the graph half-joined.
    """
    def _load(eid):
        rows = run_sql("SELECT FROM Entity WHERE id = :id", {"id": eid})
        return {k: v for k, v in rows[0].items() if not k.startswith("@")} if rows else {}

    try:
        keep, dead = _load(keep_id), _load(dead_id)
        if not keep or not dead:
            return 0
        updates = _merge_entity_props(keep, dead)
        if not updates:
            return 0
        assignments = ", ".join(f"e.{k} = ${k}" for k in updates)
        run_command(f"MATCH (e:Entity {{id: $keep_id}}) SET {assignments}",
                    {"keep_id": keep_id, **updates})
        return len(updates)
    except Exception as exc:  # noqa: BLE001 - never abort a merge on this
        log.warning("could not coalesce props %s -> %s: %s", dead_id, keep_id, exc)
        return 0


def _migrate_entity_edges(dead_id: str, keep_id: str) -> int:
    """Move every OWNS / HAS_ROLE / RELATED_TO edge off ``dead_id`` onto ``keep_id``.

    An edge is RECREATED, not moved, so its property list decides what
    survives — and hand-written lists here fell behind three separate times
    (interest_types/direct_or_indirect/psc_self_link, then the share counts,
    then shortcut/also_ultimate/until_reason/value_usd). The lists now come
    from ``edge_schema``: one tuple per edge kind, and the RETURN and CREATE
    clauses are generated from it, so a property added to the schema is
    carried through every merge without this function changing.

    Why not ``properties(r)`` server-side: prod ArcadeDB silently no-ops
    cross-edge property reads. Generated clauses with bound $params are the
    one shape proven reliable there.

    An edge that ``keep`` already has (active, same target/role/relation) is
    dropped rather than duplicated. Returns the number migrated.
    """
    from app.scraper.edge_schema import (OWNS_PROPS, ROLE_PROPS, RELATED_TO_PROPS,
                                         edge_return_clause, edge_create_clause,
                                         edge_params)
    migrated = 0

    # 1. Outgoing OWNS. Labelled endpoints so the id lookups are index-backed —
    # a label-less match full-scans every node (~14s each on 3M), which hung a
    # merge once.
    for e in run_query(
        f"""MATCH (a:Entity {{id: $id}})-[r:OWNS]->(t:Entity)
            RETURN t.id AS tid, {edge_return_clause('r', OWNS_PROPS)}""",
        {"id": dead_id},
    ):
        if run_query(
            "MATCH (a:Entity {id: $k})-[r:OWNS]->(t:Entity {id: $tid}) "
            "WHERE r.until IS NULL RETURN r LIMIT 1",
            {"k": keep_id, "tid": e["tid"]},
        ):
            continue
        run_command(
            f"""MATCH (a:Entity {{id: $k}}), (t:Entity {{id: $tid}})
                CREATE (a)-[:OWNS {{{edge_create_clause(OWNS_PROPS)}}}]->(t)""",
            {"k": keep_id, "tid": e["tid"], **edge_params(e, OWNS_PROPS)},
        )
        migrated += 1

    # 2. Incoming OWNS — the owner may be a Person or an Entity, so its label
    # is captured at read time and interpolated, keeping the match index-backed.
    for e in run_query(
        f"""MATCH (s)-[r:OWNS]->(b:Entity {{id: $id}})
            RETURN s.id AS sid, labels(s) AS slabels,
                   {edge_return_clause('r', OWNS_PROPS)}""",
        {"id": dead_id},
    ):
        slabels = e.get("slabels") or []
        slabel = slabels[0] if slabels and slabels[0] in ("Entity", "Person") else "Entity"
        if run_query(
            f"MATCH (s:{slabel} {{id: $sid}})-[r:OWNS]->(b:Entity {{id: $k}}) "
            "WHERE r.until IS NULL RETURN r LIMIT 1",
            {"sid": e["sid"], "k": keep_id},
        ):
            continue
        run_command(
            f"""MATCH (s:{slabel} {{id: $sid}}), (b:Entity {{id: $k}})
                CREATE (s)-[:OWNS {{{edge_create_clause(OWNS_PROPS)}}}]->(b)""",
            {"sid": e["sid"], "k": keep_id, **edge_params(e, OWNS_PROPS)},
        )
        migrated += 1

    # 3. Incoming HAS_ROLE.
    for e in run_query(
        f"""MATCH (p:Person)-[r:HAS_ROLE]->(b:Entity {{id: $id}})
            RETURN p.id AS pid, {edge_return_clause('r', ROLE_PROPS)}""",
        {"id": dead_id},
    ):
        if run_query(
            "MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(b:Entity {id: $k}) "
            "WHERE r.role = $role AND r.until IS NULL RETURN r LIMIT 1",
            {"pid": e["pid"], "k": keep_id, "role": e.get("role")},
        ):
            continue
        run_command(
            f"""MATCH (p:Person {{id: $pid}}), (b:Entity {{id: $k}})
                CREATE (p)-[:HAS_ROLE {{{edge_create_clause(ROLE_PROPS)}}}]->(b)""",
            {"pid": e["pid"], "k": keep_id, **edge_params(e, ROLE_PROPS)},
        )
        migrated += 1

    # 4. RELATED_TO, both directions — filing-group membership and 13F fund
    # affiliation. Direction preserved: a member points AT its group.
    for e in run_query(
        f"""MATCH (a:Entity {{id: $id}})-[r:RELATED_TO]->(t:Entity)
            RETURN t.id AS tid, {edge_return_clause('r', RELATED_TO_PROPS)}""",
        {"id": dead_id},
    ):
        if run_query(
            "MATCH (a:Entity {id: $k})-[r:RELATED_TO]->(t:Entity {id: $tid}) "
            "WHERE r.relation = $rel RETURN r LIMIT 1",
            {"k": keep_id, "tid": e["tid"], "rel": e.get("relation")},
        ):
            continue
        run_command(
            f"""MATCH (a:Entity {{id: $k}}), (t:Entity {{id: $tid}})
                CREATE (a)-[:RELATED_TO {{{edge_create_clause(RELATED_TO_PROPS)}}}]->(t)""",
            {"k": keep_id, "tid": e["tid"], **edge_params(e, RELATED_TO_PROPS)},
        )
        migrated += 1

    for e in run_query(
        f"""MATCH (s)-[r:RELATED_TO]->(b:Entity {{id: $id}})
            RETURN s.id AS sid, labels(s) AS slabels,
                   {edge_return_clause('r', RELATED_TO_PROPS)}""",
        {"id": dead_id},
    ):
        slabels = e.get("slabels") or []
        slabel = slabels[0] if slabels and slabels[0] in ("Entity", "Person") else "Entity"
        if run_query(
            f"MATCH (s:{slabel} {{id: $sid}})-[r:RELATED_TO]->(b:Entity {{id: $k}}) "
            "WHERE r.relation = $rel RETURN r LIMIT 1",
            {"sid": e["sid"], "k": keep_id, "rel": e.get("relation")},
        ):
            continue
        run_command(
            f"""MATCH (s:{slabel} {{id: $sid}}), (b:Entity {{id: $k}})
                CREATE (s)-[:RELATED_TO {{{edge_create_clause(RELATED_TO_PROPS)}}}]->(b)""",
            {"sid": e["sid"], "k": keep_id, **edge_params(e, RELATED_TO_PROPS)},
        )
        migrated += 1

    from app.claims import migrate_claims
    migrate_claims(dead_id, keep_id)

    return migrated

#: The credibility floor of the official tier. GLEIF (92), UK PSC (97) and SEC
#: EDGAR (98) sit above it; Wikidata (80) and OpenCorporates (85) below. Tier by
#: score rather than by source name, so a new source lands in the right tier by
#: setting its credibility honestly instead of by editing lists here.
OFFICIAL_TIER_MIN_CREDIBILITY = 90

#: How long a community-tier ownership assertion stays current without anybody
#: re-confirming it. Six months: Wikidata has no retirement signal at all — a
#: deleted statement simply stops appearing — so age of last confirmation is the
#: only evidence available, and it is weak evidence, which is why the outcome is
#: a marking and not a closure.
STALE_AFTER_DAYS = 180


def mark_stale_ownership(days: int = STALE_AFTER_DAYS) -> dict:
    """Mark community-tier OWNS edges nothing has confirmed in `days` as stale.

    The Wikidata half of the removal problem. The register sources have real
    retirement channels — GLEIF deltas close relationships, the PSC snapshot diff
    closes vanished records, a 13G/A can amend to 0% — but a Wikidata statement
    that an editor deletes just stops being seen, and the edge it created would
    otherwise stand forever, indistinguishable from a confirmed fact.

    So: dimmed, never deleted, never closed. An unconfirmed community edge is
    weak evidence of removal — the next scrape may simply not have covered that
    company — and stamping `until` would assert an end date nobody stated.

    Exempt, in order of the reason:
      * edges at or above the official tier — their sources retire facts properly;
      * edges whose PAIR any official-tier source corroborates (a Claim with
        credibility ≥ 90) — the register vouches for the fact even though the
        edge happens to carry community attribution;
      * closed edges — history is not stale, it is history.

    Clears as well as sets, so the pass is self-healing in both directions and a
    re-confirmed edge recovers on the next run even if the write path's own
    clearing were bypassed.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Pairs an official-tier source vouches for, from the claims.
    vouched: set[tuple] = set()
    for r in run_sql("SELECT from_id, to_id FROM Claim "
                     "WHERE kind = 'owns' AND credibility_score >= :c",
                     {"c": OFFICIAL_TIER_MIN_CREDIBILITY}):
        vouched.add((r["from_id"], r["to_id"]))

    marked = cleared = 0
    with db.get_session() as session:
        rows = list(session.run(
            """MATCH (a)-[r:OWNS]->(b)
               WHERE r.until IS NULL
                 AND COALESCE(r.credibility_score, 0) < $tier
               RETURN a.id AS aid, b.id AS bid, r.last_scraped_at AS seen,
                      r.stale AS stale""",
            tier=OFFICIAL_TIER_MIN_CREDIBILITY))
        for r in rows:
            is_stale = bool(r["seen"]) and str(r["seen"]) < cutoff \
                and (r["aid"], r["bid"]) not in vouched
            if is_stale and not r["stale"]:
                session.run(
                    """MATCH (a {id: $a})-[r:OWNS]->(b {id: $b})
                       WHERE r.until IS NULL SET r.stale = true""",
                    a=r["aid"], b=r["bid"])
                marked += 1
            elif r["stale"] and not is_stale:
                session.run(
                    """MATCH (a {id: $a})-[r:OWNS]->(b {id: $b})
                       WHERE r.until IS NULL SET r.stale = false""",
                    a=r["aid"], b=r["bid"])
                cleared += 1
    return {"marked": marked, "cleared": cleared, "community_edges": len(rows),
            "cutoff": cutoff}


# Depth to search for a direct chain before calling a shortcut load-bearing.
# Corporate structures are deep but not unbounded; erring short is the safe
# direction, since an unfound chain leaves the edge drawn.
_SHORTCUT_MAX_DEPTH = 6
# Edge updates per sqlscript round-trip, matching the dedup pass's batching.
_SHORTCUT_WRITE_BATCH = 500

def mark_ownership_shortcuts(limit: int | None = None) -> dict:
    """Flag the GLEIF ultimate-parent edges that duplicate a path already in the graph.

    GLEIF records "X is the ultimate parent of Y" alongside the chain that links
    them, so most ``indirect`` OWNS edges are shortcuts for a route the graph
    already draws — and drawn, they are indistinguishable from a direct holding.
    But not all: where GLEIF gave the top of a chain and not its steps, the
    shortcut is the only ownership there is. Filtering by *kind* rather than by
    proof removed those companies from the graph entirely; this computes the
    proof.

    Per edge: is the target reachable from the same parent over ``direct`` edges
    only? Then ``shortcut = true`` and the renderer may omit it. Otherwise
    ``shortcut = false`` and it must be drawn.

    Reachability is deliberately restricted to direct edges. Allowing a route
    through another *indirect* edge would be circular — that edge may itself be
    hidden — and is exactly the mistake that produced the regression.

    Whether an edge is redundant is a property of the whole graph, not of the
    record, so this is a batch pass rather than an import-time decision, and it
    must be **re-run after every import**: a delta that retires a direct edge
    turns a redundant shortcut into the only link to a company. It clears flags
    as well as setting them for that reason, and is idempotent.

    Cost: the closure is built in memory from two bulk reads rather than one
    traversal per parent. Measured on the dev database (233k companies, 19k OWNS
    edges): 1.4 s, against 562 s for the per-parent form. If the edge set ever
    outgrows memory, the ``_DiskMap`` in ``bulk_import`` is the established answer.

    ``limit`` bounds the number of PARENTS processed; ``remaining`` reports the rest.
    """
    direct_edges = run_query(
        "MATCH (a)-[r:OWNS]->(b) WHERE r.direct_or_indirect = 'direct' "
        "RETURN a.id AS a, b.id AS b")
    indirect_edges = run_query(
        "MATCH (a)-[r:OWNS]->(b) WHERE r.direct_or_indirect = 'indirect' "
        "RETURN a.id AS a, b.id AS b, r.shortcut AS flag")

    adjacency: dict[str, list[str]] = {}
    for e in direct_edges:
        a, b = e.get("a"), e.get("b")
        if a and b:
            adjacency.setdefault(a, []).append(b)

    by_parent: dict[str, list[dict]] = {}
    for e in indirect_edges:
        if e.get("a") and e.get("b"):
            by_parent.setdefault(e["a"], []).append(e)

    parents = sorted(by_parent)
    batch = parents if limit is None else parents[:limit]

    pending: list[tuple[str, str, bool]] = []
    unchanged = 0
    for pid in batch:
        reachable = _reachable_by_direct(pid, adjacency)
        for edge in by_parent[pid]:
            target, was = edge["b"], edge.get("flag")
            now = target in reachable
            if was is not None and bool(was) == now:
                unchanged += 1
                continue
            pending.append((pid, target, now))

    _write_shortcut_flags(pending)
    result = {
        "parents_total": len(parents),
        "parents_processed": len(batch),
        "remaining": max(0, len(parents) - len(batch)),
        "marked_redundant": sum(1 for _, _, v in pending if v),
        "marked_load_bearing": sum(1 for _, _, v in pending if not v),
        "unchanged": unchanged,
    }
    log.info("Ownership shortcut pass: %s", result)
    return result


def _reachable_by_direct(start: str, adjacency: dict[str, list[str]]) -> set[str]:
    """Everything reachable from `start` over direct edges, to _SHORTCUT_MAX_DEPTH."""
    seen: set[str] = set()
    frontier = [start]
    for _ in range(_SHORTCUT_MAX_DEPTH):
        nxt = [child for node in frontier for child in adjacency.get(node, ())
               if child not in seen]
        if not nxt:
            break
        seen.update(nxt)
        frontier = nxt
    return seen


def _write_shortcut_flags(pending: list[tuple[str, str, bool]]) -> None:
    """Persist the decided flags, batched — one round-trip per edge would make the
    pass slower than the traversal it replaced."""
    for i in range(0, len(pending), _SHORTCUT_WRITE_BATCH):
        chunk = pending[i:i + _SHORTCUT_WRITE_BATCH]
        stmts, params = [], {}
        for k, (parent, target, value) in enumerate(chunk):
            params[f"p{k}"], params[f"b{k}"], params[f"v{k}"] = parent, target, value
            # `@out.id` / `@in.id`, NOT `out.id`. On an edge, the unprefixed form
            # matches zero rows and reports success — verified against a real
            # ArcadeDB, along with `out IN (SELECT ...)`, which fails the same
            # silent way. Same trap as the Vanguard succession delete.
            stmts.append(
                f"UPDATE OWNS SET shortcut = :v{k} WHERE direct_or_indirect = 'indirect' "
                f"AND @out.id = :p{k} AND @in.id = :b{k};")
        try:
            run_sqlscript("\n".join(stmts), params)
        except Exception as exc:  # noqa: BLE001 — a failed chunk must not lose the rest
            log.warning("shortcut flag batch failed (%d edges): %s", len(chunk), exc)


def _duplicate_keys(key_prop: str) -> list[str]:
    """Return only the identifier values that appear on more than one Entity.

    Aggregated server-side (GROUP BY … HAVING count > 1) so we ship back just the
    handful of *duplicated* keys, never the whole entity set — the difference
    between a bounded response and loading a full GLEIF import into memory.
    """
    rows = run_query(
        f"MATCH (e:Entity) WHERE e.{key_prop} IS NOT NULL "
        f"WITH e.{key_prop} AS key, count(e) AS cnt WHERE cnt > 1 RETURN key"
    )
    return [r["key"] for r in rows]


# Edges are single-source (one fact, one source) → deleting by source_id is exact.
_WIPE_EDGE_TYPES = ["OWNS", "HAS_ROLE", "RELATED_TO", "DUAL_LISTED_WITH", "SUCCEEDED_BY"]
# Nodes carry a single origin source_id; only delete the ones this source created
# that are left with no edges (degree 0) after its edges go — a node another source
# still references is kept.
_WIPE_NODE_TYPES = ["Entity", "Person"]


def _batched_delete(where_sql: str, params: dict, batch: int) -> int:
    """`DELETE FROM … WHERE … LIMIT batch` in a loop until drained. Small batches
    keep each request under the proxy timeout and never hold a whole-DB lock (the
    dev-db gotcha). A missing type is treated as nothing to delete."""
    total = 0
    while True:
        try:
            r = run_sql(f"{where_sql} LIMIT {batch}", params)
        except RuntimeError as exc:
            if "was not found" in str(exc):
                return total
            raise
        n = int(r[0].get("count", 0)) if r and isinstance(r[0], dict) else 0
        total += n
        if n < batch:
            return total


def wipe_source(source_name: str, batch: int = 10000, rebuild_indexes: bool = True,
                id_prefixes: list[str] | None = None) -> dict:
    """Delete one source's contribution to the graph — its edges, then the nodes
    only it created (now orphaned). Nodes/edges another source also references are
    kept: a scraped GLEIF company that also has Wikidata edges survives (minus
    GLEIF's own edges). Batched (never a single mass delete), and it never drops a
    type — other sources' data lives in the same types — so this can't wipe the
    whole DB. A fresh start is a database drop, not this command.

    `id_prefixes` makes the node deletion fast for a source whose nodes are keyed by
    a known prefix (UK PSC → `chpsc:` persons, `gb-coh:` companies): it deletes by an
    index-backed `id` range instead of an unindexed `source_id` full scan (which
    crawls once a type holds millions of rows + delete tombstones). Still
    degree-aware (the `both().size() = 0` filter) and still `source_id`-guarded.

    `rebuild_indexes` (default on) runs `REBUILD INDEX *` afterwards to clear the
    stale index entries a batched `DELETE` leaves behind — otherwise a later
    re-import can 500 on a duplicate-key insert against a stale entry. (The old
    whole-DB wipe got this for free by dropping + recreating the types; wipe-source
    keeps the types, so it rebuilds instead.) On a large remaining set this can be
    slow — run with a direct `--db-url` to ArcadeDB so it isn't cut off by a proxy
    read timeout.
    """
    src = run_sql("SELECT id FROM Source WHERE name = :n", {"n": source_name})
    if not src:
        known = ", ".join(r["name"] for r in run_sql("SELECT name FROM Source ORDER BY name"))
        raise ValueError(f"No Source named {source_name!r}. Known sources: {known or '(none)'}")
    sid = src[0]["id"]
    out: dict = {"source": source_name, "source_id": sid, "edges": {}, "nodes": {}}

    # 1) Edges first — single-source, so this is exact and it orphans the nodes only
    #    this source connected.
    for et in _WIPE_EDGE_TYPES:
        out["edges"][et] = _batched_delete(f"DELETE FROM {et} WHERE source_id = :s", {"s": sid}, batch)
    # 2) The source's own nodes that are now orphaned (degree 0). Shared/corroborated
    #    nodes still carry another source's edge → not orphaned → kept. With known id
    #    prefixes, restrict by an indexed id range (fast); else scan by source_id.
    prefixes = [p for p in (id_prefixes or []) if p]
    for nt in _WIPE_NODE_TYPES:
        if prefixes:
            deleted = 0
            for pfx in prefixes:
                lo, hi = pfx, pfx[:-1] + chr(ord(pfx[-1]) + 1)   # [pfx, next-after-pfx)
                deleted += _batched_delete(
                    f"DELETE FROM {nt} WHERE id >= :lo AND id < :hi AND source_id = :s "
                    "AND both().size() = 0", {"lo": lo, "hi": hi, "s": sid}, batch)
            out["nodes"][nt] = deleted
        else:
            out["nodes"][nt] = _batched_delete(
                f"DELETE FROM {nt} WHERE source_id = :s AND both().size() = 0", {"s": sid}, batch)
    # 3) Removing GLEIF's baseline must reset the incremental checkpoint, or the
    #    gleif-update cron would apply deltas onto a graph with no foundation.
    if source_name.strip().upper() == "GLEIF":
        for key in ("gleif-full-load", "gleif-update"):
            run_sql("DELETE FROM ImportState WHERE key = :k", {"k": key})
        out["reset_import_state"] = True
    # Clear the stale index entries the batched DELETE leaves behind (see docstring).
    if rebuild_indexes:
        try:
            # Long timeout — a full rebuild takes far longer than the default 60s.
            # It only completes over a DIRECT connection (nginx caps at ~600s), so
            # run wipe-source with --db-url to a tunnel/host for the reindex to land.
            r = run_sql("REBUILD INDEX *", timeout=3600)
            out["reindexed"] = int(r[0].get("totalIndexed", 0)) if r and isinstance(r[0], dict) else True
        except Exception as exc:  # noqa: BLE001 - don't lose the wipe result over a reindex hiccup
            out["reindexed"] = False
            out["reindex_error"] = str(exc)[:300]
    return out


def _not_duplicate_pairs(ids: list[str] | None = None) -> set:
    """Entity pairs a human has confirmed are DIFFERENT companies.

    Rare — they only come from keep-separate — so short-circuit on a SQL count
    over the edge type first. A vertex-anchored
    `MATCH (a:Entity)-[:NOT_DUPLICATE]->` full-scans every entity even when there
    are none, which is minutes on a full-GLEIF database. Same trick as
    `_dismissed_pairs` on the person side.
    """
    try:
        if run_sql("SELECT count(*) AS n FROM NOT_DUPLICATE")[0]["n"] == 0:
            return set()
    except Exception as exc:  # noqa: BLE001 — never block a merge on this read
        log.warning("keep-separate lookup failed, treating as none: %s", exc)
        return set()

    pairs: set = set()
    if ids is None:
        for r in run_query("MATCH (a:Entity)-[:NOT_DUPLICATE]->(b:Entity) "
                           "RETURN a.id AS a, b.id AS b"):
            pairs.add(frozenset((r.get("a"), r.get("b"))))
    else:
        for eid in dict.fromkeys(ids):
            for r in run_query("MATCH (e:Entity {id:$id})-[:NOT_DUPLICATE]-(o:Entity) "
                               "RETURN o.id AS o", {"id": eid}):
                pairs.add(frozenset((eid, r.get("o"))))
    return pairs


def _record_entity_merge_log(dead: dict, keep: dict) -> None:
    """Audit trail for an entity merge.

    Person merges have had one since they existed; entity merges — the riskier
    of the two, since they run automatically during scraping and destroyed the
    loser's data until #205 — had none at all. Deduped on (keep, dup_name) like
    the person log, so a re-scraped duplicate bumps `count` instead of piling up
    rows. `kind` separates the two so neither log shows the other's entries.
    """
    try:
        run_command(
            "MERGE (ml:MergeLog {keep_id:$keep, dup_name:$dup_name}) "
            "SET ml.id = COALESCE(ml.id, $id), ml.kind = 'entity', "
            "    ml.keep_name = $keep_name, ml.dup_id = $dup_id, ml.at = $at, "
            "    ml.count = COALESCE(ml.count, 0) + 1",
            {"keep": keep["id"], "dup_name": (dead.get("name") or "").strip(),
             "keep_name": (keep.get("name") or "").strip(), "dup_id": dead["id"],
             "id": str(uuid.uuid4()), "at": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as exc:  # noqa: BLE001 — the merge itself already happened
        log.warning("could not write entity merge log (%s -> %s): %s",
                    dead.get("id"), keep.get("id"), exc)


def _apply_entity_merge(dead: dict, keep: dict) -> int:
    """Fold `dead` into `keep`, in the one order that keeps everything.

    Extracted because this sequence was duplicated across the scoped and full
    dedup paths, and steps kept getting added to one and not the other — the
    forwarding address was missing from half of them until #204, and the
    property carry-over until #205. One definition, both callers.

    Order matters: edges and properties must move while the loser still exists,
    and the forwarding address must be written before the delete because the
    losing id may live in a shared link, a client cache, or a peer's copy.
    """
    migrated = _migrate_entity_edges(dead["id"], keep["id"])
    _coalesce_entity_props(dead["id"], keep["id"])
    record_merge_sql(dead["id"], keep["id"], kind="Entity")
    _record_entity_merge_log(dead, keep)
    run_command("MATCH (e:Entity {id: $id}) DETACH DELETE e", {"id": dead["id"]})
    return migrated


def deduplicate_entities_for(entity_ids: list[str], apply: bool = True) -> dict:
    """Scoped, high-confidence-only entity auto-merge — the entity twin of the
    post-scrape person dedup. For the entities a scrape just touched, merge any
    same-``name_normalized`` group whose confidence is ``definitive`` (members share
    a hard external id) or ``high`` (same registered address); ``medium``/``low``
    groups are left for human review (``find_duplicate_entity_names``).

    Scoped via indexed id/name lookups over the (small) touched set, so it never
    runs the full-DB same-name aggregation (``_duplicate_name_groups``). The
    survivor is the highest ``name_credibility`` node (then verified, then smallest
    id), and the loser's edges are migrated onto it before it's deleted.
    """
    if not entity_ids:
        return {"entities_merged": 0, "groups_checked": 0, "needs_review": 0, "detail": []}

    # Normalized names of the touched entities (id is UNIQUE-indexed → fast).
    names: set[str] = set()
    for eid in dict.fromkeys(entity_ids):
        rows = run_sql("SELECT name_normalized AS n FROM Entity WHERE id = :id", {"id": eid})
        n = rows[0].get("n") if rows else None
        if n:
            names.add(n)

    # Scoped to the touched entities — the whole point of this path is to avoid
    # full-DB work, and the same applies to reading their keep-separate marks.
    kept_separate = _not_duplicate_pairs(list(dict.fromkeys(entity_ids)))

    merged: list[dict] = []
    review = 0
    for nn in names:
        members = run_sql(
            "SELECT id, name, country, founded, lei_id, companies_house_id, sec_cik, "
            "wikidata_id, registered_address, COALESCE(name_credibility, 0) AS cred, "
            "COALESCE(verified, false) AS verified FROM Entity "
            "WHERE name_normalized = :nn AND type <> 'voting_group' LIMIT 50", {"nn": nn})
        members = [{k: v for k, v in m.items() if not k.startswith("@")} for m in members]
        if len(members) < 2:
            continue
        if _group_confidence(members) not in ("definitive", "high"):
            review += 1
            continue
        members.sort(key=lambda m: (-(m.get("cred") or 0), not m.get("verified"), m["id"]))
        keep = members[0]
        for dead in members[1:]:
            # Never merge a pair a human has confirmed is two different companies.
            # Checked per pair rather than per group: a third member should not
            # drag a node someone explicitly separated into a destructive merge.
            if frozenset((dead["id"], keep["id"])) in kept_separate:
                review += 1
                continue
            if apply:
                _apply_entity_merge(dead, keep)
            merged.append({"kept": keep["name"], "kept_id": keep["id"],
                           "deleted": dead["name"], "deleted_id": dead["id"]})

    return {"entities_merged": len(merged), "groups_checked": len(names),
            "needs_review": review, "detail": merged[:100]}


def deduplicate_entities(limit: int | None = 300) -> dict:
    """
    Merge Entity nodes that share a stable external identifier — the same LEI,
    Companies House number, SEC CIK or Wikidata id — into one, migrating their edges
    and deleting the extras. This is the cross-source merge: the same company arriving
    from two sources (e.g. a GLEIF node and a PSC node for one UK company, both keyed
    on companies_house_id) collapses to one node — no name match or Wikidata required.
    Also heals duplicates left by the older BODS importer, which keyed
    entities on the per-dump BODS recordId, so the same company imported in two
    runs became two nodes. Admin only.

    Processes at most ``limit`` duplicate groups per call and reports how many
    remain, so a large heal is done in bounded batches that each finish under the
    HTTP/proxy request timeout — call repeatedly until ``remaining`` is 0, or pass
    ``limit=None`` to process every group in one go (used by the background job,
    which isn't bound by the request timeout). For each group the survivor is the
    highest ``name_credibility`` node (then verified, then the lexically-smallest
    id, for a deterministic result).
    """
    # All duplicate groups across every hard external identifier (cheap aggregation).
    # A shared LEI / Companies House number / SEC CIK / Wikidata id ⇒ same company,
    # so this merges across sources (e.g. a GLEIF node and a PSC node for the same UK
    # company share companies_house_id) — no name match or Wikidata hub required.
    dup_keys = [("lei_id", k) for k in _duplicate_keys("lei_id")]
    dup_keys += [("companies_house_id", k) for k in _duplicate_keys("companies_house_id")]
    dup_keys += [("sec_cik", k) for k in _duplicate_keys("sec_cik")]
    dup_keys += [("wikidata_id", k) for k in _duplicate_keys("wikidata_id")]
    total = len(dup_keys)
    batch = dup_keys if limit is None else dup_keys[:limit]

    # Read once for the whole run rather than per group: this path is unscoped,
    # and the count short-circuit makes it free when nobody has kept anything apart.
    kept_separate = _not_duplicate_pairs()

    merged: list[dict] = []
    for key_prop, key in batch:
        members = run_query(
            f"MATCH (e:Entity) WHERE e.{key_prop} = $key "
            f"RETURN e.id AS id, e.name AS name, "
            f"COALESCE(e.name_credibility, 0) AS cred, COALESCE(e.verified, false) AS verified",
            {"key": key},
        )
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (-(m.get("cred") or 0), not m.get("verified"), m["id"]))
        keep = members[0]
        for dead in members[1:]:
            if frozenset((dead["id"], keep["id"])) in kept_separate:
                continue
            migrated = _apply_entity_merge(dead, keep)
            merged.append({
                "key": f"{key_prop}={key}",
                "kept": keep["name"], "kept_id": keep["id"],
                "deleted": dead["name"], "deleted_id": dead["id"],
                "edges_migrated": migrated,
            })

    return {
        "entities_merged": len(merged),
        "groups_processed": len(batch),
        "duplicate_groups_found": total,
        "remaining": max(0, total - len(batch)),
        "detail": merged[:100],   # cap payload; counts above are complete
    }


# ArcadeDB caps a single GROUP BY at queryMaxHeapElementsAllowedPerOp (500k)
# groups. At full-GLEIF scale there are millions of distinct LEIs, so we shard
# the key space by prefix and sub-shard adaptively when a shard still trips it.
_SHARD_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"   # LEI / Companies House ids are upper-alnum
_GROUP_LIMIT_MARKERS = ("queryMaxHeapElementsAllowedPerOp", "in-heap GROUP")


def _dup_groups_sharded(key_prop: str) -> list[tuple[str, str, int]]:
    """(value, keeper-id, member-count) for every value on >1 Entity.

    Grouped scan restricted to a key prefix (range ``[prefix, prefix+'{')`` —
    ``{`` sorts just past ``Z``/``9``), split one level deeper whenever the shard
    exceeds ArcadeDB's in-heap group cap. ArcadeDB SQL has no ``HAVING``, so the
    ``c > 1`` filter wraps the grouped subquery.
    """
    found: list[tuple[str, str, int]] = []

    def _collect(prefix: str) -> None:
        q = (f"SELECT FROM (SELECT {key_prop} AS k, count(*) AS c, min(id) AS keep "
             f"FROM Entity WHERE {key_prop} >= :lo AND {key_prop} < :hi "
             f"GROUP BY {key_prop}) WHERE c > 1")
        try:
            rows = run_sql(q, {"lo": prefix, "hi": prefix + "{"})
        except RuntimeError as exc:
            if not any(m in str(exc) for m in _GROUP_LIMIT_MARKERS):
                raise
            for ch in _SHARD_CHARSET:
                _collect(prefix + ch)
            return
        found.extend((r["k"], r["keep"], int(r["c"])) for r in rows)

    _collect("")
    return found


def deduplicate_entities_bulk(batch_size: int = 200) -> dict:
    """
    Fast heal for the recordId-keyed BODS doubling. For each external id (LEI,
    then Companies House number) that sits on more than one Entity, keep the
    lexicographically-smallest node id and ``DELETE VERTEX`` the rest — which also
    drops their edges (ArcadeDB detaches on vertex delete). Admin only; destructive.

    Unlike :func:`deduplicate_entities` this does **not** migrate the losers'
    edges onto the survivor. That's deliberate: the merge-with-migration can't
    finish at full-GLEIF scale (per-group/per-edge round trips), whereas this is a
    grouped scan per id kind (prefix-sharded to stay under ArcadeDB's group cap)
    plus batched deletes. Safe here because the surviving node already carries the
    import's edges and anything missed is re-scrapeable.
    """
    removed_total = 0
    by: dict[str, dict] = {}
    for key_prop in ("lei_id", "companies_house_id"):
        groups = _dup_groups_sharded(key_prop)   # (value, keeper, member-count)
        removed = sum(c - 1 for _, _, c in groups)
        for i in range(0, len(groups), batch_size):
            chunk = groups[i:i + batch_size]
            stmts, params = [], {}
            for n, (k, keep, _c) in enumerate(chunk):
                params[f"k__{n}"] = k
                params[f"keep__{n}"] = keep
                stmts.append(
                    f"DELETE VERTEX FROM Entity WHERE {key_prop} = :k__{n} AND id <> :keep__{n};")
            if stmts:
                run_sqlscript("\n".join(stmts), params)
        by[key_prop] = {"groups": len(groups), "entities_removed": removed}
        removed_total += removed
    return {"entities_removed": removed_total, "by": by}


def migrate_ownership_types() -> dict:
    """
    Re-derive canonical ownership_type for all OWNS edges, IN PLACE.

    Classify by `stake_percent` when a stake is disclosed
      (>=99 full · >50 majority · >=20 controlling · >0 minority);
    a stakeless 'majority' — the old Wikidata "owner with no %" default — becomes
    'unknown' (we don't assert minority/majority without a disclosed %); every other
    stakeless edge keeps its type, because it carries a real signal (a GLEIF consolidation
    'controlling' edge, or an SEC 13D/13G form-derived type).

    Writes are an UPDATE by @rid (via run_sqlscript) — never a DELETE+CREATE. The old
    delete-then-recreate both DUPLICATED active edges (its `WHERE r.until = null` never
    matched on ArcadeDB, so nothing was deleted before the re-create) and dropped every
    edge property it didn't explicitly copy. A direct-record UPDATE avoids both.
    """
    edges = run_sql("SELECT @rid AS rid, stake_percent AS stake, ownership_type AS ot FROM OWNS")
    updates: list[tuple[str, str]] = []
    for e in edges:
        d = dict(e)
        stake, ot = d.get("stake"), d.get("ot")
        if stake is not None:
            new = _derive_ownership_type(stake)
        elif ot == "majority":
            new = "unknown"
        else:
            continue                      # stakeless controlling / form-derived → keep
        if new != ot:
            updates.append((d.get("rid"), new))

    for i in range(0, len(updates), 400):
        chunk = updates[i:i + 400]
        # ownership_type is a fixed vocabulary and rid is a #x:y record id → safe to inline.
        run_sqlscript(";".join(f"UPDATE {rid} SET ownership_type = '{t}'" for rid, t in chunk))

    return {"status": "ok", "updated": len(updates), "skipped": len(edges) - len(updates)}

# Alternate country spellings seen in external data that the canonical
# _ISO2_COUNTRY map does not carry (matched case-insensitively).
_COUNTRY_NAME_VARIANTS: dict[str, str] = {
    "UAE": "AE",
    "South Korea": "KR",
    "Korea, Republic of": "KR",
    "Republic of Korea": "KR",
    "North Korea": "KP",
    "Korea, Democratic People's Republic of": "KP",
    "Czechia": "CZ",
    "United States of America": "US",
    "USA": "US",
    "Russian Federation": "RU",
    "Viet Nam": "VN",
    "Türkiye": "TR",
    "Turkiye": "TR",
    "The Netherlands": "NL",
    "Ivory Coast": "CI",
    "Côte d'Ivoire": "CI",
    "Republic of Ireland": "IE",
    "Great Britain": "GB",
    "Taiwan, Province of China": "TW",
    "Hong Kong SAR": "HK",
    "Macau": "MO",
    "Brunei Darussalam": "BN",
    "Lao People's Democratic Republic": "LA",
    "Syrian Arab Republic": "SY",
    "Moldova, Republic of": "MD",
    "Tanzania, United Republic of": "TZ",
    "Iran, Islamic Republic of": "IR",
    "Venezuela, Bolivarian Republic of": "VE",
    "Bolivia, Plurinational State of": "BO",
}


def normalize_entity_countries() -> dict:
    """
    One-time migration: convert full-name Entity.country values (as older
    BODS imports stored them, e.g. 'Brazil') to ISO-2 codes ('BR'), the
    canonical form the Wikidata scraper writes. Mixed forms made countries
    appear twice in by-country groupings. Idempotent: values that are
    already codes (or unrecognized) are left untouched.
    """
    from app.scraper.bulk_import import _ISO2_COUNTRY
    # Case-insensitive name lookup, extended with spellings other sources use.
    name_to_code = {name.lower(): code for code, name in _ISO2_COUNTRY.items()}
    name_to_code.update({name.lower(): code for name, code in _COUNTRY_NAME_VARIANTS.items()})

    rows = run_query(
        "MATCH (e:Entity) WHERE e.country IS NOT NULL RETURN DISTINCT e.country AS country"
    )
    converted: list[dict] = []
    skipped = 0
    for r in rows:
        raw = r["country"]
        cleaned = (raw or "").strip()
        code = name_to_code.get(cleaned.lower())
        if code is None and len(cleaned) == 2 and cleaned.upper() in _ISO2_COUNTRY:
            code = cleaned.upper()  # lowercase/whitespace-padded codes -> canonical
        if code and code != raw:
            run_command(
                "MATCH (e:Entity) WHERE e.country = $old SET e.country = $new",
                {"old": raw, "new": code},
            )
            converted.append({"from": raw, "to": code})
        else:
            skipped += 1

    return {"converted": converted, "skipped": skipped}


# ── Nationality ───────────────────────────────────────────────────────────────
#
# Two sources write Person.nationality in two different shapes. Wikidata gives an
# ISO-2 code (P297), while Companies House PSC gives a **demonym** typed by the
# filer — "British", not "United Kingdom" and not "GB". So the field held a mix of
# `GB` and `British` meaning the same thing, which is unusable for grouping and
# looks like two nationalities wherever both appear.
#
# ISO-2 wins as the canonical form: it is what Wikidata already writes, what
# Entity.country already uses, and a demonym is recoverable from a code for display
# while the reverse needs this table.
#
# Demonyms only — country names are handled by _COUNTRY_NAME_VARIANTS above and are
# also accepted here, since PSC filers sometimes type "Ireland" instead of "Irish".
_DEMONYM_ISO2: dict[str, str] = {
    # The UK, where most PSC filings come from. Companies House sees all of these.
    "british": "GB", "briton": "GB", "english": "GB", "scottish": "GB",
    "welsh": "GB", "northern irish": "GB", "uk": "GB", "u.k.": "GB",
    "united kingdom": "GB", "great britain": "GB", "gb": "GB", "gbr": "GB",
    # Ireland — distinct from Northern Irish above, and frequently confused
    "irish": "IE", "ireland": "IE",
    # Europe
    "german": "DE", "austrian": "AT", "french": "FR", "italian": "IT",
    "spanish": "ES", "portuguese": "PT", "dutch": "NL", "netherlands": "NL",
    "belgian": "BE", "luxembourgish": "LU", "luxembourger": "LU", "swiss": "CH",
    "danish": "DK", "swedish": "SE", "norwegian": "NO", "finnish": "FI",
    "icelandic": "IS", "polish": "PL", "czech": "CZ", "slovak": "SK",
    "hungarian": "HU", "romanian": "RO", "bulgarian": "BG", "greek": "GR",
    "cypriot": "CY", "maltese": "MT", "croatian": "HR", "slovenian": "SI",
    "serbian": "RS", "bosnian": "BA", "albanian": "AL", "estonian": "EE",
    "latvian": "LV", "lithuanian": "LT", "ukrainian": "UA", "russian": "RU",
    "belarusian": "BY", "moldovan": "MD", "turkish": "TR", "georgian": "GE",
    "armenian": "AM", "azerbaijani": "AZ",
    # Americas
    "american": "US", "united states": "US", "usa": "US", "canadian": "CA",
    "mexican": "MX", "brazilian": "BR", "argentine": "AR", "argentinian": "AR",
    "chilean": "CL", "colombian": "CO", "peruvian": "PE", "venezuelan": "VE",
    "uruguayan": "UY", "cuban": "CU", "jamaican": "JM", "bahamian": "BS",
    "barbadian": "BB", "panamanian": "PA", "costa rican": "CR",
    # Asia-Pacific
    "chinese": "CN", "hong kong": "HK", "hongkonger": "HK", "taiwanese": "TW",
    "japanese": "JP", "korean": "KR", "south korean": "KR", "indian": "IN",
    "pakistani": "PK", "bangladeshi": "BD", "sri lankan": "LK", "nepalese": "NP",
    "singaporean": "SG", "malaysian": "MY", "indonesian": "ID", "thai": "TH",
    "vietnamese": "VN", "filipino": "PH", "philippine": "PH", "australian": "AU",
    "new zealander": "NZ", "new zealand": "NZ", "kiwi": "NZ",
    # Middle East and Africa
    "israeli": "IL", "emirati": "AE", "uae": "AE", "saudi": "SA",
    "saudi arabian": "SA", "qatari": "QA", "kuwaiti": "KW", "bahraini": "BH",
    "omani": "OM", "jordanian": "JO", "lebanese": "LB", "iranian": "IR",
    "iraqi": "IQ", "egyptian": "EG", "moroccan": "MA", "tunisian": "TN",
    "algerian": "DZ", "south african": "ZA", "nigerian": "NG", "kenyan": "KE",
    "ghanaian": "GH", "ethiopian": "ET", "tanzanian": "TZ", "ugandan": "UG",
    "zimbabwean": "ZW", "mauritian": "MU", "seychellois": "SC",
}


def nationality_to_iso2(raw: str | None) -> str | None:
    """A nationality as an ISO-2 code, or None if it cannot be recognised.

    None means "leave it alone", not "discard it". A nationality we cannot map is
    still what the register said, and overwriting or blanking it would lose data
    the source published — so callers keep the original text.
    """
    from app.scraper.bulk_import import _ISO2_COUNTRY
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    if len(cleaned) == 2 and cleaned.upper() in _ISO2_COUNTRY:
        return cleaned.upper()          # already a code, possibly lowercase
    key = cleaned.lower().rstrip(".")
    if code := _DEMONYM_ISO2.get(key):
        return code
    # A full country name ("Brazil"), which PSC filers do sometimes type.
    name_to_code = {name.lower(): code for code, name in _ISO2_COUNTRY.items()}
    name_to_code.update({n.lower(): c for n, c in _COUNTRY_NAME_VARIANTS.items()})
    return name_to_code.get(key)


def normalize_person_nationalities() -> dict:
    """Convert Person.nationality to ISO-2 codes where the value can be recognised.

    Idempotent — codes pass through unchanged. Values that cannot be mapped are
    **left exactly as they are** and returned in `unmapped`, so the residue is
    visible and the table can be extended rather than the data quietly lost. A
    long tail is expected: PSC nationality is free text typed by the filer.
    """
    rows = run_query(
        "MATCH (p:Person) WHERE p.nationality IS NOT NULL AND p.nationality <> '' "
        "RETURN DISTINCT p.nationality AS nat")
    converted: list[dict] = []
    unmapped: list[str] = []
    unchanged = 0
    for r in rows:
        raw = r["nat"]
        code = nationality_to_iso2(raw)
        if code is None:
            unmapped.append(raw)
        elif code == raw:
            unchanged += 1
        else:
            run_command("MATCH (p:Person) WHERE p.nationality = $old SET p.nationality = $new",
                        {"old": raw, "new": code})
            converted.append({"from": raw, "to": code})
    return {"converted": converted, "unchanged": unchanged,
            "unmapped": sorted(unmapped), "distinct_values": len(rows)}


# ── Country backfill ──────────────────────────────────────────────────────────

def backfill_entity_countries(limit: int | None = None, fetch=None) -> dict:
    """Fill in `country` for entities that have none, from Wikidata and SEC EDGAR.

    Companies created as owner or subsidiary stubs never got a country of their
    own, so a company that only ever appears as an *owner* had none at all —
    BlackRock and The Vanguard Group among them, absent from the map entirely.

    Only ever fills a blank. An existing country is never overwritten: this is a
    repair for missing data, not a re-import.
    """
    from app.scraper.sec_edgar import sec_country
    from app.scraper.wikidata import countries_for

    # A voting group has no country and never will — it is an agreement, not an
    # organisation — so without this it is a permanent candidate, re-queried on
    # every run for a fact that does not exist.
    rows = run_query(
        "MATCH (e:Entity) WHERE (e.country IS NULL OR e.country = '') "
        "AND e.type <> 'voting_group' "
        "RETURN e.id AS id, e.name AS name, e.wikidata_id AS wd, e.sec_cik AS cik")
    if limit:
        rows = rows[:limit]

    filled: list[dict] = []
    # Wikidata in one batched query rather than one request per entity. Jurisdiction
    # (P17) and headquarters (P159) are written to their own fields — coalescing
    # them would defeat the map's Registered/Headquarters switch.
    wd = {r["wd"]: r for r in rows if r.get("wd")}
    if wd:
        for qid, found in countries_for(set(wd)).items():
            if not found["country"] and not found["hq_country"]:
                continue
            filled.append({"id": wd[qid]["id"], "name": wd[qid]["name"],
                           "country": found["country"], "hq_country": found["hq_country"],
                           "from": "wikidata"})

    done = {f["id"] for f in filled}
    for r in rows:
        if r["id"] in done or not r.get("cik"):
            continue
        try:
            subs = (fetch or _sec_submissions)(r["cik"])
        except Exception as exc:                                     # noqa: BLE001
            log.warning("country backfill: SEC fetch failed for %s: %s", r["name"], exc)
            continue
        if code := sec_country(subs or {}):
            filled.append({"id": r["id"], "name": r["name"], "country": code,
                           "hq_country": None, "from": "sec"})

    for f in filled:
        # COALESCE, not assignment: a value already known must not be replaced by
        # this repair, which only exists to fill blanks.
        run_command(
            "MATCH (e:Entity {id:$id}) SET e.country = COALESCE(e.country, $c), "
            "e.hq_country = COALESCE(e.hq_country, $h)",
            {"id": f["id"], "c": f["country"], "h": f["hq_country"]})

    return {"candidates": len(rows), "filled": len(filled),
            "still_unknown": len(rows) - len(filled), "changes": filled}


def backfill_sec_headquarters(limit: int | None = None, fetch=None) -> dict:
    """Fill in the headquarters of SEC filers from EDGAR's business address.

    EDGAR gives every filer a street address — "790 N Water Street, Milwaukee, WI
    53202" — and the scraper read only the state out of it, to guess a country,
    and discarded the rest. 40 of the 43 SEC companies in the dev graph ended up
    with no headquarters at all while EDGAR held their address the whole time.

    This is deliberately NOT the same thing as `backfill_entity_countries`.
    `country` is where a company is registered, and EDGAR's business address is
    poor evidence of that — a foreign filer often files through a US office. As
    the place a company is **run** the same address is exactly right, which is
    why it lands in `hq_*` and never touches `country`.

    Only ever fills a blank; an address already known is never replaced.
    """
    from app.scraper.sec_edgar import sec_headquarters

    rows = run_query(
        "MATCH (e:Entity) WHERE e.sec_cik IS NOT NULL "
        "AND (e.hq_address IS NULL OR e.hq_country IS NULL) "
        "RETURN e.id AS id, e.name AS name, e.sec_cik AS cik")
    if limit:
        rows = rows[:limit]

    filled: list[dict] = []
    for r in rows:
        try:
            subs = (fetch or _sec_submissions)(r["cik"])
        except Exception as exc:                                     # noqa: BLE001
            log.warning("HQ backfill: SEC fetch failed for %s: %s", r["name"], exc)
            continue
        hq = sec_headquarters(subs or {})
        if not hq:
            continue
        run_command(
            "MATCH (e:Entity {id:$id}) "
            "SET e.hq_address = COALESCE(e.hq_address, $a), "
            "    e.hq_city    = COALESCE(e.hq_city, $c), "
            "    e.hq_country = COALESCE(e.hq_country, $k)",
            {"id": r["id"], "a": hq["address"], "c": hq["city"], "k": hq["country"]})
        filled.append({"id": r["id"], "name": r["name"], **hq})

    return {"candidates": len(rows), "filled": len(filled),
            "still_unknown": len(rows) - len(filled), "changes": filled}


def _sec_submissions(cik: str) -> dict:
    """EDGAR submissions for a CIK. Uses the scraper's pooled client, which is what
    keeps sec.gov's dead IPv6 from costing six seconds on every new connection."""
    from app.scraper.sec_edgar import SUBMISSIONS_URL, _get
    return _get(f"{SUBMISSIONS_URL}/CIK{cik}.json")


def backfill_entity_sources() -> dict:
    """
    One-time backfill: stamp ``Entity.source_id`` on nodes the Wikidata / SEC
    EDGAR scrapers created before they set it. Without it, a pure *owner* (whose
    subsidiaries are deliberately excluded from its own source panel, and which
    has no inbound owners/roles) shows no source at all — e.g. "Government of
    Abu Dhabi" or "Vanguard Group Inc".

    Attribution is by identifier: a node with a ``wikidata_id`` → the Wikidata
    Source; else a ``sec_cik`` → the SEC EDGAR Source. Only fills nodes whose
    ``source_id`` is null (idempotent), and only when the Source node exists.
    Nodes with neither identifier are left untouched (can't attribute a source).

    Reads use ArcadeDB SQL SELECT; writes go through ``run_sqlscript`` — the only
    path in this codebase proven to *commit* a data write (a single Cypher
    ``MATCH … SET`` and a single ``run_sql`` UPDATE both left the rows unchanged
    on this engine; the BODS importer writes via sqlscript). SQL ``IS NULL``
    matches an *absent* property too, so the pre-fix nodes (whose ``source_id``
    key was never written) are found. Candidates are updated by ``id`` in
    batches — an indexed, fast write that avoids a full-type UPDATE scan.
    """
    def _source_id(name: str) -> str | None:
        rows = run_sql("SELECT id FROM Source WHERE name = :name", {"name": name})
        return rows[0]["id"] if rows else None

    def _stamp(where: str, sid: str, chunk: int = 200) -> int:
        ids = [r["id"] for r in run_sql(f"SELECT id FROM Entity WHERE {where}")]
        for i in range(0, len(ids), chunk):
            batch = ids[i:i + chunk]
            stmts  = ";\n".join(
                f"UPDATE Entity SET source_id = :sid WHERE id = :id{j}"
                for j in range(len(batch)))
            params = {"sid": sid, **{f"id{j}": batch[j] for j in range(len(batch))}}
            run_sqlscript(stmts, params)
        return len(ids)

    wikidata_src = _source_id("Wikidata")
    sec_src      = _source_id("SEC EDGAR")

    updated = {"wikidata": 0, "sec_edgar": 0}
    # Order matters: wikidata_id is the more specific attribution, so claim those
    # first; the SEC pass then only catches CIK-only nodes still missing a source.
    if wikidata_src:
        updated["wikidata"] = _stamp(
            "source_id IS NULL AND wikidata_id IS NOT NULL", wikidata_src)
    if sec_src:
        updated["sec_edgar"] = _stamp(
            "source_id IS NULL AND sec_cik IS NOT NULL", sec_src)

    remaining = run_sql("SELECT count(*) AS c FROM Entity WHERE source_id IS NULL")
    return {
        "updated": updated,
        "still_missing": remaining[0]["c"] if remaining else None,
        "wikidata_source_found": wikidata_src is not None,
        "sec_edgar_source_found": sec_src is not None,
    }


def flag_nominee_entities() -> dict:
    """
    Flag existing Entity nodes whose name is a nominee / custodian (holder of
    record, not a beneficial owner — "… Nominees Limited", custodians, Cede & Co).
    Name-derived, so this backfills nodes imported before the flag existed without
    a full re-import. Idempotent.

    Candidates come from the FULL_TEXT index (fast), then the precise
    `is_nominee_name` regex decides; matches are set `is_nominee = true` by id in
    batches via run_sqlscript (the write path proven to commit).
    """
    from app.scraper.mapper import is_nominee_name

    candidates: dict[str, str] = {}   # id -> name
    for token in ("nominee", "nominees", "custodian", "custody", "cede"):
        for r in run_sql(f"SELECT id, name FROM Entity WHERE search_text CONTAINSTEXT '{token}'"):
            if r.get("id"):
                candidates[r["id"]] = r.get("name")

    ids = [eid for eid, name in candidates.items() if is_nominee_name(name)]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        stmts = ";\n".join(
            f"UPDATE Entity SET is_nominee = true WHERE id = :id{j}" for j in range(len(chunk)))
        run_sqlscript(stmts, {f"id{j}": chunk[j] for j in range(len(chunk))})

    return {"candidates": len(candidates), "flagged": len(ids)}


def count_self_loop_owns() -> dict:
    """Count OWNS edges where owner == target (A owns A) — treasury shares or a
    data error. The full-profile drops these from the owners list on read; this
    is the global tally."""
    rows = run_sql("SELECT count(*) AS c FROM OWNS WHERE @out = @in")
    return {"self_loops": rows[0]["c"] if rows else 0}


def find_cross_holdings(limit: int = 100) -> list[dict]:
    """Reciprocal (circular) ownership: entity pairs where A owns B AND B owns A.
    `a.id < b.id` reports each pair once. A data-quality signal and the groundwork
    for a future ultimate-owner traversal (cycles must be broken)."""
    rows = run_query(
        "MATCH (a:Entity)-[:OWNS]->(b:Entity)-[:OWNS]->(a:Entity) WHERE a.id < b.id "
        "RETURN a.id AS a_id, a.name AS a_name, b.id AS b_id, b.name AS b_name "
        f"LIMIT {int(limit)}"
    )
    return [dict(r) for r in rows]
