-- ============================================================================
-- Migration 0026 — overnight movers: RelativeQs import + premium-decision analyzer
-- (docs/simmer.md → "Overnight movers — RelativeQs import + premium-decision
-- analyzer"). Adds the per-user toggles + import source flag, the shared
-- user-agnostic analysis table, and the RelativeQs base-URL config row.
--
-- APPLY: make db-setup   (idempotent)
-- ============================================================================

-- Toggle 1 (import RQ movers) + toggle 2 (analyze ALL watchlist items), per user.
alter table public.simmer_settings
    add column if not exists import_rq_overnight   boolean not null default false;
alter table public.simmer_settings
    add column if not exists overnight_analyze_all boolean not null default false;

-- Where a watchlist row came from — drives the (RQ) badge. 'user' | 'rq'.
alter table public.simmer_watchlist
    add column if not exists source text not null default 'user';

-- RelativeQs base URL (non-sensitive; /api/premarket is public today). Fill the
-- value after apply; backend reads it via service_role. No token row needed yet.
insert into public.app_config (key, value) values ('relativeqs_api_base', null)
on conflict (key) do nothing;

-- ── Shared, user-agnostic overnight analysis ────────────────────────────────
-- One row per (symbol, expiration, trading day): the frozen baseline (last-night
-- close, set once at 09:25), the live read, the one-line thesis, and the single
-- verdict. Every user watching the name reads the same row (the readiness math
-- is already user-agnostic). The UI reads it through a backend route, never
-- directly — RLS on, no policies (service_role only).
create table if not exists public.simmer_overnight (
    symbol         text        not null check (symbol = upper(symbol)),
    expiration     date        not null,
    session_date   date        not null,              -- ET trading day
    dir            text        not null,              -- up | down
    gap            numeric     not null,              -- signed overnight fraction
    crowd          text,                              -- RelativeQs peer tag (sector_wide/lone/…)
    thesis         text,                              -- one-line "why it moved"
    side           text,                              -- option side tracked (toward the move)
    strikes        jsonb       not null default '[]'::jsonb,   -- strikes toward the move
    baseline       jsonb       not null default '{}'::jsonb,   -- FROZEN last-night premiums {strike: mid}
    spot_baseline  numeric,                           -- underlying at baseline (9:25)
    latest         jsonb       not null default '{}'::jsonb,   -- most recent live premiums {strike: mid}
    volume         jsonb       not null default '{}'::jsonb,   -- live volume {strike: n}
    verdict        jsonb       not null default '{}'::jsonb,   -- {call, premium_trend, underlying_trend, confidence, reasons}
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    primary key (symbol, expiration, session_date)
);

comment on table public.simmer_overnight is
    'Shared overnight analysis per (symbol, expiration, trading day): frozen 9:25 baseline premiums, live read, one-line thesis, and the single sell/wait/skip verdict. Read by the backend, surfaced in the Overnight card tab.';

create index if not exists simmer_overnight_session_idx
    on public.simmer_overnight (session_date);

drop trigger if exists simmer_overnight_set_updated_at on public.simmer_overnight;
create trigger simmer_overnight_set_updated_at
    before update on public.simmer_overnight
    for each row execute function public.set_updated_at();

alter table public.simmer_overnight enable row level security;
-- (no policies — service_role only)
