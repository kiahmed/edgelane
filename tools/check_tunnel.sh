#!/usr/bin/env bash
#
# check_tunnel.sh — verify the production backend is reachable end-to-end the
# same way the deployed Vercel frontend reaches it:
#
#   browser ─ anon read ─▶ Supabase app_config.api_base   (the published URL)
#           ─ HTTPS ──────▶ *.trycloudflare.com (quick tunnel)
#           ─ tunnel ─────▶ edgelane-backend container
#
# It checks: local container health, the published api_base pointer, the tunnel
# /status, CORS for the Vercel origin, and the /session/anon (Turnstile) path.
# Read-only; safe to run anytime.
#
#   ./tools/check_tunnel.sh [origin]
#       origin  Vercel origin to test CORS against
#               (default: https://edgelane-matrix.vercel.app)

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/deploy/.env"
ORIGIN="${1:-https://edgelane-matrix.vercel.app}"
TIMEOUT=20

if [ -t 1 ]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; DIM='\033[2m'; NC='\033[0m'
else RED=''; GREEN=''; YELLOW=''; DIM=''; NC=''; fi
pass() { printf "${GREEN}✓${NC} %s\n" "$*"; }
fail() { printf "${RED}✗${NC} %s\n" "$*"; FAILED=1; }
warn() { printf "${YELLOW}!${NC} %s\n" "$*"; }
info() { printf "${DIM}  %s${NC}\n" "$*"; }
FAILED=0

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE"; exit 2; }
set -a; . "$ENV_FILE"; set +a
: "${SUPABASE_URL:?SUPABASE_URL not set in deploy/.env}"
: "${SUPABASE_ANON_KEY:?SUPABASE_ANON_KEY not set in deploy/.env}"

echo "── EdgeLane tunnel health ──────────────────────────────"

# 1) Local container up + healthy
if command -v docker >/dev/null 2>&1; then
  st=$(docker inspect -f '{{.State.Status}}{{if .State.Health}}/{{.State.Health.Status}}{{end}}' edgelane-backend 2>/dev/null)
  case "$st" in
    running/healthy) pass "container edgelane-backend: $st" ;;
    running*)        warn "container edgelane-backend: $st (not yet healthy)" ;;
    "")              warn "container edgelane-backend: not found (running remotely?)" ;;
    *)               fail "container edgelane-backend: $st" ;;
  esac
else
  warn "docker not on this host — skipping container check"
fi

# 2) Published api_base pointer (what the browser reads via the anon key)
URL=$(curl -s -m "$TIMEOUT" \
  "$SUPABASE_URL/rest/v1/app_config?select=value&key=eq.api_base" \
  -H "apikey: $SUPABASE_ANON_KEY" -H "Authorization: Bearer $SUPABASE_ANON_KEY" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['value'] if d else '')" 2>/dev/null)
if [ -n "$URL" ]; then
  pass "Supabase app_config.api_base published"
  info "$URL"
else
  fail "no api_base in Supabase — cloudflared never published (frontend can't find the backend)"
  echo; echo "Result: FRONTEND CANNOT DISCOVER BACKEND"; exit 1
fi

# 2b) Vercel Edge Config pointer — what the DEPLOYED Simmer SPA reads via
#     /api/config (the Simmer browser never touches Supabase). cloudflared
#     PATCHes this on every restart; it should match the Supabase value.
if [ -n "${VERCEL_API_TOKEN:-}" ] && [ -n "${EDGE_CONFIG_ID:-}" ]; then
  EURL=$(curl -s -m "$TIMEOUT" \
    "https://api.vercel.com/v1/edge-config/${EDGE_CONFIG_ID}/items${VERCEL_TEAM_ID:+?teamId=${VERCEL_TEAM_ID}}" \
    -H "Authorization: Bearer $VERCEL_API_TOKEN" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);L=d if isinstance(d,list) else [];print(next((i['value'] for i in L if i.get('key')=='api_base'),''))" 2>/dev/null)
  if [ -z "$EURL" ]; then
    fail "Edge Config has no api_base — deployed Simmer SPA can't discover the backend (cloudflared Edge Config publish failed?)"
  elif [ "$EURL" = "$URL" ]; then
    pass "Vercel Edge Config api_base matches Supabase"
    info "$EURL"
  else
    warn "Edge Config api_base differs from Supabase (tunnel rotation in progress, or a stale token stopped the last publish)"
    info "edge:     $EURL"
    info "supabase: $URL"
  fi
else
  warn "Edge Config not configured (VERCEL_API_TOKEN/EDGE_CONFIG_ID unset) — Simmer tunnel self-heal is OFF"
fi

# 3) Tunnel reaches the backend
read -r code time < <(curl -s -m "$TIMEOUT" -o /dev/null -w "%{http_code} %{time_total}" "$URL/status")
if [ "$code" = "200" ]; then pass "tunnel /status 200 (${time}s)"
else fail "tunnel /status $code — tunnel up but backend unreachable/erroring"; fi

# 4) CORS for the Vercel origin (browser preflight)
acao=$(curl -s -m "$TIMEOUT" -X OPTIONS "$URL/session/anon" \
  -H "Origin: $ORIGIN" -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" -D - -o /dev/null 2>/dev/null \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')
if [ "$acao" = "$ORIGIN" ]; then pass "CORS allows $ORIGIN"
else fail "CORS does NOT allow $ORIGIN (add it to CORS_ALLOW_ORIGINS, then restart backend)"; fi

# 5) Turnstile / anon-session path is wired (dummy token must be REJECTED — that
#    proves the endpoint + Cloudflare siteverify round-trip work; a 200 here
#    would mean Turnstile verification is being skipped).
code=$(curl -s -m "$TIMEOUT" -o /dev/null -w "%{http_code}" -X POST "$URL/session/anon" \
  -H "Origin: $ORIGIN" -H "Content-Type: application/json" \
  -d '{"turnstile_token":"check-tunnel-dummy"}')
case "$code" in
  403) pass "/session/anon rejects a bad Turnstile token (verification active)" ;;
  200) warn "/session/anon accepted a DUMMY token — Turnstile verification is OFF (AUTH disabled or dev mode?)" ;;
  *)   fail "/session/anon returned $code (expected 403)" ;;
esac

echo "────────────────────────────────────────────────────────"
if [ "$FAILED" = 0 ]; then
  pass "tunnel is functional — backend reachable from the deployed frontend"
  echo
  echo "If the landing-page Turnstile widget still 'fails to reach', the cause is"
  echo "CLIENT-SIDE (this script can't test it): in the Cloudflare Turnstile"
  echo "dashboard, the widget's allowed hostnames must include the deploy domain"
  echo "  ${ORIGIN#https://}"
  echo "and the site key baked into the UI must match that widget."
  exit 0
else
  echo "Result: one or more checks FAILED (see ✗ above)"
  exit 1
fi
