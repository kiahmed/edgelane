# archive/

Frozen snapshots of EdgeLane builds, kept as easy-to-grep reference
before any major refactor. The live source is always in the repo root;
these are post-build checkpoints.

Each snapshot is named `<artifact>_v<version>.<ext>`. To restore one,
copy it back to the root and rebuild (or just open the `.html` directly
in a browser — it's self-contained).

For git-level history, `git log --tags --decorate --oneline` shows every
tagged version.

## Snapshots

- `edge_lane_v4.7.19.html` — full standalone build (keys baked in,
  embossed EDGELANE wordmark, scenario-aware limit-order tier card,
  width-scaled tiers, live spot decoupled from bias, recursive quota
  parser, Atlas→Provider rebrand)
- `spread_optimizer_v4_7_19.jsx` — JSX source at v4.7.19
