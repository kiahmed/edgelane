#!/bin/sh
# Start a Cloudflare quick tunnel to the backend, then publish the assigned
# *.trycloudflare.com URL to Supabase (app_config.api_base) so the frontend can
# discover it at runtime. Publishing happens ONCE per start — the quick-tunnel
# hostname is stable for the life of the cloudflared process, so the only time
# it changes is when this container restarts, which re-runs this script.
set -eu

TARGET="${TUNNEL_TARGET:-http://edgelane-backend:8789}"
METRICS_PORT="${METRICS_PORT:-2000}"

# 1. Launch the quick tunnel (no token, no domain). Background it so we can read
#    the metrics endpoint, then hand the process back via `wait`.
cloudflared tunnel --no-autoupdate --metrics "0.0.0.0:${METRICS_PORT}" --url "$TARGET" &
CF_PID=$!

# 2. Wait (locally, fast) for cloudflared to register the quick-tunnel hostname.
HOST=""
i=0
while [ "$i" -lt 60 ]; do
  HOST=$(curl -sf "http://localhost:${METRICS_PORT}/quicktunnel" 2>/dev/null | jq -r '.hostname // empty' || true)
  [ -n "$HOST" ] && break
  kill -0 "$CF_PID" 2>/dev/null || { echo "[publish] cloudflared exited during startup" >&2; wait "$CF_PID"; exit 1; }
  i=$((i + 1)); sleep 1
done

if [ -z "$HOST" ]; then
  echo "[publish] WARN: could not determine quick-tunnel hostname after 60s; tunnel runs but URL not published" >&2
else
  URL="https://${HOST}"
  echo "[publish] quick tunnel up: ${URL}" >&2
  if [ -n "${SUPABASE_URL:-}" ] && [ -n "${SUPABASE_SERVICE_KEY:-}" ]; then
    n=0
    while [ "$n" -lt 5 ]; do
      code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
        "${SUPABASE_URL%/}/rest/v1/app_config?on_conflict=key" \
        -H "apikey: ${SUPABASE_SERVICE_KEY}" \
        -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}" \
        -H "Content-Type: application/json" \
        -H "Prefer: resolution=merge-duplicates" \
        -d "{\"key\":\"api_base\",\"value\":\"${URL}\"}" 2>/dev/null || echo 000)
      case "$code" in
        2*) echo "[publish] published api_base -> Supabase (HTTP ${code})" >&2; break ;;
        *)  n=$((n + 1)); echo "[publish] publish attempt ${n} failed (HTTP ${code}); retrying in 3s" >&2; sleep 3 ;;
      esac
    done
    [ "$n" -ge 5 ] && echo "[publish] ERROR: gave up publishing api_base after 5 attempts" >&2
  else
    echo "[publish] SUPABASE_URL / SUPABASE_SERVICE_KEY not set; skipping Supabase publish" >&2
  fi

  # Also publish to Vercel Edge Config so the DEPLOYED SPA self-heals a rotated
  # quick-tunnel URL WITHOUT a redeploy (the UI reads /api/config → Edge Config
  # at boot). Same idea as the Supabase write, aimed at the frontend host.
  if [ -n "${VERCEL_API_TOKEN:-}" ] && [ -n "${EDGE_CONFIG_ID:-}" ]; then
    n=0
    while [ "$n" -lt 5 ]; do
      code=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH         "https://api.vercel.com/v1/edge-config/${EDGE_CONFIG_ID}/items${VERCEL_TEAM_ID:+?teamId=${VERCEL_TEAM_ID}}"         -H "Authorization: Bearer ${VERCEL_API_TOKEN}"         -H "Content-Type: application/json"         -d "{\"items\":[{\"operation\":\"upsert\",\"key\":\"api_base\",\"value\":\"${URL}\"}]}" 2>/dev/null || echo 000)
      case "$code" in
        2*) echo "[publish] published api_base -> Vercel Edge Config (HTTP ${code})" >&2; break ;;
        *)  n=$((n + 1)); echo "[publish] edge-config attempt ${n} failed (HTTP ${code}); retrying in 3s" >&2; sleep 3 ;;
      esac
    done
    [ "$n" -ge 5 ] && echo "[publish] ERROR: gave up publishing to Edge Config after 5 attempts" >&2
  else
    echo "[publish] VERCEL_API_TOKEN / EDGE_CONFIG_ID not set; skipping Edge Config publish" >&2
  fi
fi

# 3. Run cloudflared in the foreground. When it exits, the container restarts
#    (restart: unless-stopped) and this script republishes the new URL.
wait "$CF_PID"
