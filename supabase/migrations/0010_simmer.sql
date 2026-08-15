-- ============================================================================
-- Migration 0010 — Simmer: per-user watchlists, alert feed, and settings
-- ----------------------------------------------------------------------------
-- Simmer is the premium-selling readiness engine (see docs/simmer.md). It adds
-- three USER-SCOPED tables here; everything it computes (readiness scores,
-- research cache, news, catalysts, outcomes) is market data and lives in DuckDB
-- on the backend, keyed by symbol only and carrying no user identifier. That
-- split is deliberate: the shared computation is reusable across users, while
-- nothing about *which* tickers a given user watches is inferable from it.
--
-- All three tables are browser-managed under RLS (own row only), same posture as
-- user_settings (0003) — no secrets here, so no service_role round-trip is
-- needed just to add a ticker.
--
-- Entitlement is separate: access to the tool is gated by
-- profiles.tools_enabled containing 'simmer' (see 0009), enforced server-side.
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- watchlist — the tickers a user wants Simmer to cook
-- ---------------------------------------------------------------------------
create table if not exists public.simmer_watchlist (
    id           uuid        primary key default gen_random_uuid(),
    user_id      uuid        not null references auth.users (id) on delete cascade,
    symbol       text        not null,
    -- null = let the engine auto-pick the expiration; set = user pinned ONE expiry
    expiration   date,
    -- display order in the UI
    position     int         not null default 0,
    active       boolean     not null default true,
    -- per-ticker overrides of the TOGGLEABLE settings tier only (e.g. disable the
    -- squeeze veto for this one name). Validated server-side against the
    -- allow-list; locked safety gates can never be disabled here.
    overrides    jsonb       not null default '{}'::jsonb,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    unique (user_id, symbol)
);

comment on table public.simmer_watchlist is
    'Tickers a user has asked Simmer to monitor. One row per (user, symbol); expiration null means the engine picks.';

create index if not exists simmer_watchlist_user_idx
    on public.simmer_watchlist (user_id) where active;

-- the watcher loop reads the DISTINCT active (symbol, expiration) set across all
-- users to build its union-of-watchlists sweep
create index if not exists simmer_watchlist_sweep_idx
    on public.simmer_watchlist (symbol, expiration) where active;

drop trigger if exists simmer_watchlist_set_updated_at on public.simmer_watchlist;
create trigger simmer_watchlist_set_updated_at
    before update on public.simmer_watchlist
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- alerts — the feed of "this name became ready" transitions
-- ---------------------------------------------------------------------------
create table if not exists public.simmer_alerts (
    id           uuid        primary key default gen_random_uuid(),
    user_id      uuid        not null references auth.users (id) on delete cascade,
    symbol       text        not null,
    expiration   date        not null,
    score        numeric(5,2) not null,
    -- ready | cooling | vetoed
    state        text        not null,
    -- bull_put | bear_call | iron_condor | null when vetoed
    structure    text,
    -- FULL component breakdown at fire time, so the UI can explain *why* without
    -- recomputing, and so post-hoc calibration can ask which components predicted.
    payload      jsonb       not null default '{}'::jsonb,
    acknowledged boolean     not null default false,
    created_at   timestamptz not null default now()
);

comment on table public.simmer_alerts is
    'Per-user alert feed. One row per state transition (never per sweep); payload carries the full component breakdown for explainability and calibration.';

create index if not exists simmer_alerts_user_created_idx
    on public.simmer_alerts (user_id, created_at desc);

create index if not exists simmer_alerts_unacked_idx
    on public.simmer_alerts (user_id) where not acknowledged;

-- ---------------------------------------------------------------------------
-- settings — the tunable/toggleable tiers (see docs/simmer.md "User settings")
-- ---------------------------------------------------------------------------
-- LOCKED gates (catalyst lockout, liquidity floor, friction test, chain sanity,
-- macro-table validity) are deliberately absent: they are correctness, not
-- preference, and making them optional would also poison calibration.
create table if not exists public.simmer_settings (
    user_id            uuid        primary key references auth.users (id) on delete cascade,

    -- preset; the Advanced drawer overrides individual knobs below
    risk_profile       text        not null default 'balanced',   -- conservative|balanced|aggressive

    -- TUNABLE tier — bounded ranges, enforced server-side on write
    min_score          numeric(5,2) not null default 70,
    min_iv_percentile  numeric(5,2) not null default 40,
    min_dte            int         not null default 7,
    max_dte            int         not null default 45,
    -- short delta band is hard-clamped to 0.15-0.40 by the backend: below ~0.15
    -- is the measurably negative-EV region, so the bound is how the research
    -- reaches the user without a lecture.
    short_delta_min    numeric(4,3) not null default 0.200,
    short_delta_max    numeric(4,3) not null default 0.350,
    regime_strictness  text        not null default 'balanced',   -- relaxed|balanced|strict
    max_concurrent     int         not null default 5,

    -- TOGGLEABLE tier
    structures_enabled text[]      not null default '{bull_put,bear_call,iron_condor}',
    -- soft signals only (sentiment weighting, squeeze veto, dispersion check).
    -- Validated against the toggleable allow-list on write — a client asking to
    -- disable a LOCKED gate is rejected, not silently ignored.
    gate_overrides     jsonb       not null default '{}'::jsonb,

    notify_email       boolean     not null default false,
    prefs              jsonb       not null default '{}'::jsonb,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),

    constraint simmer_settings_dte_window   check (min_dte >= 0 and max_dte > min_dte),
    constraint simmer_settings_delta_band   check (
        short_delta_min >= 0.15 and short_delta_max <= 0.40
        and short_delta_max > short_delta_min),
    constraint simmer_settings_risk_profile check (
        risk_profile in ('conservative','balanced','aggressive')),
    constraint simmer_settings_regime       check (
        regime_strictness in ('relaxed','balanced','strict'))
);

comment on table public.simmer_settings is
    'Per-user Simmer preferences. Holds only the tunable/toggleable tiers; locked safety gates are not adjustable by design. Applied at read/alert time, so settings never multiply the compute cost of a sweep.';

comment on column public.simmer_settings.gate_overrides is
    'Toggles for SOFT signals only (sentiment, squeeze veto, dispersion check). Validated server-side against an allow-list; locked gates cannot be disabled.';

drop trigger if exists simmer_settings_set_updated_at on public.simmer_settings;
create trigger simmer_settings_set_updated_at
    before update on public.simmer_settings
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- row level security: each user fully manages their OWN rows only
-- ---------------------------------------------------------------------------
-- No signup trigger seeds these (unlike user_settings): Simmer is an entitled
-- product, so most users will never have rows here. The frontend upserts a
-- settings row on first use, and the backend falls back to the defaults above
-- when none exists.

alter table public.simmer_watchlist enable row level security;
alter table public.simmer_alerts    enable row level security;
alter table public.simmer_settings  enable row level security;

-- watchlist: full own-row CRUD from the browser
drop policy if exists simmer_watchlist_select_own on public.simmer_watchlist;
create policy simmer_watchlist_select_own
    on public.simmer_watchlist for select to authenticated
    using (auth.uid() = user_id);

drop policy if exists simmer_watchlist_insert_own on public.simmer_watchlist;
create policy simmer_watchlist_insert_own
    on public.simmer_watchlist for insert to authenticated
    with check (auth.uid() = user_id);

drop policy if exists simmer_watchlist_update_own on public.simmer_watchlist;
create policy simmer_watchlist_update_own
    on public.simmer_watchlist for update to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists simmer_watchlist_delete_own on public.simmer_watchlist;
create policy simmer_watchlist_delete_own
    on public.simmer_watchlist for delete to authenticated
    using (auth.uid() = user_id);

-- alerts: the user reads and acknowledges; only the backend (service_role)
-- writes new alert rows, so there is deliberately NO insert policy.
drop policy if exists simmer_alerts_select_own on public.simmer_alerts;
create policy simmer_alerts_select_own
    on public.simmer_alerts for select to authenticated
    using (auth.uid() = user_id);

drop policy if exists simmer_alerts_update_own on public.simmer_alerts;
create policy simmer_alerts_update_own
    on public.simmer_alerts for update to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- settings: full own-row CRUD (bounds are enforced by the CHECK constraints
-- above plus server-side validation of gate_overrides)
drop policy if exists simmer_settings_select_own on public.simmer_settings;
create policy simmer_settings_select_own
    on public.simmer_settings for select to authenticated
    using (auth.uid() = user_id);

drop policy if exists simmer_settings_insert_own on public.simmer_settings;
create policy simmer_settings_insert_own
    on public.simmer_settings for insert to authenticated
    with check (auth.uid() = user_id);

drop policy if exists simmer_settings_update_own on public.simmer_settings;
create policy simmer_settings_update_own
    on public.simmer_settings for update to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
