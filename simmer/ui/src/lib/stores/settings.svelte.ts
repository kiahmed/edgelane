// Settings store. Reads via GET /simmer/settings (row + defaults) and
// /simmer/config (clamp bounds, gate lists, structures). ALL writes go through
// POST /simmer/settings — the backend clamps bounded knobs, 422s any
// locked-gate override, and accepts `risk_profile` alongside the knobs
// (server-side enforcement is authoritative; the browser never writes Supabase
// tables directly).
import { getJSON, postJSON, ApiError, errorMessage } from '../api';
import type { SettingsResponse, SimmerConfig, SimmerSettings } from '../types';

export type Preset = 'conservative' | 'balanced' | 'aggressive';

/** Preset knob values. `balanced` mirrors the migration-0010 column defaults;
 *  the other two shift every tunable knob in the obvious direction while
 *  staying inside the server clamps (USER_CLAMPS in simmer_config.py). */
// Presets are ALERT-STRICTNESS only — a post-filter on the shared engine's
// output (min score + min IV%). Engine behavior (DTE, delta band, structures,
// regime) is admin-global (simmer/simmer_tickers.json), never per user, so it is not
// part of a preset.
export const PRESETS: Record<Preset, SimmerSettings> = {
	conservative: {
		min_score: 80,
		min_iv_percentile: 50
	},
	balanced: {
		min_score: 70,
		min_iv_percentile: 40
	},
	aggressive: {
		min_score: 60,
		min_iv_percentile: 30
	}
};

class SettingsStore {
	settings = $state<SimmerSettings>({});
	defaults = $state<SettingsResponse['defaults'] | null>(null);
	config = $state<SimmerConfig | null>(null);
	loading = $state(false);
	saving = $state(false);
	error = $state<string | null>(null);

	get clamps(): Record<string, [number, number]> {
		return this.config?.user_clamps ?? {};
	}
	get toggleableGates(): string[] {
		return this.config?.toggleable_gates ?? [];
	}
	get lockedGates(): string[] {
		return this.config?.locked_gates ?? [];
	}
	get structures(): string[] {
		return this.config?.structures ?? ['bull_put', 'bear_call', 'iron_condor'];
	}
	/** gate name -> false means "veto switched OFF by the user" */
	get activeGateOverrides(): string[] {
		const o = this.settings.gate_overrides ?? {};
		return Object.keys(o).filter((k) => o[k] === false);
	}

	async load(): Promise<void> {
		this.loading = true;
		try {
			const [cfg, st] = await Promise.all([
				getJSON<SimmerConfig>('/simmer/config'),
				getJSON<SettingsResponse>('/simmer/settings')
			]);
			this.config = cfg;
			this.settings = st.settings ?? {};
			this.defaults = st.defaults ?? null;
			this.error = null;
		} catch (e) {
			this.error = errorMessage(e);
		} finally {
			this.loading = false;
		}
	}

	/** POST the knobs (server clamps + validates); returns error text or null.
	 *  The server echoes back the SANITIZED values — those, not the client's,
	 *  land in local state. */
	async save(patch: SimmerSettings): Promise<string | null> {
		this.saving = true;
		try {
			const res = await postJSON<{ settings: SimmerSettings; persisted: boolean }>(
				'/simmer/settings',
				patch
			);
			this.settings = { ...this.settings, ...res.settings };
			if (!res.persisted) return 'saved locally but persistence failed (Supabase unreachable)';
			return null;
		} catch (e) {
			if (e instanceof ApiError) return String(e.message);
			return errorMessage(e);
		} finally {
			this.saving = false;
		}
	}

	/** Apply a preset: ONE POST carrying risk_profile + its knob values (the
	 *  sanitizer accepts risk_profile now — no client-side table write). */
	async applyPreset(preset: Preset): Promise<string | null> {
		return this.save({ ...PRESETS[preset], risk_profile: preset });
	}
}

export const settingsStore = new SettingsStore();
