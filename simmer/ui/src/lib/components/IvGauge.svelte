<script lang="ts">
	// TODO(charts pass): IV rank/percentile gauge as inline SVG.
	// Stubbed pending the dedicated dataviz pass (docs/simmer.md, Phase 4
	// item 32 — load the `dataviz` skill before building).
	import type { ReadinessEnvelope } from '$lib/types';

	let { env }: { env: ReadinessEnvelope } = $props();
</script>

<!-- gauge intentionally not rendered yet -->
