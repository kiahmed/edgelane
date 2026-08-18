<script lang="ts">
	// Dashboard. Refusal-first: the lead line is "N of M vetoed", and every
	// card explains its gates before showing any score. Charts are a later
	// pass (stubs exist); nothing here pretends readiness it can't defend.
	import WatchlistManager from '$lib/components/WatchlistManager.svelte';
	import ReadinessCard from '$lib/components/ReadinessCard.svelte';
	import AlertFeed from '$lib/components/AlertFeed.svelte';
	import { watchlist } from '$lib/stores/watchlist.svelte';
	import { settingsStore } from '$lib/stores/settings.svelte';
	import type { WatchlistRow } from '$lib/types';

	let selected = $state<string | null>(null);

	const withEnv = $derived(watchlist.rows.filter((r) => r.readiness));
	const readyCount = $derived(withEnv.filter((r) => r.readiness!.decision === 'ready').length);
	// Nearest expiry first; rows without an evaluated expiry sink to the bottom.
	// Expirations are ISO YYYY-MM-DD, so a string compare is chronological.
	const shown = $derived.by(() => {
		const list = selected
			? watchlist.rows.filter((r) => r.symbol === selected)
			: watchlist.rows.slice();
		return list.sort((a, b) => {
			const ax = a.readiness?.expiration ?? null;
			const bx = b.readiness?.expiration ?? null;
			if (ax && bx) return ax < bx ? -1 : ax > bx ? 1 : 0;
			if (ax) return -1;
			if (bx) return 1;
			return 0;
		});
	});
	const bands = $derived({
		ready: settingsStore.config?.decision.ready ?? 70,
		watch: settingsStore.config?.decision.watch ?? 50
	});

	function select(row: WatchlistRow) {
		selected = selected === row.symbol ? null : row.symbol;
	}
</script>

<div class="grid gap-4 lg:grid-cols-[minmax(280px,1fr)_2fr]">
	<div class="space-y-4">
		<WatchlistManager {selected} onselect={select} />
		<AlertFeed />
	</div>

	<div class="space-y-2">
		{#if watchlist.rows.length}
			<div class="flex items-baseline gap-2 px-1 text-sm">
				{#if withEnv.length === 0}
					<span class="text-slate-500">Waiting for the first sweep…</span>
				{:else if readyCount === 0}
					<span class="font-bold text-slate-300">
						{withEnv.length - readyCount} of {withEnv.length} vetoed or below band
					</span>
					<span class="text-slate-500">— the engine's default answer is no. Each card says why.</span>
				{:else}
					<span class="font-bold text-emerald-300">{readyCount} ready</span>
					<span class="text-slate-500">· {withEnv.length - readyCount} held back</span>
				{/if}
				{#if selected}
					<button
						class="btn ml-auto"
						type="button"
						onclick={() => (selected = null)}>show all</button
					>
				{/if}
			</div>
		{/if}

		{#each shown as row (row.id)}
			{#if row.readiness}
				<ReadinessCard
					env={row.readiness}
					readyBand={bands.ready}
					watchBand={bands.watch}
					activeOverrides={settingsStore.activeGateOverrides}
					pinnedExpiration={row.expiration}
				/>
			{:else}
				<div class="card p-4 text-sm text-slate-500">
					{row.symbol}: <span class="text-amber-300/90">no analysis yet</span> — first sweep is
					still running (a freshly-added ticker is analyzed within a few minutes; off-hours it
					uses the last session's data).
				</div>
			{/if}
		{/each}

		{#if !watchlist.rows.length && watchlist.backendUp}
			<div class="card p-6 text-center text-sm text-slate-500">
				Add tickers on the left. Simmer evaluates each one continuously and refuses loudly — a
				quiet dashboard is the system working.
			</div>
		{/if}
	</div>
</div>
