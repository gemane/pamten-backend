#!/bin/bash
# The dev rebuild (template of the /rebuild skill; copy the kit to
# ~/.cache/rebuild-<date>/ and run it there — see SKILL.md): fresh golden copies, drop/create/schema, the
# curated bulk import (GLEIF + UK PSC test subset) and its finishing steps, the
# GLEIF history, the accounts back, then the replay of every scrape since the last
# rebuild, and the finishing steps again for what the scrapes added.
set -uo pipefail
. /home/administrator/scripts/lib.sh
owlgraph_env
cd "$WORKDIR" && source venv/bin/activate
export SCRAPER_TMP_DIR="$DATA/tmp"
R="$(cd "$(dirname "$0")" && pwd)"     # the kit folder this copy lives in
t() { echo; echo "######## $* ($(date +%H:%M:%S))"; }
die() { echo "❌ FAILED: $*"; python3 manage.py import-lock release >/dev/null 2>&1; exit 1; }
api() {
  local out
  out=$(curl -sS -u "root:$ARCADEDB_PASSWORD" -X POST "$ARCADEDB_URL/api/v1/server" \
        -H 'Content-Type: application/json' -d "{\"command\":\"$1\"}")
  echo "  $1 -> ${out:-ok}"
  case "$out" in *'"error"'*) return 1 ;; esac
}
START=$(date +%s)

t "0 refreshing the golden copies"
bash /home/administrator/scripts/steps/refresh-golden-copies.sh || die "golden copy refresh"

t "1 drop / create / schema of $ARCADEDB_DATABASE"
[ -s "$R/accounts.json" ] || die "no saved accounts — refusing to drop"
api "drop database $ARCADEDB_DATABASE" || die "drop"
api "create database $ARCADEDB_DATABASE" || die "create"
python3 manage.py init-schema || die "schema"

t "2 bulk import (curated GLEIF + UK PSC subset)"
python3 manage.py import-lock acquire --holder new-database || die "import lock"
export IMPORT_ORCHESTRATED=1
bash /home/administrator/scripts/steps/lei-cdf-import-test.sh || die "GLEIF test import"
bash /home/administrator/scripts/steps/psc-import-test.sh || die "PSC test import"
t "3 finishing the import"
REBUILD_SEARCH=1 bash /home/administrator/scripts/steps/finish-import.sh || die "finish-import"
t "4 GLEIF history from the kept intervals file"
python3 manage.py gleif-rr-history --intervals "$DATA/gleif-rr-history.jsonl.gz" || echo "  ! history apply failed (continuing)"
python3 manage.py import-lock release

t "5 accounts and sessions back"
python3 "$R/restore_accounts.py" || echo "  ! account restore failed — the saved file is $R/accounts.json"

t "6 replaying the scrapes"
python3 "$R/replay_scrapes.py" || echo "  ! replay ended with an error (see above)"

t "7 finishing what the scrapes added"
# the securities bridge (findings item 71): an EDGAR issuer's CUSIP -> its US ISIN ->
# GLEIF's daily LEI-ISIN file -> its LEI. The CUSIPs arrive with the replay's 13F and
# 13D/G reads, so it runs here and not in finish-import (a bulk import stores none).
# GLEIF's catalogue switch is its only gate; it downloads ~33 MB once a day. First,
# because it re-keys sec: nodes to lei: — LEIs the succession step below must see.
python3 manage.py gleif-isin-bridge --apply || echo "  ! securities bridge failed"
# The successions of every LEI the graph holds NOW (findings item 89): the chain's
# steps 4/5 and 5/5 read them for the import's ~2,000 LEIs, and the replay adds
# ~600 lei: nodes (the register pointer, the bridge, Exhibit 21 confirmations)
# whose successions nobody read — 84 of the 86 pairs of the 11-08 guard touched
# dev's final LEI set, 46 the import's. The list and the endpoints go into the kit
# folder, not /tmp (the chain's mktemp files were gone when the verify needed them);
# the endpoints the subset lacked are then named from the golden copy, as the chain
# does. The same rule as every finish-import step repeated here: the replay writes
# after the import's finish.
LEICDF="$DATA/lei-cdf/gleif-lei2.json.zip"
python3 "$R/lei_ids.py" "$R/leis_after_replay.txt" || echo "  ! LEI list failed"
if [ -s "$R/leis_after_replay.txt" ]; then
  python3 manage.py gleif-succession --file "$LEICDF" --only-file "$R/leis_after_replay.txt" \
    --emit-leis "$R/succession_leis.txt" || echo "  ! succession after the replay failed"
  if [ -s "$R/succession_leis.txt" ]; then
    python3 manage.py gleif-lei-cdf --file "$LEICDF" --only-file "$R/succession_leis.txt" \
      || echo "  ! naming the succession endpoints failed"
  fi
fi
# The entity dedup's SEC/LEI bridge reads EDGAR (`_bridge_sec_lei`, gated by
# `_sec_lei_check_enabled`), and the backend .env has scraping off: every
# rebuild's step 7 printed "Merged 0 entities" and never asked a filing for a
# same-name LEI/CIK pair — on the server ops.sh runs with the flags on. The
# replay forces them in-process; this step gets them for the dedupe alone
# (findings item 70, 2026-11-06). After the bridge and the succession step, so
# it folds what they added (a DUPLICATE registration read as a predecessor
# lands on its live record here — item 88 stops it at the source).
SCRAPER_ENABLED=true SCRAPER_SEC_EDGAR_ENABLED=true SCRAPER_BODS_GLEIF_ENABLED=true \
  python3 manage.py dedupe-entities --limit 100000 || echo "  ! dedupe failed"
# the ends the replay and the succession step wrote (GLEIF's end events, item 85a;
# Wikidata's P576): finish-import's step 2/5, repeated for what came after it
python3 manage.py heal-ends-at-dissolution --apply 2>&1 | grep -v "^WARNING\|^INFO" | tail -3
# the persons two scopes wrote twice — a Wikidata part retried hours after the SEC
# part wrote the same insiders under EDGAR's spelling (findings item 55)
python3 manage.py dedupe-persons || echo "  ! dedupe-persons failed"
python3 manage.py geocode || echo "  ! geocode failed"
python3 manage.py close-reported-by-parent || echo "  ! close-reported-by-parent failed"
python3 manage.py mark-shortcuts || echo "  ! mark-shortcuts failed"
python3 manage.py mark-stale || echo "  ! mark-stale failed"

t "8 checks (read-only)"
python3 manage.py schema-audit 2>&1 | grep -v "^WARNING\|^INFO" | tail -5
python3 manage.py heal-ends-at-dissolution 2>&1 | grep -v "^WARNING\|^INFO" | tail -3
# calls to each outside source and the bucket since step 1 created the database
# (step 0's golden copies are shell downloads, not counted)
python3 manage.py source-calls --from 2000-01-01 2>&1 | grep -v "^WARNING\|^INFO"
python3 "$R/snapshot.py" "$R/after.json" 2>&1 | grep -v "^WARNING\|^INFO"
echo
echo "✅ REBUILD DONE in $(( ($(date +%s) - START) / 60 )) min ($(date +%H:%M:%S))"
