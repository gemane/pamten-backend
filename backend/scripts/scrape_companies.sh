#!/usr/bin/env bash
#
# scrape_companies.sh — scrape the built-in company list via the Pamten API.
#
# Logs in as a contributor/admin, then calls POST /scraper/run-all for each
# company. run-all triggers every scraper source that is enabled server-side
# (Wikidata + SEC EDGAR + OpenCorporates); disabled sources are skipped and
# reported as "disabled". The master switch SCRAPER_ENABLED=true must be set on
# the server or every call returns 403.
#
# The default company list mirrors backend/seed.py.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#   API_EMAIL=you@example.com API_PASSWORD=secret ./scrape_companies.sh
#
#   # Point at a deployed instance and follow 2 subsidiary levels:
#   API_BASE=https://ownership-platform-api.onrender.com \
#   API_EMAIL=you@example.com API_PASSWORD=secret DEPTH=2 \
#     ./scrape_companies.sh
#
#   # Scrape a custom set instead of the built-in list:
#   API_EMAIL=... API_PASSWORD=... ./scrape_companies.sh "Apple" "Nestlé"
#
# ── Configuration (environment variables) ────────────────────────────────────
#   API_BASE      Base URL of the API            (default: http://localhost:8000)
#   API_EMAIL     Login email                    (required)
#   API_PASSWORD  Login password                 (required)
#   DEPTH         Wikidata subsidiary depth 0–3  (default: 1)
#   SLEEP         Seconds to wait between calls  (default: 1)
#   ENDPOINT      run-all | run | sec-edgar/run  (default: run-all)
#
set -euo pipefail

API_BASE="${API_BASE:-http://localhost:8000}"
API_BASE="${API_BASE%/}"          # strip any trailing slash
DEPTH="${DEPTH:-1}"
SLEEP="${SLEEP:-1}"
ENDPOINT="${ENDPOINT:-run-all}"

# ── Company list (mirrors backend/seed.py). Override by passing names as args. ─
DEFAULT_COMPANIES=(
  "AB InBev"
  "Heineken"
  "Carlsberg"
  "Nestlé"
  "Unilever"
  "Bertelsmann"
  "Axel Springer"
  "Alphabet"
  "Microsoft"
  "Apple"
  "News Corp"
  "Grupo Televisa"
  "Embraer"
  "MercadoLibre"
  "Grupo Bimbo"
  "SoftBank"
  "Samsung Electronics"
  "Tata Group"
  "Alibaba Group"
  "CITIC Group"
  "Saudi Aramco"
  "Mubadala Investment Company"
  "Al Jazeera Media Network"
  "Naspers"
  "Dangote Group"
  "MTN Group"
  "Wesfarmers"
  "Nine Entertainment"
)

if [[ $# -gt 0 ]]; then
  COMPANIES=("$@")
else
  COMPANIES=("${DEFAULT_COMPANIES[@]}")
fi

# ── Preconditions ─────────────────────────────────────────────────────────────
command -v curl >/dev/null 2>&1 || { echo "error: curl is required" >&2; exit 1; }
HAVE_JQ=0
command -v jq >/dev/null 2>&1 && HAVE_JQ=1

if [[ -z "${API_EMAIL:-}" || -z "${API_PASSWORD:-}" ]]; then
  echo "error: set API_EMAIL and API_PASSWORD environment variables" >&2
  echo "  e.g. API_EMAIL=you@example.com API_PASSWORD=secret $0" >&2
  exit 1
fi

# ── URL-encode a string (for the ?company= query parameter) ───────────────────
urlencode() {
  local s="$1" out="" c i
  for (( i=0; i<${#s}; i++ )); do
    c="${s:$i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      *) printf -v c '%%%02X' "'$c" ; out+="$c" ;;
    esac
  done
  printf '%s' "$out"
}

# ── Extract a JSON string field without requiring jq ──────────────────────────
json_field() {  # json_field <body> <key>
  if [[ $HAVE_JQ -eq 1 ]]; then
    printf '%s' "$1" | jq -r ".${2} // empty"
  else
    printf '%s' "$1" | grep -o "\"${2}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
      | head -n1 | sed -E "s/.*:[[:space:]]*\"([^\"]*)\"/\1/"
  fi
}

# ── Log in and capture a bearer token ─────────────────────────────────────────
echo "Logging in to ${API_BASE} as ${API_EMAIL} ..."
login_body=$(curl -sS -X POST "${API_BASE}/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"${API_EMAIL}\",\"password\":\"${API_PASSWORD}\"}") || {
    echo "error: login request failed" >&2; exit 1; }

TOKEN="$(json_field "$login_body" access_token)"
if [[ -z "$TOKEN" ]]; then
  echo "error: login failed — no access_token in response:" >&2
  echo "$login_body" >&2
  exit 1
fi
echo "Authenticated."
echo

# ── Build the endpoint query and scrape each company ──────────────────────────
total=${#COMPANIES[@]}
ok=0; failed=0; i=0

echo "Scraping ${total} companies via /scraper/${ENDPOINT} (depth=${DEPTH})"
echo "────────────────────────────────────────────────────────────"

for company in "${COMPANIES[@]}"; do
  i=$((i + 1))
  enc="$(urlencode "$company")"
  url="${API_BASE}/scraper/${ENDPOINT}?company=${enc}"
  # run-all and run accept a depth parameter; the single-source SEC endpoint does not.
  [[ "$ENDPOINT" == "run-all" || "$ENDPOINT" == "run" ]] && url+="&depth=${DEPTH}"

  printf "[%2d/%2d] %-30s " "$i" "$total" "$company"

  http_code=$(curl -sS -o /tmp/scrape_resp.$$ -w '%{http_code}' \
    -X POST "$url" -H "Authorization: Bearer ${TOKEN}") || http_code="000"
  body="$(cat /tmp/scrape_resp.$$ 2>/dev/null || true)"
  rm -f /tmp/scrape_resp.$$

  status="$(json_field "$body" status)"
  if [[ "$http_code" == "200" ]]; then
    ok=$((ok + 1))
    echo "OK  (status=${status:-ok})"
  else
    failed=$((failed + 1))
    detail="$(json_field "$body" detail)"
    echo "FAIL (HTTP ${http_code}) ${detail}"
  fi

  # Be polite to the upstream APIs between companies.
  [[ $i -lt $total ]] && sleep "$SLEEP"
done

echo "────────────────────────────────────────────────────────────"
echo "Done: ${ok} succeeded, ${failed} failed, ${total} total."
[[ $failed -eq 0 ]]
