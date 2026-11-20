-- ============================================================================
-- Migration 0034 — per-ticker Reflex on/off (torque_tickers_setup.reflex_enabled).
--
-- false = Reflex (facades-news-reactor) ENTRY signals for this ticker are
-- refused ("Reflex signals are off for <T>"); the ticker stays tradeable on
-- the Torque page, cancels for orders Reflex already placed are still
-- processed, and open positions keep their exits. Default true, so every
-- existing ticker keeps today's behaviour. Live within
-- TORQUE_TICKERS_REFRESH_SEC — no restart. See docs/torque.md.
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

alter table public.torque_tickers_setup
    add column if not exists reflex_enabled boolean not null default true;

comment on column public.torque_tickers_setup.reflex_enabled is
    'false = Reflex entry signals for this ticker are refused (page trading and cancels unaffected). Default true.';
