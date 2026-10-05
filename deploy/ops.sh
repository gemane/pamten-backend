#!/bin/bash
# ops.sh — day-to-day operations on the production box (installed by
# setup-server.sh to /opt/owlgraph/deploy/ops.sh; run as root).
#
#   ops.sh status                       containers, /health, the import lock
#   ops.sh import full|test             load the graph (drop nothing — run on an empty DB)
#   ops.sh refresh-golden-copies        GLEIF lei2 / rr / repex to the current publish
#   ops.sh refresh-psc                  Companies House PSC snapshot
#   ops.sh refresh-company-data         Companies House BasicCompanyData
#   ops.sh gleif-update                 the daily GLEIF delta           (cron)
#   ops.sh backup                       online backup + verify + rotate + offsite (cron)
#   ops.sh weekly-report                the Monday digest mail          (cron)
#   ops.sh prune-analytics              retention of usage counters     (cron)
#   ops.sh alerts [args]                manage.py alerts …
#   ops.sh upgrade X.Y.Z                a new release: web tarball + image
#   ops.sh install-web [X.Y.Z]          (re)install the web app of a release
#   ops.sh manage <args>                any manage.py command, in the api container
#   ops.sh count <Type>                 vertices of a type (Entity, Person, …)
#
# The production twin of the dev box's ~/scripts: the same steps, but every
# manage.py call runs inside the `api` container (docker compose run/exec),
# because production has no venv — only the release image. Files the importers
# read live in DATA_DIR on the host, mounted at /data in the container.
#
# Configuration: deploy/.env (compose + the app) and /opt/owlgraph/host.env
# (host-only: backups, the first import, check URLs — never passed into a
# container). Both are written by setup-server.sh from one input file.
set -euo pipefail

ROOT="${OWLGRAPH_ROOT:-/opt/owlgraph}"
DEPLOY="$ROOT/deploy"
HOST_ENV="$ROOT/host.env"

die()  { echo "❌ $*" >&2; exit 1; }
step() { echo "### $* — $(date -u +%FT%TZ)"; }

load_env() {
  [ -f "$DEPLOY/.env" ] || die "no $DEPLOY/.env — run setup-server.sh first"
  set -a
  # shellcheck disable=SC1091
  . "$DEPLOY/.env"
  # shellcheck disable=SC1090
  [ -f "$HOST_ENV" ] && . "$HOST_ENV"
  set +a
  DB="${ARCADEDB_DATABASE:-owlgraph}"
  DATA="${DATA_DIR:-/srv/owlgraph/data}"
}

dc() { (cd "$DEPLOY" && docker compose "$@"); }
# A one-off manage.py run in a fresh api container (imports: long, own process).
# ORCHESTRATED is set once cmd_import holds the import lock: the importers then
# know the lock is the chain's, not someone else's (app/db/import_lock.py).
run_manage()  { dc run --rm -T -e SCRAPER_TMP_DIR=/data/tmp \
                  ${ORCHESTRATED:+-e IMPORT_ORCHESTRATED=1} api python manage.py "$@"; }
# A manage.py call in the running api container (short jobs).
exec_manage() { dc exec -T api python manage.py "$@"; }

# ── dead man's switch (host.env: HC_<JOB>=https://hc-ping.com/<uuid>) ────────
ping_hc() { curl -fsS -m 10 --retry 3 -o /dev/null "$1" 2>/dev/null || true; }
watch_job() {
  local var url
  var="HC_$(echo "$1" | tr '[:lower:]-' '[:upper:]_')"
  url="${!var:-}"
  [ -n "$url" ] || return 0
  ping_hc "$url/start"
  # shellcheck disable=SC2064
  trap "rc=\$?; if [ \$rc -eq 0 ]; then ping_hc '$url'; else ping_hc '$url/fail'; fi" EXIT
}

# ── downloads: written to .tmp, checked, then swapped in (never a truncated file) ─
fetch_checked() {   # <url> <dest> [expected-bytes]
  local url="$1" dest="$2" size="${3:-}" tmp="$2.tmp"
  mkdir -p "$(dirname "$dest")"
  rm -f "$tmp"
  curl -fL --retry 3 --retry-delay 5 --progress-bar -o "$tmp" "$url"
  if [ -n "$size" ] && [ "$(stat -c%s "$tmp")" != "$size" ]; then
    rm -f "$tmp"; die "$(basename "$dest"): size differs from the publish record — kept the old file"
  fi
  if ! unzip -tq "$tmp" >/dev/null 2>&1; then
    rm -f "$tmp"; die "$(basename "$dest"): not a readable zip — kept the old file"
  fi
  [ -f "$dest" ] && mv -f "$dest" "$dest.prev"
  mv -f "$tmp" "$dest"
}

cmd_refresh_golden_copies() {
  local force=""
  [ "${1:-}" = "--force" ] && force=1
  step "GLEIF: the current publish"
  local meta published
  meta=$(curl -fsS "https://goldencopy.gleif.org/api/v2/golden-copies/publishes?per_page=1")
  published=$(echo "$meta" | jq -r '.data[0].publish_date')
  echo "  publish $published"
  for pair in "lei2 lei-cdf/gleif-lei2.json.zip" "rr rr-cdf/gleif-rr.json.zip" \
              "repex repex/gleif-repex.json.zip"; do
    local section=${pair%% *} dest="$DATA/${pair#* }" url size
    url=$(echo "$meta" | jq -r ".data[0].$section.full_file.json.url")
    size=$(echo "$meta" | jq -r ".data[0].$section.full_file.json.size")
    if [ -f "$dest.publish" ] && [ "$(cat "$dest.publish")" = "$published" ] \
       && [ -z "$force" ]; then
      echo "  $section: already current"; continue
    fi
    echo "  $section: downloading $(numfmt --to=iec "$size")"
    fetch_checked "$url" "$dest" "$size"
    echo "$published" > "$dest.publish"
  done
}

# Companies House publishes no API for its bulk files: the download pages name
# the current file, and the newest link wins.
ch_latest_link() {   # <index-page> <filename-regex>
  curl -fsSL "$1" | grep -oE "href=\"[^\"]*$2\"" | sed 's/^href="//; s/"$//' | sort | tail -1
}

cmd_refresh_psc() {
  step "Companies House PSC snapshot"
  local href
  href=$(ch_latest_link "https://download.companieshouse.gov.uk/en_pscdata.html" \
                        'persons-with-significant-control-snapshot-[0-9-]+\.zip')
  [ -n "$href" ] || die "no PSC snapshot link on the Companies House page"
  fetch_checked "https://download.companieshouse.gov.uk/${href#/}" \
                "$DATA/companies-house-psc/psc-snapshot.zip"
}

cmd_refresh_company_data() {
  step "Companies House BasicCompanyData"
  local href
  href=$(ch_latest_link "https://download.companieshouse.gov.uk/en_output.html" \
                        'BasicCompanyDataAsOneFile-[0-9-]+\.zip')
  [ -n "$href" ] || die "no BasicCompanyData link on the Companies House page"
  fetch_checked "https://download.companieshouse.gov.uk/${href#/}" \
                "$DATA/companies-house-basic/basic-company-data.zip"
}

cmd_import() {
  local mode="${1:-}"
  [ "$mode" = full ] || [ "$mode" = test ] || die "usage: ops.sh import full|test"
  local lei=/data/lei-cdf/gleif-lei2.json.zip rr=/data/rr-cdf/gleif-rr.json.zip
  local repex=/data/repex/gleif-repex.json.zip
  local psc=/data/companies-house-psc/psc-snapshot.zip
  local basic=/data/companies-house-basic/basic-company-data.zip
  [ -f "$DATA/lei-cdf/gleif-lei2.json.zip" ] || cmd_refresh_golden_copies
  [ -f "$DATA/companies-house-psc/psc-snapshot.zip" ] || cmd_refresh_psc
  [ -f "$DATA/companies-house-basic/basic-company-data.zip" ] || cmd_refresh_company_data
  mkdir -p "$DATA/tmp"

  step "import lock"
  run_manage import-lock acquire --holder "ops-import-$mode" \
    || die "another import (or the gleif-update cron) holds the lock"
  # released whatever happens below
  trap 'ORCHESTRATED= run_manage import-lock release >/dev/null 2>&1 || true' EXIT
  ORCHESTRATED=1
  local t0
  t0=$(date +%s)
  if [ "$mode" = full ]; then
    step "1/6 GLEIF entities";       run_manage gleif-lei-cdf --file "$lei" --bulk-load
    step "2/6 GLEIF relationships";  run_manage gleif-rr --file "$rr"
    step "3/6 GLEIF succession";     run_manage gleif-succession --file "$lei"
    step "4/6 GLEIF no-parent reasons"; run_manage gleif-repex --file "$repex"
    step "5/6 UK PSC$([ -n "${PSC_LIMIT:-}" ] && echo " (capped at $PSC_LIMIT)")"
    run_manage ch-psc --file "$psc" --bulk-load --batch-size 1000 \
      ${PSC_LIMIT:+--limit "$PSC_LIMIT"}
    step "6/6 UK company names";     run_manage ch-company-data --file "$basic" --bulk-load --batch-size 1000
  else
    local seeds=app/scraper/data/test_leis.txt companies=app/scraper/data/test_companies.txt
    step "1/4 GLEIF test seeds";     run_manage gleif-lei-cdf --file "$lei" --only-file "$seeds"
    step "2/4 their families"
    run_manage gleif-rr --file "$rr" --only-file "$seeds" --emit-leis /data/tmp/test-family.txt
    run_manage gleif-lei-cdf --file "$lei" --only-file /data/tmp/test-family.txt
    step "3/4 UK PSC test companies"; run_manage ch-psc --file "$psc" --only-file "$companies"
    step "4/4 their names";          run_manage ch-company-data --file "$basic" --only-file "$companies"
  fi
  step "finishing"
  # a --bulk-load chain rebuilds the FULL_TEXT index itself; the curated one does not
  [ "$mode" = test ] && run_manage rebuild-search
  run_manage geocode
  run_manage mark-shortcuts
  # (the GLEIF entity import records the baseline the daily delta continues from)
  echo "✅ import ($mode) done in $(( ($(date +%s) - t0) / 60 )) min"
}

cmd_backup() {
  watch_job backup
  step "backup"
  exec_manage backup-database
  local dir="$ARCADEDB_DATA/backups/$DB" newest
  newest=$(ls -1t "$dir"/*.zip 2>/dev/null | head -1)
  [ -n "$newest" ] || die "no archive appeared in $dir"
  unzip -tq "$newest" >/dev/null || die "$newest is not a readable zip"
  echo "  verified $(basename "$newest") ($(du -h "$newest" | cut -f1))"
  local offsite=false
  if [ -n "${BACKUP_REMOTE:-}" ]; then
    rsync -a --partial "$newest" "$BACKUP_REMOTE" && offsite=true
    echo "  copied offsite to $BACKUP_REMOTE"
  fi
  # rotate only AFTER verify (+ offsite): never delete history before the new copy is proven
  ls -1t "$dir"/*.zip | tail -n +"$(( ${BACKUP_KEEP:-7} + 1 ))" | xargs -r rm -f
  # the marker `manage.py alerts` reads (ALERT_BACKUP_MARKER=/data/backup.last):
  # the dev script's format, and in DATA_DIR because alerts runs in the container
  echo "$(date -u +%FT%TZ) $(basename "$newest")$([ "$offsite" = true ] && echo " offsite")" \
    > "$DATA/backup.last"
}

# The web app of release X.Y.Z: the frontend's release tarball, checked against
# its .sha256, and checked to be built for THIS domain (PROD_API_URL is baked
# into the bundle at build time — a tarball built for another origin loads and
# then fails every API call).
cmd_install_web() {
  local v="${1:-$APP_VERSION}" base tmp
  base="https://github.com/$GITHUB_OWNER/pamten-frontend/releases/download/v$v"
  tmp=$(mktemp -d)
  step "web app $v"
  curl -fsSL -o "$tmp/web.tgz" "$base/owlgraph-web-$v.tar.gz" \
    || die "no web tarball for v$v — is the frontend release published?"
  curl -fsSL -o "$tmp/web.sha" "$base/owlgraph-web-$v.tar.gz.sha256" || die "no .sha256 for v$v"
  [ "$(sha256sum "$tmp/web.tgz" | cut -d' ' -f1)" = "$(cut -d' ' -f1 "$tmp/web.sha")" ] \
    || die "web tarball checksum mismatch"
  mkdir -p "$tmp/www" && tar -xzf "$tmp/web.tgz" -C "$tmp/www"
  grep -rqs "$DOMAIN" "$tmp/www" \
    || die "the v$v web app was not built for $DOMAIN (set the frontend repo variable PROD_API_URL=https://$DOMAIN and re-release)"
  mkdir -p "$WEB_DIR"
  rm -rf "${WEB_DIR:?}.old" && [ -d "$WEB_DIR" ] && mv "$WEB_DIR" "$WEB_DIR.old"
  mv "$tmp/www" "$WEB_DIR" && chmod -R a+rX "$WEB_DIR"
  rm -rf "$tmp"
  echo "  installed into $WEB_DIR"
}

# How many vertices of a type the database holds (setup-server.sh asks before
# importing, so a re-run never loads a second copy on top of the first).
cmd_count() {
  dc exec -T api python -c "import sys
from app.db.arcadedb import run_sql
print(run_sql('SELECT count(*) AS n FROM ' + sys.argv[1])[0]['n'])" "${1:?usage: ops.sh count <Type>}"
}

cmd_status() {
  dc ps
  curl -fsS "https://$DOMAIN/health" && echo || echo "  /health not answering at https://$DOMAIN"
  exec_manage import-lock status || true
}

cmd_upgrade() {
  local v="${1:-}"
  [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: ops.sh upgrade X.Y.Z"
  cmd_install_web "$v"
  sed -i "s/^APP_VERSION=.*/APP_VERSION=$v/" "$DEPLOY/.env"
  dc pull api
  dc up -d
  echo "✅ now on $v — check: ops.sh status"
}

main() {
  local cmd="${1:-}"; shift || true
  load_env
  case "$cmd" in
    status)                cmd_status ;;
    import)                cmd_import "$@" ;;
    refresh-golden-copies) cmd_refresh_golden_copies "$@" ;;
    refresh-psc)           cmd_refresh_psc ;;
    refresh-company-data)  cmd_refresh_company_data ;;
    gleif-update)          watch_job gleif-update; run_manage gleif-update "$@" ;;
    backup)                cmd_backup ;;
    weekly-report)         watch_job weekly-report; exec_manage weekly-report --email ;;
    prune-analytics)       watch_job prune-analytics; exec_manage prune-analytics ;;
    alerts)                exec_manage alerts "$@" ;;
    upgrade)               cmd_upgrade "$@" ;;
    install-web)           cmd_install_web "$@" ;;
    manage)                exec_manage "$@" ;;
    count)                 cmd_count "$@" ;;
    help|"") awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
    *) echo "unknown command: $cmd (ops.sh help)" >&2; exit 2 ;;
  esac
}

main "$@"
