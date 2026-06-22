-- ============================================================================
-- Migration 0002 — per-user broker credentials
-- ----------------------------------------------------------------------------
-- public.broker_configs: each user connects their OWN broker (Tradier today;
-- column set generalizes to others later). The backend reads the active row
-- via the service_role key to place orders on that user's account. Market data
-- (SPX GEX / bias) stays on a shared system token — only the order/account path
-- is per-user.
--
-- APPLY: Supabase dashboard → SQL Editor → paste → Run. Idempotent.
--
-- SECURITY NOTE: tradier_token is a live broker credential. RLS restricts it to
-- the owning user. Migration 0004 ENCRYPTS this column at rest (Vault key +
-- pgcrypto trigger → tradier_token_enc; the plaintext column is always NULL
-- after the trigger runs). The backend decrypts only via the service_role RPC
-- public.get_broker_secret(). Do not reintroduce plaintext reads of this column.
-- ============================================================================

create extension if not exists pgcrypto;

create table if not exists public.broker_configs (
    id                  uuid        primary key default gen_random_uuid(),
    user_id             uuid        not null references auth.users (id) on delete cascade,
    broker              text        not null default 'tradier',   -- tradier | webull | ...
    tradier_token       text,                                     -- the user's own broker token
    tradier_account_id  text,
    tradier_env         text        not null default 'production', -- production | sandbox
    is_active           boolean     not null default true,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

comment on table public.broker_configs is
    'Per-user broker credentials. tradier_token is sensitive — encrypt at rest (Supabase Vault) before production. Read server-side via service_role for the order path.';

-- one active config per (user, broker)
create unique index if not exists broker_configs_active_uq
    on public.broker_configs (user_id, broker)
    where is_active;

drop trigger if exists broker_configs_set_updated_at on public.broker_configs;
create trigger broker_configs_set_updated_at
    before update on public.broker_configs
    for each row execute function public.set_updated_at();

-- row level security: a user fully manages their OWN rows; no cross-user access.
alter table public.broker_configs enable row level security;

drop policy if exists broker_configs_select_own on public.broker_configs;
create policy broker_configs_select_own
    on public.broker_configs for select to authenticated
    using (auth.uid() = user_id);

drop policy if exists broker_configs_insert_own on public.broker_configs;
create policy broker_configs_insert_own
    on public.broker_configs for insert to authenticated
    with check (auth.uid() = user_id);

drop policy if exists broker_configs_update_own on public.broker_configs;
create policy broker_configs_update_own
    on public.broker_configs for update to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists broker_configs_delete_own on public.broker_configs;
create policy broker_configs_delete_own
    on public.broker_configs for delete to authenticated
    using (auth.uid() = user_id);
