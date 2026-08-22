<script lang="ts">
	// Earnings panel — appears only when the viewed expiry is in an earnings
	// window. Earnings is a FOLDED FACTOR, never a hard veto: the card's decision
	// already reflects the "consider" view (bias folded, a no-go held back). This
	// panel shows the bias and a toggle that switches the DISPLAYED verdict
	// between consider (earnings folded) and ignore (pure ex-earnings) — a pure
	// display swap using both verdicts already on the envelope (env.earnings /
	// env.earnings_alt). No re-analysis on toggle. The choice persists per symbol
	// in localStorage, so it survives a refresh.
	import { getEarnings, errorMessage } from '$lib/api';
	import type { EarningsResp, ReadinessEnvelope } from '$lib/types';

	let { env }: { env: ReadinessEnvelope } = $props();

	const symbol = $derived(env.symbol);
	const earn = $derived(env.earnings ?? null);
	const alt = $derived(env.earnings_alt ?? null);

	// Relevant only when earnings sits on/before the viewed expiry.
	const relevant = $derived(
		!!earn?.in_window &&
			!!earn.earnings_date &&
			!!env.expiration &&
			earn.earnings_date.slice(0, 10) <= env.expiration.slice(0, 10)
	);

	const KEY = $derived(`simmer_earnings_view_${symbol}`);
	// 'consider' shows the card's own decision; 'ignore' shows the alt verdict.
	let view = $state<'consider' | 'ignore'>('consider');
	$effect(() => {
		try {
			const v = localStorage.getItem(KEY);
			view = v === 'ignore' ? 'ignore' : 'consider';
		} catch {
			view = 'consider';
		}
	});

	function toggle() {
		view = view === 'consider' ? 'ignore' : 'consider';
		try {
			localStorage.setItem(KEY, view);
		} catch {
			/* storage unavailable — session-only */
		}
	}

	// The bias may not be computed yet at sweep time (cache cold). Fetch once to
	// populate it; the panel still renders from env in the meantime.
	let fetchedFor = '';
	let fetching = $state(false);
	let err = $state<string | null>(null);
	let fetchedBias = $state<EarningsResp['bias'] | null>(null);
	$effect(() => {
		const sym = symbol;
		if (!relevant || sym === fetchedFor) return;
		fetchedFor = sym;
		if (earn?.direction) return; // already have a bias on the envelope
		fetching = true;
		getEarnings<EarningsResp>(sym)
			.then((r) => {
				fetchedBias = r.bias;
			})
			.catch((e) => {
				err = errorMessage(e);
			})
			.finally(() => {
				fetching = false;
			});
	});

	const bias = $derived(earn?.direction ? earn : fetchedBias);
	const shownScore = $derived(
		view === 'ignore' && alt ? Math.round(alt.score) : Math.round(env.score)
	);
	const shownDecision = $derived(view === 'ignore' && alt ? alt.decision : env.decision);
	const DIR: Record<string, string> = { bullish: '▲', bearish: '▼', neutral: '◆' };
</script>

{#if relevant}
	<div class="earn">
		<div class="earn-head">
			<span class="earn-title">Earnings</span>
			{#if earn?.earnings_date}<span class="earn-date">{earn.earnings_date}</span>{/if}
			<span class="view-label">{view === 'consider' ? 'considered' : 'ignored'}</span>
			<button
				class="earn-toggle"
				class:on={view === 'consider'}
				type="button"
				onclick={toggle}
				aria-pressed={view === 'consider'}
				title="Switch between the earnings-considered decision and the pure ex-earnings one"
				>{view === 'consider' ? 'CONSIDER' : 'IGNORE'}</button>
		</div>

		{#if bias}
			<div class="earn-row">
				<span class="dir dir--{bias.direction}">{DIR[bias.direction] ?? '◆'} {bias.direction}</span>
				<span class="muted">conf {Math.round((bias.confidence ?? 0) * 100)}%</span>
				<span class="tag {bias.go ? 'go' : 'nogo'}">{bias.go ? 'go' : 'no-go'}</span>
				<span class="score">{shownDecision} · <b>{shownScore}</b></span>
			</div>

			{#if earn?.held_back}
				<div class="earn-flag hold">⚠ No-go read — held back below “ready” (earnings cuts both ways).</div>
			{:else if earn?.close_before_print}
				<div class="earn-flag close">↩ Run-up play — <b>close BEFORE the print</b>, never hold through earnings.</div>
			{/if}

			{#if bias.rationale}<div class="earn-why">{bias.rationale}</div>{/if}
			<div class="earn-note">
				Earnings is a folded factor, not a veto. Consider = bias in (go lifts ±15, no-go holds back);
				Ignore = pure ex-earnings decision.
			</div>
		{:else if fetching}
			<div class="earn-why muted">Analyzing earnings headlines…</div>
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
	}
	.earn-date {
		font-size: 0.68rem;
		color: rgb(148, 163, 184);
	}
	.view-label {
		font-size: 0.62rem;
		color: rgb(148, 163, 184);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.earn-toggle {
		margin-left: auto;
		min-width: 4.4rem;
		border-radius: 0.4rem;
		border: 1px solid rgba(100, 116, 139, 0.5);
		background: rgba(100, 116, 139, 0.15);
		color: rgb(148, 163, 184);
		font-weight: 800;
		font-size: 0.62rem;
		letter-spacing: 0.03em;
		padding: 0.12rem 0.5rem;
		cursor: pointer;
	}
	.earn-toggle.on {
		border-color: rgba(217, 119, 6, 0.6);
		background: rgba(217, 119, 6, 0.2);
		color: #f59e0b;
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
		text-transform: capitalize;
	}
	.earn-flag {
		margin-top: 0.4rem;
		font-size: 0.68rem;
		border-radius: 0.35rem;
		padding: 0.25rem 0.45rem;
	}
	.earn-flag.hold {
		color: #fca5a5;
		background: rgba(248, 113, 113, 0.1);
	}
	.earn-flag.close {
		color: #fcd34d;
		background: rgba(217, 119, 6, 0.12);
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
