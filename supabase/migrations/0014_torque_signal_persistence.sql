-- ============================================================================
-- Migration 0014 — Torque news-signal idempotency, order correlation, and
-- full watcher-state persistence
-- ----------------------------------------------------------------------------
-- Three backend-only tables (service_role writes/reads only — no browser
-- access, no RLS policies, same posture as broker_configs). Deliberately in
-- Supabase rather than the backend's own DuckDB file: these are low-frequency,
-- low-volume rows (one per signal, one per resulting order, one per active
-- watcher) where a network round-trip is a non-issue, and DuckDB is
-- single-process by design — it does not support multiple backend containers
-- safely touching the same file, which this needs to survive.
--
-- news_reactor_signals: has this source_event_id already been acted on?
--   Pure replay/idempotency for POST /webhook/news_signal — nothing here
--   describes the resulting order(s); see torque_orders for that.
--
-- torque_orders: which order(s), on which account, came from which signal.
--   Read by a CANCEL signal (state="cancel"/cancels_event_id — see
--   docs/torque.md "CANCEL: unwinding a prior signal") to find and act on
--   exactly the right order(s) across every account that received the
--   original signal.
--
-- torque_watchers: the live state _watch_and_close/_monitor_stop/the
-- stop-exit ladder holds in-memory today (see docs/torque.md "Multi-user
-- order persistence"). Checkpointed at state TRANSITIONS (not on every price
-- tick), so a backend restart can resume in-flight monitoring instead of
-- losing it — the in-memory dict stays authoritative for a running process;
-- this is the recovery path for when that process didn't stay running.
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

create table if not exists public.news_reactor_signals (
    source_event_id text        primary key,
    received_at     timestamptz not null default now()
);

comment on table public.news_reactor_signals is
    'Idempotency for POST /webhook/news_signal — one row per source_event_id ever acted on. Purged after 24h (backend, on the poller''s open->closed transition). Says nothing about the resulting order(s); see torque_orders.';

create index if not exists news_reactor_signals_received_idx
    on public.news_reactor_signals (received_at);

-- ---------------------------------------------------------------------------
create table if not exists public.torque_orders (
    id              uuid        primary key default gen_random_uuid(),
    source_event_id text        not null,
    uid             uuid        not null references auth.users (id) on delete cascade,
    entry_order_id  text        not null,
    symbol          text        not null,
    strategy        text        not null,
    quantity        int         not null,
    is_single       boolean     not null,
    entry_type      text        not null,        -- debit | credit
    legs            jsonb       not null,         -- original legs, needed to build closes later
    tick            numeric     not null,
    placed_at       timestamptz not null default now()
);

comment on table public.torque_orders is
    'Which order, on which account, resulted from which news-signal event. One row per (signal, qualified account) pair — a single signal fans out to N accounts, N rows. Correlation index for a future CANCEL signal to find and market-close whatever actually filled.';

create index if not exists torque_orders_source_event_idx
    on public.torque_orders (source_event_id);

create index if not exists torque_orders_uid_idx
    on public.torque_orders (uid);

-- ---------------------------------------------------------------------------
create table if not exists public.torque_watchers (
    entry_order_id      text        primary key,
    uid                 uuid        not null references auth.users (id) on delete cascade,
    account_id          text,
    symbol              text        not null,
    strategy            text        not null,
    legs                jsonb       not null,
    is_single           boolean     not null,
    entry_type          text        not null,
    quantity            int         not null,
    tick                numeric     not null,
    auto_close          boolean     not null default true,
    close_target_pct    numeric,
    stop_loss_pct       numeric,
    floor_pct           numeric     not null default 1.0,
    state               text        not null default 'watching_fill',
    entry_status        text,
    entry_fill          numeric,
    close_order_id      text,
    close_target_price  numeric,
    stop_order_id       text,
    stop_exit_price     numeric,
    stop_market_fallback boolean,
    stop_note           text,
    done                boolean     not null default false,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

comment on table public.torque_watchers is
    'Checkpointed state of an in-flight auto-close/stop-loss watcher, at transitions (not every price tick). Read once at backend startup to resume monitoring positions a restart would otherwise have dropped; the in-memory _WATCHERS dict remains authoritative for a running process. Rows with done=true are purged after 24h alongside news_reactor_signals.';

create index if not exists torque_watchers_uid_idx
    on public.torque_watchers (uid);

create index if not exists torque_watchers_resume_idx
    on public.torque_watchers (done) where not done;

drop trigger if exists torque_watchers_set_updated_at on public.torque_watchers;
create trigger torque_watchers_set_updated_at
    before update on public.torque_watchers
    for each row execute function public.set_updated_at();

-- No RLS policies on any of the three: service_role (the backend) is the
-- only reader/writer. Enabling RLS with zero policies default-denies every
-- other role, same posture as broker_configs.
alter table public.news_reactor_signals enable row level security;
alter table public.torque_orders        enable row level security;
alter table public.torque_watchers      enable row level security;
