<script lang="ts">
	// Header + nav + profile menu, Matrix layout vocabulary (neon wordmark,
	// pills cluster wraps internally, avatar pinned right).
	import type { Snippet } from 'svelte';
	import StatusPills from './StatusPills.svelte';
	import SocialShare from './SocialShare.svelte';
	import ContactModal from './ContactModal.svelte';
	import AboutModal from './AboutModal.svelte';
	import { CAPTURE_ID } from '$lib/social';
	import { auth } from '$lib/stores/auth.svelte';
	import { watchlist } from '$lib/stores/watchlist.svelte';
	import { page } from '$app/state';
	import { asset, resolve } from '$app/paths';

	let { children }: { children: Snippet } = $props();

	let menuOpen = $state(false);
	let contactOpen = $state(false);
	let aboutOpen = $state(false);
	// Computed, not hardcoded — Matrix fills its footer year at runtime too.
	const year = new Date().getFullYear();
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
			<!-- Brand lockup — logomark + SIMMER + "by Facades" byline. Mirrors the
			     Matrix header treatment so the two products read as one family.
			     simmer-logo.png and every favicon are GENERATED from the master
			     art at simmer/ui/assets/facades_simmer_logo_mark.png — replace
			     that, then run `python3 tools/simmer_icons.py`. -->
			<div class="brand-lockup">
				<img
					class="logomark"
					src={asset('/assets/simmer-logo.png')}
					alt="Simmer"
					width="192"
					height="192"
				/>
				<div class="names">
					<h1 class="neon wordmark text-2xl tracking-tight text-emerald-400 md:text-3xl">SIMMER</h1>
					<div class="brand-byline">by Facades</div>
				</div>
			</div>
			<p class="mt-1 text-[13px] text-slate-200 italic">
				Cut the research friction. Simmer monitors the heat and alerts you when options
				premium is fully baked.
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
		<div class="mb-1 flex items-center justify-center gap-2 text-xs">
			<img
				src={asset('/assets/simmer-logo.png')}
				alt=""
				aria-hidden="true"
				class="h-4 w-4 opacity-90"
			/>
			<span>&copy; Facades Simmer {year}. All rights reserved.</span>
		</div>
		<!-- "paper outcomes" refers to how the engine grades its OWN past calls
		     (simulated, no real fills) — not to the market data, which is a live
		     production feed. The old wording read as though the data were fake. -->
		Research tool, not investment advice. Live market data; the engine&rsquo;s track record is
		self-graded on simulated outcomes, not real fills.
		<span class="mx-1.5 text-slate-700">·</span>
		<button
			class="cursor-pointer border-none bg-transparent p-0 text-[0.68rem] text-slate-500 underline underline-offset-2 hover:text-emerald-400"
			type="button"
			onclick={() => (aboutOpen = true)}
		>
			About
		</button>
		<span class="mx-1.5 text-slate-700">·</span>
		<button
			class="cursor-pointer border-none bg-transparent p-0 text-[0.68rem] text-slate-500 underline underline-offset-2 hover:text-emerald-400"
			type="button"
			onclick={() => (contactOpen = true)}
		>
			Contact us
		</button>
	</footer>
</div>

<AboutModal open={aboutOpen} onclose={() => (aboutOpen = false)} />
<ContactModal open={contactOpen} onclose={() => (contactOpen = false)} />
