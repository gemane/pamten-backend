"""
GLEIF incremental (delta) update — a retirement-aware daily refresh.

The full golden-copy import (`gleif_lei_cdf` / `gleif_rr` / `gleif_succession`,
run via `full-import.sh --bulk-load`) loads ~3.4M records. GLEIF also publishes
**delta files** — only the records changed since the last publish (LastDay ≈ 14k
entities + ~2k relationships) — so a daily update is seconds/minutes, not hours.

The delta files are the same JSON format as the full copy, so we reuse the full
importers' parsers. Two things differ from a bulk load:

  * **Idempotent writes.** A delta may be re-applied (retries, catch-up), and the
    bulk `CREATE EDGE` isn't idempotent, so edges here are *upserted* (create if
    absent, else refresh) and nodes UPSERT by id. No `--bulk-load` (indexes stay
    live) and no whole-DB dedup. Node upserts are batched (`_BatchWriter`); the
    far-fewer edges are per-record Cypher (`MATCH (a)-[r]->(b)` — ArcadeDB SQL
    can't filter an edge by `out.id`/`in.id`, so Cypher is used for edges).
  * **Retirements.** GLEIF never deletes; retirement is a status change *inside*
    the delta record. A relationship that goes non-ACTIVE closes its OWNS edge
    (`until` = the relationship period's end date); an entity whose `EntityStatus`
    is INACTIVE is marked (`gleif_entity_status` + `active=false`), never deleted.
    Merges keep flowing through succession (SuccessorLEI).
"""
import logging
import os
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime
from typing import IO

import httpx
import ijson

from app.db.arcadedb import run_command, run_sql
from app.claims import KIND_OWNS, record_claim
from app.scraper.bulk_import import _BatchWriter, _now_iso, _ProgressBar, _ProgressStream
from app.scraper.gleif_lei_cdf import _entity_props
from app.scraper.gleif_rr import _CONSOLIDATION, _node_lei, _relationship_dates
from app.scraper.gleif_succession import _iter_lei_records, _pairs_from_record, _v

log = logging.getLogger(__name__)

_PUBLISHES_API = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes"

# Checkpoint key for the last GLEIF publish a delta update applied (ImportState).
_STATE_KEY = "gleif-update"
# Marker key stamped by the full LEI-CDF load — the delta update's precondition.
_FULL_LOAD_KEY = "gleif-full-load"
# GLEIF publishes the `publish_date` as "YYYY-MM-DD HH:MM:SS".
_PUBLISH_FMT = "%Y-%m-%d %H:%M:%S"
# Delta windows GLEIF offers, and the max gap (days) each safely covers. A wider
# window is always a superset (idempotent), so on a missed run we escalate to the
# smallest window that still spans the gap; past LastMonth a delta can't cover it
# → the caller must full-reload. Buffers sit under each nominal window (1/7/30).
_INTERVAL_MAX_GAP_DAYS = (("LastDay", 1.5), ("LastWeek", 6.5), ("LastMonth", 29.0))


# ── delta-record field extraction (reuses the full importers' _v/parsers) ─────

def _registration_status(rec: dict) -> str | None:
    return _v((rec.get("Registration") or {}).get("RegistrationStatus"))


def _entity_status(rec: dict) -> str | None:
    return _v((rec.get("Entity") or {}).get("EntityStatus"))


def _relationship_end_date(rel: dict) -> str | None:
    """End date of the RELATIONSHIP_PERIOD, if the relationship has ended.
    `RelationshipPeriods.RelationshipPeriod` is an object *or* a list."""
    periods = (rel.get("RelationshipPeriods") or {}).get("RelationshipPeriod")
    if isinstance(periods, dict):
        periods = [periods]
    for p in periods or []:
        if _v((p or {}).get("PeriodType")) == "RELATIONSHIP_PERIOD":
            return _v(p.get("EndDate"))
    return None


def _rr_delta_relationship(rec: dict) -> tuple[str, str, str, str | None, dict] | None:
    """(parent_lei, child_lei, direct_or_indirect, status, rel) for a *consolidation*
    relationship record, else None (non-consolidation types are ignored)."""
    rel = (rec.get("RelationshipRecord") or {}).get("Relationship") or {}
    marker = _CONSOLIDATION.get(_v(rel.get("RelationshipType")))
    if not marker:
        return None
    child = _node_lei(rel.get("StartNode"))
    parent = _node_lei(rel.get("EndNode"))
    if not child or not parent or child == parent:
        return None
    return parent, child, marker, _v(rel.get("RelationshipStatus")), rel


# ── idempotent edge primitives (Cypher; nodes must already exist) ─────────────

def _ensure_lei_node(lei: str, source_id: str) -> None:
    """Ensure a `lei:{LEI}` Entity exists (non-clobbering — never touches name/type,
    which the entity importer owns)."""
    run_sql("UPDATE Entity SET lei_id = :lei, source_id = :src UPSERT WHERE id = :id",
            {"lei": lei, "src": source_id, "id": f"lei:{lei}"})


def _existing_consolidation_edge(parent_id: str, child_id: str) -> dict | None:
    """The RR-authored OWNS edge for this pair, whatever marker it carries.

    Matched on the **pair**, not on the marker. The full importer folds a pair
    stated both ways into a single edge (see ``gleif_rr._collapse``), so the edge
    holding an ultimate relationship is usually marked ``direct``. Looking it up by
    marker would miss it and create a second, parallel edge — re-introducing the
    duplicates the fold removed, a few thousand per delta.

    Scoped to edges that carry a ``direct_or_indirect`` marker at all, which is how
    this module has always identified its own edges (only RR sets one), so a BODS
    or SEC edge for the same pair is never touched.
    """
    rows = run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
        "WHERE r.direct_or_indirect IS NOT NULL "
        "RETURN r.direct_or_indirect AS marker, r.since AS since LIMIT 1",
        {"p": parent_id, "c": child_id})
    return rows[0] if rows else None


def _owns_edge_upsert(parent_id: str, child_id: str, child_lei: str, marker: str,
                      source_id: str, credibility_score: int, since: str | None = None) -> str:
    """Create the (parent)-[:OWNS {marker}]->(child) edge if absent, else refresh it
    and clear any stale `until`. Assumes both nodes already exist.
    'created'|'updated'|'folded'. `since` (relationship start date) is set on create
    and backfilled on update without clobbering an existing value.

    'folded' means the delta stated the *other* relationship type for a pair we
    already hold: one edge per pair is the invariant the full import establishes, so
    the second assertion is recorded **on** the existing edge rather than beside it.
    """
    now = _now_iso()
    # Every path below asserts the same fact — this parent consolidates this
    # child — so the claim is recorded once, up front. Delta edges previously
    # carried no claim at all, which meant a GLEIF-confirmed pair could not
    # vouch for anything in mark_stale_ownership's register-backed set, and the
    # corroboration badge never counted GLEIF's agreement.
    record_claim(kind=KIND_OWNS, from_id=parent_id, to_id=child_id,
                 source_id=source_id, ownership_type="controlling", since=since,
                 source_url=f"https://search.gleif.org/#/record/{child_lei}",
                 credibility_score=credibility_score)
    existing = _existing_consolidation_edge(parent_id, child_id)

    if existing is None:
        run_command(
            "MATCH (a:Entity {id:$p}) MATCH (b:Entity {id:$c}) "
            "CREATE (a)-[:OWNS {direct_or_indirect:$m, ownership_type:'controlling', "
            "interest_types:$it, source_id:$src, credibility_score:$cred, "
            "source_url:$url, since:$since, last_scraped_at:$now}]->(b)",
            {"p": parent_id, "c": child_id, "m": marker, "it": ["accountingConsolidation"],
             "src": source_id, "cred": credibility_score, "since": since,
             "url": f"https://search.gleif.org/#/record/{child_lei}", "now": now})
        return "created"

    if existing["marker"] == marker:
        run_command(
            "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
            "WHERE r.direct_or_indirect = $m "
            "SET r.until = null, r.last_scraped_at = $now, r.credibility_score = $cred, "
            "r.since = coalesce(r.since, $since)",
            {"p": parent_id, "c": child_id, "m": marker, "now": now,
             "cred": credibility_score, "since": since})
        return "updated"

    # Stated both ways. The direct claim is the more specific one and owns the
    # edge; the ultimate one is recorded alongside, with its own period whenever
    # that differs — the same shape the full importer's fold produces.
    if marker == "indirect":
        kept_since, other_since = existing["since"], since
    else:
        kept_since, other_since = since or existing["since"], existing["since"]
    run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
        "WHERE r.direct_or_indirect IS NOT NULL "
        "SET r.direct_or_indirect = 'direct', r.also_ultimate = true, r.until = null, "
        "r.since = $since, r.ultimate_since = $ult, "
        "r.last_scraped_at = $now, r.credibility_score = $cred",
        {"p": parent_id, "c": child_id, "since": kept_since, "now": now,
         "cred": credibility_score,
         "ult": other_since if other_since and other_since != kept_since else None})
    return "folded"


def _upsert_owns(parent_lei: str, child_lei: str, marker: str,
                 source_id: str, credibility_score: int, since: str | None = None) -> str:
    """Node-ensuring convenience wrapper (standalone use / tests)."""
    _ensure_lei_node(parent_lei, source_id)
    _ensure_lei_node(child_lei, source_id)
    return _owns_edge_upsert(f"lei:{parent_lei}", f"lei:{child_lei}", child_lei,
                             marker, source_id, credibility_score, since)


def _close_owns(parent_lei: str, child_lei: str, marker: str, until: str) -> int:
    """Close a retired relationship's OWNS edge by stamping `until`. Returns the
    number of edges closed (0 if the edge isn't in our graph).

    A **folded** edge carries two relationships, so retiring one of them must not
    close the edge — the other still stands:

    * the ultimate relationship ends → the edge stays a live direct holding, and
      just stops claiming the parent is the top of the tree;
    * the direct relationship ends → the edge stays live as the ultimate one, with
      that relationship's own period restored as its `since`.

    Stamping `until` in either case would delete a holding GLEIF still asserts.
    """
    pid, cid = f"lei:{parent_lei}", f"lei:{child_lei}"
    rows = run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
        "WHERE r.also_ultimate = true AND r.until IS NULL "
        "RETURN r.ultimate_since AS ult LIMIT 1", {"p": pid, "c": cid})
    if rows:
        if marker == "indirect":
            run_command(
                "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
                "WHERE r.also_ultimate = true "
                "SET r.also_ultimate = null, r.ultimate_since = null, r.ultimate_until = null",
                {"p": pid, "c": cid})
        else:
            run_command(
                "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
                "WHERE r.also_ultimate = true "
                "SET r.direct_or_indirect = 'indirect', r.also_ultimate = null, "
                "r.since = coalesce($ult, r.since), r.ultimate_since = null",
                {"p": pid, "c": cid, "ult": rows[0].get("ult")})
        return 1

    rows = run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
        "WHERE r.direct_or_indirect = $m SET r.until = $until RETURN count(r) AS n",
        {"p": pid, "c": cid, "m": marker, "until": until})
    return int(rows[0]["n"]) if rows else 0


def _succeeded_by_edge(pred_id: str, succ_id: str, source_id: str) -> str:
    """Create (pred)-[:SUCCEEDED_BY]->(succ) if absent. Assumes nodes exist."""
    rows = run_command(
        "MATCH (a:Entity {id:$p})-[r:SUCCEEDED_BY]->(b:Entity {id:$s}) RETURN r LIMIT 1",
        {"p": pred_id, "s": succ_id})
    if rows:
        return "exists"
    run_command(
        "MATCH (a:Entity {id:$p}) MATCH (b:Entity {id:$s}) "
        "CREATE (a)-[:SUCCEEDED_BY {source_id:$src, last_scraped_at:$now}]->(b)",
        {"p": pred_id, "s": succ_id, "src": source_id, "now": _now_iso()})
    return "created"


def _upsert_succeeded_by(pred_lei: str, succ_lei: str, source_id: str) -> str:
    """Node-ensuring convenience wrapper (standalone use / tests)."""
    _ensure_lei_node(pred_lei, source_id)
    _ensure_lei_node(succ_lei, source_id)
    return _succeeded_by_edge(f"lei:{pred_lei}", f"lei:{succ_lei}", source_id)


# ── delta importers ───────────────────────────────────────────────────────────

def _open_json(filepath: str) -> tuple[IO[bytes], int]:
    if filepath.lower().endswith(".zip"):
        zf = zipfile.ZipFile(filepath)
        entry = next(n for n in zf.namelist() if n.lower().endswith(".json"))
        return zf.open(entry), zf.getinfo(entry).file_size
    return open(filepath, "rb"), os.path.getsize(filepath)  # noqa: WPS515


_LEI_ID_PAGE = 20000


def existing_lei_ids() -> set[str]:
    """Every ``lei:`` Entity id currently in the graph — the guest list for
    ``only_existing``.

    Loaded once per delta run and held in memory, because the alternative is an
    existence query per record and a delta carries hundreds of thousands. Paged by
    @rid for the usual reason: an unpaged select over a full database's millions of
    entities blows the query heap.
    """
    ids: set[str] = set()
    last: str | None = None
    while True:
        where = "WHERE id LIKE 'lei:%'" + (f" AND @rid > {last}" if last else "")
        rows = run_sql(f"SELECT @rid AS rid, id FROM Entity {where} "
                       f"ORDER BY @rid LIMIT {_LEI_ID_PAGE}")
        if not rows:
            break
        ids.update(r["id"] for r in rows)
        last = rows[-1]["rid"]
        if len(rows) < _LEI_ID_PAGE:
            break
    return ids


def import_lei_cdf_delta(filepath: str, source_id: str, credibility_score: int,
                         limit: int | None = None, only_existing: bool = False) -> dict:
    """Apply an LEI-CDF delta: upsert entity props (batched), mark dissolved
    (INACTIVE) entities, and add succession (merge) edges. Idempotent.

    ``only_existing`` refreshes the companies this database already holds and
    ignores the rest. A delta describes every change GLEIF made worldwide, so
    without it a curated subset does not get refreshed — it gets buried.
    """
    raw, total = _open_json(filepath)
    counts = {"records": 0, "updated": 0, "marked_inactive": 0, "succession": 0,
              "not_here": 0, "errors": 0}
    known = existing_lei_ids() if only_existing else None
    batch = _BatchWriter()
    succession: list[tuple[str, str]] = []
    bar = _ProgressBar("LEI-CDF delta")
    try:
        for rec in _iter_lei_records(_ProgressStream(raw, total, bar)):
            if limit and counts["records"] >= limit:
                break
            counts["records"] += 1
            try:
                built = _entity_props(rec, source_id, credibility_score)
                if built and known is not None and built[0] not in known:
                    counts["not_here"] += 1
                    continue          # a company this database does not carry
                if built:
                    node_id, props = built
                    props["gleif_registration_status"] = _registration_status(rec) or ""
                    inactive = _entity_status(rec) == "INACTIVE"
                    props["active"] = not inactive
                    if inactive:
                        counts["marked_inactive"] += 1
                    batch.entity(node_id, props)
                    counts["updated"] += 1
                succession.extend(_pairs_from_record(rec))
            except Exception as exc:  # noqa: BLE001 - one bad record mustn't abort
                counts["errors"] += 1
                if counts["errors"] <= 5:
                    log.warning("LEI-CDF delta record error: %s", exc)
        batch.flush()                      # entities upserted before succession edges
        for pred, succ in succession:
            # A succession edge needs both ends; in only_existing mode the successor
            # is often a company this database does not carry, and creating it would
            # be the same leak by another route. `known` holds node ids, these are
            # bare LEIs.
            if known is not None and not (f"lei:{pred}" in known and f"lei:{succ}" in known):
                counts["not_here"] += 1
                continue
            if _upsert_succeeded_by(pred, succ, source_id) == "created":
                counts["succession"] += 1
        bar.finish(f"{counts['records']:,} records, {counts['updated']:,} upserted, "
                   f"{counts['succession']:,} successions"
                   + (f", {counts['not_here']:,} not in this database" if known is not None else ""))
    finally:
        raw.close()
    log.info("LEI-CDF delta done: %s", counts)
    return counts


def import_rr_delta(filepath: str, source_id: str, credibility_score: int,
                    limit: int | None = None, only_existing: bool = False) -> dict:
    """Apply an RR-CDF delta: upsert ACTIVE consolidation edges, close ones that
    went non-ACTIVE. Endpoint nodes are batch-upserted first. Idempotent.

    ``only_existing`` keeps a relationship only when **both** endpoints are already
    in the graph. This importer creates its endpoint nodes, so it is the bigger of
    the two leaks: a relationship between two companies the database has never
    heard of would otherwise drag both of them in.
    """
    raw, total = _open_json(filepath)
    counts = {"records": 0, "created": 0, "updated": 0, "folded": 0,
              "closed": 0, "skipped": 0, "not_here": 0, "errors": 0}
    known = existing_lei_ids() if only_existing else None
    batch = _BatchWriter()
    active: list[tuple[str, str, str, str | None]] = []
    closures: list[tuple[str, str, str, str]] = []
    bar = _ProgressBar("RR delta")
    try:
        for rec in ijson.items(_ProgressStream(raw, total, bar), "relations.item"):
            if limit and counts["records"] >= limit:
                break
            counts["records"] += 1
            try:
                parsed = _rr_delta_relationship(rec)
                if not parsed:
                    counts["skipped"] += 1
                    continue
                parent, child, marker, status, rel = parsed
                if known is not None and not (
                        f"lei:{parent}" in known and f"lei:{child}" in known):
                    counts["not_here"] += 1
                    continue
                batch.entity(f"lei:{parent}", {"lei_id": parent, "source_id": source_id})
                batch.entity(f"lei:{child}", {"lei_id": child, "source_id": source_id})
                if status == "ACTIVE":
                    active.append((parent, child, marker, _relationship_dates(rel)[0]))
                elif status == "INACTIVE":
                    closures.append((parent, child, marker, _relationship_end_date(rel) or _now_iso()))
                else:
                    counts["skipped"] += 1   # NULL / unknown — leave as-is
            except Exception as exc:  # noqa: BLE001
                counts["errors"] += 1
                if counts["errors"] <= 5:
                    log.warning("RR delta record error: %s", exc)
        batch.flush()                      # endpoint nodes exist before edge ops
        for parent, child, marker, since in active:
            outcome = _owns_edge_upsert(f"lei:{parent}", f"lei:{child}", child, marker,
                                        source_id, credibility_score, since)
            counts[outcome if outcome in counts else "updated"] += 1
        for parent, child, marker, until in closures:
            counts["closed"] += _close_owns(parent, child, marker, until)
        bar.finish(f"{counts['records']:,} records, +{counts['created']:,} edges, "
                   f"{counts['folded']:,} folded, {counts['closed']:,} closed"
                   + (f", {counts['not_here']:,} not in this database" if known is not None else ""))
    finally:
        raw.close()
    log.info("RR delta done: %s", counts)
    return counts


# ── delta fetch (GLEIF golden-copy API) ───────────────────────────────────────

def fetch_publish_metadata() -> dict:
    """The latest GLEIF golden-copy publish record (its `publish_date` + the
    `full_file`/`delta_files` URLs for each section)."""
    data = httpx.get(_PUBLISHES_API, params={"per_page": 1}, timeout=60).json()
    return data["data"][0] if "data" in data else data


# The golden copy's three sections: entities, relationships, and the reasons a
# company gives for reporting no parent. `repex` is optional on download — an
# older publish record without it must not stop the entity and relationship
# refresh, which is the part the graph cannot be correct without.
_DELTA_SECTIONS = ("lei2", "rr", "repex")


def download_deltas(publish: dict, interval: str, dest_dir: str | None = None) -> dict:
    """Download the LEI-CDF + RR + repex delta .json.zip files for `interval` from
    a publish record. Returns {'lei2': path, 'rr': path, 'repex': path}.

    Without `dest_dir` this leaves a temp directory behind for the caller to deal
    with — see `downloaded_deltas`, which is what the nightly update uses and what
    anything running repeatedly should use.
    """
    dest = dest_dir or tempfile.mkdtemp(prefix="gleif-delta-")
    out: dict = {}
    for section in _DELTA_SECTIONS:
        try:
            url = publish[section]["delta_files"][interval]["json"]["url"]
        except (KeyError, TypeError):
            if section == "repex":
                log.warning("GLEIF publish has no repex %s delta — skipping it", interval)
                continue
            raise
        path = os.path.join(dest, os.path.basename(url))
        with httpx.stream("GET", url, timeout=300) as resp:
            resp.raise_for_status()
            with open(path, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
        out[section] = path
        log.info("GLEIF %s %s delta: %s", section, interval, path)
    return out


@contextmanager
def downloaded_deltas(publish: dict, interval: str):
    """The delta files for `interval`, cleaned up afterwards.

    `download_deltas` alone leaves its temp directory behind, and the nightly cron
    called it directly: one `gleif-delta-*` directory per run, for months, never
    removed. Thirteen of them and 135 MB had accumulated in /tmp by the time anyone
    looked — slow enough to go unnoticed and unbounded, which is the worst shape a
    leak can have. `/tmp` here survives reboots.

    A context manager rather than a `finally` at the call site, because the call
    site is where it was forgotten. Cleanup runs on failure too: the URLs come from
    a dated publish record, so a failed run re-fetches exactly the same bytes and
    keeping them buys nothing.
    """
    dest = tempfile.mkdtemp(prefix="gleif-delta-")
    try:
        yield download_deltas(publish, interval, dest_dir=dest)
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def fetch_gleif_deltas(interval: str = "LastDay", dest_dir: str | None = None) -> dict:
    """Fetch + download the current delta files for a fixed `interval`
    (IntraDay | LastDay | LastWeek | LastMonth)."""
    return download_deltas(fetch_publish_metadata(), interval, dest_dir)


# ── full-load precondition ────────────────────────────────────────────────────
# The delta update rides *on top of* the full golden copy — applying a delta to a
# DB without that baseline would build a partial, wrong graph (only the recently
# changed records, no foundation). The full LEI-CDF importer stamps this marker on
# success; the update refuses to run without it, and `wipe-source --source GLEIF`
# (or dropping the database) clears it so a fresh full load is required before deltas resume.
#
# A **subset** load must not stamp it. A delta carries every record GLEIF changed
# anywhere in the world, so applying one to a curated test database does not
# refresh it — it imports the rest of the world into it. One night's delta added
# 226,902 entity records and 18,720 edges to a 488-entity test subset, because the
# subset import had stamped this marker exactly as a full load does.

def mark_full_load_done(scope: str = "full") -> None:
    """Record that a GLEIF LEI-CDF load has established the entity baseline.

    `scope` is "full" for a complete golden-copy pass and "subset" for anything
    narrowed by --only-file / --limit / --jurisdiction. Only "full" satisfies the
    delta update; a subset is recorded so the refusal can say *why* rather than
    claim GLEIF was never loaded.
    """
    run_sql(
        "UPDATE ImportState SET key = :k, last_run_at = :now, scope = :scope "
        "UPSERT WHERE key = :k",
        {"k": _FULL_LOAD_KEY, "now": _now_iso(), "scope": scope})


def load_scope() -> str | None:
    """"full", "subset", or None when no GLEIF entity load has run at all.

    Rows written before `scope` existed have none; they came from the importer
    when it stamped unconditionally, so they cannot be trusted to be full and are
    reported as "subset" — the conservative direction, since guessing "full" is
    what lets a delta flood a curated database.
    """
    rows = run_command(
        "MATCH (s:ImportState {key:$k}) RETURN s.last_run_at AS t, s.scope AS scope",
        {"k": _FULL_LOAD_KEY})
    if not (rows and rows[0].get("t")):
        return None
    return rows[0].get("scope") or "subset"


def full_load_present() -> bool:
    """True once a *full* LEI-CDF load has run (the delta update's precondition)."""
    return load_scope() == "full"


# ── gap-aware catch-up (pick a window that covers any missed runs) ────────────

def read_last_publish() -> str | None:
    """The `publish_date` of the last GLEIF publish a delta update applied, or None
    (never run — e.g. right after the initial full load)."""
    rows = run_command(
        "MATCH (s:ImportState {key:$k}) RETURN s.last_publish_date AS d", {"k": _STATE_KEY})
    return rows[0]["d"] if rows and rows[0].get("d") else None


def write_last_publish(publish_date: str) -> None:
    """Checkpoint the publish just applied (idempotent UPSERT on the key)."""
    run_sql(
        "UPDATE ImportState SET key = :k, last_publish_date = :d, last_run_at = :now "
        "UPSERT WHERE key = :k",
        {"k": _STATE_KEY, "d": publish_date, "now": _now_iso()})


def choose_catchup_interval(last_publish: str | None, current_publish: str) -> str | None:
    """Smallest delta window covering the gap since `last_publish`. None means the
    gap is too wide for any delta (> ~30 days) → the caller should full-reload.

    Cold start (no checkpoint — e.g. the first run after the full load) uses
    LastMonth: the widest delta, so it reconciles up to a month of drift since the
    load rather than assuming the graph is current."""
    if not last_publish:
        return "LastMonth"
    try:
        gap_days = (datetime.strptime(current_publish.strip(), _PUBLISH_FMT)
                    - datetime.strptime(last_publish.strip(), _PUBLISH_FMT)).total_seconds() / 86400
    except (ValueError, AttributeError):
        return "LastMonth"          # unparseable checkpoint → be safe, go wide
    for interval, max_gap in _INTERVAL_MAX_GAP_DAYS:
        if gap_days <= max_gap:
            return interval
    return None
