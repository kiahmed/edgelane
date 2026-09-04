<script lang="ts">
	// Refusal-first lead element: every gate with its pass/fail/unknown state
	// and the engine's actual reason strings. The default state of this product
	// is "vetoed — here's why", so this renders before any score.
	import { gateChecklist, failedCount, type GateCheck, isCoupledDeltaCreditVeto} from '$lib/gates';
	import { humanizeReason } from '$lib/fmt';
	import type { ReadinessEnvelope } from '$lib/types';

	let { env }: { env: ReadinessEnvelope } = $props();

	const checks: GateCheck[] = $derived(gateChecklist(env));
	const failed = $derived(failedCount(checks));

	const icon = (s: string) => (s === 'pass' ? '✓' : s === 'fail' ? '✗' : '?');
	const cls = (s: string) =>
		s === 'pass' ? 'gate-pass' : s === 'fail' ? 'gate-fail' : 'gate-unknown';

	/** Distinct failing sub-checks for one gate, in first-seen order.
	 *  `bear_call:liquidity:open_interest` -> "open interest". Falls back to the
	 *  gate segment when a reason has no detail. */
	function detailsOf(reasons: string[]): string[] {
		const seen = new Set<string>();
		for (const raw of reasons) {
			const parts = String(raw).split(':');
			seen.add(humanizeReason(parts.length > 2 ? parts[2] : parts[parts.length - 1]));
		}
		return [...seen];
	}
</script>

<div>
	<div class="mb-1 flex items-baseline justify-between">
		<span class="text-[0.65rem] font-bold tracking-wider text-slate-500 uppercase">Gates</span>
		{#if failed > 0}
			<span class="text-[0.7rem] text-rose-300">{failed} of {checks.length} vetoed</span>
		{:else}
			<span class="text-[0.7rem] text-emerald-300">all {checks.length} clear</span>
		{/if}
	</div>
	<div class="space-y-px">
		{#each checks as c (c.key)}
			<div class="gate-row">
				<span class="w-4 shrink-0 text-center font-mono {cls(c.state)}">{icon(c.state)}</span>
				<span class="shrink-0 {cls(c.state)}">{c.label}</span>
				{#if c.locked}
					<span class="text-[0.6rem] text-slate-600" title="Safety gate — never user-adjustable">🔒</span>
				{/if}
				{#if c.reasons.length}
					<!-- Reasons arrive as structure:gate:detail, one per permutation — six
					     entries saying "liquidity failed" three different ways. The gate is
					     already named to the left, so show only the DISTINCT failing detail,
					     and wrap instead of truncating (this line used to read
					     "bear call · liquidity · open interest · be…"). -->
					<span class="gate-why" title={c.reasons.join('\n')}>
						{detailsOf(c.reasons).join(' · ')}
					</span>
				{/if}
				{#if c.hint && c.state === 'fail'}
					<span class="gate-hint">{c.hint}</span>
				{/if}
			</div>
		{/each}
	</div>
	{#if isCoupledDeltaCreditVeto(checks)}
		<!-- Three vetoes, one cause. Without this, a reader tunes three knobs. -->
		<p class="gate-coupled">
			These are one problem, not three: a low-delta short is a far-OTM short, and a far-OTM short
			collects thin credit — so the round-trip cost eats it. Read it as
			<strong>too far out for the premium on offer</strong>.
		</p>
	{/if}
</div>
