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
	import { readSnap, snapOgImage, snapHref } from '$lib/snap';
	import { page } from '$app/state';

	let selected = $state<string | null>(null);

	// `?snap=1&symbol=SYM`: render just that symbol's card for the screenshot
	// service. The layout has already stripped the nav/toasts/social chrome.
	const snap = readSnap();
	const snapRow = $derived.by(() => {
		if (!snap.snap) return null;
		const rows = watchlist.rows.filter((r) => r.readiness);
		return (snap.symbol ? rows.find((r) => r.symbol === snap.symbol) : rows[0]) ?? null;
	});

	// `?symbol=SYM` (non-snap) deep link — the click-through from a social post.
	// Focus that ticker and open its card. One-shot so a later "show all" or a
	// manual selection isn't overridden on re-render.
	const deepSymbol = $derived(
		snap.snap ? null : (page.url.searchParams.get('symbol') || '').toUpperCase() || null
	);
	let deepApplied = false;
	$effect(() => {
		if (!deepApplied && deepSymbol) {
			selected = deepSymbol;
			deepApplied = true;
		}
	});

	const withEnv = $derived(watchlist.rows.filter((r) => r.readiness));
	const readyCount = $derived(withEnv.filter((r) => r.readiness!.decision === 'ready').length);

	// Filter/sort bar — revealed in the full-list view (after "show all"); a
	// focused single card is never filtered. Overnight gap comes from the row's
	// RQ overlay; readiness status/confidence from the sweep envelope.
	let search = $state('');
	let statusFilter = $state<'all' | 'ready' | 'watch' | 'avoid' | 'vetoed'>('all');
	let minConf = $state(0);
	let sortBy = $state<'expiry' | 'gap' | 'confidence'>('expiry');
	let rqOnly = $state(false);
	const ovnGap = (r: WatchlistRow) =>
		Math.abs(Number((r.overrides?.overnight as { gap?: number } | undefined)?.gap ?? 0));
	// Best confidence across the two tabs, on a 0–100 scale, for sorting.
	const confVal = (r: WatchlistRow) =>
		Math.max((r.readiness?.confidence ?? 0) * 100, r.overnight_conf ?? 0);

	// Nearest expiry first; rows without an evaluated expiry sink to the bottom.
	// Expirations are ISO YYYY-MM-DD, so a string compare is chronological.
	const shown = $derived.by(() => {
		let list = selected
			? watchlist.rows.filter((r) => r.symbol === selected)
			: watchlist.rows.slice();
		if (!selected) {
			const q = search.trim().toUpperCase();
			list = list.filter((r) => {
				if (q && !r.symbol.toUpperCase().includes(q)) return false;
				const env = r.readiness;
				if (statusFilter !== 'all' && env?.decision !== statusFilter) return false;
				// Min confidence (percent). Pass if EITHER confidence clears it: the
				// readiness confidence (0–1 fraction, shown ×100%) or the overnight
				// catalyst confidence (already 0–100). A row with both passes on
				// whichever matches; a row with only one is judged on that one.
				if (minConf > 0) {
					const rc = (env?.confidence ?? -1) * 100; // −100 ⇒ no readiness conf
					const oc = r.overnight_conf ?? -1; // −1 ⇒ no overnight conf
					if (rc < minConf && oc < minConf) return false;
				}
				if (rqOnly && !r.overrides?.overnight) return false;
				return true;
			});
		}
		return list.sort((a, b) => {
			if (!selected && sortBy === 'gap') return ovnGap(b) - ovnGap(a);
			if (!selected && sortBy === 'confidence') return confVal(b) - confVal(a);
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

<svelte:head>
	{#if snap.snap}
		{@const sym = snapRow?.symbol ?? snap.symbol ?? 'Simmer'}
		<title>{sym} — Simmer readiness</title>
		<meta property="og:title" content={`${sym} — Simmer readiness`} />
		<meta
			property="og:description"
			content="Refusal-first premium-selling readiness. Most days the right trade is no trade."
		/>
		{#if snapRow}<meta property="og:image" content={snapOgImage(sym)} />{/if}
	{/if}
</svelte:head>

{#if snap.snap}
	{@const sym = snapRow?.symbol ?? snap.symbol ?? 'Simmer'}
	<!-- Click-through back to the full board; the crop targets the wrapper. -->
	<a href={snapHref(sym)} class="block max-w-xl">
		{#if snapRow?.readiness}
			<ReadinessCard
				env={snapRow.readiness}
				readyBand={bands.ready}
				watchBand={bands.watch}
				activeOverrides={settingsStore.activeGateOverrides}
				pinnedExpiration={snapRow.expiration}
			/>
		{:else}
			<div class="card p-6 text-center text-sm text-slate-500">
				{sym}: no analysis yet.
			</div>
		{/if}
	</a>
{:else}
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

			{#if !selected && withEnv.length}
				<div
					class="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-slate-700/40 bg-slate-800/30 px-3 py-2 text-xs">
					<input
						class="gate-input mt-0!"
						style="margin-top:0;flex:1 1 9rem;min-width:7rem"
						placeholder="search ticker"
						bind:value={search}
						autocomplete="off"
						spellcheck="false"
					/>
					<select
						class="gate-input mt-0!"
						style="margin-top:0;width:auto;flex:0 0 auto"
						bind:value={statusFilter}>
						<option value="all">all status</option>
						<option value="ready">ready</option>
						<option value="watch">watch</option>
						<option value="avoid">avoid</option>
						<option value="vetoed">vetoed</option>
					</select>
					<label class="flex items-center gap-1.5 whitespace-nowrap text-slate-400">
						min&nbsp;conf&nbsp;%
						<input
							class="gate-input mt-0! text-center"
							style="margin-top:0;width:4rem"
							type="number"
							min="0"
							max="100"
							placeholder="0"
							bind:value={minConf}
						/>
					</label>
					<select
						class="gate-input mt-0!"
						style="margin-top:0;width:auto;flex:0 0 auto"
						bind:value={sortBy}>
						<option value="expiry">sort: expiry</option>
						<option value="gap">sort: gap ↓</option>
						<option value="confidence">sort: conf ↓</option>
					</select>
					<label class="flex items-center gap-1 whitespace-nowrap text-violet-300">
						<input type="checkbox" bind:checked={rqOnly} /> RQ
					</label>
					<span class="ml-auto whitespace-nowrap text-slate-500">{shown.length} shown</span>
				</div>
			{/if}
		{/if}

		{#each shown as row (row.id)}
			{#if row.readiness}
				<ReadinessCard
					env={row.readiness}
					readyBand={bands.ready}
					watchBand={bands.watch}
					activeOverrides={settingsStore.activeGateOverrides}
					pinnedExpiration={row.expiration}
					startExpanded={row.symbol === deepSymbol || row.symbol === selected}
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
{/if}
