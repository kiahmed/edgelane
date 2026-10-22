-- ============================================================================
-- Migration 0031 — Phase B llm_recall column
-- (docs/simmer_overnight_rq_impl_spec.md §6). The premium-triggered LLM recall
-- (simmer_overnight_catalyst.run_recall) persists its re-read here. Without the
-- column the recall upsert fails (PGRST204 unknown column) and silently drops,
-- so this must exist before overnight_llm.recall_enabled is turned on.
--
-- Shape: {for_call, call, deserved, sentiment, sustain, why_moved, one_liner,
--         confidence, provider, at, count}
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

alter table public.simmer_overnight_analysis
    add column if not exists llm_recall jsonb not null default '{}'::jsonb;
