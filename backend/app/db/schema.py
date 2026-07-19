"""
Schema bootstrap: create the vertex types, properties, and indexes the app
relies on for its lookups.

Without these indexes the name-based upserts in the scraper (e.g.
`MATCH (e:Entity) WHERE e.wikidata_id = ... OR e.name_normalized = ...`)
do a full scan of every node, which makes bulk imports O(n^2). The unique
index on User.email also enforces the account-uniqueness the app assumes.

Design notes
------------
- Idempotent: vertex types and indexes use `IF NOT EXISTS`; property
  statements are guarded by the per-statement try/except (ArcadeDB does
  not support `IF NOT EXISTS` on `CREATE PROPERTY`).
- Fault-tolerant: each statement is guarded individually, so a single
  failure never aborts the rest, and an unreachable DB is skipped with one
  warning rather than crashing the caller. This lets it run best-effort on
  startup while remaining useful as an explicit `manage.py init-schema`.
"""
import logging

from app.db.arcadedb import run_sql

log = logging.getLogger(__name__)

# (vertex type, property, uniqueness) — the properties queries filter/join on.
_INDEXES: list[tuple[str, str, str]] = [
    ("Entity",   "id",                  "UNIQUE"),
    ("Entity",   "name",               "NOTUNIQUE"),
    ("Entity",   "name_normalized",    "NOTUNIQUE"),
    ("Entity",   "wikidata_id",        "NOTUNIQUE"),
    ("Entity",   "sec_cik",            "NOTUNIQUE"),
    ("Entity",   "lei_id",             "NOTUNIQUE"),
    ("Entity",   "companies_house_id", "NOTUNIQUE"),
    ("Person",   "id",              "UNIQUE"),
    ("Person",   "full_name",       "NOTUNIQUE"),
    ("Person",   "wikidata_id",     "NOTUNIQUE"),
    ("Location", "id",              "UNIQUE"),
    ("Source",   "id",              "UNIQUE"),
    ("User",     "id",              "UNIQUE"),
    ("User",     "email",           "UNIQUE"),
    ("MergeLog", "id",              "UNIQUE"),
    ("MergeLog", "keep_id",         "NOTUNIQUE"),
    ("Peer",     "id",              "UNIQUE"),
    ("Peer",     "base_url",        "NOTUNIQUE"),
    ("ScrapeRun", "id",             "UNIQUE"),
    ("ScrapeRun", "started_at",     "NOTUNIQUE"),
    # Verification flags (user reports that a node/edge looks wrong).
    ("Flag",      "id",             "UNIQUE"),
    ("Flag",      "status",         "NOTUNIQUE"),
    ("Flag",      "target_kind",    "NOTUNIQUE"),
    ("Flag",      "node_id",        "NOTUNIQUE"),
    ("Flag",      "from_id",        "NOTUNIQUE"),
    ("Flag",      "to_id",          "NOTUNIQUE"),
    # Suppressions — a moderator override hiding a wrong edge (Phase-B resolution).
    ("Suppression", "id",           "UNIQUE"),
    ("Suppression", "from_id",      "NOTUNIQUE"),
    ("Suppression", "to_id",        "NOTUNIQUE"),
    ("Suppression", "node_id",      "NOTUNIQUE"),
    # Pins — a moderator-corrected OWNS value that overrides the scraped one.
    ("Pin",         "id",           "UNIQUE"),
    ("Pin",         "from_id",      "NOTUNIQUE"),
    ("Pin",         "to_id",        "NOTUNIQUE"),
]

# Edge types the app creates via Cypher and needs to exist up front (also what
# wipe-data recreates after dropping them).
_EDGE_TYPES: list[str] = [
    "OWNS", "HAS_ROLE", "RELATED_TO", "DUAL_LISTED_WITH",
    "HEADQUARTERED_IN", "REGISTERED_IN", "OPERATES_IN", "NOT_DUPLICATE",
]


def _statements() -> list[str]:
    stmts: list[str] = []
    for vtype in sorted({t for t, _, _ in _INDEXES}):
        stmts.append(f"CREATE VERTEX TYPE {vtype} IF NOT EXISTS")
    for etype in _EDGE_TYPES:
        stmts.append(f"CREATE EDGE TYPE {etype} IF NOT EXISTS")
    for vtype, prop, kind in _INDEXES:
        stmts.append(f"CREATE PROPERTY {vtype}.{prop} STRING")
        stmts.append(f"CREATE INDEX IF NOT EXISTS ON {vtype} ({prop}) {kind}")
    return stmts


def ensure_indexes() -> dict:
    """
    Create the types/properties/indexes best-effort. Returns a summary:
    {"ok": [stmt, ...], "failed": [{"stmt", "error"}, ...], "skipped": bool}.
    """
    ok: list[str] = []
    failed: list[dict] = []
    for stmt in _statements():
        try:
            run_sql(stmt)
            ok.append(stmt)
        except ConnectionError as exc:
            # DB not reachable — don't spam a warning per statement.
            log.warning("Schema bootstrap skipped — ArcadeDB unreachable: %s", exc)
            return {"ok": ok, "failed": failed, "skipped": True}
        except Exception as exc:  # noqa: BLE001 - best-effort DDL
            log.warning("Schema statement failed (%s): %s", stmt, exc)
            failed.append({"stmt": stmt, "error": str(exc)})
    log.info("Schema bootstrap complete: %d applied, %d failed", len(ok), len(failed))
    return {"ok": ok, "failed": failed, "skipped": False}
