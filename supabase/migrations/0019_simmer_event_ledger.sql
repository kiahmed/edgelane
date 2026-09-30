-- ============================================================================
-- Migration 0019 — simmer_event_ledger: the engine → poster hand-off (Simmer)
-- ----------------------------------------------------------------------------
-- The Simmer version of the Matrix ledger (0017). WHY it exists is identical:
-- the poster (soljet-postiz, GCP) used to call BACK into EdgeLane over the
-- tunnel — GET /simmer/state for numbers/wording and the snap service loading
-- GET /simmer/snap for the image — both rendered from the readiness cache AT
-- FETCH TIME, so a post could describe a later state than the event that fired
-- it. Now the engine freezes everything the post needs (attributes, the state
-- blocks, the card HTML) into ONE row here at event time, and Pub/Sub only wakes
-- the poster after the row exists. The engine detects and records; it no longer
-- serves the poster.
--
-- WHAT'S DIFFERENT FROM MATRIX (docs/simmer_events_update.md §3): Simmer keeps an
-- EXACTLY-ONCE publish guarantee — a transition re-seen across sweeps, or watched
-- by many users, publishes ONCE per (symbol, expiry, state, UTC day). The
-- event_id (SMR-<SYM>-<YYMMDD>-<expiryYYMMDD>-<state>) is that key, and it lives
-- in this table's UNIQUE column. Two consequences here:
--   * the engine publishes only when its INSERT created a NEW row (PostgREST
--     resolution=ignore-duplicates + return=representation reports which);
--   * `done` does NOT delete the row (Matrix does). If it did, a same-UTC-day
--     re-seen transition would insert a fresh row and publish again. Instead
--     `done` CLEARS the payload (data, snap_html, attributes → empty) and sets
--     status='posted', keeping the event_id as a tombstone dedupe key. `prune`
--     then drops rows older than 1 day, which outlives the UTC-day id scope.
--
-- ACCESS. RLS on, no policies: the browser (anon) and signed-in users can't read
-- or write it. The backend writes with service_role. The poster does NOT get
-- service_role — that key can decrypt users' broker tokens — it gets a narrow
-- LEDGER TOKEN and four SECURITY DEFINER functions that can only touch this
-- table: peek (0020), claim, done, prune. The token lives in
-- simmer_ledger_secret (RLS, no policies); its value is provisioned separately
-- (Secret Manager `simmer-ledger-token`), never committed.
--
-- APPLY: Supabase dashboard → SQL Editor → paste → Run. Idempotent.
-- ============================================================================

create table if not exists public.simmer_event_ledger (
    id           uuid        primary key default gen_random_uuid(),
    event_id     text        not null unique,     -- SMR-<SYM>-<YYMMDD>-<expiryYYMMDD>-<state>
    product      text        not null default 'simmer',
    symbol       text        not null,
    state        text        not null,            -- watch_entered | ready
    expiry       text        not null default '',
    event_at     timestamptz not null,            -- when the ENGINE detected it
    attributes   jsonb       not null default '{}'::jsonb,   -- Pub/Sub attributes + takeaways
    data         jsonb       not null default '{}'::jsonb,   -- card/score/gates/sentiment/evolution, frozen
    snap_html    text,                            -- standalone card, frozen at event_at
    status       text        not null default 'pending',     -- pending | claimed | posted
    claimed_at   timestamptz,
    claimed_by   text,
    posted_at    timestamptz,
    created_at   timestamptz not null default now()
);

create index if not exists simmer_event_ledger_status_idx
    on public.simmer_event_ledger (status, event_at);

comment on table public.simmer_event_ledger is
    'Rolling engine→poster queue for Simmer: one row per postable event with data and card HTML frozen at event time. Exactly-once: publish only on a NEW row; done keeps the event_id row as a tombstone (payload cleared, status=posted); prune drops rows older than 1 day.';

alter table public.simmer_event_ledger enable row level security;
-- (no policies on purpose — see header)

-- ---- ledger token -----------------------------------------------------------
create table if not exists public.simmer_ledger_secret (
    id          int  primary key default 1 check (id = 1),   -- single row
    token_hash  text not null,                               -- sha256 hex of the token
    updated_at  timestamptz not null default now()
);
alter table public.simmer_ledger_secret enable row level security;
-- (no policies — only the definer functions below read it)

create or replace function public._simmer_ledger_token_ok(p_token text)
returns boolean language sql stable security definer set search_path = public as $$
    select exists (
        select 1 from public.simmer_ledger_secret
        where token_hash = encode(extensions.digest(coalesce(p_token, ''), 'sha256'), 'hex')
    );
$$;

-- CLAIM: atomically move one pending row to claimed and return it. A row already
-- claimed (concurrent worker, or a redelivery) returns nothing, so two workers
-- can never post the same row. A claim older than 15 minutes is abandoned
-- (worker crashed mid-post) and may be re-claimed. A posted tombstone is NOT
-- claimable (status is neither pending nor a stale claim).
create or replace function public.simmer_ledger_claim(p_token text, p_event_id text, p_worker text)
returns setof public.simmer_event_ledger
language plpgsql security definer set search_path = public as $$
begin
    if not public._simmer_ledger_token_ok(p_token) then
        raise exception 'invalid ledger token' using errcode = '28000';
    end if;
    return query
    update public.simmer_event_ledger
       set status = 'claimed', claimed_at = now(), claimed_by = coalesce(p_worker, '')
     where event_id = p_event_id
       and (status = 'pending'
            or (status = 'claimed' and claimed_at < now() - interval '15 minutes'))
    returning *;
end;
$$;

-- DONE: the poster finished (posted, or decided not to). Unlike Matrix, DO NOT
-- delete — clear the payload and keep the event_id row as the exactly-once
-- tombstone (§3), so a same-UTC-day re-seen transition can't insert a fresh row
-- and publish again.
create or replace function public.simmer_ledger_done(p_token text, p_event_id text)
returns integer language plpgsql security definer set search_path = public as $$
declare n integer;
begin
    if not public._simmer_ledger_token_ok(p_token) then
        raise exception 'invalid ledger token' using errcode = '28000';
    end if;
    update public.simmer_event_ledger
       set status = 'posted', posted_at = now(),
           data = '{}'::jsonb, attributes = '{}'::jsonb, snap_html = null
     where event_id = p_event_id;
    get diagnostics n = row_count;
    return n;
end;
$$;

-- PRUNE: keep the ledger rolling — drop rows (pending, posted tombstones, and
-- abandoned claims) for events before p_before. Never deletes a row currently
-- claimed within the last 15 minutes. The poster passes now()-1 day, which
-- outlives the UTC-day scope of the exactly-once id.
create or replace function public.simmer_ledger_prune(p_token text, p_before timestamptz)
returns integer language plpgsql security definer set search_path = public as $$
declare n integer;
begin
    if not public._simmer_ledger_token_ok(p_token) then
        raise exception 'invalid ledger token' using errcode = '28000';
    end if;
    delete from public.simmer_event_ledger
     where event_at < p_before
       and not (status = 'claimed' and claimed_at >= now() - interval '15 minutes');
    get diagnostics n = row_count;
    return n;
end;
$$;

-- Only the entry points are callable from outside; the helper is not.
revoke all on function public._simmer_ledger_token_ok(text) from public, anon, authenticated;
revoke all on function public.simmer_ledger_claim(text, text, text) from public;
revoke all on function public.simmer_ledger_done(text, text) from public;
revoke all on function public.simmer_ledger_prune(text, timestamptz) from public;
grant execute on function public.simmer_ledger_claim(text, text, text) to anon, authenticated;
grant execute on function public.simmer_ledger_done(text, text) to anon, authenticated;
grant execute on function public.simmer_ledger_prune(text, timestamptz) to anon, authenticated;
