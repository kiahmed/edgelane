<script lang="ts">
	// App root: auth init → AuthGate (signed out) → ProductGate (no 'simmer'
	// entitlement) → AppShell + page. Polling for watchlist/status lives here
	// so every page shares one cadence (15s default, refresh-on-focus).
	import '../app.css';
	import type { Snippet } from 'svelte';
	import AuthGate from '$lib/components/AuthGate.svelte';
	import ProductGate from '$lib/components/ProductGate.svelte';
	import AppShell from '$lib/components/AppShell.svelte';
	import Toast from '$lib/components/Toast.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { resolveApiBasePointer } from '$lib/api';
	import { watchlist } from '$lib/stores/watchlist.svelte';
	import { settingsStore } from '$lib/stores/settings.svelte';
	import { Poller } from '$lib/stores/poll.svelte';

	let { children }: { children: Snippet } = $props();

	const poller = new Poller();

	$effect(() => {
		// Deployed builds: adopt the Edge Config pointer (self-heals a rotated
		// tunnel URL) BEFORE auth makes its first call. No-op on dev builds.
		void resolveApiBasePointer().finally(() => auth.init());
	});

	// Start/stop the poll loop off the full-access state (dev-bypass or signed
	// in AND entitled).
	$effect(() => {
		const active = auth.ready && auth.isFull && auth.hasSimmer;
		if (active) {
			poller.start(() => watchlist.refresh());
			void settingsStore.load();
			return () => poller.stop();
		}
	});
</script>

{#if !auth.ready}
	<div class="flex min-h-screen items-center justify-center text-slate-500">Loading…</div>
{:else if !auth.isFull}
	<AuthGate />
{:else if !auth.toolsKnown}
	<div class="flex min-h-screen items-center justify-center text-slate-500">Checking access…</div>
{:else if !auth.hasSimmer}
	<ProductGate />
{:else}
	<AppShell>
		{@render children()}
	</AppShell>
{/if}

<Toast />
