// Runtime config globals — written by static/edgelane.config.js (nulls in dev)
// and overwritten at deploy by deploy.sh stage_simmer(). Null/absent globals
// => local dev-bypass mode.
//
// NOTE (no-Supabase-in-browser rule): the app's only REQUIRED config is the
// API base. The Supabase URL/anon-key globals are still parsed for
// back-compat with existing deploy scripts, but nothing contacts Supabase
// with them anymore — auth and all data flow through the backend API. Their
// presence merely marks a build as "deployed" (see isDevBypass).

declare global {
	interface Window {
		__EDGELANE_API_BASE__?: string | null;
		__EDGELANE_SUPABASE_URL__?: string | null;
		__EDGELANE_SUPABASE_ANON_KEY__?: string | null;
		__EDGELANE_TURNSTILE_SITE_KEY__?: string | null;
	}
}

export interface EdgelaneGlobals {
	apiBase: string | null;
	supabaseUrl: string | null;
	supabaseAnonKey: string | null;
	turnstileSiteKey: string | null;
}

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);

/** Read the deploy-baked globals. Safe to call on the server (returns nulls). */
export function readGlobals(w?: Partial<Window>): EdgelaneGlobals {
	const win = w ?? (typeof window !== 'undefined' ? window : undefined);
	return {
		apiBase: str(win?.__EDGELANE_API_BASE__),
		supabaseUrl: str(win?.__EDGELANE_SUPABASE_URL__),
		supabaseAnonKey: str(win?.__EDGELANE_SUPABASE_ANON_KEY__),
		turnstileSiteKey: str(win?.__EDGELANE_TURNSTILE_SITE_KEY__)
	};
}

/** Dev-bypass: the open, no-gate UI that talks to a local backend.
 *
 *  The deployed-vs-dev signal is `import.meta.env.PROD` — Vite compiles it into
 *  the bundle at `vite build`, so it survives Vercel's remote rebuild and needs
 *  NO baked config. This is the point of the API + Edge Config migration: a
 *  production build resolves the backend URL from /api/config and carries no
 *  Supabase creds or other secrets in the browser, so "deployed" can no longer
 *  hinge on their presence. Only a Vite dev server (import.meta.env.DEV) is
 *  dev-bypass. The globals fallback is kept for tests / non-Vite consumers.
 *
 *  The backend still 401s unless it runs AUTH_ENABLED=false or an admin token is
 *  provided via localStorage `edgelane_admin_token` — see api.ts. */
export function isDevBypass(g: EdgelaneGlobals = readGlobals()): boolean {
	if (import.meta.env.PROD) return false;                 // any production build is deployed
	return !(g.apiBase || (g.supabaseUrl && g.supabaseAnonKey));
}

export const POLL_INTERVAL_KEY = 'edgelane_simmer_poll_interval_v1';
export const API_BASE_KEY = 'edgelane_api_base';
export const ADMIN_TOKEN_KEY = 'edgelane_admin_token';
export const DEFAULT_POLL_MS = 15_000;
