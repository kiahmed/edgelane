-- ============================================================================
-- Migration 0006 — per-user Webull broker credentials (encrypted at rest)
-- ----------------------------------------------------------------------------
-- Adds Webull alongside Tradier in broker_configs. Webull's OpenAPI auth is an
-- app_key + app_secret pair (not a single bearer token), used via Webull's
-- signed SDK. Both are sensitive, so they follow the SAME at-rest pattern as
-- tradier_token: the browser writes plaintext, a BEFORE INSERT/UPDATE trigger
-- encrypts them (Vault key edgelane_broker_key → *_enc bytea) and NULLs the
-- plaintext columns. The backend decrypts only via the service_role RPC
-- get_broker_secret(); the browser shows webull_app_key_last4 for display.
--
-- APPLY: `make db-push` (idempotent).
-- ============================================================================

-- 1. Webull columns (plaintext inbound + ciphertext at rest + display) ---------
alter table public.broker_configs add column if not exists webull_app_key        text;
alter table public.broker_configs add column if not exists webull_app_secret     text;
alter table public.broker_configs add column if not exists webull_app_key_enc    bytea;
alter table public.broker_configs add column if not exists webull_app_secret_enc bytea;
alter table public.broker_configs add column if not exists webull_app_key_last4  text;
alter table public.broker_configs add column if not exists webull_region         text default 'us';
alter table public.broker_configs add column if not exists webull_env            text default 'production';  -- production | uat
alter table public.broker_configs add column if not exists webull_account_id     text;

-- 2. Encrypt-on-write trigger — now covers tradier_token + webull app_key/secret
create or replace function public.broker_configs_encrypt_token()
returns trigger
language plpgsql
security definer
set search_path = public, vault, extensions
as $$
declare
  v_key text;
begin
  select decrypted_secret into v_key
    from vault.decrypted_secrets where name = 'edgelane_broker_key';

  -- Tradier token
  if NEW.tradier_token is not null and length(trim(NEW.tradier_token)) > 0 then
    if v_key is null then raise exception 'edgelane_broker_key not found in Vault'; end if;
    NEW.tradier_token_enc := pgp_sym_encrypt(NEW.tradier_token, v_key);
    NEW.token_last4       := right(NEW.tradier_token, 4);
  elsif TG_OP = 'UPDATE' then
    NEW.tradier_token_enc := OLD.tradier_token_enc;
    NEW.token_last4       := OLD.token_last4;
  end if;
  NEW.tradier_token := null;

  -- Webull app_key
  if NEW.webull_app_key is not null and length(trim(NEW.webull_app_key)) > 0 then
    if v_key is null then raise exception 'edgelane_broker_key not found in Vault'; end if;
    NEW.webull_app_key_enc   := pgp_sym_encrypt(NEW.webull_app_key, v_key);
    NEW.webull_app_key_last4 := right(NEW.webull_app_key, 4);
  elsif TG_OP = 'UPDATE' then
    NEW.webull_app_key_enc   := OLD.webull_app_key_enc;
    NEW.webull_app_key_last4 := OLD.webull_app_key_last4;
  end if;
  NEW.webull_app_key := null;

  -- Webull app_secret (no last4 — never display any part of the secret)
  if NEW.webull_app_secret is not null and length(trim(NEW.webull_app_secret)) > 0 then
    if v_key is null then raise exception 'edgelane_broker_key not found in Vault'; end if;
    NEW.webull_app_secret_enc := pgp_sym_encrypt(NEW.webull_app_secret, v_key);
  elsif TG_OP = 'UPDATE' then
    NEW.webull_app_secret_enc := OLD.webull_app_secret_enc;
  end if;
  NEW.webull_app_secret := null;

  return NEW;
end;
$$;

-- trigger already exists from 0004 and points at this function; recreate to be safe
drop trigger if exists broker_configs_encrypt on public.broker_configs;
create trigger broker_configs_encrypt
  before insert or update on public.broker_configs
  for each row execute function public.broker_configs_encrypt_token();

-- 3. Decrypt RPC — return Tradier + Webull secrets (service_role only) ----------
drop function if exists public.get_broker_secret(uuid, uuid);
create function public.get_broker_secret(p_user_id uuid, p_id uuid default null)
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
       and (p_id is not null and bc.id = p_id
            or p_id is null and bc.is_active)
     order by bc.is_active desc, bc.updated_at desc
     limit 1;
end;
$$;

revoke all on function public.get_broker_secret(uuid, uuid) from public, anon, authenticated;
grant execute on function public.get_broker_secret(uuid, uuid) to service_role;

comment on table public.broker_configs is
  'Per-user broker credentials (Tradier + Webull). Secrets (tradier_token, webull_app_key, webull_app_secret) are encrypted at rest (Vault key edgelane_broker_key → *_enc); plaintext columns are always NULL after the encrypt trigger. Backend decrypts via service_role RPC get_broker_secret(); browser uses *_last4 for display.';
