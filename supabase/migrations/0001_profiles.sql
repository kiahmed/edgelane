-- ============================================================================
-- Migration 0001 — user profiles
-- ----------------------------------------------------------------------------
-- public.profiles: 1:1 companion to Supabase's managed auth.users, holding
-- app-specific data — the subscription plan + Stripe bookkeeping for the
-- planned monetization step. Auto-created on signup via a trigger.
--
-- APPLY: Supabase dashboard → SQL Editor → paste → Run. Idempotent.
-- ============================================================================

create table if not exists public.profiles (
    id                     uuid        primary key references auth.users (id) on delete cascade,
    email                  text,
    full_name              text,
    plan                   text        not null default 'free',   -- free | pro
    stripe_customer_id     text,
    stripe_subscription_id text,
    subscription_status    text,
    current_period_end     timestamptz,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now()
);

comment on table public.profiles is
    'App-specific user data, 1:1 with auth.users. Plan/billing columns are written only by the backend via the service_role key.';

-- keep updated_at fresh -------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
    before update on public.profiles
    for each row execute function public.set_updated_at();

-- auto-create a profile row on signup ----------------------------------------
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    insert into public.profiles (id, email, full_name)
    values (new.id, new.email, coalesce(new.raw_user_meta_data ->> 'full_name', ''))
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- row level security ----------------------------------------------------------
alter table public.profiles enable row level security;

-- Browser (anon key) may READ its own profile only.
drop policy if exists profiles_select_own on public.profiles;
create policy profiles_select_own
    on public.profiles for select
    to authenticated
    using (auth.uid() = id);

-- No INSERT/UPDATE/DELETE policy on purpose: plan/billing writes happen only
-- from the backend via service_role (which bypasses RLS). This stops a user
-- from setting their own plan to 'pro' from the browser.

-- backfill any existing users -------------------------------------------------
insert into public.profiles (id, email, full_name)
select u.id, u.email, coalesce(u.raw_user_meta_data ->> 'full_name', '')
from auth.users u
on conflict (id) do nothing;
