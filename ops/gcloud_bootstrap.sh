#!/usr/bin/env bash
# Idempotent gcloud readiness for the Facades Pub/Sub integration.
#
# Run by `make setup` (and standalone via `make gcloud-setup`). It ONLY fixes
# what's missing:
#   1. GCP_PROJECT / GCP_SA_EMAIL are set in edgelane_market.config
#   2. the gcloud CLI is installed (installs the latest SDK if not)
#   3. an active account is pointed at GCP_SA_EMAIL and the project at GCP_PROJECT
#   4. that account can reach the project and holds the roles this repo needs
#
# Never mutates anything on GCP beyond `gcloud config set` (local). Provisioning
# of the topic/secret/IAM is the separate `make simmer-postiz-integrate`.
set -uo pipefail

CONFIG="${MARKET_CONFIG:-edgelane_market.config}"
cfg() { grep -E "^$1=" "$CONFIG" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]'; }

PROJECT="${GCP_PROJECT:-$(cfg GCP_PROJECT)}"
SA="${GCP_SA_EMAIL:-$(cfg GCP_SA_EMAIL)}"

fail=0
row() { printf "  %-42s %s\n" "$1" "$2"; }
echo "── GCP readiness ─────────────────────────────────────────"

# 1. Config values present.
if [ -n "$PROJECT" ]; then row "GCP_PROJECT (config)" "$PROJECT"; else
  row "GCP_PROJECT (config)" "MISSING — set GCP_PROJECT= in $CONFIG"; fail=1; fi
if [ -n "$SA" ]; then row "GCP_SA_EMAIL (config)" "$SA"; else
  row "GCP_SA_EMAIL (config)" "MISSING — set GCP_SA_EMAIL= in $CONFIG"; fail=1; fi

# 2. gcloud installed — install the latest SDK if not present.
if command -v gcloud >/dev/null 2>&1; then
  row "gcloud installed" "$(gcloud version 2>/dev/null | head -1)"
else
  row "gcloud installed" "no — installing the Google Cloud SDK…"
  curl -sSL https://sdk.cloud.google.com | bash -s -- --disable-prompts >/dev/null 2>&1 \
    && echo "  → installed to ~/google-cloud-sdk. Open a new shell (or 'source" \
            "~/google-cloud-sdk/path.bash.inc'), then re-run 'make setup'." \
    || echo "  → auto-install failed; install manually: https://cloud.google.com/sdk/docs/install"
  exit 0    # PATH won't update in this shell; stop here, user re-runs.
fi

[ "$fail" = 0 ] || { echo "!! fix the config values above, then re-run."; exit 1; }

# 3. Active account → GCP_SA_EMAIL (only if it's already authorized), project pin.
active="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)"
if [ "$active" != "$SA" ]; then
  if gcloud auth list --format='value(account)' 2>/dev/null | grep -qx "$SA"; then
    gcloud config set account "$SA" >/dev/null 2>&1 && active="$SA"
    row "active account" "$SA (set)"
  else
    row "active account" "$active — NOT $SA"
    echo "  → authorize the SA once, then re-run:"
    echo "      gcloud auth activate-service-account $SA --key-file=<KEY.json>"
    fail=1
  fi
else
  row "active account" "$active"
fi
[ "$(gcloud config get-value project 2>/dev/null)" = "$PROJECT" ] \
  || gcloud config set project "$PROJECT" >/dev/null 2>&1
row "active project" "$(gcloud config get-value project 2>/dev/null)"

# 4. Project reachable + the roles this repo needs.
if [ "$fail" = 0 ] && gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  row "project reachable" "OK"
  roles="$(gcloud projects get-iam-policy "$PROJECT" \
            --flatten='bindings[].members' \
            --filter="bindings.members:serviceAccount:$SA" \
            --format='value(bindings.role)' 2>/dev/null)"
  # Owner/editor covers everything; otherwise it needs pubsub + secret admin to
  # provision, and pubsub.publisher to emit events (topic-level binding is added
  # by `make simmer-postiz-integrate`).
  if echo "$roles" | grep -qE 'roles/(owner|editor)'; then
    row "provisioning roles" "OK (owner/editor)"
  else
    for need in pubsub.admin secretmanager.admin; do
      if echo "$roles" | grep -q "roles/$need"; then row "role roles/$need" "OK"
      else row "role roles/$need" "MISSING (needed to provision)"; fail=1; fi
    done
  fi
  echo "  → run 'make simmer-postiz-integrate' to ensure the topic + pubsub.publisher binding + secret."
elif [ "$fail" = 0 ]; then
  row "project reachable" "FAIL — $SA cannot access $PROJECT"; fail=1
fi

echo ""
[ "$fail" = 0 ] && echo ">>> GCP ready." || echo "!! GCP not fully ready (see above)."
exit "$fail"
