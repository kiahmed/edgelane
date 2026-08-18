<script lang="ts">
	// Add box → single POST /simmer/watchlist (the backend validates AND writes;
	// write_model: "api"). Remove = DELETE, expiration-pin = PATCH — the store
	// owns those calls; server error details (including the dev-bypass 422)
	// surface verbatim as toasts. Rows show a compact readiness summary plus any
	// per-ticker overrides the row carries.
	import ExpiryPicker from './ExpiryPicker.svelte';
	import { watchlist } from '$lib/stores/watchlist.svelte';
	import { toast } from '$lib/stores/toast.svelte';
	import { decisionBadgeClass, DECISION_LABEL, money, scoreClass } from '$lib/fmt';
	import type { WatchlistRow } from '$lib/types';

	let {
		selected = null,
		onselect
	}: {
		selected?: string | null;
		onselect?: (row: WatchlistRow) => void;
	} = $props();

	let symbolInput = $state('');
	let adding = $state(false);
	let pickerRow = $state<WatchlistRow | null>(null);

	async function add(e: SubmitEvent) {
		e.preventDefault();
		if (!symbolInput.trim() || adding) return;
		adding = true;
		try {
			const err = await watchlist.add(symbolInput);
			if (err) toast(err);
			else {
				toast(`${symbolInput.trim().toUpperCase()} added`);
				symbolInput = '';
			}
		} finally {
			adding = false;
		}
	}

	async function remove(row: WatchlistRow) {
		const err = await watchlist.remove(row);
		toast(err ?? `${row.symbol} removed`);
	}

	/** Per-ticker override chips: `gate: false` reads "gate off"; anything else
	 *  shows key:value. The row-level `overrides` column is toggleable-tier
	 *  only (migration 0010), so this stays tiny. */
	function overrideChips(row: WatchlistRow): string[] {
		return Object.entries(row.overrides ?? {}).map(([k, v]) =>
			v === false ? `${k.replace(/_/g, ' ')} off` : `${k.replace(/_/g, ' ')}: ${v}`
		);
	}

	async function pin(expiration: string | null) {
		const row = pickerRow;
		pickerRow = null;
		if (!row) return;
		const err = await watchlist.pinExpiration(row, expiration);
		toast(err ?? (expiration ? `${row.symbol} pinned to ${expiration}` : `${row.symbol} back to auto`));
	}
</script>

<div class="card p-4">
	<div class="mb-2 flex items-baseline justify-between">
		<h2 class="text-[0.7rem] font-bold tracking-wider text-slate-400 uppercase">Watchlist</h2>
		<span class="text-[0.65rem] text-slate-600">{watchlist.rows.length} tickers</span>
	</div>

	<form class="mb-3 flex gap-2" onsubmit={add}>
		<input
			class="gate-input mt-0! flex-1"
			style="margin-top:0"
			placeholder="Add ticker (e.g. AMD)"
			bind:value={symbolInput}
			autocomplete="off"
			spellcheck="false"
		/>
		<button class="btn btn-primary" type="submit" disabled={adding || !symbolInput.trim()}>
			{adding ? 'Validating…' : 'Add'}
		</button>
	</form>

	{#if watchlist.error && !watchlist.rows.length}
		<p class="text-sm text-rose-300">{watchlist.error}</p>
	{:else if !watchlist.rows.length}
		<p class="text-sm text-slate-500">
			No tickers yet. Add one above — the engine validates the chain before it lands.
		</p>
	{/if}

	<div class="space-y-1">
		{#each watchlist.rows as row (row.id)}
			{@const env = row.readiness}
			<div
				class="flex w-full items-center gap-2 rounded-lg border px-2 py-1.5 text-left transition-colors {selected === row.symbol
					? 'border-emerald-500/40 bg-emerald-500/5'
					: 'border-transparent hover:border-slate-600/40'}"
			>
				<button
					class="flex min-w-0 flex-1 cursor-pointer items-center gap-2 border-none bg-transparent p-0 text-left"
					type="button"
					onclick={() => onselect?.(row)}
				>
					<span class="font-bold">{row.symbol}</span>
					{#if row.expiration}
						<span class="text-[0.65rem] text-sky-300" title="Expiration pinned">📌 {row.expiration}</span>
					{/if}
					{#if env}
						<span class="pill {decisionBadgeClass(env.decision)}">{DECISION_LABEL[env.decision] ?? env.decision}</span>
						<span class="font-mono text-xs {scoreClass(env.score)}">{money(env.score, 0)}</span>
					{:else}
						<span class="pill bg-slate-700 text-slate-400" title="No sweep result yet for this symbol">pending</span>
					{/if}
					{#each overrideChips(row) as chip (chip)}
						<span
							class="rounded bg-amber-500/10 px-1 text-[0.6rem] whitespace-nowrap text-amber-300/90"
							title="Per-ticker override on this watchlist row">⚙ {chip}</span>
					{/each}
				</button>
				<button
					class="btn"
					type="button"
					title="Pin a specific expiration"
					onclick={() => (pickerRow = row)}>exp</button
				>
				<button class="btn btn-danger" type="button" title="Remove from watchlist" onclick={() => remove(row)}>
					✕
				</button>
			</div>
		{/each}
	</div>
</div>

{#if pickerRow}
	<ExpiryPicker
		symbol={pickerRow.symbol}
		current={pickerRow.expiration}
		open={pickerRow !== null}
		onpick={pin}
		onclose={() => (pickerRow = null)}
	/>
{/if}
