#!/usr/bin/env bash
# EdgeLane side of the Facades posting pipeline (soljet-postiz docs/simmer.md,
# "## Idempotent provisioning"). Ensures ONLY the pieces EdgeLane owns:
#
#   Pub/Sub topic        facades.ticker-events      (SHARED — may already exist)
#   IAM binding          roles/pubsub.publisher on that topic for the EdgeLane
#                        publisher SA (facades-poster-sa or $GCP_SA_EMAIL)
#   Secret Manager       simmer-api-token           (bearer for the read-only API)
#
# The topic and the SA are SHARED with soljet-postiz's ops/simmer/deploy.sh —
# whichever repo runs first creates them; this side just BINDS to them and never
# recreates. Re-running when everything already exists is a clean no-op.
#
#   ops/simmer/edgelane_provision.sh          # provision (idempotent)
#   DRY=1 ops/simmer/edgelane_provision.sh    # print every gcloud command
#
# Values resolve from the environment, else edgelane_market.config (single source
# of truth — same keys `make` reads), else a sensible default / gcloud config.
set -uo pipefail

CONFIG="${MARKET_CONFIG:-edgelane_market.config}"
cfg(){ grep -E "^$1=" "$CONFIG" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]'; }

PROJECT="${GCP_PROJECT:-$(cfg GCP_PROJECT)}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
TOPIC="${FACADES_EVENTS_TOPIC:-$(cfg FACADES_EVENTS_TOPIC)}"
TOPIC="${TOPIC:-facades.ticker-events}"
SECRET_TOKEN="${SIMMER_API_TOKEN_SECRET:-simmer-api-token}"

# The publisher identity. Reuse the shared runtime SA (facades-poster-sa) or an
# explicitly provided one — never a fresh account. It does NOT have to be the
# backend runtime SA; it only needs pubsub.publisher on the topic.
RUNTIME_SA_ID="${FACADES_RUNTIME_SA_ID:-facades-poster-sa}"
PUBLISHER_SA="${GCP_SA_EMAIL:-$(cfg GCP_SA_EMAIL)}"
PUBLISHER_SA="${PUBLISHER_SA:-${RUNTIME_SA_ID}@${PROJECT}.iam.gserviceaccount.com}"

if [ -z "$PROJECT" ]; then
  echo "!! no project — set GCP_PROJECT or 'gcloud config set project <id>'" >&2
  exit 1
fi

run(){ if [ "${DRY:-}" = 1 ]; then printf '  + %s\n' "$*"; else "$@"; fi; }
gc(){ run gcloud "$@" --project="$PROJECT"; }

echo "project=$PROJECT  topic=$TOPIC  publisher_sa=$PUBLISHER_SA  secret=$SECRET_TOKEN"

# --- Pub/Sub topic (shared; create only if absent) --------------------------
echo "== Pub/Sub topic: $TOPIC =="
gc pubsub topics create "$TOPIC" 2>/dev/null || echo "   (exists)"

# --- Publisher SA (shared; ensure, never recreate) --------------------------
# `describe` first so a re-run is a clean no-op; create only when truly missing.
# If IAM to create SAs is unavailable, the sibling repo (or an admin) creates it
# and this just binds below.
echo "== Publisher service account: $PUBLISHER_SA =="
if [ "${DRY:-}" = 1 ]; then
  printf '  + %s\n' "gcloud iam service-accounts describe $PUBLISHER_SA --project=$PROJECT || gcloud iam service-accounts create $RUNTIME_SA_ID ..."
elif gcloud iam service-accounts describe "$PUBLISHER_SA" --project="$PROJECT" >/dev/null 2>&1; then
  echo "   (exists)"
else
  gc iam service-accounts create "$RUNTIME_SA_ID" \
    --display-name="Facades posting - Pub/Sub publisher" 2>/dev/null \
    || echo "   ! could not create $PUBLISHER_SA — create it (or run the postiz side) then re-run"
fi

# --- IAM: pubsub.publisher on the topic (idempotent by nature) --------------
echo "== IAM: roles/pubsub.publisher for $PUBLISHER_SA on $TOPIC =="
gc pubsub topics add-iam-policy-binding "$TOPIC" \
  --member="serviceAccount:${PUBLISHER_SA}" \
  --role="roles/pubsub.publisher" \
  || echo "   ! binding failed — ensure the topic and SA exist, then re-run"

# --- Secret Manager: simmer-api-token (create if missing, else LEAVE) -------
echo "== Secret Manager: $SECRET_TOKEN =="
if [ "${DRY:-}" = 1 ]; then
  printf '  + %s\n' "gcloud secrets describe $SECRET_TOKEN --project=$PROJECT  # else:"
  printf '  + %s\n' "openssl rand -hex 32 | gcloud secrets create $SECRET_TOKEN --data-file=- --replication-policy=automatic --project=$PROJECT"
elif gcloud secrets describe "$SECRET_TOKEN" --project="$PROJECT" >/dev/null 2>&1; then
  echo "   (exists — left as-is)"
else
  echo "   creating with a generated token"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32 | gcloud secrets create "$SECRET_TOKEN" \
      --data-file=- --replication-policy=automatic --project="$PROJECT" \
      && echo "   ! set SIMMER_API_TOKEN in edgelane_market.config to this secret's value" \
      || echo "   ! secret create failed — create '$SECRET_TOKEN' manually, then re-run"
  else
    echo "   ! openssl not found — create '$SECRET_TOKEN' manually with a random value"
  fi
fi

# --- Always surface the token + integration status (every run) ---------------
# Whether we just created the secret or it already existed, read the live value
# back and print it, plus whether the local config already matches.
if [ "${DRY:-}" != 1 ]; then
  echo "== Integration status =="
  TOKEN="$(gcloud secrets versions access latest --secret="$SECRET_TOKEN" \
            --project="$PROJECT" 2>/dev/null)"
  if [ -n "$TOKEN" ]; then
    CFG_TOKEN="$(cfg SIMMER_API_TOKEN)"
    echo "   SIMMER_API_TOKEN = $TOKEN"
    if [ "$CFG_TOKEN" = "$TOKEN" ]; then
      echo "   ✓ integration is set up — config already matches Secret Manager."
    else
      echo "   ! set this in $CONFIG:  SIMMER_API_TOKEN=$TOKEN   (then: make deploy-be-restart)"
    fi
  else
    echo "   ! could not read '$SECRET_TOKEN' — check secretAccessor on $PUBLISHER_SA / your account."
  fi
fi

echo "done."
