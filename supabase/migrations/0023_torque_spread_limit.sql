-- ============================================================================
-- Migration 0023 — torque_tickers_setup.max_spread_pct: per-ticker wide-book limit
-- ----------------------------------------------------------------------------
-- Torque refuses to trade (news signals), arm a profit target or flag a build
-- as tradeable when the package's own bid/ask is wider than this % of its mid.
-- Null = the global default (MAX_AUTO_CLOSE_SPREAD_PCT, 100). Entries are
-- always priced at mid either way; this only decides whether to trade at all.
--
-- XLE and XLF are set to 150: their 0DTE books routinely run 50-120% of mid,
-- which the global 100% would refuse outright, while a near-empty book (bid
-- close to 0, i.e. approaching 200%) is still refused.
--
-- One-time: the column is added and the values set ONLY when the column
-- doesn't exist yet, so re-running (every `make db-setup`) never overwrites a
-- value someone has since changed in the table. The JSON between the $seed$
-- markers is also merged into the backend test suite's ticker setup.
--
-- APPLY: make db-setup   (idempotent)
-- ============================================================================

do $$
begin
    if not exists (select 1 from information_schema.columns
                   where table_schema = 'public' and table_name = 'torque_tickers_setup'
                     and column_name = 'max_spread_pct') then
        alter table public.torque_tickers_setup
            add column max_spread_pct numeric
            check (max_spread_pct is null or max_spread_pct > 0);

        update public.torque_tickers_setup t
           set max_spread_pct = s.max_spread_pct
          from jsonb_to_recordset($seed$[
            {"ticker": "XLE", "max_spread_pct": 150},
            {"ticker": "XLF", "max_spread_pct": 150}
          ]$seed$::jsonb) as s(ticker text, max_spread_pct numeric)
         where t.ticker = s.ticker;
    end if;
end $$;

comment on column public.torque_tickers_setup.max_spread_pct is
    'Widest package bid/ask (as % of mid) Torque will trade, arm a profit target on, or call tradeable. Null = global default (100). XLE/XLF: 150.';
