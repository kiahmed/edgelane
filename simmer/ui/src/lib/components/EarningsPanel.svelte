<script lang="ts">
	// Earnings mode — auto-appears in a card ONLY when the expiry is in an
	// earnings window. Shows the cached directional bias and a toggle that
	// re-scores with / without the earnings fold so you can compare. The
	// backend caches the (expensive) bias, so toggling only re-scores.
	import { getEarnings, analyzeEarnings, errorMessage } from '$lib/api';
	import type { EarningsResp, ReadinessEnvelope } from '$lib/types';

	let { symbol, expiration, baseScore }: {
		symbol: string;
		expiration: string | null;
		baseScore: number;
	} = $props();

	let resp = $state<EarningsResp | null>(null);
	let loading = $state(true);
	let err = $state<string | null>(null);
	let on = $state(true); // earnings mode default ON (matches the sweep)
	let altScore = $state<number | null>(null);
	let switching = $state(false);

	// Fetch once per symbol (the panel only mounts when the card is expanded).
	$effect(() => {
		const sym = symbol;
		loading = true;
		err = null;
		getEarnings<EarningsResp>(sym)
			.then((r) => {
				resp = r;
			})
			.catch((e) => {
				err = errorMessage(e);
			})
			.finally(() => {
				loading = false;
			});
	});

	async function toggle() {
		on = !on;
		switching = true;
		try {
			const env = await analyzeEarnings<ReadinessEnvelope>(symbol, expiration, on);
			altScore = env.score;
		} catch (e) {
			err = errorMessage(e);
		} finally {
			switching = false;
		}
	}

	const DIR: Record<string, string> = { bullish: '▲', bearish: '▼', neutral: '◆' };
	const shownScore = $derived(Math.round(altScore ?? baseScore));
</script>

{#if !loading && resp?.in_window}
	<div class="earn">
		<div class="earn-head">
			<span class="earn-title">Earnings mode</span>
			{#if resp.earnings_date}<span class="earn-date">{resp.earnings_date}</span>{/if}
			<button
				class="earn-toggle"
				class:on={on}
				type="button"
				onclick={toggle}
				disabled={switching}
				aria-pressed={on}
				title="Fold the earnings bias into the score, or compare without it"
				>{switching ? '…' : on ? 'ON' : 'OFF'}</button>
		</div>

		{#if resp.bias}
			<div class="earn-row">
				<span class="dir dir--{resp.bias.direction}">{DIR[resp.bias.direction] ?? '◆'} {resp.bias.direction}</span>
				<span class="muted">conf {Math.round((resp.bias.confidence ?? 0) * 100)}%</span>
				<span class="tag {resp.bias.go ? 'go' : 'nogo'}">{resp.bias.go ? 'go' : 'no-go'}</span>
				<span class="score">score {on ? 'with' : 'without'}: <b>{shownScore}</b></span>
			</div>
			{#if resp.bias.rationale}<div class="earn-why">{resp.bias.rationale}</div>{/if}
			<div class="earn-note">News-driven bias — not a veto. Aligned + go lifts the score (±15), mixed stays flagged.</div>
		{:else}
			<div class="earn-why muted">No bias computed yet for this window.</div>
		{/if}

		{#if err}<div class="earn-err">{err}</div>{/if}
	</div>
{/if}

<style>
	.earn {
		margin-top: 0.6rem;
		border: 1px solid rgba(217, 119, 6, 0.35);
		border-left: 3px solid #d97706;
		border-radius: 0.5rem;
		background: rgba(217, 119, 6, 0.06);
		padding: 0.5rem 0.7rem;
		font-size: 0.75rem;
	}
	.earn-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.earn-title {
		font-weight: 700;
		color: #d97706;
		letter-spacing: 0.01em;
	}
	.earn-date {
		font-size: 0.68rem;
		color: rgb(148, 163, 184);
	}
	.earn-toggle {
		margin-left: auto;
		min-width: 2.6rem;
		border-radius: 0.4rem;
		border: 1px solid rgba(100, 116, 139, 0.5);
		background: rgba(100, 116, 139, 0.15);
		color: rgb(148, 163, 184);
		font-weight: 800;
		font-size: 0.66rem;
		padding: 0.12rem 0.5rem;
		cursor: pointer;
	}
	.earn-toggle.on {
		border-color: rgba(217, 119, 6, 0.6);
		background: rgba(217, 119, 6, 0.2);
		color: #f59e0b;
	}
	.earn-toggle:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.earn-row {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		margin-top: 0.4rem;
		flex-wrap: wrap;
	}
	.dir {
		font-weight: 700;
		text-transform: capitalize;
	}
	.dir--bullish {
		color: #34d399;
	}
	.dir--bearish {
		color: #f87171;
	}
	.dir--neutral {
		color: #94a3b8;
	}
	.muted {
		color: rgb(148, 163, 184);
	}
	.tag {
		font-size: 0.62rem;
		font-weight: 700;
		text-transform: uppercase;
		border-radius: 999px;
		padding: 0.05rem 0.4rem;
	}
	.tag.go {
		color: #052e16;
		background: #86efac;
	}
	.tag.nogo {
		color: rgb(148, 163, 184);
		background: rgba(100, 116, 139, 0.25);
	}
	.score {
		margin-left: auto;
		color: rgb(203, 213, 225);
	}
	.earn-why {
		margin-top: 0.35rem;
		color: rgb(203, 213, 225);
		line-height: 1.4;
	}
	.earn-note {
		margin-top: 0.35rem;
		font-size: 0.66rem;
		color: rgb(148, 163, 184);
	}
	.earn-err {
		margin-top: 0.35rem;
		color: #f87171;
		font-size: 0.68rem;
	}
</style>
