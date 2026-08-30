<script lang="ts">
	// The one genuinely new gating concept vs Matrix: signed in, but not
	// entitled — GET /simmer/status answered 403 (the backend checks
	// profiles.tools_enabled server-side). A proper screen instead of a bare
	// 403 (docs/simmer.md, "Product gating").
	import { asset } from '$app/paths';
	import { auth } from '$lib/stores/auth.svelte';
	import ContactModal from './ContactModal.svelte';

	// This screen is the likeliest place someone needs support — they are signed
	// in and stuck — so "contact the operator" has to actually do something.
	let contactOpen = $state(false);
</script>

<div class="gate-mask show">
	<div class="gate-card">
		<div class="glow-orb"></div>
		<div class="gate-logo mb-4">
			<img class="mark" src={asset('/assets/simmer-logo.png')} alt="" aria-hidden="true" />
			<div>
				<div class="gate-eyebrow">Facades</div>
				<div class="text-lg font-black tracking-tight text-slate-100">SIMMER</div>
			</div>
		</div>

		<h2 class="mb-2 text-base font-bold text-slate-100">Simmer isn't enabled for this account</h2>
		<p class="text-sm leading-relaxed text-slate-400">
			You're signed in as <span class="text-slate-200">{auth.user?.email ?? 'unknown'}</span>, but
			the Simmer readiness engine isn't part of your plan yet.
			<button
				class="gate-link cursor-pointer border-none bg-transparent p-0"
				type="button"
				onclick={() => (contactOpen = true)}
			>
				Contact the operator
			</button>
			to get it switched on — access is per-account.
		</p>

		<button class="gate-btn mt-6" type="button" onclick={() => auth.signOut()}>
			Sign in with a different account
		</button>
	</div>
</div>

<ContactModal open={contactOpen} onclose={() => (contactOpen = false)} />
