<script lang="ts">
	// What one alert means, and what to do about it.
	//
	// Replaces dumping `JSON.stringify(payload)` into a title tooltip. The
	// payload is the full engine envelope — dozens of raw floats — which told a
	// reader nothing at the moment they most needed a decision. Everything here
	// is derived from that same payload; nothing new is fetched.
	import Modal from './Modal.svelte';
	import { etDateTime, money, prob, structureName } from '$lib/fmt';
	import type { SimmerAlert } from '$lib/types';

	let {
		alert = null,
		onclose
	}: { alert?: SimmerAlert | null; onclose?: () => void } = $props();

	const p = $derived((alert?.payload ?? {}) as Record<string, unknown>);
	const num = (v: unknown): number | null =>
		typeof v === 'number' && Number.isFinite(v) ? v : null;

	const strikes = $derived(p.strikes as Record<string, unknown> | undefined);
	const earnings = $derived(p.earnings as { in_window?: boolean } | undefined);
	const isRunup = $derived(!!earnings?.in_window);

	// The exit plan is fixed BEFORE entry — same three rules the About modal and
	// the engine docs state. Shown here because an alert is exactly the moment
	// someone is deciding to put the trade on.
	const EXITS = [
		['Take profit', 'Close at 50% of max profit — do not hold for the last few cents.'],
		['Time stop', 'Manage or close at 21 DTE, whatever the P&L is doing.'],
		['Stop loss', 'Cut at 2× the credit you took in.']
	];

	function vertical(side: 'short' | 'long'): number | null {
		const v = strikes?.[side];
		return num(v);
	}
</script>

<Modal
	open={!!alert}
	title={alert ? `${alert.symbol} — ${alert.state.toUpperCase()} alert` : 'Alert'}
	{onclose}
>
	{#if alert}
		<div class="doc-prose">
			<!-- The one-line answer, before any numbers. -->
			<p class="lede">
				{#if alert.state === 'ready'}
					The engine says <strong class="text-emerald-300">{alert.symbol}</strong> was
					conditioned to sell premium against on
					<strong>{alert.expiration}</strong>{alert.structure
						? ` as a ${structureName(alert.structure).toLowerCase()}`
						: ''}.
				{:else}
					<strong>{alert.symbol}</strong> moved to <strong>{alert.state}</strong> for
					{alert.expiration}.
				{/if}
			</p>

			{#if isRunup}
				<div class="alert-warn">
					<strong>⚡ Earnings run-up — close before the print.</strong>
					This is a play on the drift into earnings, not a hold-through. Holding across the
					announcement is a different trade with a different risk profile.
				</div>
			{/if}

			<h4>The numbers</h4>
			<div class="kv-grid">
				<span>Score</span><span class="font-mono">{money(alert.score, 0)} / 100</span>
				<span>Expiration</span><span class="font-mono">{alert.expiration}</span>
				{#if alert.structure}
					<span>Structure</span><span>{structureName(alert.structure)}</span>
				{/if}
				{#if vertical('short') != null}
					<span>Short strike</span><span class="font-mono">{vertical('short')}</span>
				{/if}
				{#if vertical('long') != null}
					<span>Long strike</span><span class="font-mono">{vertical('long')}</span>
				{/if}
				{#if num(p.credit_fill) != null}
					<span>Credit (achievable)</span>
					<span class="font-mono text-emerald-200">{money(num(p.credit_fill))}</span>
				{/if}
				{#if num(p.max_loss) != null}
					<span>Max loss</span><span class="font-mono">{money(num(p.max_loss))}</span>
				{/if}
				{#if num(p.pop_breakeven) != null}
					<span>Chance of profit</span><span class="font-mono">{prob(num(p.pop_breakeven))}</span>
				{/if}
				<span>Fired</span><span class="font-mono">{etDateTime(alert.created_at)}</span>
			</div>

			<h4>Before you act</h4>
			<ul>
				<li>
					<strong>This is a snapshot, not a standing recommendation.</strong> It was true when it
					fired — re-check the card before entering, because liquidity and IV move.
				</li>
				<li>
					<strong>Credit shown is the achievable fill</strong>, already discounted from the quoted
					mid. If you can't get near it, the edge this was scored on isn't there.
				</li>
				<li>
					<strong>Size it yourself.</strong> The score ranks edge, not position size. Max loss above
					is per contract.
				</li>
			</ul>

			<h4>Exit plan — decide now, not later</h4>
			<ul>
				{#each EXITS as [name, rule]}
					<li><strong>{name}.</strong> {rule}</li>
				{/each}
			</ul>

			<p class="text-[11px] text-slate-500">
				Research tool, not investment advice. The engine's record is self-graded on simulated
				outcomes, not real fills.
			</p>
		</div>
	{/if}
</Modal>

<style>
	.lede {
		font-size: 0.88rem !important;
		color: #cbd5e1;
	}
	.alert-warn {
		margin: 0.6rem 0;
		border-left: 3px solid rgba(251, 191, 36, 0.7);
		background: rgba(251, 191, 36, 0.08);
		padding: 0.55rem 0.7rem;
		border-radius: 0 6px 6px 0;
		font-size: 0.78rem;
		line-height: 1.5;
		color: #fde68a;
	}
	.kv-grid {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 0.25rem 1rem;
		font-size: 0.78rem;
		margin: 0.35rem 0 0.2rem;
	}
	.kv-grid > :nth-child(odd) {
		color: #94a3b8;
	}
	.kv-grid > :nth-child(even) {
		color: #e2e8f0;
		text-align: right;
	}
</style>
