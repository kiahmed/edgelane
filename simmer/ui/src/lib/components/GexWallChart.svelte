<script lang="ts">
	// TODO(charts pass): GEX/DEX wall visualization as inline SVG.
	// Deliberately stubbed — the visualization components (GexWallChart,
	// IvGauge, ExpectedMoveViz) are a later task that must load the `dataviz`
	// skill first (docs/simmer.md, Phase 4 item 32). Do not add a chart library.
	import type { ReadinessEnvelope } from '$lib/types';

	let { env }: { env: ReadinessEnvelope } = $props();
</script>

<!-- chart intentionally not rendered yet -->
