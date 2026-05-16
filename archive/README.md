# archive/

Frozen snapshots of EdgeLane builds, kept as easy-to-grep reference before any
major refactor. The live source is always in the repo root; these are
post-build checkpoints.

Each snapshot is named `<artifact>_v<version>.<ext>`. To restore one, copy it
back to the root and rebuild (or just open the `.html` directly in a browser —
it's self-contained).

For git-level history, `git log --tags --decorate --oneline` shows every
tagged version.

## Snapshots

- **`edge_lane_v4.7.19.html`** / **`spread_optimizer_v4_7_19.jsx`** — pre-Lookup-tab
  baseline. Embossed wordmark, scenario-aware limit tiers, recursive quota
  parser, Atlas→Provider rebrand.
- **`edge_lane_v4.7.24.html`** / **`spread_optimizer_v4_7_24.jsx`** — current.
  Adds Lookup tab with 15-min intraday P&L heatmap, BS spread projection,
  adaptive greek-exposure (heavy-chain denylist + chunked fallback + 5-min
  cache), 75s AbortController on JSX fetch, canonical hyphenated tool names.
