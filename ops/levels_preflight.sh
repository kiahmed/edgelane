#!/usr/bin/env bash
# Preflight for `make deploy-levels` — fail fast, one line per problem, and
# NEVER print a secret value (only whether a key is set / true / false).
#   usage: ops/levels_preflight.sh <level-engine config> <edgelane_market.config>
set -u
LVL_CFG=${1:?usage: levels_preflight.sh LVL_CONFIG MARKET_CONFIG}
MKT_CFG=${2:?usage: levels_preflight.sh LVL_CONFIG MARKET_CONFIG}

# Value of KEY in a KEY=VALUE file, parsed like level_engine.config.parse_kv
# (last wins, trailing " #comment" and quotes stripped). Empty if absent.
kv() { grep -E "^[[:space:]]*$2[[:space:]]*=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- \
        | sed -e 's/ #.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e "s/^[\"']//" -e "s/[\"']\$//"; }
die() { echo "levels preflight: $*" >&2; exit 1; }

[ -f "$LVL_CFG" ] || die "missing $LVL_CFG — run \`make db-setup\` first (it creates it from the .example + provisions the role passwords + token)"
for k in LVL_DB_URL INTERNAL_FEED_TOKEN; do
  [ -n "$(kv "$LVL_CFG" "$k")" ] || die "$k is empty in $LVL_CFG — run \`make db-setup\` first (it provisions the role passwords + token)"
done
[ "$(docker inspect -f '{{.State.Running}}' edgelane-backend 2>/dev/null)" = "true" ] \
  || die "edgelane-backend is not running (the engine reads its feed) — run \`make deploy-be\` first"

# Warnings only: the backend's side of the feed.
if ! grep -qE '^[[:space:]]*LEVEL_FEED_ENABLED[[:space:]]*=' "$MKT_CFG" 2>/dev/null; then feed=missing
else case "$(kv "$MKT_CFG" LEVEL_FEED_ENABLED | tr '[:upper:]' '[:lower:]')" in true|1|yes|on) feed=true;; *) feed=false;; esac; fi
[ "$feed" = true ] || echo "levels preflight: WARNING LEVEL_FEED_ENABLED=$feed in $MKT_CFG — the engine will see feed=down until it is true (then \`make deploy-be-restart\`)" >&2
[ "$(kv "$MKT_CFG" INTERNAL_FEED_TOKEN)" = "$(kv "$LVL_CFG" INTERNAL_FEED_TOKEN)" ] \
  || echo "levels preflight: WARNING INTERNAL_FEED_TOKEN differs between $MKT_CFG and $LVL_CFG — the backend will 404 every feed call" >&2
echo "levels preflight: ok (LEVEL_FEED_ENABLED=$feed)"
