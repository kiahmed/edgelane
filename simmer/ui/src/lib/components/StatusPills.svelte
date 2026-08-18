<script lang="ts">
	// Header pills — mirrors Matrix's renderStatus() vocabulary against
	// /simmer/status: watcher health, market hours, regime, last sweep.
	import { ago } from '$lib/fmt';
	import type { SimmerStatus } from '$lib/types';

	let {
		status,
		backendUp = true
	}: {
		status: SimmerStatus | null;
		backendUp?: boolean;
	} = $props();
</script>

<div class="flex flex-wrap items-center justify-end gap-2">
	{#if !backendUp}
		<span class="pill bg-rose-500/20 text-rose-200" title="Backend unreachable — check the tunnel or local server">
			<span class="dot bg-rose-400"></span>Backend Down
		</span>
	{:else if !status}
		<span class="pill bg-slate-700 text-slate-200"><span class="dot bg-slate-400"></span>Connecting</span>
	{:else}
		{#if status.market_open}
			<span class="pill bg-emerald-500/20 text-emerald-200" title="Regular NYSE session">
				<span class="dot dot-pulse bg-emerald-400"></span>Market Open
			</span>
		{:else}
			<span class="pill bg-amber-500/20 text-amber-200" title={status.market_reason ?? ''}>
				<span class="dot bg-amber-400"></span>Closed{status.market_reason ? ` · ${status.market_reason}` : ''}
			</span>
		{/if}
		{#if status.is_running}
			<span
				class="pill bg-emerald-500/20 text-emerald-200"
				title="Watcher loop sweeping · {status.watched_count} keys · sweep #{status.sweep_count}"
			>
				<span class="dot dot-pulse bg-emerald-400"></span>Watcher Live
			</span>
		{:else}
			<span class="pill bg-rose-500/20 text-rose-200" title={status.last_error ?? 'watcher loop not running'}>
				<span class="dot bg-rose-400"></span>Watcher Down
			</span>
		{/if}
		{#if status.regime?.state}
			<span class="pill bg-sky-500/20 text-sky-200" title="Market regime (put-side guard)">
				{String(status.regime.state).replace(/_/g, ' ')}
			</span>
		{/if}
		<span class="pill bg-slate-700 text-slate-300" title="Last watcher sweep">
			sweep {ago(status.last_sweep_at)}
		</span>
	{/if}
</div>
