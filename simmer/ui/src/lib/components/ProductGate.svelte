<script lang="ts">
	// The one genuinely new gating concept vs Matrix: signed in, but not
	// entitled — GET /simmer/status answered 403 (the backend checks
	// profiles.tools_enabled server-side). A proper screen instead of a bare
	// 403 (docs/simmer.md, "Product gating").
	import { auth } from '$lib/stores/auth.svelte';
</script>

<div class="gate-mask show">
	<div class="gate-card">
		<div class="glow-orb"></div>
		<div class="gate-logo mb-4">
			<div class="mark">
				<svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
					<path d="M4 17c3-1 5-6 8-6s5 4 8 2" stroke="#022c22" stroke-width="2.4" stroke-linecap="round" />
				</svg>
			</div>
			<div>
				<div class="gate-eyebrow">EdgeLane</div>
				<div class="text-lg font-black tracking-tight text-slate-100">SIMMER</div>
			</div>
		</div>

		<h2 class="mb-2 text-base font-bold text-slate-100">Simmer isn't enabled for this account</h2>
		<p class="text-sm leading-relaxed text-slate-400">
			You're signed in as <span class="text-slate-200">{auth.user?.email ?? 'unknown'}</span>, but
			the Simmer readiness engine isn't part of your plan yet. Contact the operator to get it
			switched on — access is per-account.
		</p>

		<button class="gate-btn mt-6" type="button" onclick={() => auth.signOut()}>
			Sign in with a different account
		</button>
	</div>
</div>
