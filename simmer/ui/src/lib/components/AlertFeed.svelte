<script lang="ts">
	// In-app alert feed (/simmer/alerts). One row per state TRANSITION (the
	// watcher fires once, with hysteresis); ack is a POST the backend applies
	// to the user's own rows. Payload carries the full component breakdown, so
	// "why did this fire" needs no recompute — surfaced via the title tooltip
	// Detail lives in a click-through dialog (AlertDetailModal), not a title
	// tooltip — the payload is the raw engine envelope and dumping it as JSON on
	// hover was unreadable at exactly the moment a decision gets made.
	import { getJSON, postJSON } from '$lib/api';
	import AlertDetailModal from './AlertDetailModal.svelte';
	import { ago, money, scoreClass, structureName } from '$lib/fmt';
	import { toast } from '$lib/stores/toast.svelte';
	import type { AlertsResponse, SimmerAlert } from '$lib/types';

	let alerts = $state<SimmerAlert[]>([]);
	let error = $state<string | null>(null);
	let unackedOnly = $state(true);
	let selected = $state<SimmerAlert | null>(null);

	export async function refresh(): Promise<void> {
		try {
			const r = await getJSON<AlertsResponse>(
				`/simmer/alerts?limit=50${unackedOnly ? '&unacknowledged_only=true' : ''}`
			);
			alerts = r.alerts ?? [];
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	$effect(() => {
		void unackedOnly;
		void refresh();
	});

	async function ack(a: SimmerAlert) {
		try {
			await postJSON('/simmer/alerts/ack', { ids: [a.id] });
			alerts = alerts.filter((x) => x.id !== a.id);
		} catch (e) {
			toast(`ack failed: ${e instanceof Error ? e.message : e}`);
		}
	}

	const stateClass = (s: string) =>
		s === 'ready' ? 'badge-healthy' : s === 'cooling' ? 'badge-thin' : 'badge-broken';

	// An earnings-window "ready" is a run-up play — flag it so it's never mistaken
	// for a plain hold-through sell.
	const isEarningsRunup = (a: SimmerAlert) =>
		!!(a.payload?.earnings as { in_window?: boolean } | undefined)?.in_window;
</script>

<div class="card p-4">
	<div class="mb-2 flex items-baseline justify-between">
		<h2 class="text-[0.7rem] font-bold tracking-wider text-slate-400 uppercase">Alerts</h2>
		<label class="flex items-center gap-1.5 text-[0.7rem] text-slate-400">
			<input type="checkbox" bind:checked={unackedOnly} />
			unread only
		</label>
	</div>

	{#if error}
		<p class="text-sm text-rose-300">{error}</p>
	{:else if !alerts.length}
		<p class="text-sm text-slate-500">
			Nothing yet. Alerts fire once per readiness transition — silence is the normal state.
		</p>
	{:else}
		<div class="space-y-1.5">
			{#each alerts as a (a.id)}
				<button
					class="alert-row flex w-full cursor-pointer items-center gap-2 text-left text-sm"
					type="button"
					title="Click to see what this alert means and what to do"
					onclick={() => (selected = a)}
				>
					<span class="pill {stateClass(a.state)}">{a.state}</span>
					<span class="font-bold">{a.symbol}</span>
					<span class="text-[0.7rem] text-slate-400">{a.expiration}</span>
					{#if a.structure}
						<span class="text-[0.7rem] text-slate-400">{structureName(a.structure)}</span>
					{/if}
					{#if isEarningsRunup(a)}
						<span
							class="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[0.62rem] font-semibold text-amber-300"
							title="Earnings run-up play — sell the drift, CLOSE BEFORE THE PRINT. Never hold through earnings."
						>
							⚡ earnings · close before print
						</span>
					{/if}
					<!-- Snapshot at fire time. The card beside it shows the CURRENT score,
					     which drifts away within minutes — an alert that fired at 70 sitting
					     next to a card reading 55 is the engine working, not a mismatch. -->
					<span
						class="ml-auto font-mono text-xs {scoreClass(a.score)}"
						title="Score when this alert fired — the card shows the current score, which moves"
						>{money(a.score, 0)}</span
					>
					<span class="text-[0.6rem] text-slate-600">at fire</span>
					<span class="text-[0.65rem] text-slate-600">{ago(a.created_at)}</span>
					{#if !a.acknowledged}
						<!-- stopPropagation: ack must not also open the detail dialog -->
						<span
							class="btn"
							role="button"
							tabindex="0"
							title="Acknowledge"
							onclick={(e) => {
								e.stopPropagation();
								ack(a);
							}}
							onkeydown={(e) => {
								if (e.key === 'Enter' || e.key === ' ') {
									e.preventDefault();
									e.stopPropagation();
									ack(a);
								}
							}}>ack</span
						>
					{/if}
				</button>
			{/each}
		</div>
	{/if}
</div>

<AlertDetailModal alert={selected} onclose={() => (selected = null)} />
