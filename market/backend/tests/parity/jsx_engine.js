// jsx_engine.js — pure-math extracts from spread_optimizer_v4_7_html.jsx.
// No React, no DOM, no JSX. Functions copied verbatim with their bodies
// intact so the JSX engine is the ground-truth math layer for the parity
// test framework. If you change anything here, the parity test loses its
// meaning.

// ==============================================
// CONSTANTS (verbatim from JSX)
// ==============================================
const STRATEGIES = {
  bull_put: { name: 'Bull Put Spread', short: 'Bull Put', type: 'credit', fits: ['bullish', 'mild_bullish', 'neutral'] },
  bear_call: { name: 'Bear Call Spread', short: 'Bear Call', type: 'credit', fits: ['bearish', 'mild_bearish', 'neutral'] },
  iron_condor: { name: 'Iron Condor', short: 'Iron Condor', type: 'credit', fits: ['neutral', 'mild_bullish', 'mild_bearish'] },
  iron_butterfly: { name: 'Iron Butterfly', short: 'Iron Fly', type: 'credit', fits: ['neutral'] },
  bull_call: { name: 'Bull Call Spread', short: 'Bull Call', type: 'debit', fits: ['bullish', 'mild_bullish'] },
  bear_put: { name: 'Bear Put Spread', short: 'Bear Put', type: 'debit', fits: ['bearish', 'mild_bearish'] },
  call_butterfly: { name: 'Call Butterfly', short: 'Call Fly', type: 'debit', fits: ['neutral', 'mild_bullish'] },
  put_butterfly: { name: 'Put Butterfly', short: 'Put Fly', type: 'debit', fits: ['neutral', 'mild_bearish'] },
};

const BIAS_TO_STRATEGY = { bullish: 'bull_put', mild_bullish: 'bull_put', neutral: 'iron_condor', mild_bearish: 'bear_call', bearish: 'bear_call' };

const WIDTH_PREFS = {
  tight: { name: 'Tight', desc: 'Min credit, min capital. Theta-only — needs time.', factor: 1.0 },
  balanced: { name: 'Balanced', desc: 'Recommended. Both delta + theta work.', factor: 1.5 },
  generous: { name: 'Generous', desc: 'More capital, smoothest P&L curve.', factor: 2.5 },
};

const HEALTH_BADGES = {
  healthy: { label: '✓ Healthy', color: 'emerald' },
  thin: { label: '⚠ Thin', color: 'amber' },
  directional: { label: '⚠ Directional', color: 'amber' },
  broken: { label: '⚠ Broken', color: 'rose' },
  capital_trap: { label: '⚠ Capital Trap', color: 'rose' },
};

const WALL_STRENGTH_MULT = { high: 1.0, medium: 0.5, low: 0.25 };

const _DEALER_CONTRACT_MULT = 100;

const LIMIT_EDGE_TIERS = [
  { name: 'modest',   pct: 0.0075, hint: 'often fills' },
  { name: 'balanced', pct: 0.0150, hint: 'patient' },
  { name: 'strong',   pct: 0.0300, hint: 'on dislocations' },
];

const COMPOSITE_WEIGHTS = {
  CENTER:           50,
  EV_MAX:           40,
  EV_MULT:          2,
  BADGE_HEALTHY:    15,
  BADGE_NEUTRAL:    -5,
  BADGE_DISQUAL:    -30,
  LIQ_HIGH:         10,
  LIQ_MID:          0,
  LIQ_LOW:          -10,
  LIMIT_NEG_EV_MAX: 30,
  LIMIT_POS_EV_MAX: 10,
  POP_TIEBREAK_DIV: 10,
};

const TRADEABLE_THRESHOLD = 60;
const SKIP_THRESHOLD       = 40;

// ==============================================
// HELPERS
// ==============================================
const _num = (v) => (v == null || v === '' ? null : Number(v));

// ==============================================
// DEALER GEX / DEX / VEX / TEX AGGREGATOR
// ==============================================
function _emptyDealerExposures() {
  return {
    exposures_by_date: {},
    portfolio_totals: { net_gex: 0, net_dex: 0, net_vex: 0, net_tex: 0 },
    key_levels: { call_wall: null, put_wall: null, vex_wall: null, tex_wall: null },
  };
}

function _computeDealerExposures(contracts, spot) {
  if (!Array.isArray(contracts) || contracts.length === 0 || !spot || spot <= 0) {
    return _emptyDealerExposures();
  }
  const buckets = new Map();
  for (const c of contracts) {
    const exp = c.expiration;
    const strike = c.strike;
    const side = (c.side || '').toLowerCase();
    if (exp == null || strike == null || (side !== 'call' && side !== 'put')) continue;
    if (!buckets.has(exp)) buckets.set(exp, new Map());
    const sm = buckets.get(exp);
    if (!sm.has(strike)) sm.set(strike, {});
    sm.get(strike)[side] = c;
  }
  if (buckets.size === 0) return _emptyDealerExposures();

  const spotSq = spot * spot;
  const exposures_by_date = {};
  let portNetGex = 0, portNetDex = 0, portNetVex = 0, portNetTex = 0;
  const allStrikesGex = new Map();
  const allStrikesVex = new Map();
  const allStrikesTex = new Map();

  for (const [exp, sm] of buckets) {
    const sortedStrikes = [...sm.keys()].sort((a, b) => a - b);
    const by_strike = [];
    let expNetGex = 0, expNetDex = 0, expNetVex = 0, expNetTex = 0;
    for (const strike of sortedStrikes) {
      const sides = sm.get(strike);
      const call = sides.call || {};
      const put = sides.put || {};
      const cG = Number(call.gamma) || 0;
      const pG = Number(put.gamma) || 0;
      const cD = Number(call.delta) || 0;
      const pD = Number(put.delta) || 0;
      const cV = Number(call.vega)  || 0;
      const pV = Number(put.vega)   || 0;
      const cT = Number(call.theta) || 0;
      const pT = Number(put.theta)  || 0;
      const cOi = Number(call.open_interest) || 0;
      const pOi = Number(put.open_interest) || 0;
      const callGex = cG * cOi * _DEALER_CONTRACT_MULT * spotSq;
      const putGex  = pG * pOi * _DEALER_CONTRACT_MULT * spotSq;
      const netGex  = putGex - callGex;
      const callDex = cD * cOi * _DEALER_CONTRACT_MULT * spot;
      const putDex  = pD * pOi * _DEALER_CONTRACT_MULT * spot;
      const netDex  = callDex - putDex;
      const callVex = cV * cOi * _DEALER_CONTRACT_MULT;
      const putVex  = pV * pOi * _DEALER_CONTRACT_MULT;
      const netVex  = putVex - callVex;
      const callTex = cT * cOi * _DEALER_CONTRACT_MULT;
      const putTex  = pT * pOi * _DEALER_CONTRACT_MULT;
      const netTex  = putTex - callTex;
      by_strike.push({
        strike,
        call_gex: callGex, put_gex: putGex, net_gex: netGex,
        call_dex: callDex, put_dex: putDex, net_dex: netDex,
        call_vex: callVex, put_vex: putVex, net_vex: netVex,
        call_tex: callTex, put_tex: putTex, net_tex: netTex,
        call_oi: cOi, put_oi: pOi,
      });
      expNetGex += netGex;
      expNetDex += netDex;
      expNetVex += netVex;
      expNetTex += netTex;
      allStrikesGex.set(strike, (allStrikesGex.get(strike) || 0) + netGex);
      allStrikesVex.set(strike, (allStrikesVex.get(strike) || 0) + netVex);
      allStrikesTex.set(strike, (allStrikesTex.get(strike) || 0) + netTex);
    }
    exposures_by_date[exp] = {
      by_strike,
      totals: { net_gex: expNetGex, net_dex: expNetDex, net_vex: expNetVex, net_tex: expNetTex },
    };
    portNetGex += expNetGex;
    portNetDex += expNetDex;
    portNetVex += expNetVex;
    portNetTex += expNetTex;
  }

  let callWall = null, putWall = null;
  let bestCallGex = 0, bestPutGex = 0;
  for (const [strike, gex] of allStrikesGex) {
    if (strike > spot && gex < bestCallGex) { bestCallGex = gex; callWall = { strike, gex }; }
    if (strike < spot && gex > bestPutGex)  { bestPutGex  = gex; putWall  = { strike, gex }; }
  }
  let vexWall = null, bestAbsVex = 0;
  for (const [strike, vex] of allStrikesVex) {
    if (Math.abs(vex) > bestAbsVex) { bestAbsVex = Math.abs(vex); vexWall = { strike, vex }; }
  }
  let texWall = null, bestAbsTex = 0;
  for (const [strike, tex] of allStrikesTex) {
    if (Math.abs(tex) > bestAbsTex) { bestAbsTex = Math.abs(tex); texWall = { strike, tex }; }
  }

  return {
    exposures_by_date,
    portfolio_totals: { net_gex: portNetGex, net_dex: portNetDex, net_vex: portNetVex, net_tex: portNetTex },
    key_levels: { call_wall: callWall, put_wall: putWall, vex_wall: vexWall, tex_wall: texWall },
  };
}

// ==============================================
// WALL / BIAS HELPERS
// ==============================================
function _strikeRowsFrom(exposuresForChosen) {
  if (!exposuresForChosen || typeof exposuresForChosen !== 'object') return [];
  for (const k of ['by_strike', 'strikes', 'rows', 'data']) {
    if (Array.isArray(exposuresForChosen[k])) return exposuresForChosen[k];
  }
  if (Array.isArray(exposuresForChosen)) return exposuresForChosen;
  return [];
}

function _gexFromRow(row) {
  return Number(row?.gex ?? row?.GEX ?? row?.net_gex ?? row?.gamma_exposure ?? 0);
}
function _dexFromRow(row) {
  return Number(row?.dex ?? row?.DEX ?? row?.net_dex ?? row?.delta_exposure ?? 0);
}
function _strikeFromRow(row) {
  return Number(row?.strike ?? row?.strike_price ?? row?.K);
}

function _findGexWallsBilateral(greeksRaw, exposuresForChosen) {
  const rows = _strikeRowsFrom(exposuresForChosen);
  const enriched = rows.map(r => ({
    strike: _strikeFromRow(r),
    netGex: _gexFromRow(r),
    callGex: Number((r && (r.call_gex !== undefined ? r.call_gex : r.callGex)) || 0),
    putGex:  Number((r && (r.put_gex  !== undefined ? r.put_gex  : r.putGex))  || 0),
  })).filter(o => o.strike != null && !isNaN(o.strike));

  if (enriched.length === 0) return { putWall: null, callWall: null };

  let totalAbs = 0;
  for (const o of enriched) totalAbs += Math.abs(o.netGex);

  const classify = (gexAbs) => {
    if (!gexAbs || !totalAbs) return 'low';
    const share = gexAbs / totalAbs;
    return share > 0.20 ? 'high' : share > 0.10 ? 'medium' : 'low';
  };

  let putWall = null;
  for (const o of enriched) {
    if (o.netGex > 0 && (!putWall || o.netGex > putWall.netGex)) putWall = Object.assign({}, o);
  }
  if (putWall) putWall.strength = classify(Math.abs(putWall.netGex));

  let callWall = null;
  for (const o of enriched) {
    if (o.netGex < 0 && (!callWall || o.netGex < callWall.netGex)) callWall = Object.assign({}, o);
  }
  if (callWall) callWall.strength = classify(Math.abs(callWall.netGex));

  return { putWall, callWall };
}

function _computeEdgelaneProviderScore(spot, putWall, callWall) {
  if (!spot || !putWall || !putWall.strike) {
    if (callWall && callWall.strike) {
      const callDist = (callWall.strike - spot) / spot;
      const sm = callWall.strength === 'high' ? 1.5 : callWall.strength === 'medium' ? 1.0 : 0.5;
      return Math.max(-30, Math.min(30, Math.round(-Math.sign(callDist) * 20 * sm * 10) / 10));
    }
    return 0;
  }
  const putDistPct = (putWall.strike - spot) / spot;
  const putDir = Math.sign(putDistPct);
  const distAbs = Math.abs(putDistPct);

  const reach = distAbs < 0.005 ? 1.0
              : distAbs < 0.01  ? 0.80
              : distAbs < 0.02  ? 0.55
              : distAbs < 0.03  ? 0.35
              : 0.15;

  const sm = putWall.strength === 'high' ? 1.5 : putWall.strength === 'medium' ? 1.0 : 0.5;
  let magnetScore = putDir * 90 * sm * reach;

  if (callWall && callWall.strike) {
    const callDistPct = (callWall.strike - spot) / spot;
    const sameSide = Math.sign(callDistPct) === putDir;
    const closer   = Math.abs(callDistPct) < distAbs;
    if (sameSide && closer) {
      const callMag = Math.abs(callWall.netGex);
      const putMag  = Math.abs(putWall.netGex);
      const blockRatio = callMag / Math.max(callMag + putMag, 1e-9);
      magnetScore *= (1 - blockRatio * 0.75);
    }
  }
  return Math.max(-100, Math.min(100, Math.round(magnetScore * 10) / 10));
}

function _findGexWall(greeksRaw, exposuresForChosen) {
  const kl = greeksRaw?.key_levels || {};
  const atlasCallWall = Number(kl.call_wall ?? kl.callWall ?? kl.gex_call_wall ?? 0) || null;
  const atlasPutWall  = Number(kl.put_wall  ?? kl.putWall  ?? kl.gex_put_wall  ?? 0) || null;

  const rows = _strikeRowsFrom(exposuresForChosen);
  const gexByStrike = rows
    .map(r => ({
      strike: _strikeFromRow(r),
      gex: _gexFromRow(r),
      callGex: Number(r?.call_gex ?? r?.callGex ?? 0) || 0,
      putGex:  Number(r?.put_gex  ?? r?.putGex  ?? 0) || 0,
    }))
    .filter(o => o.strike != null && !isNaN(o.strike));

  let best = null;
  for (const o of gexByStrike) {
    if (best == null || Math.abs(o.gex) > Math.abs(best.gex)) best = o;
  }

  const sortedAbs = gexByStrike.map(o => Math.abs(o.gex)).sort((a,b) => b-a);
  let strength = 'low';
  if (sortedAbs.length >= 2 && sortedAbs[1] > 0) {
    const ratio = sortedAbs[0] / sortedAbs[1];
    strength = ratio > 2 ? 'high' : ratio > 1.2 ? 'medium' : 'low';
  } else if (sortedAbs.length === 1 && sortedAbs[0] > 0) {
    strength = 'high';
  }

  let wallType = null;
  if (best) {
    const cgex = Math.abs(best.callGex);
    const pgex = Math.abs(best.putGex);
    if (cgex + pgex > 0) {
      const dominanceRatio = Math.max(cgex, pgex) / Math.max(1e-9, Math.min(cgex, pgex));
      if (dominanceRatio < 1.3) {
        wallType = 'mixed';
      } else if (pgex > cgex) {
        wallType = 'put';
      } else {
        wallType = 'call';
      }
    }
  }

  const wallStrike = best?.strike ?? atlasCallWall ?? atlasPutWall ?? null;
  if (best == null && wallStrike != null) {
    wallType = (wallStrike === atlasCallWall) ? 'call' : (wallStrike === atlasPutWall) ? 'put' : null;
  }

  return { strike: wallStrike, strength, type: wallType, atlasCallWall, atlasPutWall, computedTopGex: best?.gex || 0 };
}

function _aggregateExposures(exposuresForChosen, portfolioTotals) {
  const rows = _strikeRowsFrom(exposuresForChosen);
  let netGex = 0, netDex = 0;
  for (const r of rows) { netGex += _gexFromRow(r); netDex += _dexFromRow(r); }
  if (rows.length === 0 && portfolioTotals) {
    netGex = Number(portfolioTotals.net_gex ?? portfolioTotals.gex ?? netGex);
    netDex = Number(portfolioTotals.net_dex ?? portfolioTotals.dex ?? netDex);
  }
  return { netGex, netDex };
}

function _computeDirectionalScore(spot, wall, netGex, netDex, dte) {
  if (!spot || wall.strike == null) return 0;
  const pctFromWall = (spot - wall.strike) / wall.strike;
  let score = pctFromWall * 200;

  const strengthMult = wall.strength === 'high' ? 1.5 : wall.strength === 'medium' ? 1.0 : 0.5;
  score *= strengthMult;

  const intraday = dte != null && dte < 1.5;
  if (!intraday && netGex > 0) {
    score *= 0.3;
    if (netDex !== 0) score += 10 * Math.sign(netDex);
  } else if (intraday) {
    const distPct = Math.abs(pctFromWall);
    const reach = distPct < 0.005 ? 6 : distPct < 0.01 ? 4 : distPct < 0.02 ? 2 : distPct < 0.03 ? 1 : 0.3;
    score = -score * 8 * reach;
    if (netDex !== 0) score += 15 * Math.sign(netDex);
  } else if (netGex < 0) {
    score *= 1.2;
    if (netDex !== 0) score += -10 * Math.sign(netDex);
  }

  return Math.max(-100, Math.min(100, Math.round(score * 10) / 10));
}

function _scoreToBiasLabel(score) {
  if (score >= 60)  return 'bullish';
  if (score >= 20)  return 'mild_bullish';
  if (score <= -60) return 'bearish';
  if (score <= -20) return 'mild_bearish';
  return 'neutral';
}

function _computeConfidence(score, wall, netDex, spot, dte) {
  const absScore = Math.abs(score);
  if (wall.strength === 'low' || wall.strike == null) return 'low';
  if (absScore < 15) return 'low';

  const wallDir = spot > wall.strike ? 1 : spot < wall.strike ? -1 : 0;
  const dexDir = -Math.sign(netDex);
  const aligned = wallDir !== 0 && dexDir !== 0 && wallDir === dexDir;

  const intraday = dte != null && dte < 1.5;
  const wallDistPct = (wall.strike != null && spot) ? Math.abs(spot - wall.strike) / spot : 0;
  const unreachableIntraday = intraday && wallDistPct > 0.015;

  let base;
  if (wall.strength === 'high' && absScore > 30 && aligned) base = 'high';
  else if (absScore < 25) base = 'low';
  else base = 'medium';

  if (unreachableIntraday) {
    if (base === 'high') return 'medium';
    if (base === 'medium') return 'low';
    return 'low';
  }
  return base;
}

function _recommendStrategies(biasLabel) {
  const fits = Object.entries(STRATEGIES)
    .filter(([_, s]) => s.fits.includes(biasLabel))
    .map(([k]) => k);
  const primary = BIAS_TO_STRATEGY[biasLabel];
  if (primary && fits.includes(primary)) {
    return [primary, ...fits.filter(k => k !== primary)].slice(0, 3);
  }
  return fits.slice(0, 3);
}

function _extractGreekWall(greeksRaw, exposuresForChosen, keyName, fallbackRowField) {
  const kl = greeksRaw?.key_levels || exposuresForChosen?.key_levels || {};
  const direct = kl[keyName];
  if (direct && (direct.strike != null)) {
    return { strike: Number(direct.strike), value: Number(direct[fallbackRowField] ?? direct.value ?? 0) };
  }
  const rows = _strikeRowsFrom(exposuresForChosen);
  let best = null;
  for (const r of rows) {
    const strike = _strikeFromRow(r);
    const val = Number(r?.[fallbackRowField] ?? r?.[fallbackRowField?.replace('net_', '')] ?? 0);
    if (strike == null || isNaN(strike)) continue;
    if (best == null || Math.abs(val) > Math.abs(best.value)) best = { strike, value: val };
  }
  return (best && best.value !== 0) ? best : null;
}

function _computeBiasSignals(symbol, expiration, spot, greeksRaw, exposuresForChosen, regimeOverride = null, chosenDte = null) {
  const wall = _findGexWall(greeksRaw, exposuresForChosen);
  const { putWall, callWall } = _findGexWallsBilateral(greeksRaw, exposuresForChosen);
  const fallback = _aggregateExposures(exposuresForChosen, greeksRaw?.portfolio_totals);
  const intraday = chosenDte != null && chosenDte < 1.5;
  const netGex = (!intraday && regimeOverride && regimeOverride.netGex != null) ? regimeOverride.netGex : fallback.netGex;
  const netDex = (!intraday && regimeOverride && regimeOverride.netDex != null) ? regimeOverride.netDex : fallback.netDex;
  const intradayForScore = chosenDte != null && chosenDte < 1.5;
  const score = intradayForScore
    ? _computeEdgelaneProviderScore(spot, putWall, callWall)
    : _computeDirectionalScore(spot, wall, netGex, netDex, chosenDte);
  const biasLabel = _scoreToBiasLabel(score);
  const confidence = _computeConfidence(score, wall, netDex, spot, chosenDte);
  const recommended = _recommendStrategies(biasLabel);

  const vexWall = _extractGreekWall(greeksRaw, exposuresForChosen, 'vex_wall', 'net_vex');
  const texWall = _extractGreekWall(greeksRaw, exposuresForChosen, 'tex_wall', 'net_tex');

  const wallSide = (wall.strike != null && spot != null)
    ? (spot > wall.strike ? 'above' : spot < wall.strike ? 'below' : 'at')
    : 'unknown';
  const gammaRegime = netGex > 0 ? 'positive (pinning)' : netGex < 0 ? 'negative (amplifying)' : 'neutral';
  const dexSkewSide = netDex > 0 ? 'positive (dealer-long-delta)' : netDex < 0 ? 'negative (dealer-short-delta)' : 'flat';

  return {
    spot,
    directional_score: score,
    bias_label: biasLabel,
    confidence,
    net_gex: netGex,
    dte: chosenDte,
    gex_wall_strike: wall.strike,
    gex_wall_strength: wall.strength,
    gex_wall_type: wall.type,
    put_wall_strike:   putWall  ? putWall.strike   : null,
    put_wall_strength: putWall  ? putWall.strength : null,
    put_wall_net_gex:  putWall  ? putWall.netGex   : null,
    call_wall_strike:   callWall ? callWall.strike   : null,
    call_wall_strength: callWall ? callWall.strength : null,
    call_wall_net_gex:  callWall ? callWall.netGex   : null,

    vex_wall_strike: vexWall?.strike ?? null,
    vex_wall_value:  vexWall?.value  ?? null,
    tex_wall_strike: texWall?.strike ?? null,
    tex_wall_value:  texWall?.value  ?? null,
    recommended_strategies: recommended,
    _facts: { wallSide, gammaRegime, dexSkewSide, netGex, netDex, wall, vexWall, texWall },
  };
}

// ==============================================
// STRATEGY MATH HELPERS
// ==============================================
const getMid = (c) => (c.mid != null ? c.mid : (c.bid + c.ask) / 2);

function widthBaseForDTE(dte, expectedMove) {
  if (dte < 1) return 0.4 * expectedMove;
  if (dte <= 7) return 1.0 * expectedMove;
  return 1.5 * expectedMove;
}

function findStrikeByDelta(contracts, side, targetAbsDelta) {
  let best = null, bestDiff = Infinity;
  for (const c of contracts) {
    if (c.side !== side || c.delta == null || c.bid <= 0) continue;
    const diff = Math.abs(Math.abs(c.delta) - targetAbsDelta);
    if (diff < bestDiff) { bestDiff = diff; best = c; }
  }
  return best;
}

function findClosestStrike(contracts, side, target, condition) {
  let best = null, bestDiff = Infinity;
  for (const c of contracts) {
    if (c.side !== side || c.bid <= 0) continue;
    if (condition && !condition(c)) continue;
    const diff = Math.abs(c.strike - target);
    if (diff < bestDiff) { bestDiff = diff; best = c; }
  }
  return best;
}

function classifyHealth(strategyType, netDelta, dte, maxLoss, maxProfit) {
  if (strategyType === 'debit') {
    const ratio = maxProfit / Math.max(0.01, maxLoss);
    if (dte <= 2)         return 'broken';
    if (ratio < 0.30)     return 'capital_trap';
    if (ratio < 0.60)     return 'thin';
    return 'healthy';
  }
  if (netDelta < 0.05 && dte <= 5) return 'broken';
  if (maxLoss > 5 * maxProfit)     return 'capital_trap';
  if (netDelta < 0.10)             return 'thin';
  if (netDelta > 0.30)             return 'directional';
  return 'healthy';
}

function classifyLiquidity(...legs) {
  const minOI = Math.min(...legs.map(l => l.open_interest || 0));
  if (minOI > 200) return 'high';
  if (minOI > 50) return 'mid';
  return 'low';
}

function healthExplanation(strategyType, h, netDelta, dte, maxLoss, maxProfit) {
  if (strategyType === 'debit') {
    const ratio = (maxProfit / Math.max(0.01, maxLoss)).toFixed(2);
    if (h === 'broken')        return `DTE ${dte} too short for a debit — directional move can't play out before gamma dominates.`;
    if (h === 'capital_trap')  return `Payoff/debit ${ratio} — paid too much premium relative to width; breakeven is hard to reach.`;
    if (h === 'thin')          return `Payoff/debit ${ratio} — mediocre return; needs a strong directional move to justify the cost.`;
    return `Payoff/debit ${ratio} — debit reasonable for the width and DTE. Manage size by the debit paid.`;
  }
  const d = netDelta.toFixed(3);
  if (h === 'broken')        return `Net Δ ${d} with ${dte} DTE — gamma will dominate before theta delivers.`;
  if (h === 'capital_trap')  return `Max loss ${(maxLoss/maxProfit).toFixed(1)}× max profit — one loss undoes many wins.`;
  if (h === 'thin')          return `Net Δ ${d} — only theta works. Needs days, not hours.`;
  if (h === 'directional')   return `Net Δ ${d} — directional bet, not premium harvest.`;
  return `Net Δ ${d} sits in the working zone. Theta + delta both contribute.`;
}

function _computeLimitPremiums(candidate) {
  const { type, pop_pct, max_profit, max_loss, net_premium, wall_penalty } = candidate;
  const pop = (pop_pct || 0) / 100;
  const factor = Math.max(0.01, wall_penalty?.factor ?? 1.0);
  const width = (max_profit || 0) + (max_loss || 0);
  if (width <= 0 || pop <= 0 || pop >= 1) return null;

  const isCredit = type === 'credit';
  const breakeven = isCredit ? (1 - pop) * width : pop * width;
  const current   = net_premium;

  const tiers = LIMIT_EDGE_TIERS.map(t => {
    const targetEV = t.pct * width;
    const target = isCredit
      ? breakeven + targetEV / factor
      : breakeven - targetEV / factor;
    const delta    = isCredit ? target - current : current - target;
    const feasible = isCredit ? target < width   : target > 0.05;
    return {
      name: t.name,
      hint: t.hint,
      pctOfWidth: t.pct,
      targetEV,
      target,
      delta,
      feasible,
    };
  });

  return {
    side: isCredit ? 'credit' : 'debit',
    current,
    breakeven,
    tiers,
    target:    tiers[0].target,
    targetEV:  tiers[0].targetEV,
    delta:     tiers[0].delta,
    feasible:  tiers[0].feasible,
  };
}

function computeWallPenalty(strategy, strikes, breakevens, wallStrike, wallStrength, netGex = 0, dte = null) {
  if (!wallStrike || !strikes) return { factor: 1.0, reason: null, verdict: 'neutral' };
  const w = wallStrike;
  const s = WALL_STRENGTH_MULT[wallStrength] ?? 0.5;
  const fmt = (v) => Number(v).toFixed(2);
  const intraday = dte != null && dte < 1.5;
  const longGamma = !intraday && netGex > 0;

  switch (strategy) {
    case 'bull_put': {
      const sp = strikes.short_put;
      if (sp == null) return { factor: 1.0, reason: null, verdict: 'neutral' };
      if (w > sp) return { factor: 1.0, reason: `Wall ${fmt(w)} above short put — acts as upward support.`, verdict: 'good' };
      if (Math.abs(w - sp) < 0.01) return { factor: 1 - 0.4 * s, reason: `Wall AT short put ${fmt(sp)} — high pin risk on the short strike.`, verdict: 'bad' };
      if (longGamma) {
        return {
          factor: 1.0,
          reason: `Wall ${fmt(w)} below short put ${fmt(sp)} in long-gamma regime — put wall acts as support floor below the spread, stabilizing.`,
          verdict: 'good',
        };
      }
      const distPct = (sp - w) / sp;
      const proximity = Math.max(0, 1 - distPct * 10);
      return {
        factor: 1 - 0.4 * proximity * s,
        reason: `Wall ${fmt(w)} below short put ${fmt(sp)} (short-gamma) — no upward pull; price can drift through.`,
        verdict: proximity > 0.5 ? 'bad' : 'warn',
      };
    }

    case 'bear_call': {
      const sc = strikes.short_call;
      if (sc == null) return { factor: 1.0, reason: null, verdict: 'neutral' };
      if (w < sc) return { factor: 1.0, reason: `Wall ${fmt(w)} below short call — caps upside as resistance.`, verdict: 'good' };
      if (Math.abs(w - sc) < 0.01) return { factor: 1 - 0.4 * s, reason: `Wall AT short call ${fmt(sc)} — high pin risk on the short strike.`, verdict: 'bad' };
      if (longGamma) {
        return {
          factor: 1.0,
          reason: `Wall ${fmt(w)} above short call ${fmt(sc)} in long-gamma regime — call wall acts as resistance ceiling above the spread, stabilizing.`,
          verdict: 'good',
        };
      }
      const distPct = (w - sc) / sc;
      const proximity = Math.max(0, 1 - distPct * 10);
      return {
        factor: 1 - 0.4 * proximity * s,
        reason: `Wall ${fmt(w)} above short call ${fmt(sc)} (short-gamma) — no downward push; price can drift through.`,
        verdict: proximity > 0.5 ? 'bad' : 'warn',
      };
    }

    case 'iron_condor': {
      const sp = strikes.short_put, sc = strikes.short_call;
      if (sp == null || sc == null) return { factor: 1.0, reason: null, verdict: 'neutral' };
      if (w >= sp && w <= sc) {
        const mid = (sp + sc) / 2;
        const half = (sc - sp) / 2 || 1;
        const offCenter = Math.abs(w - mid) / half;
        const centered = 1 - offCenter;
        if (centered > 0.7) return { factor: 1.0, reason: `Wall ${fmt(w)} centered between shorts ${fmt(sp)}/${fmt(sc)} — ideal pin.`, verdict: 'good' };
        const skewReason = longGamma
          ? `Wall ${fmt(w)} inside body ${fmt(sp)}/${fmt(sc)} but skewed (long-gamma) — anchors price toward nearer short.`
          : `Wall ${fmt(w)} inside body ${fmt(sp)}/${fmt(sc)} but skewed — pinning will pull price toward nearer short.`;
        return { factor: 1 - 0.3 * (1 - centered) * s, reason: skewReason, verdict: 'warn' };
      }
      const dist = w < sp ? sp - w : w - sc;
      const distPct = dist / ((sp + sc) / 2);
      const proximity = Math.max(0, 1 - distPct * 10);
      const sideDesc = w < sp ? 'below body' : 'above body';
      const reason = longGamma
        ? `Wall ${fmt(w)} ${sideDesc} ${fmt(sp)}/${fmt(sc)} in long-gamma regime — anchors price ${(distPct*100).toFixed(1)}% from the profit zone.`
        : `Wall ${fmt(w)} OUTSIDE shorts ${fmt(sp)}/${fmt(sc)} (short-gamma) — pinning drags price out of profit zone.`;
      return { factor: 1 - 0.5 * proximity * s, reason, verdict: 'bad' };
    }

    case 'iron_butterfly':
    case 'call_butterfly':
    case 'put_butterfly': {
      const center = strikes.center;
      if (center == null) return { factor: 1.0, reason: null, verdict: 'neutral' };
      const offsetPct = Math.abs(w - center) / center;
      if (offsetPct < 0.005) return { factor: 1.0, reason: `Wall ${fmt(w)} pinned at center ${fmt(center)} — ideal alignment.`, verdict: 'good' };
      const proximity = Math.max(0, 1 - offsetPct * 30);
      return {
        factor: 1 - 0.5 * (1 - proximity) * s,
        reason: `Wall ${fmt(w)} ${(offsetPct * 100).toFixed(1)}% off center ${fmt(center)} — pin misses target.`,
        verdict: proximity < 0.3 ? 'bad' : 'warn',
      };
    }

    case 'bull_call': {
      const sc = strikes.short_call;
      const lp = strikes.long_call ?? strikes.lp;
      const be = breakevens?.[0];
      if (sc == null || be == null) return { factor: 1.0, reason: null, verdict: 'neutral' };
      if (w >= sc) return {
        factor: 1.0,
        reason: `Wall ${fmt(w)} above short call ${fmt(sc)} — pulls price past max-profit cap.`,
        verdict: 'good',
      };
      if (w >= be) return {
        factor: 1.0,
        reason: `Wall ${fmt(w)} inside profit zone ${fmt(be)}–${fmt(sc)} — pulls price into profit.`,
        verdict: 'good',
      };
      const distFromBe = (be - w) / be;
      const proximity = Math.max(0, 1 - distFromBe * 5);
      return {
        factor: 1 - 0.5 * proximity * s,
        reason: `Wall ${fmt(w)} below breakeven ${fmt(be)} — pulls price AWAY from profit zone.`,
        verdict: proximity > 0.4 ? 'bad' : 'warn',
      };
    }

    case 'bear_put': {
      const sp = strikes.short_put;
      const be = breakevens?.[0];
      if (sp == null || be == null) return { factor: 1.0, reason: null, verdict: 'neutral' };
      if (w <= sp) return {
        factor: 1.0,
        reason: `Wall ${fmt(w)} below short put ${fmt(sp)} — pulls price past max-profit cap.`,
        verdict: 'good',
      };
      if (w <= be) return {
        factor: 1.0,
        reason: `Wall ${fmt(w)} inside profit zone ${fmt(sp)}–${fmt(be)} — pulls price into profit.`,
        verdict: 'good',
      };
      const distFromBe = (w - be) / be;
      const proximity = Math.max(0, 1 - distFromBe * 5);
      return {
        factor: 1 - 0.5 * proximity * s,
        reason: `Wall ${fmt(w)} above breakeven ${fmt(be)} — pulls price AWAY from profit zone.`,
        verdict: proximity > 0.4 ? 'bad' : 'warn',
      };
    }

    default:
      return { factor: 1.0, reason: null, verdict: 'neutral' };
  }
}

function scoreVertical(short, long, type, dte) {
  if (!short || !long) return null;
  const width = Math.abs(short.strike - long.strike);
  if (width <= 0) return null;
  const sMid = getMid(short), lMid = getMid(long);

  let netPremium, maxProfit, maxLoss;
  if (type === 'credit') {
    netPremium = sMid - lMid;
    if (netPremium <= 0.01) return null;
    maxProfit = netPremium; maxLoss = width - netPremium;
  } else {
    netPremium = lMid - sMid;
    if (netPremium <= 0.01) return null;
    maxProfit = width - netPremium; maxLoss = netPremium;
  }

  const sDelta = Math.abs(short.delta), lDelta = Math.abs(long.delta);
  const netDelta = sDelta - lDelta;
  const netTheta = (short.theta - long.theta);

  let popPct;
  if (type === 'credit') popPct = (1 - sDelta) * 100;
  else {
    const frac = Math.min(1, netPremium / width);
    popPct = Math.max(0, Math.min(100, (lDelta * (1 - frac) + sDelta * frac) * 100));
  }
  const ev = (popPct / 100) * maxProfit - (1 - popPct / 100) * maxLoss;

  let breakevens = [];
  if (type === 'credit' && short.side === 'put') breakevens = [short.strike - netPremium];
  else if (type === 'credit' && short.side === 'call') breakevens = [short.strike + netPremium];
  else if (type === 'debit' && long.side === 'call') breakevens = [long.strike + netPremium];
  else if (type === 'debit' && long.side === 'put') breakevens = [long.strike - netPremium];

  const health = classifyHealth(type, netDelta, dte, maxLoss, maxProfit);
  const liquidity = classifyLiquidity(short, long);
  const sideChar = short.side[0].toUpperCase();

  const strikes = {
    short_put: short.side === 'put' ? short.strike : null,
    long_put: long.side === 'put' ? long.strike : null,
    short_call: short.side === 'call' ? short.strike : null,
    long_call: long.side === 'call' ? long.strike : null,
    center: null,
  };

  return {
    structure_text: type === 'credit'
      ? `Short ${short.strike}${sideChar} / Long ${long.strike}${sideChar}`
      : `Long ${long.strike}${sideChar} / Short ${short.strike}${sideChar}`,
    width, type, net_premium: netPremium, max_profit: maxProfit, max_loss: maxLoss,
    capital_required: maxLoss, rr_ratio: maxProfit / maxLoss,
    pop_pct: popPct, ev, breakevens, liquidity,
    short_delta: sDelta, long_delta: lDelta,
    net_spread_delta: netDelta, net_theta_dollar: netTheta,
    strikes,
    dte,
    legs: [
      { strike: long.strike,  side: long.side,  longShort:  1, iv: long.iv  > 0 ? long.iv  / 100 : null, symbol: long.symbol  ?? null },
      { strike: short.strike, side: short.side, longShort: -1, iv: short.iv > 0 ? short.iv / 100 : null, symbol: short.symbol ?? null },
    ],
    health, health_explanation: healthExplanation('credit', health, netDelta, dte, maxLoss, maxProfit),
  };
}

function scoreCondor(sp, lp, sc, lc, dte) {
  if (!sp || !lp || !sc || !lc) return null;
  const putWidth = sp.strike - lp.strike;
  const callWidth = lc.strike - sc.strike;
  if (putWidth <= 0 || callWidth <= 0) return null;
  const putCredit = getMid(sp) - getMid(lp);
  const callCredit = getMid(sc) - getMid(lc);
  const netPremium = putCredit + callCredit;
  if (netPremium <= 0.01) return null;
  const maxLoss = Math.max(putWidth, callWidth) - netPremium;

  const popPct = Math.max(0, (1 - Math.abs(sp.delta) - Math.abs(sc.delta)) * 100);
  const ev = (popPct / 100) * netPremium - (1 - popPct / 100) * maxLoss;
  const putNetD = Math.abs(sp.delta) - Math.abs(lp.delta);
  const callNetD = Math.abs(sc.delta) - Math.abs(lc.delta);
  const netDelta = Math.max(putNetD, callNetD);
  const netTheta = (sp.theta - lp.theta) + (sc.theta - lc.theta);
  const health = classifyHealth('credit', netDelta, dte, maxLoss, netPremium);
  const liquidity = classifyLiquidity(sp, lp, sc, lc);

  const strikes = {
    short_put: sp.strike, long_put: lp.strike,
    short_call: sc.strike, long_call: lc.strike,
    center: (sp.strike + sc.strike) / 2,
  };

  return {
    structure_text: `${lp.strike}/${sp.strike}P + ${sc.strike}/${lc.strike}C`,
    width: Math.max(putWidth, callWidth), type: 'credit',
    net_premium: netPremium, max_profit: netPremium, max_loss: maxLoss,
    capital_required: maxLoss, rr_ratio: netPremium / maxLoss,
    pop_pct: popPct, ev,
    breakevens: [sp.strike - netPremium, sc.strike + netPremium],
    liquidity,
    short_delta: Math.max(Math.abs(sp.delta), Math.abs(sc.delta)),
    long_delta: Math.max(Math.abs(lp.delta), Math.abs(lc.delta)),
    net_spread_delta: netDelta, net_theta_dollar: netTheta,
    strikes,
    dte,
    legs: [
      { strike: lp.strike, side: 'put',  longShort:  1, iv: lp.iv > 0 ? lp.iv / 100 : null, symbol: lp.symbol ?? null },
      { strike: sp.strike, side: 'put',  longShort: -1, iv: sp.iv > 0 ? sp.iv / 100 : null, symbol: sp.symbol ?? null },
      { strike: sc.strike, side: 'call', longShort: -1, iv: sc.iv > 0 ? sc.iv / 100 : null, symbol: sc.symbol ?? null },
      { strike: lc.strike, side: 'call', longShort:  1, iv: lc.iv > 0 ? lc.iv / 100 : null, symbol: lc.symbol ?? null },
    ],
    health, health_explanation: healthExplanation('credit', health, netDelta, dte, maxLoss, netPremium),
  };
}

function scoreButterfly(low, mid, high, side, dte) {
  if (!low || !mid || !high) return null;
  const w1 = mid.strike - low.strike;
  const w2 = high.strike - mid.strike;
  if (Math.abs(w1 - w2) > 0.5 * Math.min(w1, w2)) return null;
  const debit = getMid(low) + getMid(high) - 2 * getMid(mid);
  if (debit <= 0.01) return null;
  const maxProfit = Math.min(w1, w2) - debit;
  const maxLoss = debit;
  const popPct = 25;
  const ev = (popPct / 100) * maxProfit - (1 - popPct / 100) * maxLoss;
  const netDelta = Math.abs(low.delta - 2 * mid.delta + high.delta);
  const netTheta = low.theta - 2 * mid.theta + high.theta;
  const health = classifyHealth('credit', netDelta, dte, maxLoss, maxProfit);
  const liquidity = classifyLiquidity(low, mid, high);
  const sideChar = side[0].toUpperCase();

  const strikes = {
    short_put: side === 'put' ? mid.strike : null,
    long_put: side === 'put' ? null : null,
    short_call: side === 'call' ? mid.strike : null,
    long_call: null,
    center: mid.strike,
  };

  return {
    structure_text: `${low.strike}/${mid.strike}/${high.strike} ${sideChar} fly`,
    width: Math.min(w1, w2), type: 'debit',
    net_premium: debit, max_profit: maxProfit, max_loss: maxLoss,
    capital_required: maxLoss, rr_ratio: maxProfit / maxLoss,
    pop_pct: popPct, ev,
    breakevens: [low.strike + debit, high.strike - debit],
    liquidity,
    short_delta: Math.abs(mid.delta), long_delta: (Math.abs(low.delta) + Math.abs(high.delta)) / 2,
    net_spread_delta: netDelta, net_theta_dollar: netTheta,
    strikes,
    dte,
    legs: [
      { strike: low.strike,  side, longShort:  1, iv: low.iv  > 0 ? low.iv  / 100 : null, symbol: low.symbol  ?? null },
      { strike: mid.strike,  side, longShort: -2, iv: mid.iv  > 0 ? mid.iv  / 100 : null, symbol: mid.symbol  ?? null },
      { strike: high.strike, side, longShort:  1, iv: high.iv > 0 ? high.iv / 100 : null, symbol: high.symbol ?? null },
    ],
    health, health_explanation: healthExplanation(health, netDelta, dte, maxLoss, maxProfit),
  };
}

function buildVertical(strategy, contracts, dte, expectedMove, targetDelta, widthFactor) {
  const baseW = widthBaseForDTE(dte, expectedMove);
  const desiredW = baseW * widthFactor;

  if (strategy === 'bull_put') {
    const s = findStrikeByDelta(contracts, 'put', targetDelta);
    if (!s) return null;
    const l = findClosestStrike(contracts, 'put', s.strike - desiredW, c => c.strike < s.strike);
    return scoreVertical(s, l, 'credit', dte);
  }
  if (strategy === 'bear_call') {
    const s = findStrikeByDelta(contracts, 'call', targetDelta);
    if (!s) return null;
    const l = findClosestStrike(contracts, 'call', s.strike + desiredW, c => c.strike > s.strike);
    return scoreVertical(s, l, 'credit', dte);
  }
  if (strategy === 'bull_call') {
    const long = findStrikeByDelta(contracts, 'call', Math.max(0.5, 1 - targetDelta));
    if (!long) return null;
    const short = findClosestStrike(contracts, 'call', long.strike + desiredW, c => c.strike > long.strike);
    return scoreVertical(short, long, 'debit', dte);
  }
  if (strategy === 'bear_put') {
    const long = findStrikeByDelta(contracts, 'put', Math.max(0.5, 1 - targetDelta));
    if (!long) return null;
    const short = findClosestStrike(contracts, 'put', long.strike - desiredW, c => c.strike < long.strike);
    return scoreVertical(short, long, 'debit', dte);
  }
  return null;
}

function buildIronCondor(contracts, dte, expectedMove, targetDelta, widthFactor) {
  const baseW = widthBaseForDTE(dte, expectedMove);
  const desiredW = baseW * widthFactor;
  const sp = findStrikeByDelta(contracts, 'put', targetDelta);
  const sc = findStrikeByDelta(contracts, 'call', targetDelta);
  if (!sp || !sc) return null;
  const lp = findClosestStrike(contracts, 'put', sp.strike - desiredW, c => c.strike < sp.strike);
  const lc = findClosestStrike(contracts, 'call', sc.strike + desiredW, c => c.strike > sc.strike);
  return scoreCondor(sp, lp, sc, lc, dte);
}

function buildIronButterfly(contracts, dte, expectedMove, widthFactor) {
  const baseW = widthBaseForDTE(dte, expectedMove);
  const desiredW = baseW * widthFactor;
  const sp = findStrikeByDelta(contracts, 'put', 0.50);
  const sc = findStrikeByDelta(contracts, 'call', 0.50);
  if (!sp || !sc) return null;
  const lp = findClosestStrike(contracts, 'put', sp.strike - desiredW, c => c.strike < sp.strike);
  const lc = findClosestStrike(contracts, 'call', sc.strike + desiredW, c => c.strike > sc.strike);
  return scoreCondor(sp, lp, sc, lc, dte);
}

function buildButterfly(strategy, contracts, dte, expectedMove, widthFactor) {
  const baseW = widthBaseForDTE(dte, expectedMove);
  const desiredW = baseW * widthFactor;
  const side = strategy === 'call_butterfly' ? 'call' : 'put';
  const center = findStrikeByDelta(contracts, side, 0.50);
  if (!center) return null;
  let low, high;
  if (side === 'call') {
    low = findClosestStrike(contracts, 'call', center.strike - desiredW, c => c.strike < center.strike);
    high = findClosestStrike(contracts, 'call', center.strike + desiredW, c => c.strike > center.strike);
  } else {
    low = findClosestStrike(contracts, 'put', center.strike - desiredW, c => c.strike < center.strike);
    high = findClosestStrike(contracts, 'put', center.strike + desiredW, c => c.strike > center.strike);
  }
  return scoreButterfly(low, center, high, side, dte);
}

function generateCandidates(strategy, contracts, dte, expectedMove, targetDelta, widthFactor, walls) {
  const builders = {
    bull_put: (d, w) => buildVertical('bull_put', contracts, dte, expectedMove, d, w),
    bear_call: (d, w) => buildVertical('bear_call', contracts, dte, expectedMove, d, w),
    bull_call: (d, w) => buildVertical('bull_call', contracts, dte, expectedMove, d, w),
    bear_put: (d, w) => buildVertical('bear_put', contracts, dte, expectedMove, d, w),
    iron_condor: (d, w) => buildIronCondor(contracts, dte, expectedMove, d, w),
    iron_butterfly: (d, w) => buildIronButterfly(contracts, dte, expectedMove, w),
    call_butterfly: (d, w) => buildButterfly('call_butterfly', contracts, dte, expectedMove, w),
    put_butterfly: (d, w) => buildButterfly('put_butterfly', contracts, dte, expectedMove, w),
  };
  const builder = builders[strategy];
  if (!builder) return [];

  const isFly = strategy === 'iron_butterfly' || strategy === 'call_butterfly' || strategy === 'put_butterfly';
  let configs;
  if (isFly) {
    configs = [
      { label: 'Conservative', delta: targetDelta, width: widthFactor * 1.5 },
      { label: 'Balanced', delta: targetDelta, width: widthFactor * 1.0 },
      { label: 'Aggressive', delta: targetDelta, width: widthFactor * 0.7 },
    ];
  } else {
    configs = [
      { label: 'Conservative', delta: Math.max(0.05, targetDelta - 0.10), width: widthFactor },
      { label: 'Balanced', delta: targetDelta, width: widthFactor },
      { label: 'Aggressive', delta: Math.min(0.45, targetDelta + 0.10), width: widthFactor },
    ];
  }

  const candidates = [];
  for (const cfg of configs) {
    const c = builder(cfg.delta, cfg.width);
    if (!c) continue;
    const wallPen = computeWallPenalty(strategy, c.strikes, c.breakevens, walls?.strike, walls?.strength, walls?.netGex || 0, walls?.dte);
    const evAdjusted = c.ev * wallPen.factor;
    let rationale = `${cfg.label} variant — short Δ target ${cfg.delta.toFixed(2)}, width factor ${cfg.width.toFixed(1)}×.`;
    if (wallPen.reason) rationale += ` ${wallPen.reason}`;
    const enriched = { ...c, label: cfg.label, rationale, wall_penalty: wallPen, ev_adjusted: evAdjusted };
    enriched.limit_premiums = _computeLimitPremiums(enriched);
    enriched.composite_score = _compositeScore(enriched);
    enriched.composite_verdict = _compositeVerdict(enriched.composite_score, enriched.ev_adjusted ?? enriched.ev);
    candidates.push(enriched);
  }
  return candidates;
}

function _compositeScore(c) {
  const W = COMPOSITE_WEIGHTS;
  const ev = c.ev_adjusted ?? c.ev ?? 0;

  const evScore = Math.max(0, Math.min(W.EV_MAX, ev * W.EV_MULT));

  const badgeScore = ({
    healthy:      W.BADGE_HEALTHY,
    thin:         W.BADGE_NEUTRAL,
    directional:  W.BADGE_NEUTRAL,
    broken:       W.BADGE_DISQUAL,
    capital_trap: W.BADGE_DISQUAL,
  })[c.health] ?? 0;

  const liqScore = ({ high: W.LIQ_HIGH, mid: W.LIQ_MID, low: W.LIQ_LOW })[c.liquidity] ?? 0;

  let limitScore = 0;
  if (c.limit_premiums?.feasible) {
    const lp = c.limit_premiums;
    const feasibility = Math.max(0, Math.min(1, 1 - Math.abs(lp.delta || 0) / Math.max(0.01, lp.current || 1)));
    limitScore = feasibility * (ev < 0 ? W.LIMIT_NEG_EV_MAX : W.LIMIT_POS_EV_MAX);
  }

  const popScore = ((c.pop_pct ?? 50) - 50) / W.POP_TIEBREAK_DIV;

  const total = W.CENTER + evScore + badgeScore + liqScore + limitScore + popScore;
  return Math.max(0, Math.min(100, Math.round(total * 10) / 10));
}

function _compositeVerdict(score, ev) {
  if (score >= TRADEABLE_THRESHOLD) {
    return ev >= 0
      ? { label: 'tradeable now',       mode: 'market', color: 'emerald' }
      : { label: 'tradeable on limit',  mode: 'limit',  color: 'emerald' };
  }
  if (score >= SKIP_THRESHOLD)  return { label: 'marginal',     mode: 'wait', color: 'amber' };
  return                               { label: 'do not trade', mode: 'skip', color: 'rose'  };
}

function pickBestCandidate(candidates) {
  if (!candidates.length) return null;
  const sorted = [...candidates].sort((a, b) => (b.composite_score ?? 0) - (a.composite_score ?? 0));
  return sorted[0]?.label;
}

module.exports = {
  STRATEGIES, BIAS_TO_STRATEGY, WALL_STRENGTH_MULT, HEALTH_BADGES, WIDTH_PREFS,
  _computeDealerExposures,
  _computeBiasSignals,
  generateCandidates,
  pickBestCandidate,
  computeWallPenalty,
  _computeEdgelaneProviderScore,
};
