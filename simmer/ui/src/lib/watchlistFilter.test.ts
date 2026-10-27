import { describe, it, expect } from 'vitest';
import { cardConf, passesMinConf } from './watchlistFilter';
import type { WatchlistRow } from './types';

// Minimal row builder — only the fields the filter reads matter.
const row = (o: Partial<WatchlistRow>): WatchlistRow =>
	({ id: '1', symbol: 'X', expiration: null, position: 0, active: true, readiness: null, ...o }) as WatchlistRow;

describe('cardConf — the number the card shows', () => {
	it('uses the overnight (reconciled) read when present, NOT the readiness confidence', () => {
		// PCAR: tape flipped it to wait → reconciled 30, even though readiness conf is high
		expect(cardConf(row({ overnight_conf: 30, readiness: { confidence: 0.85 } as never }))).toBe(30);
	});
	it('falls back to readiness confidence (×100) when there is no overnight read', () => {
		expect(cardConf(row({ readiness: { confidence: 0.72 } as never }))).toBe(72);
	});
	it('is 0 when neither is present', () => {
		expect(cardConf(row({}))).toBe(0);
	});
});

describe('passesMinConf at min 80 — the reported bug', () => {
	it('EXCLUDES PCAR (overnight 30) even though its readiness confidence is 85', () => {
		expect(passesMinConf(row({ overnight_conf: 30, readiness: { confidence: 0.85 } as never }), 80)).toBe(false);
	});
	it('INCLUDES AMD (overnight 84)', () => {
		expect(passesMinConf(row({ overnight_conf: 84 }), 80)).toBe(true);
	});
	it('INCLUDES AVGO (overnight 82)', () => {
		expect(passesMinConf(row({ overnight_conf: 82 }), 80)).toBe(true);
	});
	it('a readiness-only name is judged on its readiness confidence', () => {
		expect(passesMinConf(row({ readiness: { confidence: 0.9 } as never }), 80)).toBe(true);
		expect(passesMinConf(row({ readiness: { confidence: 0.5 } as never }), 80)).toBe(false);
	});
	it('min 0 disables the filter (everything passes)', () => {
		expect(passesMinConf(row({ overnight_conf: 5 }), 0)).toBe(true);
	});
});
