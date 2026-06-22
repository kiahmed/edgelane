-- ============================================================================
-- Migration 0003 — per-user notification / UI preferences
-- ----------------------------------------------------------------------------
-- public.user_settings: 1:1 companion to auth.users holding non-sensitive,
-- browser-managed preferences — notification toggles and UI defaults. Written
-- directly from the browser with the anon key under RLS (own row only); no
-- service_role needed since nothing here is a secret.
--
-- APPLY: Supabase dashboard → SQL Editor → paste → Run. Idempotent.
-- ============================================================================

create table if not exists public.user_settings (
    user_id              uuid        primary key references auth.users (id) on delete cascade,
    -- notification preferences (free-form JSON so we can add toggles without a migration)
    notifications        jsonb       not null default '{
        "email_fills": true,
        "email_regime_shift": true,
        "email_daily_summary": false
    }'::jsonb,
    -- misc UI defaults (poll interval, default symbol, etc.)
    ui_prefs             jsonb       not null default '{}'::jsonb,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

comment on table public.user_settings is
    'Per-user non-sensitive preferences (notification toggles, UI defaults). Browser-managed under RLS; no secrets here.';

drop trigger if exists user_settings_set_updated_at on public.user_settings;
create trigger user_settings_set_updated_at
    before update on public.user_settings
    for each row execute function public.set_updated_at();

-- auto-create a settings row on signup (reuse the same pattern as profiles) ---
create or replace function public.handle_new_user_settings()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    insert into public.user_settings (user_id)
    values (new.id)
    on conflict (user_id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created_settings on auth.users;
create trigger on_auth_user_created_settings
    after insert on auth.users
    for each row execute function public.handle_new_user_settings();

-- row level security: a user fully manages their OWN row only -----------------
alter table public.user_settings enable row level security;

drop policy if exists user_settings_select_own on public.user_settings;
create policy user_settings_select_own
    on public.user_settings for select to authenticated
    using (auth.uid() = user_id);

drop policy if exists user_settings_insert_own on public.user_settings;
create policy user_settings_insert_own
    on public.user_settings for insert to authenticated
    with check (auth.uid() = user_id);

drop policy if exists user_settings_update_own on public.user_settings;
create policy user_settings_update_own
    on public.user_settings for update to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- backfill any existing users -------------------------------------------------
insert into public.user_settings (user_id)
select u.id from auth.users u
on conflict (user_id) do nothing;
