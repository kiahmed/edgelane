-- ============================================================================
-- Migration 0008 — contact / support tickets
-- ----------------------------------------------------------------------------
-- The market UI Contact dialog POSTs a support ticket to the backend
-- (POST /contact, multipart). The backend stores any attachment in a PRIVATE
-- Storage bucket, inserts a row here, then emails support (Resend). See
-- docs/contact_tickets.md + app/routes/contact.py.
--
-- Writes happen ONLY from the backend via the service_role key (which bypasses
-- RLS), so RLS is enabled with NO policies — anon/authenticated browsers can
-- neither read nor write this table directly. user_id is captured opportunistically
-- from the JWT when present, and is ON DELETE SET NULL so deleting a user keeps
-- the support record intact (audit/history) rather than cascading it away.
--
-- APPLY: `make db-push` (idempotent). Safe to re-run.
-- ============================================================================

create table if not exists public.contact_tickets (
    id              uuid        primary key default gen_random_uuid(),
    user_id         uuid        references auth.users (id) on delete set null,
    name            text        not null,
    email           text        not null,
    message         text        not null,
    attachment_path text,                                   -- object path in the private bucket
    attachment_name text,                                   -- original filename
    attachment_size bigint,                                 -- bytes
    status          text        not null default 'open',    -- open | closed | spam
    created_at      timestamptz not null default now()
);

comment on table public.contact_tickets is
    'Support tickets from the UI Contact form. Written only by the backend via service_role; RLS denies all browser access. Attachments live in the private contact-attachments Storage bucket (attachment_path).';

create index if not exists contact_tickets_created_at_idx on public.contact_tickets (created_at desc);
create index if not exists contact_tickets_status_idx     on public.contact_tickets (status);

-- RLS on, no policies → only service_role (backend) can touch it.
alter table public.contact_tickets enable row level security;

-- Private bucket for attachments (no public read; backend signs/streams if ever needed).
insert into storage.buckets (id, name, public)
values ('contact-attachments', 'contact-attachments', false)
on conflict (id) do nothing;
