# Build spec: Strike Profiles admin page

Handoff for a UI session. Build an **admin-only** page to view + edit every
per-ticker debit strike profile. Backend (API, auth, DB) is already done — this is
purely the page. Context: `docs/strike_profiles.md` (what profiles do),
`docs/torque.md` (the auth/serving pattern to mirror).

## Goal

A single page where the server owner can:
- list all saved profiles (`DEFAULT` + per-ticker),
- edit any profile's fields and save,
- add a profile for a new symbol,
- see which symbols fall back to `DEFAULT` (no own row).

These control how the engine builds **debit** verticals (bull_call/bear_put) per
ticker. Saves take effect on the next poll cycle (~15s). No live trading happens
from this page — it's config only.

## Auth (must match Torque exactly)

Every endpoint is admin-gated. Two accepted credentials, same `ADMIN_API_TOKEN`
the Torque page uses:
- `?token=<ADMIN_API_TOKEN>` query param (browser nav), OR
- `X-Admin-Token: <ADMIN_API_TOKEN>` header (fetch/XHR).

Behavior when `AUTH_ENABLED=true`: anonymous → **401**, signed-in non-admin user →
**403**, admin token → **200**. When `AUTH_ENABLED=false` (dev) the gate is a no-op.

**Page-serving requirement:** serve this page from the backend the same way Torque
is served (see `app/routes/torque.py` → `_page_authorized` + `FileResponse`). The
top-level GET that returns the HTML shell must accept `?token=` and 401 with a short
"append ?token=" message otherwise. Then the page's own fetch calls should forward
the token — simplest is to read `token` from `location.search` and append it to every
API URL (the Torque page does this). Do NOT hardcode the token in the file.

Suggested route: `GET /strike-profiles/admin` → serves the HTML. Put the file at
`market/backend/ui/strike_profiles.html` (sibling of `ui/torque.html`) and register
it in `app/routes/strike_profiles.py` (or a small dedicated route module) mirroring
the Torque page route. Keep the JSON API router admin-gated as it already is.

## API contract (already live)

Base URL = backend host (prod = the CF `*.trycloudflare.com` tunnel URL; local =
`http://127.0.0.1:8789`).

### `GET /strike-profiles`
List all saved rows.
```json
{ "profiles": [
  { "symbol": "DEFAULT", "enabled": true, "long_delta_lo": 0.38, "long_delta_hi": 0.46,
    "long_offset_pts": null, "short_min_delta": 0.12, "min_width_pts": null,
    "max_width_pts": null, "target_source": "wall", "round_snap": 0.0,
    "min_oi": 0, "min_vol": 0 },
  { "symbol": "SPX", "...": "..." }
] }
```

### `GET /strike-profiles/{symbol}`
The symbol's own row if present, else the effective fallback flagged as default.
```json
{ "profile": { "...": "..." }, "is_default": false }
```
`is_default: true` means there is no row for this symbol; the returned profile is the
`DEFAULT` row (or built-in). Use this to show a "inherits DEFAULT — create override?"
state.

### `PUT /strike-profiles/{symbol}`
Create or replace. **Full-object replace**, not a patch: any field omitted from the
body is set to its built-in default, NOT kept from the existing row. So the UI must
load the current values, let the user edit, and send the **entire** object back.

Request body:
```json
{ "enabled": true, "long_delta_lo": 0.38, "long_delta_hi": 0.46,
  "long_offset_pts": null, "short_min_delta": 0.12, "min_width_pts": 20.0,
  "max_width_pts": 50.0, "target_source": "wall", "round_snap": 5.0,
  "min_oi": 50, "min_vol": 0 }
```
Success: `{ "ok": true, "profile": { ...echoed... } }`.

## Fields (form spec)

| field | control | range / values | meaning |
|---|---|---|---|
| `symbol` | text (path, not body) | uppercased server-side | ticker; `DEFAULT` is the fallback row |
| `enabled` | toggle | bool | off → legacy deep-ITM path for this symbol |
| `long_delta_lo` | number | 0.0–1.0 | long-leg |Δ| band low (gamma-rich, just OTM) |
| `long_delta_hi` | number | 0.0–1.0, ≥ lo | long-leg |Δ| band high |
| `long_offset_pts` | number or blank | ≥ 0 or null | if set, overrides the delta band: long ≈ N pts OTM from spot |
| `short_min_delta` | number | 0.0–1.0 | floor so the short still carries premium |
| `min_width_pts` | number or blank | ≥ 0 or null | width floor; **blank = derive from expected move** |
| `max_width_pts` | number or blank | ≥ 0 or null, ≥ min | width cap; **blank = derive from expected move** |
| `target_source` | select | `wall` \| `oi` \| `move` | where the short is sold: magnet wall → high-OI strike → 1× expected move |
| `round_snap` | number | ≥ 0 | listed-strike spacing for snapping (SPX = 5; 0 = off) |
| `min_oi` | integer | ≥ 0 | liquidity gate (open interest) |
| `min_vol` | integer | ≥ 0 | liquidity gate (today's volume) |

Nullable fields (`long_offset_pts`, `min_width_pts`, `max_width_pts`): render an
empty input as `null` (meaning "auto / derive"), not `0`. Make this obvious in the UI
(placeholder like "auto").

## Client-side validation (mirror the server's 422s)

The server returns **422** for:
- `long_delta_hi < long_delta_lo`
- `max_width_pts < min_width_pts` (when both set)

Validate these before submit and surface the message inline. Also clamp the numeric
ranges in the table above; the API enforces them too (pydantic `ge/le`/`pattern`).

## UX

- **List view:** table of all profiles, one row per symbol. Show key columns
  (enabled, long band, target_source, width, snap, min_oi). Mark the `DEFAULT` row.
- **Edit:** click a row → form (modal or inline) prefilled from
  `GET /strike-profiles/{symbol}`. Save → `PUT`, then refresh the list.
- **Add symbol:** input for a new ticker → opens an empty/`DEFAULT`-seeded form →
  `PUT /strike-profiles/{NEW}`.
- **Fallback indicator:** if you let the user open a symbol with no row
  (`is_default: true`), show "inherits DEFAULT" and a "Create override" affordance.
- Show a small note: "Saves apply on the next poll cycle (~15s)."
- No delete endpoint exists yet — either omit delete, or request a
  `DELETE /strike-profiles/{symbol}` be added (backend change; flag it, don't fake it).

## Style

Match the existing market UI / Torque look (`market/ui/index.html`,
`market/backend/ui/torque.html`) — dark theme, `font-num`, the same card/stat idiom.
Reuse their CSS conventions rather than inventing a new visual language.

## Out of scope

- Credit spreads / condors / flies — they don't use profiles yet (future phase).
- No live order placement from this page. Config only.
