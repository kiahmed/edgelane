<script lang="ts">
	// Consumer settings are intentionally TINY: a risk-appetite preset (an
	// alert-strictness bar over the SHARED engine's output) and notifications.
	// Engine behavior — DTE window, strike delta, structures, regime, soft
	// vetoes — is admin-global (simmer_tickers.json), never per user, so a
	// consumer can't alter what the engine finds/validates. Presets only filter
	// the already-analyzed result, and only tighten (never loosen) the floors.
	import { settingsStore, PRESETS, type Preset } from '$lib/stores/settings.svelte';
	import { toast } from '$lib/stores/toast.svelte';

	const s = $derived(settingsStore.settings);

	const presetLabel: Record<Preset, string> = {
		conservative: 'Conservative',
		balanced: 'Balanced',
		aggressive: 'Aggressive'
	};
	const presetBlurb: Record<Preset, string> = {
		conservative: 'Fewest, strictest alerts — only the richest, highest-conviction setups.',
		balanced: 'The engine default. Where calibration has the most data.',
		aggressive: 'More alerts — a lower bar on the same validated setups.'
	};

	async function applyPreset(p: Preset) {
		const err = await settingsStore.applyPreset(p);
		toast(err ?? `${presetLabel[p]} preset applied`);
	}

	async function toggleNotify(on: boolean) {
		const err = await settingsStore.save({ notify_email: on });
		toast(err ?? (on ? 'Email alerts on' : 'Email alerts off'));
	}
</script>

<div class="space-y-4">
	<!-- Risk preset — the only engine-adjacent control, and it only sets the alert bar -->
	<div class="card p-4">
		<h2 class="mb-1 text-[0.7rem] font-bold tracking-wider text-slate-400 uppercase">Risk preset</h2>
		<p class="mb-3 text-[0.72rem] text-slate-500">
			How strict your alert bar is on the engine's shared analysis. It filters results — it does
			not change what the engine finds or how it validates.
		</p>
		<div class="grid gap-3 sm:grid-cols-3">
			{#each Object.keys(PRESETS) as p (p)}
				{@const preset = p as Preset}
				<button
					type="button"
					class="rounded-xl border p-3 text-left transition-colors {s.risk_profile === preset
						? 'border-emerald-500/50 bg-emerald-500/10'
						: 'border-slate-700/50 hover:border-slate-500/60'}"
					disabled={settingsStore.saving}
					onclick={() => applyPreset(preset)}
				>
					<div class="mb-1 font-bold text-slate-100">{presetLabel[preset]}</div>
					<div class="text-[0.72rem] leading-snug text-slate-400">{presetBlurb[preset]}</div>
					<div class="mt-2 font-mono text-[0.65rem] text-slate-500">
						score ≥ {PRESETS[preset].min_score} · IV% ≥ {PRESETS[preset].min_iv_percentile}
					</div>
				</button>
			{/each}
		</div>
	</div>

	<!-- Notifications -->
	<div class="card p-4">
		<h2 class="mb-2 text-[0.7rem] font-bold tracking-wider text-slate-400 uppercase">Notifications</h2>
		<label class="toggle-row cursor-pointer">
			<span>Email on readiness alerts</span>
			<input
				type="checkbox"
				checked={s.notify_email ?? false}
				disabled={settingsStore.saving}
				onchange={(e) => toggleNotify((e.currentTarget as HTMLInputElement).checked)}
			/>
		</label>
	</div>

	<p class="px-1 text-[0.68rem] leading-relaxed text-slate-500">
		Engine behavior — DTE window, strike delta, structures, regime, and the safety gates — is set
		globally by the operator. It's the same validated engine for everyone; your settings only
		filter what you're alerted on.
	</p>
</div>
