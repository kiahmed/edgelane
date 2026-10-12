<script lang="ts">
	// The "Overnight" card tab (docs/simmer.md → "Overnight movers"). Decision-
	// FIRST: the big line is the call — sell bear-call / sell bull-put / wait /
	// skip — then the one-line thesis and two supporting lines. The baseline-vs-
	// live premium tables are an OPTIONAL, collapsed drill-down, never the
	// headline. The sampling/smoothing that produced the call lives on the
	// backend; the user never assembles it.
	import type { OvernightData } from '$lib/types';

	let { data }: { data: OvernightData } = $props();

	const v = $derived(data.verdict ?? { call: 'wait', premium_trend: null, underlying_trend: null, confidence: 0, reasons: [] });

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
	const dirPct = $derived(data.gap != null ? `${(Math.abs(data.gap) * 100).toFixed(1)}%` : '');
	const crowdLabel = $derived(
		data.crowd === 'sector_wide' || data.crowd === 'with_market'
			? 'sector-wide (peers moved too — more durable)'
			: data.crowd === 'lone' || data.crowd === 'against_market'
				? 'lone move (peers did not — idiosyncratic)'
				: 'mixed peer signal'
	);
	const premiumLabel = $derived(
		v.premium_trend ? `premiums ${v.premium_trend}` : 'premiums — waiting for the open'
	);

	let showTables = $state(false);
	const strikes = $derived((data.strikes ?? []).map((s) => String(s)));
	const fmt = (n: number | undefined) => (n == null ? '—' : n.toFixed(2));
</script>

<div class="ovn">
	<!-- 1. The call -->
	<div class="call {callClass}">
		<span class="call-label">{CALL_LABEL[v.call] ?? v.call}</span>
		<span class="call-conf">confidence {v.confidence}%</span>
	</div>

	<!-- 2. Thesis -->
	{#if data.thesis}
		<p class="thesis">{data.thesis}</p>
	{/if}

	<!-- 3. Two supporting lines -->
	<div class="support">
		<div>↕ {data.dir === 'down' ? 'Gapped down' : 'Gapped up'} {dirPct} · {premiumLabel}</div>
		<div>👥 {crowdLabel}</div>
	</div>
	{#if v.reasons?.length}
		<div class="why">{v.reasons[0]}</div>
	{/if}

	<!-- 4. Optional drill-down: baseline vs live premiums (collapsed) -->
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

	{#if data.as_of}
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
	.support {
		margin-top: 0.5rem;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: 0.76rem;
		color: #94a3b8;
	}
	.why {
		margin-top: 0.35rem;
		font-size: 0.72rem;
		color: #64748b;
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
