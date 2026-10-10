-- ============================================================================
-- Migration 0021 — torque_tickers_setup: Torque's per-ticker setup, in the DB
-- ----------------------------------------------------------------------------
-- The single source of truth for which tickers Torque trades and how: price
-- step, strike offsets per strategy shape, spot-quote symbol, option-root
-- allowlist, fees, stop-loss / profit-target defaults, market-order policy and
-- the Counter Read noise floor. Every backend container reads this same table
-- (no per-container JSON file, nothing hardcoded), refreshing its in-memory
-- copy every TORQUE_TICKERS_REFRESH_SEC — an edit here reaches all of them
-- within that window, no redeploy or restart.
--
-- Columns:
--   enabled          false = hidden from the ticker list and ignored by news
--                    signals (settings are still used for any open position)
--   sort_order       list order; the first enabled ticker is the page default
--   tick             minimum price increment (the step below $3 for
--                    penny-pilot names)
--   tick_above_3     the step once a price is >= $3 (penny pilot: 0.05;
--                    SPX/NDX/RUT: 0.10); null = `tick` at every price
--   yahoo_symbol     primary live spot quote (null = Tradier put-call parity)
--   allowed_roots    option roots allowed (null = any; DJX trades DJXW only)
--   offsets          strike offsets per strategy shape, merged over the
--                    built-in base offsets: single / vertical / condor /
--                    iron_fly / butterfly
--   commission_per_contract, extra_fee_per_contract
--                    per contract per leg, one way (null = global default)
--   stop_loss_default, close_target_default, close_target_min
--                    percent (null = global default)
--   close_target_spread_scaled, close_target_required, market_orders_allowed
--   richness_floor   Counter Read noise floor (null = global default)
--   daily_min_range, daily_max_range
--                    reserved for later use; not read by Torque yet
--
-- Seeded with the values previously hardcoded in torque_config.py, plus IWM,
-- XLF, XLE and SMH (Reflex engine_v2 symbols). `on conflict do nothing`: this
-- file is re-run by every `make db-setup`, and must never overwrite a value
-- someone has since changed in the table.
--
-- APPLY: make db-setup   (idempotent)
-- ============================================================================

create table if not exists public.torque_tickers_setup (
    ticker                      text        primary key check (ticker = upper(ticker)),
    enabled                     boolean     not null default true,
    sort_order                  int         not null default 100,
    tick                        numeric     not null default 0.05 check (tick > 0),
    tick_above_3                numeric     check (tick_above_3 is null or tick_above_3 > 0),
    yahoo_symbol                text,
    allowed_roots               text[],
    offsets                     jsonb       not null default '{}'::jsonb,
    commission_per_contract     numeric,
    extra_fee_per_contract      numeric,
    stop_loss_default           numeric,
    close_target_default        numeric,
    close_target_min            numeric,
    close_target_spread_scaled  boolean     not null default false,
    close_target_required       boolean     not null default false,
    market_orders_allowed       boolean     not null default true,
    richness_floor              numeric,
    daily_min_range             numeric,
    daily_max_range             numeric,
    notes                       text,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

comment on table public.torque_tickers_setup is
    'Torque per-ticker setup (which tickers trade, price steps, strike offsets, fees, exit defaults). Read by every backend container and refreshed in memory every TORQUE_TICKERS_REFRESH_SEC; edits apply without a redeploy. Seeded by migration 0021 (on conflict do nothing).';

drop trigger if exists torque_tickers_setup_set_updated_at on public.torque_tickers_setup;
create trigger torque_tickers_setup_set_updated_at
    before update on public.torque_tickers_setup
    for each row execute function public.set_updated_at();

-- Backend-only (service_role); no browser access. RLS with zero policies
-- default-denies every other role, same posture as torque_orders.
alter table public.torque_tickers_setup enable row level security;

-- Seed. The JSON between the $seed$ markers is also what the backend test
-- suite loads as its ticker setup, so tests always run against exactly what
-- this migration installs.
insert into public.torque_tickers_setup (
    ticker, sort_order, tick, tick_above_3, yahoo_symbol, allowed_roots, offsets,
    commission_per_contract, stop_loss_default, close_target_default, close_target_min,
    close_target_spread_scaled, close_target_required, market_orders_allowed,
    richness_floor, notes)
select ticker, sort_order, tick, tick_above_3, yahoo_symbol, allowed_roots,
       coalesce(offsets, '{}'::jsonb),
       commission_per_contract, stop_loss_default, close_target_default, close_target_min,
       coalesce(close_target_spread_scaled, false), coalesce(close_target_required, false),
       coalesce(market_orders_allowed, true), richness_floor, notes
from jsonb_to_recordset($seed$[
  {"ticker": "NDX", "sort_order": 10, "tick": 0.05, "tick_above_3": 0.10, "yahoo_symbol": "^NDX",
   "offsets": {"vertical": {"anchor": 20, "width": 100}, "condor": {"short_offset": 100, "width": 100},
               "iron_fly": {"width": 100}, "butterfly": {"body_offset": 0, "wing": 100}},
   "commission_per_contract": 0.95, "richness_floor": 0.05,
   "notes": "Nasdaq-100 index, 5pt grid; $0.05 below $3, $0.10 at/above"},
  {"ticker": "SPX", "sort_order": 20, "tick": 0.05, "tick_above_3": 0.10, "yahoo_symbol": "^GSPC",
   "offsets": {"vertical": {"anchor": 5, "width": 25}, "condor": {"short_offset": 25, "width": 25},
               "iron_fly": {"width": 25}, "butterfly": {"body_offset": 0, "wing": 25}},
   "commission_per_contract": 0.95, "richness_floor": 0.05,
   "notes": "~7490, 5pt grid; $0.05 below $3, $0.10 at/above"},
  {"ticker": "RUT", "sort_order": 30, "tick": 0.05, "tick_above_3": 0.10, "yahoo_symbol": "^RUT",
   "offsets": {"vertical": {"anchor": 5, "width": 20}, "condor": {"short_offset": 20, "width": 20},
               "iron_fly": {"width": 20}, "butterfly": {"body_offset": 0, "wing": 20}},
   "commission_per_contract": 0.95, "richness_floor": 0.05,
   "notes": "~2960, 5pt grid; $0.05 below $3, $0.10 at/above"},
  {"ticker": "SPY", "sort_order": 40, "tick": 0.01, "yahoo_symbol": "SPY",
   "offsets": {"vertical": {"anchor": 1, "width": 5}, "condor": {"short_offset": 5, "width": 5},
               "iron_fly": {"width": 5}, "butterfly": {"body_offset": 0, "wing": 5}},
   "commission_per_contract": 0.35, "richness_floor": 0.02,
   "notes": "~746, 1pt grid; penny increments at every price"},
  {"ticker": "QQQ", "sort_order": 50, "tick": 0.01, "yahoo_symbol": "QQQ",
   "offsets": {"vertical": {"anchor": 1, "width": 5}, "condor": {"short_offset": 5, "width": 5},
               "iron_fly": {"width": 5}, "butterfly": {"body_offset": 0, "wing": 5}},
   "commission_per_contract": 0.35, "richness_floor": 0.02,
   "notes": "~738, 1pt grid; penny increments at every price"},
  {"ticker": "DJX", "sort_order": 60, "tick": 0.01, "yahoo_symbol": "^DJX", "allowed_roots": ["DJXW"],
   "offsets": {"vertical": {"anchor": 1, "width": 10}, "condor": {"short_offset": 10, "width": 10},
               "iron_fly": {"width": 10}, "butterfly": {"body_offset": 0, "wing": 10}},
   "commission_per_contract": 0.53, "richness_floor": 0.02,
   "stop_loss_default": 50, "close_target_default": 60, "close_target_min": 45,
   "close_target_spread_scaled": true, "close_target_required": true, "market_orders_allowed": false,
   "notes": "DJXW root only, ~525, penny-quoted; wide book: wider widths, mandatory auto-close, no market orders"},
  {"ticker": "IWM", "sort_order": 70, "tick": 0.01, "yahoo_symbol": "IWM",
   "offsets": {"vertical": {"anchor": 1, "width": 5}, "condor": {"short_offset": 5, "width": 5},
               "iron_fly": {"width": 5}, "butterfly": {"body_offset": 0, "wing": 5}},
   "commission_per_contract": 0.35, "richness_floor": 0.02,
   "notes": "~240, 1pt grid; penny increments at every price. Spread offsets are a starting point."},
  {"ticker": "XLF", "sort_order": 80, "tick": 0.01, "tick_above_3": 0.05, "yahoo_symbol": "XLF",
   "offsets": {"vertical": {"anchor": 0.5, "width": 1}, "condor": {"short_offset": 1, "width": 1},
               "iron_fly": {"width": 1}, "butterfly": {"body_offset": 0, "wing": 1}},
   "commission_per_contract": 0.35, "richness_floor": 0.02,
   "notes": "~50, 0.5-1pt grid; penny pilot ($0.01 below $3, $0.05 at/above). Spread offsets are a starting point."},
  {"ticker": "XLE", "sort_order": 90, "tick": 0.01, "tick_above_3": 0.05, "yahoo_symbol": "XLE",
   "offsets": {"vertical": {"anchor": 1, "width": 2}, "condor": {"short_offset": 2, "width": 2},
               "iron_fly": {"width": 2}, "butterfly": {"body_offset": 0, "wing": 2}},
   "commission_per_contract": 0.35, "richness_floor": 0.02,
   "notes": "~90, 1pt grid; penny pilot ($0.01 below $3, $0.05 at/above). Spread offsets are a starting point."},
  {"ticker": "SMH", "sort_order": 100, "tick": 0.01, "tick_above_3": 0.05, "yahoo_symbol": "SMH",
   "offsets": {"vertical": {"anchor": 2, "width": 5}, "condor": {"short_offset": 5, "width": 5},
               "iron_fly": {"width": 5}, "butterfly": {"body_offset": 0, "wing": 5}},
   "commission_per_contract": 0.35, "richness_floor": 0.02,
   "notes": "~280, 1-5pt grid; penny pilot ($0.01 below $3, $0.05 at/above). Spread offsets are a starting point."}
]$seed$::jsonb) as s(
    ticker text, sort_order int, tick numeric, tick_above_3 numeric, yahoo_symbol text,
    allowed_roots text[], offsets jsonb, commission_per_contract numeric,
    stop_loss_default numeric, close_target_default numeric, close_target_min numeric,
    close_target_spread_scaled boolean, close_target_required boolean,
    market_orders_allowed boolean, richness_floor numeric, notes text)
on conflict (ticker) do nothing;
