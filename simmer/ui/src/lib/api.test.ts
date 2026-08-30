import { describe, expect, it, vi } from 'vitest';
import { authHeaders, resolveApiBase, withAdminToken } from './api';
import { isDevBypass } from './config';

describe('isDevBypass — deployed-build detection (gates the ?api= lockout)', () => {
	it('true only when NO deploy config is baked at all (real local dev)', () => {
		expect(
			isDevBypass({ apiBase: null, supabaseUrl: null, supabaseAnonKey: null, turnstileSiteKey: null })
		).toBe(true);
	});

	it('REGRESSION: a deployed build with no baked apiBase is still NOT dev', () => {
		// Deploys now always bake the tunnel hostname, but dev-detection must not
		// hinge on it: api.ts once used `!apiBase` here, which flagged deployed
		// builds as dev and reopened the ?api= phishing override. The Supabase
		// marker globals are what prove a build is deployed.
		expect(
			isDevBypass({
				apiBase: null,
				supabaseUrl: 'https://x.supabase.co',
				supabaseAnonKey: 'anon',
				turnstileSiteKey: null
			})
		).toBe(false);
	});

	it('a baked API base alone also marks a deployed build', () => {
		expect(
			isDevBypass({
				apiBase: 'https://tunnel.example',
				supabaseUrl: null,
				supabaseAnonKey: null,
				turnstileSiteKey: null
			})
		).toBe(false);
	});

	it('ROBUSTNESS: a production build is deployed even with ZERO baked config', () => {
		// The core of the API/Edge-Config migration: a vite-built bundle needs no
		// baked globals to know it is deployed, so detection can't silently break
		// if edgelane.config.js is missing or stale.
		vi.stubEnv('PROD', true);
		try {
			expect(
				isDevBypass({
					apiBase: null,
					supabaseUrl: null,
					supabaseAnonKey: null,
					turnstileSiteKey: null
				})
			).toBe(false);
		} finally {
			vi.unstubAllEnvs();
		}
	});
});

describe('resolveApiBase precedence', () => {
	it('falls back to the Makefile default port with no sources', () => {
		expect(resolveApiBase({})).toBe('http://127.0.0.1:8789');
	});

	it('SECURITY: baked global wins over ?api= and stored (no override on deployed builds)', () => {
		// A deployed build (baked set) must be un-overridable, else
		// ?api=https://evil.tld phishes the login credentials.
		expect(
			resolveApiBase({
				queryApi: 'https://evil.tld',
				stored: 'https://also-evil.tld',
				baked: 'https://baked.example',
				origin: 'https://origin.example'
			})
		).toBe('https://baked.example');
	});

	it('?api= is honored only on DEV builds (no baked global)', () => {
		expect(
			resolveApiBase({ queryApi: 'http://127.0.0.1:8788', stored: 'http://stored.example' })
		).toBe('http://127.0.0.1:8788');
	});

	it('stored override is honored only on DEV builds', () => {
		expect(
			resolveApiBase({ stored: 'http://stored.example', origin: 'https://origin.example' })
		).toBe('http://stored.example');
	});

	it('baked global beats same-origin', () => {
		expect(
			resolveApiBase({ baked: 'https://baked.example', origin: 'https://origin.example' })
		).toBe('https://baked.example');
	});

	it('same-origin used when nothing else set', () => {
		expect(resolveApiBase({ origin: 'https://ui.example' })).toBe('https://ui.example');
	});

	it('strips trailing slashes', () => {
		expect(resolveApiBase({ queryApi: 'http://h:1/' })).toBe('http://h:1');
		expect(resolveApiBase({ baked: 'https://b.example//' })).toBe('https://b.example');
	});
});

describe('authHeaders', () => {
	it('bearer JWT when signed in', () => {
		expect(authHeaders({ accessToken: 'jwt123' })).toEqual({ Authorization: 'Bearer jwt123' });
	});
	it('empty when signed out (no anon teaser in Simmer v1)', () => {
		expect(authHeaders({})).toEqual({});
		expect(authHeaders({ accessToken: null })).toEqual({});
	});
});

describe('withAdminToken (dev-bypass passthrough)', () => {
	it('appends ?token= to a bare URL', () => {
		expect(withAdminToken('http://h:1/simmer/status', 'abc')).toBe(
			'http://h:1/simmer/status?token=abc'
		);
	});
	it('appends &token= when a query string exists', () => {
		expect(withAdminToken('http://h:1/simmer/alerts?limit=50', 'abc')).toBe(
			'http://h:1/simmer/alerts?limit=50&token=abc'
		);
	});
	it('URL-encodes the token', () => {
		expect(withAdminToken('http://h:1/x', 'a b&c')).toBe('http://h:1/x?token=a%20b%26c');
	});
	it('no-op without a token', () => {
		expect(withAdminToken('http://h:1/x', null)).toBe('http://h:1/x');
		expect(withAdminToken('http://h:1/x', undefined)).toBe('http://h:1/x');
	});
});
