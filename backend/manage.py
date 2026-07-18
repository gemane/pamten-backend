#!/usr/bin/env python3
"""
Owlgraph management commands – run directly on the server.

Usage:
  python3 manage.py init-schema
  python3 manage.py geocode [--limit N]
  python3 manage.py normalize-countries
  python3 manage.py bods-gleif [options]
  python3 manage.py bods-uk-psc [options]
  python3 manage.py seed [options]

Run inside a tmux session to keep running after SSH disconnect:
  tmux new -s import
  python3 manage.py bods-gleif --file /data/bods/gleif.zip
  Ctrl+B then D   (detach)
  tmux attach -t import   (reattach to check progress)
"""

import argparse
import sys

def cmd_bods_gleif(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_GLEIF_ENABLED = True
    from app.scraper.runner import run_import_bods_gleif
    result = run_import_bods_gleif(
        limit=args.limit,
        filter_jurisdiction=args.jurisdiction,
        local_file=args.file
    )
    print(result)

def cmd_bods_uk_psc(args):
    from app.config import settings
    settings.SCRAPER_ENABLED = True
    settings.SCRAPER_BODS_UK_PSC_ENABLED = True
    from app.scraper.runner import run_import_bods_uk_psc
    result = run_import_bods_uk_psc(
        limit=args.limit,
        local_file=args.file
    )
    print(result)

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

def cmd_wipe_data(args):
    import os
    if os.getenv("DEBUG", "").lower() not in ("1", "true", "yes"):
        print("wipe-data only runs with DEBUG=true. Aborted.")
        sys.exit(1)
    from app.db.arcadedb import run_sql
    types = ["OWNS", "HAS_ROLE", "Entity", "Person", "Location", "Source"]
    if not args.yes:
        print("This will delete ALL imported data (entities, persons, edges, sources).")
        print("User accounts are NOT affected.")
        print(f"Types to wipe: {', '.join(types)}")
        confirm = input("Type YES to confirm: ")
        if confirm.strip() != "YES":
            print("Aborted.")
            sys.exit(1)
    for t in types:
        try:
            run_sql(f"DELETE FROM {t}")
            print(f"  Wiped {t}")
        except Exception as exc:
            print(f"  {t}: {exc}")
    # Rebuild indexes — DELETE FROM leaves stale index entries pointing to
    # deleted RIDs, which cause RecordNotFoundException on the next import/read
    # (e.g. the SEC scraper 500'd until this ran). ensure_indexes only CREATEs
    # missing indexes; REBUILD INDEX * is what actually clears the stale entries.
    print("Rebuilding indexes...")
    from app.db.schema import ensure_indexes
    ensure_indexes()
    try:
        run_sql("REBUILD INDEX *")
        print("  Rebuilt all indexes (cleared stale entries).")
    except Exception as exc:
        print(f"  REBUILD INDEX * failed: {exc}")
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

    # bods-gleif command
    p_gleif = subparsers.add_parser('bods-gleif')
    p_gleif.add_argument('--file', help='Path to local gleif.zip')
    p_gleif.add_argument('--limit', type=int, help='Max statements')
    p_gleif.add_argument('--jurisdiction', help='Country code e.g. AT')
    p_gleif.set_defaults(func=cmd_bods_gleif)

    # bods-uk-psc command
    p_psc = subparsers.add_parser('bods-uk-psc')
    p_psc.add_argument('--file', help='Path to local uk_psc.zip')
    p_psc.add_argument('--limit', type=int, help='Max statements')
    p_psc.set_defaults(func=cmd_bods_uk_psc)

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

    # wipe-data command
    p_wipe = subparsers.add_parser('wipe-data', help='Delete all imported data (keeps user accounts and schema)')
    p_wipe.add_argument('--yes', action='store_true', help='Skip confirmation prompt')
    p_wipe.set_defaults(func=cmd_wipe_data)

    # geocode command
    p_geo = subparsers.add_parser('geocode', help='Backfill lat/lng for Location nodes via Nominatim')
    p_geo.add_argument('--limit', type=int, help='Max locations to geocode this run')
    p_geo.set_defaults(func=cmd_geocode)

    # normalize-countries command
    p_norm = subparsers.add_parser('normalize-countries',
                                   help='Convert full-name Entity.country values to ISO-2 codes')
    p_norm.set_defaults(func=cmd_normalize_countries)
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    if hasattr(args, 'func'):
        args.func(args)
    else:
        _build_parser().print_help()
