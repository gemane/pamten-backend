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
    ("Entity",   "registered_address", "NOTUNIQUE"),  # dup-detection corroborator
    # Filtered on every map drill-down (/entities/by-country/{country}) and by the
    # country-scoped search; without an index each one scans every Entity.
    ("Entity",   "country",            "NOTUNIQUE"),
    ("Entity",   "type",               "NOTUNIQUE"),
    ("Person",   "id",              "UNIQUE"),
    ("Person",   "full_name",       "NOTUNIQUE"),
    ("Person",   "wikidata_id",     "NOTUNIQUE"),
    ("Source",   "id",              "UNIQUE"),
    ("User",     "id",              "UNIQUE"),
    ("User",     "email",           "UNIQUE"),
    ("MergeLog", "id",              "UNIQUE"),
    ("MergeLog", "keep_id",         "NOTUNIQUE"),
    # Forwarding addresses for ids a merge folded away, so shared links, cached
    # client ids and federation peers don't dangle. old_id is UNIQUE — an id can
    # only have been merged into one survivor — and the lookup must be indexed,
    # since it runs on every by-id miss.
    ("MergedId", "old_id",          "UNIQUE"),
    ("MergedId", "new_id",          "NOTUNIQUE"),
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
    # Import checkpoints — e.g. the last GLEIF publish a delta update applied, so
    # the daily refresh can pick a catch-up interval that covers any missed runs.
    ("ImportState", "key",          "UNIQUE"),
    # Fruitless on-demand searches, so the same one is not run at every source
    # again the moment a user clicks twice. Keyed by normalised query + country,
    # which is exactly what makes two searches "the same search".
    ("ScrapeMiss",  "key",          "UNIQUE"),
    # Usage measurement — aggregate counters only, never linked to a user, a
    # session or an IP (see app/analytics.py for why that is the whole design).
    ("SearchDemand", "key",          "UNIQUE"),
    ("UsageCounter", "key",          "UNIQUE"),
    ("EndpointStat", "key",          "UNIQUE"),
    # Address -> coordinate cache, so a shared registered-agent building is
    # geocoded once rather than once per company registered there (24 companies
    # share one Wilmington address in the dev graph alone). UNIQUE on the cleaned
    # query string, which is also the only way it is ever looked up.
    ("GeoCache",    "query",        "UNIQUE"),
    # Runtime settings a deploy should not be needed to change — currently the
    # mobile minimum-supported-version policy (see routers/app_version.py). Keyed
    # so a lookup is indexed; the value is JSON so a policy updates atomically.
    ("AppSetting",  "key",          "UNIQUE"),
    # Rate-limit buckets: sliding-window attempt timestamps keyed by bucket id
    # (e.g. "login:<ip>:<email>", "email:<email>", "mfa:<user_id>").
    ("RateLimit",   "key",          "UNIQUE"),
    # Per-source assertions behind an edge (see app/claims.py). claim_key is
    # UNIQUE — one row per (kind, from, to, source) — which is what makes a
    # re-import update a source's claim instead of stacking another copy.
    # from_id/to_id are the lookup path when rendering an entity's provenance.
    ("Claim",       "claim_key",    "UNIQUE"),
    ("Claim",       "from_id",      "NOTUNIQUE"),
    ("Claim",       "to_id",        "NOTUNIQUE"),
    ("Claim",       "source_id",    "NOTUNIQUE"),
    # Refresh tokens (see app/auth/refresh.py). token_hash is UNIQUE because it
    # identifies the row on every refresh and two tokens must never collide;
    # user_id and family_id are the revoke-all and replay-response paths.
    ("RefreshToken", "token_hash",  "UNIQUE"),
    ("RefreshToken", "user_id",     "NOTUNIQUE"),
    ("RefreshToken", "family_id",   "NOTUNIQUE"),
]

# Edge types the app creates via Cypher and needs to exist up front (init-schema
# and the startup bootstrap create them).
_EDGE_TYPES: list[str] = [
    "OWNS", "HAS_ROLE", "RELATED_TO", "DUAL_LISTED_WITH", "SUCCEEDED_BY",
    "NOT_DUPLICATE",
]

# Full-text indexes powering /search. A FULL_TEXT index (tokenized, queried with
# the `CONTAINSTEXT` operator) turns name search from an un-indexable
# `toLower(name) CONTAINS` full scan — ~12s on 3M entities — into an index
# lookup. It lives on a dedicated `search_text` column (name [+ description])
# because FULL_TEXT can't share a property with the existing LSM indexes the
# scrapers use for exact-match resolution. Populate search_text with
# `manage.py backfill-search` (and the BODS importer sets it inline).
_FULLTEXT_INDEXES: list[tuple[str, str]] = [
    ("Entity", "search_text"),
    ("Person", "search_text"),
]


# Indexes on an EDGE property. Separate from `_INDEXES` because that list's first
# element drives `CREATE VERTEX TYPE`, and OWNS is an edge — appending it there
# would try to create a vertex type of the same name.
#
# `psc_self_link` is the Companies House PSC appointment link. The incremental
# refresh finds the edge a changed snapshot record belongs to by matching on it, in
# batches of ~1000 via `WHERE psc_self_link IN :links`; unindexed that is a full
# scan of every OWNS edge per batch.
_EDGE_INDEXES: list[tuple[str, str, str]] = [
    ("OWNS", "psc_self_link", "NOTUNIQUE"),
]


def _statements() -> list[str]:
    stmts: list[str] = []
    for vtype in sorted({t for t, _, _ in _INDEXES}):
        stmts.append(f"CREATE VERTEX TYPE {vtype} IF NOT EXISTS")
    for etype in _EDGE_TYPES:
        stmts.append(f"CREATE EDGE TYPE {etype} IF NOT EXISTS")
    for vtype, prop, kind in _INDEXES:
        # `IF NOT EXISTS` goes BEFORE the type in ArcadeDB SQL — makes the DDL
        # idempotent so re-runs (startup, bulk-load index rebuild) don't log a
        # "property already exists" failure for every property.
        stmts.append(f"CREATE PROPERTY {vtype}.{prop} IF NOT EXISTS STRING")
        stmts.append(f"CREATE INDEX IF NOT EXISTS ON {vtype} ({prop}) {kind}")
    for etype, prop, kind in _EDGE_INDEXES:
        stmts.append(f"CREATE PROPERTY {etype}.{prop} IF NOT EXISTS STRING")
        stmts.append(f"CREATE INDEX IF NOT EXISTS ON {etype} ({prop}) {kind}")
    for vtype, prop in _FULLTEXT_INDEXES:
        stmts.append(f"CREATE PROPERTY {vtype}.{prop} IF NOT EXISTS STRING")
        stmts.append(f"CREATE INDEX IF NOT EXISTS ON {vtype} ({prop}) FULL_TEXT")
    return stmts


def _fulltext_index_names(vtype: str, prop: str) -> list[str]:
    """Every index name on `vtype.<prop>` — the logical `Type[prop]` name AND the
    per-bucket physical names ArcadeDB auto-generates (`Type_<bucket>_<id>`). Used by
    the hard rebuild to drop a stuck FULL_TEXT index completely. `search_text` carries
    only the FULL_TEXT index, so matching on the property alone is unambiguous."""
    names: list[str] = []
    try:
        for row in run_sql("SELECT name, properties FROM schema:indexes"):
            r = dict(row)
            props = r.get("properties")
            name = r.get("name") or ""
            if props == [[prop]] and (name == f"{vtype}[{prop}]"
                                      or name.startswith(f"{vtype}_")):
                names.append(name)
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        log.warning("could not list indexes for %s.%s: %s", vtype, prop, exc)
    return names


def rebuild_fulltext_indexes(timeout: float = 3600, hard: bool = False) -> dict:
    """Repopulate the FULL_TEXT search indexes and return {"ok", "failed"}.

    A `--bulk-load` leaves these incomplete: the load drops them (per-insert FULL_TEXT
    maintenance is slow and, when a load is interrupted, unreliable), and afterwards
    ``ensure_indexes()`` only re-`CREATE`s them — `CREATE INDEX IF NOT EXISTS` is a
    no-op the moment the index exists, so it never backfills. An explicit `REBUILD`
    repopulates from the final data in one pass, which is what /search depends on.
    Runs with a long timeout (REBUILD is synchronous, minutes on millions of rows);
    a bulk load already talks to ArcadeDB directly (`--db-url`), bypassing any proxy
    read-timeout ceiling.

    `hard=True` first **drops** the FULL_TEXT index entirely (by every physical + logical
    name) and re-`CREATE`s it before the `REBUILD`. A plain `REBUILD` rebuilds the
    existing index structure in place; if that structure is itself corrupted/stuck — as
    happens when a past `CREATE`/`REBUILD` was cut off by a proxy read-timeout mid-flight,
    leaving a half-built Lucene index that later `REBUILD`s report "ok" on yet never
    repopulate — only a full drop + fresh `CREATE` (which reindexes all rows) recovers it.
    Run the hard path directly against ArcadeDB (`--db-url http://localhost:2480`) so it
    isn't cut off by the same proxy timeout again.
    """
    ok: list[str] = []
    failed: list[dict] = []
    for vtype, prop in _FULLTEXT_INDEXES:
        name = f"{vtype}[{prop}]"
        try:
            if hard:
                for idx in _fulltext_index_names(vtype, prop):
                    run_sql(f"DROP INDEX `{idx}` IF EXISTS", timeout=timeout)
                    log.info("hard rebuild: dropped %s", idx)
                # CREATE over existing rows reindexes them; REBUILD below is belt-and-braces.
                run_sql(f"CREATE INDEX IF NOT EXISTS ON {vtype} ({prop}) FULL_TEXT",
                        timeout=timeout)
            run_sql(f"REBUILD INDEX `{name}`", timeout=timeout)
            ok.append(name)
        except Exception as exc:  # noqa: BLE001 - best-effort maintenance
            log.warning("FULL_TEXT rebuild failed (%s): %s", name, exc)
            failed.append({"index": name, "error": str(exc)})
    log.info("FULL_TEXT rebuild (hard=%s): %d ok, %d failed", hard, len(ok), len(failed))
    return {"ok": ok, "failed": failed}


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
