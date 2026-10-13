-- ============================================================================
-- Migration 0029 — per-user overnight-alert toggle
-- (docs/simmer_overnight_llm_spec.md). Controls whether the overnight catalyst
-- analyzer fires an in-app alert (bottom-left AlertFeed + sound) when a play's
-- confidence is ≥70. Mirrors the existing `notify_email` column. Default ON.
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

alter table public.simmer_settings
    add column if not exists notify_overnight boolean not null default true;
