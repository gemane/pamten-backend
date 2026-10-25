"""One Source node per source, keyed by a stable id — the heal for a database
written before 2026-10-24 (see app.scraper.sources).

Until then a source had two nodes: a `Source` with a random uuid, minted
per database, that every entity, edge, claim, stake-history row and industry
statement carried as `source_id`; and a `ScraperSource`, keyed by the
catalogue slug, that held the toggle and the data mode. The uuid made a
claim's provenance meaningless outside the database that wrote it, and two
nodes for one source meant two lookups for one question.

`heal` re-keys every Source node to its stable id (`sources.source_id_for`),
re-points every holder of the old id, moves the switches from the
ScraperSource row onto the Source node and deletes the ScraperSource rows,
then re-keys the claims — a claim's key hashes its source id. The holder
updates are whole-type scans on an unindexed column (Entity.source_id,
OWNS.source_id); on a full-size database that is minutes per source and must
run straight against ArcadeDB (`--db-url`), not through the proxy's 60 s.
"""
from __future__ import annotations

import logging

from app.db.arcadedb import run_sql

log = logging.getLogger(__name__)

#: Every record type that stores a Source node's id.
HOLDERS: tuple[str, ...] = ("Entity", "Person", "VotingGroup", "OWNS", "HAS_ROLE", "RELATED_TO",
                            "MEMBER_OF", "SUCCEEDED_BY", "DUAL_LISTED_WITH", "Claim", "StakeHistory",
                            "IndustryCode")
_SCAN_TIMEOUT = 6 * 3600


def _updated(rows) -> int:
    return sum(int(r.get("count") or 0) for r in rows or [] if isinstance(r, dict))


def plan() -> list[dict]:
    """What the heal would do: one entry per Source node whose id is not its
    stable one — {old, new, name, key} — in catalogue order, peers after."""
    from app.scraper.sources import source_id_for, source_key_for
    out = []
    for r in run_sql("SELECT id, name, type FROM Source"):
        old, name = r.get("id"), r.get("name")
        if not old or not name:
            continue
        new = source_id_for(name, r.get("type"))
        if new != old:
            out.append({"old": old, "new": new, "name": name, "key": source_key_for(name)})
    return out


def heal(apply: bool = True) -> dict:
    """Re-key the Source nodes, re-point their holders, fold the toggles in,
    re-key the claims. Dry run plans and counts the holders, writes nothing."""
    from app.claims import heal_claim_keys
    from app.scraper.sources import KNOWN_SOURCES, SOURCE_ID
    counts: dict = {"sources": 0, "holders": {}, "toggles_moved": 0, "scraper_rows_deleted": 0,
                    "conflicts": [], "applied": bool(apply)}
    taken = {r["id"] for r in run_sql("SELECT id FROM Source")}
    for step in plan():
        old, new = step["old"], step["new"]
        if new in taken:
            # two nodes would collapse into one — not this heal's call
            counts["conflicts"].append(step)
            log.warning("source %s: id %s already taken by another node — left as is", old, new)
            continue
        counts["sources"] += 1
        for holder in HOLDERS:
            if apply:
                n = _updated(run_sql(f"UPDATE {holder} SET source_id = :new WHERE source_id = :old",
                                     {"new": new, "old": old}, timeout=_SCAN_TIMEOUT))
            else:
                rows = run_sql(f"SELECT count(*) AS n FROM {holder} WHERE source_id = :old",
                               {"old": old}, timeout=_SCAN_TIMEOUT)
                n = int((rows[0].get("n") if rows else 0) or 0)
            if n:
                counts["holders"][holder] = counts["holders"].get(holder, 0) + n
        if apply:
            run_sql("UPDATE Source SET id = :new, registry_key = :key WHERE id = :old",
                    {"new": new, "old": old, "key": step["key"]})
            taken.add(new)
    # the switches: from the ScraperSource row onto the Source node, then no row
    try:
        rows = run_sql("SELECT name, enabled, data_mode, kind, description FROM ScraperSource")
    except Exception:  # noqa: BLE001 - the type is gone on a database born after the fold
        rows = []
    for r in rows:
        key = r.get("name")
        if key not in KNOWN_SOURCES:
            continue
        counts["toggles_moved"] += 1
        if apply:
            run_sql("UPDATE Source SET enabled = :e, data_mode = COALESCE(:m, data_mode), "
                    "kind = :k, registry_key = :key WHERE id = :id",
                    {"e": bool(r.get("enabled", True)), "m": r.get("data_mode"),
                     "k": r.get("kind") or KNOWN_SOURCES[key]["kind"], "key": key, "id": SOURCE_ID[key]})
    if rows and apply:
        counts["scraper_rows_deleted"] = _updated(run_sql("DELETE FROM ScraperSource"))
        try:
            run_sql("DROP TYPE ScraperSource IF EXISTS")
        except Exception as exc:  # noqa: BLE001 - best effort; an empty type harms nothing
            log.warning("could not drop ScraperSource: %s", exc)
    # a claim's key hashes its source id: every re-pointed claim needs its key back in step
    if counts["sources"]:
        counts["claims"] = heal_claim_keys(dry_run=not apply)
    log.info("source ids heal: %s", counts)
    return counts
