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
python3 manage.py dedupe-entities --limit 100000 || echo "  ! dedupe failed"
python3 manage.py geocode || echo "  ! geocode failed"
python3 manage.py close-reported-by-parent || echo "  ! close-reported-by-parent failed"
python3 manage.py mark-shortcuts || echo "  ! mark-shortcuts failed"
python3 manage.py mark-stale || echo "  ! mark-stale failed"

t "8 checks (read-only)"
python3 manage.py schema-audit 2>&1 | grep -v "^WARNING\|^INFO" | tail -5
python3 manage.py heal-ends-at-dissolution 2>&1 | grep -v "^WARNING\|^INFO" | tail -3
python3 "$R/snapshot.py" "$R/after.json" 2>&1 | grep -v "^WARNING\|^INFO"
echo
echo "✅ REBUILD DONE in $(( ($(date +%s) - START) / 60 )) min ($(date +%H:%M:%S))"
