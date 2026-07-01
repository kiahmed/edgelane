#!/usr/bin/env bash
#
# deploy.sh — deploy EdgeLane to production.
#
#   Frontend (market UI)        -> Vercel              -> https://<project>.vercel.app
#   Backend  (FastAPI + Torque) -> local Docker host   -> Cloudflare tunnel (HTTPS)
#
# The Torque order-builder ships WITH the backend (served at /torque); only the
# market dashboard (market/ui/index.html) goes to Vercel. The legacy single-file
# edge_lane.html is NOT deployed.
#
# Run `./deploy.sh --help` for usage. Normally invoked via the Makefile:
#   make deploy-be      backend only   (DEPLOY=local_container)
#   make deploy-fe      frontend only  (Vercel)
#   make deploy-prod    both

set -euo pipefail

# ----------------------------------------------------------------------------
# Paths + config
# ----------------------------------------------------------------------------
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$ROOT_DIR/deploy"
TOOLS_DIR="$ROOT_DIR/tools"
DEPLOY_ENV="$DEPLOY_DIR/.env"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
BACKEND_CONFIG="$ROOT_DIR/edgelane_market.config"
UI_SRC="$ROOT_DIR/market/ui"
DIST_DIR="$ROOT_DIR/dist"

# Load deploy/.env if present (tunnel token, image, Vercel project, API base).
if [ -f "$DEPLOY_ENV" ]; then
  set -a; . "$DEPLOY_ENV"; set +a
fi

# Where the backend lands. 'local_container' = docker compose on this host
# behind a Cloudflare tunnel. 'cloud' is reserved for a future hosted target.
DEPLOY_TARGET="${DEPLOY_TARGET:-local_container}"
VERCEL_PROJECT="${VERCEL_PROJECT:-edgelane-matrix}"
EDGELANE_API_BASE="${EDGELANE_API_BASE:-}"
COMPOSE="docker compose -f $COMPOSE_FILE --env-file $DEPLOY_ENV"

# ----------------------------------------------------------------------------
# Flags
# ----------------------------------------------------------------------------
DO_FRONTEND=false
DO_BACKEND=false
DRY_RUN=false
ASSUME_YES=false
SKIP_DB=false

# ----------------------------------------------------------------------------
# Pretty output
# ----------------------------------------------------------------------------
if [ -t 1 ]; then
  BOLD='\033[1m'; DIM='\033[2m'; RED='\033[0;31m'; GREEN='\033[0;32m'
  YELLOW='\033[0;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
  BOLD=''; DIM=''; RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi
info()  { printf "${BLUE}==>${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}!${NC} %s\n" "$*"; }
die()   { printf "${RED}✗ %s${NC}\n" "$*" >&2; exit 1; }

# Run a command, or just print it in dry-run mode.
run() {
  if $DRY_RUN; then
    printf "${DIM}[dry-run]${NC} %s\n" "$*"
  else
    printf "${DIM}\$ %s${NC}\n" "$*"
    "$@"
  fi
}

usage() {
  cat <<EOF
${BOLD}deploy.sh${NC} — deploy EdgeLane (frontend → Vercel, backend → Docker + Cloudflare tunnel)

${BOLD}USAGE${NC}
  ./deploy.sh [targets] [options]

${BOLD}TARGETS${NC} (default: both)
  -f, --frontend          Deploy only the market UI (Vercel)
  -b, --backend           Deploy only the backend (Docker + tunnel)
      (no target)         Deploy both

${BOLD}OPTIONS${NC}
  -t, --target <name>     Backend target: local_container (default) | cloud
  -n, --dry-run           Print every command without running it
  -y, --yes               Don't prompt for confirmation
      --skip-db           Skip the Supabase migration push (full deploy only)
  -h, --help              Show this help

${BOLD}CONFIG${NC} (deploy/.env or env overrides)
  DEPLOY_TARGET=$DEPLOY_TARGET
  VERCEL_PROJECT=$VERCEL_PROJECT
  EDGELANE_API_BASE=${EDGELANE_API_BASE:-<unset>}

${BOLD}EXAMPLES${NC}
  ./deploy.sh -n                 # dry-run the full deploy
  ./deploy.sh -b                 # backend only (local container + tunnel)
  ./deploy.sh -f                 # frontend only (Vercel)
  ./deploy.sh -b -t cloud        # (reserved) hosted backend target

${BOLD}PREREQS${NC}
  backend:   docker + docker compose; deploy/.env with SUPABASE_URL +
             SUPABASE_SERVICE_KEY (quick tunnel publishes its URL there);
             edgelane_market.config with production Tradier token (DEVMODE=false)
  frontend:  vercel CLI (npm i -g vercel) + 'vercel login'. EDGELANE_API_BASE is
             optional (blank = discover the backend URL at runtime via Supabase)
EOF
}

# ----------------------------------------------------------------------------
# Parse args
# ----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    -f|--frontend) DO_FRONTEND=true ;;
    -b|--backend)  DO_BACKEND=true ;;
    -t|--target)   shift; DEPLOY_TARGET="${1:-}"; [ -n "$DEPLOY_TARGET" ] || die "--target needs a value" ;;
    -n|--dry-run)  DRY_RUN=true ;;
    -y|--yes)      ASSUME_YES=true ;;
    --skip-db)     SKIP_DB=true ;;
    -h|--help)     usage; exit 0 ;;
    *) die "unknown option: $1  (try --help)" ;;
  esac
  shift
done

# No explicit target => do both.
if ! $DO_FRONTEND && ! $DO_BACKEND; then DO_FRONTEND=true; DO_BACKEND=true; fi

confirm() {
  $ASSUME_YES && return 0
  $DRY_RUN && return 0
  printf "${YELLOW}%s${NC} [y/N] " "$1"
  read -r ans
  case "$ans" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

need() {
  command -v "$1" >/dev/null 2>&1 && return 0
  $DRY_RUN && { warn "$1 not found (ignored in dry-run) — $2"; return 0; }
  die "$1 not found — $2"
}

# ----------------------------------------------------------------------------
# Backend: local Docker container behind a Cloudflare tunnel
# ----------------------------------------------------------------------------
deploy_backend_local_container() {
  need docker "install Docker + docker compose"

  [ -f "$COMPOSE_FILE" ] || die "missing $COMPOSE_FILE"
  if [ ! -f "$BACKEND_CONFIG" ]; then
    $DRY_RUN && warn "missing $BACKEND_CONFIG (ignored in dry-run)" \
             || die "missing $BACKEND_CONFIG — copy edgelane_market.config.example and fill in Tradier token"
  fi
  if [ ! -f "$DEPLOY_ENV" ]; then
    $DRY_RUN && warn "missing $DEPLOY_ENV (ignored in dry-run)" \
             || die "missing $DEPLOY_ENV — copy deploy/.env.example and fill in CF_TUNNEL_TOKEN"
  fi

  # Guardrails: production backend should run live Tradier, market-hours gated.
  if [ -f "$BACKEND_CONFIG" ] && grep -q '^DEVMODE=true' "$BACKEND_CONFIG"; then
    warn "edgelane_market.config has DEVMODE=true (sandbox Tradier). Production usually wants DEVMODE=false."
    confirm "Deploy backend with DEVMODE=true anyway?" || die "aborted — set DEVMODE=false in edgelane_market.config"
  fi
  # Quick-tunnel mode (default): no CF token needed. The cloudflared publisher
  # writes the rotating *.trycloudflare.com URL to Supabase app_config.api_base,
  # so it needs SUPABASE_URL + SUPABASE_SERVICE_KEY. Without them the frontend
  # can't discover the backend URL.
  if [ -f "$DEPLOY_ENV" ] && { ! grep -qE '^SUPABASE_URL=.+' "$DEPLOY_ENV" || ! grep -qE '^SUPABASE_SERVICE_KEY=.+' "$DEPLOY_ENV"; }; then
    $DRY_RUN && warn "SUPABASE_URL/SUPABASE_SERVICE_KEY missing in deploy/.env (ignored in dry-run)" \
             || die "SUPABASE_URL + SUPABASE_SERVICE_KEY must be set in deploy/.env so the tunnel can publish its URL (see deploy/.env.example)"
  fi

  info "Deploying backend -> local Docker container + Cloudflare tunnel"
  confirm "Build and (re)start the EdgeLane backend stack on this host?" || die "aborted"
  run $COMPOSE build
  run $COMPOSE up -d
  ok "backend stack up — API on http://127.0.0.1:${EDGELANE_PORT:-8789}, public via the Cloudflare tunnel"
}

deploy_backend() {
  case "$DEPLOY_TARGET" in
    local_container) deploy_backend_local_container ;;
    cloud) die "DEPLOY_TARGET=cloud is reserved and not implemented yet — use local_container" ;;
    *) die "unknown DEPLOY_TARGET '$DEPLOY_TARGET' (expected local_container | cloud)" ;;
  esac
}

# ----------------------------------------------------------------------------
# Frontend: stage market/ui -> dist/, bake the backend URL, deploy to Vercel
# ----------------------------------------------------------------------------
stage_frontend() {
  info "Staging market UI -> $DIST_DIR"
  run rm -rf "$DIST_DIR"
  run mkdir -p "$DIST_DIR"
  run cp -r "$UI_SRC/." "$DIST_DIR/"

  # Bake the backend URL the deployed UI should call. In quick-tunnel mode this
  # is intentionally blank — the UI discovers the rotating URL at runtime from
  # Supabase app_config.api_base (published by the cloudflared container). Only
  # set EDGELANE_API_BASE for a stable named tunnel / custom domain, where it
  # serves as a static fallback baked into window.__EDGELANE_API_BASE__.
  if [ -z "$EDGELANE_API_BASE" ]; then
    info "EDGELANE_API_BASE unset — UI will resolve the backend URL at runtime from Supabase (quick-tunnel mode)"
  fi
  # Public auth keys baked alongside the API base. These are browser-safe by
  # design (Supabase anon key is RLS-gated; Turnstile site key is public). The
  # secrets (SERVICE_KEY, TURNSTILE_SECRET, JWT_SECRET) stay server-side only.
  # If unset, the UI runs in dev-bypass (gates open, no login).
  # Fall back to the unprefixed backend names already in deploy/.env so we don't
  # duplicate the same Supabase URL/anon key under two variable names.
  EDGELANE_SUPABASE_URL="${EDGELANE_SUPABASE_URL:-${SUPABASE_URL:-}}"
  EDGELANE_SUPABASE_ANON_KEY="${EDGELANE_SUPABASE_ANON_KEY:-${SUPABASE_ANON_KEY:-}}"
  if [ -z "$EDGELANE_SUPABASE_URL" ] || [ -z "$EDGELANE_SUPABASE_ANON_KEY" ]; then
    warn "Supabase URL/anon key unset — deployed UI will run in dev-bypass (no login gate)"
  fi
  if [ -z "$EDGELANE_TURNSTILE_SITE_KEY" ]; then
    warn "EDGELANE_TURNSTILE_SITE_KEY unset — anonymous teaser session disabled (signed-in users unaffected)"
  fi
  _js() { [ -n "$1" ] && printf '"%s"' "$1" || printf 'null'; }
  if $DRY_RUN; then
    printf "${DIM}[dry-run]${NC} write %s (API base=%s, supabase=%s, turnstile=%s)\n" \
      "$DIST_DIR/edgelane.config.js" "${EDGELANE_API_BASE:-<unset>}" \
      "${EDGELANE_SUPABASE_URL:-<unset>}" "${EDGELANE_TURNSTILE_SITE_KEY:-<unset>}"
  else
    {
      printf 'window.__EDGELANE_API_BASE__ = %s;\n'           "$(_js "$EDGELANE_API_BASE")"
      printf 'window.__EDGELANE_SUPABASE_URL__ = %s;\n'       "$(_js "$EDGELANE_SUPABASE_URL")"
      printf 'window.__EDGELANE_SUPABASE_ANON_KEY__ = %s;\n'  "$(_js "$EDGELANE_SUPABASE_ANON_KEY")"
      printf 'window.__EDGELANE_TURNSTILE_SITE_KEY__ = %s;\n' "$(_js "$EDGELANE_TURNSTILE_SITE_KEY")"
    } > "$DIST_DIR/edgelane.config.js"
    ok "wrote dist/edgelane.config.js (API base: ${EDGELANE_API_BASE:-null}, auth: $([ -n "$EDGELANE_SUPABASE_URL" ] && echo configured || echo dev-bypass))"
  fi
}

deploy_frontend() {
  need vercel "npm i -g vercel + run 'vercel login'"
  # Telemetry adds end-of-run network calls that can stall the CLI on exit.
  export VERCEL_TELEMETRY_DISABLED=1
  if ! $DRY_RUN; then
    vercel whoami >/dev/null 2>&1 && ok "Vercel auth OK ($(vercel whoami 2>/dev/null))" \
      || die "not logged in to Vercel — run 'vercel login' or export VERCEL_TOKEN=..."
  else
    warn "skipping Vercel auth check (dry-run)"
  fi

  stage_frontend

  # Ensure this dir is linked to a Vercel project named '$VERCEL_PROJECT',
  # creating it if it doesn't exist yet. `vercel link --yes --project NAME`
  # links to the existing project under the current scope, or creates a new one
  # with that name when none exists — so the public URL is always pinned to
  # https://$VERCEL_PROJECT.vercel.app. No-op once .vercel/project.json exists.
  if [ -f "$ROOT_DIR/.vercel/project.json" ]; then
    ok "Vercel project already linked (.vercel/project.json present)"
  else
    info "Linking Vercel project '$VERCEL_PROJECT' (auto-creates it if missing)"
    confirm "Link/create Vercel project '$VERCEL_PROJECT'?" || die "aborted"
    run vercel link --yes --project "$VERCEL_PROJECT"
  fi

  info "Deploying market UI -> Vercel project '$VERCEL_PROJECT'"
  # --no-wait: the static build is ~2s and reliably reaches READY; without it the
  # CLI keeps the build log-stream open and never exits (worse when stdout isn't a
  # TTY, e.g. under `make`), forcing a manual Ctrl+C even though the deploy already
  # succeeded. The final production URL is resolved via `vercel project ls` below.
  run vercel deploy --prod --yes --no-wait
  # NB: the bare $VERCEL_PROJECT.vercel.app domain may be taken by another
  # account (global namespace) — Vercel then assigns a suffixed alias
  # (e.g. edgelane-hazel.vercel.app for the old 'edgelane' name). Print the real
  # production URL instead of guessing the bare one.
  if ! $DRY_RUN; then
    # Strip ANSI colour codes from `vercel project ls` before matching the row.
    PROD_URL=$(vercel project ls 2>/dev/null \
      | sed -E 's/\x1b\[[0-9;]*m//g' \
      | awk -v p="$VERCEL_PROJECT" '$1==p {print $2}')
    PROD_URL="${PROD_URL:-https://$VERCEL_PROJECT.vercel.app}"
    ok "frontend deployed -> $PROD_URL"
    sync_supabase_site_url "$PROD_URL"
  else
    ok "frontend deployed (dry-run)"
    sync_supabase_site_url "https://$VERCEL_PROJECT.vercel.app"
  fi
}

# Point Supabase Auth at the *current* Vercel production URL so confirmation /
# magic-link emails redirect back to the live site (default Site URL is
# localhost:3000 → the link is dead). Never hardcoded: derived from PROD_URL on
# every deploy. site_url + uri_allow_list are editable on the free tier (unlike
# the email *templates*, which need custom SMTP). Uses curl — Supabase's WAF
# 403s the Python-urllib user-agent (error 1010).
sync_supabase_site_url() {
  local origin="$1"
  # Normalize to a bare https origin (strip any trailing path).
  origin="https://$(printf '%s' "$origin" | sed -E 's#^https?://##; s#/.*$##')"
  if [ -z "${SUPABASE_ACCESS_TOKEN:-}" ] || [ -z "${SUPABASE_PROJECT_REF:-}" ]; then
    warn "SUPABASE_ACCESS_TOKEN/SUPABASE_PROJECT_REF unset — skipping Auth site_url sync (set Site URL to $origin manually)"
    return 0
  fi
  local allow="$origin/**,https://$VERCEL_PROJECT-*.vercel.app/**,http://localhost:8080/**,http://localhost:8789/**"
  if $DRY_RUN; then
    info "would PATCH Supabase Auth site_url=$origin (dry-run)"
    return 0
  fi
  info "Syncing Supabase Auth site_url -> $origin"
  local body code
  body=$(printf '{"site_url":"%s","uri_allow_list":"%s"}' "$origin" "$allow")
  code=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH \
    -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
    -H "Content-Type: application/json" -d "$body" \
    "https://api.supabase.com/v1/projects/$SUPABASE_PROJECT_REF/config/auth")
  [ "$code" = "200" ] \
    && ok "Supabase Auth site_url + redirect allow-list synced" \
    || warn "Supabase Auth sync got HTTP $code — set Site URL to $origin manually in the dashboard"
}

# ----------------------------------------------------------------------------
# DB migrations (Supabase) — idempotent push via tools/db_push.py
# ----------------------------------------------------------------------------
deploy_db() {
  need python3 "python3 is required to push migrations"
  info "DB migrations (Supabase) — idempotent push"
  if $DRY_RUN; then
    # db_push --dry-run is read-only (just lists files), safe in a dry run.
    python3 "$TOOLS_DIR/db_push.py" --dry-run || true
    return 0
  fi
  confirm "Apply Supabase migrations to the project?" || die "aborted"
  python3 "$TOOLS_DIR/db_push.py"
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
$DRY_RUN && warn "DRY RUN — no commands will execute"

# Full deploy (both FE + BE) migrates the shared DB first — schema before the
# code that depends on it. Single-service deploys (-b / -f) stay DB-free; push
# the DB explicitly with `make db-push`. Opt out of the auto-push with --skip-db.
if $DO_BACKEND && $DO_FRONTEND && ! $SKIP_DB; then
  deploy_db
fi

$DO_BACKEND  && deploy_backend
$DO_FRONTEND && deploy_frontend

ok "all done"
