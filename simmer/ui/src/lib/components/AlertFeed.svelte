<script lang="ts">
	// In-app alert feed (/simmer/alerts). One row per state TRANSITION (the
	// watcher fires once, with hysteresis); ack is a POST the backend applies
	// to the user's own rows. Payload carries the full component breakdown, so
	// "why did this fire" needs no recompute — surfaced via the title tooltip
	// for now.
	import { getJSON, postJSON } from '$lib/api';
	import { ago, money, scoreClass, structureName } from '$lib/fmt';
	import { toast } from '$lib/stores/toast.svelte';
	import type { AlertsResponse, SimmerAlert } from '$lib/types';

	let alerts = $state<SimmerAlert[]>([]);
	let error = $state<string | null>(null);
	let unackedOnly = $state(true);

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
				<div class="flex items-center gap-2 text-sm" title={JSON.stringify(a.payload, null, 2)}>
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
					<span class="ml-auto font-mono text-xs {scoreClass(a.score)}">{money(a.score, 0)}</span>
					<span class="text-[0.65rem] text-slate-600">{ago(a.created_at)}</span>
					{#if !a.acknowledged}
						<button class="btn" type="button" title="Acknowledge" onclick={() => ack(a)}>ack</button>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>
