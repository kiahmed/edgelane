-- ============================================================================
-- Migration 0013 — signup grants the tool of the product you signed up from
-- ----------------------------------------------------------------------------
-- Until now handle_new_user() created every profile with tools_enabled = '{}'
-- (0009 defaulted the column and never granted anything), so a user who signed
-- up through Matrix or Simmer could not be told apart, and the only grant path
-- was operator seeding at backend startup.
--
-- Now each product's signup passes `signup_product` in the auth metadata
-- (supa.auth.signUp({ options: { data: { signup_product } } })) and the
-- trigger seeds tools_enabled from it:
--
--     'market'  → {market}     (Matrix)
--     'simmer'  → {simmer}     (Simmer)
--     anything else / absent   → {}  (unchanged behaviour)
--
-- WHITELISTED, because raw_user_meta_data is USER-CONTROLLED: a caller can put
-- anything in signUp's `data`. Only the two self-serve product keys are ever
-- honoured — never 'torque' or any other tool, which stay operator-granted.
--
-- Deliberately NO `raise` and NO CHECK constraint. This trigger runs inside
-- the auth.users insert, so failing it fails the signup itself — and users
-- created from the Supabase dashboard / admin API carry no metadata at all.
-- "Every profile must have a tool" is enforced at LOGIN (each product refuses a
-- session whose profile lacks its key), not by the database.
--
-- APPLY: Supabase dashboard → SQL Editor → paste → Run. Idempotent (create or
-- replace + drop/create trigger, same as 0001).
-- ============================================================================

create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
declare
    product text := lower(coalesce(new.raw_user_meta_data ->> 'signup_product', ''));
    tools   text[];
begin
    tools := case product
                 when 'market' then array['market']
                 when 'simmer' then array['simmer']
                 else '{}'::text[]
             end;

    insert into public.profiles (id, email, full_name, tools_enabled)
    values (new.id,
            new.email,
            coalesce(new.raw_user_meta_data ->> 'full_name', ''),
            tools)
    on conflict (id) do nothing;
    return new;
end;
$$;

comment on function public.handle_new_user() is
    'Creates the profile on signup and grants the tool of the signup product (raw_user_meta_data.signup_product, whitelisted to market|simmer). Never raises.';

-- The trigger itself is unchanged from 0001; re-assert it so this file is
-- self-sufficient on a fresh project.
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();
