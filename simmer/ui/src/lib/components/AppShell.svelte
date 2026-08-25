<script lang="ts">
	// Header + nav + profile menu, Matrix layout vocabulary (neon wordmark,
	// pills cluster wraps internally, avatar pinned right).
	import type { Snippet } from 'svelte';
	import StatusPills from './StatusPills.svelte';
	import SocialShare from './SocialShare.svelte';
	import { CAPTURE_ID } from '$lib/social';
	import { auth } from '$lib/stores/auth.svelte';
	import { watchlist } from '$lib/stores/watchlist.svelte';
	import { page } from '$app/state';
	import { resolve } from '$app/paths';

	let { children }: { children: Snippet } = $props();

	let menuOpen = $state(false);
	const initial = $derived((auth.user?.email ?? 'D')[0]?.toUpperCase() ?? 'D');
	const path = $derived(page.url.pathname);
</script>

<svelte:window
	onclick={(e) => {
		if (menuOpen && !(e.target as HTMLElement)?.closest?.('.profile-cluster')) menuOpen = false;
	}}
/>

<div class="mx-auto max-w-6xl space-y-4 p-4 md:p-6">
	<header class="mb-2 flex items-start gap-3">
		<div class="flex-shrink-0">
			<h1 class="neon text-2xl font-black tracking-tight md:text-3xl">
				EDGELANE <span class="text-emerald-400">SIMMER</span>
			</h1>
			<p class="mt-0.5 text-[13px] text-slate-200 italic">
				Sell premium only when the market pays you to. Most days it doesn't.
			</p>
		</div>
		<div class="ml-auto min-w-0">
			<StatusPills status={watchlist.status} backendUp={watchlist.backendUp} />
		</div>
		<div class="profile-cluster relative flex-shrink-0">
			<button
				class="avatar-btn"
				type="button"
				title={auth.devBypass ? 'dev-bypass (no auth configured)' : (auth.user?.email ?? '')}
				onclick={() => (menuOpen = !menuOpen)}
			>
				{auth.devBypass ? '⚙' : initial}
			</button>
			{#if menuOpen}
				<div class="settings-panel">
					<h3>Account</h3>
					<p class="mb-3 text-sm text-slate-300">
						{auth.devBypass ? 'Dev-bypass mode — no deploy config baked.' : (auth.user?.email ?? '')}
					</p>
					{#if !auth.devBypass}
						<button class="btn w-full" type="button" onclick={() => auth.signOut()}>Sign out</button>
					{/if}
				</div>
			{/if}
		</div>
	</header>

	<nav class="flex items-center border-b border-slate-700/50">
		<a class="tab-btn" class:active={path === resolve('/')} href={resolve('/')}>Dashboard</a>
		<a class="tab-btn" class:active={path.startsWith(resolve('/settings'))} href={resolve('/settings')}>
			Settings
		</a>
		<!-- Social cluster, pinned right (Matrix layout) -->
		<div class="ml-auto pb-1.5">
			<SocialShare />
		</div>
	</nav>

	<!-- Capture target for Share: the board only (no header/nav/footer), so the
	     snapshot is the center content with no empty side margins. -->
	<div id={CAPTURE_ID}>
		{@render children()}
	</div>

	<footer class="pt-4 text-center text-[0.68rem] text-slate-600">
		Research tool, not investment advice. Engine verdicts are calibrated on paper outcomes.
	</footer>
</div>
