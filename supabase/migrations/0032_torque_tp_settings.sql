-- ============================================================================
-- Migration 0032 — saved take-profit (auto-close) per account per ticker.
--
-- torque_sl_settings becomes the one saved exit-settings row per broker
-- account per ticker, written by the Torque page:
--   enabled / pct         stop-loss on/off + %          (unchanged)
--   tp_enabled / tp_pct   take-profit (auto-close) on/off + %; null = never
--                         saved (the page's default: on, at the ticker's
--                         close_target_default)
--   sl_saved              whether the stop-loss half was ever saved. A row
--                         created by a TP-only save has sl_saved = false, so
--                         it never counts as "stop-loss switched on" — the
--                         stop guardian only guards positions opened outside
--                         Torque once stop-loss has been SAVED for the ticker.
--
-- sl_saved is added with default true (every existing row came from a
-- stop-loss save), then its default flips to false: a TP save omits the
-- column, so a new row gets false while an existing row keeps its value; a
-- stop-loss save always sends true.
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

do $$
begin
  if not exists (select 1 from information_schema.columns
                  where table_schema = 'public' and table_name = 'torque_sl_settings'
                    and column_name = 'sl_saved') then
    alter table public.torque_sl_settings
        add column sl_saved boolean not null default true;
    alter table public.torque_sl_settings
        alter column sl_saved set default false;
  end if;
end
$$;

alter table public.torque_sl_settings
    add column if not exists tp_enabled boolean,
    add column if not exists tp_pct     numeric;

-- A row with no take-profit data can only be a stop-loss save. Rows written by
-- a backend that predates sl_saved (between this migration and the deploy)
-- would otherwise default to false; fix them on every push. Never touches a
-- TP-only row (tp_enabled is set there).
update public.torque_sl_settings
   set sl_saved = true
 where sl_saved = false and tp_enabled is null;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'torque_sl_settings_tp_pct_check') then
    alter table public.torque_sl_settings
        add constraint torque_sl_settings_tp_pct_check
        check (tp_pct is null or (tp_pct >= 1 and tp_pct <= 500));
  end if;
end
$$;

comment on column public.torque_sl_settings.tp_enabled is
    'Saved take-profit (auto-close) on/off for this account + ticker; null = never saved (page default: on).';
comment on column public.torque_sl_settings.tp_pct is
    'Saved take-profit %; null = the ticker''s close_target_default.';
comment on column public.torque_sl_settings.sl_saved is
    'False when the row only holds a TP save — the stop-loss half then reads as never saved.';
