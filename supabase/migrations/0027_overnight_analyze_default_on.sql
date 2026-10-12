-- ============================================================================
-- Migration 0027 — overnight analysis (toggle 2) defaults ON
-- (docs/simmer.md → "Overnight movers"). Toggle 2 — "run overnight analysis on
-- my overnight-mover watchlist names" (the intersection of a user's watchlist
-- and today's RelativeQs board) — shipped as a no-op defaulting OFF in 0026.
-- It is now wired and defaults ON: enabling it mid-day misses the frozen 09:25
-- baseline, so OFF-by-default would make it unusable. It writes only the
-- user-agnostic simmer_overnight rows + a non-destructive watchlist overlay, and
-- NEVER touches the 10-gate readiness scoring, so default-ON is safe.
--
-- APPLY: make db-setup   (idempotent)
-- ============================================================================

-- Future rows default ON.
alter table public.simmer_settings
    alter column overnight_analyze_all set default true;

-- One-time backfill: flip the 0026 default-false rows to true. Toggle 2 was a
-- no-op until now, so every stored false is a default, not a deliberate opt-out
-- — safe to flip. Guarded by an app_config sentinel so a migration re-run never
-- clobbers a user who later turns it off.
do $$
begin
    if not exists (select 1 from public.app_config
                   where key = 'overnight_analyze_all_backfilled_0027') then
        update public.simmer_settings set overnight_analyze_all = true
            where overnight_analyze_all = false;
        insert into public.app_config (key, value)
            values ('overnight_analyze_all_backfilled_0027', 'true')
            on conflict (key) do nothing;
    end if;
end $$;
