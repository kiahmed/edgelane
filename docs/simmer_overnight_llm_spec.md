# Simmer Overnight — LLM catalyst analyzer: implementation spec (DRAFT)

Status: **design, pre-build.** Organizes the decisions from the Oct 2026 grounded-LLM
experiments (docs below) into an implementation plan. Nothing here is wired yet.

Related: `docs/simmer.md` → "Overnight movers", `docs/simmer_off_hours_catalyst.md`,
`docs/simmer_overnight_option_b.md`. Builds on the existing `simmer_overnight` table
and `simmer_premium_tracker` (deterministic premium math).

---

## 1. Goal

Add a **catalyst-quality read** to each overnight mover: *why* it moved, whether the
move looks deserved vs an overreaction, the web/social vibe, and whether it is likely
to **hold / extend / fade** through the session. This is the missing ingredient the
pure premium+price heuristic can't see (the "it jumped but doesn't deserve it" case).

It is an **input signal, never the decision engine.** Simmer's rules hold:
- *Never delegate math to the LLM* (CLAUDE.md). Premium/underlying math stays
  deterministic in `simmer_premium_tracker`.
- *Catalyst never promotes.* The LLM can only sharpen/annotate a side the
  deterministic engine already allows; it cannot create a trade.

## 2. What the experiments settled (takeaways)

- **Grounded web search delivers real catalysts** (CXMT/DRAM for MU/SNDK, OCP-Summit
  for LITE, analyst actions for TMUS/SHOP). This is the value.
- **Two-call shape works:** call 1 = web-grounded prose read; call 2 = strict-schema
  extraction. Grounding and strict response-schema can't be combined in one call on
  Gemini, so two calls is required anyway.
- **Model read (2 days, post-close, hindsight-contaminated, ~16 names):** OpenAI
  `gpt-6-luna` ≈7–8/8 both days and caught both big down-gaps; `gemini-3.8-flash`
  5/8–6/8 with two repeatable flaws — a **down-gap "it'll mean-revert / bounce" bias**
  and a **broken `confidence` field** in extraction. `gpt-6-luna` == `gpt-6-sol` on
  decisions, cheaper → Luna over Sol.
- **Cost:** OpenAI's hosted `web_search` bills the retrieved pages as input tokens
  (~25–40K/call) vs Gemini grounding's ~6K. `search_context_size:low` did **not**
  reduce it in testing (run-to-run variance dominated). Budget for the high token cost
  if OpenAI is live.
- **Not deterministic:** same model/prompt flipped a few calls run-to-run. Argues for
  logging + evaluation over trusting a single call, and possibly 2-run agreement.
- **Caveat on all of the above:** every run was post-close, so a clean **forward test**
  at the real 9:25–close window is required before the signal affects any user verdict.

## 3. Default config (all DB-held in `simmer_engine_settings`, per existing pattern)

| setting | default | notes |
|---|---|---|
| `overnight_llm_enabled` | `false` | master switch; starts off |
| `overnight_llm_provider` | `openai` | `openai` \| `gemini` |
| `overnight_llm_model` | `gpt-6-luna` | |
| `overnight_llm_thinking` | `medium` | Gemini `thinkingLevel` / OpenAI `reasoning.effort` |
| `overnight_llm_context_size` | `medium` | OpenAI `web_search.search_context_size`; **configurable to `low`** |
| `overnight_decision_mode` | `llm_only` | `llm_only` \| `combiner_blended` — only TWO; both logged for comparison |
| `overnight_shadow_provider` | `gemini` | run in parallel, logged, not acted on; `null` to disable |

**All DB-held — no restart to change.** These live in the `overnight_llm` section of
`simmer_engine_settings`; `run_catalyst` **re-reads the table right before each run**, so
a change made that morning takes effect same-day. Only the **API keys** stay in
`edgelane_market.config` (secrets out of the DB; set once).

Reading schedule (9:25 / ~10:00 / 16:00) stays as code constants, not knobs.

## 4. Provider abstraction (the "no framework" decision)

A thin internal seam — **not** LangChain/LangGraph. Mirrors reflex's
`gemini.py`/`openai_llm.py` split.

```
simmer_llm/
  base.py      # OvernightLLM protocol: 2 methods
  gemini.py    # google_search grounding + responseSchema
  openai.py    # Responses API web_search + json_schema (strict)
  factory.py   # provider string -> adapter (+ shadow)
```

```python
class OvernightLLM(Protocol):
    def grounded_read(self, ctx: CatalystCtx) -> GroundedNote: ...      # call 1 (web)
    def structured_read(self, note: str, ctx) -> CatalystVerdict: ...   # call 2 (schema)
```

- Swapping provider = change `overnight_llm_provider`; no call-site changes.
- Testable: mock the 2 methods; the pure combiner (§6) is already unit-testable.
- `CatalystVerdict` is a typed model (pydantic) with the fields in §6 — one place to
  validate `confidence` as `int 0..100` (fixes the Gemini bug by construction).
- LiteLLM/Pydantic-AI are optional later swaps *behind* this seam; grounding config is
  provider-specific so the adapter owns it regardless.

## 5. Persistence (DEDICATED table — keep every day's reading; don't drop the analysis)

A **separate table**, NOT an extension of `simmer_overnight` — so the experimental /
droppable LLM + bake-off data never touches the lean core row the UI reads (your
"separate pluggable, don't corrupt the core" rule).

- `simmer_overnight` — UNCHANGED: premium baseline + the live premium verdict the
  Overnight tab renders today.
- **`simmer_overnight_analysis`** (NEW, keyed `(symbol, session_date)`; rows never
  deleted → it IS the per-day history):
  - `readings` jsonb array — `[{t:"09:25"|"10:00"|"16:00", spot, premiums:{strike:mid}}]`.
    Minimal by design: **3 readings/day**, not per-minute.
  - `premarket` jsonb — the 9:25 snapshot `{spot, gap, premiums}` (mirrors
    simmer_overnight's baseline, copied here so the analysis row is self-contained).
  - `close` jsonb — the 16:00 snapshot + `intraday_pct`, `vs_prior_close_pct`.
  - `outcome` text — realized `held | faded | extended` (+ `direction_right` bool),
    computed deterministically from the spot path at close by the evaluator.
  - `premium` jsonb — the premium-only verdict snapshot (call + trends + confidence).
  - `llm` jsonb — `{provider, model, catalyst_type, deserved, web_social_sentiment,
    sustain, confidence, call, one_liner, note, grounded_queries, latency_ms, tokens}`.
  - `llm_shadow` jsonb — the shadow provider's parallel verdict (same shape), logged only.
  - `combiner` jsonb — `{call, confidence, source, reasons, mode}` (read-only in Phase 0).

A reading job (new child of `overnight_loop`) fires at 09:25 (baseline + LLM), ~10:00
(reading), and 16:00 (close reading + outcome stamp). **Lookback:** when a symbol
reappears, query prior `simmer_overnight_analysis` rows for it → `{date, gap,
catalyst_type, sustain(pred) vs outcome, intraday result}`. Reference/plot only —
**not** a decision input (per your call), at least initially.

## 6. Call architecture (bang-for-buck, keeps the math rule)

Three stages, premium math stays out of the LLM:

1. **Call 1 — grounded read** (provider-agnostic). Input: symbol, gap, dir, crowd,
   date, **plus a one-line premium-trend summary** (so the sustain judgment is
   premium-aware) — but the LLM is told to *reason*, not compute. Output: prose note.
2. **Call 2 — structured extract** (strict schema): `catalyst_type, deserved,
   web_social_sentiment, sustain, confidence(0-100), call, one_liner`.
3. **Deterministic combiner** (pure fn, unit-tested): final `call` from the LLM fields
   **+** `premium_trend` **+** `underlying_trend` **+** `crowd`. The LLM supplies
   catalyst quality + sustain probability; `simmer_premium_tracker` supplies the
   price/premium confirmation; the combiner decides sell_bull_put / sell_bear_call /
   wait / skip and confidence.

> Decision: **do NOT hand the final play to the LLM** (your "mix premium into call 2
> and let it decide" idea). It violates *never delegate the decision/math to the LLM*
> and *catalyst never promotes*, and it's far less testable. Premium-aware **context**
> into call 1 is the safe way to get the same benefit.

## 7. Rollout (de-contaminate before it matters)

- **Phase 0 — shadow/read-only (build first):** adapter + persistence + reading job +
  shadow logging. Runs live at 9:25–close for **~1–2 weeks**, persisting predictions and
  realized outcomes. **Does not change the user-facing verdict.** This is the clean
  forward test (no hindsight).
- **Phase 1 — evaluate:** accuracy of predicted sustain/call vs realized outcome, per
  provider/model; confirm OpenAN>Gemini (or not); apply the two Gemini fixes if keeping
  it as shadow. Pick the live model.
- **Phase 2 — promote:** fold the catalyst signal into the combiner as an input
  (still catalyst-never-promotes). Surface the lookback/history in the Overnight tab.

## 8. Open decisions (for you)

1. **Framework:** confirm thin adapter (recommended) vs LiteLLM vs LangGraph.
2. **Call architecture:** confirm LLM-as-signal + deterministic combiner (recommended)
   vs letting call 2 decide the play.
3. **Reading cadence:** 09:25 + 10:00 + 16:00 (recommended) vs just 09:25 + 16:00.
4. **Shadow provider:** keep Gemini shadow on (recommended, cheap insurance) vs off.
5. **Phase 0 read-only forward test** before it affects any verdict — confirm.

## 9. Non-goals / constraints

- No per-minute premium recording. No LLM-computed numbers. No new user-facing trade
  created by the catalyst. No framework dependency unless a decision above changes it.

## 10. Remaining feature list (post Phase 0)

Phase 0 (grounded read + shadow + readings + outcome, read-only) is BUILT. What's left:

### Phase 1 — Evaluate (after ~1–2 weeks of data)
- Build a small accuracy evaluator/report: query `simmer_overnight_analysis`, compute
  hit-rate + `direction_right %` by provider (OpenAI live vs Gemini shadow) and by mode
  (`llm_only` vs `combiner_blended`) against the realized outcome.
- Decide the winner: provider, model, mode. Apply prompt tweaks if Gemini stays shadow.
- Buildable now (the evaluator is just a read over the table), but it produces nothing
  until rows accumulate — so it's a "when data exists" task, not tonight.

### Phase 2 — Promote + surface (the only part that touches users)
- Fold the chosen catalyst signal into the user-facing verdict (still
  catalyst-never-promotes).
- Frontend: surface the catalyst read (why-it-moved, sustain, confidence) + the history
  lookback ("last time this name gapped") in the Overnight tab. The
  `get_overnight_analysis_history` helper exists; still needs a route + UI.
- **Per-user display toggle** (`overnight_show_combiner`): the backend ALWAYS computes
  AND scores BOTH `llm_only` and `combiner_blended` for everyone (user-agnostic row);
  this per-user toggle only picks which the Overnight tab shows (on → the frozen
  combiner_blended call, off → llm_only). Clean build (don't half-wire): add the column
  via a migration (mirroring `import_rq_overnight` in 0026), map it in
  `sanitize_settings`, default it in the settings GET, extend the `/simmer/overnight`
  route to return both calls from `simmer_overnight_analysis`, and add the checkbox.
  NOT built in Phase 0 — a column-less sanitize mapping would 400 on write.
- This is the only phase with a Simmer frontend deploy.

### Scoring note (built in Phase 0)
The 16:00 close reading scores EVERY candidate independently into `close`:
`llm_only_call`/`llm_only_right` and `combiner_blended_call`/`combiner_blended_right`
(plus the decision_mode `final` as top-level `direction_right`). So the bake-off can
compare both modes no matter what `decision_mode` is set to — and both scored calls are
made before the outcome window (llm_only frozen 09:25, combiner_blended frozen 10:00).
