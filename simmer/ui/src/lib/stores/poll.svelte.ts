// Polling engine. 15s default (persisted in localStorage), refresh-on-focus —
// the Torque lesson: a background tab's timers are throttled, so on
// visibilitychange->visible we fire immediately instead of waiting a stale
// interval out.
import { POLL_INTERVAL_KEY, DEFAULT_POLL_MS } from '../config';

export function loadPollInterval(): number {
	try {
		const v = Number(localStorage.getItem(POLL_INTERVAL_KEY));
		if (Number.isFinite(v) && v >= 5_000 && v <= 300_000) return v;
	} catch {
		/* storage unavailable */
	}
	return DEFAULT_POLL_MS;
}

export function savePollInterval(ms: number): void {
	try {
		localStorage.setItem(POLL_INTERVAL_KEY, String(ms));
	} catch {
		/* storage unavailable */
	}
}

export class Poller {
	intervalMs = $state(DEFAULT_POLL_MS);
	running = $state(false);
	lastTick = $state<number | null>(null);

	#fn: (() => Promise<void> | void) | null = null;
	#timer: ReturnType<typeof setInterval> | undefined;
	#onVisible = () => {
		if (document.visibilityState === 'visible') void this.tick();
	};

	start(fn: () => Promise<void> | void): void {
		this.stop();
		this.#fn = fn;
		this.intervalMs = loadPollInterval();
		this.running = true;
		void this.tick();
		this.#timer = setInterval(() => void this.tick(), this.intervalMs);
		document.addEventListener('visibilitychange', this.#onVisible);
	}

	setInterval(ms: number): void {
		savePollInterval(ms);
		this.intervalMs = ms;
		if (this.running && this.#fn) {
			clearInterval(this.#timer);
			this.#timer = setInterval(() => void this.tick(), ms);
		}
	}

	async tick(): Promise<void> {
		if (!this.#fn) return;
		this.lastTick = Date.now();
		try {
			await this.#fn();
		} catch (e) {
			console.warn('[simmer] poll tick failed', e);
		}
	}

	stop(): void {
		clearInterval(this.#timer);
		document.removeEventListener('visibilitychange', this.#onVisible);
		this.running = false;
		this.#fn = null;
	}
}
