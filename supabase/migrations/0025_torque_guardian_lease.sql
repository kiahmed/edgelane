-- ============================================================================
-- Migration 0025 — Torque stop guardian: single-runner lease
-- ----------------------------------------------------------------------------
-- Only one backend process may run the stop guardian per environment. Each
-- pass, a guardian takes (or renews) the lease row for its environment
-- ("guardian:live" / "guardian:sandbox"); it acts only while it holds it. A
-- second container — or a misconfigured local backend — finds the lease held
-- and stands down, so two guardians can never race the same exit. A lease
-- that isn't renewed expires (a crashed holder) and the next one takes over.
--
-- Backend-only (service_role); RLS on with zero policies.
-- APPLY: make db-setup   (idempotent)
-- ============================================================================

create table if not exists public.torque_guardian_lease (
    name        text        primary key,
    holder      text        not null,
    expires_at  timestamptz not null,
    updated_at  timestamptz not null default now()
);

comment on table public.torque_guardian_lease is
    'Torque stop guardian single-runner lease, one row per environment. Only the holder acts; expired leases are taken over.';

drop trigger if exists torque_guardian_lease_set_updated_at on public.torque_guardian_lease;
create trigger torque_guardian_lease_set_updated_at
    before update on public.torque_guardian_lease
    for each row execute function public.set_updated_at();

alter table public.torque_guardian_lease enable row level security;
