-- ============================================================================
-- Migration 0009 — per-user tool entitlements
-- ----------------------------------------------------------------------------
-- public.profiles.tools_enabled: the set of tool keys a user may use, e.g.
-- {'market','torque'}. Gating is enforced server-side (the backend reads this
-- via the service_role key); the column is also readable by the owner through
-- the existing profiles RLS select-own policy so the frontend can show/hide
-- tool entry points. Like plan/billing, it is WRITTEN only by the backend
-- (service_role) — a user cannot grant themselves a tool from the browser.
--
-- APPLY: Supabase dashboard → SQL Editor → paste → Run. Idempotent.
-- ============================================================================

alter table public.profiles
    add column if not exists tools_enabled text[] not null default '{}';

comment on column public.profiles.tools_enabled is
    'Tool keys this user is entitled to use (e.g. {market,torque}). Written only by the backend via service_role; readable by the owner via RLS so the UI can show/hide tools.';

-- No operator is seeded here. Operator entitlements are granted at backend
-- startup from config (TORQUE_OPERATOR_UIDS env / torque_tickers.json
-- "operator_uids"), so no identity literal (email/uid) is committed to VCS.
-- The admin token bypasses this gate regardless. To grant a specific user
-- manually, run in the dashboard (substitute the uid):
--   update public.profiles
--      set tools_enabled = (select array(select distinct
--            unnest(coalesce(tools_enabled,'{}') || array['market','torque'])))
--    where id = '<operator-auth-uid>';
