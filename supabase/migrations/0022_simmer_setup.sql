-- ============================================================================
-- Migration 0022 — simmer_tickers_setup + simmer_engine_settings: Simmer's
-- ticker and engine config, in the DB (docs/simmer.md → "Database-held ticker
-- and engine setup"). Same move Torque made in 0021: every setting that is data
-- rather than algorithm lives in Supabase, every backend container reads the
-- same rows and refreshes an in-memory copy, an edit applies without a redeploy,
-- and nothing per-ticker is hardcoded in code or read from a JSON file.
--
-- Backend-only (service_role); RLS on with zero policies default-denies every
-- other role, same posture as torque_tickers_setup. Seeded from today's exact
-- code + simmer_tickers.json values with `on conflict do nothing`, so re-running
-- (every `make db-setup`) never overwrites an edited row.
--
-- The JSON between the $seed$ markers is ALSO what the backend test suite loads
-- as its setup, so tests and database can't drift.
--
-- APPLY: make db-setup   (idempotent)
-- ============================================================================

-- ── Table 1: per-ticker setup ───────────────────────────────────────────────
create table if not exists public.simmer_tickers_setup (
    ticker                   text        primary key check (ticker = upper(ticker)),
    enabled                  boolean     not null default true,
    sort_order               int         not null default 100,
    default_watchlist        boolean     not null default false,   -- today's TICKERS universe
    index_data               boolean     not null default false,   -- quoted every sweep, never traded
    blocked                  boolean     not null default false,   -- can't trade or watch
    eligible                 boolean     not null default true,     -- false = chain too thin to sell
    tick                     numeric     not null default 0.01 check (tick > 0),
    tick_above_3             numeric     check (tick_above_3 is null or tick_above_3 > 0),
    min_open_interest        int,                                   -- null = the gates default
    wall_strength_floor      text        not null default 'low',    -- low | medium | high
    structures               text[]      not null default array['bull_put','bear_call','iron_condor'],
    is_index                 boolean     not null default false,    -- uses the index sentiment half-life
    squeeze_veto             boolean     not null default true,
    sector                   text,                                  -- for the sector-dislocation check
    commission_per_contract  numeric,                               -- per contract per leg one way (null = 0.35)
    extra_fee_per_contract   numeric,                               -- null = 0
    yahoo_symbol             text,                                  -- spot-quote symbol (null = bare ticker)
    daily_min_range          numeric,                               -- reserved (as on Torque's table)
    daily_max_range          numeric,
    notes                    text,
    created_at               timestamptz not null default now(),
    updated_at               timestamptz not null default now()
);

comment on table public.simmer_tickers_setup is
    'Simmer per-ticker setup (default universe, data-only symbols, blocked names, per-ticker rules, commissions, spot-quote symbol). Read by every backend container, refreshed in memory every SIMMER_SETUP_REFRESH_SEC; edits apply without a redeploy. Seeded by migration 0022 (on conflict do nothing).';

drop trigger if exists simmer_tickers_setup_set_updated_at on public.simmer_tickers_setup;
create trigger simmer_tickers_setup_set_updated_at
    before update on public.simmer_tickers_setup
    for each row execute function public.set_updated_at();

alter table public.simmer_tickers_setup enable row level security;
-- (no policies — service_role only)

-- ── Table 2: engine settings (one row per section) ──────────────────────────
create table if not exists public.simmer_engine_settings (
    section     text        primary key,   -- weights | gates | targets | decision | … | engine
    settings    jsonb       not null default '{}'::jsonb,
    notes       text,
    updated_at  timestamptz not null default now()
);

comment on table public.simmer_engine_settings is
    'Simmer engine settings, one row per section, each deep-merged OVER the code defaults (the code keeps the algorithm''s built-in defaults; the table holds only what is tuned). Seeded with today''s simmer_tickers.json overrides (gates, social) and the engine version.';

drop trigger if exists simmer_engine_settings_set_updated_at on public.simmer_engine_settings;
create trigger simmer_engine_settings_set_updated_at
    before update on public.simmer_engine_settings
    for each row execute function public.set_updated_at();

alter table public.simmer_engine_settings enable row level security;
-- (no policies — service_role only)

-- ── Seed: tickers ───────────────────────────────────────────────────────────
-- 13 default names (NVDA/TSLA min OI 1000); SPY/QQQ data-only index; VIX both
-- data and blocked; VIX3M data-only (term structure); SPX/NDX/RUT/DJX blocked.
insert into public.simmer_tickers_setup (
    ticker, sort_order, default_watchlist, index_data, blocked, eligible,
    min_open_interest, is_index, yahoo_symbol, notes)
select ticker, sort_order,
       coalesce(default_watchlist, false), coalesce(index_data, false),
       coalesce(blocked, false), coalesce(eligible, true),
       min_open_interest, coalesce(is_index, false), yahoo_symbol, notes
from jsonb_to_recordset($seed$[
  {"ticker": "AAPL",  "sort_order": 10,  "default_watchlist": true},
  {"ticker": "AMD",   "sort_order": 20,  "default_watchlist": true},
  {"ticker": "AMZN",  "sort_order": 30,  "default_watchlist": true},
  {"ticker": "AVGO",  "sort_order": 40,  "default_watchlist": true},
  {"ticker": "GOOGL", "sort_order": 50,  "default_watchlist": true},
  {"ticker": "META",  "sort_order": 60,  "default_watchlist": true},
  {"ticker": "MSFT",  "sort_order": 70,  "default_watchlist": true},
  {"ticker": "MU",    "sort_order": 80,  "default_watchlist": true},
  {"ticker": "NFLX",  "sort_order": 90,  "default_watchlist": true},
  {"ticker": "NVDA",  "sort_order": 100, "default_watchlist": true, "min_open_interest": 1000},
  {"ticker": "PLTR",  "sort_order": 110, "default_watchlist": true},
  {"ticker": "SMCI",  "sort_order": 120, "default_watchlist": true},
  {"ticker": "TSLA",  "sort_order": 130, "default_watchlist": true, "min_open_interest": 1000},
  {"ticker": "SPY",   "sort_order": 200, "index_data": true, "is_index": true, "eligible": false,
   "notes": "data only — regime gate"},
  {"ticker": "QQQ",   "sort_order": 210, "index_data": true, "is_index": true, "eligible": false,
   "notes": "data only — regime gate"},
  {"ticker": "VIX",   "sort_order": 220, "index_data": true, "blocked": true, "is_index": true,
   "eligible": false, "yahoo_symbol": "^VIX", "notes": "data only — regime; never traded"},
  {"ticker": "VIX3M", "sort_order": 230, "eligible": false, "yahoo_symbol": "^VIX3M",
   "notes": "data only — VIX term-structure slope"},
  {"ticker": "SPX",   "sort_order": 300, "blocked": true, "eligible": false, "is_index": true,
   "notes": "index product — excluded from trading (Matrix's domain)"},
  {"ticker": "NDX",   "sort_order": 310, "blocked": true, "eligible": false, "is_index": true,
   "notes": "index product — excluded from trading"},
  {"ticker": "RUT",   "sort_order": 320, "blocked": true, "eligible": false, "is_index": true,
   "notes": "index product — excluded from trading"},
  {"ticker": "DJX",   "sort_order": 330, "blocked": true, "eligible": false, "is_index": true,
   "notes": "index product — excluded from trading"}
]$seed$::jsonb) as s(
    ticker text, sort_order int, default_watchlist boolean, index_data boolean,
    blocked boolean, eligible boolean, min_open_interest int, is_index boolean,
    yahoo_symbol text, notes text)
on conflict (ticker) do nothing;

-- ── Seed: engine settings ───────────────────────────────────────────────────
-- Exactly today's simmer_tickers.json overrides (gates with dte_min=0, social)
-- plus the engine version. Each section is deep-merged OVER the code defaults,
-- so seeding only the tuned keys reproduces today's resolved() byte-for-byte.
insert into public.simmer_engine_settings (section, settings, notes)
select section, settings, notes
from jsonb_to_recordset($eng$[
  {"section": "gates",
   "settings": {"dte_min": 0, "dte_max": 45, "short_delta_min": 0.20, "short_delta_max": 0.35,
                "iv_percentile_floor": 40.0, "vrp_ratio_floor": 1.15},
   "notes": "dte_min deliberately 0 (vs code's conservative 7) so same-day/0DTE expirations are eligible"},
  {"section": "social",
   "settings": {"enabled": true, "handle": "@facades_simmer",
                "x_url": "https://x.com/facades_simmer",
                "linkedin_url": "https://www.linkedin.com/showcase/facades-simmer"},
   "notes": "product X / LinkedIn profile chips"},
  {"section": "engine",
   "settings": {"engine_version": "simmer-engine-1.0.0"},
   "notes": "engine version — bump when weights/gates/targets/decision change"}
]$eng$::jsonb) as s(section text, settings jsonb, notes text)
on conflict (section) do nothing;
