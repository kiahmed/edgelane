-- ============================================================================
-- Migration 0030 — overnight two-sided (opposite/reversal) premium snapshot
-- (docs/simmer_overnight_rq_impl_spec.md §5). Adds one jsonb column holding the
-- OPPOSITE-side (reversal) strikes + baseline + live premiums, captured from the
-- SAME chain the toward side already fetches (zero extra broker calls). Powers the
-- leading reversal signals (skew_shift, gap_retraced) the drift override reads so
-- it can react before the toward-side premium fully deflates.
--
-- Shape: {"side": "call|put", "strikes": [..], "baseline": {k:mid}, "latest": {k:mid}}
--
-- APPLY: make db-push   (idempotent)
-- ============================================================================

alter table public.simmer_overnight
    add column if not exists opp jsonb not null default '{}'::jsonb;
