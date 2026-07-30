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
import tempfile
import zipfile
from datetime import datetime
from typing import IO

import httpx
import ijson

from app.db.arcadedb import run_command, run_sql
from app.scraper.bulk_import import _BatchWriter, _now_iso, _ProgressBar, _ProgressStream
from app.scraper.gleif_lei_cdf import _entity_props
from app.scraper.gleif_rr import _CONSOLIDATION, _node_lei
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


def _owns_edge_upsert(parent_id: str, child_id: str, child_lei: str, marker: str,
                      source_id: str, credibility_score: int) -> str:
    """Create the (parent)-[:OWNS {marker}]->(child) edge if absent, else refresh it
    and clear any stale `until`. Assumes both nodes already exist. 'created'|'updated'."""
    now = _now_iso()
    exists = run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
        "WHERE r.direct_or_indirect = $m RETURN r LIMIT 1",
        {"p": parent_id, "c": child_id, "m": marker})
    if exists:
        run_command(
            "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
            "WHERE r.direct_or_indirect = $m "
            "SET r.until = null, r.last_scraped_at = $now, r.credibility_score = $cred",
            {"p": parent_id, "c": child_id, "m": marker, "now": now, "cred": credibility_score})
        return "updated"
    run_command(
        "MATCH (a:Entity {id:$p}) MATCH (b:Entity {id:$c}) "
        "CREATE (a)-[:OWNS {direct_or_indirect:$m, ownership_type:'controlling', "
        "interest_types:$it, source_id:$src, credibility_score:$cred, "
        "source_url:$url, last_scraped_at:$now}]->(b)",
        {"p": parent_id, "c": child_id, "m": marker, "it": ["accountingConsolidation"],
         "src": source_id, "cred": credibility_score,
         "url": f"https://search.gleif.org/#/record/{child_lei}", "now": now})
    return "created"


def _upsert_owns(parent_lei: str, child_lei: str, marker: str,
                 source_id: str, credibility_score: int) -> str:
    """Node-ensuring convenience wrapper (standalone use / tests)."""
    _ensure_lei_node(parent_lei, source_id)
    _ensure_lei_node(child_lei, source_id)
    return _owns_edge_upsert(f"lei:{parent_lei}", f"lei:{child_lei}", child_lei,
                             marker, source_id, credibility_score)


def _close_owns(parent_lei: str, child_lei: str, marker: str, until: str) -> int:
    """Close a retired relationship's OWNS edge by stamping `until`. Returns the
    number of edges closed (0 if the edge isn't in our graph)."""
    rows = run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
        "WHERE r.direct_or_indirect = $m SET r.until = $until RETURN count(r) AS n",
        {"p": f"lei:{parent_lei}", "c": f"lei:{child_lei}", "m": marker, "until": until})
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


def import_lei_cdf_delta(filepath: str, source_id: str, credibility_score: int,
                         limit: int | None = None) -> dict:
    """Apply an LEI-CDF delta: upsert entity props (batched), mark dissolved
    (INACTIVE) entities, and add succession (merge) edges. Idempotent."""
    raw, total = _open_json(filepath)
    counts = {"records": 0, "updated": 0, "marked_inactive": 0, "succession": 0, "errors": 0}
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
            if _upsert_succeeded_by(pred, succ, source_id) == "created":
                counts["succession"] += 1
        bar.finish(f"{counts['records']:,} records, {counts['updated']:,} upserted, "
                   f"{counts['succession']:,} successions")
    finally:
        raw.close()
    log.info("LEI-CDF delta done: %s", counts)
    return counts


def import_rr_delta(filepath: str, source_id: str, credibility_score: int,
                    limit: int | None = None) -> dict:
    """Apply an RR-CDF delta: upsert ACTIVE consolidation edges, close ones that
    went non-ACTIVE. Endpoint nodes are batch-upserted first. Idempotent."""
    raw, total = _open_json(filepath)
    counts = {"records": 0, "created": 0, "updated": 0, "closed": 0, "skipped": 0, "errors": 0}
    batch = _BatchWriter()
    active: list[tuple[str, str, str]] = []
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
                batch.entity(f"lei:{parent}", {"lei_id": parent, "source_id": source_id})
                batch.entity(f"lei:{child}", {"lei_id": child, "source_id": source_id})
                if status == "ACTIVE":
                    active.append((parent, child, marker))
                elif status == "INACTIVE":
                    closures.append((parent, child, marker, _relationship_end_date(rel) or _now_iso()))
                else:
                    counts["skipped"] += 1   # NULL / unknown — leave as-is
            except Exception as exc:  # noqa: BLE001
                counts["errors"] += 1
                if counts["errors"] <= 5:
                    log.warning("RR delta record error: %s", exc)
        batch.flush()                      # endpoint nodes exist before edge ops
        for parent, child, marker in active:
            outcome = _owns_edge_upsert(f"lei:{parent}", f"lei:{child}", child, marker,
                                        source_id, credibility_score)
            counts["created" if outcome == "created" else "updated"] += 1
        for parent, child, marker, until in closures:
            counts["closed"] += _close_owns(parent, child, marker, until)
        bar.finish(f"{counts['records']:,} records, +{counts['created']:,} edges, "
                   f"{counts['closed']:,} closed")
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


def download_deltas(publish: dict, interval: str, dest_dir: str | None = None) -> dict:
    """Download the LEI-CDF + RR delta .json.zip files for `interval` from a
    publish record. Returns {'lei2': path, 'rr': path}."""
    dest = dest_dir or tempfile.mkdtemp(prefix="gleif-delta-")
    out: dict = {}
    for section in ("lei2", "rr"):
        url = publish[section]["delta_files"][interval]["json"]["url"]
        path = os.path.join(dest, os.path.basename(url))
        with httpx.stream("GET", url, timeout=300) as resp:
            resp.raise_for_status()
            with open(path, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
        out[section] = path
        log.info("GLEIF %s %s delta: %s", section, interval, path)
    return out


def fetch_gleif_deltas(interval: str = "LastDay", dest_dir: str | None = None) -> dict:
    """Fetch + download the current delta files for a fixed `interval`
    (IntraDay | LastDay | LastWeek | LastMonth)."""
    return download_deltas(fetch_publish_metadata(), interval, dest_dir)


# ── full-load precondition ────────────────────────────────────────────────────
# The delta update rides *on top of* the full golden copy — applying a delta to a
# DB without that baseline would build a partial, wrong graph (only the recently
# changed records, no foundation). The full LEI-CDF importer stamps this marker on
# success; the update refuses to run without it, and `wipe-data` clears it so a wipe
# forces a fresh full load before deltas resume.

def mark_full_load_done() -> None:
    """Record that a full GLEIF LEI-CDF load has established the entity baseline."""
    run_sql(
        "UPDATE ImportState SET key = :k, last_run_at = :now UPSERT WHERE key = :k",
        {"k": _FULL_LOAD_KEY, "now": _now_iso()})


def full_load_present() -> bool:
    """True once a full LEI-CDF load has run (the delta update's precondition)."""
    rows = run_command(
        "MATCH (s:ImportState {key:$k}) RETURN s.last_run_at AS t", {"k": _FULL_LOAD_KEY})
    return bool(rows and rows[0].get("t"))


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
