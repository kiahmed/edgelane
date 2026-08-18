-- 0011_simmer_sweep_interval.sql
-- UI/CLI-controlled watcher sweep cadence, in MINUTES.
--
-- Replaces editing simmer_config.CADENCE.sweep_seconds by hand: the Simmer
-- watcher reads the MIN sweep_interval across all simmer_settings rows each
-- cycle (there is one global sweep loop, so the tightest cadence any user asked
-- for wins) and uses it as the open-market interval, overriding the config
-- default. Set it with tools/supabase_admin.py or the /simmer/settings API.
-- Idempotent (safe to re-run under db_push).

alter table public.simmer_settings
    add column if not exists sweep_interval int not null default 5;   -- minutes

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'simmer_settings_sweep_interval'
    ) then
        alter table public.simmer_settings
            add constraint simmer_settings_sweep_interval
            check (sweep_interval between 1 and 60);
    end if;
end $$;
