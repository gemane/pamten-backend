#!/bin/bash
# setup-server.sh — a fresh production server, from nothing to serving, in one run.
#
#   scp setup-server.sh owlgraph.env root@<server>:
#   ssh root@<server> bash setup-server.sh owlgraph.env
#
# owlgraph.env is deploy/.env.example filled in: the domain, the release, the
# secrets, the backup target. Nothing secret lives in this script or in git.
#
# What it does, in order — every step safe to run again:
#   1  preflight      root, Debian/Ubuntu, the env file complete, RAM/disk, DNS
#   2  packages       Docker (official repo) + the few tools the scripts use,
#                     unattended security upgrades
#   3  swap + firewall  a 4 GB swapfile if there is none; ufw: 22, 80, 443 only
#   4  code           the release's deploy/ files from git (tag vAPP_VERSION)
#   5  config         deploy/.env (compose + app) and host.env (host-only), 0600
#   6  web app        the release's web tarball, checksum + domain verified
#   7  containers     pull, up, wait until every service is healthy, schema
#   8  cron           backup → GLEIF delta → weekly report → analytics pruning
#   9  data           the first import (IMPORT=full|test|none), as a systemd
#                     unit so it survives this SSH session
#
# Prerequisites it cannot do for you:
#   - a DNS A/AAAA record for DOMAIN pointing at this server (Caddy needs it for
#     the TLS certificate);
#   - a published release vX.Y.Z of BOTH repos (the tag-triggered release
#     workflow builds the API image and the web tarball), the frontend built
#     with PROD_API_URL=https://DOMAIN and the LEGAL_* variables set.
set -euo pipefail

ROOT=/opt/owlgraph
DEPLOY="$ROOT/deploy"

die()  { echo "❌ $*" >&2; exit 1; }
step() { echo; echo "### $* — $(date -u +%FT%TZ)"; }
warn() { echo "⚠️  $*" >&2; }

ENV_IN="${1:-}"
[ -n "$ENV_IN" ] || die "usage: bash setup-server.sh /path/to/owlgraph.env"
[ "$(id -u)" -eq 0 ] || die "run as root"
[ -f "$ENV_IN" ] || die "no env file at $ENV_IN"

# Keys only the host uses. They go to host.env and never into a container's
# environment (compose passes deploy/.env to the api with env_file).
HOST_KEYS="IMPORT PSC_LIMIT BACKUP_KEEP BACKUP_REMOTE GHCR_USER GHCR_TOKEN \
HC_BACKUP HC_GLEIF_UPDATE HC_WEEKLY_REPORT HC_PRUNE_ANALYTICS SKIP_DNS_CHECK"

# ── 1 preflight ──────────────────────────────────────────────────────────────
step "1/9 preflight"
. /etc/os-release
case "$ID" in debian|ubuntu) ;; *) die "Debian or Ubuntu only (found $ID)" ;; esac
grep -nE '^[A-Z_]+=.*[[:space:]]#' "$ENV_IN" \
  && die "values must not carry inline '# comments' (the lines above)" || true
set -a
# shellcheck disable=SC1090
. "$ENV_IN"
set +a
for k in DOMAIN GITHUB_OWNER APP_VERSION ARCADEDB_VERSION ARCADEDB_ROOT_PASSWORD \
         SECRET_KEY ADMIN_EMAIL ADMIN_PASSWORD; do
  [ -n "${!k:-}" ] || die "$k is empty in $ENV_IN"
done
[[ "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "APP_VERSION must be X.Y.Z (got $APP_VERSION)"
[[ "$DOMAIN" == *example.com* ]] && die "DOMAIN is still the example"
[[ "$ARCADEDB_ROOT_PASSWORD" == change-me* ]] && die "ARCADEDB_ROOT_PASSWORD is still the example"
[[ "$ARCADEDB_ROOT_PASSWORD" == *:* ]] && die "ARCADEDB_ROOT_PASSWORD must not contain ':' (ArcadeDB #7783)"
[ "${#SECRET_KEY}" -ge 32 ] || die "SECRET_KEY must be at least 32 characters (openssl rand -hex 32)"
[[ "${CORS_ORIGINS:-}" == *"$DOMAIN"* ]] || die "CORS_ORIGINS must include https://$DOMAIN"
IMPORT="${IMPORT:-none}"
case "$IMPORT" in full|test|none) ;; *) die "IMPORT must be full, test or none" ;; esac

heap_gb=$(echo "${ARCADEDB_HEAP:-8g}" | tr -dc '0-9')
ram_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
[ "$ram_gb" -ge $(( heap_gb + 3 )) ] \
  || die "${ram_gb} GB RAM is too little for a ${heap_gb} GB ArcadeDB heap plus the API (lower ARCADEDB_HEAP)"
data_root=$(dirname "${ARCADEDB_DATA:-/srv/owlgraph/arcadedb}")
mkdir -p "$data_root"
free_gb=$(df -BG --output=avail "$data_root" | tail -1 | tr -dc '0-9')
need_gb=20; [ "$IMPORT" = full ] && need_gb=120
[ "$free_gb" -ge "$need_gb" ] || die "${free_gb} GB free under $data_root; IMPORT=$IMPORT needs ~${need_gb} GB"
echo "  $PRETTY_NAME, ${ram_gb} GB RAM, ${free_gb} GB free, import: $IMPORT"

if [ -z "${SKIP_DNS_CHECK:-}" ]; then
  me=$(curl -fsS -4 -m 10 https://ifconfig.co 2>/dev/null || true)
  there=$(getent ahostsv4 "$DOMAIN" | awk '{print $1; exit}')
  [ -n "$me" ] && [ "$me" = "$there" ] \
    || die "$DOMAIN resolves to '${there:-nothing}', this server is '${me:-unknown}' — fix DNS first (SKIP_DNS_CHECK=1 to override)"
  echo "  DNS: $DOMAIN → $me ✓"
fi

# ── 2 packages ───────────────────────────────────────────────────────────────
step "2/9 packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl gnupg git cron jq unzip rsync ufw unattended-upgrades >/dev/null
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
if [ ! -f /etc/apt/sources.list.d/docker.sources ]; then
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $VERSION_CODENAME
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -qq
fi
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
systemctl enable --now docker cron >/dev/null
echo 'Unattended-Upgrade::Automatic-Reboot "false";' > /etc/apt/apt.conf.d/52owlgraph-no-reboot
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null
echo "  $(docker --version), $(docker compose version | head -1)"

# ── 3 swap + firewall ────────────────────────────────────────────────────────
step "3/9 swap + firewall"
if ! swapon --show | grep -q .; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "  4 GB swapfile — an OOM safety net for the import, not memory to plan with"
fi
# NB: Docker publishes ports past ufw (its own iptables chain). Only Caddy
# publishes anything (80/443); ArcadeDB is never published — that, not ufw,
# is what keeps the database off the internet.
ufw default deny incoming >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443 >/dev/null
ufw --force enable >/dev/null
echo "  ufw: $(ufw status | grep -c ALLOW) allow rules (ssh, http, https)"

# ── 4 code ───────────────────────────────────────────────────────────────────
step "4/9 the release's deploy files (v$APP_VERSION)"
SRC="$ROOT/src"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch -q --depth 1 origin "refs/tags/v$APP_VERSION:refs/tags/v$APP_VERSION"
  git -C "$SRC" -c advice.detachedHead=false checkout -q "v$APP_VERSION"
else
  git clone -q --depth 1 --branch "v$APP_VERSION" \
    "https://github.com/$GITHUB_OWNER/pamten-backend.git" "$SRC" \
    || die "no tag v$APP_VERSION in $GITHUB_OWNER/pamten-backend — publish the release first"
fi
mkdir -p "$DEPLOY"
install -m 0644 "$SRC/deploy/docker-compose.yml" "$SRC/deploy/Caddyfile" "$DEPLOY/"
install -m 0755 "$SRC/deploy/ops.sh" "$DEPLOY/ops.sh"
ln -sf "$DEPLOY/ops.sh" /usr/local/bin/owlgraph
echo "  $DEPLOY (ops: \`owlgraph help\`)"

# ── 5 config ─────────────────────────────────────────────────────────────────
step "5/9 config"
umask 077
: > "$ROOT/host.env.new"; : > "$DEPLOY/.env.new"
while IFS= read -r line; do
  key="${line%%=*}"
  if [[ "$line" =~ ^[A-Z_][A-Z0-9_]*= ]] && [[ " $HOST_KEYS " == *" $key "* ]]; then
    echo "$line" >> "$ROOT/host.env.new"
  else
    echo "$line" >> "$DEPLOY/.env.new"
  fi
done < "$ENV_IN"
grep -q '^DATA_DIR=' "$DEPLOY/.env.new" || echo "DATA_DIR=/srv/owlgraph/data" >> "$DEPLOY/.env.new"
mv "$ROOT/host.env.new" "$ROOT/host.env"
mv "$DEPLOY/.env.new" "$DEPLOY/.env"
umask 022
set -a; . "$DEPLOY/.env"; set +a
mkdir -p "$ARCADEDB_DATA/databases" "$ARCADEDB_DATA/backups" "$WEB_DIR" "$DATA_DIR"
# the arcadedb image runs as uid 1000; it must own its data directory
chown -R 1000:1000 "$ARCADEDB_DATA"
echo "  deploy/.env + host.env written (0600); data under $(dirname "$ARCADEDB_DATA")"

# ── 6 web app ────────────────────────────────────────────────────────────────
step "6/9 web app"
"$DEPLOY/ops.sh" install-web "$APP_VERSION"

# ── 7 containers ─────────────────────────────────────────────────────────────
step "7/9 containers"
if [ -n "${GHCR_TOKEN:-}" ]; then
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-$GITHUB_OWNER}" --password-stdin >/dev/null
fi
(cd "$DEPLOY" && docker compose pull -q) \
  || die "pulling failed — is ghcr.io/$GITHUB_OWNER/pamten-backend:$APP_VERSION published (and public, or GHCR_TOKEN set)? arcadedata/arcadedb:$ARCADEDB_VERSION?"
(cd "$DEPLOY" && docker compose up -d)
echo -n "  waiting for every service to be healthy"
for _ in $(seq 1 60); do
  unhealthy=$(cd "$DEPLOY" && docker compose ps --format '{{.Service}} {{.Health}}' | grep -vc ' healthy$' || true)
  [ "$unhealthy" -eq 0 ] && break
  echo -n "."; sleep 5
done
echo
(cd "$DEPLOY" && docker compose ps)
[ "$unhealthy" -eq 0 ] || die "not every service became healthy in 5 min — docker compose logs"
"$DEPLOY/ops.sh" manage init-schema
curl -fsS "https://$DOMAIN/health" >/dev/null && echo "  https://$DOMAIN/health ✓" \
  || warn "https://$DOMAIN/health does not answer yet (certificate still being issued?)"

# ── 8 cron ───────────────────────────────────────────────────────────────────
step "8/9 cron"
# Times in UTC. The backup runs BEFORE the delta, so there is always a copy of
# the state the delta started from. The import lock keeps the delta from
# writing into a running import. Further pipelines (PSC refresh, alerts) start
# manual and are scheduled once they have run by hand.
cat > /etc/cron.d/owlgraph <<EOF
# Owlgraph — written by setup-server.sh. Logs: /var/log/owlgraph/
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 2 * * *  root $DEPLOY/ops.sh backup          >> /var/log/owlgraph/backup.log 2>&1
0 3 * * *  root $DEPLOY/ops.sh gleif-update    >> /var/log/owlgraph/gleif-update.log 2>&1
0 6 * * 1  root $DEPLOY/ops.sh weekly-report   >> /var/log/owlgraph/weekly-report.log 2>&1
30 4 * * * root $DEPLOY/ops.sh prune-analytics >> /var/log/owlgraph/prune-analytics.log 2>&1
EOF
mkdir -p /var/log/owlgraph
cat > /etc/logrotate.d/owlgraph <<'EOF'
/var/log/owlgraph/*.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
}
EOF
echo "  /etc/cron.d/owlgraph: backup 02:00, GLEIF delta 03:00, analytics 04:30, weekly report Mon 06:00 (UTC)"

# ── 9 data ───────────────────────────────────────────────────────────────────
step "9/9 data"
entities=$("$DEPLOY/ops.sh" count Entity 2>/dev/null | tail -1 || echo "?")
if [ "$IMPORT" = none ]; then
  echo "  IMPORT=none — load data later with: owlgraph import full"
elif systemctl is-active --quiet owlgraph-import; then
  echo "  an import is already running: journalctl -fu owlgraph-import"
elif [ "$entities" != "0" ] && [ "$entities" != "?" ]; then
  echo "  the database already holds $entities entities — not importing again"
else
  systemd-run --unit owlgraph-import --collect \
    --property=StandardOutput=append:/var/log/owlgraph/import.log \
    --property=StandardError=append:/var/log/owlgraph/import.log \
    "$DEPLOY/ops.sh" import "$IMPORT" >/dev/null
  echo "  the $IMPORT import is running as the systemd unit owlgraph-import"
  echo "  follow it: tail -f /var/log/owlgraph/import.log"
  [ "$IMPORT" = full ] && echo "  (a full import takes many hours — the site serves meanwhile)"
fi

echo
echo "✅ https://$DOMAIN is set up on v$APP_VERSION."
echo "   owlgraph status       containers, /health, import lock"
echo "   owlgraph backup       take a backup now (also nightly)"
echo "   owlgraph upgrade X.Y.Z"
echo "   Still to do by hand: the outside uptime check (docs/operations.md, Monitoring),"
echo "   and a restore drill once the first backup exists."
