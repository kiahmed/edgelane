<script lang="ts">
	// Where the composite score comes from — the counterpart to the gate
	// checklist. Gates are pass/fail vetoes; THIS is the weighted quality sum
	// that decides the number once a name is un-vetoed:
	//   score = 100 × Σ(weightᵢ · componentᵢ) × regime_mult  (+ earnings lift)
	// Weights are read LIVE from the envelope (component.weight, emitted by the
	// engine after simmer_tickers.json overrides) — never hardcoded here, so a
	// backend retune can't make this reconciliation silently stop adding up.
	import type { ReadinessEnvelope } from '$lib/types';

	let { env }: { env: ReadinessEnvelope } = $props();

	// Display labels + a fallback weight used ONLY for pre-emit cached envelopes
	// (before the engine started emitting component.weight). Live envelopes
	// override every one of these from env.components[key].weight.
	const LABELS: Record<string, { label: string; fallback: number }> = {
		structural_safety: { label: 'Structural safety', fallback: 0.3 },
		expected_move_headroom: { label: 'EM headroom', fallback: 0.2 },
		volatility_richness: { label: 'Vol richness', fallback: 0.2 },
		credit_quality: { label: 'Credit quality', fallback: 0.15 },
		liquidity_quality: { label: 'Liquidity', fallback: 0.1 },
		sentiment_lean: { label: 'Sentiment lean', fallback: 0.05 }
	};

	const mult = $derived(env.regime?.multiplier ?? 1);

	// Build from the components actually present, weight sourced live, ordered
	// weight-descending so the heaviest factors read top-down.
	const rows = $derived(
		Object.entries(env.components ?? {})
			.filter(([k]) => k in LABELS)
			.map(([key, c]) => {
				const weight = c?.weight ?? LABELS[key].fallback;
				const s = Math.max(0, Math.min(1, c?.score ?? 0));
				const max = 100 * weight; // ceiling for this factor (regime ×1)
				return { key, label: LABELS[key].label, weight, s, max, pts: max * s * mult };
			})
			.sort((a, b) => b.weight - a.weight)
	);

	// Earnings fold (only when it actually moved the score).
	const lift = $derived(env.earnings?.applied ? (env.earnings?.lift ?? 0) : 0);
	const base = $derived(rows.reduce((a, r) => a + r.pts, 0));

	// Colour a bar by how much of its own ceiling it filled — instantly shows
	// which factor is the drag regardless of its weight.
	const tone = (s: number) => (s >= 0.66 ? 'good' : s >= 0.34 ? 'mid' : 'low');
	const fmt = (n: number) => n.toFixed(1);
</script>

<div>
	<div class="mb-1 flex items-baseline justify-between">
		<span class="text-[0.65rem] font-bold tracking-wider text-slate-500 uppercase">Score matrix</span>
		<span class="text-[0.7rem] text-slate-500">of {(env.score ?? 0).toFixed(1)}</span>
	</div>
	<div class="space-y-1">
		{#each rows as r (r.key)}
			<div class="matrix-row" title={`weight ${(r.weight * 100).toFixed(0)}% · factor ${r.s.toFixed(2)} → ${fmt(r.pts)} of ${fmt(r.max)} pts`}>
				<span class="matrix-label">{r.label}</span>
				<span class="matrix-bar">
					<span class="matrix-fill matrix-fill--{tone(r.s)}" style="width:{r.s * 100}%"></span>
				</span>
				<span class="matrix-pts">
					<span class="font-mono">{fmt(r.pts)}</span><span class="text-slate-600">/{fmt(r.max)}</span>
				</span>
			</div>
		{/each}
	</div>

	<div class="mt-1.5 space-y-0.5 border-t border-slate-700/50 pt-1.5 text-[0.68rem]">
		<div class="flex justify-between text-slate-400">
			<span>weighted sum{mult !== 1 ? ` · regime ×${mult.toFixed(2)}` : ''}</span>
			<span class="font-mono">{fmt(base)}</span>
		</div>
		{#if lift}
			<div class="flex justify-between" class:text-emerald-300={lift > 0} class:text-rose-300={lift < 0}>
				<span>earnings {lift > 0 ? 'lift' : 'drag'}</span>
				<span class="font-mono">{lift > 0 ? '+' : ''}{fmt(lift)}</span>
			</div>
		{/if}
		<div class="flex justify-between font-semibold text-slate-200">
			<span>score</span>
			<span class="font-mono">{(env.score ?? 0).toFixed(1)}</span>
		</div>
	</div>
</div>

<style>
	.matrix-row {
		display: grid;
		grid-template-columns: minmax(5.5rem, auto) 1fr minmax(3.5rem, auto);
		align-items: center;
		gap: 0.5rem;
	}
	.matrix-label {
		font-size: 0.72rem;
		color: rgb(148 163 184); /* slate-400 */
		white-space: nowrap;
	}
	.matrix-bar {
		position: relative;
		height: 0.5rem;
		border-radius: 9999px;
		background: rgb(51 65 85 / 0.5); /* slate-700/50 */
		overflow: hidden;
	}
	.matrix-fill {
		position: absolute;
		inset: 0 auto 0 0;
		border-radius: 9999px;
	}
	.matrix-fill--good {
		background: rgb(52 211 153); /* emerald-400 */
	}
	.matrix-fill--mid {
		background: rgb(251 191 36); /* amber-400 */
	}
	.matrix-fill--low {
		background: rgb(251 113 133); /* rose-400 */
	}
	.matrix-pts {
		text-align: right;
		font-size: 0.68rem;
		color: rgb(148 163 184);
	}
</style>
