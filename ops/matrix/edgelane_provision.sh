#!/usr/bin/env bash
# EdgeLane side of the Matrix→Postiz pipeline (docs/matrix_events_update.md).
# Mirror of ops/simmer/edgelane_provision.sh, on Matrix's OWN topic + token:
#
#   Pub/Sub topic        facades.matrix-events      (Matrix-specific, not shared)
#   IAM binding          roles/pubsub.publisher on that topic for the EdgeLane
#                        publisher SA (facades-poster-sa or $GCP_SA_EMAIL)
#   Secret Manager       matrix-api-token           (bearer for /matrix/* read-only)
#
#   ops/matrix/edgelane_provision.sh          # provision (idempotent)
#   DRY=1 ops/matrix/edgelane_provision.sh    # print every gcloud command
#
# Values resolve from the environment, else edgelane_market.config, else a
# sensible default / gcloud config. Re-running when everything exists is a no-op.
set -uo pipefail

CONFIG="${MARKET_CONFIG:-edgelane_market.config}"
cfg(){ grep -E "^$1=" "$CONFIG" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]'; }

PROJECT="${GCP_PROJECT:-$(cfg GCP_PROJECT)}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
TOPIC="${MATRIX_EVENTS_TOPIC:-$(cfg MATRIX_EVENTS_TOPIC)}"
TOPIC="${TOPIC:-facades.matrix-events}"
SECRET_TOKEN="${MATRIX_API_TOKEN_SECRET:-matrix-api-token}"

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

# --- Pub/Sub topic (create only if absent) ----------------------------------
echo "== Pub/Sub topic: $TOPIC =="
gc pubsub topics create "$TOPIC" 2>/dev/null || echo "   (exists)"

# --- Publisher SA (ensure, never recreate) ----------------------------------
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

# --- Secret Manager: matrix-api-token (create if missing, else LEAVE) -------
echo "== Secret Manager: $SECRET_TOKEN =="
if [ "${DRY:-}" = 1 ]; then
  printf '  + %s\n' "gcloud secrets describe $SECRET_TOKEN --project=$PROJECT  # else:"
  printf '  + %s\n' "openssl rand -hex 32 | gcloud secrets create $SECRET_TOKEN --data-file=- --replication-policy=automatic --project=$PROJECT"
elif gcloud secrets describe "$SECRET_TOKEN" --project="$PROJECT" >/dev/null 2>&1; then
  # The secret existing is NOT the same as it holding a usable token: the postiz
  # side pre-creates it with a placeholder ("placeholder-not-yet-issued-by-..."),
  # and EdgeLane is the side that issues the real value. Treat a placeholder or
  # an empty version as "not issued yet" and add a real one; anything else is a
  # live token and is never touched (rotating it would break the poster mid-run).
  CURRENT="$(gcloud secrets versions access latest --secret="$SECRET_TOKEN" \
              --project="$PROJECT" 2>/dev/null)"
  case "$CURRENT" in
    ""|placeholder*)
      echo "   exists but not issued yet (${CURRENT:-empty}) — adding a real token version"
      if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32 | gcloud secrets versions add "$SECRET_TOKEN" \
          --data-file=- --project="$PROJECT" >/dev/null \
          && echo "   ✓ issued" \
          || echo "   ! could not add a version — check secretVersionAdder on your account"
      else
        echo "   ! openssl not found — add a version to '$SECRET_TOKEN' manually"
      fi ;;
    *) echo "   (exists — real token already issued, left as-is)" ;;
  esac
else
  echo "   creating with a generated token"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32 | gcloud secrets create "$SECRET_TOKEN" \
      --data-file=- --replication-policy=automatic --project="$PROJECT" \
      && echo "   ! set MATRIX_API_TOKEN in edgelane_market.config to this secret's value" \
      || echo "   ! secret create failed — create '$SECRET_TOKEN' manually, then re-run"
  else
    echo "   ! openssl not found — create '$SECRET_TOKEN' manually with a random value"
  fi
fi

# --- Always surface the token + integration status (every run) --------------
if [ "${DRY:-}" != 1 ]; then
  echo "== Integration status =="
  TOKEN="$(gcloud secrets versions access latest --secret="$SECRET_TOKEN" \
            --project="$PROJECT" 2>/dev/null)"
  if [ -n "$TOKEN" ]; then
    CFG_TOKEN="$(cfg MATRIX_API_TOKEN)"
    echo "   MATRIX_API_TOKEN = $TOKEN"
    if [ "$CFG_TOKEN" = "$TOKEN" ]; then
      echo "   ✓ integration is set up — config already matches Secret Manager."
    else
      echo "   ! set this in $CONFIG:  MATRIX_API_TOKEN=$TOKEN   (then: make deploy-be-restart)"
    fi
  else
    echo "   ! could not read '$SECRET_TOKEN' — check secretAccessor on $PUBLISHER_SA / your account."
  fi
fi

echo "done."
