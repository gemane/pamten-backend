"""Replay what built the dev database beyond the bulk import, in-process with
the current code against the database in backend/.env — what built dev beyond the
import after the 6 October rebuild (kept fixed since, so rebuilds compare):

  A  run-all over ~/scripts/scrape-targets.txt (160 names)
  B  run-all for the companies refreshed one by one in the panel (wikidata +
     sec_edgar runs), minus "Voting group · 8 parties" (the Alibaba bug), plus
     the Unilever scrapes of 2026-10-27 (run-all "Unilever" and the two panel
     refreshes); the hand merge of UNILEVER PLC that followed is NOT replayed —
     the SEC↔LEI bridge (#462) is meant to make it by itself
  F  the industry fill scrape-companies.sh ends with (POST /scraper/backfill-industry,
     default on) — the 2026-10-26 replay skipped it and its gap was a false finding
  C  the Exhibit 21 pass over the list and the 17 node names it also ran for
  D  the acquisitions reader for the three companies it ran for
  E  13F holders for the two companies it ran for

After A and B, two retries, once each: A2 the whole run-all again for the
companies whose Wikidata part failed ("too busy", a read timeout) — EVERY
source, not Wikidata alone: the SEC part reads the item's CIK pointer from the
node Wikidata writes, and without it resolved "T. Rowe Price Associates, Inc."
to another legal entity; the 2026-11-04 Wikidata-only retry then wrote the item
and never re-ran the SEC part (findings item 54) — and A3 the SEC parts that
came back `partial` (a section lost to the network, findings item 41).

Each step writes its run-log row, as the API would. Usage: replay_scrapes.py [A|B|C|D|E ...]
"""
import json
import logging
import os
import sys
import time

os.chdir("/home/administrator/github/pamten-backend/backend")
sys.path.insert(0, ".")
from dotenv import load_dotenv  # noqa: E402
load_dotenv(".env")
# forced, not defaulted: the rebuild job exports the backend's .env, where the
# master switch is off — setdefault left it off and every scrape was refused
os.environ["SCRAPER_ENABLED"] = "true"
os.environ["SCRAPER_SEC_EDGAR_ENABLED"] = "true"
logging.basicConfig(level=logging.WARNING)

from app.industry import backfill as industry_backfill  # noqa: E402
from app.scraper.runner import run_scrape_all, run_sec_ex21, run_sec_acquisitions, run_sec_13f  # noqa: E402
from app.scraper.run_log import record_run  # noqa: E402
from app import source_calls  # noqa: E402
# count every call to an outside source and the bucket the replay makes, as
# manage.py does for its commands (stored in SourceCall, flushed each minute)
source_calls.start()

TARGETS = [l.strip() for l in open("/home/administrator/scripts/scrape-targets.txt")
           if l.strip() and not l.startswith("#")]
# Frozen 2026-11-01 from the run log of the 2026-10-26 rebuild's before.json (the panel
# refreshes and the extra Exhibit 21 names), so every replay runs the same targets and
# two rebuilds compare. "SoftBank" is here since the target became "SoftBank Group"
# (findings item 28): the old name still gets its Exhibit 21 read, as on 2026-11-01.
REFRESHED = ["ALIBABA CLOUD (EUROPE) LIMITED", "Apple Inc.", "BARCLAYS PLC", "BERKSHIRE HATHAWAY INC.", "BRIGHTSTAR 20:20 UK LIMITED", "CASTLEGATE BUSINESS PARK LIMITED", "Carlsberg Uk Holdings Limited", "Eversource", "Eversource Energy", "GITHUB INC", "GRUPO TELEVISA S A B", "GRUPO TELEVISA, S.A.B.", "Gitlab", "Heineken International B.V.", "Jpmorgan Chase & Co", "LINKEDIN CORPORATION", "METASWITCH NETWORKS LTD", "Robert Hitchins Limited", "TATA CONSUMER PRODUCTS UK GROUP LIMITED", "Tesla, Inc.", "UNILEVER EUROPE B.V.", "UNILEVER PLC", "Unilever", "WIZ, INC.", "YouTube LLC"]
EX21_EXTRA = ["ANHEUSER-BUSCH INBEV", "Alibaba Group Holding Limited", "Alphabet, Inc.", "Apple Inc.", "BERKSHIRE HATHAWAY INC.", "BlackRock, Inc.", "Domino's Pizza LLC", "EMBRAER S.A.", "GRUPO TELEVISA, S.A.B.", "Heineken Holding", "MERCADOLIBRE, INC.", "MICROSOFT CORPORATION", "NEWS CORPORATION", "SPACE EXPLORATION TECHNOLOGIES CORP.", "SoftBank", "Softbank Group Corp", "SpaceX", "Tesla, Inc.", "Unilever Plc", "Vanguard Group Inc"]
ACQ = ["Alphabet", "Apple Inc.", "BERKSHIRE HATHAWAY INC."]
F13 = ["BERKSHIRE HATHAWAY INC.", "YouTube LLC"]


WIKIDATA_FAILED: list[str] = []
# Findings item 41: an SEC scrape that lost a section (13D/G owners, insiders,
# holdings) to the network now says `partial`. The rebuild has no later visit to
# catch up, so those companies get their SEC part once more after A and B.
SEC_PARTIAL: list[str] = []


def run_all(name):
    with record_run("all", name) as run:
        res = run_scrape_all(name, 1)
        if ((res.get("results") or {}).get("wikidata") or {}).get("status") == "error":
            WIKIDATA_FAILED.append(name)
        sec = (res.get("results") or {}).get("sec_edgar") or {}
        if sec.get("status") == "partial":
            SEC_PARTIAL.append(name)
            run["status"] = "partial"
            run["note"] = "lost to the network: sec_edgar: " + ", ".join(sec.get("failed_sections") or [])
        per = [v for v in (res.get("results") or {}).values() if isinstance(v, dict)]
        run["total"] = sum((v or {}).get("total", 0) for v in per)
        run["entity_id"] = next((v["entity_id"] for v in per if v.get("entity_id")), None)
    return source_line(res.get("results") or {})


def source_line(results: dict) -> dict:
    """The replay line's per-source part: (status, total) plus markers —
    `lost N` when a source lost single requests to the network (findings item
    45: a count that dropped next to it is the network, not the code), and
    `seats +N/M` when run-all asked the SEC's seat holders once more after the
    knowledge bases (findings item 67: N holdings written for M seats looked
    up, `seats error` when that pass failed). The replay logs at WARNING, so
    without the marker a verify cannot tell whether the second pass ran."""
    out = {}
    for k, v in results.items():
        if not isinstance(v, dict):
            continue
        row = (v.get("status"), v.get("total"))
        if v.get("lost_requests"):
            row += (f"lost {v['lost_requests']}",)
        rerun = v.get("seats_rerun")
        if isinstance(rerun, dict):
            row += (f"seats +{rerun.get('total') or 0}/{len(rerun.get('seats_asked') or [])}"
                    if rerun.get("status") == "ok" else "seats error",)
        out[k] = row
    return out


def short(res):
    # the 13F selection too (findings item 40): fetched/total and known filers found
    return {k: res.get(k) for k in ("status", "total", "written", "enriched", "filings_fetched",
                                    "filings_total", "priority_found", "priority_known") if k in res}


def phase(label, names, fn):
    print(f"\n=== {label}: {len(names)} ({time.strftime('%H:%M:%S')})", flush=True)
    ok = failed = 0
    for i, name in enumerate(names, 1):
        t0 = time.time()
        try:
            summary = fn(name)
            ok += 1
        except Exception as exc:  # noqa: BLE001 — one company must not stop the replay
            summary = f"FAILED: {str(exc)[:160]}"
            failed += 1
        print(f"[{i:3}/{len(names)}] {name:45} {time.time() - t0:5.0f}s {summary}", flush=True)
    print(f"=== {label} done: {ok} ok, {failed} failed ({time.strftime('%H:%M:%S')})", flush=True)


steps = sys.argv[1:] or ["A", "B", "C", "D", "E", "F"]
if "A" in steps:
    phase("A run-all over the list", TARGETS, run_all)
if "B" in steps:
    phase("B run-all for the panel refreshes", REFRESHED, run_all)
# 2026-10-29: 28 Wikidata searches answered "too busy" and the rebuild compared
# counts with a hole in them. A company whose Wikidata part failed gets its whole
# run-all once more — every source, as the on-demand gate re-runs an incomplete
# company (findings item 54): a Wikidata-only retry (2026-10-29 … 2026-11-04) wrote
# the item but left the SEC part where it had landed without the item's pointer.
# A second failure in A2 adds nothing (the list below is a copy); a `partial` SEC
# part in A2 still gets its A3 retry.
if WIKIDATA_FAILED:
    def retry_all(name):
        time.sleep(3)
        return run_all(name)
    phase("A2 run-all retry for the companies whose Wikidata part failed",
          sorted(set(WIKIDATA_FAILED)), retry_all)
if SEC_PARTIAL:
    from app.scraper.scraper_registry import registered
    sec_spec = next(s for s in registered() if s.name == "sec_edgar")

    def retry_sec(name):
        time.sleep(3)
        with record_run("sec_edgar", name) as run:
            res = sec_spec.run(name, 1, None)
            run["total"] = res.get("total", 0)
            run["entity_id"] = res.get("entity_id")
            if res.get("status") == "partial":
                run["status"] = "partial"
                run["note"] = "lost to the network: " + ", ".join(res.get("failed_sections") or [])
        return (res.get("status"), res.get("total"), res.get("failed_sections") or None)
    phase("A3 SEC retry for the partial parts", sorted(set(SEC_PARTIAL)), retry_sec)
if "C" in steps:
    phase("C Exhibit 21", TARGETS + EX21_EXTRA, lambda n: short(run_sec_ex21(n)))
if "D" in steps:
    phase("D acquisitions", ACQ, lambda n: short(run_sec_acquisitions(n)))
if "E" in steps:
    phase("E 13F holders", F13, lambda n: short(run_sec_13f(n)))
if "F" in steps:
    print(f"\n=== F industry fill ({time.strftime('%H:%M:%S')})", flush=True)
    res = industry_backfill()
    print({k: v for k, v in res.items() if not isinstance(v, list)}, flush=True)
print("REPLAY DONE", flush=True)
