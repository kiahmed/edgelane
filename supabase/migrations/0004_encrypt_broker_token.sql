-- ============================================================================
-- Migration 0004 — encrypt broker tokens at rest (Supabase Vault + pgcrypto)
-- ----------------------------------------------------------------------------
-- broker_configs.tradier_token previously stored a LIVE broker credential in
-- plaintext (RLS-protected, but readable from any DB dump/backup). This makes
-- the token write-only from the browser and ciphertext on disk:
--
--   • A symmetric key lives in Supabase Vault (encrypted by the project root
--     key, NOT stored beside the data) — secret name `edgelane_broker_key`.
--   • A BEFORE INSERT/UPDATE trigger encrypts any incoming plaintext
--     `tradier_token` into `tradier_token_enc` (bytea), records the last 4
--     chars in the non-sensitive `token_last4`, and NULLs the plaintext column
--     so nothing sensitive ever persists. Browser write path is UNCHANGED
--     (it still sends `tradier_token`; the trigger swallows it).
--   • The backend reads the decrypted token ONLY via the service_role-only
--     SECURITY DEFINER RPC `public.get_broker_secret(user_id[, id])`.
--   • The browser can no longer read the token back (by design) — it uses
--     `token_last4` for display and a server-side test endpoint for validation.
--
-- APPLY: `make db-push` (idempotent). Safe to re-run.
-- ============================================================================

create extension if not exists pgcrypto;

-- 1. Vault key (create once; random 32-byte key, base64-encoded as text) -------
do $$
begin
  if not exists (select 1 from vault.secrets where name = 'edgelane_broker_key') then
    perform vault.create_secret(
      encode(gen_random_bytes(32), 'base64'),
      'edgelane_broker_key',
      'Symmetric key for encrypting broker_configs.tradier_token at rest.'
    );
  end if;
end $$;

-- 2. Storage columns -----------------------------------------------------------
alter table public.broker_configs add column if not exists tradier_token_enc bytea;
alter table public.broker_configs add column if not exists token_last4        text;

-- 3. Encrypt-on-write trigger (SECURITY DEFINER so it can read the Vault key) --
create or replace function public.broker_configs_encrypt_token()
returns trigger
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  v_key text;
begin
  if NEW.tradier_token is not null and length(trim(NEW.tradier_token)) > 0 then
    select decrypted_secret into v_key
      from vault.decrypted_secrets where name = 'edgelane_broker_key';
    if v_key is null then
      raise exception 'edgelane_broker_key not found in Vault';
    end if;
    NEW.tradier_token_enc := pgp_sym_encrypt(NEW.tradier_token, v_key);
    NEW.token_last4       := right(NEW.tradier_token, 4);
  elsif TG_OP = 'UPDATE' then
    -- no new token supplied on an edit → keep the existing ciphertext/last4
    NEW.tradier_token_enc := OLD.tradier_token_enc;
    NEW.token_last4       := OLD.token_last4;
  end if;
  -- never persist plaintext
  NEW.tradier_token := null;
  return NEW;
end;
$$;

drop trigger if exists broker_configs_encrypt on public.broker_configs;
create trigger broker_configs_encrypt
  before insert or update on public.broker_configs
  for each row execute function public.broker_configs_encrypt_token();

-- 4. Backfill any pre-existing plaintext rows (idempotent; 0 rows today) --------
do $$
declare
  v_key text;
begin
  select decrypted_secret into v_key
    from vault.decrypted_secrets where name = 'edgelane_broker_key';
  update public.broker_configs
     set tradier_token_enc = pgp_sym_encrypt(tradier_token, v_key),
         token_last4       = right(tradier_token, 4),
         tradier_token     = null
   where tradier_token is not null and length(trim(tradier_token)) > 0;
end $$;

-- 5. Service-role-only decrypt RPC --------------------------------------------
-- p_id null → the user's active connection (order path); else a specific row
-- (the connection "Test" button). Returns at most one row.
-- DROP first (not just `create or replace`): later migrations (0006) widen this
-- function's return type, and `create or replace` can't change a return type —
-- so on a full idempotent re-push this must drop the existing definition first.
drop function if exists public.get_broker_secret(uuid, uuid);
create function public.get_broker_secret(p_user_id uuid, p_id uuid default null)
returns table (
  id                 uuid,
  broker             text,
  tradier_token      text,
  tradier_account_id text,
  tradier_env        text
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
           pgp_sym_decrypt(bc.tradier_token_enc, v_key) as tradier_token,
           bc.tradier_account_id, bc.tradier_env
      from public.broker_configs bc
     where bc.user_id = p_user_id
       and bc.tradier_token_enc is not null
       and (p_id is not null and bc.id = p_id
            or p_id is null and bc.is_active)
     order by bc.is_active desc, bc.updated_at desc
     limit 1;
end;
$$;

-- Lock it down: only the backend (service_role) may decrypt. Never anon/authenticated.
revoke all on function public.get_broker_secret(uuid, uuid) from public, anon, authenticated;
grant execute on function public.get_broker_secret(uuid, uuid) to service_role;

comment on table public.broker_configs is
  'Per-user broker credentials. tradier_token is encrypted at rest (Vault key edgelane_broker_key → tradier_token_enc); plaintext column is always NULL after the encrypt trigger. Backend decrypts via service_role RPC get_broker_secret(); browser uses token_last4 for display.';
