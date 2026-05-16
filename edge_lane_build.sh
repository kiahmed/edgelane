#!/usr/bin/env bash
# edge_lane_build.sh
# ----------------------------------------------------------------------------
# Reads the JSX source + edge_lane_config.config, transforms the JSX so it
# can run in a browser via Babel-standalone (strips ESM imports, drops
# `export default`), wraps it in the HTML template, and substitutes the API
# keys.
#
# Usage:
#   ./edge_lane_build.sh                                       # all defaults
#   ./edge_lane_build.sh CONFIG TEMPLATE JSX OUT               # positional
#   ./edge_lane_build.sh --jsx spread_optimizer_v4_5.jsx       # build legacy
#   ./edge_lane_build.sh --dry-run                             # don't write
#   ./edge_lane_build.sh --help
#
# Flags:
#   -h, --help        Show this message and exit.
#       --dry-run     Run all checks + show what would be substituted, but
#                     don't write OUT. Keys are masked in the output.
#   -n, --new-output  Write to edge_lane_v{VERSION}.html instead of overwriting
#                     edge_lane.html. Useful for preserving prior builds.
#       --config FILE   override CONFIG   (default: edge_lane_config.config)
#       --template FILE override TEMPLATE (default: edge_lane.template.html)
#       --jsx FILE      override JSX      (default: spread_optimizer_v4_7_html.jsx)
#       --out FILE      override OUT      (default: edge_lane.html)
#
# Positional args (unchanged for backward compat):
#   $1 = CONFIG, $2 = TEMPLATE, $3 = JSX, $4 = OUT
# ----------------------------------------------------------------------------

set -euo pipefail

# ---- defaults -------------------------------------------------------------
CONFIG="edge_lane_config.config"
TEMPLATE="edge_lane.template.html"
JSX="spread_optimizer_v4_7_html.jsx"
OUT="edge_lane.html"
DRY_RUN=0
NEW_OUTPUT=0

show_help() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

# ---- argv parsing ---------------------------------------------------------
# Two modes: flags OR positional. Flags take precedence; if first arg looks
# like a path/file, fall back to positional behavior.
positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)      show_help ;;
    -n|--new-output) NEW_OUTPUT=1; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --config)       CONFIG="$2"; shift 2 ;;
    --template)     TEMPLATE="$2"; shift 2 ;;
    --jsx)          JSX="$2"; shift 2 ;;
    --out)          OUT="$2"; shift 2 ;;
    --)             shift; while [[ $# -gt 0 ]]; do positional+=("$1"); shift; done ;;
    -*)             echo "✗ unknown flag: $1" >&2; echo "   try --help" >&2; exit 1 ;;
    *)              positional+=("$1"); shift ;;
  esac
done

# Apply positional args if present (backward compat)
[[ ${#positional[@]} -ge 1 ]] && CONFIG="${positional[0]}"
[[ ${#positional[@]} -ge 2 ]] && TEMPLATE="${positional[1]}"
[[ ${#positional[@]} -ge 3 ]] && JSX="${positional[2]}"
[[ ${#positional[@]} -ge 4 ]] && OUT="${positional[3]}"

# ---- helpers --------------------------------------------------------------
mask() {
  # Mask middle of a secret: keep first 4 + last 2 chars
  local s="$1"
  local n=${#s}
  if (( n <= 8 )); then printf '****'; return; fi
  printf '%s****%s' "${s:0:4}" "${s: -2}"
}

# ---- 1. Load config -------------------------------------------------------
if [[ ! -f "$CONFIG" ]]; then
  echo "✗ config not found: $CONFIG" >&2
  echo "  copy edge_lane_config.config.example → $CONFIG and fill in your keys" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG"

: "${ATLAS_KEY:?ATLAS_KEY not set in $CONFIG}"
: "${ANTHROPIC_KEY:?ANTHROPIC_KEY not set in $CONFIG}"
: "${GEMINI_API_KEY:?GEMINI_API_KEY not set in $CONFIG}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"  # default if not in config
ATLAS_BASE_URL="${ATLAS_BASE_URL:-}"     # empty = direct (will CORS-fail from browser)
GEMINI_BASE_URL="${GEMINI_BASE_URL:-}"   # empty = direct
BIAS_NARRATIVE_OPEN_DEFAULT="${BIAS_NARRATIVE_OPEN_DEFAULT:-0}"  # 1 = open by default
# ---- Version auto-bump ----------------------------------------------------
# EDGE_LANE_VERSION in config is the major.minor BASE (e.g. "4.7"). The build
# script auto-appends a patch number that bumps when JSX or template content
# changes (sha1 hash) and resets to 1 when you edit the base in config.
EDGE_LANE_VERSION_BASE="${EDGE_LANE_VERSION:-4.7}"
STATE_FILE=".edge_lane_buildstate"

# Compute current hash of build inputs
CURRENT_HASH=$(cat "$JSX" "$TEMPLATE" | sha1sum | awk '{print $1}')

# Read previous state (if any)
LAST_BASE=""; LAST_PATCH=0; LAST_HASH=""
if [[ -f "$STATE_FILE" ]]; then
  # shellcheck disable=SC1090,SC1091
  source "$STATE_FILE"
fi

# Decide patch number
if [[ -z "$LAST_BASE" || "$LAST_BASE" != "$EDGE_LANE_VERSION_BASE" ]]; then
  PATCH=1                                          # first build OR base changed → reset
  PATCH_REASON="base change ($LAST_BASE → $EDGE_LANE_VERSION_BASE)"
elif [[ "$LAST_HASH" == "$CURRENT_HASH" ]]; then
  PATCH=$LAST_PATCH                                # idempotent rebuild
  PATCH_REASON="no content change"
else
  PATCH=$((LAST_PATCH + 1))                        # JSX/template changed → bump
  PATCH_REASON="content change (hash ${LAST_HASH:0:7} → ${CURRENT_HASH:0:7})"
fi
[[ "$LAST_BASE" == "" ]] && PATCH_REASON="first build"

EDGE_LANE_VERSION="${EDGE_LANE_VERSION_BASE}.${PATCH}"

# ---- 2. Sanity-check inputs -----------------------------------------------
[[ -f "$TEMPLATE" ]] || { echo "✗ template not found: $TEMPLATE" >&2; exit 1; }
[[ -f "$JSX"      ]] || { echo "✗ jsx source not found: $JSX"     >&2; exit 1; }

# ---- 3. Transform JSX so Babel-standalone can run it in-browser ----------
#   - drop  `import { ... } from 'react';`        (hooks come from window.React)
#   - drop  `export default `                      (component becomes a top-level fn)
JSX_BODY="$(
  sed -E \
    -e "/^[[:space:]]*import[[:space:]]*\{[^}]*\}[[:space:]]*from[[:space:]]*['\"]react['\"];?[[:space:]]*$/d" \
    -e "s/^[[:space:]]*export[[:space:]]+default[[:space:]]+/  /" \
    "$JSX"
)"

# ---- 4. Stitch template + jsx + keys via bash parameter expansion --------
TEMPLATE_CONTENT="$(<"$TEMPLATE")"
OUTPUT="${TEMPLATE_CONTENT//__JSX_BODY__/$JSX_BODY}"
OUTPUT="${OUTPUT//__ATLAS_KEY__/$ATLAS_KEY}"
OUTPUT="${OUTPUT//__ANTHROPIC_KEY__/$ANTHROPIC_KEY}"
OUTPUT="${OUTPUT//__GEMINI_API_KEY__/$GEMINI_API_KEY}"
OUTPUT="${OUTPUT//__GEMINI_MODEL__/$GEMINI_MODEL}"
OUTPUT="${OUTPUT//__ATLAS_BASE_URL__/$ATLAS_BASE_URL}"
OUTPUT="${OUTPUT//__GEMINI_BASE_URL__/$GEMINI_BASE_URL}"
OUTPUT="${OUTPUT//__EDGE_LANE_VERSION__/$EDGE_LANE_VERSION}"
OUTPUT="${OUTPUT//__BIAS_NARRATIVE_OPEN_DEFAULT__/$BIAS_NARRATIVE_OPEN_DEFAULT}"

# ---- 5. Dry-run: report and exit ------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
  echo "── DRY RUN ─ no files will be written ─────────────────────────────"
  echo "  CONFIG    = $CONFIG"
  echo "  TEMPLATE  = $TEMPLATE  ($(wc -l < "$TEMPLATE" | tr -d ' ') lines)"
  echo "  JSX       = $JSX  ($(wc -l < "$JSX" | tr -d ' ') lines)"
  echo "  OUT       = $OUT  (would be overwritten if it exists)"
  echo
  echo "── substitutions ──────────────────────────────────────────────────"
  printf "  %-20s = %s\n" "ATLAS_KEY"      "$(mask "$ATLAS_KEY")"
  printf "  %-20s = %s\n" "ANTHROPIC_KEY"  "$(mask "$ANTHROPIC_KEY")"
  printf "  %-20s = %s\n" "GEMINI_API_KEY" "$(mask "$GEMINI_API_KEY")"
  printf "  %-20s = %s\n" "GEMINI_MODEL"   "$GEMINI_MODEL"
  printf "  %-20s = %s\n" "VERSION"        "v$EDGE_LANE_VERSION ($PATCH_REASON)"
  echo
  echo "── output preview ─────────────────────────────────────────────────"
  WOULD_LINES=$(printf '%s\n' "$OUTPUT" | wc -l | tr -d ' ')
  WOULD_BYTES=${#OUTPUT}
  echo "  would write: $WOULD_LINES lines / ~$WOULD_BYTES bytes"
  REMAINING=$(printf '%s' "$OUTPUT" | grep -cE '__(JSX_BODY|ATLAS_KEY|ANTHROPIC_KEY|GEMINI_API_KEY|GEMINI_MODEL)__' || true)
  echo "  remaining placeholders: $REMAINING (should be 0)"
  if [[ $REMAINING -gt 0 ]]; then
    echo "  ✗ would have unfilled placeholders! template/build mismatch?" >&2
    exit 1
  fi
  echo
  echo "  Re-run without --dry-run to actually write $OUT."
  exit 0
fi

# ---- 6. Write final HTML --------------------------------------------------
if [[ $NEW_OUTPUT -eq 1 ]]; then
  # Build a versioned filename next to the configured OUT
  BASE_DIR=$(dirname "$OUT")
  BASE_NAME=$(basename "$OUT" .html)
  OUT="${BASE_DIR}/${BASE_NAME}_v${EDGE_LANE_VERSION}.html"
  echo "  (--new-output) writing to versioned file: $OUT"
fi
printf '%s\n' "$OUTPUT" > "$OUT"

# ---- 7. Persist build state for next run's auto-bump ----------------------
{
  echo "# Auto-managed by edge_lane_build.sh — don't edit by hand."
  echo "LAST_BASE=\"$EDGE_LANE_VERSION_BASE\""
  echo "LAST_PATCH=$PATCH"
  echo "LAST_HASH=\"$CURRENT_HASH\""
} > "$STATE_FILE"

# ---- 8. Loud summary ------------------------------------------------------
LINES=$(wc -l < "$OUT" | tr -d ' ')
BYTES=$(wc -c < "$OUT" | tr -d ' ')
echo "✓ wrote $OUT  ($LINES lines, $BYTES bytes)"
echo "  version: v$EDGE_LANE_VERSION  ($PATCH_REASON)"
echo "  open it directly in a browser, or serve via:  python3 -m http.server 8080"
echo "  then visit http://localhost:8080/$OUT"
