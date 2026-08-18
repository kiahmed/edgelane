<script lang="ts">
	// Expiration pin picker: lists the chain's expirations from
	// /simmer/expirations/{symbol} with DTE, in-window and engine-recommended
	// markers. Picking one PINS it on the watchlist row (client RLS write in
	// the parent); "Auto" clears the pin so the engine picks again.
	import Modal from './Modal.svelte';
	import { getJSON } from '$lib/api';
	import type { ExpirationsResponse } from '$lib/types';

	let {
		symbol,
		current = null,
		open = false,
		onpick,
		onclose
	}: {
		symbol: string;
		current?: string | null;
		open?: boolean;
		onpick: (expiration: string | null) => void;
		onclose: () => void;
	} = $props();

	let resp = $state<ExpirationsResponse | null>(null);
	let error = $state<string | null>(null);
	let loading = $state(false);

	$effect(() => {
		if (open && symbol) {
			loading = true;
			resp = null;
			error = null;
			getJSON<ExpirationsResponse>(`/simmer/expirations/${encodeURIComponent(symbol)}`)
				.then((r) => (resp = r))
				.catch((e) => (error = e instanceof Error ? e.message : String(e)))
				.finally(() => (loading = false));
		}
	});
</script>

<Modal {open} title="Pin expiration — {symbol}" {onclose}>
	{#if loading}
		<p class="text-sm text-slate-400">Loading expirations…</p>
	{:else if error}
		<p class="text-sm text-rose-300">{error}</p>
	{:else if resp}
		<p class="mb-2 text-[0.72rem] text-slate-500">
			Engine DTE window: {resp.dte_window[0]}–{resp.dte_window[1]} days. Pinning outside it will
			veto on the DTE gate.
		</p>
		<div class="max-h-72 space-y-1 overflow-y-auto">
			<button
				class="btn w-full text-left"
				class:btn-primary={current === null}
				type="button"
				onclick={() => onpick(null)}
			>
				Auto — let the engine pick each sweep
			</button>
			{#each resp.expirations as e (e.date)}
				<button
					class="btn flex w-full items-center gap-2 text-left"
					class:btn-primary={current === e.date}
					type="button"
					onclick={() => onpick(e.date)}
				>
					<span class="font-mono">{e.date}</span>
					<span class="text-slate-400">{e.dte} DTE</span>
					{#if e.recommended}<span class="pill badge-healthy">engine pick</span>{/if}
					{#if !e.in_window}<span class="pill badge-thin">outside window</span>{/if}
				</button>
			{/each}
		</div>
	{/if}
</Modal>
