-- ============================================================================
-- Migration 0020 — simmer_ledger_peek: read a ledger row WITHOUT claiming it
-- ----------------------------------------------------------------------------
-- The Simmer poster's dry-run (compose + snap, never post) must not consume a
-- row: a claim would block the real run from posting it. Same ledger-token gate
-- as the other three functions (0019). Read-only.
--
-- APPLY: Supabase dashboard → SQL Editor → paste → Run. Idempotent.
-- ============================================================================

create or replace function public.simmer_ledger_peek(p_token text, p_event_id text)
returns setof public.simmer_event_ledger
language plpgsql stable security definer set search_path = public as $$
begin
    if not public._simmer_ledger_token_ok(p_token) then
        raise exception 'invalid ledger token' using errcode = '28000';
    end if;
    return query select * from public.simmer_event_ledger where event_id = p_event_id;
end;
$$;

revoke all on function public.simmer_ledger_peek(text, text) from public;
grant execute on function public.simmer_ledger_peek(text, text) to anon, authenticated;
