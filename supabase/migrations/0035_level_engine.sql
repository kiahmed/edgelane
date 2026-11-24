-- ============================================================================
-- Migration 0035 — Level Engine storage (docs/level_engine.md §5, §5.1, §14).
--
-- Live tables in schema public, prefix lvl_: the day's levels (+ audit), the
-- per-tick feed (the replay source, chain included), the per-level state
-- machine, the agents' evidence board, beliefs, picks, graded legs and the
-- end-of-day summary. Schema lvl_replay mirrors the OUTPUT tables, each row
-- tagged with run_id, so a replay can never write live data.
--
-- Past-date rule: a trigger refuses any insert/update/delete on lvl_levels
-- whose trade_date is before today in America/New_York (the engine enforces
-- the same rule; this is the backstop).
--
-- Roles (least privilege, same pattern as poster_dedupe_rw):
--   facades_levels_engine_rw      — select/insert/update/delete on public.lvl_* only.
--   facades_levels_engine_replay  — select on public.lvl_*; all on lvl_replay.* only.
-- RLS is on everywhere with policies for these two roles only; anon /
-- authenticated get nothing (Matrix reads via the backend proxy, not PostgREST).
--
-- Passwords are NOT in git. `make db-setup` (tools/levels_provision.py, run
-- after the migrations) generates one per role when its DSN is empty in
-- facades_levels_engine.config, sets it (as a SCRAM verifier) and writes the
-- DSNs there as LVL_DB_URL / LVL_REPLAY_DB_URL. A filled DSN is left alone.
-- Supabase pooler username form: facades_levels_engine_rw.<project_ref>
-- (facades_levels_engine_replay.<project_ref>). Never use the service_role key.
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

-- ── Roles ────────────────────────────────────────────────────────────────
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'facades_levels_engine_rw') then
    create role facades_levels_engine_rw login nosuperuser nocreatedb nocreaterole nobypassrls;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'facades_levels_engine_replay') then
    create role facades_levels_engine_replay login nosuperuser nocreatedb nocreaterole nobypassrls;
  end if;
end
$$;

-- ── Live tables (public.lvl_*) ───────────────────────────────────────────
create table if not exists public.lvl_levels (
  id              uuid primary key default gen_random_uuid(),
  trade_date      date not null,
  symbol          text not null check (symbol in ('SPY', 'SPX')),
  price           numeric,                       -- nullable for SPX
  name            text not null,
  kind            text check (kind in ('support', 'resistance', 'pivot', 'trigger')),
  condition_text  text,                          -- free text, unused v1
  condition_rule  jsonb,                         -- null v1
  source          text not null default 'entered' check (source in ('entered', 'converted')),
  ratio           numeric,                       -- SPX/SPY ratio used for a converted row
  converted_from  uuid references public.lvl_levels(id) on delete cascade,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index if not exists lvl_levels_trade_date_idx on public.lvl_levels (trade_date);

-- No FK on level_id: the audit outlives the level it describes.
create table if not exists public.lvl_level_audit (
  id          bigserial primary key,
  level_id    uuid,
  trade_date  date not null,
  action      text not null check (action in ('add', 'edit', 'delete', 'convert', 'import')),
  before      jsonb,
  after       jsonb,
  at          timestamptz not null default now()
);
create index if not exists lvl_level_audit_trade_date_idx on public.lvl_level_audit (trade_date);

create table if not exists public.lvl_ticks (
  id            bigserial primary key,
  trade_date    date not null,
  ts            timestamptz not null,
  spx           numeric,
  spy           numeric,
  ratio         numeric,
  ratio_locked  boolean not null default false,
  zone          jsonb,
  chain         jsonb,
  bias          jsonb,
  unique (trade_date, ts)                        -- also the (trade_date, ts) index
);

-- level_id is a CLUSTER id string (§7.0), not a lvl_levels uuid.
create table if not exists public.lvl_level_state (
  id              bigserial primary key,
  trade_date      date not null,
  level_id        text not null,
  ts              timestamptz not null,
  state           text,
  since           timestamptz,
  crosses         int,
  last_held_side  smallint,
  detail          jsonb
);
create index if not exists lvl_level_state_trade_date_ts_idx on public.lvl_level_state (trade_date, ts);

create table if not exists public.lvl_evidence (
  id          bigserial primary key,
  trade_date  date not null,
  ts          timestamptz not null,
  agent       text,
  scenario    text check (scenario in ('up', 'down', 'range')),
  weight      numeric check (weight between -1 and 1),
  kind        text,
  detail      jsonb,
  reason      text,
  pass        smallint not null default 0
);
create index if not exists lvl_evidence_trade_date_ts_idx on public.lvl_evidence (trade_date, ts);

create table if not exists public.lvl_beliefs (
  id                     bigserial primary key,
  trade_date             date not null,
  ts                     timestamptz not null,
  p_up                   numeric,
  p_down                 numeric,
  p_range                numeric,
  call                   text check (call in ('up', 'down', 'range', 'wait', 'done')),
  target_level_id        text,
  invalidation_level_id  text,
  confidence             numeric,
  trace                  text,
  logodds                jsonb
);
create index if not exists lvl_beliefs_trade_date_ts_idx on public.lvl_beliefs (trade_date, ts);

create table if not exists public.lvl_picks (
  id          bigserial primary key,
  trade_date  date not null,
  ts          timestamptz not null,
  pick_id     text,
  pick        jsonb
);
create index if not exists lvl_picks_trade_date_ts_idx on public.lvl_picks (trade_date, ts);

create table if not exists public.lvl_legs (
  id                     text primary key,       -- app-supplied
  trade_date             date not null,
  opened_at              timestamptz,
  call                   text,
  target_level_id        text,
  invalidation_level_id  text,
  entry_spot             numeric,
  target_level           jsonb,                  -- {price, name} at open
  invalidation_level     jsonb,                  -- {price, name} at open
  pick                   jsonb,
  closed_at              timestamptz,
  -- §12 binary: new legs are win | loss; 'flipped' / 'unresolved' stay allowed
  -- only so rows written by the pre-binary grader still load (read as loss).
  result                 text check (result in ('win', 'loss', 'flipped', 'unresolved')),
  close_reason           text check (close_reason in ('target', 'invalidation', 'flipped',
                                                      'session_close', 'zone_held', 'zone_exit')),
  exit_spot              numeric,
  progress_pct           numeric,
  mae_pts                numeric,
  time_to_target_min     numeric,
  strategy_pnl           numeric
);
create index if not exists lvl_legs_trade_date_opened_idx on public.lvl_legs (trade_date, opened_at);
create index if not exists lvl_legs_closed_idx on public.lvl_legs (closed_at desc) where result is not null;
-- columns added after the first draft (idempotent for a database that has it)
alter table public.lvl_legs add column if not exists target_level jsonb;
alter table public.lvl_legs add column if not exists invalidation_level jsonb;
alter table public.lvl_legs add column if not exists close_reason text
  check (close_reason in ('target', 'invalidation', 'flipped', 'session_close', 'zone_held',
                          'zone_exit'));
alter table public.lvl_legs add column if not exists exit_spot numeric;

create table if not exists public.lvl_day_summary (
  trade_date      date primary key,
  close           numeric,
  final_zone      jsonb,
  levels_outcome  jsonb,
  legs            jsonb,
  notes           text,
  created_at      timestamptz not null default now()
);

-- ── lvl_levels triggers: past-date guard + updated_at ────────────────────
create or replace function public.lvl_levels_guard_past_date()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
declare
  today_et date := (now() at time zone 'America/New_York')::date;
begin
  if tg_op in ('UPDATE', 'DELETE') and old.trade_date < today_et then
    raise exception 'lvl_levels: levels for % are read-only (today ET is %)', old.trade_date, today_et
      using errcode = 'check_violation';
  end if;
  if tg_op in ('INSERT', 'UPDATE') and new.trade_date < today_et then
    raise exception 'lvl_levels: levels for % are read-only (today ET is %)', new.trade_date, today_et
      using errcode = 'check_violation';
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;

drop trigger if exists lvl_levels_guard_past_date on public.lvl_levels;
create trigger lvl_levels_guard_past_date
  before insert or update or delete on public.lvl_levels
  for each row execute function public.lvl_levels_guard_past_date();

create or replace function public.lvl_levels_touch_updated_at()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
begin
  new.updated_at := now();
  return new;
end
$$;

drop trigger if exists lvl_levels_touch_updated_at on public.lvl_levels;
create trigger lvl_levels_touch_updated_at
  before update on public.lvl_levels
  for each row execute function public.lvl_levels_touch_updated_at();

-- ── Replay schema (§14): output tables tagged with run_id ────────────────
create schema if not exists lvl_replay;

create table if not exists lvl_replay.runs (
  run_id      uuid primary key default gen_random_uuid(),
  trade_date  date,
  label       text,
  config      jsonb,
  created_at  timestamptz not null default now()
);

-- run_id is deliberately not an FK to runs: the store tags rows with any run_id
-- it is given; registering a run in lvl_replay.runs is the harness's choice.
create table if not exists lvl_replay.level_state (
  id              bigserial primary key,
  run_id          uuid not null,
  trade_date      date not null,
  level_id        text not null,
  ts              timestamptz not null,
  state           text,
  since           timestamptz,
  crosses         int,
  last_held_side  smallint,
  detail          jsonb
);
create index if not exists replay_level_state_run_idx on lvl_replay.level_state (run_id, trade_date, ts);

create table if not exists lvl_replay.evidence (
  id          bigserial primary key,
  run_id      uuid not null,
  trade_date  date not null,
  ts          timestamptz not null,
  agent       text,
  scenario    text check (scenario in ('up', 'down', 'range')),
  weight      numeric check (weight between -1 and 1),
  kind        text,
  detail      jsonb,
  reason      text,
  pass        smallint not null default 0
);
create index if not exists replay_evidence_run_idx on lvl_replay.evidence (run_id, trade_date, ts);

create table if not exists lvl_replay.beliefs (
  id                     bigserial primary key,
  run_id                 uuid not null,
  trade_date             date not null,
  ts                     timestamptz not null,
  p_up                   numeric,
  p_down                 numeric,
  p_range                numeric,
  call                   text check (call in ('up', 'down', 'range', 'wait', 'done')),
  target_level_id        text,
  invalidation_level_id  text,
  confidence             numeric,
  trace                  text,
  logodds                jsonb
);
create index if not exists replay_beliefs_run_idx on lvl_replay.beliefs (run_id, trade_date, ts);

create table if not exists lvl_replay.picks (
  id          bigserial primary key,
  run_id      uuid not null,
  trade_date  date not null,
  ts          timestamptz not null,
  pick_id     text,
  pick        jsonb
);
create index if not exists replay_picks_run_idx on lvl_replay.picks (run_id, trade_date, ts);

create table if not exists lvl_replay.legs (
  run_id                 uuid not null,
  id                     text not null,
  trade_date             date not null,
  opened_at              timestamptz,
  call                   text,
  target_level_id        text,
  invalidation_level_id  text,
  entry_spot             numeric,
  target_level           jsonb,                  -- {price, name} at open
  invalidation_level     jsonb,                  -- {price, name} at open
  pick                   jsonb,
  closed_at              timestamptz,
  -- §12 binary: new legs are win | loss; 'flipped' / 'unresolved' stay allowed
  -- only so rows written by the pre-binary grader still load (read as loss).
  result                 text check (result in ('win', 'loss', 'flipped', 'unresolved')),
  close_reason           text check (close_reason in ('target', 'invalidation', 'flipped',
                                                      'session_close', 'zone_held', 'zone_exit')),
  exit_spot              numeric,
  progress_pct           numeric,
  mae_pts                numeric,
  time_to_target_min     numeric,
  strategy_pnl           numeric,
  primary key (run_id, id)
);
create index if not exists replay_legs_run_idx on lvl_replay.legs (run_id, trade_date, opened_at);
-- columns added after the first draft (idempotent for a database that has it)
alter table lvl_replay.legs add column if not exists target_level jsonb;
alter table lvl_replay.legs add column if not exists invalidation_level jsonb;
alter table lvl_replay.legs add column if not exists close_reason text
  check (close_reason in ('target', 'invalidation', 'flipped', 'session_close', 'zone_held',
                          'zone_exit'));
alter table lvl_replay.legs add column if not exists exit_spot numeric;

create table if not exists lvl_replay.day_summary (
  run_id          uuid not null,
  trade_date      date not null,
  close           numeric,
  final_zone      jsonb,
  levels_outcome  jsonb,
  legs            jsonb,
  notes           text,
  created_at      timestamptz not null default now(),
  primary key (run_id, trade_date)
);

-- ── Grants ───────────────────────────────────────────────────────────────
-- Strip everything first: Supabase's default privileges auto-grant new public
-- tables/sequences to anon/authenticated. Nobody but the two roles below.
revoke all on public.lvl_levels, public.lvl_level_audit, public.lvl_ticks,
              public.lvl_level_state, public.lvl_evidence, public.lvl_beliefs,
              public.lvl_picks, public.lvl_legs, public.lvl_day_summary
  from public, anon, authenticated;
revoke all on sequence public.lvl_level_audit_id_seq, public.lvl_ticks_id_seq,
              public.lvl_level_state_id_seq, public.lvl_evidence_id_seq,
              public.lvl_beliefs_id_seq, public.lvl_picks_id_seq
  from public, anon, authenticated;
revoke all on function public.lvl_levels_guard_past_date(),
              public.lvl_levels_touch_updated_at()
  from public, anon, authenticated;                -- trigger-only; firing needs no EXECUTE
revoke all on schema lvl_replay from public, anon, authenticated;
revoke all on all tables in schema lvl_replay from public, anon, authenticated;
revoke all on all sequences in schema lvl_replay from public, anon, authenticated;

-- facades_levels_engine_rw: CRUD on public.lvl_* only. Nothing else.
grant usage on schema public to facades_levels_engine_rw;
grant select, insert, update, delete on
      public.lvl_levels, public.lvl_level_audit, public.lvl_ticks,
      public.lvl_level_state, public.lvl_evidence, public.lvl_beliefs,
      public.lvl_picks, public.lvl_legs, public.lvl_day_summary
  to facades_levels_engine_rw;
grant usage on sequence public.lvl_level_audit_id_seq, public.lvl_ticks_id_seq,
      public.lvl_level_state_id_seq, public.lvl_evidence_id_seq,
      public.lvl_beliefs_id_seq, public.lvl_picks_id_seq
  to facades_levels_engine_rw;

-- facades_levels_engine_replay: read live, write lvl_replay only (cannot write live).
grant usage on schema public to facades_levels_engine_replay;
grant select on
      public.lvl_levels, public.lvl_level_audit, public.lvl_ticks,
      public.lvl_level_state, public.lvl_evidence, public.lvl_beliefs,
      public.lvl_picks, public.lvl_legs, public.lvl_day_summary
  to facades_levels_engine_replay;
grant usage on schema lvl_replay to facades_levels_engine_replay;
grant all privileges on all tables in schema lvl_replay to facades_levels_engine_replay;
grant all privileges on all sequences in schema lvl_replay to facades_levels_engine_replay;

-- ── RLS + policies ───────────────────────────────────────────────────────
-- A non-owner without BYPASSRLS sees no rows unless a policy admits it.
do $$
declare
  t text;
begin
  foreach t in array array['lvl_levels', 'lvl_level_audit', 'lvl_ticks', 'lvl_level_state',
                           'lvl_evidence', 'lvl_beliefs', 'lvl_picks', 'lvl_legs',
                           'lvl_day_summary']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists level_engine_rw_all on public.%I', t);
    execute format('create policy level_engine_rw_all on public.%I '
                   'for all to facades_levels_engine_rw using (true) with check (true)', t);
    execute format('drop policy if exists level_engine_replay_read on public.%I', t);
    execute format('create policy level_engine_replay_read on public.%I '
                   'for select to facades_levels_engine_replay using (true)', t);
  end loop;

  foreach t in array array['runs', 'level_state', 'evidence', 'beliefs', 'picks', 'legs',
                           'day_summary']
  loop
    execute format('alter table lvl_replay.%I enable row level security', t);
    execute format('drop policy if exists level_engine_replay_all on lvl_replay.%I', t);
    execute format('create policy level_engine_replay_all on lvl_replay.%I '
                   'for all to facades_levels_engine_replay using (true) with check (true)', t);
  end loop;
end
$$;
