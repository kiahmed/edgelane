<script lang="ts">
	// Refusal-first lead element: every gate with its pass/fail/unknown state
	// and the engine's actual reason strings. The default state of this product
	// is "vetoed — here's why", so this renders before any score.
	import { gateChecklist, failedCount, type GateCheck } from '$lib/gates';
	import { humanizeReason } from '$lib/fmt';
	import type { ReadinessEnvelope } from '$lib/types';

	let { env }: { env: ReadinessEnvelope } = $props();

	const checks: GateCheck[] = $derived(gateChecklist(env));
	const failed = $derived(failedCount(checks));

	const icon = (s: string) => (s === 'pass' ? '✓' : s === 'fail' ? '✗' : '?');
	const cls = (s: string) =>
		s === 'pass' ? 'gate-pass' : s === 'fail' ? 'gate-fail' : 'gate-unknown';
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
					<span class="min-w-0 truncate text-[0.7rem] text-slate-400" title={c.reasons.join('\n')}>
						{c.reasons.map(humanizeReason).join(' · ')}
					</span>
				{/if}
			</div>
		{/each}
	</div>
</div>
