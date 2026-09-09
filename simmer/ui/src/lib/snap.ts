// `?snap=1` render mode — the crop the `simmer-snap` screenshot service targets
// (see soljet-postiz ops/simmer, selector `[data-snap="card"]`). When on, the
// app renders a single symbol's card with no nav/toasts/social chrome and a
// per-symbol og:image, and the card links through to the full board. Reading the
// flag is isolated here so the normal UI paths stay untouched behind the flag.

export interface SnapState {
	/** true when `?snap=1` is present. */
	snap: boolean;
	/** the requested symbol (validated `[A-Z.\-]{1,10}`), or null. */
	symbol: string | null;
}

const SYMBOL_RE = /^[A-Z.\-]{1,10}$/;

export function readSnap(search?: string): SnapState {
	// SSR / prerender has no window; default to the normal (non-snap) UI.
	const qs = search ?? (typeof window !== 'undefined' ? window.location.search : '');
	const params = new URLSearchParams(qs);
	const snap = params.get('snap') === '1';
	const raw = (params.get('symbol') ?? '').toUpperCase();
	const symbol = SYMBOL_RE.test(raw) ? raw : null;
	return { snap, symbol };
}

/** Public URL of a symbol's card the social embed shows (screenshotted upstream). */
export function snapOgImage(symbol: string, base = 'https://simmer.facades.trade'): string {
	return `${base}/og/${encodeURIComponent(symbol)}.png`;
}

/** Click-through target from a snapped card back to the full board. */
export function snapHref(symbol: string): string {
	return `/?symbol=${encodeURIComponent(symbol)}`;
}
