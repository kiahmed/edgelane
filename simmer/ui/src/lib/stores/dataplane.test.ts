// API-first data plane: the stores must hit the backend endpoints (bearer JWT
// attached by api.ts) and NEVER a Supabase table. These tests pin the request
// shapes (POST/DELETE/PATCH watchlist), the verbatim 422 surfacing, the
// /simmer/status entitlement inference (200/403/401) and the 401→refresh→
// replay behavior in api.ts.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
	_resetForTests,
	getJSON,
	setAuthTokenProvider,
	setUnauthorizedHandler
} from '../api';
import { watchlist } from './watchlist.svelte';
import { auth, decodeJwtPayload } from './auth.svelte';
import type { WatchlistRow } from '../types';

const BASE = 'http://127.0.0.1:8789';

const res = (status: number, body: unknown): Response =>
	({
		ok: status >= 200 && status < 300,
		status,
		json: async () => body
	}) as unknown as Response;

interface Call {
	url: string;
	init?: RequestInit;
}
let calls: Call[];

function mockFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
	calls = [];
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string, init?: RequestInit) => {
			calls.push({ url, init });
			return handler(url, init);
		})
	);
}

/** Default happy-path routing for the refresh() the store runs after writes. */
const okRoutes = (url: string, init?: RequestInit): Response => {
	const method = init?.method ?? 'GET';
	if (method !== 'GET') return res(200, { symbol: 'X', written: true, deleted: true, updated: [] });
	if (url.startsWith(`${BASE}/simmer/watchlist`))
		return res(200, { watchlist: [], supabase: true, write_model: 'api', last_sweep_at: null });
	return res(200, { alive: true });
};

beforeEach(() => {
	_resetForTests();
	auth.session = null;
	auth.toolsEnabled = null;
	auth.devBypass = false;
});

describe('watchlist writes go through the API', () => {
	it('add = single POST /simmer/watchlist {symbol} (validation is server-side)', async () => {
		mockFetch(okRoutes);
		const err = await watchlist.add('amd');
		expect(err).toBeNull();
		const post = calls[0];
		expect(post.url).toBe(`${BASE}/simmer/watchlist`);
		expect(post.init?.method).toBe('POST');
		expect(JSON.parse(String(post.init?.body))).toEqual({ symbol: 'AMD' });
	});

	it('add carries the optional expiration in the same POST', async () => {
		mockFetch(okRoutes);
		await watchlist.add('AMD', '2026-09-18');
		expect(JSON.parse(String(calls[0].init?.body))).toEqual({
			symbol: 'AMD',
			expiration: '2026-09-18'
		});
	});

	it('surfaces the backend 422 detail VERBATIM (dev-bypass identity case)', async () => {
		const detail =
			'watchlist rows belong to a signed-in user account; the admin/dev bypass has none';
		mockFetch(() => res(422, { detail }));
		const err = await watchlist.add('AMD');
		expect(err).toBe(detail);
	});

	it('remove = DELETE /simmer/watchlist/{symbol}', async () => {
		mockFetch(okRoutes);
		const row = { id: 'r1', symbol: 'AMD' } as WatchlistRow;
		const err = await watchlist.remove(row);
		expect(err).toBeNull();
		expect(calls[0].url).toBe(`${BASE}/simmer/watchlist/AMD`);
		expect(calls[0].init?.method).toBe('DELETE');
	});

	it('expiration pin = PATCH /simmer/watchlist/{symbol} {expiration}', async () => {
		mockFetch(okRoutes);
		const row = { id: 'r1', symbol: 'AMD' } as WatchlistRow;
		const err = await watchlist.pinExpiration(row, '2026-09-18');
		expect(err).toBeNull();
		expect(calls[0].url).toBe(`${BASE}/simmer/watchlist/AMD`);
		expect(calls[0].init?.method).toBe('PATCH');
		expect(JSON.parse(String(calls[0].init?.body))).toEqual({ expiration: '2026-09-18' });
	});
});

describe('entitlement inference from GET /simmer/status', () => {
	const session = { access_token: 'jwt', refresh_token: 'rt' };

	it('200 → entitled (hasSimmer true)', async () => {
		auth.session = { ...session };
		mockFetch(() => res(200, { alive: true }));
		await auth.loadEntitlements();
		expect(auth.toolsEnabled).toEqual(['simmer']);
		expect(auth.hasSimmer).toBe(true);
	});

	it('403 → signed in but not entitled → ProductGate state', async () => {
		auth.session = { ...session };
		mockFetch(() => res(403, { detail: 'simmer is not enabled for this account' }));
		await auth.loadEntitlements();
		expect(auth.toolsEnabled).toEqual([]);
		expect(auth.hasSimmer).toBe(false);
		expect(auth.toolsKnown).toBe(true);
	});

	it('401 (refresh already failed) → session invalidated → signed out', async () => {
		auth.session = { ...session };
		mockFetch(() => res(401, { detail: 'invalid token' }));
		await auth.loadEntitlements();
		expect(auth.session).toBeNull();
		expect(auth.toolsEnabled).toBeNull();
		expect(auth.isFull).toBe(false);
	});
});

describe('401 → refresh-once → replay (api.ts)', () => {
	it('replays with the fresh token when the handler refreshes', async () => {
		let token = 'stale';
		setAuthTokenProvider(() => token);
		setUnauthorizedHandler(async () => {
			token = 'fresh';
			return true;
		});
		mockFetch((_url, init) => {
			const authz = (init?.headers as Record<string, string>)?.Authorization;
			return authz === 'Bearer fresh' ? res(200, { ok: true }) : res(401, { detail: 'expired' });
		});
		await expect(getJSON('/simmer/status')).resolves.toEqual({ ok: true });
		expect(calls).toHaveLength(2);
	});

	it('does NOT try to refresh on a 401 from /auth/* (that 401 IS the answer)', async () => {
		const handler = vi.fn(async () => true);
		setUnauthorizedHandler(handler);
		mockFetch(() => res(401, { detail: 'Invalid login credentials' }));
		await expect(getJSON('/auth/login')).rejects.toThrow('Invalid login credentials');
		expect(handler).not.toHaveBeenCalled();
	});
});

describe('JWT payload decode (display only)', () => {
	it('decodes base64url payloads', () => {
		const payload = { sub: 'u-1', email: 'a@b.co' };
		const b64 = btoa(JSON.stringify(payload))
			.replace(/\+/g, '-')
			.replace(/\//g, '_')
			.replace(/=+$/, '');
		expect(decodeJwtPayload(`h.${b64}.sig`)).toEqual(payload);
	});
	it('returns null on garbage', () => {
		expect(decodeJwtPayload('not-a-jwt')).toBeNull();
		expect(decodeJwtPayload(null)).toBeNull();
	});
});
