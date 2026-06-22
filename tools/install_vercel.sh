#!/usr/bin/env bash
# Install the Vercel CLI on Ubuntu/Debian and log in (browser).
#
# - Installs Node.js (NodeSource LTS) if missing.
# - Installs the Vercel CLI globally if missing.
# - Runs `vercel login` (opens a browser / prints a verification URL) if not
#   already authenticated.
#
# Idempotent: skips anything already present. Run via `make vercel-setup`.
set -euo pipefail

SUDO=""
[ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

info() { printf "\033[0;34m==>\033[0m %s\n" "$*"; }

# 1. Node.js -----------------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  info "Node.js not found — installing NodeSource LTS (20.x)…"
  if command -v apt-get >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | $SUDO -E bash -
    $SUDO apt-get install -y nodejs
  else
    echo "This installer targets Ubuntu/Debian (apt). Install Node.js 18+ manually, then re-run." >&2
    exit 1
  fi
fi
info "node $(node -v) · npm $(npm -v)"

# 2. Vercel CLI --------------------------------------------------------------
if ! command -v vercel >/dev/null 2>&1; then
  info "Installing Vercel CLI globally (npm i -g vercel)…"
  $SUDO npm install -g vercel
fi
info "vercel $(vercel --version)"

# 3. Auth --------------------------------------------------------------------
if vercel whoami >/dev/null 2>&1; then
  info "Already logged in to Vercel as $(vercel whoami)"
else
  info "Not logged in — launching browser login (follow the prompt / URL)…"
  vercel login
fi

printf "\033[0;32m✓ Vercel CLI ready — you can now run 'make deploy-fe' or 'make deploy-prod'.\033[0m\n"
