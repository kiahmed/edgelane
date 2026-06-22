#!/usr/bin/env bash
# Bump the market UI version constant (UI_VERSION = 'X.Y.Z') in
# market/ui/index.html. Default bumps the patch; pass minor|major to bump those.
#
#   tools/bump_ui_version.sh            # 2.0.0 -> 2.0.1
#   tools/bump_ui_version.sh minor      # 2.0.1 -> 2.1.0
#   tools/bump_ui_version.sh major      # 2.1.0 -> 3.0.0
#   tools/bump_ui_version.sh --print    # just print current version
#
# Wired into .githooks/pre-commit so any commit touching market/ui/index.html
# carries a fresh patch version (enable: git config core.hooksPath .githooks).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FILE="$ROOT/market/ui/index.html"
KIND="${1:-patch}"

cur="$(grep -oE "UI_VERSION = '[0-9]+\.[0-9]+\.[0-9]+'" "$FILE" | head -1 | grep -oE "[0-9]+\.[0-9]+\.[0-9]+")"
[ -n "$cur" ] || { echo "could not find UI_VERSION in $FILE" >&2; exit 1; }

if [ "$KIND" = "--print" ]; then echo "$cur"; exit 0; fi

IFS=. read -r MA MI PA <<<"$cur"
case "$KIND" in
  major) MA=$((MA+1)); MI=0; PA=0 ;;
  minor) MI=$((MI+1)); PA=0 ;;
  patch) PA=$((PA+1)) ;;
  *) echo "usage: $0 [major|minor|patch|--print]" >&2; exit 1 ;;
esac
new="$MA.$MI.$PA"

# Portable in-place edit (GNU + BSD sed).
if sed --version >/dev/null 2>&1; then
  sed -i "s/UI_VERSION = '$cur'/UI_VERSION = '$new'/" "$FILE"
else
  sed -i '' "s/UI_VERSION = '$cur'/UI_VERSION = '$new'/" "$FILE"
fi
echo "UI_VERSION $cur -> $new"
