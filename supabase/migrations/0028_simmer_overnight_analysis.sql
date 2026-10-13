-- ============================================================================
-- Migration 0028 — simmer_overnight_analysis: the overnight catalyst analyzer's
-- dedicated, DROPPABLE table (docs/simmer_overnight_llm_spec.md). Kept SEPARATE
-- from public.simmer_overnight (the lean core the Overnight tab reads) so the
-- experimental LLM + bake-off data never touches / bloats the core row.
--
-- Phase 0 is READ-ONLY: predictions + readings persist here, the user-facing
-- premium verdict in simmer_overnight is untouched.
--
-- APPLY: make db-setup   (idempotent)
-- ============================================================================

-- One row per (symbol, trading day). Rows are never deleted → this IS the
-- per-ticker history that a later lookback plots ("last time it gapped like this").
create table if not exists public.simmer_overnight_analysis (
    symbol        text        not null check (symbol = upper(symbol)),
    session_date  date        not null,              -- ET trading day
    dir           text,                              -- up | down (from the RQ board)
    gap           numeric,                           -- signed overnight fraction
    crowd         text,                              -- RelativeQs peer tag
    premarket     jsonb       not null default '{}'::jsonb,   -- {spot, gap, premiums{strike:mid}} at 09:25
    readings      jsonb       not null default '[]'::jsonb,   -- [{t:"09:25"|"10:00"|"16:00", spot, premiums}]
    close         jsonb       not null default '{}'::jsonb,   -- {spot, intraday_pct, vs_prior_close_pct} at 16:00
    outcome       text,                              -- realized: held | faded | extended (stamped at close)
    direction_right boolean,                         -- did the realized move match the analyzer's side
    premium       jsonb       not null default '{}'::jsonb,   -- premium-only verdict snapshot {call,premium_trend,underlying_trend,confidence}
    llm           jsonb       not null default '{}'::jsonb,   -- live provider verdict (+ note, queries, tokens, latency)
    llm_shadow    jsonb       not null default '{}'::jsonb,   -- shadow provider verdict (logged, not acted on)
    combiner      jsonb       not null default '{}'::jsonb,   -- {call,confidence,source,reasons,mode} (read-only in Phase 0)
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    primary key (symbol, session_date)
);

comment on table public.simmer_overnight_analysis is
    'Overnight catalyst analyzer (separate from simmer_overnight): per (symbol, trading day) premarket/10:00/close readings, the premium-only + LLM + shadow verdicts, the deterministic combiner output, and the realized outcome. Read-only logging in Phase 0; the per-ticker history source for later lookback/plots.';

create index if not exists simmer_overnight_analysis_session_idx
    on public.simmer_overnight_analysis (session_date);
create index if not exists simmer_overnight_analysis_symbol_idx
    on public.simmer_overnight_analysis (symbol);

drop trigger if exists simmer_overnight_analysis_set_updated_at on public.simmer_overnight_analysis;
create trigger simmer_overnight_analysis_set_updated_at
    before update on public.simmer_overnight_analysis
    for each row execute function public.set_updated_at();

alter table public.simmer_overnight_analysis enable row level security;
-- (no policies — service_role only; the UI reads it through a backend route)

-- Seed the overnight_llm engine-settings section so the live config is visible /
-- tunable from day one (deep-merged over the code defaults in simmer_config.py).
insert into public.simmer_engine_settings (section, settings) values
    ('overnight_llm', '{"enabled": false, "provider": "openai", "model": "gpt-6-luna", "thinking": "medium", "context_size": "medium", "premium_context_to_llm": true, "decision_mode": "llm_only", "shadow_provider": "gemini", "shadow_model": "gemini-3.8-flash"}'::jsonb)
on conflict (section) do nothing;
