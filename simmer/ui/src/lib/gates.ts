// Gate-checklist derivation — pure, testable. Maps the engine's veto_reasons
// (global_gates + structure_gates reason tokens, simmer_engine.py:893–1005)
// plus data_quality onto a fixed, refusal-first checklist. Reason vocabulary
// was read from the backend source, not guessed.
import type { ReadinessEnvelope } from './types';

export type GateState = 'pass' | 'fail' | 'unknown';

export interface GateCheck {
	key: string;
	label: string;
	locked: boolean;
	state: GateState;
	/** matched raw reasons (human-formatted by the component) */
	reasons: string[];
}

const STRUCTURE_PREFIX = /^(bull_put|bear_call|iron_condor):/;

/** structure_gates failures arrive prefixed "bull_put:liquidity:…" when every
 *  candidate died; normalize those off before matching. */
export function stripStructurePrefix(reason: string): string {
	return reason.replace(STRUCTURE_PREFIX, '');
}

interface GateDef {
	key: string;
	label: string;
	locked: boolean;
	/** reason token matches (prefix match on the normalized reason) */
	match: string[];
	/** tokens that mean "we could not evaluate" rather than "you failed" */
	unknown?: string[];
}

// Order is the display order: locked safety gates first, then the tunables.
const GATES: GateDef[] = [
	{
		key: 'catalyst_lockout',
		label: 'Catalyst lockout',
		locked: true,
		match: ['catalyst:']
	},
	{
		key: 'liquidity',
		label: 'Liquidity floor',
		locked: true,
		match: ['liquidity:'],
		unknown: ['liquidity:unquoted']
	},
	{
		key: 'dollar_friction',
		label: 'Dollar-friction test',
		locked: true,
		match: ['dollar_friction', 'friction:'],
		unknown: ['friction:unavailable']
	},
	{
		key: 'chain_sanity',
		label: 'Chain sanity',
		locked: true,
		match: ['chain_sanity:']
	},
	{
		key: 'macro_validity',
		label: 'Macro calendar validity',
		locked: true,
		match: ['macro_calendar:'],
		unknown: ['macro_calendar:unavailable']
	},
	{
		key: 'dte_window',
		label: 'DTE window',
		locked: false,
		match: ['dte_window']
	},
	{
		key: 'iv_floor',
		label: 'IV percentile floor',
		locked: false,
		match: ['iv_percentile_floor', 'volatility_history_unavailable'],
		unknown: ['volatility_history_unavailable']
	},
	{
		key: 'vrp_floor',
		label: 'Vol risk premium floor',
		locked: false,
		match: ['vrp_floor', 'vrp_unavailable', 'rv_estimator_disagreement'],
		unknown: ['vrp_unavailable']
	},
	{
		key: 'short_delta',
		label: 'Short-delta band',
		locked: false,
		match: ['short_delta_band', 'short_delta:'],
		unknown: ['short_delta:unavailable']
	},
	{
		key: 'structure_eligibility',
		label: 'Structure eligibility',
		locked: false,
		match: ['no_eligible_structure', 'no_candidate', 'ineligible:']
	}
];

/** Build the checklist for one readiness envelope. */
export function gateChecklist(env: ReadinessEnvelope): GateCheck[] {
	const raw = env.veto_reasons ?? [];
	const normalized = raw.map(stripStructurePrefix);
	const dq = env.data_quality ?? {};

	return GATES.map((def) => {
		const reasons: string[] = [];
		let anyUnknownToken = false;
		normalized.forEach((r, i) => {
			if (def.match.some((m) => (m.endsWith(':') ? r.startsWith(m) : r === m || r.startsWith(m + ':')))) {
				reasons.push(raw[i]);
				if ((def.unknown ?? []).some((u) => r === u)) anyUnknownToken = true;
			}
		});
		let state: GateState;
		if (reasons.length) {
			state = anyUnknownToken && reasons.length === 1 ? 'unknown' : 'fail';
		} else {
			state = 'pass';
			// No veto, but flag gates whose underlying data never arrived.
			if (def.key === 'macro_validity' && dq.macro_calendar === false) state = 'unknown';
			if (def.key === 'iv_floor' && (dq.iv_history ?? 1) === 0) state = 'unknown';
			if (def.key === 'liquidity' && (dq.quote ?? 1) === 0 && env.decision === 'vetoed')
				state = 'unknown';
		}
		return { key: def.key, label: def.label, locked: def.locked, state, reasons };
	});
}

export function failedCount(checks: GateCheck[]): number {
	return checks.filter((c) => c.state === 'fail').length;
}
