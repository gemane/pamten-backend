"""Graph-maintenance services: purge, dedup, and one-time migrations.

Pure orchestration over the DB layer, extracted from the scraper router so
the endpoints stay thin. Behaviour is unchanged from the previous inline
implementations.
"""
from app.database import db
from app.db.arcadedb import run_query, run_command
from app.scraper.mapper import derive_ownership_type as _derive_ownership_type


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


def deduplicate_owns_edges() -> dict:
    """
    For every (owner → target) pair that has more than one active OWNS edge,
    keep the most informative edge (highest stake_percent, then most recent
    file_date) and delete the rest.  Admin only.
    """
    # Find all pairs with duplicates
    pairs = run_query(
        """
        MATCH (a)-[r:OWNS]->(b)
        WHERE r.until IS NULL
        WITH a.id AS owner_id, b.id AS target_id, count(r) AS cnt
        WHERE cnt > 1
        RETURN owner_id, target_id, cnt
        """
    )

    total_deleted = 0
    cleaned = []

    for pair in pairs:
        oid = pair["owner_id"]
        nid = pair["target_id"]

        # Fetch all active edges for this pair with their properties
        edges = run_query(
            """
            MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid})
            WHERE r.until IS NULL
            RETURN r.stake_percent   AS stake,
                   r.file_date       AS file_date,
                   r.source_id       AS source_id,
                   r.ownership_type  AS ownership_type,
                   r.since           AS since
            """,
            {"oid": oid, "nid": nid},
        )

        # Sort: prefer edge with stake_percent, then most recent file_date
        def _sort_key(e):
            return (
                0 if e.get("stake") is not None else 1,
                e.get("file_date") or "",
            )

        edges_sorted = sorted(edges, key=_sort_key, reverse=True)
        best = edges_sorted[0]

        # Delete all active edges between this pair
        run_command(
            """
            MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid})
            WHERE r.until IS NULL
            DELETE r
            """,
            {"oid": oid, "nid": nid},
        )

        # Recreate the single best edge
        run_command(
            """
            MATCH (a {id: $oid}), (b {id: $nid})
            CREATE (a)-[:OWNS {
                stake_percent:  $stake,
                file_date:      $file_date,
                source_id:      $source_id,
                ownership_type: $ownership_type,
                since:          $since,
                until:          null
            }]->(b)
            """,
            {
                "oid":            oid,
                "nid":            nid,
                "stake":          best.get("stake"),
                "file_date":      best.get("file_date"),
                "source_id":      best.get("source_id"),
                "ownership_type": best.get("ownership_type"),
                "since":          best.get("since"),
            },
        )

        deleted_count = len(edges_sorted) - 1
        total_deleted += deleted_count
        cleaned.append({"owner_id": oid, "target_id": nid, "duplicates_removed": deleted_count})

    return {"duplicates_removed": total_deleted, "pairs_cleaned": len(pairs), "detail": cleaned}


def _migrate_person_edges(dead_id: str, keep_id: str) -> int:
    """Move all OWNS and HAS_ROLE edges from dead_id → keep_id, return count migrated."""
    migrated = 0

    # OWNS edges the dead node owns
    owns_out = run_query(
        """
        MATCH (p:Person {id: $pid})-[r:OWNS]->(t)
        RETURN t.id AS tid, r.stake_percent AS stake, r.file_date AS file_date,
               r.source_id AS source_id, r.ownership_type AS ownership_type,
               r.since AS since, r.until AS until
        """,
        {"pid": dead_id},
    )
    for e in owns_out:
        tid = e["tid"]
        # Skip if keep already has an active OWNS edge to the same target
        existing = run_query(
            "MATCH (p:Person {id: $pid})-[r:OWNS]->(t {id: $tid}) WHERE r.until IS NULL RETURN r LIMIT 1",
            {"pid": keep_id, "tid": tid},
        )
        if not existing:
            run_command(
                """
                MATCH (p:Person {id: $pid}), (t {id: $tid})
                CREATE (p)-[:OWNS {
                    stake_percent: $stake, file_date: $file_date,
                    source_id: $source_id, ownership_type: $otype,
                    since: $since, until: $until
                }]->(t)
                """,
                {"pid": keep_id, "tid": tid, "stake": e.get("stake"),
                 "file_date": e.get("file_date"), "source_id": e.get("source_id"),
                 "otype": e.get("ownership_type"), "since": e.get("since"),
                 "until": e.get("until")},
            )
            migrated += 1

    # HAS_ROLE edges
    roles = run_query(
        """
        MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(t)
        RETURN t.id AS tid, r.role AS role, r.since AS since, r.until AS until,
               r.source_id AS source_id
        """,
        {"pid": dead_id},
    )
    for e in roles:
        tid = e["tid"]
        existing = run_query(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(t {id: $tid})
            WHERE r.role = $role AND r.until IS NULL RETURN r LIMIT 1
            """,
            {"pid": keep_id, "tid": tid, "role": e.get("role")},
        )
        if not existing:
            run_command(
                """
                MATCH (p:Person {id: $pid}), (t {id: $tid})
                CREATE (p)-[:HAS_ROLE {
                    role: $role, since: $since, until: $until, source_id: $source_id
                }]->(t)
                """,
                {"pid": keep_id, "tid": tid, "role": e.get("role"),
                 "since": e.get("since"), "until": e.get("until"),
                 "source_id": e.get("source_id")},
            )
            migrated += 1

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


def _migrate_entity_edges(dead_id: str, keep_id: str) -> int:
    """Move every OWNS / HAS_ROLE / location edge off ``dead_id`` onto ``keep_id``.

    Covers all four ways an Entity is wired: OWNS it makes (outgoing), OWNS made
    *to* it (incoming, from a Person or Entity), HAS_ROLE held *in* it, and its
    HEADQUARTERED_IN / REGISTERED_IN / OPERATES_IN location links. An edge that
    ``keep`` already has (active, same target/role/location) is dropped rather
    than duplicated. Returns the number of edges migrated.
    """
    migrated = 0

    # 1. Outgoing OWNS  (dead)-[:OWNS]->(t)
    for e in run_query(
        """
        MATCH (a:Entity {id: $id})-[r:OWNS]->(t)
        RETURN t.id AS tid, r.stake_percent AS stake, r.ownership_type AS otype,
               r.voting_power_pct AS vpp, r.since AS since, r.until AS until,
               r.source_id AS source_id, r.credibility_score AS cred,
               r.source_url AS surl, r.source_date AS sdate, r.last_scraped_at AS lsa
        """,
        {"id": dead_id},
    ):
        if run_query(
            "MATCH (a:Entity {id: $k})-[r:OWNS]->(t {id: $tid}) WHERE r.until IS NULL RETURN r LIMIT 1",
            {"k": keep_id, "tid": e["tid"]},
        ):
            continue
        run_command(
            """
            MATCH (a:Entity {id: $k}), (t {id: $tid})
            CREATE (a)-[:OWNS {stake_percent: $stake, ownership_type: $otype,
                voting_power_pct: $vpp, since: $since, until: $until,
                source_id: $source_id, credibility_score: $cred,
                source_url: $surl, source_date: $sdate, last_scraped_at: $lsa}]->(t)
            """,
            {"k": keep_id, "tid": e["tid"], "stake": e.get("stake"), "otype": e.get("otype"),
             "vpp": e.get("vpp"), "since": e.get("since"), "until": e.get("until"),
             "source_id": e.get("source_id"), "cred": e.get("cred"), "surl": e.get("surl"),
             "sdate": e.get("sdate"), "lsa": e.get("lsa")},
        )
        migrated += 1

    # 2. Incoming OWNS  (s)-[:OWNS]->(dead)   — owner may be Person or Entity
    for e in run_query(
        """
        MATCH (s)-[r:OWNS]->(b:Entity {id: $id})
        RETURN s.id AS sid, r.stake_percent AS stake, r.ownership_type AS otype,
               r.voting_power_pct AS vpp, r.since AS since, r.until AS until,
               r.source_id AS source_id, r.credibility_score AS cred,
               r.source_url AS surl, r.source_date AS sdate, r.last_scraped_at AS lsa
        """,
        {"id": dead_id},
    ):
        if run_query(
            "MATCH (s {id: $sid})-[r:OWNS]->(b:Entity {id: $k}) WHERE r.until IS NULL RETURN r LIMIT 1",
            {"sid": e["sid"], "k": keep_id},
        ):
            continue
        run_command(
            """
            MATCH (s {id: $sid}), (b:Entity {id: $k})
            CREATE (s)-[:OWNS {stake_percent: $stake, ownership_type: $otype,
                voting_power_pct: $vpp, since: $since, until: $until,
                source_id: $source_id, credibility_score: $cred,
                source_url: $surl, source_date: $sdate, last_scraped_at: $lsa}]->(b)
            """,
            {"sid": e["sid"], "k": keep_id, "stake": e.get("stake"), "otype": e.get("otype"),
             "vpp": e.get("vpp"), "since": e.get("since"), "until": e.get("until"),
             "source_id": e.get("source_id"), "cred": e.get("cred"), "surl": e.get("surl"),
             "sdate": e.get("sdate"), "lsa": e.get("lsa")},
        )
        migrated += 1

    # 3. Incoming HAS_ROLE  (p:Person)-[:HAS_ROLE]->(dead)
    for e in run_query(
        """
        MATCH (p:Person)-[r:HAS_ROLE]->(b:Entity {id: $id})
        RETURN p.id AS pid, r.role AS role, r.since AS since, r.until AS until,
               r.source_id AS source_id, r.credibility_score AS cred,
               r.source_url AS surl, r.source_date AS sdate, r.last_scraped_at AS lsa
        """,
        {"id": dead_id},
    ):
        if run_query(
            "MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(b:Entity {id: $k}) "
            "WHERE r.role = $role AND r.until IS NULL RETURN r LIMIT 1",
            {"pid": e["pid"], "k": keep_id, "role": e.get("role")},
        ):
            continue
        run_command(
            """
            MATCH (p:Person {id: $pid}), (b:Entity {id: $k})
            CREATE (p)-[:HAS_ROLE {role: $role, since: $since, until: $until,
                source_id: $source_id, credibility_score: $cred,
                source_url: $surl, source_date: $sdate, last_scraped_at: $lsa}]->(b)
            """,
            {"pid": e["pid"], "k": keep_id, "role": e.get("role"), "since": e.get("since"),
             "until": e.get("until"), "source_id": e.get("source_id"), "cred": e.get("cred"),
             "surl": e.get("surl"), "sdate": e.get("sdate"), "lsa": e.get("lsa")},
        )
        migrated += 1

    # 4. Outgoing location links  (dead)-[:HEADQUARTERED_IN|REGISTERED_IN|OPERATES_IN]->(loc)
    for rel in ("HEADQUARTERED_IN", "REGISTERED_IN", "OPERATES_IN"):
        for e in run_query(
            f"MATCH (a:Entity {{id: $id}})-[:{rel}]->(l:Location) RETURN l.id AS lid",
            {"id": dead_id},
        ):
            if run_query(
                f"MATCH (a:Entity {{id: $k}})-[:{rel}]->(l:Location {{id: $lid}}) RETURN 1 LIMIT 1",
                {"k": keep_id, "lid": e["lid"]},
            ):
                continue
            run_command(
                f"MATCH (a:Entity {{id: $k}}), (l:Location {{id: $lid}}) CREATE (a)-[:{rel}]->(l)",
                {"k": keep_id, "lid": e["lid"]},
            )
            migrated += 1

    return migrated


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


def deduplicate_entities(limit: int | None = 300) -> dict:
    """
    Merge Entity nodes that share a stable external identifier — the same LEI or
    the same Companies House number — into one, migrating their edges and deleting
    the extras. Heals duplicates left by the older BODS importer, which keyed
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
    # All duplicate groups across both identifier kinds (cheap aggregation).
    dup_keys = [("lei_id", k) for k in _duplicate_keys("lei_id")]
    dup_keys += [("companies_house_id", k) for k in _duplicate_keys("companies_house_id")]
    total = len(dup_keys)
    batch = dup_keys if limit is None else dup_keys[:limit]

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
            migrated = _migrate_entity_edges(dead["id"], keep["id"])
            run_command("MATCH (e:Entity {id: $id}) DETACH DELETE e", {"id": dead["id"]})
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


def migrate_ownership_types() -> dict:
    """
    One-time migration: derive canonical ownership_type values for all OWNS
    edges using stake_percent and the old 'passive'/'active' markers.

    Rules applied in order (first matching rule wins per edge):
      stake >= 99                          → full
      stake > 50                           → majority
      stake >= 20                          → controlling
      stake > 0                            → minority
      no stake, old type = 'active'        → controlling
      no stake, old type = 'passive'       → minority
      no stake, no type (Wikidata sub)     → majority
    """
    edges = run_query(
        """
        MATCH (a)-[r:OWNS]->(b)
        RETURN a.id AS owner_id, b.id AS target_id,
               r.stake_percent  AS stake,
               r.ownership_type AS old_type,
               r.since          AS since,
               r.until          AS until,
               r.file_date      AS file_date,
               r.source_id      AS source_id,
               r.credibility_score AS cred,
               r.voting_power_pct  AS voting_pct
        """
    )

    updated = 0
    skipped = 0
    detail: list[dict] = []

    for e in edges:
        old_type = e.get("old_type")
        stake    = e.get("stake")

        # Derive from stake % when available, else fall back on old marker
        form_hint = None
        if old_type == "active":
            form_hint = "SC 13D"
        elif old_type == "passive":
            form_hint = "SC 13G"

        new_type = _derive_ownership_type(stake, form_hint)

        if new_type == old_type:
            skipped += 1
            continue

        oid = e["owner_id"]
        nid = e["target_id"]

        run_command(
            "MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid}) WHERE r.until = $until DELETE r",
            {"oid": oid, "nid": nid, "until": e.get("until")},
        )
        run_command(
            """
            MATCH (a {id: $oid}), (b {id: $nid})
            CREATE (a)-[:OWNS {
                stake_percent:      $stake,
                ownership_type:     $otype,
                since:              $since,
                until:              $until,
                file_date:          $file_date,
                source_id:          $source_id,
                credibility_score:  $cred,
                voting_power_pct:   $voting_pct
            }]->(b)
            """,
            {
                "oid":       oid,
                "nid":       nid,
                "stake":     stake,
                "otype":     new_type,
                "since":     e.get("since"),
                "until":     e.get("until"),
                "file_date": e.get("file_date"),
                "source_id": e.get("source_id"),
                "cred":      e.get("cred"),
                "voting_pct": e.get("voting_pct"),
            },
        )
        updated += 1
        detail.append({"owner_id": oid, "target_id": nid,
                        "old": old_type, "new": new_type, "stake": stake})

    return {
        "status":  "ok",
        "updated": updated,
        "skipped": skipped,
        "detail":  detail,
    }

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
    from app.scraper.bods import _ISO2_COUNTRY
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
