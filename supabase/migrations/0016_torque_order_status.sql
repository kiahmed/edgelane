-- ============================================================================
-- Migration 0016 — torque_orders tracks each signal order's live status
-- ----------------------------------------------------------------------------
-- The broker, not Torque, is the source of truth for an order's state, but a
-- signal order's fate (rejected on arrival, retried on the next expiry,
-- unwound by a cancel signal, cancelled by hand in Torque or at the broker,
-- filled) must be visible here too. Updated at placement (the confirmed
-- post-submit status), by the cancel-signal handler, by Torque's cancel
-- button, and by the orders panel's broker poll (which catches anything done
-- directly at the broker).
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

alter table public.torque_orders
    add column if not exists status        text,          -- broker status / Torque action (pending, rejected, canceled, filled, closed_at_market, …)
    add column if not exists status_reason text,
    add column if not exists status_at     timestamptz,
    add column if not exists expiration    date,          -- the contract expiry actually traded
    add column if not exists attempt       int not null default 1;   -- 2 = retried on the next expiry after a same-contract rejection

create index if not exists torque_orders_entry_order_idx
    on public.torque_orders (entry_order_id);
