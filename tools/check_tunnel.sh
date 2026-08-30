#!/usr/bin/env bash
#
# check_tunnel.sh — verify the production backend is reachable end-to-end the
# same way the deployed Vercel frontend reaches it:
#
#   browser ─ HTTPS ─▶ edge.facades.trade (named tunnel, permanent hostname)
#           ─ tunnel ─▶ edgelane-backend container
#
# It checks: local container health, the tunnel /status, CORS for the frontend
# origin, and the /session/anon (Turnstile) path. Read-only; safe to run anytime.
#
# The backend URL is EDGELANE_API_BASE from deploy/.env — the same value baked
# into both SPAs. There is no pointer to look up: the hostname is permanent.
#
#   ./tools/check_tunnel.sh [origin]
#       origin  frontend origin to test CORS against
#               (default: https://matrix.facades.trade)

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/deploy/.env"
ORIGIN="${1:-https://matrix.facades.trade}"
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
: "${EDGELANE_API_BASE:?EDGELANE_API_BASE not set in deploy/.env}"
URL="${EDGELANE_API_BASE%/}"

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

# 2) The backend URL under test — baked, not discovered
pass "backend URL (baked into both SPAs)"
info "$URL"

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
