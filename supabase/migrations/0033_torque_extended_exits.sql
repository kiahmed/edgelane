-- ============================================================================
-- Migration 0033 — separate stop-loss / take-profit for extended-hours entries.
--
-- For the broker's extended-hours tickers, exit values follow the clock: in
-- Torque's extended session (inside TORQUE_TRADING_HOURS, outside 09:30-16:00)
-- every entry is armed with these, and the stop guardian re-prices Torque's
-- resting exits on open positions to them and enforces them; at 09:30 it moves
-- them back to the regular values.
--
--   torque_tickers_setup.ext_stop_loss_pct / ext_close_target_pct
--       per-ticker extended-hours stop / target %; null = the global value
--       (app_config torque_ext_stop_loss_pct / torque_ext_close_target_pct,
--       pushed by `make db-push` from edgelane_market.config), else the
--       regular-hours default for the ticker.
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

alter table public.torque_tickers_setup
    add column if not exists ext_stop_loss_pct    numeric,
    add column if not exists ext_close_target_pct numeric;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'torque_tickers_setup_ext_sl_check') then
    alter table public.torque_tickers_setup
        add constraint torque_tickers_setup_ext_sl_check
        check (ext_stop_loss_pct is null or (ext_stop_loss_pct >= 1 and ext_stop_loss_pct <= 99));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'torque_tickers_setup_ext_tp_check') then
    alter table public.torque_tickers_setup
        add constraint torque_tickers_setup_ext_tp_check
        check (ext_close_target_pct is null or (ext_close_target_pct >= 1 and ext_close_target_pct <= 500));
  end if;
end
$$;

comment on column public.torque_tickers_setup.ext_stop_loss_pct is
    'Stop-loss % for entries placed in the extended session; null = global torque_ext_stop_loss_pct, else the regular default.';
comment on column public.torque_tickers_setup.ext_close_target_pct is
    'Take-profit % for entries placed in the extended session; null = global torque_ext_close_target_pct, else the regular default.';
