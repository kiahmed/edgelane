<script lang="ts">
	// Per-user settings. Presets first (Conservative / Balanced / Aggressive);
	// the Advanced drawer holds the bounded knobs — bounds rendered from
	// /simmer/config user_clamps, enforcement server-side. Gate toggles are
	// ONLY the toggleable tier (locked gates never appear as controls; the
	// backend would 422 them anyway). Active overrides also surface on the
	// ReadinessCard, not just here.
	import { settingsStore, PRESETS, type Preset } from '$lib/stores/settings.svelte';
	import { toast } from '$lib/stores/toast.svelte';
	import { humanizeReason } from '$lib/fmt';

	let advancedOpen = $state(false);

	// Draft knob values (strings for inputs; committed on Save).
	let draft = $state<Record<string, string>>({});
	let draftStructures = $state<string[]>([]);
	let draftGates = $state<Record<string, boolean>>({});
	let draftStrictness = $state('balanced');
	let draftNotify = $state(false);

	const KNOBS: Array<{ key: string; label: string; step: string }> = [
		{ key: 'min_score', label: 'Min score to alert', step: '1' },
		{ key: 'min_iv_percentile', label: 'Min IV percentile', step: '1' },
		{ key: 'min_dte', label: 'Min DTE', step: '1' },
		{ key: 'max_dte', label: 'Max DTE', step: '1' },
		{ key: 'short_delta_min', label: 'Short delta min', step: '0.01' },
		{ key: 'short_delta_max', label: 'Short delta max', step: '0.01' }
	];

	const s = $derived(settingsStore.settings);
	const effective = $derived((k: string) => {
		const v = (s as Record<string, unknown>)[k];
		return v == null ? '' : String(v);
	});

	// Seed drafts whenever the loaded settings change.
	$effect(() => {
		const cur = settingsStore.settings;
		const d: Record<string, string> = {};
		for (const { key } of KNOBS) {
			const v = (cur as Record<string, unknown>)[key];
			if (v != null) d[key] = String(v);
		}
		draft = d;
		draftStructures = cur.structures_enabled ?? [...settingsStore.structures];
		draftGates = { ...(cur.gate_overrides ?? {}) };
		draftStrictness = cur.regime_strictness ?? 'balanced';
		draftNotify = cur.notify_email ?? false;
	});

	function clampHint(key: string): string {
		const c = settingsStore.clamps[key];
		return c ? `${c[0]}–${c[1]}` : '';
	}

	async function applyPreset(p: Preset) {
		const err = await settingsStore.applyPreset(p);
		toast(err ?? `${p} preset applied`);
	}

	function toggleStructure(name: string) {
		draftStructures = draftStructures.includes(name)
			? draftStructures.filter((x) => x !== name)
			: [...draftStructures, name];
	}

	async function saveAdvanced() {
		const patch: Record<string, unknown> = {};
		for (const { key } of KNOBS) {
			const raw = draft[key];
			if (raw != null && raw !== '') {
				const n = Number(raw);
				if (!Number.isFinite(n)) {
					toast(`${key} must be a number`);
					return;
				}
				patch[key] = n;
			}
		}
		if (!draftStructures.length) {
			toast('enable at least one structure');
			return;
		}
		patch.structures_enabled = draftStructures;
		patch.regime_strictness = draftStrictness;
		patch.notify_email = draftNotify;
		// Only toggleable gates ever leave the client; locked gates have no
		// control here at all.
		const overrides: Record<string, boolean> = {};
		for (const g of settingsStore.toggleableGates) {
			if (draftGates[g] === false) overrides[g] = false;
		}
		patch.gate_overrides = overrides;
		const err = await settingsStore.save(patch);
		toast(err ?? 'settings saved (server-clamped values applied)');
	}

	const presetLabel: Record<Preset, string> = {
		conservative: 'Conservative',
		balanced: 'Balanced',
		aggressive: 'Aggressive'
	};
	const presetBlurb: Record<Preset, string> = {
		conservative: 'Fewer, safer alerts. Farther strikes, richer vol required.',
		balanced: 'The engine defaults. Where calibration has the most data.',
		aggressive: 'More alerts, closer strikes. Still inside the research clamps.'
	};
</script>

<div class="space-y-4">
	<!-- Presets — front and centre -->
	<div class="card p-4">
		<h2 class="mb-3 text-[0.7rem] font-bold tracking-wider text-slate-400 uppercase">Risk preset</h2>
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
						score ≥ {PRESETS[preset].min_score} · IV%≥{PRESETS[preset].min_iv_percentile} ·
						Δ {PRESETS[preset].short_delta_min}–{PRESETS[preset].short_delta_max}
					</div>
				</button>
			{/each}
		</div>
	</div>

	<!-- Advanced drawer -->
	<div class="card p-4">
		<button
			type="button"
			class="flex w-full items-center justify-between border-none bg-transparent p-0 text-left"
			onclick={() => (advancedOpen = !advancedOpen)}
		>
			<h2 class="text-[0.7rem] font-bold tracking-wider text-slate-400 uppercase">Advanced</h2>
			<span class="text-slate-500">{advancedOpen ? '▾' : '▸'}</span>
		</button>

		{#if advancedOpen}
			<div class="mt-3 space-y-4">
				<!-- Bounded knobs -->
				<div class="grid gap-3 sm:grid-cols-2">
					{#each KNOBS as k (k.key)}
						<div>
							<label class="text-[0.68rem] tracking-wider text-slate-500 uppercase" for="knob-{k.key}">
								{k.label}
								{#if clampHint(k.key)}
									<span class="text-slate-600" title="Hard clamp — the server will not accept values outside this band">
										[{clampHint(k.key)}]</span
									>
								{/if}
							</label>
							<input
								id="knob-{k.key}"
								class="gate-input"
								type="number"
								step={k.step}
								min={settingsStore.clamps[k.key]?.[0]}
								max={settingsStore.clamps[k.key]?.[1]}
								placeholder={effective(k.key) || 'engine default'}
								bind:value={draft[k.key]}
							/>
						</div>
					{/each}
				</div>

				<!-- Structures -->
				<div class="settings-section">
					<h3>Structures traded</h3>
					{#each settingsStore.structures as st (st)}
						<label class="toggle-row cursor-pointer">
							<span>{humanizeReason(st)}</span>
							<input
								type="checkbox"
								checked={draftStructures.includes(st)}
								onchange={() => toggleStructure(st)}
							/>
						</label>
					{/each}
				</div>

				<!-- Regime strictness -->
				<div class="settings-section">
					<h3>Regime-gate strictness</h3>
					<div class="radio-row">
						{#each ['relaxed', 'balanced', 'strict'] as rs (rs)}
							<label>
								<input type="radio" name="strictness" value={rs} bind:group={draftStrictness} />
								{rs}
							</label>
						{/each}
					</div>
				</div>

				<!-- Toggleable gate overrides ONLY -->
				<div class="settings-section">
					<h3>Soft-signal vetoes</h3>
					<p class="mb-1 text-[0.68rem] text-slate-500">
						Only these are toggleable. Safety gates (catalyst lockout, liquidity, friction, chain
						sanity, macro validity) are locked by design.
					</p>
					{#each settingsStore.toggleableGates as g (g)}
						<label class="toggle-row cursor-pointer">
							<span>{humanizeReason(g)}</span>
							<input
								type="checkbox"
								checked={draftGates[g] !== false}
								onchange={(e) => {
									draftGates = { ...draftGates, [g]: (e.currentTarget as HTMLInputElement).checked };
								}}
							/>
						</label>
					{/each}
					{#if settingsStore.activeGateOverrides.length}
						<div class="override-warn mt-2">
							<span>⚠</span>
							<span>
								Off: {settingsStore.activeGateOverrides.map(humanizeReason).join(', ')} — every
								readiness card will carry this warning.
							</span>
						</div>
					{/if}
				</div>

				<!-- Notifications -->
				<div class="settings-section">
					<h3>Notifications</h3>
					<label class="toggle-row cursor-pointer">
						<span>Email on readiness alerts</span>
						<input type="checkbox" bind:checked={draftNotify} />
					</label>
				</div>

				<button class="btn btn-primary" type="button" disabled={settingsStore.saving} onclick={saveAdvanced}>
					{settingsStore.saving ? 'Saving…' : 'Save advanced settings'}
				</button>
			</div>
		{/if}
	</div>
</div>
