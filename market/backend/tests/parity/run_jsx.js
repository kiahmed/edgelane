#!/usr/bin/env node
// run_jsx.js — CLI wrapper around jsx_engine.js. Reads {op, args} JSON from
// stdin, dispatches to the matching ground-truth JSX function, writes the
// raw result JSON to stdout. Used by test_parity.py to compare port outputs
// against the original JSX engine running on Node.
const e = require('./jsx_engine.js');

let input = '';
process.stdin.on('data', d => input += d);
process.stdin.on('end', () => {
  const { op, args } = JSON.parse(input);
  let result;
  if (op === 'dealer_exposures') {
    result = e._computeDealerExposures(args.contracts, args.spot);
  } else if (op === 'bias_signals') {
    result = e._computeBiasSignals(
      args.symbol,
      args.expiration,
      args.spot,
      args.greeks_raw,
      args.exposures_for_chosen,
      args.regime_override,
      args.chosen_dte,
    );
  } else if (op === 'generate_candidates') {
    result = e.generateCandidates(
      args.strategy,
      args.contracts,
      args.dte,
      args.expected_move,
      args.target_delta,
      args.width_factor,
      args.walls,
    );
  } else if (op === 'composite_pick') {
    const cands = e.generateCandidates(
      args.strategy,
      args.contracts,
      args.dte,
      args.expected_move,
      args.target_delta,
      args.width_factor,
      args.walls,
    );
    result = { candidates: cands, best_label: e.pickBestCandidate(cands) };
  } else {
    throw new Error('unknown op: ' + op);
  }
  process.stdout.write(JSON.stringify(result));
});
