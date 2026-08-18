import { describe, expect, it } from 'vitest';
import {
	ago,
	decisionBadgeClass,
	humanizeReason,
	money,
	pct,
	prob,
	scoreBarClass,
	scoreClass,
	sentimentLabel,
	sigInt,
	strike,
	structureName
} from './fmt';

describe('numeric formatting (Matrix parity)', () => {
	it('money renders em-dash for nullish/NaN', () => {
		expect(money(null)).toBe('—');
		expect(money(undefined)).toBe('—');
		expect(money(NaN)).toBe('—');
		expect(money(1.234)).toBe('1.23');
		expect(money(1.2345, 3)).toBe('1.234');
	});
	it('pct and prob', () => {
		expect(pct(12.34)).toBe('12.3%');
		expect(pct(null)).toBe('—');
		expect(prob(0.7312)).toBe('73.1%');
		expect(prob(null)).toBe('—');
	});
	it('strike + sigInt', () => {
		expect(strike(4500)).toBe('4,500');
		expect(strike(null)).toBe('—');
		expect(sigInt(1234.6)).toBe('1,235');
	});
});

describe('ago', () => {
	const now = new Date('2026-08-16T12:00:00Z');
	it('seconds / minutes / hours / days', () => {
		expect(ago('2026-08-16T11:59:30Z', now)).toBe('30s ago');
		expect(ago('2026-08-16T11:55:00Z', now)).toBe('5m ago');
		expect(ago('2026-08-16T09:00:00Z', now)).toBe('3h ago');
		expect(ago('2026-08-13T12:00:00Z', now)).toBe('3d ago');
	});
	it('nullish and garbage', () => {
		expect(ago(null, now)).toBe('—');
		expect(ago('not-a-date', now)).toBe('not-a-date');
	});
});

describe('score semantics — bands from /simmer/config decision', () => {
	it('composite classes at the default 70/50 bands', () => {
		expect(scoreClass(85)).toBe('composite-good');
		expect(scoreClass(70)).toBe('composite-good');
		expect(scoreClass(69.9)).toBe('composite-meh');
		expect(scoreClass(50)).toBe('composite-meh');
		expect(scoreClass(49.9)).toBe('composite-bad');
		expect(scoreClass(null)).toBe('composite-bad');
	});
	it('honors non-default bands', () => {
		expect(scoreClass(65, 60, 40)).toBe('composite-good');
		expect(scoreBarClass(45, 60, 40)).toBe('scorebar-meh');
	});
});

describe('decision + structure labels', () => {
	it('badge classes per decision', () => {
		expect(decisionBadgeClass('ready')).toBe('badge-healthy');
		expect(decisionBadgeClass('watch')).toBe('badge-thin');
		expect(decisionBadgeClass('avoid')).toBe('badge-directional');
		expect(decisionBadgeClass('vetoed')).toBe('badge-broken');
	});
	it('structure names', () => {
		expect(structureName('bull_put')).toBe('Bull Put');
		expect(structureName('iron_condor')).toBe('Iron Condor');
		expect(structureName(null)).toBe('—');
		expect(structureName('mystery')).toBe('mystery');
	});
});

describe('sentimentLabel — "0.0 balanced" is not "no news"', () => {
	it('no news when n is 0 or null', () => {
		expect(sentimentLabel(null, 0)).toBe('no news');
		expect(sentimentLabel(0, null)).toBe('no news');
	});
	it('balanced zero with n > 0', () => {
		expect(sentimentLabel(0, 4)).toBe('+0.00 balanced');
	});
	it('signed positive/negative words', () => {
		expect(sentimentLabel(0.42, 3)).toBe('+0.42 positive');
		expect(sentimentLabel(-0.3, 2)).toBe('-0.30 negative');
	});
});

describe('humanizeReason — engine veto tokens', () => {
	it('splits scopes and words', () => {
		expect(humanizeReason('liquidity:spread_pct_of_credit')).toBe(
			'liquidity · spread % of credit'
		);
		expect(humanizeReason('bull_put:short_delta_band')).toBe('bull put · short delta band');
		expect(humanizeReason('catalyst:earnings_in_tenor')).toBe('catalyst · earnings in tenor');
	});
	it('uppercases the finance acronyms', () => {
		expect(humanizeReason('dte_window')).toBe('DTE window');
		expect(humanizeReason('vrp_floor')).toBe('VRP floor');
		expect(humanizeReason('iv_percentile_floor')).toBe('IV percentile floor');
	});
});
