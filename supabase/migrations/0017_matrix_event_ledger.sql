-- ============================================================================
-- Migration 0017 — matrix_event_ledger: the engine → poster hand-off
-- ----------------------------------------------------------------------------
-- WHY. The Matrix poster (soljet-postiz, GCP) used to call BACK into EdgeLane
-- over the tunnel for everything it needed: GET /matrix/state for the numbers
-- and wording, and the snap service loaded GET /matrix/snap for the image. Both
-- were rendered from the snapshot AT FETCH TIME, not at event time — so a post
-- could show a later state than the event it described.
--
-- NOW. When the engine decides an event is worth posting it writes ONE row here
-- holding everything the post needs, frozen at that moment: the event
-- attributes, the data blocks, and the card's HTML. Pub/Sub still wakes the
-- poster (the message carries the same event_id); the poster reads the row,
-- has its Chromium render the stored HTML, posts, and deletes the row. The
-- engine only detects and records; it no longer serves the poster.
--
-- A ROLLING QUEUE, not an archive: a row lives until it is posted (then the
-- poster deletes it) or until it goes stale (the poster prunes Matrix rows from
-- before today, ET). The engine keeps writing new rows meanwhile; the poster
-- only ever touches rows it has CLAIMED.
--
-- ACCESS. RLS on, no policies: the browser (anon) and signed-in users can't
-- read or write it. The backend writes with service_role. The poster does NOT
-- get service_role — that key can decrypt users' broker tokens — it gets a
-- narrow LEDGER TOKEN and three SECURITY DEFINER functions that can only touch
-- this table: claim, done, prune. The token lives in matrix_ledger_secret (RLS,
-- no policies — readable only inside those functions); its value is
-- provisioned separately, never committed.
--
-- APPLY: Supabase dashboard → SQL Editor → paste → Run. Idempotent.
-- ============================================================================

create table if not exists public.matrix_event_ledger (
    id           uuid        primary key default gen_random_uuid(),
    event_id     text        not null unique,     -- MTX-<SYM>-<YYMMDD>-<state>[-<disc>]
    product      text        not null default 'matrix',
    symbol       text        not null,
    state        text        not null,            -- pick_selected | pick_result | …
    expiry       text        not null default '',
    event_at     timestamptz not null,            -- when the ENGINE detected it
    attributes   jsonb       not null default '{}'::jsonb,   -- the Pub/Sub attributes
    data         jsonb       not null default '{}'::jsonb,   -- state blocks, frozen
    snap_view    text,                            -- which card view the html is
    snap_html    text,                            -- standalone card, frozen at event_at
    status       text        not null default 'pending',     -- pending | claimed
    claimed_at   timestamptz,
    claimed_by   text,
    created_at   timestamptz not null default now()
);

create index if not exists matrix_event_ledger_status_idx
    on public.matrix_event_ledger (status, event_at);

comment on table public.matrix_event_ledger is
    'Rolling engine→poster queue: one row per postable Matrix event with its data and card HTML frozen at event time. Poster deletes after posting and prunes stale rows.';

alter table public.matrix_event_ledger enable row level security;
-- (no policies on purpose — see header)

-- ---- ledger token -----------------------------------------------------------
create table if not exists public.matrix_ledger_secret (
    id          int  primary key default 1 check (id = 1),   -- single row
    token_hash  text not null,                               -- sha256 hex of the token
    updated_at  timestamptz not null default now()
);
alter table public.matrix_ledger_secret enable row level security;
-- (no policies — only the definer functions below read it)

create or replace function public._matrix_ledger_token_ok(p_token text)
returns boolean language sql stable security definer set search_path = public as $$
    select exists (
        select 1 from public.matrix_ledger_secret
        where token_hash = encode(extensions.digest(coalesce(p_token, ''), 'sha256'), 'hex')
    );
$$;

-- CLAIM: atomically move one pending row to claimed and return it. A row
-- already claimed (by a concurrent worker, or a redelivery) returns nothing, so
-- two workers can never post the same row. A claim older than 15 minutes is
-- considered abandoned (worker crashed mid-post) and may be re-claimed.
create or replace function public.matrix_ledger_claim(p_token text, p_event_id text, p_worker text)
returns setof public.matrix_event_ledger
language plpgsql security definer set search_path = public as $$
begin
    if not public._matrix_ledger_token_ok(p_token) then
        raise exception 'invalid ledger token' using errcode = '28000';
    end if;
    return query
    update public.matrix_event_ledger
       set status = 'claimed', claimed_at = now(), claimed_by = coalesce(p_worker, '')
     where event_id = p_event_id
       and (status = 'pending'
            or (status = 'claimed' and claimed_at < now() - interval '15 minutes'))
    returning *;
end;
$$;

-- DONE: the poster finished with this row (posted, or decided not to) — delete it.
create or replace function public.matrix_ledger_done(p_token text, p_event_id text)
returns integer language plpgsql security definer set search_path = public as $$
declare n integer;
begin
    if not public._matrix_ledger_token_ok(p_token) then
        raise exception 'invalid ledger token' using errcode = '28000';
    end if;
    delete from public.matrix_event_ledger where event_id = p_event_id;
    get diagnostics n = row_count;
    return n;
end;
$$;

-- PRUNE: keep the ledger rolling — drop rows for events before p_before.
-- Never deletes a row currently claimed within the last 15 minutes.
create or replace function public.matrix_ledger_prune(p_token text, p_before timestamptz)
returns integer language plpgsql security definer set search_path = public as $$
declare n integer;
begin
    if not public._matrix_ledger_token_ok(p_token) then
        raise exception 'invalid ledger token' using errcode = '28000';
    end if;
    delete from public.matrix_event_ledger
     where event_at < p_before
       and not (status = 'claimed' and claimed_at >= now() - interval '15 minutes');
    get diagnostics n = row_count;
    return n;
end;
$$;

-- Only the three entry points are callable from outside; the helper is not.
revoke all on function public._matrix_ledger_token_ok(text) from public, anon, authenticated;
revoke all on function public.matrix_ledger_claim(text, text, text) from public;
revoke all on function public.matrix_ledger_done(text, text) from public;
revoke all on function public.matrix_ledger_prune(text, timestamptz) from public;
grant execute on function public.matrix_ledger_claim(text, text, text) to anon, authenticated;
grant execute on function public.matrix_ledger_done(text, text) to anon, authenticated;
grant execute on function public.matrix_ledger_prune(text, timestamptz) to anon, authenticated;
