#!/usr/bin/env python3
"""
Owlgraph management commands – run directly on the server.

Usage:
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

parser = argparse.ArgumentParser(description='Owlgraph management')
subparsers = parser.add_subparsers()

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

args = parser.parse_args()
if hasattr(args, 'func'):
    args.func(args)
else:
    parser.print_help()
