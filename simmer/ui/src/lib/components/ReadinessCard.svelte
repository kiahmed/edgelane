<script lang="ts">
	// The core surface. Refusal-first: gate checklist leads, score is a
	// supporting horizontal bar, and the trade block only appears when the
	// engine says "ready". credit_fill is labeled ACHIEVABLE vs credit_mid's
	// ADVERTISED — the gap is the fill-slippage honesty the docs demand.
	import GateChecklist from './GateChecklist.svelte';
	import ScoreBar from './ScoreBar.svelte';
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

	const isReady = $derived(env.decision === 'ready');
	const condor = $derived(
		env.strikes && 'put' in env.strikes ? (env.strikes as StrikesCondor) : null
	);
	const vertical = $derived(
		env.strikes && 'short' in env.strikes ? (env.strikes as StrikesVertical) : null
	);
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
	<!-- Header -->
	<div class="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
		<span class="text-lg font-black tracking-tight">{env.symbol}</span>
		<span class="pill {decisionBadgeClass(env.decision)}">{DECISION_LABEL[env.decision] ?? env.decision}</span>
		<span class="text-xs text-slate-400">
			{env.expiration ?? '—'} · {sigInt(env.dte)} DTE
			{#if pinnedExpiration}<span class="text-sky-300" title="Expiration pinned by you — the engine is not auto-picking">📌 pinned</span>{/if}
		</span>
		<span class="ml-auto text-[0.68rem] text-slate-500" title={env.computed_at ?? ''}>
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
	</div>

	<!-- Active override warnings — a guard is down; say so where the user looks -->
	{#each activeOverrides as ov (ov)}
		<div class="override-warn mb-2">
			<span>⚠</span>
			<span>{OVERRIDE_TEXT[ov] ?? `${humanizeReason(ov)} off`} — this envelope was scored with that guard down.</span>
		</div>
	{/each}

	<!-- 1. Gate checklist (leads) -->
	<GateChecklist {env} />

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
				Blocked by {(env.veto_reasons ?? []).length} gate{(env.veto_reasons ?? []).length === 1 ? '' : 's'}
				— see the checklist above. Strikes are chosen (delta 0.20–0.35, beyond the
				expected move &amp; wall) only when a name passes.
			{:else}
				Score below the ready band — the engine is holding, not selling.
			{/if}
		</div>
	{/if}

	<!-- 4. Soft warnings (avoid_if) — inform, never veto -->
	{#if env.avoid_if?.length}
		<div class="mt-2 space-y-0.5">
			{#each env.avoid_if as w (w)}
				<div class="text-[0.7rem] text-amber-300/90" title={w}>⚠ {humanizeReason(w)}</div>
			{/each}
		</div>
	{/if}
</div>
