#!/usr/bin/env python3
"""
Pamten seed script – populates the database with real-world ownership data.
Run with: python3 seed.py [--region europe|americas|asia|middleeast|africa|oceania|all]
Default: all regions

Make sure your .env file is configured before running.
"""

import argparse
import sys
import os
import time
from unittest.mock import patch

# ── Load .env before any app imports ─────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── Override scraper flags before Settings() is instantiated ─────────────────
os.environ["SCRAPER_ENABLED"]             = "true"
os.environ["SCRAPER_SEC_EDGAR_ENABLED"]   = "true"

from app.config import settings
settings.SCRAPER_ENABLED           = True
settings.SCRAPER_SEC_EDGAR_ENABLED = True

from app.scraper.runner import run_scrape, run_scrape_sec_edgar
from app.scraper.sources import _ensure_sources
from app.database import db


# ── Ensure ScraperSource nodes exist and are enabled ─────────────────────────

def _enable_sources():
    _ensure_sources()
    with db.get_session() as session:
        session.run(
            "MATCH (s:ScraperSource) SET s.enabled = true"
        )


# ── Company list ──────────────────────────────────────────────────────────────

COMPANIES = [
    # (name,                          region,       use_sec_edgar)
    ("AB InBev",                      "europe",      False),
    ("Heineken",                      "europe",      False),
    ("Carlsberg",                     "europe",      False),
    ("Nestlé",                        "europe",      False),
    ("Unilever",                      "europe",      False),
    ("Bertelsmann",                   "europe",      False),
    ("Axel Springer",                 "europe",      False),
    ("Alphabet",                      "americas",    True),
    ("Microsoft",                     "americas",    True),
    ("Apple",                         "americas",    True),
    ("News Corp",                     "americas",    True),
    ("Grupo Televisa",                "americas",    False),
    ("Embraer",                       "americas",    False),
    ("MercadoLibre",                  "americas",    True),
    ("Grupo Bimbo",                   "americas",    False),
    ("SoftBank",                      "asia",        False),
    ("Samsung Electronics",           "asia",        False),
    ("Tata Group",                    "asia",        False),
    ("Alibaba Group",                 "asia",        False),
    ("CITIC Group",                   "asia",        False),
    ("Saudi Aramco",                  "middleeast",  False),
    ("Mubadala Investment Company",   "middleeast",  False),
    ("Al Jazeera Media Network",      "middleeast",  False),
    ("Naspers",                       "africa",      False),
    ("Dangote Group",                 "africa",      False),
    ("MTN Group",                     "africa",      False),
    ("Wesfarmers",                    "oceania",     False),
    ("Nine Entertainment",            "oceania",     False),
]


# ── Seeding logic ─────────────────────────────────────────────────────────────

def seed_company(name: str, region: str, use_sec_edgar: bool) -> dict:
    """
    Seed one company. Returns a result dict with keys:
      status: 'ok' | 'partial' | 'failed'
      wikidata_nodes: int
      sec_nodes: int
      error: str | None
    """
    result = {"status": "ok", "wikidata_nodes": 0, "sec_nodes": 0, "error": None}

    # Wikidata
    try:
        wd = run_scrape(name, depth=1)
        result["wikidata_nodes"] = wd.get("total", 0)
        if wd.get("status") == "no_results":
            print(f"    Wikidata: no results")
            result["status"] = "partial"
        else:
            print(f"    Wikidata: {wd['total']} nodes  (wikidata_id: {wd.get('wikidata_id', '?')})")
    except Exception as e:
        print(f"    Wikidata ERROR: {e}")
        result["status"] = "partial"
        result["error"] = str(e)

    # SEC EDGAR (US-listed companies only)
    if use_sec_edgar:
        try:
            sec = run_scrape_sec_edgar(name)
            result["sec_nodes"] = sec.get("total", 0)
            if sec.get("status") == "no_results":
                print(f"    SEC EDGAR: no results")
            else:
                print(f"    SEC EDGAR: {sec['total']} nodes  (CIK: {sec.get('cik', '?')})")
        except Exception as e:
            print(f"    SEC EDGAR ERROR: {e}")
            if result["status"] == "ok":
                result["status"] = "partial"
            if not result["error"]:
                result["error"] = str(e)

    if result["wikidata_nodes"] == 0 and result["sec_nodes"] == 0:
        result["status"] = "failed"

    return result


def main(region: str | None = None):
    if region is None:
        parser = argparse.ArgumentParser(description="Seed Pamten database with real-world ownership data.")
        parser.add_argument(
            "--region",
            default="all",
            choices=["all", "europe", "americas", "asia", "middleeast", "africa", "oceania"],
            help="Region to seed (default: all)",
        )
        args = parser.parse_args()
        region = args.region

    companies = COMPANIES if region == "all" else [
        c for c in COMPANIES if c[1] == region
    ]

    if not companies:
        print(f"No companies found for region: {region}")
        sys.exit(1)

    print(f"\n🌱  Pamten seed — region: {region} ({len(companies)} companies)")
    print("─" * 60)

    # Ensure ScraperSource nodes exist and are enabled
    print("\n⚙️   Enabling scrapers in DB...")
    try:
        _enable_sources()
        print("    Sources enabled.")
    except Exception as e:
        print(f"    WARNING: could not enable sources: {e}")
        print("    Continuing — sources may already be enabled.")

    # Seed each company
    succeeded = []
    failed    = []
    total_wikidata_nodes = 0
    total_sec_nodes      = 0

    for name, region, use_sec in companies:
        scrapers = "Wikidata" + (" + SEC EDGAR" if use_sec else "")
        print(f"\n[{region}] {name}  ({scrapers})")
        result = seed_company(name, region, use_sec)
        total_wikidata_nodes += result["wikidata_nodes"]
        total_sec_nodes      += result["sec_nodes"]

        if result["status"] == "failed":
            failed.append((name, result["error"] or "no data returned"))
            print(f"    ⚠️  No data retrieved")
        else:
            succeeded.append(name)
            print(f"    ✅ Done")

        # Brief pause between companies to be polite to external APIs
        time.sleep(0.5)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print(f"  ✅  Seeded:  {len(succeeded)} / {len(companies)} companies")
    if failed:
        print(f"  ⚠️   Failed:  {len(failed)} companies")
        for name, err in failed:
            print(f"        • {name}: {err}")
    print(f"  📊  Wikidata nodes : {total_wikidata_nodes}")
    print(f"  📊  SEC EDGAR nodes: {total_sec_nodes}")
    print(f"  📊  Total nodes    : {total_wikidata_nodes + total_sec_nodes}")
    print("═" * 60)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
