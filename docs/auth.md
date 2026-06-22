# EdgeLane Auth & Gating — backend contract

Multi-user, public-facing. **Frontend (Vercel) does signup/login against Supabase
directly; the backend only verifies the JWT it attaches.** This doc is the
contract the UI session builds against.

## Tiers

| Tier | Who | How the backend authorizes | What they get |
|---|---|---|---|
| **Teaser** | anonymous | `X-EdgeLane-Session: <anon token>` (minted after Turnstile) | `/snapshot/*` **redacted** — spot + bias (walls) only; `strategies`/`engine_pick` stripped, `teaser:true`, `locked:[...]` |
| **Product** | signed-up user | `Authorization: Bearer <supabase JWT>` | full snapshot, picks, scoring, all strategies, Lookup, orders |
| **Admin** | ops/curl | `X-Admin-Token: <ADMIN_API_TOKEN>` | full (server secret, never in a browser) |

`AUTH_ENABLED=false` (dev/tests) → all gates open, no rate limit. `true` (prod) → enforced.

## Endpoints

- `POST /session/anon` — body `{ "turnstile_token": "<cf-turnstile-response>" }`.
  Verifies Turnstile with Cloudflare, returns `{ session_token, expires_in, sid }`.
  Send `session_token` back as the `X-EdgeLane-Session` header on teaser calls.
  (Dev skips Turnstile but still issues a token, so the FE flow is identical.)
- `GET /snapshot/{symbol}` / `GET /snapshot` — `require_teaser_session`: full for
  user/admin/dev, redacted teaser for a valid anon session, else 401.
- `POST /orders/preview` · `POST /orders/submit` — `get_current_user` (401 if
  unauthenticated). Uses the user's OWN broker token from `broker_configs` when
  present; otherwise falls back to the system token (dev/admin/operator).
- `POST /broker/test` — body `{ "id": "<broker_configs.id>" | null }`,
  `get_current_user` (signed-in user only). Decrypts that connection's token
  server-side and probes Tradier `/v1/user/profile`; returns
  `{ ok, env, account_id }` — never the token. (The browser can't read the
  token back, so connection validation lives here, not client-side.)

## Frontend flow (UI session)

1. On load: render a Cloudflare Turnstile widget → `POST /session/anon` → store
   `session_token`; send it as `X-EdgeLane-Session` on teaser fetches.
2. Show the 3 teaser chips (spot/bias/walls); blur everything below + the Lookup
   tab behind a signup/signin modal (Supabase Auth).
3. After login: send `Authorization: Bearer <supabase access_token>` on all
   calls; drop the anon header. Full data unlocks.
4. Broker-config form → writes the user's Tradier token/account to
   `broker_configs` (RLS: own rows only) so their orders use their own account.

## Config keys (`edgelane_market.config`) — backend (server secrets)

`AUTH_ENABLED`, `SUPABASE_URL`, `SUPABASE_JWT_SECRET` (legacy HS256 only),
`SUPABASE_SERVICE_KEY`, `TURNSTILE_SECRET`, `ANON_SESSION_SECRET`,
`ANON_SESSION_TTL_SEC`, `ADMIN_API_TOKEN`, `RATE_LIMIT_PER_MIN`,
`RATE_LIMIT_WINDOW_SEC`. See `edgelane_market.config.example`.

## Frontend (browser-safe) keys — `dist/edgelane.config.js`

The deployed market UI (`market/ui/index.html`) reads three public globals,
baked by `deploy.sh` from `EDGELANE_*` env vars (browser-safe by design — the
anon key is RLS-gated, the Turnstile site key is public):

- `window.__EDGELANE_SUPABASE_URL__`
- `window.__EDGELANE_SUPABASE_ANON_KEY__`
- `window.__EDGELANE_TURNSTILE_SITE_KEY__`

(plus `window.__EDGELANE_API_BASE__`). See `market/ui/edgelane.config.example.js`.
**If the Supabase values are absent the UI runs in DEV-BYPASS** — gate disabled,
full dashboard open, no login (mirrors `AUTH_ENABLED=false`). Never put
SERVICE_KEY / TURNSTILE_SECRET / JWT_SECRET in this file.

## UI (built)

`market/ui/index.html` ships the gate: the top-3 teaser chips stay crisp while
everything `[data-gated]` is blurred behind a signup/signin card (Supabase Auth +
Turnstile). After login the profile panel (top-right) manages **per-user broker
connections** (written to `broker_configs` via the anon key under RLS — add /
edit / test / set-default / remove) and **notification toggles** (`user_settings`,
migration `0003`). All signed-up users currently get full access — subscription
gating comes later.

**Per-tab sessions:** the Supabase client persists the session in `sessionStorage`
(not the default `localStorage`), keyed `edgelane-auth-<project-ref>`. So two
different users in two tabs of the same origin never clobber each other's token;
**duplicating** a signed-in tab carries the session (browsers copy sessionStorage)
→ auto-signed-in; a brand-new tab starts signed-out. Local (`localhost`) vs prod
(deployed domain) are already isolated — different origins, separate storage.

**Cross-tab sign-out:** because sessionStorage is per-tab, Supabase's
localStorage-based multi-tab sync doesn't fire, so signing out broadcasts over a
`BroadcastChannel` (`edgelane-auth-bc-<ref>`) **scoped to the user id** — the
signed-in user's other tabs (duplicates) follow it out, but a *different* user
signed in elsewhere on the same origin is unaffected.

## Supabase

Project **edgelane** (ref `wfezpfswpywsmbnjrbri`, org Solution Jet) — not shared
with RelativeQs. Migrations live in `supabase/migrations/*.sql` and are applied
idempotently with **`make db-push`** (`make db-push-dry` to preview). A full
`make deploy-prod` runs the push automatically first (`--skip-db` to opt out);
`deploy-be`/`deploy-fe` never touch the DB. Needs `SUPABASE_PROJECT_REF` +
`SUPABASE_ACCESS_TOKEN` in `deploy/.env`.

### Broker token encryption at rest (migration `0004`)

`broker_configs.tradier_token` is **encrypted at rest** and **write-only from
the browser**:

- A symmetric key lives in **Supabase Vault** (`edgelane_broker_key`, encrypted
  by the project root key — not stored beside the data).
- A `BEFORE INSERT/UPDATE` trigger (`broker_configs_encrypt_token`) encrypts any
  incoming plaintext into `tradier_token_enc` (bytea, pgcrypto), records
  `token_last4`, and **NULLs the plaintext column**. The browser write path is
  unchanged — it still sends `tradier_token`; the trigger swallows it.
- The backend decrypts only via the `service_role`-only `SECURITY DEFINER` RPC
  `public.get_broker_secret(p_user_id[, p_id])` (`supabase_admin.get_broker_config`).
  `anon`/`authenticated` are **revoked** from it.
- The browser never reads the token back: it shows `token_last4`, edits are
  "leave blank to keep," and validation goes through `POST /broker/test`.

### Per-user Webull (migration `0006`)

Webull is supported alongside Tradier — each user connects their OWN Webull
OpenAPI app. Webull auth is an **`app_key` + `app_secret`** pair (+ region/env),
not a bearer token; we never ship app keys. Same at-rest pattern as Tradier:
browser writes plaintext, trigger encrypts (`webull_app_key_enc` /
`webull_app_secret_enc`), nulls plaintext, stores `webull_app_key_last4`.

**UI form (built in `market/ui/index.html`):** the broker dropdown offers
Tradier **and Webull**; selecting a broker swaps the field set, and the Webull
fields show the trade-authorization warning inline. Submitting writes a
`broker_configs` row (anon key, RLS own-row); the "Test" button calls
`POST /broker/test` which branches on `broker` server-side. The row columns:
- `broker = 'webull'`
- `webull_app_key`, `webull_app_secret` (plaintext in; encrypted at rest by the trigger)
- `webull_region` (default `'us'`), `webull_env` (`'production'` | `'uat'` for paper)
- `webull_account_id` (optional — backend auto-resolves the first account if blank)

Display the saved connection with `webull_app_key_last4` (never the secret).
Editing: leave the key/secret blank to keep the stored values.

`POST /broker/test` branches on `broker`: for Webull it validates by listing the
user's accounts via the signed SDK (returns `{ok, broker:'webull', account_id,
hint}`). The Webull response always carries a short `hint` —
*"Webull production requires the account owner to authorize the app for trading
before its token activates."* — **show this during Webull setup and on a failed
test** (the form already renders `hint` inline). A not-yet-authorized app makes
the SDK's token poll hang; the backend caps that at 20s and returns this hint as
the error instead of blocking ~5 min.
Orders (`/orders/preview` · `/orders/submit`) branch too — the user's `broker`
selects the Tradier or Webull path automatically; the Webull path maps the same
candidate to a `preview_option`/`place_option` payload.

**Live-validated** against Webull's UAT API (2026-06-20, using Webull's public
demo sandbox keys — we have no Webull account of our own yet): connection +
account list, and `preview_option` for SINGLE, VERTICAL (bull_put), and
IRON_CONDOR all return clean estimates through our builder. Other mappings
(butterfly) follow the same shape but weren't individually previewed; production
uses each user's own creds via the UI form, and every order previews before submit.

## Abuse / DDoS

Real shield = Cloudflare edge (WAF + rate-limit rules + Turnstile) in front of
the tunnel; the home-PC origin can't absorb a flood alone. App-level
per-session token-bucket (`ratelimit.py`) is fairness only, keyed on bearer/anon
token, falling back to `CF-Connecting-IP`.
