#!/usr/bin/env python3
"""
Owlgraph management commands – run directly on the server.

Usage:
  python3 manage.py init-schema
  python3 manage.py geocode [--limit N]
  python3 manage.py normalize-countries
  python3 manage.py normalize-nationalities
  python3 manage.py gleif-lei-cdf [options]   # GLEIF entities (golden copy)
  python3 manage.py gleif-rr [options]        # GLEIF relationships (golden copy)
  python3 manage.py gleif-repex [options]     # GLEIF reasons for reporting no parent
  python3 manage.py ch-psc [options]          # Companies House PSC snapshot (UK ownership)
  python3 manage.py ch-company-data [options] # Companies House register (UK company names)
  python3 manage.py seed [options]
  python3 manage.py set-password EMAIL         # prompt for a new password for one account
  python3 manage.py sec-holdings CIK [--limit N] [--succeeds CIK]  # what a filer owns

Run inside a tmux session to keep running after SSH disconnect:
  tmux new -s import
  python3 manage.py gleif-lei-cdf --file /data/lei-cdf/gleif-lei2.json.zip --bulk-load
  Ctrl+B then D   (detach)
  tmux attach -t import   (reattach to check progress)
"""

import argparse
import sys

def _run_guarded_import(holder, fn, *, skip_ok=False):
    """Run an import under the cross-process DB import lock so no two imports write
    concurrently (which on top of a --bulk-load corrupts the load). If another import
    already holds the lock, the cron (skip_ok) skips cleanly; a manual run exits 1.
    A no-op when IMPORT_ORCHESTRATED is set — full-import.sh holds the lock itself."""
    from app.db.import_lock import ImportLocked, import_lock
    try:
        with import_lock(holder):
            return fn()
    except ImportLocked as exc:
        if skip_ok:
            print(f"{holder} skipped — {exc}")   # a full import (or another update) is running
            return None
        print(f"❌ {holder} refused — {exc}")
        sys.exit(1)

def cmd_gleif_succession(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    from app.scraper.runner import run_import_gleif_succession
    result = _run_guarded_import("gleif-succession",
        lambda: run_import_gleif_succession(local_file=args.file, limit=args.limit))
    if result is not None:
        print(result)

def cmd_gleif_rr(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    from app.scraper.runner import run_import_gleif_rr
    result = _run_guarded_import("gleif-rr",
        lambda: run_import_gleif_rr(local_file=args.file, limit=args.limit,
                                    only_leis=_only_ids(args),
                                    emit_leis_path=getattr(args, "emit_leis", None)))
    if result is not None:
        print(result)

def _apply_direct_db_url(args):
    """--db-url points the importer straight at ArcadeDB, bypassing a proxy that
    imposes a short read timeout (e.g. dev-db's 60s nginx). Removing that ceiling
    stops heavy flushes 504-ing and retrying — the main cause of a slow import."""
    url = getattr(args, "db_url", None)
    if url:
        from app.config import settings
        from app.db import arcadedb
        settings.ARCADEDB_URL = url
        arcadedb.close_client()   # drop the pooled client so it reconnects to url
        print(f"Using direct ArcadeDB URL: {url}")

def _only_ids(args):
    """Build the allow-list set from --only (comma list) and/or --only-file (one id per
    line, '#' comments allowed) — the curated test subset. None = import everything.
    Used to load a handful of test companies straight from the full golden-copy file."""
    ids: set[str] = set()
    inline = getattr(args, "only", None)
    if inline:
        ids.update(x.strip() for x in inline.split(",") if x.strip())
    path = getattr(args, "only_file", None)
    if path:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                tok = line.split("#", 1)[0].strip()
                if tok:
                    ids.add(tok)
    return ids or None

def cmd_ch_psc(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_UK_PSC_ENABLED = True
    _apply_direct_db_url(args)
    from app.scraper.runner import run_import_ch_psc
    result = _run_guarded_import("ch-psc",
        lambda: run_import_ch_psc(local_file=args.file, limit=args.limit,
                                  bulk_load=getattr(args, "bulk_load", False),
                                  batch_size=getattr(args, "batch_size", None) or 400,
                                  only_companies=_only_ids(args)))
    if result is not None:
        print(result)

def cmd_ch_company_data(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_UK_PSC_ENABLED = True
    _apply_direct_db_url(args)
    from app.scraper.runner import run_import_basic_company_data
    result = _run_guarded_import("ch-company-data",
        lambda: run_import_basic_company_data(local_file=args.file, limit=args.limit,
                                              bulk_load=getattr(args, "bulk_load", False),
                                              batch_size=getattr(args, "batch_size", None) or 400,
                                              only_companies=_only_ids(args)))
    if result is not None:
        print(result)

def cmd_gleif_lei_cdf(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    from app.scraper.runner import run_import_gleif_lei_cdf
    result = _run_guarded_import("gleif-lei-cdf",
        lambda: run_import_gleif_lei_cdf(
            local_file=args.file, limit=args.limit,
            filter_jurisdiction=args.jurisdiction, bulk_load=getattr(args, "bulk_load", False),
            only_leis=_only_ids(args)))
    if result is not None:
        print(result)

def cmd_gleif_repex(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    _apply_direct_db_url(args)
    from app.scraper.runner import run_import_gleif_repex
    result = _run_guarded_import("gleif-repex",
        lambda: run_import_gleif_repex(local_file=args.file, limit=args.limit))
    if result is not None:
        print(result)

def cmd_dedupe_entities(args):
    """Merge entities that share a hard external id (LEI / Companies House / SEC CIK /
    Wikidata) — the cross-source merge, no name match or Wikidata hub needed. Run after
    importing overlapping sources (e.g. GLEIF + PSC for UK companies)."""
    _apply_direct_db_url(args)
    from app.scraper.maintenance import deduplicate_entities
    res = deduplicate_entities(limit=getattr(args, "limit", None))
    print(f"Merged {res['entities_merged']} entities across "
          f"{res.get('total', '?')} shared-id groups; {res.get('remaining', 0)} groups remaining")

def cmd_mark_shortcuts(args):
    """Flag GLEIF ultimate-parent OWNS edges that duplicate a path the graph already
    draws, so the renderer can omit them. Run after every import: a delta that retires
    a direct edge turns a redundant shortcut into the only link to a company, and only
    a re-run notices."""
    _apply_direct_db_url(args)
    from app.scraper.maintenance import mark_ownership_shortcuts
    res = mark_ownership_shortcuts(limit=getattr(args, "limit", None))
    print(f"Processed {res['parents_processed']}/{res['parents_total']} parents; "
          f"{res['marked_redundant']} marked redundant, "
          f"{res['marked_load_bearing']} load-bearing, "
          f"{res['unchanged']} unchanged; {res['remaining']} parents remaining")

def cmd_gleif_update(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    _apply_direct_db_url(args)
    from app.scraper.runner import run_gleif_update
    result = _run_guarded_import("gleif-update",
        lambda: run_gleif_update(interval=args.interval, lei_file=args.lei_file,
                                 rr_file=args.rr_file, limit=args.limit,
                                 only_existing=args.only_existing,
                                 repex_file=args.repex_file),
        skip_ok=True)   # the cron rides on top of full-import → skip, don't error
    if result is not None:
        print(result)

def cmd_import_lock(args):
    _apply_direct_db_url(args)
    from app.db import import_lock
    if args.action == "status":
        print(import_lock.status())
    elif args.action == "release":
        import_lock.release()
        print("import lock released")
    elif args.action == "acquire":
        try:
            import_lock.acquire(args.holder or "manual")
        except import_lock.ImportLocked as exc:
            print(f"cannot acquire: {exc}")
            sys.exit(1)
        print(f"import lock acquired: {import_lock.status()}")

def cmd_flag_nominees(args):
    from app.scraper.maintenance import flag_nominee_entities
    result = flag_nominee_entities()
    print(f"Flagged {result['flagged']} nominee/custodian entities "
          f"(of {result['candidates']} name candidates)")

def cmd_prune_analytics(args):
    """Drop usage counters nothing has touched inside the retention window."""
    from app.analytics import prune
    result = prune(days=args.days, dry_run=args.dry_run)
    verb = "Would delete" if args.dry_run else "Deleted"
    print(f"{verb} rows older than {result.pop('cutoff')}:")
    for vtype, n in result.items():
        print(f"  {vtype:14} {n}")


def cmd_verify_users(args):
    """One-off: mark all existing accounts email-verified. Login now requires a
    verified email, so accounts created before that feature would otherwise be
    locked out. New sign-ups still verify via the emailed link."""
    from app.db.arcadedb import run_sql
    target = getattr(args, "email", None)
    if target:
        rows = run_sql("UPDATE User SET email_verified = true WHERE email = :e",
                       {"e": target.strip().lower()})
    else:
        rows = run_sql("UPDATE User SET email_verified = true WHERE email_verified IS NULL OR email_verified = false")
    n = int(rows[0].get("count", 0)) if rows and isinstance(rows[0], dict) else 0
    print(f"Marked {n} user(s) email-verified.")

def cmd_set_password(args):
    """Set a user's password directly against the database.

    The operator escape hatch for when the in-app flows can't be used: the reset
    flow needs email (blocked on Render) and /auth/change-password needs the
    current password. ADMIN_PASSWORD deliberately does NOT help here — it only
    seeds a *missing* account and never overwrites an existing one.

    The password is read from a hidden prompt, never an argv (which would land in
    shell history and `ps` output). Same policy as the API — both call
    password_policy_error.
    """
    import getpass
    from app.auth.password_policy import password_policy_error
    from app.auth.security import hash_password
    from app.db.arcadedb import run_sql

    email = args.email.strip().lower()
    rows = run_sql("SELECT email FROM User WHERE email = :e", {"e": email})
    if not rows:
        print(f"No user with email {email!r}.")
        raise SystemExit(1)

    password = args.password or getpass.getpass(f"New password for {email}: ")
    if not args.password:
        if password != getpass.getpass("Repeat new password: "):
            print("Passwords don't match.")
            raise SystemExit(1)

    problem = password_policy_error(password)
    if problem:
        print(problem)
        raise SystemExit(1)

    run_sql("UPDATE User SET password_hash = :h WHERE email = :e",
            {"h": hash_password(password), "e": email})
    print(f"Password updated for {email}.")


def cmd_sec_holdings(args):
    """Ingest the >5% stakes one SEC filer discloses in other companies.

    The mirror of a normal SEC scrape, which reads filings ABOUT a company. An
    asset manager has none of those — it is privately held and not a listed
    issuer — so its node stays empty until its own 13D/13G filings are read.

    Keyed on CIK: Vanguard's live book is filed by VANGUARD CAPITAL MANAGEMENT
    LLC (0002100119), not the VANGUARD GROUP INC (0000102909) you would search
    for. Pass --succeeds to record that handover as a SUCCEEDED_BY edge.
    """
    from app.scraper.runner import run_sec_holdings
    result = run_sec_holdings(args.cik, limit=args.limit, succeeds_cik=args.succeeds)
    if result["status"] != "ok":
        print(f"No EDGAR filer found for CIK {args.cik}.")
        raise SystemExit(1)
    print(f"{result['filer']}: {result['total']} holdings written "
          f"({result['ended']} already ended, {result.get('affiliates', 0)} affiliated managers linked).")
    if result.get("succession"):
        print(f"  succession: {result['succession']['predecessor']} → "
              f"{result['succession']['successor']}")


def cmd_seed(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_WIKIDATA_ENABLED = True
    import seed
    seed.main(region=args.region)

def cmd_init_schema(args):
    from app.db.schema import ensure_indexes
    result = ensure_indexes()
    if result.get("skipped"):
        print("Skipped — ArcadeDB unreachable.")
        sys.exit(1)
    print(f"Schema bootstrap: {len(result['ok'])} applied, {len(result['failed'])} failed")
    for f in result["failed"]:
        print(f"  FAILED: {f['stmt']}\n          -> {f['error']}")

def cmd_duplicate_names(args):
    """List same-name entity duplicates (same company under different LEIs/ids)
    for review after an import."""
    from app.scraper.maintenance import count_duplicate_entity_names, find_duplicate_entity_names
    c = count_duplicate_entity_names()
    print(f"Duplicate-name groups: {c['duplicate_name_groups']}  "
          f"(redundant nodes: {c['redundant_nodes']})")
    for g in find_duplicate_entity_names(limit=getattr(args, "limit", None) or 50,
                                         min_confidence=getattr(args, "min_confidence", None)):
        print(f"\n  [{g['confidence']}] {g['name_normalized']!r}  ({g['count']} nodes):")
        for m in g["members"]:
            print(f"    {m.get('id'):<28} {m.get('name')!r:40} "
                  f"country={m.get('country')} lei={m.get('lei_id')} "
                  f"wd={m.get('wikidata_id')} addr={m.get('registered_address')}")


def cmd_backfill_search(args):
    """Populate the FULL_TEXT-indexed `search_text` column for existing rows so
    /search can use the index instead of a full scan. Batched to stay under the
    DB proxy timeout. `ifnull(name, '')` guards against a null name leaving
    search_text NULL (which would re-match the WHERE and loop forever)."""
    from app.db.arcadedb import run_sql
    batch = getattr(args, "batch", None) or 20000
    # Fold aliases into search_text so a merged duplicate stays findable by its
    # alias (a LIST can't take a FULL_TEXT index directly). ifnull(...) guards
    # keep the result non-null even when name/aliases are absent, so a row can't
    # re-match `search_text IS NULL` and loop forever.
    specs = [
        ("Entity", "ifnull(name, '') + ' ' + ifnull(description, '') + ' ' + ifnull(aliases, []).join(' ')"),
        ("Person", "ifnull(full_name, '') + ' ' + ifnull(alias, []).join(' ')"),
    ]
    for t, expr in specs:
        total = 0
        while True:
            try:
                r = run_sql(f"UPDATE {t} SET search_text = ({expr}) "
                            f"WHERE search_text IS NULL LIMIT {batch}")
            except RuntimeError as exc:
                if "was not found" in str(exc):
                    break
                print(f"  {t}: {exc}")
                break
            n = int(r[0].get("count", 0)) if r and isinstance(r[0], dict) else 0
            total += n
            if n:
                print(f"  {t}: +{n} (total {total})")
            if n < batch:
                break
        print(f"  Done {t}: {total} rows")
    print("Backfill complete. Ensure the FULL_TEXT index exists: python manage.py init-schema")


def cmd_rebuild_search(args):
    """REBUILD the FULL_TEXT search indexes so /search (CONTAINSTEXT) finds every row.
    Needed after a non-bulk import (e.g. the --only test subset): entities are written
    with search_text set, but the FULL_TEXT index isn't maintained incrementally, so
    freshly imported companies aren't findable until this runs. Instant on a small
    test-only DB; minutes on the full ~4M graph."""
    _apply_direct_db_url(args)
    from app.db.schema import rebuild_fulltext_indexes
    res = rebuild_fulltext_indexes(hard=getattr(args, "hard", False))
    print(f"FULL_TEXT rebuilt (hard={getattr(args, 'hard', False)}): "
          f"ok={res.get('ok')} failed={res.get('failed')}")


def cmd_wipe_source(args):
    """Delete ONE source's data (edges + the nodes only it created). There is no
    whole-database wipe — a fresh dev start is a database DROP. Guards mirror the
    old wipe-data: an opt-in env var, a --confirm-database that must match the
    connected DB, and a final interactive retype (of the source name)."""
    import os
    from app.config import settings
    from app.scraper.maintenance import wipe_source

    target_db = settings.ARCADEDB_DATABASE
    source = args.source

    # Guard 1 — a dedicated opt-in var (NOT DEBUG, so debugging prod can't arm it).
    if os.getenv("ALLOW_DESTRUCTIVE_WIPE", "").lower() not in ("1", "true", "yes"):
        print("wipe-source is disabled. Set ALLOW_DESTRUCTIVE_WIPE=true to enable it.")
        print("(Not tied to DEBUG on purpose, so debugging prod cannot arm a delete.)")
        sys.exit(1)

    # Guard 2 — name the database you intend to modify; it must match the connected one.
    confirm_db = getattr(args, "confirm_database", None)
    if not confirm_db:
        print(f"Refusing: connected database is '{target_db}'.")
        print(f"Re-run with --confirm-database {target_db} to confirm the target.")
        sys.exit(1)
    if confirm_db != target_db:
        print(f"Refusing: --confirm-database '{confirm_db}' does not match the "
              f"connected database '{target_db}'.")
        sys.exit(1)

    # Guard 3 — final interactive check: retype the SOURCE name (the delete target).
    if not args.yes:
        print(f"This will delete the '{source}' source's data from '{target_db}': its ownership/")
        print("role edges and the nodes ONLY it created. Nodes another source also references")
        print("are kept. Other sources, user accounts and config are NOT affected.")
        print("(There is no whole-DB wipe — drop the database for a fresh start.)")
        confirm = input(f"Retype the source name '{source}' to confirm: ")
        if confirm.strip() != source:
            print("Aborted.")
            sys.exit(1)

    _apply_direct_db_url(args)   # point straight at ArcadeDB so the reindex isn't cut off by a proxy timeout
    prefixes = [p.strip() for p in (getattr(args, "id_prefix", None) or "").split(",") if p.strip()]
    try:
        result = wipe_source(source, batch=getattr(args, "batch", None) or 10000,
                             id_prefixes=prefixes or None)
    except ValueError as exc:
        print(exc)
        sys.exit(1)
    edges = sum(result["edges"].values())
    nodes = sum(result["nodes"].values())
    print(f"Wiped source '{source}': {edges:,} edges, {nodes:,} nodes deleted.")
    print(f"  edges: {result['edges']}")
    print(f"  nodes: {result['nodes']}")
    if result.get("reset_import_state"):
        print("  reset GLEIF import checkpoints (re-baseline with full-import.sh before the delta cron).")
    if result.get("reindexed") is False:
        print(f"  ⚠️ index rebuild failed ({result.get('reindex_error')}) — run REBUILD INDEX * "
              "against ArcadeDB (e.g. --db-url http://localhost:2480) before any re-import.")
    else:
        print(f"  rebuilt indexes (cleared stale entries from the deletes): {result.get('reindexed')}")
    print("Done.")

def cmd_geocode(args):
    from app.config import settings
    settings.GEOCODING_ENABLED = True
    from app.scraper.geocode_backfill import backfill
    result = backfill(limit=args.limit, target=args.target)
    for name, p in result["passes"].items():
        print(f"  {name:10} geocoded {p['geocoded']} of {p['total']} candidates")
    print(f"Geocoded {result['geocoded']} of {result['entities_total']} candidates")

def cmd_normalize_countries(args):
    from app.scraper.maintenance import normalize_entity_countries
    result = normalize_entity_countries()
    for c in result["converted"]:
        print(f"  {c['from']} -> {c['to']}")
    print(f"Converted {len(result['converted'])} country values "
          f"({result['skipped']} already canonical or unrecognized)")

def cmd_normalize_nationalities(args):
    from app.scraper.maintenance import normalize_person_nationalities
    result = normalize_person_nationalities()
    for c in result["converted"]:
        print(f"  {c['from']} -> {c['to']}")
    print(f"Converted {len(result['converted'])} of {result['distinct_values']} distinct values "
          f"({result['unchanged']} already ISO-2)")
    if result["unmapped"]:
        # Printed, not hidden: these are kept verbatim, and the list is how the
        # demonym table gets extended instead of the data quietly staying mixed.
        print(f"Left unchanged, not recognised ({len(result['unmapped'])}): "
              f"{', '.join(result['unmapped'])}")


def cmd_backfill_countries(args):
    from app.scraper.maintenance import backfill_entity_countries
    result = backfill_entity_countries(limit=args.limit)
    for c in result["changes"]:
        print(f"  {c['name'][:44]:44} -> {c['country']}  ({c['from']})")
    print(f"Filled {result['filled']} of {result['candidates']} entities with no country "
          f"({result['still_unknown']} still unknown — the source has none either)")


def cmd_backfill_sec_headquarters(args):
    """Fill in SEC filers' headquarters from EDGAR's business address.

    Separate from backfill-countries on purpose: that fills where a company is
    REGISTERED, this fills where it is RUN, and EDGAR's business address is only
    good evidence of the second.
    """
    from app.scraper.maintenance import backfill_sec_headquarters
    res = backfill_sec_headquarters(limit=args.limit)
    for c in res["changes"][:20]:
        print(f"  {c['name'][:40]:42} {c['country']}  {c['address'][:48]}")
    print(f"Filled {res['filled']} of {res['candidates']} SEC filers "
          f"({res['still_unknown']} EDGAR could not place)")


def cmd_backfill_entity_sources(args):
    from app.scraper.maintenance import backfill_entity_sources
    result = backfill_entity_sources()
    u = result["updated"]
    print(f"Stamped source_id on {u['wikidata']} Wikidata + {u['sec_edgar']} SEC EDGAR entities")
    print(f"{result['still_missing']} entities still have no source_id "
          f"(no wikidata_id/sec_cik to attribute)")
    if not result["wikidata_source_found"] or not result["sec_edgar_source_found"]:
        print("  note: a Source node was missing — "
              f"Wikidata found={result['wikidata_source_found']}, "
              f"SEC EDGAR found={result['sec_edgar_source_found']}")

def cmd_gen_federation_key(args):
    from app.federation_keys import generate_keypair, fingerprint
    priv, pub = generate_keypair()
    print("Ed25519 federation signing keypair generated.\n")
    print("Set this SECRET on your instance (env var, never commit):")
    print(f"  FEDERATION_SIGNING_KEY={priv}\n")
    print("Share this PUBLIC key with peers so they can verify your exports:")
    print(f"  public_key={pub}")
    print(f"  key_id={fingerprint(pub)}")

def cmd_backup_database(args):
    """Take a consistent online backup of the connected database.

    The file is written by the SERVER, into its own backup directory — the
    command cannot choose a path — so all this prints is the filename. On a
    production box, where the database is local, ~/scripts/backup-database.sh
    wraps this to verify, rotate and copy the archive off the machine.
    """
    _apply_direct_db_url(args)
    from app.db.backup import backup_database, BackupError
    try:
        res = backup_database()
    except BackupError as exc:
        print(f"BACKUP FAILED: {exc}")
        raise SystemExit(1)
    print(f"Backed up '{res['database']}' on the server.")
    # Machine-readable last line: the wrapper script reads the filename from it.
    print(f"backup_file={res['file']}")


def _build_parser():
    parser = argparse.ArgumentParser(description='Owlgraph management')
    subparsers = parser.add_subparsers()

    p_fedkey = subparsers.add_parser('gen-federation-key',
        help='Generate an Ed25519 signing keypair for federation')
    p_fedkey.set_defaults(func=cmd_gen_federation_key)

    # seed command
    p_seed = subparsers.add_parser('seed')
    p_seed.add_argument(
        '--region',
        default='all',
        choices=['europe','americas','asia','middleeast',
                 'africa','oceania','all']
    )
    p_seed.set_defaults(func=cmd_seed)

    # init-schema command
    p_schema = subparsers.add_parser('init-schema', help='Create vertex types and indexes')
    p_schema.set_defaults(func=cmd_init_schema)

    # duplicate-names command
    p_dn = subparsers.add_parser('duplicate-names',
        help='List same-name entity duplicates (same company under different LEIs) for review')
    p_dn.add_argument('--limit', type=int, default=50, help='Max groups to list (default 50)')
    p_dn.add_argument('--min-confidence', choices=['definitive', 'high', 'medium', 'low'],
                      help='Only show groups at least this confident they are the same company')
    p_dn.set_defaults(func=cmd_duplicate_names)

    # backfill-search command
    p_bfs = subparsers.add_parser('backfill-search',
        help='Populate the FULL_TEXT search_text column for existing rows (run after a bulk import)')
    p_bfs.add_argument('--batch', type=int, default=20000,
                       help='Rows updated per request — keep under the DB proxy timeout (default 20000)')
    p_bfs.set_defaults(func=cmd_backfill_search)

    # rebuild-search command (make freshly non-bulk-imported rows findable via /search)
    p_rbs = subparsers.add_parser('rebuild-search',
        help='REBUILD the FULL_TEXT search indexes (run after a non-bulk / --only import)')
    p_rbs.add_argument('--db-url', help='Override ARCADEDB_URL for this run')
    p_rbs.add_argument('--hard', action='store_true',
        help='DROP + re-CREATE the FULL_TEXT indexes before rebuilding — recovers a '
             'stuck/corrupted index a plain REBUILD reports "ok" on but never repopulates. '
             'Run against --db-url http://localhost:2480 to avoid a proxy read-timeout.')
    p_rbs.set_defaults(func=cmd_rebuild_search)

    # dedupe-entities command (cross-source merge by shared external id)
    p_de = subparsers.add_parser('dedupe-entities',
        help='Merge entities sharing a hard external id (LEI / CH number / SEC CIK / Wikidata)')
    p_de.add_argument('--limit', type=int, help='Max shared-id groups to process (default: all)')
    p_de.add_argument('--db-url', help='Override ARCADEDB_URL for this run')
    p_de.set_defaults(func=cmd_dedupe_entities)

    # mark-shortcuts command (flag redundant ultimate-parent edges for the renderer)
    p_ms = subparsers.add_parser('mark-shortcuts',
        help='Flag GLEIF ultimate-parent OWNS edges that duplicate an existing path')
    p_ms.add_argument('--limit', type=int, help='Max parents to process (default: all)')
    p_ms.add_argument('--db-url', help='Override ARCADEDB_URL for this run')
    p_ms.set_defaults(func=cmd_mark_shortcuts)

    # wipe-source command (replaces the removed whole-DB wipe-data; drop the
    # database for a fresh start instead)
    p_wipe = subparsers.add_parser('wipe-source',
                                   help="Delete ONE source's data (its edges + the nodes only it created). "
                                        "No whole-DB wipe — drop the database for a fresh start.")
    p_wipe.add_argument('--source', required=True,
                        help='Source name to delete, e.g. "UK PSC" / "GLEIF" / "Wikidata" / "SEC EDGAR"')
    p_wipe.add_argument('--confirm-database',
                        help='Name of the connected database; must match, to confirm the target')
    p_wipe.add_argument('--yes', action='store_true', help='Skip the interactive retype-the-source-name prompt')
    p_wipe.add_argument('--batch', type=int, default=10000,
                        help='Rows deleted per request — keep each well under the DB proxy timeout (default 10000)')
    p_wipe.add_argument('--id-prefix',
                        help='Comma-separated node id prefixes for this source (e.g. "chpsc:,gb-coh:" for UK PSC) — '
                             'deletes nodes by an indexed id range instead of an unindexed source_id scan (much '
                             'faster on millions of rows). Still degree-aware + source_id-guarded.')
    p_wipe.add_argument('--db-url',
                        help='Override ARCADEDB_URL — point straight at ArcadeDB (only via an SSH tunnel to the DB '
                             'host; localhost here is the test container) so the reindex is not cut off by a proxy timeout')
    p_wipe.set_defaults(func=cmd_wipe_source)

    # geocode command
    p_geo = subparsers.add_parser('geocode',
        help='Backfill entity coordinates via Nominatim (headquarters AND registered office)')
    p_geo.add_argument('--limit', type=int, help='Max entities to geocode this run')
    p_geo.add_argument('--target', default='both', choices=['both', 'hq', 'registered'],
                       help="Which address to geocode (default both — a company has two places)")
    p_geo.set_defaults(func=cmd_geocode)

    # normalize-countries command
    p_norm = subparsers.add_parser('normalize-countries',
                                   help='Convert full-name Entity.country values to ISO-2 codes')
    p_norm.set_defaults(func=cmd_normalize_countries)

    # normalize-nationalities command
    p_nat = subparsers.add_parser('normalize-nationalities',
                                  help='Convert Person.nationality demonyms ("British") to ISO-2 codes')
    p_nat.set_defaults(func=cmd_normalize_nationalities)

    # backfill-sec-headquarters: EDGAR's business address -> hq_* (where it is RUN)
    p_hq = subparsers.add_parser('backfill-sec-headquarters',
        help="Fill SEC filers' hq_address/hq_city/hq_country from EDGAR's business address")
    p_hq.add_argument('--limit', type=int, help='Max entities to process this run')
    p_hq.set_defaults(func=cmd_backfill_sec_headquarters)

    # backfill-countries command
    p_bc = subparsers.add_parser('backfill-countries',
                                 help="Fill missing Entity.country from Wikidata and SEC EDGAR")
    p_bc.add_argument('--limit', type=int, help='Only consider the first N candidates')
    p_bc.set_defaults(func=cmd_backfill_countries)

    # backfill-entity-sources command
    p_bes = subparsers.add_parser('backfill-entity-sources',
                                  help='Stamp source_id on Wikidata/SEC entities created before it was set')
    p_bes.set_defaults(func=cmd_backfill_entity_sources)

    # gleif-succession command
    p_succ = subparsers.add_parser('gleif-succession',
                                   help='Import GLEIF LEI-CDF succession (MERGED/DUPLICATE → SuccessorLEI) as SUCCEEDED_BY edges')
    p_succ.add_argument('--file', required=True, help='Path to a local LEI-CDF golden-copy .json/.zip')
    p_succ.add_argument('--limit', type=int, help='Max records to scan')
    p_succ.set_defaults(func=cmd_gleif_succession)

    # gleif-rr command
    p_rr = subparsers.add_parser('gleif-rr',
                                 help='Import GLEIF RR-CDF direct/ultimate parents as direct/indirect OWNS edges')
    p_rr.add_argument('--file', required=True, help='Path to a local RR-CDF golden-copy .json/.zip')
    p_rr.add_argument('--limit', type=int, help='Max records to scan')
    p_rr.add_argument('--only', help='Comma-separated seed LEIs — import their whole corporate family')
    p_rr.add_argument('--only-file', help='File of seed LEIs (one per line, # comments) — their corporate family')
    p_rr.add_argument('--emit-leis', help='With --only: write the family LEIs here (feed to gleif-lei-cdf --only-file to name them)')
    p_rr.set_defaults(func=cmd_gleif_rr)

    # gleif-repex command (why a company reports no parent)
    p_rx = subparsers.add_parser('gleif-repex',
                                 help='Import GLEIF reporting exceptions — the published reasons companies give for naming no parent')
    p_rx.add_argument('--file', required=True, help='Path to a local repex golden-copy .json/.zip')
    p_rx.add_argument('--limit', type=int, help='Max records to scan')
    p_rx.add_argument('--db-url',
                      help='Override ARCADEDB_URL for this run — point straight at ArcadeDB to bypass a proxy timeout')
    p_rx.set_defaults(func=cmd_gleif_repex)

    # ch-psc command (Companies House PSC snapshot — replaces UK PSC BODS)
    p_chp = subparsers.add_parser('ch-psc',
                                  help='Import a Companies House PSC snapshot (current UK beneficial ownership)')
    p_chp.add_argument('--file', required=True, help='Path to a local PSC snapshot .zip/.txt')
    p_chp.add_argument('--limit', type=int, help='Max records to scan')
    p_chp.add_argument('--bulk-load', action='store_true',
                       help='Drop secondary indexes during the load and rebuild after')
    p_chp.add_argument('--batch-size', type=int,
                       help='Records per flush (default 400). Lower it behind a short proxy timeout; raise it on a direct connection')
    p_chp.add_argument('--db-url',
                       help='Override ARCADEDB_URL for this run — point straight at ArcadeDB to bypass a proxy timeout')
    p_chp.add_argument('--only', help='Comma-separated company numbers to import (curated test subset)')
    p_chp.add_argument('--only-file', help='File of company numbers (one per line, # comments) to import')
    p_chp.set_defaults(func=cmd_ch_psc)

    # ch-company-data command (Companies House register — names/addresses for PSC companies)
    p_chc = subparsers.add_parser('ch-company-data',
                                  help='Enrich UK companies with names/addresses from a Companies House BasicCompanyData snapshot')
    p_chc.add_argument('--file', required=True, help='Path to a local BasicCompanyData .zip')
    p_chc.add_argument('--limit', type=int, help='Max rows to scan')
    p_chc.add_argument('--bulk-load', action='store_true',
                       help='Drop secondary indexes during the load and rebuild after')
    p_chc.add_argument('--batch-size', type=int,
                       help='Rows per flush (default 400). Lower it behind a short proxy timeout; raise it on a direct connection')
    p_chc.add_argument('--db-url',
                       help='Override ARCADEDB_URL for this run — point straight at ArcadeDB to bypass a proxy timeout')
    p_chc.add_argument('--only', help='Comma-separated company numbers to import (curated test subset)')
    p_chc.add_argument('--only-file', help='File of company numbers (one per line, # comments) to import')
    p_chc.set_defaults(func=cmd_ch_company_data)

    # gleif-lei-cdf command (entities from the golden copy — replaces GLEIF BODS)
    p_lei = subparsers.add_parser('gleif-lei-cdf',
                                  help='Import GLEIF entities from the LEI-CDF golden copy (name/country/address)')
    p_lei.add_argument('--file', required=True, help='Path to a local LEI-CDF golden-copy .json/.zip')
    p_lei.add_argument('--limit', type=int, help='Max records to scan')
    p_lei.add_argument('--jurisdiction', help='Country code filter, e.g. AT')
    p_lei.add_argument('--bulk-load', action='store_true',
                       help='Drop secondary indexes during the load and rebuild after (faster on the full 3.4M)')
    p_lei.add_argument('--only', help='Comma-separated LEIs to import (curated test subset)')
    p_lei.add_argument('--only-file', help='File of LEIs (one per line, # comments) to import')
    p_lei.set_defaults(func=cmd_gleif_lei_cdf)

    # import-lock command (inspect/manage the cross-process import lock)
    p_lock = subparsers.add_parser('import-lock',
                                   help='Cross-process import lock: status / acquire / release (manual recovery)')
    p_lock.add_argument('action', choices=['status', 'acquire', 'release'])
    p_lock.add_argument('--holder', help='Holder label for acquire (default: manual)')
    p_lock.add_argument('--db-url', help='Override ARCADEDB_URL for this run')
    p_lock.set_defaults(func=cmd_import_lock)

    # gleif-update command (retirement-aware daily delta on top of the full load)
    p_gu = subparsers.add_parser('gleif-update',
                                 help='Apply a GLEIF delta update (daily refresh: new/changed entities, merges, and closed relationships)')
    p_gu.add_argument('--interval', default='auto',
                      choices=['auto', 'IntraDay', 'LastDay', 'LastWeek', 'LastMonth'],
                      help='Delta window to fetch. Default "auto" = gap-aware: pick the '
                           'smallest window covering any missed runs since the last one')
    p_gu.add_argument('--lei-file', help='Use a local LEI-CDF delta .json/.zip instead of fetching')
    p_gu.add_argument('--rr-file', help='Use a local RR-CDF delta .json/.zip instead of fetching')
    p_gu.add_argument('--repex-file', help='Use a local reporting-exceptions delta .json/.zip instead of fetching')
    p_gu.add_argument('--limit', type=int, help='Max records to scan (per file)')
    p_gu.add_argument('--only-existing', dest='only_existing', default=None,
                      action=argparse.BooleanOptionalAction,
                      help='Refresh only companies already in this database, ignoring '
                           'records for the rest of the world. Defaults to ON when the '
                           'GLEIF baseline here is a curated subset (a whole delta would '
                           'bury it rather than refresh it) and OFF after a full load')
    p_gu.add_argument('--db-url',
                      help='Override ARCADEDB_URL for this run — point straight at ArcadeDB to bypass a proxy timeout')
    p_gu.set_defaults(func=cmd_gleif_update)

    # flag-nominees command
    p_nom = subparsers.add_parser('flag-nominees',
                                  help='Flag nominee/custodian entities (holders of record) by name')
    p_nom.set_defaults(func=cmd_flag_nominees)

    # prune-analytics command
    p_prune = subparsers.add_parser('prune-analytics',
                                    help='Delete usage counters untouched within the retention window')
    p_prune.add_argument('--days', type=int, default=365,
                         help='Retention window in days (default 365)')
    p_prune.add_argument('--dry-run', action='store_true', help='Report without deleting')
    p_prune.set_defaults(func=cmd_prune_analytics)

    # verify-users command (one-off: unblock pre-existing accounts under the new
    # "login requires a verified email" rule)
    p_vu = subparsers.add_parser('verify-users',
                                 help='Mark existing user accounts email-verified (login now requires it)')
    p_vu.add_argument('--email', help='Only verify this address (default: all unverified users)')
    # sec-holdings: read what an institutional filer OWNS (its own 13D/13G
    # filings), as opposed to a normal scrape which reads filings about a company.
    p_sh = subparsers.add_parser('sec-holdings',
                                 help="Ingest the >5%% stakes a SEC filer discloses in others")
    p_sh.add_argument('cik', help='CIK of the filer (e.g. 0002100119 for Vanguard Capital Management)')
    p_sh.add_argument('--limit', type=int, default=100, help='Max subject companies (default 100)')
    p_sh.add_argument('--succeeds', help='CIK this filer took over from → SUCCEEDED_BY edge')
    p_sh.set_defaults(func=cmd_sec_holdings)

    p_vu.set_defaults(func=cmd_verify_users)

    # set-password: operator reset when neither in-app route is usable (the email
    # reset flow needs SMTP; /auth/change-password needs the current password).
    p_sp = subparsers.add_parser('set-password',
                                 help="Set a user's password (prompts; ADMIN_PASSWORD only seeds new accounts)")
    p_sp.add_argument('email', help='Email address of the account to update')
    p_sp.add_argument('--password',
                      help='Non-interactive password (avoid — lands in shell history and ps)')
    p_sp.set_defaults(func=cmd_set_password)
    # backup-database: server-side online backup (app/db/backup.py explains why a
    # disk snapshot is not a substitute).
    p_bk = subparsers.add_parser('backup-database',
        help="Take a consistent online backup (written to the SERVER's backup directory)")
    p_bk.add_argument('--db-url', help='Override ARCADEDB_URL for this run')
    p_bk.set_defaults(func=cmd_backup_database)

    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    if hasattr(args, 'func'):
        args.func(args)
    else:
        _build_parser().print_help()
