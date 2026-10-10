<script lang="ts">
	// About · Contact for visitors who aren't in the app yet. AuthGate and
	// ProductGate render INSTEAD of AppShell, so AppShell's footer — the only
	// place these links lived — never shows on the sign-in / access screens.
	// Same copy and modals as AppShell's footer; the modals already stack above
	// .gate-mask (app.css: .modal-mask z-index 80 vs 70).
	import ContactModal from './ContactModal.svelte';
	import AboutModal from './AboutModal.svelte';

	let contactOpen = $state(false);
	let aboutOpen = $state(false);
	const year = new Date().getFullYear();
</script>

<div class="mt-5 border-t border-slate-800 pt-4 text-center text-[0.68rem] text-slate-600">
	<span>&copy; Facades Simmer {year}</span>
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
</div>

<AboutModal open={aboutOpen} onclose={() => (aboutOpen = false)} />
<ContactModal open={contactOpen} onclose={() => (contactOpen = false)} />
