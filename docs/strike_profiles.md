# Debit smart strike picker + per-ticker profiles

Backend-authority strike selection for **debit verticals** (`bull_call`,
`bear_put`). Credit spreads, condors and flies are unchanged.

## Why

The legacy builder anchors a debit spread's long leg at `delta = 1 - target_delta`.
With a 0.20–0.30 short-delta target that lands the long at ~0.70–0.80 delta — deep
ITM. A deep-ITM debit vertical enters near its max value: tiny net delta, almost no
room to expand, wide markets. It barely moves on a real directional push (sluggish /
flaky / no gains).

Pros build directional debits **OTM-OTM**: long the gamma-rich strike just out of the
money (~0.40 delta), short **sold into the expected target** (GEX magnet wall → nearest
high-OI strike → 1× expected move). "Buy the move, sell the wall." Cheap, convex,
expands fast toward the target.

## How

`app/strike_profiles.py`:
- `StrikeProfile` — per-ticker config (dataclass).
- `pick_debit_strikes(...)` — deterministic OTM-OTM selection: long by delta band (or
  `long_offset_pts`), short clamped into the width window + `short_min_delta` floor,
  snapped to listed strikes, liquidity-gated. Returns `{short, long, logic}`.
- `resolve_profile(db, symbol)` — symbol row → saved `DEFAULT` row → built-in default.

Wired through `build_vertical(... spot, walls, profile, aggressiveness)` and
`generate_candidates(... spot, profile)`; `engine.compute_engine_output(...,
strike_profile=...)`; the **poller** resolves the profile each cycle (live edits apply
next poll). All new params default to off → legacy behavior, so the 56 JSX-parity tests
are untouched (picker only fires when a profile + spot are passed).

The chosen candidate carries `strike_source: "smart_picker"` and a human-readable
`strike_logic` string (e.g. *"OTM debit: long 5010 (|Δ|0.42, 10 pts OTM) · short 4965
(|Δ|0.18) sold into magnet wall · width 45 pts"*), surfaced on the snapshot payload.

## Profile fields

| field | meaning |
|---|---|
| `enabled` | off → legacy deep-ITM path |
| `long_delta_lo/hi` | long-leg |Δ| band (gamma-rich, just OTM) |
| `long_offset_pts` | if set, overrides the band: long ~N pts OTM from spot |
| `short_min_delta` | floor so the short still carries premium |
| `min/max_width_pts` | width window; `None` = derive from expected move |
| `target_source` | `wall` → `oi` → `move` (where the short is sold) |
| `round_snap` | listed-strike spacing (SPX = 5; 0 disables) |
| `min_oi` / `min_vol` | liquidity gate |

The Conservative/Balanced/Aggressive fan maps to aggressiveness −1/0/+1, scaling the
width window and shifting the long delta.

## Persistence + API

DuckDB table `strike_profiles` (db.py), seeded with `DEFAULT` + `SPX` on first migrate
(`INSERT OR IGNORE`, never clobbers edits). **No deploy migration script** — the table is
DuckDB (not Supabase), so it self-creates + seeds when the backend boots; `make db-push`
(Supabase-only) is unrelated.

**Admin-only** (`router = APIRouter(dependencies=[Depends(require_admin)])`). When
`AUTH_ENABLED=true`, only the server admin token grants access (`X-Admin-Token: <ADMIN_API_TOKEN>`);
a regular signed-in Supabase user gets **403**, anonymous **401**. No-op when
`AUTH_ENABLED=false` (dev).

- `GET  /strike-profiles` — list all
- `GET  /strike-profiles/{symbol}` — symbol row, else effective fallback (`is_default`)
- `PUT  /strike-profiles/{symbol}` — create/replace (takes effect next poll cycle)

## Seeded defaults

- **SPX**: `long_delta 0.38–0.46`, `short_min_delta 0.12`, `min/max_width 20/50 pts`,
  `target_source wall`, `round_snap 5`, `min_oi 50`.
- **NDX**: same delta band; `min/max_width 75/200 pts`, `round_snap 25`, `min_oi 10`
  (Nasdaq-100 trades ~4× SPX in points and is thinner, so the window/snap scale up and
  the liquidity gate eases). Confirm the listed strike grid for your expirations — NDX
  spacing varies — and tune live via the API.

Add more tickers by `PUT /strike-profiles/{symbol}` (and list them in the backend's
`SYMBOLS=` config so the poller fetches them). Unconfigured symbols inherit `DEFAULT`.

## Tests

`tests/strike_profiles/test_picker.py` — OTM-OTM selection, magnet anchoring, width
clamp, aggressiveness spread, and the legacy-fallback / no-profile-identical guarantees.
