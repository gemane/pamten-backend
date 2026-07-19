#!/usr/bin/env bash
#
# arcadedb-it.sh — spin up a throwaway ArcadeDB for the integration tests.
#
#   ./arcadedb-it.sh start    # run the container, wait until ready, print env exports
#   ./arcadedb-it.sh stop     # remove the container and its anonymous volumes
#   ./arcadedb-it.sh status   # show container state + readiness
#   ./arcadedb-it.sh test     # start, run `pytest tests/integration`, then stop
#
# Reads no secrets: the root password is a throwaway (the DBs are ephemeral —
# each test creates a `pamten_it_<random>` database and drops it on teardown).
# See tests/integration/README.md for the full explanation.
set -euo pipefail

IMAGE="${ARCADEDB_IT_IMAGE:-arcadedata/arcadedb:26.7.2}"
NAME="${ARCADEDB_IT_NAME:-arcadedb-it}"
PORT="${ARCADEDB_IT_PORT:-2480}"
PASS="${ARCADEDB_IT_PASSWORD:-RootPass123!}"   # must satisfy ArcadeDB's policy (upper+lower+digit+symbol)
URL="http://localhost:${PORT}"

# docker wrapper: use `docker` directly, but if the daemon socket rejects us for
# lack of group membership (common right after `usermod -aG docker` without a
# re-login), transparently retry via `sg docker`.
dk() {
  if docker "$@" 2>/tmp/.arcadedb-it-dk.err; then return 0; fi
  if grep -qi "permission denied" /tmp/.arcadedb-it-dk.err 2>/dev/null && command -v sg >/dev/null; then
    sg docker -c "docker $(printf '%q ' "$@")"
  else
    cat /tmp/.arcadedb-it-dk.err >&2
    return 1
  fi
}

wait_ready() {
  for _ in $(seq 1 60); do
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" -u "root:${PASS}" "${URL}/api/v1/ready" 2>/dev/null || true)
    if [ "$code" = "204" ] || [ "$code" = "200" ]; then return 0; fi
    sleep 1
  done
  echo "ArcadeDB did not become ready at ${URL} within 60s" >&2
  return 1
}

print_env() {
  cat <<EOF
# ArcadeDB ready at ${URL}. Export these, then run the tests:
export ARCADEDB_IT_URL=${URL}
export ARCADEDB_IT_USERNAME=root
export ARCADEDB_IT_PASSWORD='${PASS}'
EOF
}

cmd_start() {
  dk rm -f "${NAME}" >/dev/null 2>&1 || true
  dk run -d --name "${NAME}" -p "${PORT}:2480" \
     -e JAVA_OPTS="-Darcadedb.server.rootPassword=${PASS}" "${IMAGE}" >/dev/null
  echo "Started ${NAME} (${IMAGE}); waiting for readiness…"
  wait_ready
  print_env
}

cmd_stop() {
  dk rm -f "${NAME}" >/dev/null 2>&1 && echo "Removed container ${NAME}." || echo "No container ${NAME}."
  local vols
  vols=$(dk volume ls -qf dangling=true 2>/dev/null || true)
  if [ -n "${vols}" ]; then
    # shellcheck disable=SC2086
    dk volume rm ${vols} >/dev/null 2>&1 && echo "Removed dangling volumes."
  fi
}

cmd_status() {
  dk ps -a --filter "name=${NAME}" --format '{{.Names}}: {{.Status}}' || true
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" -u "root:${PASS}" "${URL}/api/v1/ready" 2>/dev/null || echo "000")
  echo "readiness ${URL}/api/v1/ready → HTTP ${code} (204/200 = ready)"
}

cmd_test() {
  cmd_start
  local rc=0 backend py
  backend="$(cd "$(dirname "$0")/../.." && pwd)"
  # Prefer the project venv if present, else whatever python is on PATH (CI).
  if [ -x "${backend}/venv/bin/python" ]; then py="${backend}/venv/bin/python"; else py="python"; fi
  ( cd "${backend}" \
    && ARCADEDB_IT_URL="${URL}" ARCADEDB_IT_USERNAME=root ARCADEDB_IT_PASSWORD="${PASS}" \
       "${py}" -m pytest tests/integration -v ) || rc=$?
  cmd_stop
  return "${rc}"
}

case "${1:-}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  test)   cmd_test ;;
  *) echo "usage: $0 {start|stop|status|test}" >&2; exit 2 ;;
esac
