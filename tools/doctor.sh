#!/usr/bin/env bash
# make doctor — verify everything needed to BUILD and DEPLOY EdgeLane is present.
# Lists ONLY what's missing (silent on what's fine), with a fix hint each. Exits
# non-zero if anything required is missing. Advisories (!) don't fail the check.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/deploy/.env"
BACKEND_CONFIG="$ROOT/edgelane_market.config"

if [ -t 1 ]; then RED='\033[0;31m'; YEL='\033[0;33m'; GRN='\033[0;32m'; NC='\033[0m'
else RED=''; YEL=''; GRN=''; NC=''; fi

miss=0
bad()  { printf "${RED}✗${NC} %s\n" "$*"; miss=$((miss + 1)); }
warn() { printf "${YEL}!${NC} %s\n" "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ── Backend build/run (Docker) ──────────────────────────────────────────────
if have docker; then
  docker info >/dev/null 2>&1 || bad "docker daemon not running — start Docker Desktop / 'sudo systemctl start docker'"
  docker compose version >/dev/null 2>&1 || bad "docker compose plugin missing — install 'docker-compose-plugin'"
else
  bad "docker not installed — https://docs.docker.com/engine/install/"
fi

# ── Tooling used by deploy.sh / db_push ─────────────────────────────────────
have python3 || bad "python3 not installed — 'sudo apt-get install -y python3'"
have git     || bad "git not installed — 'sudo apt-get install -y git'"
have curl    || bad "curl not installed — 'sudo apt-get install -y curl'"

# ── Frontend (Vercel) ───────────────────────────────────────────────────────
have node   || bad "node not installed — run 'make vercel-setup' (installs Node + Vercel CLI)"
if have vercel; then
  vercel whoami >/dev/null 2>&1 || bad "not logged in to Vercel — run 'make vercel-setup' (or 'vercel login')"
else
  bad "vercel CLI not installed — run 'make vercel-setup'"
fi

# ── Config files ────────────────────────────────────────────────────────────
[ -f "$ENV_FILE" ]       || bad "missing deploy/.env — 'cp deploy/.env.example deploy/.env' then fill in"
[ -f "$BACKEND_CONFIG" ] || bad "missing edgelane_market.config — copy the .example and fill in Tradier token"

# ── Required deploy/.env keys (non-empty) ───────────────────────────────────
keyval() { [ -f "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d "\r\"' "; }
if [ -f "$ENV_FILE" ]; then
  for k in SUPABASE_URL SUPABASE_SERVICE_KEY SUPABASE_ANON_KEY \
           SUPABASE_PROJECT_REF SUPABASE_ACCESS_TOKEN EDGELANE_TURNSTILE_SITE_KEY; do
    [ -n "$(keyval "$k")" ] || bad "deploy/.env: $k is empty"
  done
fi

# ── Advisories (won't fail doctor, but bite you in prod) ────────────────────
if [ -f "$BACKEND_CONFIG" ]; then
  grep -qE '^AUTH_ENABLED=true' "$BACKEND_CONFIG" \
    || warn "edgelane_market.config: AUTH_ENABLED is not 'true' — JWT/teaser/rate-limit gates are OFF (open API). Set true for a public deploy."
  grep -qE '^DEVMODE=false' "$BACKEND_CONFIG" \
    || warn "edgelane_market.config: DEVMODE is not 'false' — using sandbox Tradier. Set false for production."
fi

echo
if [ "$miss" -eq 0 ]; then
  printf "${GRN}✓ all build/deploy dependencies present${NC}\n"
  exit 0
else
  printf "${RED}%d required item(s) missing above — fix those before deploying.${NC}\n" "$miss"
  exit 1
fi
