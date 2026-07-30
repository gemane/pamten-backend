#!/usr/bin/env python3
"""
Owlgraph management commands – run directly on the server.

Usage:
  python3 manage.py init-schema
  python3 manage.py geocode [--limit N]
  python3 manage.py normalize-countries
  python3 manage.py gleif-lei-cdf [options]   # GLEIF entities (golden copy)
  python3 manage.py gleif-rr [options]        # GLEIF relationships (golden copy)
  python3 manage.py ch-psc [options]          # Companies House PSC snapshot (UK ownership)
  python3 manage.py ch-company-data [options] # Companies House register (UK company names)
  python3 manage.py seed [options]

Run inside a tmux session to keep running after SSH disconnect:
  tmux new -s import
  python3 manage.py gleif-lei-cdf --file /data/lei-cdf/gleif-lei2.json.zip --bulk-load
  Ctrl+B then D   (detach)
  tmux attach -t import   (reattach to check progress)
"""

import argparse
import sys

def cmd_gleif_succession(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    from app.scraper.runner import run_import_gleif_succession
    result = run_import_gleif_succession(local_file=args.file, limit=args.limit)
    print(result)

def cmd_gleif_rr(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    from app.scraper.runner import run_import_gleif_rr
    result = run_import_gleif_rr(local_file=args.file, limit=args.limit)
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

def cmd_ch_psc(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_UK_PSC_ENABLED = True
    _apply_direct_db_url(args)
    from app.scraper.runner import run_import_ch_psc
    result = run_import_ch_psc(local_file=args.file, limit=args.limit,
                               bulk_load=getattr(args, "bulk_load", False),
                               batch_size=getattr(args, "batch_size", None) or 400)
    print(result)

def cmd_ch_company_data(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_UK_PSC_ENABLED = True
    _apply_direct_db_url(args)
    from app.scraper.runner import run_import_basic_company_data
    result = run_import_basic_company_data(local_file=args.file, limit=args.limit,
                                           bulk_load=getattr(args, "bulk_load", False),
                                           batch_size=getattr(args, "batch_size", None) or 400)
    print(result)

def cmd_gleif_lei_cdf(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    from app.scraper.runner import run_import_gleif_lei_cdf
    result = run_import_gleif_lei_cdf(
        local_file=args.file, limit=args.limit,
        filter_jurisdiction=args.jurisdiction, bulk_load=getattr(args, "bulk_load", False))
    print(result)

def cmd_gleif_update(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    _apply_direct_db_url(args)
    from app.scraper.runner import run_gleif_update
    result = run_gleif_update(interval=args.interval, lei_file=args.lei_file,
                              rr_file=args.rr_file, limit=args.limit)
    print(result)

def cmd_flag_nominees(args):
    from app.scraper.maintenance import flag_nominee_entities
    result = flag_nominee_entities()
    print(f"Flagged {result['flagged']} nominee/custodian entities "
          f"(of {result['candidates']} name candidates)")

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


def cmd_wipe_data(args):
    import os
    from app.config import settings
    from app.db.arcadedb import run_sql
    from app.db.schema import ensure_indexes

    target_db = settings.ARCADEDB_DATABASE

    # Guard 1 — a DEDICATED opt-in var. Deliberately NOT DEBUG: DEBUG gets flipped
    # on a live box to diagnose problems, and that must never arm an irreversible
    # wipe. This var has no other purpose, so it's only ever set on purpose.
    if os.getenv("ALLOW_DESTRUCTIVE_WIPE", "").lower() not in ("1", "true", "yes"):
        print("wipe-data is disabled. Set ALLOW_DESTRUCTIVE_WIPE=true to enable it.")
        print("(Not tied to DEBUG on purpose, so debugging prod cannot arm a wipe.)")
        sys.exit(1)

    # Guard 2 — the caller must name the database they intend to wipe, and it must
    # match the one actually connected. This kills the "aimed at the wrong DB"
    # failure mode: you cannot wipe prod without typing prod's real name.
    confirm_db = getattr(args, "confirm_database", None)
    if not confirm_db:
        print(f"Refusing to wipe: connected database is '{target_db}'.")
        print(f"Re-run with --confirm-database {target_db} to confirm the target.")
        sys.exit(1)
    if confirm_db != target_db:
        print(f"Refusing to wipe: --confirm-database '{confirm_db}' does not match "
              f"the connected database '{target_db}'.")
        sys.exit(1)

    batch = getattr(args, "batch", None) or 10000
    # Clear each type's rows in small batches, THEN drop the now-empty type and
    # recreate it. A single DROP/DELETE on millions of rows exceeds the DB's
    # reverse-proxy timeout (dev-db is behind nginx, ~60s) and locks ArcadeDB
    # until it's restarted — batched `DELETE ... LIMIT` keeps every request short
    # and never holds a whole-DB lock; the final DROP is instant on the emptied
    # type and clears the stale index entries DELETE leaves behind. User accounts
    # and config (ScraperSource toggles, federation Peers) are kept; types that
    # don't exist are skipped.
    types = [
        "OWNS", "HAS_ROLE", "RELATED_TO", "DUAL_LISTED_WITH",
        "HEADQUARTERED_IN", "REGISTERED_IN", "OPERATES_IN", "NOT_DUPLICATE",
        "Entity", "Person", "Location", "Source",
        "MergeLog", "ScrapeRun", "Flag", "Suppression", "Pin", "Conflict",
        # Import checkpoints/markers: wiping the data removes the GLEIF baseline, so
        # the gleif-update precondition must reset too — deltas can't resume until a
        # fresh full load re-stamps the marker.
        "ImportState",
    ]
    # Guard 3 — final interactive check: retype the DB name (not a generic YES),
    # so muscle memory can't fire it against the wrong target. --yes skips this
    # for the user's own `!` runs, but Guards 1 & 2 still apply.
    if not args.yes:
        print(f"This will delete ALL imported data from '{target_db}' (entities, persons,")
        print("edges, sources) plus verification flags/suppressions/pins, merge logs and")
        print("scrape-run logs. User accounts and scraper/federation config are NOT affected.")
        print(f"Types to clear: {', '.join(types)}")
        confirm = input(f"Retype the database name '{target_db}' to confirm: ")
        if confirm.strip() != target_db:
            print("Aborted.")
            sys.exit(1)
    for t in types:
        deleted = 0
        while True:
            try:
                r = run_sql(f"DELETE FROM {t} LIMIT {batch}")
            except RuntimeError as exc:
                if "was not found" in str(exc):   # type doesn't exist — nothing to clear
                    break
                print(f"  {t}: {exc}")
                break
            n = int(r[0].get("count", 0)) if r and isinstance(r[0], dict) else 0
            deleted += n
            if n < batch:                          # last (partial) batch drained the type
                break
        try:
            run_sql(f"DROP TYPE {t} IF EXISTS UNSAFE")   # instant on the emptied type
        except Exception as exc:
            print(f"  {t} (drop): {exc}")
        print(f"  Cleared {t}" + (f" ({deleted} rows)" if deleted else ""))
    # Recreate the now-empty vertex/edge types + indexes.
    print("Recreating schema (types + indexes)...")
    res = ensure_indexes()
    print(f"  schema: {len(res.get('ok', []))} applied, {len(res.get('failed', []))} failed")
    print("Done.")

def cmd_geocode(args):
    from app.config import settings
    settings.GEOCODING_ENABLED = True
    from app.scraper.geocode_backfill import backfill
    result = backfill(limit=args.limit)
    print(f"Geocoded {result['geocoded']} of "
          f"{result['locations_total'] + result['entities_total']} candidates "
          f"({result['locations_geocoded']} locations, {result['entities_geocoded']} entities)")

def cmd_normalize_countries(args):
    from app.scraper.maintenance import normalize_entity_countries
    result = normalize_entity_countries()
    for c in result["converted"]:
        print(f"  {c['from']} -> {c['to']}")
    print(f"Converted {len(result['converted'])} country values "
          f"({result['skipped']} already canonical or unrecognized)")

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

    # wipe-data command
    p_wipe = subparsers.add_parser('wipe-data', help='Delete all imported data (keeps user accounts and schema)')
    p_wipe.add_argument('--confirm-database',
                        help='Name of the database you intend to wipe; must match the connected DB')
    p_wipe.add_argument('--yes', action='store_true', help='Skip the interactive retype-the-name prompt')
    p_wipe.add_argument('--batch', type=int, default=10000,
                        help='Rows deleted per request — keep each well under the DB proxy timeout (default 10000)')
    p_wipe.set_defaults(func=cmd_wipe_data)

    # geocode command
    p_geo = subparsers.add_parser('geocode', help='Backfill lat/lng for Location nodes via Nominatim')
    p_geo.add_argument('--limit', type=int, help='Max locations to geocode this run')
    p_geo.set_defaults(func=cmd_geocode)

    # normalize-countries command
    p_norm = subparsers.add_parser('normalize-countries',
                                   help='Convert full-name Entity.country values to ISO-2 codes')
    p_norm.set_defaults(func=cmd_normalize_countries)

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
    p_rr.set_defaults(func=cmd_gleif_rr)

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
    p_chc.set_defaults(func=cmd_ch_company_data)

    # gleif-lei-cdf command (entities from the golden copy — replaces GLEIF BODS)
    p_lei = subparsers.add_parser('gleif-lei-cdf',
                                  help='Import GLEIF entities from the LEI-CDF golden copy (name/country/address)')
    p_lei.add_argument('--file', required=True, help='Path to a local LEI-CDF golden-copy .json/.zip')
    p_lei.add_argument('--limit', type=int, help='Max records to scan')
    p_lei.add_argument('--jurisdiction', help='Country code filter, e.g. AT')
    p_lei.add_argument('--bulk-load', action='store_true',
                       help='Drop secondary indexes during the load and rebuild after (faster on the full 3.4M)')
    p_lei.set_defaults(func=cmd_gleif_lei_cdf)

    # gleif-update command (retirement-aware daily delta on top of the full load)
    p_gu = subparsers.add_parser('gleif-update',
                                 help='Apply a GLEIF delta update (daily refresh: new/changed entities, merges, and closed relationships)')
    p_gu.add_argument('--interval', default='auto',
                      choices=['auto', 'IntraDay', 'LastDay', 'LastWeek', 'LastMonth'],
                      help='Delta window to fetch. Default "auto" = gap-aware: pick the '
                           'smallest window covering any missed runs since the last one')
    p_gu.add_argument('--lei-file', help='Use a local LEI-CDF delta .json/.zip instead of fetching')
    p_gu.add_argument('--rr-file', help='Use a local RR-CDF delta .json/.zip instead of fetching')
    p_gu.add_argument('--limit', type=int, help='Max records to scan (per file)')
    p_gu.add_argument('--db-url',
                      help='Override ARCADEDB_URL for this run — point straight at ArcadeDB to bypass a proxy timeout')
    p_gu.set_defaults(func=cmd_gleif_update)

    # flag-nominees command
    p_nom = subparsers.add_parser('flag-nominees',
                                  help='Flag nominee/custodian entities (holders of record) by name')
    p_nom.set_defaults(func=cmd_flag_nominees)

    # verify-users command (one-off: unblock pre-existing accounts under the new
    # "login requires a verified email" rule)
    p_vu = subparsers.add_parser('verify-users',
                                 help='Mark existing user accounts email-verified (login now requires it)')
    p_vu.add_argument('--email', help='Only verify this address (default: all unverified users)')
    p_vu.set_defaults(func=cmd_verify_users)
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    if hasattr(args, 'func'):
        args.func(args)
    else:
        _build_parser().print_help()
