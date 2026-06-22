-- ============================================================================
-- Migration 0005 — app_config (runtime service discovery for the backend URL)
-- ----------------------------------------------------------------------------
-- The backend runs behind a Cloudflare *quick* tunnel (free, no domain) whose
-- *.trycloudflare.com hostname rotates whenever the cloudflared process
-- restarts. Rather than re-baking the URL into the Vercel frontend on every
-- rotation, the backend publishes its current public URL here on startup and
-- the frontend reads it at load (and re-reads on a failed call). No redeploy.
--
-- Only the `api_base` row is anon-readable (it's a URL, not a secret — the API
-- itself is auth-gated). Writes are service_role-only (RLS denies everyone
-- else; service_role bypasses RLS).
--
-- APPLY: `make db-push` (idempotent).
-- ============================================================================

create table if not exists public.app_config (
    key        text        primary key,
    value      text,
    updated_at timestamptz not null default now()
);

comment on table public.app_config is
    'Public runtime config (service discovery). Only the api_base row is anon-readable; values here must be non-sensitive. Written by the backend via service_role.';

-- ensure the pointer row exists so a SELECT always resolves
insert into public.app_config (key, value) values ('api_base', null)
on conflict (key) do nothing;

drop trigger if exists app_config_set_updated_at on public.app_config;
create trigger app_config_set_updated_at
    before update on public.app_config
    for each row execute function public.set_updated_at();

-- RLS: anon/authenticated may read ONLY api_base; nobody but service_role writes.
alter table public.app_config enable row level security;

drop policy if exists app_config_read_api_base on public.app_config;
create policy app_config_read_api_base
    on public.app_config for select to anon, authenticated
    using (key = 'api_base');
