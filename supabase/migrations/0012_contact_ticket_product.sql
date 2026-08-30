-- 0012_contact_ticket_product.sql
-- Which product a support ticket came from.
--
-- Until now only Matrix had a Contact dialog, so POST /contact needed no product
-- field — every row was implicitly Matrix. Simmer now posts to the SAME endpoint,
-- so without this column an operator can't tell the two apart without reading the
-- message body. Torque is included in the backend's allowlist for when it grows a
-- contact surface; it has no public UI today.
--
-- The two-step default is deliberate:
--   1. add with default 'matrix' — every PRE-EXISTING row is genuinely a Matrix
--      ticket, so this backfills history correctly rather than guessing;
--   2. then flip the default to 'unknown' — a FUTURE insert that omits the field
--      (e.g. a stale cached frontend build that predates this change) should be
--      honestly unattributed, not silently mis-filed as Matrix.
--
-- Values are validated in the backend against an allowlist (app/routes/contact.py)
-- rather than a CHECK constraint here, so adding a fourth product is a code change
-- and not a migration.
--
-- APPLY: `make db-push` (idempotent). Safe to re-run.

alter table public.contact_tickets
    add column if not exists product text not null default 'matrix';

alter table public.contact_tickets
    alter column product set default 'unknown';

comment on column public.contact_tickets.product is
    'Facades product the ticket was submitted from: matrix | simmer | torque | unknown. Backfilled to matrix (the only sender before this column existed); defaults to unknown so a client that omits it is not mis-attributed.';

create index if not exists contact_tickets_product_idx on public.contact_tickets (product);
