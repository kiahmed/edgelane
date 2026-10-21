<script lang="ts">
	// The "Overnight" card tab. ONE verdict — the premium/price read, the catalyst
	// read, and the intraday drift override are folded by the backend into a single
	// call (data.verdict) plus the ordered steps that produced it (data.verdict.steps).
	// No second opinion, no mode toggle: the card shows the one call and, beneath it,
	// the factors in sequence, then the premium tables as optional detail.
	import type { OvernightData } from '$lib/types';
	import { etTime } from '$lib/fmt';

	let { data }: { data: OvernightData } = $props();

	const v = $derived(data.verdict ?? { call: 'wait', confidence: 0, steps: [] });
	const CALL_LABEL: Record<string, string> = {
		sell_bear_call: 'Sell bear-call',
		sell_bull_put: 'Sell bull-put',
		wait: 'Wait',
		skip: 'Skip'
	};
	const callClass = $derived(
		v.call === 'sell_bear_call'
			? 'call--bear'
			: v.call === 'sell_bull_put'
				? 'call--bull'
				: v.call === 'skip'
					? 'call--skip'
					: 'call--wait'
	);
	const steps = $derived(v.steps ?? []);
	const STEP_LABEL: Record<string, string> = {
		setup: 'Setup',
		premium: 'Premium & tape',
		catalyst: 'Catalyst',
		intraday: 'Intraday'
	};

	const cat = $derived(data.catalyst ?? null);
	const OUTCOME_LABEL: Record<string, string> = {
		extended: 'extended ✓',
		held: 'held',
		faded: 'faded'
	};

	let showTables = $state(false); // premium detail collapsed by default — it's drill-down
	const strikes = $derived((data.strikes ?? []).map((s) => String(s)));
	const fmt = (n: number | undefined) => (n == null ? '—' : n.toFixed(2));
</script>

<div class="ovn">
	<!-- 1. THE call (single verdict) -->
	<div class="call {callClass}">
		<span>
			{#if data.as_of}<span class="call-time" title="when this call was last computed">{etTime(data.as_of)}</span>{/if}
			<span class="call-label">{CALL_LABEL[v.call] ?? v.call}</span>
		</span>
		<span class="call-conf">confidence {v.confidence}%</span>
	</div>

	<!-- 2. Thesis -->
	{#if data.thesis}
		<p class="thesis">{data.thesis}</p>
	{/if}

	<!-- 3. How we got there — the factors, in order -->
	{#if steps.length}
		<ol class="steps">
			{#each steps as s, i (i)}
				<li>
					<span class="step-tag">{STEP_LABEL[s.step] ?? s.step}</span>
					<span class="step-detail">{s.detail}</span>
				</li>
			{/each}
		</ol>
	{/if}

	<!-- Catalyst's realized outcome (today), if graded -->
	{#if cat?.outcome}
		<div class="chips">
			<span class="chip chip--outcome">today: {OUTCOME_LABEL[cat.outcome] ?? cat.outcome}</span>
		</div>
	{/if}

	<!-- 4. Optional drill-down: baseline vs live premiums -->
	{#if strikes.length}
		<button type="button" class="drill" onclick={() => (showTables = !showTables)}>
			{showTables ? '▾ hide' : '▸ show'} premium detail
		</button>
		{#if showTables}
			<div class="tables">
				<table>
					<caption>Last night (baseline)</caption>
					<thead><tr><th>strike</th><th>mid</th></tr></thead>
					<tbody>
						{#each strikes as k (k)}
							<tr><td>{k}</td><td>{fmt(data.baseline?.[k])}</td></tr>
						{/each}
					</tbody>
				</table>
				<table>
					<caption>Live</caption>
					<thead><tr><th>strike</th><th>mid</th></tr></thead>
					<tbody>
						{#each strikes as k (k)}
							<tr><td>{k}</td><td>{fmt(data.latest?.[k])}</td></tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}

	{#if cat?.provider}
		<div class="asof">catalyst read via {cat.provider}{data.as_of ? ` · as of ${data.as_of}` : ''}</div>
	{:else if data.as_of}
		<div class="asof">as of {data.as_of}</div>
	{/if}
</div>

<style>
	.ovn {
		margin-top: 0.25rem;
	}
	.call {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.75rem;
		border-radius: 0.5rem;
		border: 1px solid;
		padding: 0.5rem 0.75rem;
	}
	.call-label {
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.01em;
	}
	.call-time {
		margin-right: 0.45rem;
		font-size: 0.72rem;
		font-weight: 600;
		opacity: 0.7;
	}
	.call-conf {
		font-size: 0.72rem;
		opacity: 0.8;
	}
	.call--bear {
		border-color: rgba(244, 63, 94, 0.5);
		background: rgba(244, 63, 94, 0.08);
		color: #fecdd3;
	}
	.call--bull {
		border-color: rgba(16, 185, 129, 0.5);
		background: rgba(16, 185, 129, 0.08);
		color: #a7f3d0;
	}
	.call--skip {
		border-color: rgba(245, 158, 11, 0.45);
		background: rgba(245, 158, 11, 0.07);
		color: #fcd34d;
	}
	.call--wait {
		border-color: rgba(100, 116, 139, 0.4);
		background: rgba(100, 116, 139, 0.08);
		color: #cbd5e1;
	}
	.thesis {
		margin-top: 0.55rem;
		font-size: 0.85rem;
		line-height: 1.35rem;
		color: #e2e8f0;
	}
	.steps {
		margin: 0.6rem 0 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.steps li {
		display: grid;
		grid-template-columns: 6.5rem 1fr;
		gap: 0.6rem;
		align-items: baseline;
		font-size: 0.78rem;
		line-height: 1.3rem;
		padding-left: 0.6rem;
		border-left: 2px solid rgba(100, 116, 139, 0.35);
	}
	.step-tag {
		font-size: 0.62rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: #818cf8;
	}
	.step-detail {
		color: #cbd5e1;
	}
	.chips {
		margin-top: 0.5rem;
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
	}
	.chip {
		font-size: 0.65rem;
		color: #cbd5e1;
		background: rgba(100, 116, 139, 0.18);
		border-radius: 0.3rem;
		padding: 0.1rem 0.4rem;
	}
	.chip--outcome {
		background: rgba(16, 185, 129, 0.15);
		color: #a7f3d0;
	}
	.drill {
		margin-top: 0.6rem;
		border: none;
		background: transparent;
		padding: 0;
		font-size: 0.72rem;
		color: #60a5fa;
		cursor: pointer;
	}
	.tables {
		margin-top: 0.4rem;
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.75rem;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.72rem;
	}
	caption {
		text-align: left;
		color: #94a3b8;
		padding-bottom: 0.2rem;
	}
	th,
	td {
		text-align: right;
		padding: 0.1rem 0.3rem;
		border-bottom: 1px solid rgba(100, 116, 139, 0.15);
	}
	th:first-child,
	td:first-child {
		text-align: left;
		color: #94a3b8;
	}
	.asof {
		margin-top: 0.5rem;
		font-size: 0.65rem;
		color: #64748b;
	}
</style>
