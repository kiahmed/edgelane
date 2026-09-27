-- ============================================================================
-- Migration 0015 — news_reactor_signals becomes the full signal log
-- ----------------------------------------------------------------------------
-- It is the first (and only) place an incoming signal is persisted, so it
-- stores the whole payload plus what Torque decided, not just the dedupe key.
-- Written atomically with the idempotency claim (same INSERT); `outcome` /
-- `reason` are filled in once the handler reaches a decision.
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

alter table public.news_reactor_signals
    add column if not exists payload          jsonb,
    add column if not exists symbol           text,
    add column if not exists state            text,
    add column if not exists direction        text,
    add column if not exists cancels_event_id text,
    add column if not exists generated_at     timestamptz,
    add column if not exists outcome          text,     -- placed | dropped | cancel_processed
    add column if not exists reason           text;

comment on table public.news_reactor_signals is
    'Every authenticated news-reactor signal Torque received: full payload, key fields, and the outcome/reason Torque reached. PK source_event_id doubles as replay protection. Purged after 24h (backend, on the poller''s open->closed transition).';

create index if not exists news_reactor_signals_symbol_idx
    on public.news_reactor_signals (symbol, received_at desc);
