<script lang="ts">
	// The core surface. Refusal-first: gate checklist leads, score is a
	// supporting horizontal bar, and the trade block only appears when the
	// engine says "ready". credit_fill is labeled ACHIEVABLE vs credit_mid's
	// ADVERTISED — the gap is the fill-slippage honesty the docs demand.
	import GateChecklist from './GateChecklist.svelte';
	import ScoreMatrix from './ScoreMatrix.svelte';
	import ReadinessHistory from './ReadinessHistory.svelte';
	import ScoreBar from './ScoreBar.svelte';
	import EarningsPanel from './EarningsPanel.svelte';
	import { gateChecklist } from '$lib/gates';
	import { resolve } from '$app/paths';
	import {
		ago,
		decisionBadgeClass,
		DECISION_LABEL,
		etDateTime,
		humanizeReason,
		money,
		prob,
		sigInt,
		strike,
		structureName
	} from '$lib/fmt';
	import type { ReadinessEnvelope, StrikesCondor, StrikesVertical } from '$lib/types';

	let {
		env,
		readyBand = 70,
		watchBand = 50,
		activeOverrides = [],
		pinnedExpiration = null
	}: {
		env: ReadinessEnvelope;
		readyBand?: number;
		watchBand?: number;
		/** toggleable gates the user has switched OFF (global settings or
		 *  per-ticker) — must surface HERE, not buried in settings. */
		activeOverrides?: string[];
		pinnedExpiration?: string | null;
	} = $props();

	// Cards start collapsed — the watchlist can grow long, and the header's
	// summary chip already carries the one thing that matters at a glance (the
	// sell target, or the veto count). Expand to see the full evaluation.
	let expanded = $state(false);

	const isReady = $derived(env.decision === 'ready');
	const condor = $derived(
		env.strikes && 'put' in env.strikes ? (env.strikes as StrikesCondor) : null
	);
	const vertical = $derived(
		env.strikes && 'short' in env.strikes ? (env.strikes as StrikesVertical) : null
	);

	// Header summary: the short strike(s) you'd sell (the "target"), else how
	// many of the 10 gates vetoed. x/10 uses the gate checklist, not the raw
	// reason tokens (one gate can raise several reasons).
	const checklist = $derived(gateChecklist(env));
	const failedGates = $derived(checklist.filter((c) => c.state === 'fail').length);
	const totalGates = $derived(checklist.length);
	const sellTarget = $derived.by(() => {
		if (!isReady) return null;
		if (vertical) return `${strike(vertical.short)} / ${strike(vertical.long)}`;
		if (condor) return `${strike(condor.put.short)}P / ${strike(condor.call.short)}C`;
		return null;
	});
	const popForecast = $derived(
		(env.candidate?.pop_breakeven_forecast as number | null | undefined) ?? null
	);
	const mgmt = $derived(env.management ?? {});

	const OVERRIDE_TEXT: Record<string, string> = {
		squeeze_veto: 'Squeeze veto off',
		sentiment_veto: 'Sentiment veto off',
		sector_dispersion: 'Sector-dispersion check off'
	};
</script>

<div class="card p-4" class:recommended-glow={isReady}>
	<!-- Header (always visible; carries the summary chip + collapse toggle) -->
	<div class="flex flex-wrap items-center gap-x-3 gap-y-1" class:mb-3={expanded}>
		<span class="text-lg font-black tracking-tight">{env.symbol}</span>
		<span class="pill {decisionBadgeClass(env.decision)}">{DECISION_LABEL[env.decision] ?? env.decision}</span>
		<span class="text-xs text-slate-400">
			{env.expiration ?? '—'} · {sigInt(env.dte)} DTE
			{#if pinnedExpiration}<span class="text-sky-300" title="Expiration pinned by you — the engine is not auto-picking">📌 pinned</span>{/if}
		</span>

		<!-- Summary chip (the white box): the sell target when ready, else the veto count -->
		{#if isReady && sellTarget}
			<span class="summary-chip summary-chip--sell" title="Short/long strikes to sell — expand for credit, POP, EV, management">
				<span class="summary-chip__lead">SELL</span>{sellTarget}
			</span>
		{:else if env.decision === 'vetoed'}
			<span class="summary-chip summary-chip--veto" title="No sellable spread — expand for the gate checklist">
				No sellable spread · {failedGates}/{totalGates} vetoed
			</span>
		{:else}
			<span class="summary-chip summary-chip--veto" title="Score below the ready band — the engine is holding, not selling">
				No sellable spread · below band
			</span>
		{/if}

		<!-- Right group: timestamp (expanded only) + collapse toggle -->
		<div class="ml-auto flex items-center gap-2">
			{#if expanded}
				<span class="text-[0.68rem] text-slate-500" title={env.computed_at ?? ''}>
					underlying {strike(env.spot)} ·
					{#if env.rehydrated}
						<span
							class="text-amber-300/90"
							title="Served from the last persisted sweep (backend restart / market closed) — not a live evaluation"
							>as of {etDateTime(env.computed_at)}</span>
					{:else}
						{ago(env.computed_at)}
					{/if}
					{#if env.market_open === false}
						<span
							class="text-amber-300/90"
							title="Market is closed — computed from the last session's closing chain. Refreshes live during market hours (09:30–16:00 ET)."
							>· last session (check back next session)</span>
					{/if}
				</span>
			{/if}
			<button
				type="button"
				class="collapse-btn"
				aria-expanded={expanded}
				aria-label={expanded ? 'Collapse card' : 'Expand card'}
				title={expanded ? 'Collapse' : 'Expand'}
				onclick={() => (expanded = !expanded)}
			>
				<svg
					viewBox="0 0 24 24"
					class="chevron"
					class:chevron--open={expanded}
					fill="none"
					stroke="currentColor"
					stroke-width="2.5"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
			</button>
		</div>
	</div>

	{#if expanded}
	<!-- Active override warnings — a guard is down; say so where the user looks -->
	{#each activeOverrides as ov (ov)}
		<div class="override-warn mb-2">
			<span>⚠</span>
			<span>{OVERRIDE_TEXT[ov] ?? `${humanizeReason(ov)} off`} — this envelope was scored with that guard down.</span>
		</div>
	{/each}

	<!-- 1. Gate checklist (leads) + score matrix — the two halves of the verdict:
	     WHY it's un/vetoed (left) and WHERE the number comes from (right). Stacks
	     on narrow, side-by-side once there's room. -->
	<div class="grid gap-x-6 gap-y-3 md:grid-cols-2">
		<GateChecklist {env} />
		<!-- Right column: the score matrix collapses to "0.0 / 0.0" on a vetoed
		     card, and vetoed is the common state — so the history strip sits under
		     it and gives the column something to say either way. -->
		<div>
			<ScoreMatrix {env} />
			<ReadinessHistory symbol={env.symbol} {readyBand} {watchBand} />
		</div>
	</div>

	<!-- 2. Score bar -->
	<div class="mt-3">
		<ScoreBar score={env.score} ready={readyBand} watch={watchBand} />
		<div class="mt-1 flex justify-between text-[0.65rem] text-slate-500">
			<span>confidence {money((env.confidence ?? 0) * 100, 0)}%</span>
			{#if env.regime?.state}
				<span class="uppercase">regime: {String(env.regime.state).replace(/_/g, ' ')}</span>
			{/if}
		</div>
	</div>

	<!-- 3. Trade block — only when the engine is ready -->
	{#if isReady && env.structure}
		<div class="mt-3 rounded-lg border border-emerald-500/25 bg-emerald-500/5 p-3">
			<div class="mb-2 flex items-baseline gap-2">
				<span class="text-sm font-bold text-emerald-200">{structureName(env.structure)}</span>
				{#if vertical}
					<code class="kv">{strike(vertical.short)} / {strike(vertical.long)} · {strike(vertical.width)} wide</code>
				{:else if condor}
					<code class="kv">
						P {strike(condor.put.short)}/{strike(condor.put.long)} · C {strike(condor.call.short)}/{strike(condor.call.long)}
					</code>
				{/if}
			</div>
			<div class="grid grid-cols-2 gap-x-4 gap-y-1 text-[0.78rem] sm:grid-cols-3">
				<div>
					<span class="text-slate-500">credit</span>
					<div class="font-mono">
						<span class="text-emerald-200" title="Achievable — mid less realistic fill slippage, rounded down to the tick">
							{money(env.credit_fill)}</span>
						<span class="text-slate-500"> achievable</span>
					</div>
					<div class="font-mono text-slate-400" title="Advertised — the package mid; you will rarely be filled here">
						{money(env.credit_mid)} <span class="text-slate-600">advertised</span>
					</div>
				</div>
				<div>
					<span class="text-slate-500">max loss</span>
					<div class="font-mono">{money(env.max_loss)}</div>
				</div>
				<div>
					<span class="text-slate-500" title="Probability the spread expires at/above breakeven (market IV)">POP</span>
					<div class="font-mono">
						{prob(env.pop_breakeven)}
						{#if popForecast != null}
							<span class="text-slate-500" title="Forecast-vol POP — the gap vs market POP is the edge">
								/ {prob(popForecast)} fc</span>
						{/if}
					</div>
				</div>
				<div>
					<span class="text-slate-500" title="EV per share at forecast vol">EV</span>
					<div class="font-mono">{money(env.expected_value, 3)}</div>
				</div>
				<div>
					<span class="text-slate-500" title="EV / max loss — edge per unit of risk">alpha</span>
					<div class="font-mono">{env.alpha == null ? '—' : (100 * env.alpha).toFixed(2) + '%'}</div>
				</div>
			</div>
			<!-- Management targets: the exit is known before entry -->
			<div class="mt-2 border-t border-emerald-500/15 pt-2 text-[0.7rem] text-slate-400">
				manage: take profit at {money(mgmt.profit_target_pct, 0)}% of max profit ·
				manage at {sigInt(mgmt.manage_dte)} DTE ·
				stop at {money(mgmt.stop_credit_multiple, 0)}× credit
			</div>
		</div>
	{:else}
		<!-- Vetoed / not ready: NO strike is produced (a lone underlying price is
		     not a strike). Say so plainly — the short strike is delta-selected and
		     placed beyond the expected move + wall ONLY when the name clears the
		     gates. -->
		<div class="mt-3 rounded-lg border border-slate-600/30 bg-slate-800/30 p-3 text-[0.8rem] text-slate-400">
			<span class="text-slate-300">No sellable spread.</span>
			{#if env.decision === 'vetoed'}
				Blocked by {failedGates} of {totalGates} gate{failedGates === 1 ? '' : 's'}
				— see the checklist above. Strikes are chosen (delta 0.20–0.35, beyond the
				expected move &amp; wall) only when a name passes.
			{:else}
				Score below the ready band — the engine is holding, not selling.
			{/if}
		</div>
	{/if}

	<!-- Earnings — self-hides unless the expiry is in an earnings window -->
	<EarningsPanel {env} />

	<!-- 4. Soft warnings (avoid_if) — inform, never veto -->
	{#if env.avoid_if?.length}
		<div class="mt-2 space-y-0.5">
			{#each env.avoid_if as w (w)}
				<div class="text-[0.7rem] text-amber-300/90" title={w}>⚠ {humanizeReason(w)}</div>
			{/each}
		</div>
	{/if}
	{/if}

	<!-- Footer: per-symbol news link, anchored to the card bottom (visible while
	     collapsed too, so it lives inside the chip rather than in the gap below). -->
	<div class="mt-2 text-right">
		<a class="card-news-link" href={resolve(`/news/${env.symbol}`)}
			>news &amp; sentiment for {env.symbol} →</a>
	</div>
</div>

<style>
	/* Summary chip — the "white box" in the header. Reads at a glance whether
	   there's a target to sell or how many gates vetoed, even while collapsed. */
	.summary-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		border-radius: 0.375rem;
		border: 1px solid;
		padding: 0.1rem 0.45rem;
		font-size: 0.72rem;
		line-height: 1.1rem;
		white-space: nowrap;
	}
	.summary-chip--sell {
		background: #ffffff;
		border-color: rgba(16, 185, 129, 0.55); /* emerald */
		color: #0f172a; /* slate-900 */
		font-weight: 700;
		font-variant-numeric: tabular-nums;
	}
	.summary-chip__lead {
		font-size: 0.6rem;
		font-weight: 800;
		letter-spacing: 0.05em;
		color: #047857; /* emerald-700 */
	}
	.summary-chip--veto {
		background: rgba(226, 232, 240, 0.92); /* slate-200 — a light box */
		border-color: rgba(100, 116, 139, 0.5);
		color: #334155; /* slate-700 */
	}

	/* Collapse / expand chip (top-right). */
	.collapse-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		height: 1.5rem;
		width: 1.5rem;
		flex: none;
		border-radius: 0.375rem;
		border: 1px solid rgba(71, 85, 105, 0.4);
		color: rgb(148, 163, 184);
		transition: background-color 0.15s, color 0.15s;
	}
	.collapse-btn:hover {
		background: rgba(51, 65, 85, 0.45);
		color: rgb(226, 232, 240);
	}
	.chevron {
		height: 0.85rem;
		width: 0.85rem;
		transition: transform 0.2s ease;
	}
	.chevron--open {
		transform: rotate(180deg);
	}

	/* Per-symbol news link, now living inside the card footer. */
	.card-news-link {
		font-size: 0.68rem;
		color: #94a3b8; /* slate-400 — clearer than the old muted slate-500 */
		transition: color 0.15s;
	}
	.card-news-link:hover {
		color: #7dd3fc; /* sky-300 */
		text-decoration: underline;
	}
</style>
