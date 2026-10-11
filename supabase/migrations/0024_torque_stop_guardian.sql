-- ============================================================================
-- Migration 0024 — Torque stop guardian: saved stop-loss settings + per-
-- position guard state
-- ----------------------------------------------------------------------------
-- torque_sl_settings: the Torque page's stop-loss toggle and %, saved per
--   broker account and ticker (it used to live only in the browser tab). The
--   page reads and writes it; the stop guardian applies it to positions in
--   that ticker. Torque-placed positions: no row = on, at the ticker's default.
--   Positions opened outside Torque (Tradier's site, the tp CLI): guarded only
--   when a row has stop-loss ON — no row = not guarded. Reflex positions
--   ignore it: they always get the ticker default (or 30%).
--
-- torque_guarded_positions: what the guardian knows about each open position
--   ("unit" = one single option, or a spread confirmed by the order history):
--   where it came from, its stop, and its state. Persisted so a restart keeps
--   it — in particular "past its stop when first seen" (alert only, never
--   auto-closed) and "Reflex" (always guarded) survive. A row is removed once
--   its position is gone.
--
--   account_key = "<live|sandbox>:<broker account id>"
--   unit_key    = the unit's option symbols, sorted, joined by "|"
--
-- Backend-only (service_role); RLS on with zero policies, like torque_orders.
-- APPLY: make db-setup   (idempotent)
-- ============================================================================

create table if not exists public.torque_sl_settings (
    account_key  text        not null,
    ticker       text        not null check (ticker = upper(ticker)),
    enabled      boolean     not null default true,
    pct          numeric     check (pct is null or (pct > 0 and pct < 100)),
    updated_by   text,
    updated_at   timestamptz not null default now(),
    primary key (account_key, ticker)
);

comment on table public.torque_sl_settings is
    'Torque page stop-loss setting per broker account + ticker (on/off + %). Applied by the stop guardian to non-Reflex positions in that ticker: Torque-placed with no row = on at the ticker default; opened outside Torque = guarded only when a row has it on.';

create table if not exists public.torque_guarded_positions (
    account_key    text        not null,
    unit_key       text        not null,
    ticker         text        not null,
    symbols        text[]      not null,
    source         text        not null,     -- reflex | torque | outside
    state          text        not null,     -- watching | sl_off | past_stop_alert | broker_stop | exiting | exited | unconfirmed | watcher_managed
    note           text,
    stop_pct       numeric,
    entry_price    numeric,
    last_mark      numeric,
    first_seen_at  timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    primary key (account_key, unit_key)
);

comment on table public.torque_guarded_positions is
    'Torque stop guardian: one row per open position unit per broker account (source, stop, state). Removed when the position is gone.';

drop trigger if exists torque_sl_settings_set_updated_at on public.torque_sl_settings;
create trigger torque_sl_settings_set_updated_at
    before update on public.torque_sl_settings
    for each row execute function public.set_updated_at();

drop trigger if exists torque_guarded_positions_set_updated_at on public.torque_guarded_positions;
create trigger torque_guarded_positions_set_updated_at
    before update on public.torque_guarded_positions
    for each row execute function public.set_updated_at();

alter table public.torque_sl_settings       enable row level security;
alter table public.torque_guarded_positions enable row level security;
