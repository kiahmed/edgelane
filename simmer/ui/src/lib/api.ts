// Backend API client — the ONLY network surface the browser talks to.
//
// DATA-PLANE RULE (architecture decision, 2026-08): the middle-tier API is the
// data plane AND the identity surface. The browser never contacts Supabase —
// not for reads, not for writes, not even for sign-in. Identity flows through
// the backend's /auth/* proxy endpoints (app/routes/auth_proxy.py), which hand
// the client a JWT + refresh token; every other call carries that JWT as a
// bearer and the backend enforces entitlements + validation server-side.
//
// api_base resolution (ported from Matrix, minus the Supabase pointer — that
// pointer was a PostgREST read and is gone with the no-Supabase rule):
//
//   sync precedence:
//     baked global (deployed — AUTHORITATIVE, wins over everything)
//     > ?api= (sticky) > localStorage > same-origin > http://127.0.0.1:8789
//   The ?api=/localStorage override is honored ONLY on dev builds (no baked
//   global). On a deployed build a baked base cannot be overridden — see the
//   SECURITY note on resolveApiBase.
//
// Deploys MUST bake window.__EDGELANE_API_BASE__ — the named tunnel's permanent
// hostname (edge.facades.trade). There is no runtime pointer: the URL no longer
// rotates, so there is nothing to self-heal.
import { readGlobals, isDevBypass, API_BASE_KEY, ADMIN_TOKEN_KEY } from './config';

const strip = (s: string): string => s.replace(/\/+$/, '');

export interface ApiSources {
	/** current ?api= query param */
	queryApi?: string | null;
	/** localStorage 'edgelane_api_base' (set by a past ?api=) */
	stored?: string | null;
	/** window.__EDGELANE_API_BASE__ (deploy-baked; stable tunnel/custom domain) */
	baked?: string | null;
	/** page origin when served over http(s) */
	origin?: string | null;
}

/** Pure sync resolution — Matrix's `_resolveApiBase`, injectable for tests. */
export function resolveApiBase(src: ApiSources): string {
	// SECURITY: a baked API base means this is a real (deployed) build — it is
	// authoritative and CANNOT be overridden by ?api= or a stored value. Without
	// this, `https://app/?api=https://evil.tld` repoints the legitimate UI and
	// exfiltrates the password typed into the login gate and every JWT after.
	if (src.baked) return strip(src.baked);
	// Dev builds only (no baked config): the ?api= / stored override is allowed
	// so a developer can point the local UI at any backend.
	if (src.queryApi) return strip(src.queryApi);
	if (src.stored) return strip(src.stored);
	// The Vite dev server is NOT the API — same-origin there 404s every call.
	if (src.origin && !/:5173$/.test(src.origin)) return strip(src.origin);
	return 'http://127.0.0.1:8789';
}

export interface AuthHeaderOpts {
	accessToken?: string | null;
}

/** Bearer JWT when signed in, nothing otherwise.
 *  (Simmer v1 has no anonymous teaser session.) */
export function authHeaders(opts: AuthHeaderOpts): Record<string, string> {
	if (opts.accessToken) return { Authorization: `Bearer ${opts.accessToken}` };
	return {};
}

/** Dev-bypass admin passthrough: append `?token=<admin>` so a local operator
 *  can exercise a backend with AUTH_ENABLED=true from the bypassed UI. Only
 *  ever applied in dev-bypass mode (see request()). */
export function withAdminToken(url: string, adminToken: string | null | undefined): string {
	if (!adminToken) return url;
	return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(adminToken);
}

// ── Browser-bound state ─────────────────────────────────────────────────────
let API_BASE: string | null = null;
let tokenProvider: () => string | null = () => null;
/** Returns true if the auth layer refreshed the session (retry the request). */
let unauthorizedHandler: () => Promise<boolean> = async () => false;

/** auth.svelte.ts registers its session-token getter here — keeps api.ts free
 *  of store imports (and unit-testable in node). */
export function setAuthTokenProvider(fn: () => string | null): void {
	tokenProvider = fn;
}

/** auth.svelte.ts registers its refresh-once handler here: on a 401 from any
 *  non-/auth/ endpoint, request() invokes it and replays the call once with
 *  the fresh token. If it returns false the 401 propagates (auth signs out). */
export function setUnauthorizedHandler(fn: () => Promise<boolean>): void {
	unauthorizedHandler = fn;
}

function browserSources(): ApiSources {
	const g = readGlobals();
	let queryApi: string | null = null;
	let stored: string | null = null;
	try {
		const url = new URL(location.href);
		// A build is "dev" only when it's a Vite dev server (isDevBypass →
		// import.meta.env), never "has no baked apiBase" — that older check
		// flagged deployed builds as dev and reopened the ?api= override hole.
		const devBuild = isDevBypass(g);   // overrides allowed only in true dev
		queryApi = devBuild ? url.searchParams.get('api') : null;
		if (queryApi) {
			try {
				localStorage.setItem(API_BASE_KEY, queryApi);
			} catch {
				/* storage unavailable */
			}
		}
		// Dev convenience, mirroring the sticky `?api=` pattern: `?token=` stores
		// the admin token once and is immediately scrubbed from the URL/history
		// (avoids the DevTools paste-protection dance entirely). Dev-only escape
		// hatch — real users sign in; the backend treats the token as the
		// server-owner bypass either way.
		const qtok = devBuild ? url.searchParams.get('token') : null;
		if (qtok) {
			try {
				localStorage.setItem(ADMIN_TOKEN_KEY, qtok);
			} catch {
				/* storage unavailable */
			}
			url.searchParams.delete('token');
			try {
				history.replaceState(null, '', url.toString());
			} catch {
				/* history unavailable */
			}
		}
	} catch {
		/* no location (SSR) */
	}
	try {
		stored = isDevBypass(g) ? localStorage.getItem(API_BASE_KEY) : null;
	} catch {
		/* storage unavailable */
	}
	const origin =
		typeof location !== 'undefined' &&
		(location.protocol === 'http:' || location.protocol === 'https:') &&
		location.host
			? `${location.protocol}//${location.host}`
			: null;
	return { queryApi, stored, baked: g.apiBase, origin };
}

export function apiBase(): string {
	if (API_BASE === null) {
		API_BASE = resolveApiBase(browserSources());
		console.info(
			'[simmer] API_BASE =',
			API_BASE,
			'(dev builds override with ?api=http://host:port; ignored on deployed builds)'
		);
	}
	return API_BASE;
}

export class ApiError extends Error {
	status: number;
	detail: unknown;
	constructor(path: string, status: number, detail?: unknown) {
		super(
			typeof detail === 'string' && detail ? detail : `${path} → HTTP ${status}`
		);
		this.status = status;
		this.detail = detail;
	}
}

function buildUrl(path: string): string {
	let url = `${apiBase()}${path}`;
	if (isDevBypass()) {
		let admin: string | null = null;
		try {
			admin = localStorage.getItem(ADMIN_TOKEN_KEY);
		} catch {
			/* storage unavailable */
		}
		url = withAdminToken(url, admin);
	}
	return url;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	// Headers are (re)built per attempt so a mid-flight token refresh is
	// reflected in the replay.
	const doFetch = (): Promise<Response> =>
		fetch(buildUrl(path), {
			mode: 'cors',
			...init,
			headers: {
				// Only a string body is JSON. FormData MUST go without an explicit
				// Content-Type so the browser can add the multipart boundary — set it
				// by hand and the backend cannot parse a single field.
				...(typeof init?.body === 'string' ? { 'Content-Type': 'application/json' } : {}),
				...authHeaders({ accessToken: tokenProvider() }),
				...(init?.headers ?? {})
			}
		});
	let r = await doFetch();
	if (r.status === 401 && !path.startsWith('/auth/')) {
		// Session likely expired: let the auth layer refresh ONCE, then replay.
		// /auth/* is exempt — a 401 there IS the answer (bad credentials/token).
		if (await unauthorizedHandler()) r = await doFetch();
	}
	if (!r.ok) {
		let detail: unknown = null;
		try {
			const j = await r.json();
			detail = (j as { detail?: unknown })?.detail ?? j;
		} catch {
			/* non-JSON error body */
		}
		throw new ApiError(path, r.status, detail);
	}
	return (await r.json()) as T;
}

/** User-facing one-liner from any thrown error: the backend's detail message
 *  plus the HTTP status, e.g. "Invalid login credentials (401)". Non-ApiError
 *  (network/unexpected) degrades to the raw message. */
export function errorMessage(e: unknown): string {
	if (e instanceof ApiError) {
		const base = typeof e.detail === 'string' && e.detail ? e.detail : e.message;
		return `${base} (${e.status})`;
	}
	return e instanceof Error ? e.message : String(e);
}

export const getJSON = <T>(path: string): Promise<T> => request<T>(path);

/** Earnings verdict for a symbol's next earnings window (cached; computes the
 *  news→bias on demand server-side). The card's consider↔ignore toggle is a pure
 *  display swap over env.earnings / env.earnings_alt, so no per-toggle call. */
export const getEarnings = <T>(symbol: string, refresh = false): Promise<T> =>
	request<T>(`/simmer/earnings/${encodeURIComponent(symbol)}${refresh ? '?refresh=1' : ''}`);

export const postJSON = <T>(path: string, body: unknown): Promise<T> =>
	request<T>(path, { method: 'POST', body: JSON.stringify(body) });

/** multipart/form-data POST — the support-ticket form, which can carry a file.
 *  Goes through request() so it inherits the bearer token and the 401
 *  refresh-and-replay; see the Content-Type note there. */
export const postForm = <T>(path: string, form: FormData): Promise<T> =>
	request<T>(path, { method: 'POST', body: form });

export const patchJSON = <T>(path: string, body: unknown): Promise<T> =>
	request<T>(path, { method: 'PATCH', body: JSON.stringify(body) });

export const deleteJSON = <T>(path: string): Promise<T> =>
	request<T>(path, { method: 'DELETE' });

/** test-only: reset module state between cases */
export function _resetForTests(): void {
	API_BASE = null;
	tokenProvider = () => null;
	unauthorizedHandler = async () => false;
}
