// Auth store — a plain token manager over the backend's /auth/* proxy
// endpoints (app/routes/auth_proxy.py). The browser NEVER talks to Supabase:
// the backend is the identity surface, Supabase sits behind it. The only
// identity artifacts held client-side are the JWT + refresh token, kept in
// sessionStorage (per-tab isolation, same intent as the old supabase-js
// sessionStorage config).
//
// Entitlement is inferred from the API, not read from any table:
//   GET /simmer/status → 200 entitled · 403 signed in but not entitled
//   (ProductGate) · 401 session invalid (refresh once, else sign out).
//
// Dev-bypass (no deploy config baked + optional localStorage admin token)
// behaves exactly like Matrix: full UI, backend calls carry ?token=.
import { readGlobals, isDevBypass } from '../config';
import {
	ApiError,
	errorMessage,
	getJSON,
	postJSON,
	setAuthTokenProvider,
	setUnauthorizedHandler
} from '../api';

export type GateMode = 'signin' | 'signup';

/** Session shape returned by /auth/login, /auth/refresh and (when no email
 *  confirmation is pending) /auth/signup. */
export interface AuthSession {
	access_token: string;
	refresh_token: string;
	expires_in?: number | null;
	/** unix seconds */
	expires_at?: number | null;
}

const SESSION_KEY = 'simmer-session';
/** Refresh the JWT this long before it expires. */
const REFRESH_LEAD_MS = 60_000;
/** Re-probe /simmer/status after a transient (network/5xx) failure. */
const ENTITLEMENT_RETRY_MS = 5_000;

/** Base64url-decode a JWT's payload for DISPLAY ONLY (uid/email). No
 *  verification — the backend verifies; the client merely shows claims. */
export function decodeJwtPayload(
	token: string | null | undefined
): Record<string, unknown> | null {
	if (!token) return null;
	try {
		const part = token.split('.')[1];
		if (!part) return null;
		const b64 = part.replace(/-/g, '+').replace(/_/g, '/');
		const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
		return JSON.parse(atob(padded)) as Record<string, unknown>;
	} catch {
		return null;
	}
}

function loadStoredSession(): AuthSession | null {
	try {
		const raw = sessionStorage.getItem(SESSION_KEY);
		if (!raw) return null;
		const s = JSON.parse(raw) as AuthSession;
		return s?.access_token && s?.refresh_token ? s : null;
	} catch {
		return null;
	}
}

class AuthStore {
	configured = $state(false);
	devBypass = $state(false);
	ready = $state(false);
	session = $state<AuthSession | null>(null);
	mode = $state<GateMode>('signin');
	/** null = not probed yet; [] = probed, simmer not entitled (ProductGate) */
	toolsEnabled = $state<string[] | null>(null);
	toolsLoading = $state(false);
	pendingEmail = $state<string | null>(null);

	private refreshTimer: ReturnType<typeof setTimeout> | null = null;
	private entitlementTimer: ReturnType<typeof setTimeout> | null = null;
	private refreshing: Promise<boolean> | null = null;

	/** Display-only identity decoded from the JWT (never trusted for access —
	 *  the backend verifies the signature on every call). */
	get user(): { id: string; email: string | null } | null {
		const claims = decodeJwtPayload(this.session?.access_token);
		if (!claims) return null;
		return {
			id: String(claims.sub ?? ''),
			email: typeof claims.email === 'string' ? claims.email : null
		};
	}
	get isFull(): boolean {
		return this.devBypass || !!this.session;
	}
	/** Simmer entitlement: bypass in dev; otherwise the /simmer/status probe. */
	get hasSimmer(): boolean {
		if (this.devBypass) return true;
		return !!this.toolsEnabled?.includes('simmer');
	}
	get toolsKnown(): boolean {
		return this.devBypass || this.toolsEnabled !== null;
	}

	async init(): Promise<void> {
		if (this.ready) return;
		const g = readGlobals();
		this.configured = !isDevBypass(g);
		if (!this.configured) {
			this.devBypass = true;
			this.ready = true;
			return;
		}
		setAuthTokenProvider(() => this.session?.access_token ?? null);
		setUnauthorizedHandler(() => this.tryRefresh());
		const stored = loadStoredSession();
		if (stored) {
			this.session = stored;
			if (this.msToExpiry() <= REFRESH_LEAD_MS) await this.tryRefresh();
			else this.scheduleRefresh();
		}
		this.ready = true;
		if (this.session) void this.loadEntitlements();
	}

	private msToExpiry(): number {
		const at = this.session?.expires_at;
		if (!at) return Number.POSITIVE_INFINITY;
		return at * 1000 - Date.now();
	}

	private setSession(s: AuthSession): void {
		const normalized: AuthSession = { ...s };
		if (normalized.expires_at == null && normalized.expires_in != null) {
			normalized.expires_at = Math.floor(Date.now() / 1000) + Number(normalized.expires_in);
		}
		this.session = normalized;
		try {
			sessionStorage.setItem(SESSION_KEY, JSON.stringify(normalized));
		} catch {
			/* storage unavailable — session lives in memory only */
		}
		this.scheduleRefresh();
	}

	private scheduleRefresh(): void {
		if (this.refreshTimer) clearTimeout(this.refreshTimer);
		this.refreshTimer = null;
		const ms = this.msToExpiry();
		if (!this.session || !Number.isFinite(ms)) return;
		const delay = Math.max(5_000, ms - REFRESH_LEAD_MS);
		this.refreshTimer = setTimeout(() => void this.tryRefresh(), delay);
	}

	/** Single-flight refresh. Returns true on success; on failure signs out
	 *  (the refresh token is dead — there is nothing to retry with). Also the
	 *  handler api.ts calls on a 401 before replaying a request. */
	tryRefresh(): Promise<boolean> {
		if (this.refreshing) return this.refreshing;
		const refresh_token = this.session?.refresh_token;
		if (!refresh_token) return Promise.resolve(false);
		this.refreshing = (async () => {
			try {
				const res = await postJSON<{ session: AuthSession }>('/auth/refresh', {
					refresh_token
				});
				if (!res?.session?.access_token) throw new Error('no session in refresh response');
				this.setSession(res.session);
				return true;
			} catch (e) {
				console.warn('[auth] token refresh failed — signing out', e);
				await this.signOut({ revoke: false });
				return false;
			} finally {
				this.refreshing = null;
			}
		})();
		return this.refreshing;
	}

	/** Entitlement probe: GET /simmer/status through the normal API path.
	 *  200 → entitled; 403 → signed in but Simmer not on the plan; 401 (after
	 *  api.ts's one refresh attempt) → session invalid → sign out. Anything
	 *  else (backend down, network) is NOT a verdict — keep "checking" and
	 *  retry, never show ProductGate off a transient error. */
	async loadEntitlements(): Promise<void> {
		if (this.devBypass || !this.session) return;
		this.toolsLoading = true;
		try {
			await getJSON('/simmer/status');
			this.toolsEnabled = ['simmer'];
		} catch (e) {
			if (e instanceof ApiError && e.status === 403) {
				this.toolsEnabled = [];
			} else if (e instanceof ApiError && e.status === 401) {
				await this.signOut({ revoke: false });
			} else {
				console.warn('[auth] entitlement probe failed (transient?)', e);
				this.scheduleEntitlementRetry();
			}
		} finally {
			this.toolsLoading = false;
		}
	}

	private scheduleEntitlementRetry(): void {
		if (this.entitlementTimer) return;
		this.entitlementTimer = setTimeout(() => {
			this.entitlementTimer = null;
			if (this.session && this.toolsEnabled === null) void this.loadEntitlements();
		}, ENTITLEMENT_RETRY_MS);
	}

	async signIn(email: string, password: string): Promise<string | null> {
		try {
			const res = await postJSON<{ session: AuthSession }>('/auth/login', {
				email,
				password
			});
			this.setSession(res.session);
			this.toolsEnabled = null;
			void this.loadEntitlements();
			return null;
		} catch (e) {
			const msg = errorMessage(e);
			// Surface unconfirmed-email as a resend opportunity, like Matrix.
			if (/confirm/i.test(msg)) this.pendingEmail = email;
			return msg;
		}
	}

	/** Returns { error } or { needsConfirmation } */
	async signUp(
		email: string,
		password: string
	): Promise<{ error?: string; needsConfirmation?: boolean }> {
		try {
			const res = await postJSON<{
				session?: AuthSession | null;
				confirmation_required?: boolean;
			}>('/auth/signup', { email, password });
			if (res.confirmation_required || !res.session) {
				this.pendingEmail = email;
				return { needsConfirmation: true };
			}
			this.setSession(res.session);
			this.toolsEnabled = null;
			void this.loadEntitlements();
			return {};
		} catch (e) {
			return { error: errorMessage(e) };
		}
	}

	async resendConfirmation(email?: string): Promise<string | null> {
		const target = email || this.pendingEmail;
		if (!target) return 'no pending email';
		try {
			await postJSON('/auth/resend', { email: target });
			return null;
		} catch (e) {
			return errorMessage(e);
		}
	}

	async signOut(opts?: { revoke?: boolean }): Promise<void> {
		const refresh_token = this.session?.refresh_token;
		if (opts?.revoke !== false && refresh_token) {
			try {
				await postJSON('/auth/logout', { refresh_token });
			} catch {
				/* revocation is best-effort; local state clears regardless */
			}
		}
		if (this.refreshTimer) clearTimeout(this.refreshTimer);
		this.refreshTimer = null;
		this.session = null;
		this.toolsEnabled = null;
		try {
			sessionStorage.removeItem(SESSION_KEY);
		} catch {
			/* storage unavailable */
		}
	}
}

export const auth = new AuthStore();
