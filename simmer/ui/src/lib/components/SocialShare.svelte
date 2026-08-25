<script lang="ts">
	// Social cluster for the nav row (Matrix vocabulary): X + LinkedIn chips that
	// deep-link to the product pages, and a Share button that snapshots the board
	// center and copies it for a post on the chosen channel. Everything is driven
	// by the backend config (loadSocialConfig → /simmer/config), so URLs/visibility
	// change via simmer_tickers.json without a frontend rebuild. data-no-capture so
	// the cluster never appears in its own screenshot.
	import { onMount } from 'svelte';
	import { loadSocialConfig, shareBoard, SOCIAL_DEFAULT, type SocialConfig } from '$lib/social';
	import { safeHref } from '$lib/fmt';
	import { toast } from '$lib/stores/toast.svelte';

	let cfg = $state<SocialConfig>(SOCIAL_DEFAULT);
	let menuOpen = $state(false);
	let busy = $state(false);

	// Chips appear only once real product URLs are configured (no links to
	// unregistered handles); Share is independent of the product accounts.
	// safeHref rejects any non-http(s) scheme — cheap insurance against an
	// operator typo in the admin config (a bad URL just hides its chip).
	const xHref = $derived(cfg.enabled ? safeHref(cfg.x_url) : null);
	const linkedInHref = $derived(cfg.enabled ? safeHref(cfg.linkedin_url) : null);
	const showX = $derived(!!xHref);
	const showLinkedIn = $derived(!!linkedInHref);
	const showShare = $derived(cfg.share_enabled);

	onMount(async () => {
		cfg = await loadSocialConfig();
	});

	async function share(channel: 'x' | 'linkedin') {
		menuOpen = false;
		if (busy) return;
		busy = true;
		toast('Capturing the board…', 1500);
		try {
			// Same safeHref guard as the profile chips — the composer URL is
			// admin config too, and it ends up in window.open(). A rejected scheme
			// yields '' so shareBoard still captures the image, just opens nothing.
			const composer = safeHref(channel === 'x' ? cfg.x_share_url : cfg.linkedin_share_url);
			const r = await shareBoard(composer ?? '');
			const where = channel === 'x' ? 'X' : 'LinkedIn';
			if (r === 'copied') toast(`Image copied — paste (Ctrl/Cmd+V) into your ${where} post`, 4000);
			else if (r === 'downloaded') toast(`Image saved — attach it to your ${where} post`, 4000);
			else toast('Could not capture the board', 3000);
		} catch {
			toast('Share failed — try again', 3000);
		} finally {
			busy = false;
		}
	}
</script>

<svelte:window
	onclick={(e) => {
		if (menuOpen && !(e.target as HTMLElement)?.closest?.('.social-cluster')) menuOpen = false;
	}}
/>

{#if showX || showLinkedIn || showShare}
<div class="social-cluster flex items-center gap-1.5" data-no-capture>
	{#if showX}
	<a
		class="social-chip"
		href={xHref}
		target="_blank"
		rel="noopener noreferrer"
		title={`${cfg.handle || 'EdgeLane Simmer'} on X`}
		aria-label="EdgeLane Simmer on X"
	>
		<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
			<path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
		</svg>
	</a>
	{/if}
	{#if showLinkedIn}
	<a
		class="social-chip"
		href={linkedInHref}
		target="_blank"
		rel="noopener noreferrer"
		title="EdgeLane Simmer on LinkedIn"
		aria-label="EdgeLane Simmer on LinkedIn"
	>
		<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
			<path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.063 2.063 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" />
		</svg>
	</a>
	{/if}

	{#if showShare}
	<div class="relative">
		<button
			class="share-btn"
			type="button"
			disabled={busy}
			aria-haspopup="menu"
			aria-expanded={menuOpen}
			onclick={() => (menuOpen = !menuOpen)}
		>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
				stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<circle cx="18" cy="5" r="3" /><circle cx="6" cy="12" r="3" /><circle cx="18" cy="19" r="3" />
				<line x1="8.59" y1="13.51" x2="15.42" y2="17.49" /><line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
			</svg>
			<span>Share</span>
		</button>
		{#if menuOpen}
			<div class="share-menu" role="menu">
				<button class="share-menu-item" type="button" role="menuitem" onclick={() => share('x')}>
					Post to X
				</button>
				<button class="share-menu-item" type="button" role="menuitem" onclick={() => share('linkedin')}>
					Post to LinkedIn
				</button>
			</div>
		{/if}
	</div>
	{/if}
</div>
{/if}

<style>
	.social-chip {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2rem;
		height: 2rem;
		border-radius: 0.5rem;
		border: 1px solid rgb(51 65 85 / 0.7); /* slate-700 */
		background: rgb(30 41 59 / 0.5); /* slate-800 */
		color: rgb(148 163 184); /* slate-400 */
		transition: color 0.15s, border-color 0.15s, background 0.15s;
	}
	.social-chip:hover {
		color: rgb(226 232 240); /* slate-200 */
		border-color: rgb(100 116 139);
		background: rgb(51 65 85 / 0.6);
	}
	.social-chip svg {
		width: 1rem;
		height: 1rem;
	}
	.share-btn {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
		height: 2rem;
		padding: 0 0.75rem;
		border-radius: 0.5rem;
		border: 1px solid rgb(51 65 85 / 0.7);
		background: rgb(30 41 59 / 0.5);
		color: rgb(203 213 225);
		font-size: 0.8rem;
		font-weight: 600;
		transition: color 0.15s, border-color 0.15s, background 0.15s;
	}
	.share-btn:hover:not(:disabled) {
		color: rgb(226 232 240);
		border-color: rgb(100 116 139);
		background: rgb(51 65 85 / 0.6);
	}
	.share-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.share-btn svg {
		width: 0.9rem;
		height: 0.9rem;
	}
	.share-menu {
		position: absolute;
		right: 0;
		top: calc(100% + 0.35rem);
		z-index: 30;
		min-width: 10rem;
		border-radius: 0.5rem;
		border: 1px solid rgb(51 65 85 / 0.8);
		background: rgb(15 23 42 / 0.98); /* slate-900 */
		padding: 0.25rem;
		box-shadow: 0 8px 24px rgb(0 0 0 / 0.4);
	}
	.share-menu-item {
		display: block;
		width: 100%;
		text-align: left;
		padding: 0.45rem 0.6rem;
		border-radius: 0.35rem;
		font-size: 0.8rem;
		color: rgb(203 213 225);
	}
	.share-menu-item:hover {
		background: rgb(51 65 85 / 0.6);
		color: rgb(226 232 240);
	}
</style>
