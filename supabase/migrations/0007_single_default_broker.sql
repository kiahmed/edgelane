-- ============================================================================
-- Migration 0007 — exactly ONE default broker per user (across all brokers)
-- ----------------------------------------------------------------------------
-- 0002 enforced one active connection per (user, broker) — which let a user
-- have an active Tradier AND an active Webull at once, leaving the order path
-- ambiguous about which to trade against. Tighten it to ONE active row per
-- USER, regardless of broker, so the "default" is unambiguous and the API
-- always trades against exactly one account.
--
-- Also makes get_broker_secret robust: with no explicit id it returns the
-- active default, falling back to the user's most-recent connection if (somehow)
-- none is flagged active — so a lone connection always works even if the user
-- never pressed "Set default".
--
-- APPLY: `make db-push` (idempotent).
-- ============================================================================

-- 1. Defensive dedup: if any user already has >1 active row, keep only the most
--    recent and deactivate the rest (no-op on a clean DB).
update public.broker_configs b set is_active = false
where b.is_active
  and b.id <> (
    select b2.id from public.broker_configs b2
    where b2.user_id = b.user_id and b2.is_active
    order by b2.updated_at desc, b2.id desc
    limit 1
  );

-- 2. Replace the per-(user,broker) active index with a per-USER one.
drop index if exists public.broker_configs_active_uq;
create unique index if not exists broker_configs_active_user_uq
    on public.broker_configs (user_id)
    where is_active;

-- 3. Robust default selection (active wins; else most-recent connection).
create or replace function public.get_broker_secret(p_user_id uuid, p_id uuid default null)
returns table (
  id                 uuid,
  broker             text,
  tradier_token      text,
  tradier_account_id text,
  tradier_env        text,
  webull_app_key     text,
  webull_app_secret  text,
  webull_region      text,
  webull_env         text,
  webull_account_id  text
)
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  v_key text;
begin
  select decrypted_secret into v_key
    from vault.decrypted_secrets where name = 'edgelane_broker_key';
  return query
    select bc.id, bc.broker,
           pgp_sym_decrypt(bc.tradier_token_enc, v_key)     as tradier_token,
           bc.tradier_account_id, bc.tradier_env,
           pgp_sym_decrypt(bc.webull_app_key_enc, v_key)    as webull_app_key,
           pgp_sym_decrypt(bc.webull_app_secret_enc, v_key) as webull_app_secret,
           bc.webull_region, bc.webull_env, bc.webull_account_id
      from public.broker_configs bc
     where bc.user_id = p_user_id
       and (bc.tradier_token_enc is not null or bc.webull_app_key_enc is not null)
       and (p_id is null or bc.id = p_id)          -- specific row, else any
     order by bc.is_active desc, bc.updated_at desc -- active default wins, else newest
     limit 1;
end;
$$;

revoke all on function public.get_broker_secret(uuid, uuid) from public, anon, authenticated;
grant execute on function public.get_broker_secret(uuid, uuid) to service_role;
