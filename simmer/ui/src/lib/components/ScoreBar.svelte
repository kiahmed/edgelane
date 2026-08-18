<script lang="ts">
	// Horizontal composite-score bar with Matrix's composite-good/meh/bad color
	// semantics. Deliberately NOT a radar/pentagon chart — docs/simmer.md
	// rejects those explicitly. The score supports the gate checklist; it never
	// leads the card.
	import { scoreBarClass, scoreClass, money } from '$lib/fmt';

	let {
		score,
		ready = 70,
		watch = 50,
		label = 'Score'
	}: {
		score: number | null | undefined;
		ready?: number;
		watch?: number;
		label?: string;
	} = $props();

	const width = $derived(Math.max(0, Math.min(100, Number(score ?? 0))));
</script>

<div class="flex items-center gap-3">
	<span class="w-12 shrink-0 text-[0.65rem] tracking-wider text-slate-500 uppercase">{label}</span>
	<div class="scorebar-track flex-1" title="Composite 0–100 · ready ≥ {ready}, watch ≥ {watch}">
		<div class="scorebar-fill {scoreBarClass(score, ready, watch)}" style="width: {width}%"></div>
	</div>
	<span class="w-12 shrink-0 text-right font-mono text-sm font-bold {scoreClass(score, ready, watch)}">
		{money(score, 1)}
	</span>
</div>
