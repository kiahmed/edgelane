# Open items

One list of what's left undone, by product. Collected 2026-11-24 from every EdgeLane
session since 2026-11-16 (plus the Reflex and portal sessions where they touch this repo).
**Rule:** any future work left undone goes here, under its product. Delete a line when it ships.

## Matrix
- **Fly breakeven + profit-taking:** show breakevens in bright white on the engine-pick targets line, and add fly exit rules. Spec: [be_and_tp_butteryflys.md](be_and_tp_butteryflys.md).
- **OTM strike fix, live check:** strikes stay out of the money is unit-tested only (range day at deploy); confirm on the next up/down call.
- **Lookup grid IV ~100× too small:** projections come out near intrinsic; found during the level build, not fixed.
- **Bias chip is wall-only on 0DTE:** score = side of spot vs the put wall × strength × reach, so it's ±45 and flips on every cross. It read "mild bullish" about 20 times in the Nov 25 AM sell-off. Replace with the combined bias (Levels Engine).
- **Net GEX / DEX ignored on 0DTE:** they only feed the bias when DTE ≥ 1.5 (`intraday = dte < 1.5`, ported from the JSX engine). All trading is 0DTE, so add them to the 0DTE score.
- **Provider extension retired:** it no longer sends anything (zero webhook posts, no stored profile rows), so the bias falls back to Tradier volume walls. Remove its webhook and `edgelane_provider` path; premium flow comes from the server-side Torque flow below.
- **Pick-post gating (your call):** should pick posts depend only on the engine record? Low bias confidence still blocks them.
- **Open endpoints:** `/accuracy` is public and returns outcomes; `/diag/*` and `/webhook/*` have no login. Gate them or accept.
- **Signup E2E:** one real signup from a fresh inbox to confirm the link lands on Matrix.
- **Daily summary email:** built, 0 users opted in; verify the first real 4:15 PM send.
- **Logs:** a Torque logging change hid Matrix INFO logs in the container; restore the logger level if they're needed.
- **Flaky test:** one test fails intermittently in the full backend suite; never identified.

## Levels Engine
- **Combined bias re-architecture (build week of 2026-11-30):** a new module that leaves the parity-locked `bias_engine` functions untouched. It outputs one score, a confidence and a reason, and Matrix's chip points at it after replay tuning. Votes:
  - the level engine's call and evidence: hold/fail, knife-through, premium building toward the next level;
  - **premium flow (session-cumulative)** and **premium skew**, from Torque's data, see below;
  - price structure: lower highs, failed pops, trend vs VWAP;
  - net GEX regime (+ pins / − amplifies) and net DEX sign, now on 0DTE too;
  - the wall magnet as one damped vote, with a dead band and hysteresis so it can't flip on a wall cross.
- **Torque flow, what exists today:**
  - **Backend, stateless:** `torque_engine.analyze(spot, contracts, band_pct=3)`, served by `GET /torque/analyze/{sym}` and polled about every 5 s by the page. Over the ±3% band:
    - `lean = 100·(0.4·vol_sig + 0.3·skew_sig + 0.3·magnet_sig)`;
    - `components.volume`: call/put contracts and `call_dollar` / `put_dollar` (mid × vol × 100);
    - `premium_skew`: call vs put mid × OI;
    - `oi`, and `magnet` (the max-OI strike × its concentration).
  - **Browser only, lost when the tab closes:** the trailing-60-minute cumulative flow (CVD) lives in `ui/torque.html`:
    - `trackFlow`: per poll, the change in volume × the current $/contract;
    - `sessionFlow`: net, `dir`, a `consistency` over 3-minute buckets, and how long it has held since the last flip;
    - a 3-minute fast window, `SESS_WINDOW_MS` = 60 minutes (evicted), and `tensionRead`.
- **Torque flow, plan:**
  - **Server-side accumulator:** run `analyze()` on the poller's SPX chain each poll. That adds no Tradier calls.
  - **Store per-poll increments** (call $, put $, contracts, `skew_sig`, `vol_sig`, spot) in a table **cumulative from the open, with no 60-minute eviction**.
  - **Expose:** the session net, rolling 3 / 15 / 60-minute windows, the consistency, and flip timestamps.
  - **Feeds:** the bias and the level engine's evidence board, and replay can re-run it.
  - The Torque page can keep its own window, or read the same series.
- **Tune thresholds in replay:** buffer, hold, contested, flip margin, `PICK_SWITCH_MARGIN`, `HEALTH_HYSTERESIS`, all still at spec defaults.
- **Phase 4, Torque level exits** (TP at next level, SL at invalidation): paused. The patch sits in a job temp dir (`~/.claude/jobs/37780452/tmp/phase4_torque_backup/`); move it somewhere durable.
- **Phase 5:** Simmer and Reflex read the levels, later.
- **Per-level-name rules:** names (Golden Gate, triggers, PM high, EMAs) and `condition_text` are placeholders in v1; per-name logic comes later.
- **Day summary for the next day:** level outcomes are stored; the logic that uses them isn't built.
- **Admin page exposure:** reachable from other containers on the Docker network; giving it its own network is optional.
- **Re-strike churn:** watch `plan.itm_restrikes` on trend days.
- **Optional LLM narration** of the manager's reasoning (e.g. LangGraph over the same tables), later.

## Simmer
- **Retire DuckDB `simmer_published_events`:** still in `db.py`; drop it once the ledger is verified ([simmer_events_update.md](simmer_events_update.md) §8).
- **RelativeQs board size:** RQ returns only its top ~8 movers (LRCX missed); widen on the RQ side if you want more.
- **Overnight combiner modes:** your feedback is pending after live days (`llm_only` vs `combiner_blended`).
- **Gemini NEWS path:** out of prepaid credit (HTTP 402); billing, not code.
- **Signup E2E:** one real signup from a fresh inbox to confirm the link lands on Simmer.

## Torque
- **Webull gap:** one-trade-per-direction can't cover Webull, because its client can't read positions.
- **Your call:** should positions opened outside Torque get the extended 10% stop or the saved %? Also, an order placed 09:29 but filled 09:31 gets armed and guarded with different values.
- **Early-close end (13:15):** configured, not broker-verified; Tradier's calendar doesn't publish options extended hours.
- **Tradier $0.10 gap rule:** brackets on cheap contracts can't be re-priced, so the guardian falls back; watch the logs.
- **No subdomain / multi-user login yet:** add it to the CORS regex when its DNS is set up.

## TP (`tools/tradier_positions.py`, the `tp` trade client)
- **OTOCO orders:** let `tp` manage OTOCO brackets: open one, list it with its legs, re-price a leg, cancel. Today it handles plain positions and orders only.

## Reflex (news-reactor touchpoints)
- **Restart rule:** its CLAUDE.md says no restarts 04:00–16:00, but signal hours now run to 16:15. Update if wanted.
- **Old non-SPY posts 28 / 29 / 33:** still show `$SPY` / `#SPY`; re-rendering them is your call.
- **Pre-analysis:** confirm on the first live labelled headline, then review two weeks of results.

## Platform / auth / portal
- **Portal auth-link fix:** pushed (`1e5ff1f`, `f1b125b`), but the live page doesn't show it yet; deploy and verify.
- **Token links in browser history:** always send auth emails with the product redirect (`make invite` / `make reset-password`), never the dashboard buttons.
- **Commit discipline:** commit or push only on the owner's word; a reviewer once pushed unasked.
- **Gitignored live config:** edits such as `HIT_TOL_PTS=5.0` aren't versioned; keep the `.example` files in sync.
