import { useState, useEffect, useMemo, useCallback } from 'react';

// ==============================================
// CONSTANTS
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

// Why each strategy fits or doesn't fit a given bias.
// Keyed by bias_label, then strategy. Used to explain WHY a chip got its legend.
const STRATEGY_FIT_REASONS = {
  bullish: {
    bull_put: 'Sells put premium below price — collects credit as stock drifts up.',
    bear_call: 'Sells call premium above price — wrong side; rallies hit your short strike.',
    iron_condor: 'Sells both wings — call side at risk if upside continues, but pinning structure can still hold.',
    iron_butterfly: 'Pin trade — bullish drift breaks the pin, max profit zone too narrow for trending tape.',
    bull_call: 'Buys upside exposure — directly aligned with bullish momentum.',
    bear_put: 'Pays for downside — moves against you on a bullish tape.',
    call_butterfly: 'Pin trade with mild upside lean — trending tape walks past the center strike.',
    put_butterfly: 'Pin with bearish lean — wrong direction for bullish bias.',
  },
  mild_bullish: {
    bull_put: 'Slow drift up favors put-side theta decay — clean fit.',
    bear_call: 'Wrong side — even slow upside drift erodes call credit.',
    iron_condor: 'Range-bound element still present; both sides can earn if drift is mild.',
    iron_butterfly: 'Pin works only if drift stalls; risky when bias is directional even mildly.',
    bull_call: 'Cheap directional bet — pays off as drift continues.',
    bear_put: 'Pays for the wrong direction.',
    call_butterfly: 'Pin near current price — partial fit if drift is very small.',
    put_butterfly: 'Bearish lean conflicts with bullish drift.',
  },
  neutral: {
    bull_put: 'Slight upward drift assumption — works in flat tape but suboptimal vs. condor.',
    bear_call: 'Slight downward drift assumption — works in flat tape but suboptimal vs. condor.',
    iron_condor: 'Both wings out of money in flat tape — maximum premium harvested.',
    iron_butterfly: 'Tight pinning regime gives the highest credit, narrow but rich.',
    bull_call: 'Pays a debit but flat tape gives no movement to recover it.',
    bear_put: 'Pays a debit but flat tape gives no movement to recover it.',
    call_butterfly: 'Pin trade fits flat tape with bullish lean.',
    put_butterfly: 'Pin trade fits flat tape with bearish lean.',
  },
  mild_bearish: {
    bull_put: 'Wrong side — slow downside drift erodes put credit.',
    bear_call: 'Slow drift down favors call-side theta decay — clean fit.',
    iron_condor: 'Range-bound element still present; both sides can earn if drift is mild.',
    iron_butterfly: 'Pin works only if drift stalls; risky for directional bias.',
    bull_call: 'Pays for the wrong direction.',
    bear_put: 'Cheap directional bet — pays off as drift continues.',
    call_butterfly: 'Bullish lean conflicts with bearish drift.',
    put_butterfly: 'Pin near current price — partial fit if drift is very small.',
  },
  bearish: {
    bull_put: 'Sells put premium below price — wrong side; downside hits your short strike.',
    bear_call: 'Sells call premium above price — collects credit as stock drifts down.',
    iron_condor: 'Sells both wings — put side at risk if downside continues, but pinning structure can hold.',
    iron_butterfly: 'Pin trade — bearish drift breaks the pin, profit zone too narrow.',
    bull_call: 'Pays for upside on a bearish tape — moves against you.',
    bear_put: 'Buys downside exposure — directly aligned with bearish momentum.',
    call_butterfly: 'Bullish lean — wrong direction for bearish bias.',
    put_butterfly: 'Pin with bearish lean — partial fit if downside stalls at center strike.',
  },
};

const BIAS_TO_STRATEGY = { bullish: 'bull_put', mild_bullish: 'bull_put', neutral: 'iron_condor', mild_bearish: 'bear_call', bearish: 'bear_call' };
const BIAS_LABEL = { bullish: 'Bullish', mild_bullish: 'Mild Bullish', neutral: 'Neutral / Range-bound', mild_bearish: 'Mild Bearish', bearish: 'Bearish' };

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

// ==============================================
// LLM CALL HELPER — Gemini (replaces v4.5's _callAnthropic)
// ==============================================
// Uses Gemini structured-output mode (responseMimeType=application/json) so
// we get a guaranteed-parseable JSON response. No prose-scraping, no greedy
// regex. Reads model + key from window.GEMINI_API_KEY / window.GEMINI_MODEL
// (build script injects from edge_lane_config.config).
// Retry policy shared with _atlasCall — exp backoff with jitter on 429/5xx/network.
const _RETRY_HTTP = new Set([429, 500, 502, 503, 504]);
function _shouldRetry(err) {
  const m = String(err?.message || err);
  if (/Failed to fetch|NetworkError|TimeoutError/i.test(m)) return true;
  for (const c of _RETRY_HTTP) if (m.includes(`HTTP ${c}`)) return true;
  return false;
}
function _backoff(attempt, base = 1000, cap = 8000) {
  const raw = Math.min(cap, base * Math.pow(2, attempt));
  const jitter = raw * 0.2 * (2 * Math.random() - 1);
  return Math.max(300, raw + jitter);
}
function _sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function _callGemini({ prompt, label, temperature = 0.1, responseSchema = null, maxRetries = 3 }) {
  const apiKey = (typeof window !== 'undefined' && window.GEMINI_API_KEY) || (typeof localStorage !== 'undefined' && localStorage.getItem('GEMINI_API_KEY'));
  const model  = (typeof window !== 'undefined' && window.GEMINI_MODEL)   || 'gemini-2.5-flash';
  if (!apiKey) throw new Error(`${label}: GEMINI_API_KEY missing.`);

  const generationConfig = { temperature, responseMimeType: 'application/json' };
  if (responseSchema) generationConfig.responseSchema = responseSchema;
  const _geminiBase = (typeof window !== 'undefined' && window.GEMINI_BASE_URL) || 'https://generativelanguage.googleapis.com/v1beta';
  const url = `${_geminiBase}/models/${model}:generateContent`;
  const body = JSON.stringify({ contents: [{ parts: [{ text: prompt }] }], generationConfig });

  let lastErr = null;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-goog-api-key': apiKey },
        body,
      });
      if (!r.ok) {
        const errText = await r.text().catch(() => '');
        const err = new Error(`${label}: HTTP ${r.status}${errText ? ` — ${errText.slice(0, 200)}` : ''}`);
        if (attempt < maxRetries && _shouldRetry(err)) {
          const delay = _backoff(attempt);
          console.warn(`${label} attempt ${attempt + 1}/${maxRetries + 1} failed (${r.status}). Retrying in ${(delay/1000).toFixed(1)}s...`);
          lastErr = err; await _sleep(delay); continue;
        }
        throw err;
      }
      const data = await r.json();
      const text = (data?.candidates?.[0]?.content?.parts || []).map(p => p.text || '').join('');
      if (!text) throw new Error(`${label}: empty response from Gemini`);
      try { return JSON.parse(text); }
      catch (e) { throw new Error(`${label}: JSON parse failed — ${e.message}. Got: ${text.slice(0, 200)}`); }
    } catch (e) {
      // Network / TypeErrors land here — only retry the transient ones
      lastErr = e;
      if (attempt < maxRetries && _shouldRetry(e)) {
        const delay = _backoff(attempt);
        console.warn(`${label} attempt ${attempt + 1}/${maxRetries + 1} network error. Retrying in ${(delay/1000).toFixed(1)}s...`);
        await _sleep(delay); continue;
      }
      throw e;
    }
  }
  throw lastErr;
}

// ==============================================
// ATLAS REST CLIENT (direct, no MCP, no LLM-in-the-loop)
// ==============================================
// Three tools we depend on:
//   get_stock_quote          → spot price
//   analyze_greek_exposures  → raw GEX/DEX/VEX/TEX arrays for bias synthesis
//   get_options_chain        → full chain (filtered client-side to ±N% of spot)
// Atlas wraps quota/rate-limit errors in 200 responses with {error, message}.
async function _atlasCall(tool, body, { maxRetries = 3 } = {}) {
  const key = (typeof window !== 'undefined' && window.ATLAS_KEY) || (typeof localStorage !== 'undefined' && localStorage.getItem('ATLAS_KEY'));
  if (!key) throw new Error(`Provider ${tool}: API key missing.`);
  // Use proxy if set (window.ATLAS_BASE_URL like 'http://localhost:8787/atlas'),
  // otherwise direct (will hit CORS from browser unless Atlas allows your origin).
  const _atlasBase = (typeof window !== 'undefined' && window.ATLAS_BASE_URL) || 'https://atlasmcp.finmanagerai.com/api/v1/tools';
  const url = `${_atlasBase}/${tool}`;
  const reqBody = JSON.stringify(body || {});

  let lastErr = null;
  // v4.7.22: explicit 75s timeout. analyze_greek_exposures on heavy symbols
  // (MU, AMD, SMCI) regularly takes 40-60s server-side; without AbortController
  // the browser default of ~5 min just leaves users staring at a spinner.
  const REQUEST_TIMEOUT_MS = 75000;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    let r;
    const abort = new AbortController();
    const tid = setTimeout(() => abort.abort(), REQUEST_TIMEOUT_MS);
    try {
      r = await fetch(url, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${key}`, 'Content-Type': 'application/json' },
        body: reqBody,
        signal: abort.signal,
      });
      clearTimeout(tid);
    } catch (e) {
      clearTimeout(tid);
      const msg = String(e?.message || e);
      const isTimeout = e?.name === 'AbortError' || /aborted/i.test(msg);
      if (isTimeout) {
        const wrapped = new Error(`Provider ${tool}: request timed out after ${REQUEST_TIMEOUT_MS/1000}s. This endpoint is compute-heavy for some symbols; consider reducing num_expirations or retrying.`);
        if (attempt < maxRetries) {
          const delay = _backoff(attempt);
          console.warn(`Provider ${tool} attempt ${attempt + 1}/${maxRetries + 1} timed out. Retrying in ${(delay/1000).toFixed(1)}s...`);
          lastErr = wrapped; await _sleep(delay); continue;
        }
        throw wrapped;
      }
      if (/Failed to fetch|NetworkError/i.test(msg)) {
        const wrapped = new Error(`Provider ${tool}: blocked by CORS or network. May need a proxy for browser-origin calls.`);
        if (attempt < maxRetries) {
          const delay = _backoff(attempt);
          console.warn(`Provider ${tool} attempt ${attempt + 1}/${maxRetries + 1} network error. Retrying in ${(delay/1000).toFixed(1)}s...`);
          lastErr = wrapped; await _sleep(delay); continue;
        }
        throw wrapped;
      }
      throw e;
    }
    if (!r.ok) {
      const text = await r.text().catch(() => '');
      const err = new Error(`Provider ${tool}: HTTP ${r.status}: ${text.slice(0, 200)}`);
      if (attempt < maxRetries && _shouldRetry(err)) {
        const delay = _backoff(attempt);
        console.warn(`Provider ${tool} attempt ${attempt + 1}/${maxRetries + 1} failed (${r.status}). Retrying in ${(delay/1000).toFixed(1)}s...`);
        lastErr = err; await _sleep(delay); continue;
      }
      throw err;
    }
    const data = await r.json();
    // Provider wraps quota errors in 200 + {error,message} — non-retryable (not transient)
    if (data?.error) throw new Error(`Provider ${tool}: ${data.error}: ${data.message || 'unknown error'}`);
    return data;
  }
  throw lastErr;
}

const _num = (v) => (v == null || v === '' ? null : Number(v));

function _normalizeContract(c, sideHint, expiration) {
  const sideSrc = String(c.side || c.type || c.option_type || c.contract_type || sideHint || '').toLowerCase();
  const side = sideSrc.includes('p') ? 'put' : 'call';
  const bid = _num(c.bid ?? c.bidPrice);
  const ask = _num(c.ask ?? c.askPrice);
  const last = _num(c.last ?? c.last_price ?? c.lastPrice);
  const mid = _num(c.mid) ?? (bid != null && ask != null ? (bid + ask) / 2 : last);
  return {
    // v4.7.31c: carry the provider's canonical option symbol so push-to-broker
    // can use it as the OCC OptionSymbol directly. Avoids root-name guesswork
    // for index options (NDX vs NDXP, SPX vs SPXW, etc.).
    symbol: c.symbol ?? c.option_symbol ?? c.osi_symbol ?? null,
    strike: _num(c.strike ?? c.strike_price ?? c.strikePrice),
    side,
    expiration: expiration ?? c.expiration ?? c.expiry ?? c.exp_date ?? null,
    bid: bid ?? 0,
    ask: ask ?? 0,
    mid: mid ?? 0,
    delta: _num(c.delta ?? c.greeks?.delta),
    gamma: _num(c.gamma ?? c.greeks?.gamma),
    theta: _num(c.theta ?? c.greeks?.theta),
    vega:  _num(c.vega  ?? c.greeks?.vega),
    iv:    _num(c.iv ?? c.implied_volatility ?? c.impliedVolatility ?? c.greeks?.iv),
    open_interest: _num(c.open_interest ?? c.openInterest ?? c.oi) ?? 0,
    volume:        _num(c.volume ?? c.vol) ?? 0,
  };
}

// Walk Atlas's chain[] structure (or contracts[] if MCP-style). Handles flat
// arrays, per-expiration grouping with calls/puts, and per-strike grouping.
function _flattenChain(chainData, targetExpiration) {
  const out = [];
  const target = targetExpiration ? String(targetExpiration) : null;
  const walk = (node, sideHint, currentExp) => {
    if (node == null) return;
    if (Array.isArray(node)) { node.forEach(n => walk(n, sideHint, currentExp)); return; }
    if (typeof node !== 'object') return;
    const exp = node.expiration || node.expiry || node.exp_date || currentExp;
    if (target && exp && String(exp) !== target) return;
    if (Array.isArray(node.calls)) node.calls.forEach(c => out.push(_normalizeContract(c, 'call', exp)));
    if (Array.isArray(node.puts))  node.puts.forEach(c => out.push(_normalizeContract(c, 'put', exp)));
    if (Array.isArray(node.contracts)) node.contracts.forEach(c => out.push(_normalizeContract(c, sideHint, exp)));
    if (node.strike != null && (node.side || node.type || node.option_type)) out.push(_normalizeContract(node, sideHint, exp));
    for (const k of Object.keys(node)) {
      if (['calls','puts','contracts'].includes(k)) continue;
      if (Array.isArray(node[k]) || (typeof node[k] === 'object' && node[k])) walk(node[k], sideHint, exp);
    }
  };
  walk(chainData, undefined, target);
  return out;
}

const atlas = {
  async stockQuote(symbol) {
    const d = await _atlasCall('get_stock_quote', { symbol });
    return { symbol: d.symbol || symbol, price: _num(d.price), volume: _num(d.volume), timestamp: d.timestamp, raw: d };
  },
  async getSubscriptionStatus() {
    return _atlasCall('get_subscription_status', {});
  },
    async analyzeGreekExposures(symbol, num_expirations = 5) {
    return _atlasCall('analyze_greek_exposures', { symbol, num_expirations });
  },
  // One REST call. Filters to target expiration + ±strikeBandPct of spot client-side.
  async getOptionsChain(symbol, expiration, { strikeBandPct = 30, maxExpirations = 12 } = {}) {
    // Atlas REST requires `expiration` (or a range). max_expirations alone is
    // rejected. We always know the target expiration here.
    const [quote, resp] = await Promise.all([
      atlas.stockQuote(symbol).catch(() => null),
      _atlasCall('get_options_chain', { symbol, expiration }),
    ]);
    const spot = quote?.price ?? _num(resp?.current_price ?? resp?.spot ?? resp?.underlying_price);
    if (!spot) throw new Error(`Could not determine spot price for ${symbol}.`);

    let contracts = _flattenChain(resp.chain ?? resp.contracts ?? resp, expiration);
    if (contracts.length === 0) contracts = _flattenChain(resp.chain ?? resp.contracts ?? resp, null);

    const lo = spot * (1 - strikeBandPct / 100);
    const hi = spot * (1 + strikeBandPct / 100);
    contracts = contracts.filter(c => c.strike != null && c.strike >= lo && c.strike <= hi);

    let atmStrike = null;
    for (const c of contracts) if (atmStrike == null || Math.abs(c.strike - spot) < Math.abs(atmStrike - spot)) atmStrike = c.strike;
    const atmCall = contracts.find(c => c.strike === atmStrike && c.side === 'call');
    const atmPut  = contracts.find(c => c.strike === atmStrike && c.side === 'put');
    const expectedMove = ((atmCall?.mid) || 0) + ((atmPut?.mid) || 0);
    // Atlas IVs come back as percent already (e.g. 17.8 for 17.8%) — keep as-is
    const atmIvPct = (((atmCall?.iv) || 0) + ((atmPut?.iv) || 0)) / 2;

    // v4.7.32: keep DTE fractional so 0DTE intraday has proper sub-day precision.
    const dte = Math.max(0, (new Date(`${expiration}T21:00:00Z`) - new Date()) / 86400000);
    return {
      spot, dte,
      atm_iv_pct: atmIvPct, atmIV: atmIvPct,
      expected_move: expectedMove, expectedMove,
      expected_move_pct: spot ? (expectedMove / spot) * 100 : 0,
      expectedMovePct:    spot ? (expectedMove / spot) * 100 : 0,
      contracts,
    };
  },
  // v4.7.25 interface aliases — keep snake_case existing methods AND expose
  // provider-agnostic camelCase. dataProvider downstream uses the camelCase.
  async optionsChain(symbol, expiration, opts) { return atlas.getOptionsChain(symbol, expiration, opts); },
  async subscriptionStatus() { return atlas.getSubscriptionStatus(); },
  async greekExposures(symbol, num_expirations = 3) { return _getGreekExposures(symbol, num_expirations); },
};

// ==============================================
// DEALER GEX (LOCAL COMPUTATION, v4.7.25)
// ==============================================
// JS mirror of data_providers/gex_local.py. Aggregates per-contract gamma/delta
// × open_interest into the same {exposures_by_date, portfolio_totals,
// key_levels} shape Atlas\'s analyze_greek_exposures returns.
//
// Convention (matches SpotGamma / SqueezeMetrics):
//   dealer_gamma_at_strike(K) = put_gamma(K) × put_OI(K) − call_gamma(K) × call_OI(K)
//   dealer_GEX_dollars(K)     = dealer_gamma × 100 × spot²
//   call_wall = strike above spot with most-NEGATIVE dealer GEX (forced-buy)
//   put_wall  = strike below spot with most-POSITIVE dealer GEX (forced-sell)

const _DEALER_CONTRACT_MULT = 100;

function _emptyDealerExposures() {
  return {
    exposures_by_date: {},
    portfolio_totals: { net_gex: 0, net_dex: 0, net_vex: 0, net_tex: 0 },
    key_levels: { call_wall: null, put_wall: null, vex_wall: null, tex_wall: null },
  };
}

// v4.7.29: also accumulate per-strike VEX (vega × OI) and TEX (theta × OI)
// using the same dealer-hedging sign convention as GEX:
//   dealer_vega(K)  = put_vega × put_OI − call_vega × call_OI    (vol-magnet sign)
//   dealer_theta(K) = put_theta × put_OI − call_theta × call_OI  (decay-burn sign)
//   VEX$(K)  = dealer_vega  × 100   (dollars per 1-vol-pt IV move)
//   TEX$(K)  = dealer_theta × 100   (dollars/day at K)
// vex_wall = strike with max |VEX|. tex_wall = strike with max |TEX|.
function _computeDealerExposures(contracts, spot) {
  if (!Array.isArray(contracts) || contracts.length === 0 || !spot || spot <= 0) {
    return _emptyDealerExposures();
  }
  const buckets = new Map();   // exp -> (strike -> {call?, put?})
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
  // VEX/TEX walls: strike with the largest absolute exposure (peak magnet,
  // regardless of side). Used by the optional 3-lens divergence strip.
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
// TRADIER REST CLIENT (v4.7.25)
// ==============================================
// Brokerage REST: Bearer auth, hyphenated PascalCase paths via REST gateway,
// decimal IV (we convert to percent in _normalizeTradierContract to match
// Atlas downstream convention).

const _tradierRateLimit = {};

async function _tradierGet(path, params) {
  const token = (typeof window !== 'undefined' && window.TRADIER_TOKEN) || '';
  if (!token) throw new Error(`Provider ${path}: TRADIER_TOKEN missing.`);
  const base = (typeof window !== 'undefined' && window.TRADIER_BASE_URL) || 'https://sandbox.tradier.com';
  const qs = new URLSearchParams(params || {}).toString();
  const url = `${base.replace(/\/$/, '')}/v1/${path.replace(/^\//, '')}${qs ? '?' + qs : ''}`;

  const abort = new AbortController();
  const tid = setTimeout(() => abort.abort(), 30000);
  let r;
  try {
    r = await fetch(url, {
      method: 'GET',
      headers: { 'Authorization': `Bearer ${token}`, 'Accept': 'application/json' },
      signal: abort.signal,
    });
    clearTimeout(tid);
  } catch (e) {
    clearTimeout(tid);
    const isTimeout = e?.name === 'AbortError';
    throw new Error(`Provider ${path}: ${isTimeout ? 'timed out after 30s' : 'network ' + (e?.message || e)}`);
  }
  for (const h of ['X-Ratelimit-Allowed','X-Ratelimit-Used','X-Ratelimit-Available','X-Ratelimit-Expiry']) {
    const v = r.headers.get(h);
    if (v != null) _tradierRateLimit[h] = v;
  }
  if (!r.ok) {
    const text = await r.text().catch(() => '');
    throw new Error(`Provider ${path}: HTTP ${r.status}: ${text.slice(0, 200)}`);
  }
  return r.json();
}

function _normalizeTradierContract(c, expiration) {
  const sideRaw = String(c.option_type || '').toLowerCase();
  const side = sideRaw.includes('call') ? 'call' : (sideRaw.includes('put') ? 'put' : null);
  if (!side) return null;
  const g = c.greeks || {};
  const bid = _num(c.bid) ?? 0;
  const ask = _num(c.ask) ?? 0;
  const ivDecimal = _num(g.mid_iv);
  return {
    // v4.7.31c: carry Tradier's canonical OCC symbol for push-to-broker.
    symbol: c.symbol ?? null,
    strike: _num(c.strike),
    side,
    expiration,
    bid, ask,
    last: _num(c.last) ?? 0,
    mid: (bid + ask) / 2,
    delta: _num(g.delta),
    gamma: _num(g.gamma),
    theta: _num(g.theta),
    vega:  _num(g.vega),
    iv: ivDecimal != null ? ivDecimal * 100 : null,    // decimal → percent (Atlas convention)
    open_interest: _num(c.open_interest) ?? 0,
    volume:        _num(c.volume) ?? 0,
  };
}

const tradier = {
  async stockQuote(symbol) {
    const d = await _tradierGet('markets/quotes', { symbols: symbol, greeks: 'false' });
    const q = d.quotes?.quote;
    const quote = Array.isArray(q) ? q[0] : q;
    if (!quote) throw new Error(`Provider markets/quotes: empty response for ${symbol}`);
    return {
      symbol: quote.symbol || symbol,
      price: _num(quote.last),
      bid: _num(quote.bid),
      ask: _num(quote.ask),
      volume: _num(quote.volume),
      timestamp: quote.trade_date,
      raw: quote,
    };
  },

  async optionExpirations(symbol) {
    const d = await _tradierGet('markets/options/expirations', { symbol, includeAllRoots: 'true' });
    const dates = d.expirations?.date || [];
    return Array.isArray(dates) ? dates.map(String) : [String(dates)];
  },

  async optionsChain(symbol, expiration, { strikeBandPct = 30 } = {}) {
    const [quote, chainResp] = await Promise.all([
      tradier.stockQuote(symbol).catch(() => null),
      _tradierGet('markets/options/chains', { symbol, expiration, greeks: 'true' }),
    ]);
    const spot = quote?.price;
    if (!spot) throw new Error(`Provider: could not determine spot price for ${symbol}.`);

    const raw = chainResp.options?.option || [];
    const list = Array.isArray(raw) ? raw : [raw];
    let contracts = list.map(c => _normalizeTradierContract(c, expiration)).filter(Boolean);

    const lo = spot * (1 - strikeBandPct / 100);
    const hi = spot * (1 + strikeBandPct / 100);
    contracts = contracts.filter(c => c.strike != null && c.strike >= lo && c.strike <= hi);

    let atmStrike = null;
    for (const c of contracts) if (atmStrike == null || Math.abs(c.strike - spot) < Math.abs(atmStrike - spot)) atmStrike = c.strike;
    const atmCall = contracts.find(c => c.strike === atmStrike && c.side === 'call');
    const atmPut  = contracts.find(c => c.strike === atmStrike && c.side === 'put');
    const expectedMove = ((atmCall?.mid) || 0) + ((atmPut?.mid) || 0);
    const atmIvPct = (((atmCall?.iv) || 0) + ((atmPut?.iv) || 0)) / 2;

    // v4.7.32: keep DTE fractional so 0DTE intraday has proper sub-day precision.
    const dte = Math.max(0, (new Date(`${expiration}T21:00:00Z`) - new Date()) / 86400000);
    return {
      spot, dte,
      atm_iv_pct: atmIvPct, atmIV: atmIvPct,
      expected_move: expectedMove, expectedMove,
      expected_move_pct: spot ? (expectedMove / spot) * 100 : 0,
      expectedMovePct:    spot ? (expectedMove / spot) * 100 : 0,
      contracts,
    };
  },

  async greekExposures(symbol, num_expirations = 3) {
    const sym = String(symbol || '').toUpperCase();
    const cached = _gexCacheGet(sym, num_expirations);
    if (cached) { console.info(`[Tradier GEX] cache hit for ${sym}`); return cached; }
    const expDates = (await tradier.optionExpirations(sym)).slice(0, num_expirations);
    if (!expDates.length) throw new Error(`Provider: no expirations for ${sym}`);
    const chains = await Promise.all(expDates.map(exp =>
      tradier.optionsChain(sym, exp).catch(e => {
        console.warn(`[Tradier GEX] chain ${sym} ${exp} failed:`, e?.message || e);
        return null;
      })
    ));
    const ok = chains.filter(c => c && c.contracts?.length);
    if (!ok.length) throw new Error(`Provider: all ${expDates.length} chain pulls failed for ${sym}`);
    const data = _computeDealerExposures(ok.flatMap(c => c.contracts), ok[0].spot);
    data._provider = 'tradier';
    _gexCacheSet(sym, num_expirations, data);
    return data;
  },

  async subscriptionStatus() {
    const rl = _tradierRateLimit;
    const used = _num(rl['X-Ratelimit-Used']);
    const allowed = _num(rl['X-Ratelimit-Allowed']);
    return {
      plan: 'brokerage',
      status: used != null ? 'active' : 'no calls yet this session',
      used, limit: allowed,
      period: 'minute',
      resetsAt: rl['X-Ratelimit-Expiry'],
    };
  },
};



// ==============================================
// PER-USER BROKER ORDER EXECUTION (v4.7.30)
// ==============================================
// Separate path from market-data calls. Market data uses operator-level
// window.TRADIER_TOKEN (the EdgeLane app's own token, configured at build time).
// Order routing uses the END USER's connection token, stored per-user under
// settings and selected as "active". This keeps trade authority on the user
// while the optimizer still has market data on its own dime.
//
// Two providers supported:
//   - tradier : full implementation against Tradier REST API
//   - webull  : stub — official OpenAPI needs HMAC signing which can't safely
//               run in-browser. Reserved for a future signing-proxy server.
//
// Storage: sessionStorage only. Clears on tab close. See _useBrokerConnections.

const BROKER_PROVIDERS = {
  tradier: {
    id: 'tradier',
    name: 'Tradier',
    description: 'Full REST trading API. Sandbox supports paper-trading with simulated fills.',
    fields: [
      { key: 'access_token', label: 'Access Token', type: 'password', required: true,
        help: 'Bearer token from https://dash.tradier.com/settings/api (production) or https://developer.tradier.com (sandbox).' },
      { key: 'env', label: 'Environment', type: 'select', required: true,
        options: [
          { value: 'sandbox', label: 'Sandbox (paper trading, https://sandbox.tradier.com)' },
          { value: 'production', label: 'Production (real money, https://api.tradier.com)' },
        ], default: 'sandbox' },
    ],
    status: 'available',
  },
  webull: {
    id: 'webull',
    name: 'WeBull',
    description: 'Coming soon. WeBull OpenAPI requires HMAC request signing which can\'t safely run in the browser (would leak the App Secret). A signing proxy is needed before this can light up.',
    fields: [
      { key: 'app_key',    label: 'App Key',    type: 'text',     required: true,
        help: 'From the WeBull mobile app → Settings → OpenAPI Management.' },
      { key: 'app_secret', label: 'App Secret', type: 'password', required: true,
        help: 'Generated alongside the App Key. NEVER share this.' },
    ],
    status: 'stub',
  },
};

function _baseUrlForTradierEnv(env) {
  return env === 'production' ? 'https://api.tradier.com' : 'https://sandbox.tradier.com';
}

// OCC option symbol (OSI standard, 21 chars):
//   {ROOT}{YYMMDD}{C|P}{strike*1000, 8-digit zero-padded}
// Tradier accepts this exact format for option_symbol[n].
function _buildOccSymbol(underlying, expirationDate, side, strike) {
  const root = String(underlying).toUpperCase();
  const d = new Date(`${expirationDate}T00:00:00Z`);
  const yy = String(d.getUTCFullYear()).slice(2).padStart(2, '0');
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const cp = String(side).toLowerCase().startsWith('c') ? 'C' : 'P';
  const strikeInt = Math.round(Number(strike) * 1000);
  const strikeStr = String(strikeInt).padStart(8, '0');
  return `${root}${yy}${mm}${dd}${cp}${strikeStr}`;
}

// Map candidate leg longShort + side → Tradier `side` (xxx_to_open)
function _tradierLegSide(longShort, optType) {
  // longShort: +1 long (buy_to_open), -1 short (sell_to_open),
  //            -2 fly middle (sell_to_open, qty doubled)
  if (longShort > 0) return 'buy_to_open';
  return 'sell_to_open';
}

// Build a Tradier multileg-order form body. Returns the URLSearchParams-ready
// string. `priceMode` is 'market' | 'limit'; `limitPrice` is required for
// limit. We deliberately use `type=credit`/`debit` not `type=limit` because
// Tradier's multileg accepts net spread price under those types — `limit`
// is for single-leg orders.
// v4.7.31c: extract the underlying root from an OCC option symbol. OCC trails
// the root with YYMMDD(6) + C|P(1) + 8-digit strike = 15 chars. Whatever comes
// before that is the root. Returns null on malformed input.
function _rootFromOcc(occ) {
  if (typeof occ !== 'string') return null;
  if (occ.length < 16) return null;
  return occ.slice(0, occ.length - 15).trim() || null;
}

function _buildTradierOrderBody(candidate, symbol, expiration, priceMode, limitPrice, opts) {
  if (!candidate?.legs?.length) throw new Error('Order build: candidate has no legs.');
  const isCredit = candidate.type === 'credit';
  const orderType = priceMode === 'market' ? 'market' : (isCredit ? 'credit' : 'debit');
  const duration = (opts && opts.duration) || 'day';
  // v4.7.31a: Tradier `tag` is strictly alphanumeric — strip everything but
  // a-z A-Z 0-9 and cap at 30. (HTTP 400 'invalid characters' otherwise.)
  const rawTag = (opts && opts.tag) || `edgelane${candidate.label || 'candidate'}`;
  const tag = String(rawTag).replace(/[^a-zA-Z0-9]/g, '').slice(0, 30);

  // v4.7.31c: prefer chain-cached symbols on each leg (already fetched & cached
  // when the chain was pulled). Fall back to hand-built OCC only if a leg is
  // missing one. Underlying root for the order body comes from the first
  // leg's symbol — so an NDX ticket whose chain came back as NDXP routes
  // correctly without any per-user lookup.
  const legSymbols = candidate.legs.map(l =>
    (typeof l.symbol === 'string' && l.symbol) ? l.symbol : _buildOccSymbol(symbol, expiration, l.side, l.strike)
  );
  const resolvedRoot = _rootFromOcc(legSymbols[0]) || String(symbol).toUpperCase();

  const params = new URLSearchParams();
  params.set('class', 'multileg');
  params.set('symbol', resolvedRoot);
  params.set('type', orderType);
  params.set('duration', duration);
  if (orderType !== 'market') {
    if (limitPrice == null || isNaN(Number(limitPrice))) throw new Error('Order build: limit price required.');
    params.set('price', Number(limitPrice).toFixed(2));
  }
  if (tag) params.set('tag', tag);
  if (opts && opts.preview) params.set('preview', 'true');

  candidate.legs.forEach((leg, idx) => {
    // qtyFactor: standard legs are 1×, the doubled middle leg of a butterfly is 2×
    const qtyFactor = Math.abs(leg.longShort) === 2 ? 2 : 1;
    params.set(`option_symbol[${idx}]`, legSymbols[idx]);
    params.set(`side[${idx}]`, _tradierLegSide(leg.longShort, leg.side));
    params.set(`quantity[${idx}]`, String(qtyFactor));
  });
  return params.toString();
}

// POST against an arbitrary Tradier connection (NOT the operator's window-global
// token). Form-urlencoded, captures rate-limit headers, returns parsed JSON.
async function _tradierConnectionPost(connection, path, body) {
  const base = _baseUrlForTradierEnv(connection.config?.env || 'sandbox');
  const token = connection.config?.access_token;
  if (!token) throw new Error('Connection has no access_token.');
  const url = `${base.replace(/\/$/, '')}/v1/${path.replace(/^\//, '')}`;
  const r = await fetch(url, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/json',
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body,
  });
  let text;
  try { text = await r.text(); } catch (e) { text = ''; }
  if (!r.ok) {
    let detail = text;
    try { const j = JSON.parse(text); detail = j.errors?.error || j.message || text; } catch {}
    throw new Error(`Tradier ${path}: HTTP ${r.status} — ${Array.isArray(detail) ? detail.join('; ') : detail}`);
  }
  try { return JSON.parse(text); } catch (e) { throw new Error(`Tradier ${path}: bad JSON — ${text.slice(0, 200)}`); }
}

async function _tradierConnectionGet(connection, path, params) {
  const base = _baseUrlForTradierEnv(connection.config?.env || 'sandbox');
  const token = connection.config?.access_token;
  if (!token) throw new Error('Connection has no access_token.');
  const qs = new URLSearchParams(params || {}).toString();
  const url = `${base.replace(/\/$/, '')}/v1/${path.replace(/^\//, '')}${qs ? '?' + qs : ''}`;
  const r = await fetch(url, {
    method: 'GET',
    headers: { 'Authorization': `Bearer ${token}`, 'Accept': 'application/json' },
  });
  let text;
  try { text = await r.text(); } catch { text = ''; }
  if (!r.ok) {
    let detail = text;
    try { const j = JSON.parse(text); detail = j.errors?.error || j.message || text; } catch {}
    throw new Error(`Tradier ${path}: HTTP ${r.status} — ${Array.isArray(detail) ? detail.join('; ') : detail}`);
  }
  try { return JSON.parse(text); } catch (e) { throw new Error(`Tradier ${path}: bad JSON.`); }
}

// Pick the first account on the profile with option_level >= 3. Caches.
async function _resolveTradierAccountId(connection) {
  if (connection.config?.account_number) return connection.config.account_number;
  const profile = await _tradierConnectionGet(connection, 'user/profile');
  let accounts = profile?.profile?.account || [];
  if (!Array.isArray(accounts)) accounts = [accounts];
  const usable = accounts.find(a => Number(a.option_level || 0) >= 3) || accounts[0];
  if (!usable?.account_number) throw new Error('No usable account on this Tradier profile.');
  return usable.account_number;
}

// Connection healthcheck. Returns { ok, message, accountNumber?, latencyMs }.
async function _testBrokerConnection(connection) {
  const t0 = Date.now();
  if (connection.provider === 'webull') {
    return {
      ok: false,
      message: 'WeBull integration is stubbed — official OpenAPI requires server-side HMAC signing (cannot run in browser).',
      latencyMs: Date.now() - t0,
    };
  }
  if (connection.provider === 'tradier') {
    try {
      const profile = await _tradierConnectionGet(connection, 'user/profile');
      const id = profile?.profile?.id;
      let accounts = profile?.profile?.account || [];
      if (!Array.isArray(accounts)) accounts = [accounts];
      const usable = accounts.find(a => Number(a.option_level || 0) >= 3) || accounts[0];
      if (!usable) return { ok: false, message: 'No account found on profile.', latencyMs: Date.now() - t0 };
      return {
        ok: true,
        message: `Connected as ${id || 'unknown'} · account ${usable.account_number} · option level ${usable.option_level || '?'} · env ${connection.config?.env || 'sandbox'}`,
        accountNumber: usable.account_number,
        latencyMs: Date.now() - t0,
      };
    } catch (e) {
      return { ok: false, message: e.message, latencyMs: Date.now() - t0 };
    }
  }
  return { ok: false, message: `Unknown provider: ${connection.provider}`, latencyMs: Date.now() - t0 };
}

// Submit an order via the active connection. priceMode: 'market'|'limit'.
// On success returns { id, status, raw, previewed }. Throws otherwise.
async function _submitOrderToActiveBroker(connection, candidate, symbol, expiration, priceMode, limitPrice) {
  if (!connection) throw new Error('No active broker connection — set one in Settings.');
  if (connection.provider === 'webull') {
    throw new Error('WeBull integration not yet implemented. Use Tradier or contact support.');
  }
  if (connection.provider !== 'tradier') {
    throw new Error(`Unknown provider: ${connection.provider}`);
  }
  // 1. Resolve account
  const accountId = await _resolveTradierAccountId(connection);
  // 2. Preview first
  const previewBody = _buildTradierOrderBody(candidate, symbol, expiration, priceMode, limitPrice, { preview: true });
  const previewResp = await _tradierConnectionPost(connection, `accounts/${accountId}/orders`, previewBody);
  const previewOrder = previewResp?.order;
  // Tradier returns result:"ok" on accepted preview (and may also surface
  // estimated cost/margin etc). A real rejection surfaces with errors above
  // (handled in _tradierConnectionPost via HTTP status).
  if (previewOrder && previewOrder.status && String(previewOrder.status).toLowerCase() !== 'ok') {
    throw new Error(`Tradier preview rejected: ${previewOrder.status} — ${previewOrder.reason_description || JSON.stringify(previewOrder).slice(0, 200)}`);
  }
  // 3. Submit live
  const liveBody = _buildTradierOrderBody(candidate, symbol, expiration, priceMode, limitPrice, { preview: false });
  const liveResp = await _tradierConnectionPost(connection, `accounts/${accountId}/orders`, liveBody);
  const order = liveResp?.order;
  if (!order || !order.id) {
    throw new Error(`Tradier accepted POST but returned no order id. Body: ${JSON.stringify(liveResp).slice(0, 200)}`);
  }
  return {
    id: order.id,
    status: order.status || 'submitted',
    partnerId: order.partner_id,
    raw: liveResp,
    previewed: true,
    accountId,
  };
}

// ==============================================
// PROVIDER SELECTOR (v4.7.25)
// ==============================================
const dataProvider = (typeof window !== 'undefined' && String(window.DATA_PROVIDER).toLowerCase() === 'tradier')
  ? tradier
  : atlas;
if (typeof window !== 'undefined') {
  console.info(`[EdgeLane] data provider: ${dataProvider === tradier ? 'tradier' : 'atlas'}`);
}


// ==============================================
// DETERMINISTIC BIAS ENGINE (v4.7 hybrid)
// ==============================================
// In v4.6, Flash and Pro produced different bias_label / score / confidence on
// identical Atlas data because the prompt asked the LLM to do arithmetic
// (where's the wall? is spot above? what does net DEX imply?). Same data, same
// prompt, opposite directional read — bad for trade selection.
//
// Hybrid fix: compute everything mechanical in JS (wall, strength, score,
// confidence, recommended strategies). Gemini's only job is prose — the
// summary paragraph and four short signal sentences. No more LLM arithmetic;
// no more model-vs-model disagreement on structured fields.

// Walk the per-strike rows from Atlas's exposures_by_date entry. The shape
// isn't perfectly documented; try common keys.
function _strikeRowsFrom(exposuresForChosen) {
  if (!exposuresForChosen || typeof exposuresForChosen !== 'object') return [];
  for (const k of ['by_strike', 'strikes', 'rows', 'data']) {
    if (Array.isArray(exposuresForChosen[k])) return exposuresForChosen[k];
  }
  // last resort — if it's already an array
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

// Find the dominant gamma wall + classify its strength relative to others.
// Atlas often pre-computes call_wall / put_wall in key_levels — prefer those.
function _findGexWall(greeksRaw, exposuresForChosen) {
  // 1. Prefer Atlas's own key_levels if present
  const kl = greeksRaw?.key_levels || {};
  const atlasCallWall = Number(kl.call_wall ?? kl.callWall ?? kl.gex_call_wall ?? 0) || null;
  const atlasPutWall  = Number(kl.put_wall  ?? kl.putWall  ?? kl.gex_put_wall  ?? 0) || null;

  // 2. Compute from per-strike GEX
  const rows = _strikeRowsFrom(exposuresForChosen);
  const gexByStrike = rows
    .map(r => ({ strike: _strikeFromRow(r), gex: _gexFromRow(r) }))
    .filter(o => o.strike != null && !isNaN(o.strike));

  let best = null;
  for (const o of gexByStrike) {
    if (best == null || Math.abs(o.gex) > Math.abs(best.gex)) best = o;
  }

  // Strength: ratio of #1 |GEX| to #2 |GEX|
  const sortedAbs = gexByStrike.map(o => Math.abs(o.gex)).sort((a,b) => b-a);
  let strength = 'low';
  if (sortedAbs.length >= 2 && sortedAbs[1] > 0) {
    const ratio = sortedAbs[0] / sortedAbs[1];
    strength = ratio > 2 ? 'high' : ratio > 1.2 ? 'medium' : 'low';
  } else if (sortedAbs.length === 1 && sortedAbs[0] > 0) {
    strength = 'high';
  }

  // Final wall: prefer the larger of (computed, atlas call_wall, atlas put_wall)
  // by |GEX|, but if computed is null, take whichever atlas value exists.
  const wallStrike = best?.strike ?? atlasCallWall ?? atlasPutWall ?? null;

  return { strike: wallStrike, strength, atlasCallWall, atlasPutWall, computedTopGex: best?.gex || 0 };
}

// Aggregate signed exposures for the chosen expiration.
function _aggregateExposures(exposuresForChosen, portfolioTotals) {
  const rows = _strikeRowsFrom(exposuresForChosen);
  let netGex = 0, netDex = 0;
  for (const r of rows) { netGex += _gexFromRow(r); netDex += _dexFromRow(r); }
  // If per-strike rows weren't available, fall back to portfolio_totals
  if (rows.length === 0 && portfolioTotals) {
    netGex = Number(portfolioTotals.net_gex ?? portfolioTotals.gex ?? netGex);
    netDex = Number(portfolioTotals.net_dex ?? portfolioTotals.dex ?? netDex);
  }
  return { netGex, netDex };
}

// Score directional bias on [-100, 100] from spot vs wall + DEX + gamma regime.
// Wall ABOVE spot → resistance → bearish. Wall BELOW spot → support → bullish.
// Strength multiplies the effect. Positive net gamma (pinning) dampens, negative
// (amplifying) leaves it. Net DEX adds a small skew (positive dealer delta =
// dealers sell rallies = bearish flow contribution).
function _computeDirectionalScore(spot, wall, netGex, netDex) {
  if (!spot || wall.strike == null) return 0;
  const pctFromWall = (spot - wall.strike) / wall.strike;
  let score = pctFromWall * 200;  // 5% above = +10; 25% above = +50

  const strengthMult = wall.strength === 'high' ? 1.5 : wall.strength === 'medium' ? 1.0 : 0.5;
  score *= strengthMult;

  // Gamma regime modulator
  if (netGex > 0) score *= 0.7;       // positive gamma = pinning, dampen
  else if (netGex < 0) score *= 1.2;  // negative = amplifying

  // DEX skew (small contribution, max ±10)
  if (netDex !== 0) score += -10 * Math.sign(netDex);

  return Math.max(-100, Math.min(100, Math.round(score * 10) / 10));
}

function _scoreToBiasLabel(score) {
  if (score >= 60)  return 'bullish';
  if (score >= 20)  return 'mild_bullish';
  if (score <= -60) return 'bearish';
  if (score <= -20) return 'mild_bearish';
  return 'neutral';
}

// Confidence: high when wall is strong AND |score| > 30 AND DEX direction
// agrees with wall-position direction. Low when wall is weak or score tiny.
function _computeConfidence(score, wall, netDex, spot) {
  const absScore = Math.abs(score);
  if (wall.strength === 'low' || wall.strike == null) return 'low';
  if (absScore < 15) return 'low';

  const wallDir = spot > wall.strike ? 1 : spot < wall.strike ? -1 : 0;
  const dexDir = -Math.sign(netDex);  // positive DEX = bearish flow → -1
  const aligned = wallDir !== 0 && dexDir !== 0 && wallDir === dexDir;

  if (wall.strength === 'high' && absScore > 30 && aligned) return 'high';
  if (absScore < 25) return 'low';
  return 'medium';
}

// Pick recommended strategies: primary from BIAS_TO_STRATEGY, then any other
// strategies whose .fits array includes this bias label.
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

// One-pass bias computation. Returns the full structured fields the bias card
// expects, minus the prose (summary + signals.* descriptions, filled by Gemini).
// v4.7.29: also extract VEX/TEX walls. We accept whatever key_levels Atlas or
// the local aggregator put on greeksRaw — defensive about field naming so this
// works for both providers. Returns {strike, value} | null for each.
function _extractGreekWall(greeksRaw, exposuresForChosen, keyName, fallbackRowField) {
  const kl = greeksRaw?.key_levels || exposuresForChosen?.key_levels || {};
  const direct = kl[keyName];
  if (direct && (direct.strike != null)) {
    return { strike: Number(direct.strike), value: Number(direct[fallbackRowField] ?? direct.value ?? 0) };
  }
  // Scan per-strike rows for the highest |value|
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

function _computeBiasSignals(symbol, expiration, spot, greeksRaw, exposuresForChosen) {
  const wall = _findGexWall(greeksRaw, exposuresForChosen);
  const { netGex, netDex } = _aggregateExposures(exposuresForChosen, greeksRaw?.portfolio_totals);
  const score = _computeDirectionalScore(spot, wall, netGex, netDex);
  const biasLabel = _scoreToBiasLabel(score);
  const confidence = _computeConfidence(score, wall, netDex, spot);
  const recommended = _recommendStrategies(biasLabel);

  // v4.7.29: VEX/TEX walls for the optional 3-lens divergence strip (Tickets tab).
  const vexWall = _extractGreekWall(greeksRaw, exposuresForChosen, 'vex_wall', 'net_vex');
  const texWall = _extractGreekWall(greeksRaw, exposuresForChosen, 'tex_wall', 'net_tex');

  // Structured facts (prose comes from Gemini afterwards)
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
    gex_wall_strike: wall.strike,
    gex_wall_strength: wall.strength,
    vex_wall_strike: vexWall?.strike ?? null,
    vex_wall_value:  vexWall?.value  ?? null,
    tex_wall_strike: texWall?.strike ?? null,
    tex_wall_value:  texWall?.value  ?? null,
    recommended_strategies: recommended,
    // structured facts the prose call will rephrase
    _facts: { wallSide, gammaRegime, dexSkewSide, netGex, netDex, wall, vexWall, texWall },
  };
}

// Tiny prose-only schema — the only thing Gemini still produces.
const _BIAS_PROSE_SCHEMA = {
  type: 'OBJECT',
  properties: {
    summary: { type: 'STRING' },
    signals: {
      type: 'OBJECT',
      properties: {
        dex_skew: { type: 'STRING' },
        gex_wall: { type: 'STRING' },
        gamma_regime: { type: 'STRING' },
        spot_vs_wall: { type: 'STRING' },
      },
      required: ['dex_skew','gex_wall','gamma_regime','spot_vs_wall'],
    },
  },
  required: ['summary','signals'],
};


// ==============================================
// MATH HELPERS (client-side scoring)
// ==============================================
const getMid = (c) => (c.mid != null ? c.mid : (c.bid + c.ask) / 2);

function widthBaseForDTE(dte, expectedMove) {
  // v4.7.32: dte is now fractional. Same-day (<1) keeps the 0.4× tight base.
  if (dte < 1) return 0.4 * expectedMove;
  if (dte <= 7) return 1.0 * expectedMove;
  return 1.5 * expectedMove;
}
// v4.7.32: format fractional DTE for the stats strip. Same-day expirations
// show as "Xh Ym" or "Xm"; otherwise integer days.
function _formatDteForDisplay(dte) {
  if (dte == null || isNaN(dte)) return '—';
  if (dte >= 1) return String(Math.floor(dte));
  const totalMin = Math.max(0, Math.round(dte * 1440));
  if (totalMin >= 60) {
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    return m === 0 ? `${h}h` : `${h}h ${m}m`;
  }
  return `${totalMin}m`;
}


// Reject bid<=0: genuinely untradeable as a short leg, even if recent volume exists.
// This is intentionally stricter than the chain-fetch prompt's filter (which keeps
// contracts where bid==0 but volume>0). Volume can lag the bid disappearing, and
// you can't fill a short at any price > $0 if there's no bid. If a far-OTM strike
// is "missing" from candidates despite appearing in the raw chain, this is why.
function findStrikeByDelta(contracts, side, targetAbsDelta) {
  let best = null, bestDiff = Infinity;
  for (const c of contracts) {
    if (c.side !== side || c.delta == null || c.bid <= 0) continue;
    const diff = Math.abs(Math.abs(c.delta) - targetAbsDelta);
    if (diff < bestDiff) { bestDiff = diff; best = c; }
  }
  return best;
}

// Same bid<=0 rejection rationale as findStrikeByDelta above.
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
  // Debit verticals (bull_call, bear_put) profit from directional move, not theta.
  // The credit-spread Δ rule doesn't apply — net delta is structurally negative
  // for debits because the long leg outweighs the short. Classify by:
  //   - whether the trade has time for the directional move to develop
  //   - whether the debit paid is reasonable for the width risked
  if (strategyType === 'debit') {
    const ratio = maxProfit / Math.max(0.01, maxLoss);  // payoff per dollar of debit
    if (dte <= 2)         return 'broken';        // not enough time for the move
    if (ratio < 0.30)     return 'capital_trap';  // paid too much premium for the width
    if (ratio < 0.60)     return 'thin';          // mediocre payoff per dollar
    return 'healthy';
  }
  // Credit verticals + condors + flies (theta-harvest plays):
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
  // Credit-style explanations (verticals, condors, flies):
  const d = netDelta.toFixed(3);
  if (h === 'broken')        return `Net Δ ${d} with ${dte} DTE — gamma will dominate before theta delivers.`;
  if (h === 'capital_trap')  return `Max loss ${(maxLoss/maxProfit).toFixed(1)}× max profit — one loss undoes many wins.`;
  if (h === 'thin')          return `Net Δ ${d} — only theta works. Needs days, not hours.`;
  if (h === 'directional')   return `Net Δ ${d} — directional bet, not premium harvest.`;
  return `Net Δ ${d} sits in the working zone. Theta + delta both contribute.`;
}

// ==============================================
// GEX WALL PENALTY (positional, strategy-aware)
// ==============================================
// The bias engine identifies the dominant gamma wall. This function asks: given
// THIS candidate's strikes, does the wall help the spread or hurt it?
//
// Returns { factor, reason, verdict }
//   factor : 0.5–1.05  — multiplier applied to EV for ranking
//   reason : human-readable string for rationale text + UI
//   verdict: 'good' | 'neutral' | 'warn' | 'bad'  — drives badge color
//
// Strategy-by-strategy positional rules:
//   bull_put     → wall ABOVE short put = good (acts as upward support)
//   bear_call    → wall BELOW short call = good (acts as resistance cap)
//   iron_condor  → wall BETWEEN shorts, ideally CENTERED = good (clean pin)
//   iron_butterfly / call_butterfly / put_butterfly → wall AT center = ideal pin
//   bull_call    → wall in profit zone (breakeven..short_call) = barrier, bad
//   bear_put     → wall in profit zone (short_put..breakeven) = barrier, bad
const WALL_STRENGTH_MULT = { high: 1.0, medium: 0.5, low: 0.25 };


// ==============================================
// ADAPTIVE GREEK EXPOSURES (v4.7.23 — denylist + chunked + cache)
// ==============================================
// analyze_greek_exposures is fast for liquid index/mega-cap symbols (5-15s)
// but slow-to-failing for heavy single-name option chains. Workaround:
//
//   - Known-heavy symbols: skip the single call entirely, fan out parallel
//     greek_exposure_single_expiration requests per expiration, aggregate.
//   - Other symbols: try the single call with a tight 30s deadline; on
//     timeout / 5xx / 'Internal server error', fall back to chunked.
//   - Cache the aggregated result for 5 minutes per (symbol, num_exp).
//
// Add to HEAVY_CHAIN_SYMBOLS as new failures surface.

const HEAVY_CHAIN_SYMBOLS = new Set([
  'MU', 'AMD', 'SMCI', 'COIN', 'PLTR', 'ARM', 'AVGO', 'MARA', 'MSTR',
]);

const _GEX_CACHE = new Map();   // key: `${symbol}_${num}`, value: { data, fetchedAt }
const _GEX_CACHE_TTL_MS = 5 * 60 * 1000;

function _gexCacheGet(symbol, num) {
  const k = `${symbol}_${num}`;
  const hit = _GEX_CACHE.get(k);
  if (!hit) return null;
  if (Date.now() - hit.fetchedAt > _GEX_CACHE_TTL_MS) {
    _GEX_CACHE.delete(k);
    return null;
  }
  return hit.data;
}

function _gexCacheSet(symbol, num, data) {
  _GEX_CACHE.set(`${symbol}_${num}`, { data, fetchedAt: Date.now() });
}

// Heuristic for fallback trigger: timeout, 5xx, or 'Internal server error'
function _shouldFallbackToChunked(err) {
  const m = String(err?.message || err || '').toLowerCase();
  return /timed out|http 5\d\d|internal server|gateway|service unavailable/.test(m);
}

// Aggregate N per-expiration responses into the shape downstream code expects
// (matches analyze_greek_exposures: { exposures_by_date, portfolio_totals,
// key_levels }). Defensive about which exact shape the single-expiration
// endpoint returns — tries common envelope keys.
function _aggregateChunkedExposures(perExpResults, expirations, symbol) {
  const exposures_by_date = {};
  const totals = { net_gex: 0, net_dex: 0, net_vex: 0, net_tex: 0 };
  const wallCandidates = { call_wall: null, put_wall: null };
  let bestCallWallGex = -Infinity;
  let bestPutWallGex  = -Infinity;

  for (let i = 0; i < perExpResults.length; i++) {
    const exp = expirations[i];
    const raw = perExpResults[i];
    if (!raw) continue;

    // Unwrap common envelopes — single-expiration may return either the bare
    // per-strike object or wrap it under {exposures: ...} / {data: ...}.
    const entry = raw.exposures_by_date && raw.exposures_by_date[exp]
                  ? raw.exposures_by_date[exp]
                  : (raw.exposures || raw.data || raw);
    exposures_by_date[exp] = entry;

    // Roll up portfolio totals (best-effort across naming conventions)
    const t = entry?.totals || entry?.net_totals || entry;
    if (t) {
      totals.net_gex += Number(t.net_gex ?? t.gex ?? t.gamma_total ?? 0);
      totals.net_dex += Number(t.net_dex ?? t.dex ?? t.delta_total ?? 0);
      totals.net_vex += Number(t.net_vex ?? t.vex ?? t.vanna_total ?? 0);
      totals.net_tex += Number(t.net_tex ?? t.tex ?? t.theta_total ?? 0);
    }

    // Surface per-expiration walls if provided; keep the strongest as overall
    const kl = entry?.key_levels || raw?.key_levels;
    if (kl) {
      const cw = kl.call_wall;
      const pw = kl.put_wall;
      if (cw && (cw.gex ?? 0) > bestCallWallGex) {
        bestCallWallGex = cw.gex ?? 0;
        wallCandidates.call_wall = cw;
      }
      if (pw && (pw.gex ?? 0) > bestPutWallGex) {
        bestPutWallGex = pw.gex ?? 0;
        wallCandidates.put_wall = pw;
      }
    }
  }

  return {
    symbol,
    exposures_by_date,
    portfolio_totals: totals,
    key_levels: wallCandidates,
    _chunked: true,           // marker for diagnostics
  };
}

async function _chunkedGreekExposures(symbol, num_expirations) {
  // 1) Fetch the expirations list (cheap call)
  const expData = await _atlasCall('Option-Expiration-Dates', { symbol, filter: 'next_10' });
  const all = expData.expirations || expData.dates || expData.expiration_dates
              || (Array.isArray(expData) ? expData : []);
  const expirations = all.slice(0, num_expirations).map(String);
  if (expirations.length === 0) throw new Error(`Provider chunked greek_exposures: no expirations returned for ${symbol}`);

  // 2) Parallel fan-out per expiration
  const perExp = await Promise.all(
    expirations.map(exp =>
      _atlasCall('Greek-Exposure-Single-Expiration', { symbol, expiration: exp })
        .catch(e => {
          console.warn(`[Greek exposures] chunked ${symbol} ${exp} failed:`, e?.message || e);
          return null;
        })
    )
  );

  if (perExp.every(r => r == null)) {
    throw new Error(`Provider chunked greek_exposures: all ${expirations.length} per-expiration calls failed for ${symbol}`);
  }

  // 3) Aggregate to the analyze_greek_exposures shape
  return _aggregateChunkedExposures(perExp, expirations, symbol);
}

async function _getGreekExposures(symbol, num_expirations = 3) {
  const sym = String(symbol || '').toUpperCase();
  const cached = _gexCacheGet(sym, num_expirations);
  if (cached) {
    console.info(`[Greek exposures] cache hit for ${sym} (num_exp=${num_expirations})`);
    return cached;
  }

  let data;
  if (HEAVY_CHAIN_SYMBOLS.has(sym)) {
    console.info(`[Greek exposures] ${sym} on denylist → chunked path immediately`);
    data = await _chunkedGreekExposures(sym, num_expirations);
  } else {
    try {
      data = await _atlasCall('analyze_greek_exposures', { symbol: sym, num_expirations });
    } catch (e) {
      if (_shouldFallbackToChunked(e)) {
        console.warn(`[Greek exposures] ${sym} fast path failed (${e.message}) → falling back to chunked`);
        data = await _chunkedGreekExposures(sym, num_expirations);
      } else {
        throw e;
      }
    }
  }

  _gexCacheSet(sym, num_expirations, data);
  return data;
}


// ==============================================
// BLACK-SCHOLES SPREAD PROJECTION (v4.7.20)
// ==============================================
// Pricing engine for the Lookup tab. Given a candidate's per-leg contracts
// (strike, side, IV, +/-1 long/short), compute the theoretical spread
// premium at any (hypothetical spot, time-to-expiration) pair.
//
// Assumptions, documented because this is decision-relevant math:
//   - European exercise (good for index, slight error for early-exercisable
//     American single-name puts deep ITM near dividends — minor here)
//   - Sticky-strike IV (each leg's IV stays attached to its strike as spot
//     moves; we do NOT model the vol surface). Realistic for short-DTE
//     intraday projections; less so for multi-day projections through a
//     vol-changing event.
//   - Constant risk-free rate r = 5% (close to SOFR mid-2026).
//   - No dividends.
//
// Result is anchored: at (current_spot, current_dte) the grid is forced
// to match the live mid (candidate.net_premium) so the user sees their
// real entry as the center cell, with BS sensitivity radiating outward.

function _normCdf(x) {
  // Abramowitz & Stegun 7.1.26 — |error| < 7.5e-8
  const a1 =  0.254829592, a2 = -0.284496736, a3 =  1.421413741;
  const a4 = -1.453152027, a5 =  1.061405429, p  =  0.3275911;
  const sign = x < 0 ? -1 : 1;
  const ax = Math.abs(x) / Math.SQRT2;
  const t = 1.0 / (1.0 + p * ax);
  const erf = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-ax * ax);
  return 0.5 * (1.0 + sign * erf);
}

function _bsPrice(S, K, T, r, sigma, side) {
  if (S == null || K == null || S <= 0 || K <= 0) return null;
  if (T <= 0) return side === 'call' ? Math.max(0, S - K) : Math.max(0, K - S);
  if (sigma == null || sigma <= 0) {
    return side === 'call'
      ? Math.max(0, S - K * Math.exp(-r * T))
      : Math.max(0, K * Math.exp(-r * T) - S);
  }
  const sqT = Math.sqrt(T);
  const d1 = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqT);
  const d2 = d1 - sigma * sqT;
  return side === 'call'
    ? S * _normCdf(d1) - K * Math.exp(-r * T) * _normCdf(d2)
    : K * Math.exp(-r * T) * _normCdf(-d2) - S * _normCdf(-d1);
}

// Sum of signed leg prices. longShort: +1 long, -1 short, -2 butterfly mid.
// Returned `total` = "long minus short" — for DEBIT this is net cost
// (positive); for CREDIT it is the negative of the net receipt.
function _priceSpreadAtSpot(legs, S, T, r) {
  if (!legs || !legs.length || S == null) return null;
  if (r == null) r = 0.05;
  let total = 0;
  let anyMissingIv = false;
  for (const leg of legs) {
    const sigma = leg.iv != null && leg.iv > 0 ? leg.iv : null;
    if (sigma == null) anyMissingIv = true;
    const px = _bsPrice(S, leg.strike, T, r, sigma, leg.side);
    if (px == null) return null;
    total += leg.longShort * px;
  }
  return { total, missingIv: anyMissingIv };
}


// v4.7.33: ET clock-time helpers for the Lookup grid time axis.
// All times are anchored to America/New_York (handles EST/EDT automatically
// via Intl.DateTimeFormat).
//
// Session bounds: 9:30am - 4:00pm ET, Mon-Fri only.
// Holidays are not handled (half-days like day-before-Thanksgiving still
// show as full session). Acceptable simplification for an intraday tool;
// the worst case is the last hour or two never fills because the actual
// session closed early. Add a holiday calendar later if it bites.

const _ET_OPEN_MIN  = 9 * 60 + 30;   // 9:30 ET in minutes-of-day
const _ET_CLOSE_MIN = 16 * 60;       // 4:00 ET in minutes-of-day

// Decompose a ms-timestamp into ET wall-clock components.
function _etParts(ms) {
  const d = new Date(ms);
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    weekday: 'short', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: 'numeric', minute: 'numeric', hour12: false,
  }).formatToParts(d);
  const get = k => parts.find(p => p.type === k)?.value;
  return {
    weekday: get('weekday'),
    year: Number(get('year')),
    month: Number(get('month')),
    day: Number(get('day')),
    hour: Number(get('hour')) % 24,   // Intl can emit "24" for midnight
    minute: Number(get('minute')),
  };
}

// v4.7.34: clock labels render in the BROWSER's local timezone. Session
// bounds (open/close, weekend skip) stay anchored to America/New_York via
// _etParts/_inSessionET/_nextSessionOpenET — those represent real market
// hours and don't change with the user's location. Only the visible label
// formats in local time, so a user in PT sees "1:00p" for the 4:00pm ET
// close and a user in CET sees "10:00p".
//
// "2:15p" / "9:30a" — short clock label in the browser's local time
function _formatClockET(ms) {
  // Use Intl.DateTimeFormat with NO timeZone override so it uses the
  // browser's default (whatever the user's OS reports).
  const parts = new Intl.DateTimeFormat('en-US', {
    hour: 'numeric', minute: '2-digit', hour12: true,
  }).formatToParts(new Date(ms));
  const get = k => parts.find(p => p.type === k)?.value;
  const h12 = get('hour') || '?';
  const mm  = get('minute') || '00';
  const ampm = (get('dayPeriod') || '').toLowerCase().startsWith('p') ? 'p' : 'a';
  return `${h12}:${mm}${ampm}`;
}

// "Tue 9:30a" — when the cell falls on a different day than the reference,
// also rendered in browser-local time.
function _formatClockETWithDay(ms) {
  const day = new Intl.DateTimeFormat('en-US', { weekday: 'short' }).format(new Date(ms));
  return `${day} ${_formatClockET(ms)}`;
}

// Compose a UTC ms for "{dateStr} {hour}:{minute} ET", handling DST.
// dateStr is "YYYY-MM-DD" in the ET calendar.
function _msAtETClock(dateStr, hour, minute) {
  // Try EDT (UTC-4) first, then EST (UTC-5). Pick the one whose ET render
  // matches the requested hour. (DST transition days only have one valid.)
  for (const offHrs of [4, 5]) {
    const hh = String(hour).padStart(2, '0');
    const mm = String(minute).padStart(2, '0');
    const off = `-0${offHrs}:00`;
    const ms = Date.parse(`${dateStr}T${hh}:${mm}:00${off}`);
    if (isNaN(ms)) continue;
    const p = _etParts(ms);
    if (p.hour === hour && p.minute === minute) return ms;
  }
  // Fallback: best-effort, treat as UTC offset 5 (winter)
  const hh = String(hour).padStart(2, '0');
  const mm = String(minute).padStart(2, '0');
  return Date.parse(`${dateStr}T${hh}:${mm}:00-05:00`);
}

// Minute-of-day in ET (0-1439).
function _etMinOfDay(ms) {
  const p = _etParts(ms);
  return p.hour * 60 + p.minute;
}

// Is this ms inside a regular-hours session (Mon-Fri, 9:30-16:00 ET)?
function _inSessionET(ms) {
  const p = _etParts(ms);
  if (p.weekday === 'Sat' || p.weekday === 'Sun') return false;
  const mod = p.hour * 60 + p.minute;
  return mod >= _ET_OPEN_MIN && mod < _ET_CLOSE_MIN;
}

// Round the given ms UP to the next 15-min boundary in ET.
function _ceil15MinET(ms) {
  const p = _etParts(ms);
  const remainder = p.minute % 15;
  if (remainder === 0) return ms;
  return ms + (15 - remainder) * 60_000;
}

// Get next session open (9:30am ET) at-or-after `afterMs`. Three cases:
//   (a) Weekday BEFORE 9:30am ET → today's 9:30am
//   (b) Weekday AFTER 16:00 ET   → next weekday's 9:30am
//   (c) Weekend                  → next Monday's 9:30am
function _nextSessionOpenET(afterMs) {
  const p0 = _etParts(afterMs);
  const isWeekend = p0.weekday === 'Sat' || p0.weekday === 'Sun';
  const minOfDay = p0.hour * 60 + p0.minute;
  // Case (a): weekday and current ET clock is before today's open → today's 9:30
  if (!isWeekend && minOfDay < _ET_OPEN_MIN) {
    const dateStr = `${p0.year}-${String(p0.month).padStart(2,'0')}-${String(p0.day).padStart(2,'0')}`;
    return _msAtETClock(dateStr, 9, 30);
  }
  // Otherwise advance by calendar days until we land on a weekday
  let cursor = Date.UTC(p0.year, p0.month - 1, p0.day, 12, 0, 0);
  for (let step = 0; step < 7; step++) {
    cursor += 86_400_000;
    const pp = _etParts(cursor);
    if (pp.weekday !== 'Sat' && pp.weekday !== 'Sun') {
      const dateStr = `${pp.year}-${String(pp.month).padStart(2,'0')}-${String(pp.day).padStart(2,'0')}`;
      return _msAtETClock(dateStr, 9, 30);
    }
  }
  return afterMs + 86_400_000;
}

// 2D grid (rows = hypothetical spot, cols = time slices) of projected
// premiums. Anchored so the center cell at (currentSpot, now) equals
// candidate.net_premium exactly; BS sensitivity provides the surface.
function _buildProjectionGrid(candidate, currentSpot, expectedMove, opts) {
  const ROWS = (opts && opts.rows) || 9;
  const COLS = (opts && opts.cols) || 5;
  if (!candidate || !candidate.legs || !candidate.legs.length) return null;
  if (currentSpot == null || candidate.dte == null) return null;

  const isCredit = candidate.type === 'credit';
  const T0 = Math.max(0, candidate.dte / 365);

  const anchorBs = _priceSpreadAtSpot(candidate.legs, currentSpot, T0, 0.05);
  if (!anchorBs) return null;
  const liveSigned = isCredit ? -candidate.net_premium : candidate.net_premium;
  const offset = liveSigned - anchorBs.total;

  const rangePct = expectedMove > 0 && currentSpot > 0
    ? Math.min(0.30, Math.max(0.06, (2 * expectedMove) / currentSpot))
    : 0.10;
  const spotAxis = [];
  for (let i = 0; i < ROWS; i++) {
    const frac = (i - (ROWS - 1) / 2) / ((ROWS - 1) / 2);
    spotAxis.push(currentSpot * (1 + frac * rangePct));
  }

  // v4.7.33: wall-clock ET time axis. Walks trading minutes from "now" to
  // expiration close (4:00pm ET, PM-settled assumption), 15-min stride. For
  // 1+ DTE the axis continues across the overnight gap into next sessions.
  // Column labels are real ET clock times ("2:15p", "Tue 9:30a") so a trader
  // sees actual session timestamps, not abstract offsets.
  const MIN_STEP_MIN = 15;
  const MAX_COLS = 12;
  const nowMs = Date.now();
  // Expiration close: assume 4:00pm ET (PM-settled). AM-settled monthlies
  // would settle at the morning auction; we accept that small inaccuracy
  // until a settlement-type field appears on chain data.
  const expirationMs = (() => {
    const expStr = candidate.expiration || (opts && opts.expiration);
    if (!expStr) return nowMs + Math.max(0, candidate.dte) * 86_400_000;
    return _msAtETClock(String(expStr), 16, 0);
  })();
  const todayP = _etParts(nowMs);
  const firstCellDay = todayP.weekday;
  // v4.7.34: capture the local (browser) date for the first cell, used to
  // decide when later cells need a day-of-week prefix.
  const firstCellLocalDay = new Date(nowMs).getDay();
  const firstCellLocalDate = new Date(nowMs).getDate();

  // Build the cell list. Start at "now" (or the next session open if we're
  // before market hours / over the weekend), then walk 15-min increments,
  // skipping overnight + weekends until expirationMs.
  const cells = [];
  let cursor;
  if (_inSessionET(nowMs)) {
    cursor = nowMs;
  } else {
    cursor = _nextSessionOpenET(nowMs);
  }
  // First cell: keep label as "now" if we're inside today's session, else show
  // the upcoming session-open clock so the user knows we jumped.
  cells.push({
    ms: Math.min(cursor, expirationMs),
    label: cursor === nowMs ? 'now' : _formatClockETWithDay(cursor),
    dteRem: Math.max(0, (expirationMs - Math.min(cursor, expirationMs)) / 86_400_000),
  });

  // Advance: round up to next 15-min boundary so columns align on :00 :15 :30 :45
  cursor = _ceil15MinET(cursor + 60_000);   // +1 min to avoid noop on already-aligned now

  while (cells.length < MAX_COLS - 1) {   // reserve one slot for the 'exp' tail
    if (cursor >= expirationMs) break;
    if (!_inSessionET(cursor)) {
      cursor = _nextSessionOpenET(cursor);
      if (cursor >= expirationMs) break;
    }
    // v4.7.34: sameDay check uses LOCAL (browser) date, not ET date — the
    // label's day prefix should reflect what the user sees, not the market\'s
    // timezone. A user in PT viewing a Tue 4pm ET close sees "1:00p" today
    // (Tue local); a 6pm-ET cell would be "3:00p" still on local Tue.
    const cellLocalDay = new Date(cursor).getDay();
    const cellLocalDate = new Date(cursor).getDate();
    const sameDay = cellLocalDay === firstCellLocalDay && cellLocalDate === firstCellLocalDate;
    const dteRem = Math.max(0, (expirationMs - cursor) / 86_400_000);
    cells.push({
      ms: cursor,
      label: sameDay ? _formatClockET(cursor) : _formatClockETWithDay(cursor),
      dteRem,
    });
    cursor += MIN_STEP_MIN * 60_000;
  }

  // Final 'exp' cell — but only if we haven't reached it yet via the step loop
  if (cells.length === 0 || cells[cells.length - 1].ms < expirationMs) {
    cells.push({ ms: expirationMs, label: 'exp', dteRem: 0 });
  }

  const timeAxis = cells;

  const grid = spotAxis.map(S =>
    timeAxis.map(t => {
      const T = Math.max(0, t.dteRem / 365);
      const px = _priceSpreadAtSpot(candidate.legs, S, T, 0.05);
      if (!px) return null;
      const adjusted = px.total + offset;
      return isCredit ? -adjusted : adjusted;
    })
  );

  return {
    spotAxis, timeAxis, grid,
    currentSpot,
    currentPremium: candidate.net_premium,
    isCredit,
    centerRow: Math.floor((ROWS - 1) / 2),
    missingIv: anchorBs.missingIv,
  };
}


// ==============================================
// LOOKUP ANNOTATIONS (v4.7.29) — VEX/TEX clusters + cell decomposition
// ==============================================
// Per-strike dealer VEX/TEX over the visible chain. We sum |vega × OI| and
// |theta × OI| per strike and call a strike "in cluster" when it sits in the
// top-quartile of that absolute exposure. The Lookup grid then marks each
// spot row whose nearest strike falls in the VEX cluster with an amber dot,
// and (only for ≤1 DTE spreads) marks the back half of the time axis with
// an hourglass to flag accelerating theta. Anything beyond ≤1 DTE has
// roughly-constant theta per day, so the column annotation is suppressed.

function _computePerStrikeVexTex(contracts) {
  if (!Array.isArray(contracts) || contracts.length === 0) return new Map();
  const m = new Map(); // strike -> { vex, tex }
  for (const c of contracts) {
    const K = Number(c.strike);
    if (!isFinite(K)) continue;
    const oi = Number(c.open_interest) || 0;
    if (oi <= 0) continue;
    const v = (Number(c.vega)  || 0) * oi;
    const t = (Number(c.theta) || 0) * oi;
    const e = m.get(K) || { vex: 0, tex: 0 };
    e.vex += v;
    e.tex += t;
    m.set(K, e);
  }
  return m;
}

function _quartileThreshold(values, q = 0.75) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor(sorted.length * q)));
  return sorted[idx];
}

function _buildLookupAnnotations(candidate, contracts, grid) {
  if (!grid || !contracts) return { vexClusterRows: new Set(), texClusterCols: new Set(), vexClusterStrikes: new Set() };
  // Filter to the candidate's expiration so we don't average across DTEs.
  const sameExp = contracts.filter(c => c.expiration === candidate.expiration);
  const perStrike = _computePerStrikeVexTex(sameExp);
  if (perStrike.size === 0) return { vexClusterRows: new Set(), texClusterCols: new Set(), vexClusterStrikes: new Set() };

  const vexAbs = [...perStrike.values()].map(v => Math.abs(v.vex));
  const vexCut = _quartileThreshold(vexAbs, 0.75);
  const vexClusterStrikes = new Set();
  for (const [K, { vex }] of perStrike) {
    if (Math.abs(vex) >= vexCut && vexCut > 0) vexClusterStrikes.add(K);
  }
  // Map each spot row to its nearest strike → in cluster?
  const strikes = [...perStrike.keys()].sort((a, b) => a - b);
  const vexClusterRows = new Set();
  grid.spotAxis.forEach((S, i) => {
    let nearest = null, best = Infinity;
    for (const K of strikes) {
      const d = Math.abs(K - S);
      if (d < best) { best = d; nearest = K; }
    }
    if (nearest != null && vexClusterStrikes.has(nearest)) vexClusterRows.add(i);
  });
  // TEX columns: only flag for ≤1 DTE where final-hour decay actually matters.
  const texClusterCols = new Set();
  if (candidate.dte <= 1) {
    const halfway = Math.ceil(grid.timeAxis.length / 2);
    for (let j = halfway; j < grid.timeAxis.length; j++) texClusterCols.add(j);
  }
  return { vexClusterRows, texClusterCols, vexClusterStrikes };
}

// Decompose a single cell's P&L into theta / delta / (gamma+vega residual).
// Inputs come from the candidate's already-computed Greeks and the BS grid:
//   theta_dollar_per_day is candidate.net_theta_dollar (PER-CONTRACT dollars)
//   net_delta            is candidate.net_spread_delta (per share)
// We translate elapsed time to days, spot move to dollars-per-share, and the
// residual to "everything BS couldn't attribute to ∂P/∂t or ∂P/∂S linearly."
// All three components are reported in DOLLARS PER CONTRACT (the same units
// the cell shows in its tooltip P&L line).
function _decomposeCellPnl(candidate, grid, rowIdx, colIdx) {
  if (!candidate || !grid) return null;
  const live = grid.currentPremium;
  const px = grid.grid[rowIdx]?.[colIdx];
  if (px == null) return null;
  const isCredit = grid.isCredit;
  const pnlPremiumDelta = isCredit ? (live - px) : (px - live);   // per-share
  const totalDollar = pnlPremiumDelta * 100;

  const elapsedMin = colIdx * 15;            // grid step is 15-min
  const elapsedDays = elapsedMin / 1440;
  const dS = grid.spotAxis[rowIdx] - grid.currentSpot;

  // theta is per-day, positive for credit-collecting spreads (working in your favor)
  // Sign: for credit spreads, time passing helps → +theta. For debit, -theta. The
  // candidate.net_theta_dollar already carries that sign.
  const thetaDollar = (Number(candidate.net_theta_dollar) || 0) * elapsedDays * 100;
  // delta-dollar per-share × dS × 100 → dollars per contract. net_spread_delta is
  // signed for the position direction.
  const deltaDollar = (Number(candidate.net_spread_delta) || 0) * dS * 100;
  // Residual: gamma curvature + vega (we hold IV flat) + cross terms
  const residual = totalDollar - thetaDollar - deltaDollar;

  return {
    total: totalDollar,
    theta: thetaDollar,
    delta: deltaDollar,
    residual,
  };
}


// ==============================================
// LIMIT-ORDER TARGET PREMIUM (v4.7.16 — width-scaled tiers)
// ==============================================
// Given a scored candidate, compute the premium you'd need to make the trade
// hit a given edge target. Lets you place a GTC limit order at that price
// instead of taking what the market currently offers.
//
// The previous version used a constant +$5 EV target, which was nonsense:
// $5 on a $50-wide spread is 10% mispricing (fantasy), while $5 on a $500
// spread is 1% (reasonable). Now each tier is denominated as a FRACTION OF
// WIDTH, which is how institutional desks frame edge:
//
//   modest    — 0.75% of width   "patient limit, often fills"
//   balanced  — 1.50% of width   "typical achievable on minor dislocations"
//   strong    — 3.00% of width   "only fills on liquidity events / IV crush"
//
// Break-even (EV = 0) is also exposed as the math-fair-value reference.
//
// For CREDIT spreads (sell premium): you need the market to OFFER higher
// credit. Place a limit SELL at target_premium or better.
// For DEBIT spreads (pay premium): you need the market to ACCEPT lower
// debit. Place a limit BUY at target_premium or better.
const LIMIT_EDGE_TIERS = [
  { name: 'modest',   pct: 0.0075, hint: 'often fills' },
  { name: 'balanced', pct: 0.0150, hint: 'patient' },
  { name: 'strong',   pct: 0.0300, hint: 'on dislocations' },
];

function _computeLimitPremiums(candidate) {
  const { type, pop_pct, max_profit, max_loss, net_premium, wall_penalty } = candidate;
  const pop = (pop_pct || 0) / 100;
  const factor = Math.max(0.01, wall_penalty?.factor ?? 1.0);
  const width = (max_profit || 0) + (max_loss || 0);   // total spread width
  if (width <= 0 || pop <= 0 || pop >= 1) return null;

  const isCredit = type === 'credit';
  const breakeven = isCredit ? (1 - pop) * width : pop * width;
  const current   = net_premium;

  const tiers = LIMIT_EDGE_TIERS.map(t => {
    // Target EV as a dollar value, scaled to the spread's own width
    const targetEV = t.pct * width;
    // displayed_EV = raw_EV × wall_factor — invert to get raw_EV we need
    const target = isCredit
      ? breakeven + targetEV / factor
      : breakeven - targetEV / factor;
    const delta    = isCredit ? target - current : current - target;
    const feasible = isCredit ? target < width   : target > 0.05;
    return {
      name: t.name,
      hint: t.hint,
      pctOfWidth: t.pct,
      targetEV,           // dollar EV for this tier
      target,             // premium price for the limit order
      delta,              // positive = market needs to move TOWARD us by this much
      feasible,           // physically reachable on this spread?
    };
  });

  return {
    side: isCredit ? 'credit' : 'debit',
    current,
    breakeven,
    tiers,
    // Backward-compat scalars — the *modest* tier is the default reference
    // for composite scoring + trade-ticket fallback. Modest is the realistic
    // patience-limit, not the aggressive strong-tier number.
    target:    tiers[0].target,
    targetEV:  tiers[0].targetEV,
    delta:     tiers[0].delta,
    feasible:  tiers[0].feasible,
  };
}

function computeWallPenalty(strategy, strikes, breakevens, wallStrike, wallStrength) {
  if (!wallStrike || !strikes) return { factor: 1.0, reason: null, verdict: 'neutral' };
  const w = wallStrike;
  const s = WALL_STRENGTH_MULT[wallStrength] ?? 0.5;
  const fmt = (v) => Number(v).toFixed(2);

  switch (strategy) {
    case 'bull_put': {
      const sp = strikes.short_put;
      if (sp == null) return { factor: 1.0, reason: null, verdict: 'neutral' };
      if (w > sp) return { factor: 1.0, reason: `Wall ${fmt(w)} above short put — acts as upward support.`, verdict: 'good' };
      if (Math.abs(w - sp) < 0.01) return { factor: 1 - 0.4 * s, reason: `Wall AT short put ${fmt(sp)} — high pin risk on the short strike.`, verdict: 'bad' };
      const distPct = (sp - w) / sp;
      const proximity = Math.max(0, 1 - distPct * 10);
      return {
        factor: 1 - 0.4 * proximity * s,
        reason: `Wall ${fmt(w)} below short put ${fmt(sp)} — no upward pull; price can drift through.`,
        verdict: proximity > 0.5 ? 'bad' : 'warn',
      };
    }

    case 'bear_call': {
      const sc = strikes.short_call;
      if (sc == null) return { factor: 1.0, reason: null, verdict: 'neutral' };
      if (w < sc) return { factor: 1.0, reason: `Wall ${fmt(w)} below short call — caps upside as resistance.`, verdict: 'good' };
      if (Math.abs(w - sc) < 0.01) return { factor: 1 - 0.4 * s, reason: `Wall AT short call ${fmt(sc)} — high pin risk on the short strike.`, verdict: 'bad' };
      const distPct = (w - sc) / sc;
      const proximity = Math.max(0, 1 - distPct * 10);
      return {
        factor: 1 - 0.4 * proximity * s,
        reason: `Wall ${fmt(w)} above short call ${fmt(sc)} — no downward push; price can drift through.`,
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
        return {
          factor: 1 - 0.3 * (1 - centered) * s,
          reason: `Wall ${fmt(w)} inside body ${fmt(sp)}/${fmt(sc)} but skewed — pinning will pull price toward nearer short.`,
          verdict: 'warn',
        };
      }
      const dist = w < sp ? sp - w : w - sc;
      const distPct = dist / ((sp + sc) / 2);
      const proximity = Math.max(0, 1 - distPct * 10);
      return {
        factor: 1 - 0.5 * proximity * s,
        reason: `Wall ${fmt(w)} OUTSIDE shorts ${fmt(sp)}/${fmt(sc)} — pinning drags price out of profit zone.`,
        verdict: 'bad',
      };
    }

    case 'iron_butterfly':
    case 'call_butterfly':
    case 'put_butterfly': {
      const center = strikes.center;
      if (center == null) return { factor: 1.0, reason: null, verdict: 'neutral' };
      const offsetPct = Math.abs(w - center) / center;
      if (offsetPct < 0.005) return { factor: 1.0, reason: `Wall ${fmt(w)} pinned at center ${fmt(center)} — ideal alignment.`, verdict: 'good' };
      const proximity = Math.max(0, 1 - offsetPct * 30); // sharp falloff: fly profit zone is narrow
      return {
        factor: 1 - 0.5 * (1 - proximity) * s,
        reason: `Wall ${fmt(w)} ${(offsetPct * 100).toFixed(1)}% off center ${fmt(center)} — pin misses target.`,
        verdict: proximity < 0.3 ? 'bad' : 'warn',
      };
    }

    case 'bull_call': {
      // Wall as ATTRACTOR — price drifts toward it. Bull_call needs price ↑ past
      // breakeven. So:
      //   wall >= short_call            → pulls past max-profit (capped, good)
      //   be <= wall < short_call       → pulls INTO profit zone (good)
      //   sp/spot < wall < be           → pulls toward but not past BE (warn)
      //   wall < spot                   → pulls AWAY from BE (bad)
      const sc = strikes.short_call;
      const lp = strikes.long_call ?? strikes.lp;  // lower strike = current bull leg
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
      // wall is below breakeven — wall pulls price DOWN, away from where we need it
      const distFromBe = (be - w) / be;
      const proximity = Math.max(0, 1 - distFromBe * 5);  // sharper falloff than puts
      return {
        factor: 1 - 0.5 * proximity * s,
        reason: `Wall ${fmt(w)} below breakeven ${fmt(be)} — pulls price AWAY from profit zone.`,
        verdict: proximity > 0.4 ? 'bad' : 'warn',
      };
    }

    case 'bear_put': {
      // Mirror of bull_call. Bear_put needs price ↓ past breakeven. So:
      //   wall <= short_put             → pulls past max-profit cap (good)
      //   short_put < wall <= be        → pulls INTO profit zone (good)
      //   be < wall                     → pulls AWAY from BE upward (bad)
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
      // wall above breakeven — wall pulls price UP, away from where we need it
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
    // HEURISTIC, not a real probability: blends long/short delta by how much of
    // the width the trader paid for. Directionally right (deeper ITM = higher POP)
    // but does NOT model the lognormal price distribution. True debit POP would
    // be N(d2) at the breakeven strike under Black-Scholes, which depends on T,
    // IV, and skew. Expect this number to be off by 10-20% in either direction
    // for short-DTE high-IV setups. Treat displayed debit POP as rough.
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

  // Strike map for downstream wall-penalty math. We tag by side so the wall
  // function can find the relevant strike regardless of credit vs debit framing.
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

  // Center for iron-fly is roughly (sp+sc)/2; for true ATM fly sp.strike==sc.strike.
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
  // 1 long low, 2 short mid, 1 long high
  if (!low || !mid || !high) return null;
  const w1 = mid.strike - low.strike;
  const w2 = high.strike - mid.strike;
  if (Math.abs(w1 - w2) > 0.5 * Math.min(w1, w2)) return null; // require near-symmetric
  const debit = getMid(low) + getMid(high) - 2 * getMid(mid);
  if (debit <= 0.01) return null;
  const maxProfit = Math.min(w1, w2) - debit;
  const maxLoss = debit;
  const popPct = 25; // butterflies inherently low POP; rough estimate
  const ev = (popPct / 100) * maxProfit - (1 - popPct / 100) * maxLoss;
  const netDelta = Math.abs(low.delta - 2 * mid.delta + high.delta);
  const netTheta = low.theta - 2 * mid.theta + high.theta;
  const health = classifyHealth('credit', netDelta, dte, maxLoss, maxProfit);
  const liquidity = classifyLiquidity(low, mid, high);
  const sideChar = side[0].toUpperCase();

  // For butterflies, center is the only strike the wall penalty cares about.
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
  // NOTE on debit-spread long-leg selection:
  // We pick the long leg by delta = (1 - targetDelta), which is symmetric-but-aesthetic,
  // not principled — there's no mathematical reason a 20Δ short on a credit spread
  // implies an 80Δ long on a debit spread. Ideally debit spreads would have their own
  // ITM-ness control. The Math.max(0.5, ...) floor below is defensive: with the slider
  // capped at 0.40 it never activates today, but if someone widens the slider beyond
  // 0.50 the formula would otherwise produce sub-ATM longs and break the "long the
  // higher-delta leg" intent. Keep the floor; revisit if/when debit spreads get their
  // own control.
  if (strategy === 'bull_call') {
    // Long the higher-delta (closer to ATM) call; short the further OTM
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
  // Short ATM call + put, long wings
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
  // Center near ATM (delta ~0.50)
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

  // For verticals + condor: vary delta. For flies: vary width.
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
    // Strategy-aware GEX wall penalty. Adjusts EV used for ranking; keeps raw EV for display.
    const wallPen = computeWallPenalty(strategy, c.strikes, c.breakevens, walls?.strike, walls?.strength);
    const evAdjusted = c.ev * wallPen.factor;
    let rationale = `${cfg.label} variant — short Δ target ${cfg.delta.toFixed(2)}, width factor ${cfg.width.toFixed(1)}×.`;
    if (wallPen.reason) rationale += ` ${wallPen.reason}`;
    const enriched = { ...c, label: cfg.label, rationale, wall_penalty: wallPen, ev_adjusted: evAdjusted };
    enriched.limit_premiums = _computeLimitPremiums(enriched);  // width-scaled tiers (modest/balanced/strong)
    enriched.composite_score = _compositeScore(enriched);
    enriched.composite_verdict = _compositeVerdict(enriched.composite_score, enriched.ev_adjusted ?? enriched.ev);
    candidates.push(enriched);
  }
  return candidates;
}

// ==============================================
// PROVIDER QUOTA PARSER (v4.7+)
// ==============================================
// Provider docs list { plan, status, usage, limits } but the nested field
// names drift across versions and account tiers. Walk the entire response
// tree (up to a sane depth) and fuzzy-match keys by regex so we don't have
// to keep extending a fragile allowlist of literal field names.
//
// One-shot raw dump is logged to console (info level) so the user can see
// the actual shape in DevTools if the pill ever shows "unavailable".
let _quotaRawLogged = false;
function _parseAtlasQuota(resp) {
  if (!resp || typeof resp !== 'object') return null;
  if (!_quotaRawLogged) {
    try { console.info('[Provider quota] raw response:', resp); } catch (_) {}
    _quotaRawLogged = true;
  }

  // Recursive deep-walker: returns the first leaf value whose key matches `re`.
  // Skips arrays of objects past depth 4 to avoid huge chain payloads.
  const findByRe = (d, re, depth = 0) => {
    if (depth > 6 || d == null) return null;
    if (typeof d !== 'object') return null;
    if (Array.isArray(d)) {
      if (depth > 4) return null;
      for (const item of d) {
        const hit = findByRe(item, re, depth + 1);
        if (hit != null) return hit;
      }
      return null;
    }
    // 1) Direct key match at this level (prefer scalar leaves)
    for (const [k, v] of Object.entries(d)) {
      if (re.test(k) && (v == null || typeof v !== 'object')) return v;
    }
    // 2) Direct key match where value is an object — pull a scalar leaf out
    for (const [k, v] of Object.entries(d)) {
      if (re.test(k) && v && typeof v === 'object' && !Array.isArray(v)) {
        // Common shape: { usage: { count: 17 } } / { limits: { calls: 200 } }
        for (const inner of Object.values(v)) {
          if (inner != null && typeof inner !== 'object') return inner;
        }
      }
    }
    // 3) Recurse into nested objects
    for (const v of Object.values(d)) {
      const hit = findByRe(v, re, depth + 1);
      if (hit != null) return hit;
    }
    return null;
  };

  const num = (v) => {
    if (v == null) return null;
    if (typeof v === 'number') return Number.isFinite(v) ? v : null;
    const n = Number(String(v).replace(/[, ]/g, ''));
    return Number.isFinite(n) ? n : null;
  };

  // Fuzzy regexes — match any reasonable spelling of the concept
  const RE_PLAN     = /^(plan|tier|subscription_(plan|tier|name)|subscription|product|sku)$/i;
  const RE_STATUS   = /^(status|state|subscription_status|account_status|is_active|active)$/i;
  const RE_USED     = /(^|_)(calls?_?used|used_calls|usage_count|usage|consumed|calls_this_(month|period|cycle)|requests?_made|request_count|api_calls_used|current_usage|count|calls|made|spent)($|_)/i;
  const RE_LIMIT    = /(^|_)(calls?_?limit|call_quota|limit|max_calls|max_requests|monthly_limit|quota|calls_per_(month|period|cycle)|total_calls|allowance|cap|allocation|max)($|_)/i;
  const RE_PERIOD   = /^(period|billing_period|cycle|interval|window)$/i;
  const RE_RESETS   = /^(reset_at|resets_at|renewal_date|next_reset|period_end|expires_at|valid_until|renews_at|end_date)$/i;

  // Sometimes used > limit because the parser grabbed the wrong field
  // (e.g. plan price). Sanity gate: limit must be ≥ used and ≤ a sane cap.
  const usedRaw  = num(findByRe(resp, RE_USED));
  let   limitRaw = num(findByRe(resp, RE_LIMIT));
  if (limitRaw != null && limitRaw < 0)            limitRaw = null;
  if (limitRaw != null && usedRaw != null && limitRaw < usedRaw && limitRaw < 5) {
    // looks like we grabbed a price ($30) instead of the quota — drop it
    limitRaw = null;
  }

  return {
    plan:     findByRe(resp, RE_PLAN),
    status:   findByRe(resp, RE_STATUS),
    used:     usedRaw,
    limit:    limitRaw,
    period:   findByRe(resp, RE_PERIOD),
    resetsAt: findByRe(resp, RE_RESETS),
  };
}

// ==============================================
// COMPOSITE SCORE (v4.7.3 — single number per candidate, 0–100)
// ==============================================
// Aggregates every per-card signal into ONE number so the optimizer can rank
// candidates the way an institutional trader would, instead of dumping the
// integration on the user. Centered at 50 (neutral); ≥60 means tradeable;
// <50 means wait (or skip).
//
// Weights chosen to match standard options-trading practice:
//   - EV is king (it includes wall penalty already via ev_adjusted)
//   - Risk-of-ruin structures (broken / capital_trap) are HARD penalties
//   - Liquidity affects execution reality (slippage erases edge)
//   - Limit-target feasibility lets a negative-EV-now setup still score for
//     "wait for limit to fill" patience plays
//   - POP is a small tiebreaker (psychological execution preference)
//
// Tune the numeric constants in COMPOSITE_WEIGHTS to shift priorities; the
// formula stays the same.

const COMPOSITE_WEIGHTS = {
  CENTER:           50,    // neutral starting point
  EV_MAX:           40,    // cap on EV contribution
  EV_MULT:          2,     // ev_adjusted × this = ev contribution (clipped)
  BADGE_HEALTHY:    15,
  BADGE_NEUTRAL:    -5,    // thin / directional
  BADGE_DISQUAL:    -30,   // broken / capital_trap
  LIQ_HIGH:         10,
  LIQ_MID:          0,
  LIQ_LOW:          -10,
  LIMIT_NEG_EV_MAX: 30,    // weight of limit feasibility when EV is negative
  LIMIT_POS_EV_MAX: 10,    // weight when EV is already positive
  POP_TIEBREAK_DIV: 10,    // (pop - 50) / this = pop contribution
};

const TRADEABLE_THRESHOLD = 60;   // ≥ this = "engine picks this card" colored green
const SKIP_THRESHOLD       = 40;  // < this = clearly don't trade

function _compositeScore(c) {
  const W = COMPOSITE_WEIGHTS;
  const ev = c.ev_adjusted ?? c.ev ?? 0;

  // EV component (clipped 0..EV_MAX; negative EV contributes 0)
  const evScore = Math.max(0, Math.min(W.EV_MAX, ev * W.EV_MULT));

  // Badge component
  const badgeScore = ({
    healthy:      W.BADGE_HEALTHY,
    thin:         W.BADGE_NEUTRAL,
    directional:  W.BADGE_NEUTRAL,
    broken:       W.BADGE_DISQUAL,
    capital_trap: W.BADGE_DISQUAL,
  })[c.health] ?? 0;

  // Liquidity component
  const liqScore = ({ high: W.LIQ_HIGH, mid: W.LIQ_MID, low: W.LIQ_LOW })[c.liquidity] ?? 0;

  // Limit-target feasibility
  let limitScore = 0;
  if (c.limit_premiums?.feasible) {
    const lp = c.limit_premiums;
    const feasibility = Math.max(0, Math.min(1, 1 - Math.abs(lp.delta || 0) / Math.max(0.01, lp.current || 1)));
    limitScore = feasibility * (ev < 0 ? W.LIMIT_NEG_EV_MAX : W.LIMIT_POS_EV_MAX);
  }

  // POP small tiebreaker
  const popScore = ((c.pop_pct ?? 50) - 50) / W.POP_TIEBREAK_DIV;

  const total = W.CENTER + evScore + badgeScore + liqScore + limitScore + popScore;
  return Math.max(0, Math.min(100, Math.round(total * 10) / 10));
}

function _compositeVerdict(score, ev) {
  // Tradeable at score ≥60, but DISTINGUISH whether you'd fill at market or
  // need to wait on a GTC limit:
  //   ev ≥ 0  → 'tradeable now'      — current premium is already an edge
  //   ev < 0  → 'tradeable on limit' — score qualified via limit-target feasibility
  if (score >= TRADEABLE_THRESHOLD) {
    return ev >= 0
      ? { label: 'tradeable now',       mode: 'market', color: 'emerald' }
      : { label: 'tradeable on limit',  mode: 'limit',  color: 'emerald' };
  }
  if (score >= SKIP_THRESHOLD)  return { label: 'marginal',     mode: 'wait', color: 'amber' };
  return                               { label: 'do not trade', mode: 'skip', color: 'rose'  };
}


// ==============================================
// COMPOSITE BREAKDOWN (v4.7.7) — for the tooltip
// ==============================================
function _breakdownComposite(c) {
  const W = COMPOSITE_WEIGHTS;
  const ev = c.ev_adjusted ?? c.ev ?? 0;
  const evScore = Math.max(0, Math.min(W.EV_MAX, ev * W.EV_MULT));
  const badgeScore = ({
    healthy: W.BADGE_HEALTHY, thin: W.BADGE_NEUTRAL, directional: W.BADGE_NEUTRAL,
    broken: W.BADGE_DISQUAL,  capital_trap: W.BADGE_DISQUAL,
  })[c.health] ?? 0;
  const liqScore = ({ high: W.LIQ_HIGH, mid: W.LIQ_MID, low: W.LIQ_LOW })[c.liquidity] ?? 0;
  let limitScore = 0, feasibility = 0;
  if (c.limit_premiums?.feasible) {
    const lp = c.limit_premiums;
    feasibility = Math.max(0, Math.min(1, 1 - Math.abs(lp.delta || 0) / Math.max(0.01, lp.current || 1)));
    limitScore = feasibility * (ev < 0 ? W.LIMIT_NEG_EV_MAX : W.LIMIT_POS_EV_MAX);
  }
  const popScore = ((c.pop_pct ?? 50) - 50) / W.POP_TIEBREAK_DIV;
  return { center: W.CENTER, evScore, badgeScore, liqScore, limitScore, popScore, feasibility, total: c.composite_score };
}

// ==============================================
// TRADE TICKET BUILDER (v4.7.7) — Copy/Push buttons
// ==============================================
function _buildTradeTicket(c, symbol, expiration) {
  const ev = c.ev_adjusted ?? c.ev ?? 0;
  const lp = c.limit_premiums;
  const isCredit = c.type === 'credit';
  const verb = isCredit ? 'Sell' : 'Buy';
  const strategyName = STRATEGIES[c.label]?.name || c.label;
  const legs = c.structure_text || '?';
  let mode, warning = '';
  if (ev >= 0) {
    mode = 'MARKET';
  } else if (lp?.feasible) {
    mode = `LIMIT ${isCredit ? 'credit' : 'debit'} $${lp.target.toFixed(2)} GTC`;
  } else {
    const fallback = lp ? lp.breakeven : (isCredit ? c.max_profit : c.max_loss);
    mode = `LIMIT ${isCredit ? 'credit' : 'debit'} $${fallback.toFixed(2)} GTC`;
    warning = '  [WARN negative-EV setup; review before placing]';
  }
  return `${verb} ${strategyName} · ${(symbol || "").toUpperCase()} · ${expiration} · ${legs} · ${mode} · 1 contract${warning}`;
}

function pickBestCandidate(candidates) {
  if (!candidates.length) return null;
  // Pure composite-score ranking. The composite already encodes:
  //   - EV (wall-adjusted)
  //   - Health badge (broken/capital-trap heavily penalized)
  //   - Liquidity
  //   - Limit-target feasibility (helps borderline-negative EV setups)
  //   - POP tiebreaker
  // So no separate filter step needed — the score does the integration.
  const sorted = [...candidates].sort((a, b) => (b.composite_score ?? 0) - (a.composite_score ?? 0));
  return sorted[0]?.label;
}

// ==============================================
// COMPONENTS
// ==============================================
function Tooltip({ content, children, position = 'top' }) {
  return (
    <span className="relative inline-flex group">
      {children}
      <span className={`absolute z-[100] px-3 py-2 text-[11px] text-stone-200 bg-stone-900 border border-stone-700 rounded-lg shadow-2xl invisible opacity-0 group-hover:visible group-hover:opacity-100 transition-opacity duration-150 pointer-events-none w-64 leading-snug normal-case font-normal tracking-normal text-left ${
        position === 'top' ? 'bottom-full left-1/2 -translate-x-1/2 mb-2' : 'top-full left-1/2 -translate-x-1/2 mt-2'
      }`}>
        {content}
      </span>
    </span>
  );
}

function Stat({ label, value, sub }) {
  return (
    <div className="bg-[#13131c] border border-stone-800/60 rounded-lg px-4 py-3">
      <div className="text-[10px] font-num uppercase tracking-widest text-stone-400 mb-1">{label}</div>
      <div className="text-xl font-num font-bold text-stone-100">{value}</div>
      {sub && <div className="text-xs font-num text-stone-400 mt-0.5">{sub}</div>}
    </div>
  );
}

function Signal({ label, text }) {
  return (
    <div className="bg-black/30 border border-stone-800/40 rounded-lg p-3">
      <div className="text-[10px] font-num uppercase tracking-widest text-stone-400 mb-1">{label}</div>
      <div className="text-sm text-stone-200 leading-snug">{text}</div>
    </div>
  );
}

function Row({ label, value, positive, negative }) {
  const color = positive ? 'text-emerald-400' : negative ? 'text-rose-400' : 'text-stone-300';
  return (
    <div className="flex justify-between items-center">
      <span className="text-stone-500 text-xs uppercase tracking-wider">{label}</span>
      <span className={`font-bold ${color}`}>{value}</span>
    </div>
  );
}

function CardActions({ candidate, symbol, expiration, onPushBroker, hasActiveBroker }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      const ticket = _buildTradeTicket(candidate, symbol, expiration);
      await navigator.clipboard.writeText(ticket);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (e) { console.error('Copy failed:', e); }
  };
  const pushTooltip = hasActiveBroker
    ? 'Push order to active broker connection'
    : 'No active broker connection — add one under Settings to enable';
  return (
    <div className="mt-4 flex items-center gap-2 justify-end">
      <Tooltip content={copied ? 'Copied!' : 'Copy trade ticket to clipboard'}>
        <button onClick={handleCopy} aria-label="Copy trade ticket"
          className={`w-8 h-8 rounded-md border flex items-center justify-center transition-colors ${
            copied ? 'bg-emerald-200 border-emerald-700 text-emerald-900'
                   : 'bg-stone-200 border-stone-500 text-stone-700 hover:bg-stone-100'
          }`}>
          <span className="text-base leading-none">{copied ? '✓' : '⎘'}</span>
        </button>
      </Tooltip>
      <Tooltip content={pushTooltip}>
        <button onClick={() => onPushBroker && onPushBroker(candidate)}
          disabled={!onPushBroker}
          aria-label="Push to broker"
          className={`w-8 h-8 rounded-md border flex items-center justify-center transition-colors ${
            hasActiveBroker
              ? 'bg-emerald-200 border-emerald-700 text-emerald-900 hover:bg-emerald-100'
              : 'bg-stone-200/70 border-stone-500 text-stone-600 hover:bg-stone-100'
          }`}>
          <span className="text-base leading-none">↗</span>
        </button>
      </Tooltip>
    </div>
  );
}


// ==============================================
// AUTH MODULE (v4.7.30) — simulated for now
// ==============================================
// Session is stored in sessionStorage so it clears on tab close (per user
// preference). OAuth buttons are visually accurate but FAKE — they synthesize
// a user object client-side. Email/password creates a user with a hashed
// password (browser-only digest, NOT secure storage — this is a prototype).
// When a real backend exists, swap _AUTH_KEY persistence + the OAuth handlers
// for actual flows.

const _AUTH_KEY = 'edgelane_session_v1';
const _USERS_KEY = 'edgelane_users_v1';
const _BROKER_KEY = 'edgelane_broker_connections_v1';

// Tiny SHA-256 wrapper using subtle.crypto. NOT a substitute for bcrypt; this
// is a prototype-grade digest to keep passwords from being stored plaintext.
async function _hashPassword(plain) {
  const enc = new TextEncoder().encode(plain);
  const buf = await crypto.subtle.digest('SHA-256', enc);
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2,'0')).join('');
}

// Brand SVG marks from Simple Icons v11. Each path is single-color
// (fill=currentColor) and renders inside a 24x24 viewBox. Provider buttons
// use brand backgrounds with white/dark icons for proper contrast.
const SIMULATED_OAUTH_PROVIDERS = [
  { id: 'google',    name: 'Google',    color: '#fff',    text: '#3c4043', border: 'border-stone-400',
    path: 'M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48z' },
  { id: 'microsoft', name: 'Microsoft', color: '#fff',    text: '#5e5e5e', border: 'border-stone-400',
    path: 'M11.4 24H0V12.6h11.4V24zM24 24H12.6V12.6H24V24zM11.4 11.4H0V0h11.4v11.4zM24 11.4H12.6V0H24v11.4z' },
  { id: 'x',         name: 'X',         color: '#000',    text: '#fff',    border: 'border-stone-700',
    path: 'M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z' },
  { id: 'facebook',  name: 'Facebook',  color: '#1877f2', text: '#fff',    border: 'border-blue-700',
    path: 'M9.101 23.691v-7.98H6.627v-3.667h2.474v-1.58c0-4.085 1.848-5.978 5.858-5.978.401 0 .955.042 1.468.103a8.68 8.68 0 0 1 1.141.195v3.325a8.623 8.623 0 0 0-.653-.036 26.805 26.805 0 0 0-.733-.009c-.707 0-1.259.096-1.675.309a1.686 1.686 0 0 0-.679.622c-.258.42-.374.995-.374 1.752v1.297h3.919l-.386 2.103-.287 1.564h-3.246v8.245C19.396 23.238 24 18.179 24 12.044c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.628 3.874 10.35 9.101 11.647Z' },
  { id: 'instagram', name: 'Instagram', color: '#e1306c', text: '#fff',    border: 'border-pink-700',
    path: 'M12 0C8.74 0 8.333.015 7.053.072 5.775.132 4.905.333 4.14.63c-.789.306-1.459.717-2.126 1.384S.935 3.35.63 4.14C.333 4.905.131 5.775.072 7.053.012 8.333 0 8.74 0 12s.015 3.667.072 4.947c.06 1.277.261 2.148.558 2.913.306.788.717 1.459 1.384 2.126.667.666 1.336 1.079 2.126 1.384.766.296 1.636.499 2.913.558C8.333 23.988 8.74 24 12 24s3.667-.015 4.947-.072c1.277-.06 2.148-.262 2.913-.558.788-.306 1.459-.718 2.126-1.384.666-.667 1.079-1.335 1.384-2.126.296-.765.499-1.636.558-2.913.06-1.28.072-1.687.072-4.947s-.015-3.667-.072-4.947c-.06-1.277-.262-2.149-.558-2.913-.306-.789-.718-1.459-1.384-2.126C21.319 1.347 20.651.935 19.86.63c-.765-.297-1.636-.499-2.913-.558C15.667.012 15.26 0 12 0zm0 2.16c3.203 0 3.585.016 4.85.071 1.17.055 1.805.249 2.227.415.562.217.96.477 1.382.896.419.42.679.819.896 1.381.164.422.36 1.057.413 2.227.057 1.266.07 1.646.07 4.85s-.015 3.585-.074 4.85c-.061 1.17-.256 1.805-.421 2.227-.224.562-.479.96-.899 1.382-.419.419-.824.679-1.38.896-.42.164-1.065.36-2.235.413-1.274.057-1.649.07-4.859.07-3.211 0-3.586-.015-4.859-.074-1.171-.061-1.816-.256-2.236-.421-.569-.224-.96-.479-1.379-.899-.421-.419-.69-.824-.9-1.38-.165-.42-.359-1.065-.42-2.235-.045-1.26-.061-1.649-.061-4.844 0-3.196.016-3.586.061-4.861.061-1.17.255-1.814.42-2.234.21-.57.479-.96.9-1.381.419-.419.81-.689 1.379-.898.42-.166 1.051-.361 2.221-.421 1.275-.045 1.65-.06 4.859-.06zm0 3.678c-3.405 0-6.162 2.76-6.162 6.162 0 3.405 2.76 6.162 6.162 6.162 3.405 0 6.162-2.76 6.162-6.162 0-3.405-2.76-6.162-6.162-6.162zM12 16c-2.21 0-4-1.79-4-4s1.79-4 4-4 4 1.79 4 4-1.79 4-4 4zm7.846-10.405c0 .795-.646 1.44-1.44 1.44-.795 0-1.44-.646-1.44-1.44 0-.794.646-1.439 1.44-1.439.793-.001 1.44.645 1.44 1.439z' },
  { id: 'linkedin',  name: 'LinkedIn',  color: '#0a66c2', text: '#fff',    border: 'border-blue-800',
    path: 'M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.063 2.063 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z' },
  { id: 'reddit',    name: 'Reddit',    color: '#ff4500', text: '#fff',    border: 'border-orange-700',
    path: 'M12 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0zm5.01 4.744c.688 0 1.25.561 1.25 1.249a1.25 1.25 0 0 1-2.498.056l-2.597-.547-.8 3.747c1.824.07 3.48.632 4.674 1.488.308-.309.73-.491 1.207-.491.968 0 1.754.786 1.754 1.754 0 .716-.435 1.333-1.01 1.614a3.111 3.111 0 0 1 .042.52c0 2.694-3.13 4.87-7.004 4.87-3.874 0-7.004-2.176-7.004-4.87 0-.183.015-.366.043-.534A1.748 1.748 0 0 1 4.028 12c0-.968.786-1.754 1.754-1.754.464 0 .898.196 1.207.49 1.207-.883 2.878-1.43 4.744-1.487l.885-4.182a.342.342 0 0 1 .14-.197.35.35 0 0 1 .238-.042l2.906.617a1.214 1.214 0 0 1 1.108-.701zM9.25 12C8.561 12 8 12.562 8 13.25c0 .687.561 1.248 1.25 1.248.687 0 1.248-.561 1.248-1.249 0-.688-.561-1.249-1.249-1.249zm5.5 0c-.687 0-1.248.561-1.248 1.25 0 .687.561 1.248 1.249 1.248.687 0 1.249-.561 1.249-1.249 0-.687-.562-1.249-1.25-1.249zm-5.466 3.99a.327.327 0 0 0-.231.094.33.33 0 0 0 0 .463c.842.842 2.484.913 2.961.913.477 0 2.105-.056 2.961-.913a.361.361 0 0 0 .029-.463.33.33 0 0 0-.464 0c-.547.533-1.684.73-2.512.73-.828 0-1.979-.196-2.512-.73a.326.326 0 0 0-.232-.095z' },
];

function _readSession() {
  try {
    const raw = sessionStorage.getItem(_AUTH_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}
function _writeSession(session) {
  try {
    if (session) sessionStorage.setItem(_AUTH_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(_AUTH_KEY);
  } catch {}
}
function _readUsers() {
  try {
    const raw = sessionStorage.getItem(_USERS_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch { return {}; }
}
function _writeUsers(users) {
  try { sessionStorage.setItem(_USERS_KEY, JSON.stringify(users)); } catch {}
}

function useAuth() {
  const [session, setSession] = useState(() => _readSession());
  const signIn = useCallback((sessionObj) => {
    _writeSession(sessionObj);
    setSession(sessionObj);
  }, []);
  const signOut = useCallback(() => {
    _writeSession(null);
    setSession(null);
  }, []);
  const updateUser = useCallback((patch) => {
    setSession(prev => {
      if (!prev) return prev;
      const merged = { ...prev, user: { ...prev.user, ...patch } };
      _writeSession(merged);
      return merged;
    });
  }, []);
  return { session, signIn, signOut, updateUser };
}

// Simulated OAuth — just synthesizes a believable user and finishes.
async function _simulatedOAuthSignIn(providerId) {
  await new Promise(r => setTimeout(r, 600 + Math.random() * 400)); // pretend network
  const p = SIMULATED_OAUTH_PROVIDERS.find(x => x.id === providerId) || { name: providerId };
  const handle = `${providerId}_user_${Math.random().toString(36).slice(2, 8)}`;
  return {
    user: {
      id: `oauth_${providerId}_${Date.now()}`,
      email: `${handle}@${providerId}.example`,
      displayName: `${p.name} User`,
      authProvider: providerId,
      avatarLetter: p.name[0].toUpperCase(),
      createdAt: new Date().toISOString(),
    },
    issuedAt: new Date().toISOString(),
    note: 'Simulated OAuth — see operating manual.',
  };
}

async function _emailPasswordSignUp(email, password) {
  if (!email || !email.includes('@')) throw new Error('Valid email required.');
  if (!password || password.length < 6) throw new Error('Password must be at least 6 characters.');
  const users = _readUsers();
  if (users[email.toLowerCase()]) throw new Error('An account with this email already exists. Try signing in.');
  const hash = await _hashPassword(password);
  users[email.toLowerCase()] = {
    id: `email_${Date.now()}`,
    email: email.toLowerCase(),
    displayName: email.split('@')[0],
    passwordHash: hash,
    createdAt: new Date().toISOString(),
  };
  _writeUsers(users);
  return {
    user: {
      id: users[email.toLowerCase()].id,
      email: email.toLowerCase(),
      displayName: users[email.toLowerCase()].displayName,
      authProvider: 'email',
      avatarLetter: email[0].toUpperCase(),
      createdAt: users[email.toLowerCase()].createdAt,
    },
    issuedAt: new Date().toISOString(),
  };
}

async function _emailPasswordSignIn(email, password) {
  const users = _readUsers();
  const u = users[email.toLowerCase()];
  if (!u) throw new Error('No account with that email. Sign up first?');
  const hash = await _hashPassword(password);
  if (hash !== u.passwordHash) throw new Error('Incorrect password.');
  return {
    user: {
      id: u.id,
      email: u.email,
      displayName: u.displayName,
      authProvider: 'email',
      avatarLetter: u.email[0].toUpperCase(),
      createdAt: u.createdAt,
    },
    issuedAt: new Date().toISOString(),
  };
}

async function _emailPasswordReset(email, newPassword) {
  if (!newPassword || newPassword.length < 6) throw new Error('New password must be at least 6 characters.');
  const users = _readUsers();
  const u = users[email.toLowerCase()];
  if (!u) throw new Error('No account with that email.');
  u.passwordHash = await _hashPassword(newPassword);
  _writeUsers(users);
  return true;
}


// v4.7.38: returning-user breadcrumb. ONLY stores email + displayName so the
// next session can personalize the greeting and pre-fill the email field.
// Lives in localStorage (not sessionStorage) so it survives tab close. No
// password, no token, no session data — those still live in sessionStorage.
const _LAST_USER_KEY = 'edgelane_last_user_v1';
function _readLastUser() {
  try { const raw = localStorage.getItem(_LAST_USER_KEY); return raw ? JSON.parse(raw) : null; } catch { return null; }
}
function _writeLastUser(user) {
  try {
    if (user && user.email) {
      localStorage.setItem(_LAST_USER_KEY, JSON.stringify({
        email: user.email,
        displayName: user.displayName || (user.email.split('@')[0]),
      }));
    }
  } catch {}
}

// v4.7.38: Local time of day → friendly greeting
function _greetingForLocalTime(date) {
  const h = (date || new Date()).getHours();
  if (h < 5)  return 'Welcome back';      // late-night / red-eye
  if (h < 12) return 'Good morning';
  if (h < 17) return 'Good afternoon';
  if (h < 22) return 'Good evening';
  return 'Good night';
}

function AuthDialog({ onSignedIn }) {
  const [mode, setMode] = useState('signin'); // signin | signup | forgot
  // v4.7.38: pre-fill email from the returning-user breadcrumb
  const [lastUser] = useState(() => _readLastUser());
  const [email, setEmail] = useState(() => lastUser?.email || '');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [forgotStage, setForgotStage] = useState('email'); // email | newpwd | done

  // v4.7.38: subtitle changes by mode. For sign-in, personalize with the
  // returning user's first name when known.
  const subtitle = (() => {
    if (mode === 'signup') return 'Create your account';
    if (mode === 'forgot') return 'Reset password';
    const greet = _greetingForLocalTime();
    const firstname = (lastUser?.displayName || '').split(/[\s@]/)[0];
    if (firstname) return `${greet}, ${firstname}`;
    return greet;
  })();

  const handleOAuth = async (pid) => {
    setBusy(true); setError(null);
    try {
      const session = await _simulatedOAuthSignIn(pid);
      _writeLastUser(session.user);
      onSignedIn(session);
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  };

  const handleEmailSignIn = async (e) => {
    e?.preventDefault?.();
    setBusy(true); setError(null);
    try {
      const session = await _emailPasswordSignIn(email, password);
      _writeLastUser(session.user);
      onSignedIn(session);
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  const handleEmailSignUp = async (e) => {
    e?.preventDefault?.();
    if (password !== confirm) { setError('Passwords do not match.'); return; }
    setBusy(true); setError(null);
    try {
      const session = await _emailPasswordSignUp(email, password);
      _writeLastUser(session.user);
      onSignedIn(session);
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  const handleForgot = async (e) => {
    e?.preventDefault?.();
    setBusy(true); setError(null);
    try {
      if (forgotStage === 'email') {
        const users = _readUsers();
        if (!users[email.toLowerCase()]) throw new Error('No account with that email.');
        setForgotStage('newpwd');
      } else if (forgotStage === 'newpwd') {
        if (password !== confirm) throw new Error('Passwords do not match.');
        await _emailPasswordReset(email, password);
        setForgotStage('done');
        setTimeout(() => { setMode('signin'); setForgotStage('email'); setPassword(''); setConfirm(''); setError(null); }, 1400);
      }
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm p-4">
      <div className="w-full max-w-md bg-[#13131c] border border-stone-800/80 rounded-2xl p-6 shadow-2xl">
        <div className="text-center mb-5">
          <div className="inline-block text-emerald-400 text-[20px] font-extrabold tracking-[0.22em]"
               style={{ textShadow: '0 1px 0 rgba(167,243,208,0.35), 0 2px 2px rgba(0,0,0,0.6)' }}>
            EDGELANE
          </div>
          <div className="text-[11px] text-stone-300 font-num tracking-widest uppercase mt-2">
            {subtitle}
          </div>
          {mode === 'signin' && lastUser?.email && (
            <div className="text-[10px] text-stone-500 mt-1">welcome back</div>
          )}
        </div>

        {mode !== 'forgot' && (
          <>
            <div className="flex flex-wrap justify-center gap-2 mb-4">
              {SIMULATED_OAUTH_PROVIDERS.map(p => (
                <Tooltip key={p.id} content={`Sign in with ${p.name} (simulated)`}>
                  <button
                    onClick={() => handleOAuth(p.id)} disabled={busy}
                    className={`w-12 h-12 rounded-md border ${p.border} flex items-center justify-center transition-opacity hover:opacity-80 disabled:opacity-50 disabled:cursor-wait`}
                    style={{ background: p.color, color: p.text }}
                    aria-label={`Sign in with ${p.name}`}>
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                      <path d={p.path} />
                    </svg>
                  </button>
                </Tooltip>
              ))}
            </div>
            <div className="flex items-center gap-2 my-4">
              <div className="flex-1 h-px bg-stone-800"></div>
              <span className="text-[10px] text-stone-500 font-num uppercase tracking-widest">or with email</span>
              <div className="flex-1 h-px bg-stone-800"></div>
            </div>
          </>
        )}

        {mode === 'signin' && (
          <form onSubmit={handleEmailSignIn} className="space-y-3">
            <input type="email" required value={email} onChange={e => setEmail(e.target.value)}
              placeholder="email@example.com"
              className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 focus:outline-none focus:border-emerald-500" />
            <input type="password" required value={password} onChange={e => setPassword(e.target.value)}
              placeholder="password"
              className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 focus:outline-none focus:border-emerald-500" />
            <button type="submit" disabled={busy}
              className="w-full bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-stone-50 font-num font-bold uppercase tracking-widest text-xs py-2.5 rounded-md transition-colors">
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        )}

        {mode === 'signup' && (
          <form onSubmit={handleEmailSignUp} className="space-y-3">
            <input type="email" required value={email} onChange={e => setEmail(e.target.value)}
              placeholder="email@example.com"
              className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 focus:outline-none focus:border-emerald-500" />
            <input type="password" required value={password} onChange={e => setPassword(e.target.value)}
              placeholder="password (≥6 chars)"
              className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 focus:outline-none focus:border-emerald-500" />
            <input type="password" required value={confirm} onChange={e => setConfirm(e.target.value)}
              placeholder="confirm password"
              className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 focus:outline-none focus:border-emerald-500" />
            <button type="submit" disabled={busy}
              className="w-full bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-stone-50 font-num font-bold uppercase tracking-widest text-xs py-2.5 rounded-md transition-colors">
              {busy ? 'Creating account…' : 'Create account'}
            </button>
          </form>
        )}

        {mode === 'forgot' && (
          <form onSubmit={handleForgot} className="space-y-3">
            {forgotStage === 'email' && (
              <>
                <p className="text-[12px] text-stone-400 leading-relaxed">Enter the email associated with your account. We\'ll let you set a new password on the next step.</p>
                <input type="email" required value={email} onChange={e => setEmail(e.target.value)}
                  placeholder="email@example.com"
                  className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 focus:outline-none focus:border-emerald-500" />
                <button type="submit" disabled={busy}
                  className="w-full bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-stone-50 font-num font-bold uppercase tracking-widest text-xs py-2.5 rounded-md transition-colors">
                  {busy ? 'Checking…' : 'Continue'}
                </button>
              </>
            )}
            {forgotStage === 'newpwd' && (
              <>
                <p className="text-[12px] text-stone-400">Choose a new password for <span className="text-stone-200">{email}</span>.</p>
                <input type="password" required value={password} onChange={e => setPassword(e.target.value)}
                  placeholder="new password (≥6 chars)"
                  className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 focus:outline-none focus:border-emerald-500" />
                <input type="password" required value={confirm} onChange={e => setConfirm(e.target.value)}
                  placeholder="confirm new password"
                  className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 focus:outline-none focus:border-emerald-500" />
                <button type="submit" disabled={busy}
                  className="w-full bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-stone-50 font-num font-bold uppercase tracking-widest text-xs py-2.5 rounded-md transition-colors">
                  {busy ? 'Updating…' : 'Set new password'}
                </button>
              </>
            )}
            {forgotStage === 'done' && (
              <div className="text-center text-emerald-300 text-sm py-4">
                ✓ Password updated. Returning to sign in…
              </div>
            )}
          </form>
        )}

        {error && (
          <div className="mt-3 p-2 bg-rose-950/40 border border-rose-900/50 rounded text-[12px] text-rose-300">{error}</div>
        )}

        <div className="mt-5 text-center text-[12px] text-stone-500">
          {mode === 'signin' && (
            <>
              <button onClick={() => { setMode('forgot'); setError(null); setPassword(''); }} className="text-stone-400 hover:text-emerald-400 underline">Forgot password?</button>
              <span className="mx-3 text-stone-700">·</span>
              <button onClick={() => { setMode('signup'); setError(null); setPassword(''); setConfirm(''); }} className="text-stone-400 hover:text-emerald-400 underline">Create account</button>
            </>
          )}
          {mode === 'signup' && (
            <button onClick={() => { setMode('signin'); setError(null); setPassword(''); setConfirm(''); }} className="text-stone-400 hover:text-emerald-400 underline">Have an account? Sign in</button>
          )}
          {mode === 'forgot' && (
            <button onClick={() => { setMode('signin'); setForgotStage('email'); setError(null); setPassword(''); setConfirm(''); }} className="text-stone-400 hover:text-emerald-400 underline">Back to sign in</button>
          )}
        </div>

        <div className="mt-4 pt-3 border-t border-stone-800/60 text-[10px] text-stone-600 text-center leading-relaxed">
          OAuth flows are simulated for this build · Session stored in browser memory only · Clears on tab close
        </div>
      </div>
    </div>
  );
}

function ProfileMenu({ session, onSignOut, onOpenSettings }) {
  const [open, setOpen] = useState(false);
  if (!session?.user) return null;
  const u = session.user;
  return (
    <div className="relative">
      <button onClick={() => setOpen(o => !o)} aria-label="Profile menu"
        className="w-10 h-10 rounded-full bg-emerald-600 hover:bg-emerald-500 text-stone-50 font-bold flex items-center justify-center transition-colors border-2 border-emerald-400/40">
        {u.avatarLetter || u.email?.[0]?.toUpperCase() || '?'}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-12 z-50 w-64 bg-[#13131c] border border-stone-800/80 rounded-lg shadow-2xl py-1">
            <div className="px-4 py-3 border-b border-stone-800/60">
              <div className="text-sm font-bold text-stone-100">{u.displayName || u.email}</div>
              <div className="text-[11px] text-stone-400 font-num truncate">{u.email}</div>
              <div className="text-[9px] text-stone-500 font-num uppercase tracking-widest mt-1">
                {u.authProvider === 'email' ? 'email · password' : `via ${u.authProvider}`}
              </div>
            </div>
            <button onClick={() => { setOpen(false); onOpenSettings(); }}
              className="w-full text-left px-4 py-2 text-sm text-stone-200 hover:bg-stone-800/50 flex items-center gap-2">
              <span className="text-stone-500">⚙</span> Settings
            </button>
            <button onClick={() => { setOpen(false); onSignOut(); }}
              className="w-full text-left px-4 py-2 text-sm text-rose-300 hover:bg-stone-800/50 flex items-center gap-2">
              <span className="text-rose-500">↪</span> Sign out
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// ==============================================
// BROKER CONNECTIONS MODULE (v4.7.30)
// ==============================================
// Per-user, scoped by sessionStorage key. The active connection drives all
// push-to-broker order routing. Market-data connections are separate and
// unaffected. Multiple connections can be created; only one is active.

function _readBrokerState(userId) {
  try {
    const raw = sessionStorage.getItem(`${_BROKER_KEY}_${userId}`);
    return raw ? JSON.parse(raw) : { connections: [], activeId: null };
  } catch { return { connections: [], activeId: null }; }
}
function _writeBrokerState(userId, state) {
  try { sessionStorage.setItem(`${_BROKER_KEY}_${userId}`, JSON.stringify(state)); } catch {}
}

function useBrokerConnections(userId) {
  const [state, setState] = useState(() => userId ? _readBrokerState(userId) : { connections: [], activeId: null });
  useEffect(() => {
    if (userId) setState(_readBrokerState(userId));
  }, [userId]);

  const persist = useCallback((next) => {
    setState(next);
    if (userId) _writeBrokerState(userId, next);
  }, [userId]);

  const addConnection = useCallback((conn) => {
    const id = `conn_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
    const enriched = { ...conn, id, createdAt: new Date().toISOString(), lastTested: null, healthy: null };
    persist({
      connections: [...state.connections, enriched],
      activeId: state.activeId || id,
    });
    return enriched;
  }, [state, persist]);

  const updateConnection = useCallback((id, patch) => {
    persist({
      connections: state.connections.map(c => c.id === id ? { ...c, ...patch } : c),
      activeId: state.activeId,
    });
  }, [state, persist]);

  const removeConnection = useCallback((id) => {
    const next = {
      connections: state.connections.filter(c => c.id !== id),
      activeId: state.activeId === id ? null : state.activeId,
    };
    if (!next.activeId && next.connections.length > 0) next.activeId = next.connections[0].id;
    persist(next);
  }, [state, persist]);

  const setActive = useCallback((id) => {
    persist({ connections: state.connections, activeId: id });
  }, [state, persist]);

  const active = state.connections.find(c => c.id === state.activeId) || null;
  return { connections: state.connections, activeId: state.activeId, active, addConnection, updateConnection, removeConnection, setActive };
}

function ConnectionForm({ initialProvider, initial, onSave, onCancel }) {
  const [provider, setProvider] = useState(initialProvider || initial?.provider || 'tradier');
  const def = BROKER_PROVIDERS[provider];
  const [label, setLabel] = useState(initial?.label || `${def.name} ${initial ? 'updated' : 'connection'}`);
  const [config, setConfig] = useState(() => {
    const init = {};
    for (const f of def.fields) {
      init[f.key] = initial?.config?.[f.key] ?? (f.default || '');
    }
    return init;
  });

  // Reset config when provider changes
  useEffect(() => {
    const d = BROKER_PROVIDERS[provider];
    setConfig(() => {
      const init = {};
      for (const f of d.fields) init[f.key] = f.default || '';
      return init;
    });
    setLabel(`${d.name} connection`);
  }, [provider]);

  const handleSubmit = (e) => {
    e?.preventDefault?.();
    for (const f of def.fields) {
      if (f.required && !config[f.key]) {
        alert(`${f.label} is required.`);
        return;
      }
    }
    onSave({ provider, label: label || def.name, config });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-3 bg-stone-900/40 border border-stone-800/60 rounded-lg p-4">
      {!initial && (
        <div>
          <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-1.5">Provider</label>
          <select value={provider} onChange={e => setProvider(e.target.value)}
            className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100">
            {Object.values(BROKER_PROVIDERS).map(p => (
              <option key={p.id} value={p.id}>{p.name}{p.status === 'stub' ? ' (stub)' : ''}</option>
            ))}
          </select>
          <div className="mt-1.5 text-[11px] text-stone-500 leading-snug">{def.description}</div>
        </div>
      )}
      <div>
        <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-1.5">Connection label</label>
        <input value={label} onChange={e => setLabel(e.target.value)}
          placeholder="e.g. Tradier sandbox · personal"
          className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100" />
      </div>
      {def.fields.map(f => (
        <div key={f.key}>
          <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-1.5">{f.label}{f.required && <span className="text-rose-400 ml-1">*</span>}</label>
          {f.type === 'select' ? (
            <select value={config[f.key] || ''} onChange={e => setConfig(c => ({ ...c, [f.key]: e.target.value }))}
              className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100">
              {f.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          ) : (
            <input
              type={f.type === 'password' ? 'password' : 'text'}
              value={config[f.key] || ''}
              onChange={e => setConfig(c => ({ ...c, [f.key]: e.target.value }))}
              className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 font-num"
              spellCheck={false}
              autoComplete="off"
            />
          )}
          {f.help && <div className="mt-1 text-[11px] text-stone-500 leading-snug">{f.help}</div>}
        </div>
      ))}
      <div className="flex justify-end gap-2 pt-2">
        <button type="button" onClick={onCancel}
          className="px-3 py-1.5 text-xs font-num uppercase tracking-widest text-stone-300 hover:text-stone-100 border border-stone-700 rounded">Cancel</button>
        <button type="submit"
          className="px-3 py-1.5 text-xs font-num uppercase tracking-widest text-emerald-50 bg-emerald-600 hover:bg-emerald-500 rounded">
          {initial ? 'Save changes' : 'Save & add'}
        </button>
      </div>
    </form>
  );
}

function ConnectionCard({ conn, isActive, onTest, onEdit, onRemove, onSetActive }) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState(null);
  const def = BROKER_PROVIDERS[conn.provider];
  const handleTest = async () => {
    setTesting(true); setResult(null);
    try {
      const r = await _testBrokerConnection(conn);
      setResult(r);
      onTest(conn.id, r);
    } finally { setTesting(false); }
  };
  const lastTestedDisplay = conn.lastTested ? new Date(conn.lastTested).toLocaleTimeString() : 'never';
  const healthDisplay = result || (conn.healthy != null ? { ok: conn.healthy, message: conn.lastTestMessage } : null);
  return (
    <div className={`bg-stone-900/40 border rounded-lg p-4 ${isActive ? 'border-emerald-700/70' : 'border-stone-800/60'}`}>
      <div className="flex items-start justify-between mb-2">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-bold text-stone-100">{conn.label}</span>
            {isActive && <span className="text-[9px] font-num uppercase tracking-widest bg-emerald-700 text-emerald-50 px-1.5 py-0.5 rounded">Active</span>}
            {def.status === 'stub' && <span className="text-[9px] font-num uppercase tracking-widest bg-amber-900 text-amber-100 px-1.5 py-0.5 rounded">Stub</span>}
          </div>
          <div className="text-[11px] text-stone-400 font-num mt-0.5">
            {def.name}{conn.config?.env ? ` · ${conn.config.env}` : ''}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1">
          {!isActive && (
            <button onClick={() => onSetActive(conn.id)} className="text-[10px] font-num uppercase tracking-widest text-emerald-400 hover:text-emerald-300">
              Set active
            </button>
          )}
        </div>
      </div>
      {healthDisplay && (
        <div className={`text-[11px] font-num leading-snug mt-1 mb-2 ${healthDisplay.ok ? 'text-emerald-300' : 'text-rose-300'}`}>
          {healthDisplay.ok ? '✓' : '✗'} {healthDisplay.message}
          {result && <span className="text-stone-500 ml-2">({result.latencyMs}ms)</span>}
          {!result && conn.lastTested && <span className="text-stone-500 ml-2">(last tested {lastTestedDisplay})</span>}
        </div>
      )}
      <div className="flex flex-wrap gap-2 mt-3">
        <button onClick={handleTest} disabled={testing}
          className="px-2.5 py-1 text-[10px] font-num uppercase tracking-widest text-emerald-200 bg-emerald-950/60 border border-emerald-800/60 hover:bg-emerald-900/60 rounded disabled:opacity-50">
          {testing ? 'Testing…' : 'Test connection'}
        </button>
        <button onClick={() => onEdit(conn)}
          className="px-2.5 py-1 text-[10px] font-num uppercase tracking-widest text-stone-300 bg-stone-800/40 border border-stone-700 hover:bg-stone-700/60 rounded">
          Edit
        </button>
        <button onClick={() => { if (confirm('Remove this connection?')) onRemove(conn.id); }}
          className="px-2.5 py-1 text-[10px] font-num uppercase tracking-widest text-rose-300 bg-rose-950/40 border border-rose-900/60 hover:bg-rose-900/40 rounded ml-auto">
          Remove
        </button>
      </div>
    </div>
  );
}

function SettingsModal({ session, broker, onClose }) {
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(null);

  const handleSave = (data) => {
    if (editing) {
      broker.updateConnection(editing.id, data);
      setEditing(null);
    } else {
      broker.addConnection(data);
      setAdding(false);
    }
  };

  const handleTest = (id, result) => {
    broker.updateConnection(id, {
      lastTested: new Date().toISOString(),
      healthy: result.ok,
      lastTestMessage: result.message,
      ...(result.ok && result.accountNumber ? { config: { ...broker.connections.find(c => c.id === id).config, account_number: result.accountNumber } } : {}),
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/70 backdrop-blur-sm p-4 overflow-y-auto">
      <div className="w-full max-w-2xl bg-[#13131c] border border-stone-800/80 rounded-2xl p-6 my-8 shadow-2xl">
        <div className="flex items-center justify-between mb-4">
          <div>
            <div className="text-[10px] font-num uppercase tracking-widest text-stone-500">User settings</div>
            <h2 className="text-xl font-display font-bold text-stone-100 mt-1">Broker connections</h2>
          </div>
          <button onClick={onClose} className="text-stone-400 hover:text-stone-200 text-2xl leading-none">×</button>
        </div>

        <p className="text-[12px] text-stone-400 mb-4 leading-relaxed">
          Add brokers here so the <span className="text-stone-200 font-bold">push-to-broker</span> button on each candidate card can place trades. Only one connection can be active at a time. Stored in browser session memory (clears when you close this tab).
        </p>

        <div className="space-y-3">
          {broker.connections.length === 0 && !adding && (
            <div className="text-center py-8 text-stone-500 text-sm border border-dashed border-stone-700 rounded-lg">
              No broker connections yet.
            </div>
          )}
          {broker.connections.map(c => (
            <ConnectionCard
              key={c.id}
              conn={c}
              isActive={c.id === broker.activeId}
              onTest={handleTest}
              onEdit={(conn) => { setAdding(false); setEditing(conn); }}
              onRemove={broker.removeConnection}
              onSetActive={broker.setActive}
            />
          ))}
        </div>

        {(adding || editing) && (
          <div className="mt-4">
            <div className="text-[10px] font-num uppercase tracking-widest text-stone-500 mb-2">{editing ? 'Edit connection' : 'New connection'}</div>
            <ConnectionForm
              initial={editing}
              initialProvider={editing?.provider}
              onSave={handleSave}
              onCancel={() => { setAdding(false); setEditing(null); }}
            />
          </div>
        )}

        {!adding && !editing && (
          <button onClick={() => setAdding(true)}
            className="mt-4 w-full px-3 py-2 text-xs font-num uppercase tracking-widest text-emerald-200 bg-emerald-950/40 border border-dashed border-emerald-800/60 hover:bg-emerald-900/30 rounded">
            + Add broker connection
          </button>
        )}

        <div className="mt-6 pt-4 border-t border-stone-800/60 text-[10px] text-stone-600 leading-relaxed">
          <span className="text-amber-400">⚠</span> Credentials are stored in browser session memory only. They\'re cleared when you close this tab. A future release will move them to an encrypted backend.
        </div>
      </div>
    </div>
  );
}

// ==============================================
// PUSH-TO-BROKER ORDER DIALOG (v4.7.30)
// ==============================================
// Opens when the user clicks the ↗ button on a candidate card. Pre-fills the
// order with the limit-order tier's "modest" price (matching _buildTradeTicket
// logic) and pre-selects market vs. limit based on whether the live market
// already meets fair value. User can override either, then execute.

function OrderDialog({ candidate, symbol, expiration, connection, onClose }) {
  // Determine the suggested mode + price from the candidate's own limit_premiums
  // structure (the same one driving the tier card on the candidate).
  const lp = candidate?.limit_premiums;
  const isCredit = candidate?.type === 'credit';
  const liveEdge = lp ? (isCredit ? (lp.current - lp.breakeven) : (lp.breakeven - lp.current)) : 0;
  const beatsMarket = liveEdge >= 0;
  const initialMode = beatsMarket ? 'market' : 'limit';
  // Default limit price = the most achievable tier price (modest) if reachable,
  // else breakeven price.
  const initialLimit = lp
    ? (lp.tiers?.find(t => t.feasible)?.target ?? lp.breakeven ?? lp.current ?? candidate.net_premium)
    : candidate.net_premium;

  const [mode, setMode] = useState(initialMode);
  const [limitPrice, setLimitPrice] = useState(Number(initialLimit).toFixed(2));
  const [duration, setDuration] = useState('day');
  const [health, setHealth] = useState({ checking: true, ok: null, message: 'Checking connection…' });
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  // Healthcheck on open
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!connection) {
        setHealth({ checking: false, ok: false, message: 'No active broker connection. Open Settings to add one.' });
        return;
      }
      const r = await _testBrokerConnection(connection);
      if (!cancelled) setHealth({ checking: false, ok: r.ok, message: r.message });
    })();
    return () => { cancelled = true; };
  }, [connection]);

  const handleExecute = async () => {
    if (!connection || !health.ok) return;
    setSubmitting(true); setError(null);
    try {
      const r = await _submitOrderToActiveBroker(connection, candidate, symbol, expiration, mode, mode === 'market' ? null : Number(limitPrice));
      setResult(r);
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  const legSummary = candidate?.legs?.map(l =>
    `${l.longShort > 0 ? 'BUY' : 'SELL'}${Math.abs(l.longShort) === 2 ? ' ×2' : ''} ${l.side.toUpperCase()} ${l.strike}`
  ).join(' · ') || '—';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4">
      <div className="w-full max-w-lg bg-[#13131c] border border-stone-800/80 rounded-2xl p-6 shadow-2xl">
        <div className="flex items-start justify-between mb-4">
          <div>
            <div className="text-[10px] font-num uppercase tracking-widest text-stone-500">Push to broker</div>
            <h2 className="text-lg font-display font-bold text-stone-100 mt-1">
              {symbol} · {candidate?.label || 'candidate'} · {candidate?.structure_text || ''}
            </h2>
            <div className="text-[11px] text-stone-400 font-num mt-1">{legSummary}</div>
          </div>
          <button onClick={onClose} className="text-stone-400 hover:text-stone-200 text-2xl leading-none">×</button>
        </div>

        {/* Connection status banner */}
        <div className={`mb-4 p-3 rounded-md border ${
          health.checking ? 'bg-stone-900/60 border-stone-800 text-stone-300' :
          health.ok ? 'bg-emerald-950/40 border-emerald-800/60 text-emerald-200' :
          'bg-rose-950/40 border-rose-900/60 text-rose-200'
        }`}>
          <div className="text-[11px] font-num uppercase tracking-widest font-bold">
            {health.checking ? '◌ Checking connection…' : health.ok ? '✓ Broker connection healthy' : '✗ Broker connection unhealthy'}
          </div>
          <div className="text-[12px] mt-1 leading-snug">{health.message}</div>
          {connection && <div className="text-[10px] text-stone-500 font-num mt-1">Connection: {connection.label}</div>}
        </div>

        {!result && (
          <>
            <div className="space-y-3 mb-4">
              <div>
                <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-1.5">Order type</label>
                <div className="flex gap-2">
                  <button type="button" onClick={() => setMode('market')}
                    className={`flex-1 py-2 text-xs font-num uppercase tracking-widest rounded border ${
                      mode === 'market' ? 'bg-emerald-700 border-emerald-500 text-emerald-50' : 'bg-stone-900 border-stone-700 text-stone-400 hover:text-stone-200'
                    }`}>Market</button>
                  <button type="button" onClick={() => setMode('limit')}
                    className={`flex-1 py-2 text-xs font-num uppercase tracking-widest rounded border ${
                      mode === 'limit' ? 'bg-emerald-700 border-emerald-500 text-emerald-50' : 'bg-stone-900 border-stone-700 text-stone-400 hover:text-stone-200'
                    }`}>Limit</button>
                </div>
                <div className="mt-1.5 text-[11px] text-stone-500 leading-snug">
                  {mode === 'market'
                    ? 'Crosses the spread immediately. Use when the live market already meets fair value.'
                    : `Sets a GTC ${candidate?.type === 'credit' ? 'sell' : 'buy'} limit at your target price. Patient fill — only triggers if the market comes to you.`}
                </div>
              </div>
              {mode === 'limit' && (
                <div>
                  <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-1.5">
                    Limit price ({candidate?.type === 'credit' ? 'sell at or above' : 'buy at or below'})
                  </label>
                  <div className="flex items-center gap-2">
                    <span className="text-stone-500 text-sm">$</span>
                    <input type="number" step="0.01" min="0" value={limitPrice}
                      onChange={e => setLimitPrice(e.target.value)}
                      className="flex-1 bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100 font-num" />
                  </div>
                  {lp && (
                    <div className="mt-1.5 text-[10px] text-stone-500 font-num leading-snug">
                      Suggested {initialMode === 'limit' ? 'modest tier' : 'live mid'}: ${Number(initialLimit).toFixed(2)}
                      <span className="mx-2">·</span>
                      breakeven: ${Number(lp.breakeven).toFixed(2)}
                      <span className="mx-2">·</span>
                      live: ${Number(lp.current).toFixed(2)}
                    </div>
                  )}
                </div>
              )}
              <div>
                <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-1.5">Time-in-force</label>
                <select value={duration} onChange={e => setDuration(e.target.value)}
                  className="w-full bg-stone-900 border border-stone-700 rounded-md px-3 py-2 text-sm text-stone-100">
                  <option value="day">Day (expires at session close)</option>
                  <option value="gtc">GTC (good til canceled)</option>
                </select>
              </div>
            </div>

            {error && (
              <div className="mb-3 p-3 rounded-md bg-rose-950/40 border border-rose-900/60 text-rose-300 text-[12px] leading-snug">
                {error}
              </div>
            )}

            <div className="flex justify-end gap-2">
              <button onClick={onClose}
                className="px-3 py-2 text-xs font-num uppercase tracking-widest text-stone-300 hover:text-stone-100 border border-stone-700 rounded">
                Cancel
              </button>
              <button onClick={handleExecute} disabled={submitting || !health.ok}
                className="px-4 py-2 text-xs font-num uppercase tracking-widest text-emerald-50 bg-emerald-600 hover:bg-emerald-500 rounded disabled:opacity-40 disabled:cursor-not-allowed">
                {submitting ? 'Submitting…' : `Execute ${mode}`}
              </button>
            </div>
          </>
        )}

        {result && (
          <div className="text-center py-6">
            <div className="text-emerald-400 text-3xl mb-2">✓</div>
            <div className="text-stone-100 font-bold mb-1">Order submitted</div>
            <div className="text-[12px] text-stone-400 font-num">
              Order ID: <span className="text-stone-200">{result.id}</span>
              <span className="mx-2 text-stone-600">·</span>
              status: <span className="text-stone-200">{result.status}</span>
            </div>
            <div className="text-[11px] text-stone-500 mt-2 leading-snug">
              Track in your broker terminal. Preview check passed before the live submit.
            </div>
            <button onClick={onClose}
              className="mt-4 px-4 py-2 text-xs font-num uppercase tracking-widest text-emerald-50 bg-emerald-600 hover:bg-emerald-500 rounded">
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// v4.7.29: helper for the optional 3-lens positioning strip. Returns either
// null (lenses agree → don't render) or a render-ready spec. Threshold is
// "walls disagree by more than 5 strikes" — i.e. the span between the min and
// max wall strike must exceed 5 of the same chain's strike granularity.
//   For SPY/QQQ on liquid expirations that's $5 actual.
//   For high-strike names ($300+) it's effectively $5 too.
// We compute strike granularity from the candidate's own legs to stay
// chain-aware (a $50-stock chain steps in $0.50 or $1; a $500-stock chain
// usually steps in $5).
function _buildLensesStrip(biasResult, candidate) {
  if (!biasResult) return null;
  const gex = biasResult.gex_wall_strike;
  const vex = biasResult.vex_wall_strike;
  const tex = biasResult.tex_wall_strike;
  if (gex == null || vex == null || tex == null) return null;
  // Strike granularity from candidate legs (median consecutive diff)
  const legs = (candidate?.legs || []).map(l => Number(l.strike)).filter(s => !isNaN(s));
  const sortedLegs = [...new Set(legs)].sort((a,b)=>a-b);
  let stepGuess = 1;
  if (sortedLegs.length >= 2) {
    const diffs = [];
    for (let i = 1; i < sortedLegs.length; i++) diffs.push(sortedLegs[i] - sortedLegs[i-1]);
    stepGuess = diffs.sort((a,b)=>a-b)[Math.floor(diffs.length/2)] || 1;
  }
  const strikes = [gex, vex, tex];
  const span = Math.max(...strikes) - Math.min(...strikes);
  // Threshold: divergence > 5 strike steps. Below this, hide the strip.
  if (span <= 5 * stepGuess) return null;
  // Label each lens with its position relative to the GEX anchor.
  const tag = (val) => {
    if (val == null || val === gex) return '';
    return val < gex ? ' (pull below)' : ' (pull above)';
  };
  return {
    gex, vex, tex,
    vexTag: tag(vex),
    texTag: tag(tex),
  };
}

function CandidateCard({ candidate, isBest, symbol, expiration, biasResult, contracts, onPushBroker, hasActiveBroker }) {
  const c = candidate;
  const isCredit = c.type === 'credit';
  const labelColor = c.label === 'Conservative' ? 'emerald' : c.label === 'Balanced' ? 'amber' : 'rose';
  const lenses = _buildLensesStrip(biasResult, c);
  const liqColor = c.liquidity === 'high' ? 'emerald' : c.liquidity === 'mid' ? 'amber' : 'rose';
  const health = HEALTH_BADGES[c.health] || HEALTH_BADGES.healthy;
  const isBroken = c.health === 'broken' || c.health === 'capital_trap';
  const borderClass = isBest
    ? 'border-emerald-500/60 glow-emerald'
    : isBroken ? 'border-rose-700/60 glow-rose-strong' : 'border-stone-800/60 hover:border-stone-700';

  return (
    <div className={`relative bg-[#13131c] border rounded-2xl p-6 transition-all ${borderClass}`}>
      {isBest && (
        <div className="absolute -top-3 left-6 px-3 py-1 bg-emerald-500 text-stone-900 text-[10px] font-num uppercase tracking-widest font-bold rounded-full">★ Engine Pick</div>
      )}
      {!isBest && isBroken && (
        <div className="absolute -top-3 left-6 px-3 py-1 bg-rose-600 text-stone-50 text-[10px] font-num uppercase tracking-widest font-bold rounded-full">⚠ Caution</div>
      )}

      <div className="flex items-center justify-between mb-4">
        <div className={`text-xs font-num uppercase tracking-widest font-bold ${
          labelColor === 'emerald' ? 'text-emerald-400' : labelColor === 'amber' ? 'text-amber-400' : 'text-rose-400'
        }`}>{c.label}</div>
        <div className={`text-[10px] font-num uppercase px-2 py-0.5 rounded ${
          liqColor === 'emerald' ? 'bg-emerald-950/60 text-emerald-400' :
          liqColor === 'amber' ? 'bg-amber-950/60 text-amber-400' : 'bg-rose-950/60 text-rose-400'
        }`}>{c.liquidity} liq</div>
      </div>

      <div className="font-num text-sm text-stone-300 font-bold mb-3 pb-3 border-b border-stone-800/60 leading-relaxed">
        {c.structure_text}
      </div>

      <div className={`mb-4 p-3 rounded-lg border ${
        health.color === 'emerald' ? 'bg-emerald-950/40 border-emerald-800/50' :
        health.color === 'amber' ? 'bg-amber-950/40 border-amber-800/50' : 'bg-rose-950/40 border-rose-800/50'
      }`}>
        <div className={`text-xs font-num uppercase tracking-widest font-bold mb-1 ${
          health.color === 'emerald' ? 'text-emerald-300' :
          health.color === 'amber' ? 'text-amber-300' : 'text-rose-300'
        }`}>{health.label}</div>
        <div className="text-[13px] text-stone-400 leading-snug">{c.health_explanation}</div>
      </div>

      <div className="grid grid-cols-2 gap-3 mb-4">
        <div>
          <div className="text-[10px] font-num uppercase tracking-widest text-stone-500 mb-1">{isCredit ? 'Credit' : 'Debit'}</div>
          <div className={`text-2xl font-num font-bold ${isCredit ? 'text-emerald-400' : 'text-rose-400'}`}>
            {isCredit ? '+' : '−'}${c.net_premium?.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-[10px] font-num uppercase tracking-widest text-stone-500 mb-1">POP</div>
          <div className="text-2xl font-num font-bold text-stone-100">{c.pop_pct?.toFixed(0)}%</div>
        </div>
      </div>

      {/* Composite score (v4.7+) with breakdown tooltip (v4.7.7) */}
      {c.composite_score != null && (() => {
        const v = c.composite_verdict;
        const cls = v.color === 'emerald' ? 'bg-emerald-100 border-emerald-700 text-emerald-900'
                  : v.color === 'amber'   ? 'bg-amber-100 border-amber-700 text-amber-900'
                                          : 'bg-rose-100 border-rose-700 text-rose-900';
        const bd = _breakdownComposite(c);
        const fmt = (n) => (n >= 0 ? '+' : '') + n.toFixed(1);
        const tip = (
          <div className="text-left text-[12px] leading-relaxed">
            <div className="font-bold uppercase tracking-wider mb-1">Composite breakdown</div>
            <table className="font-num">
              <tbody>
                <tr><td className="pr-3">Center</td><td className="text-right font-bold">{bd.center}</td></tr>
                <tr><td className="pr-3">EV (ev_adj {fmt(c.ev_adjusted ?? c.ev ?? 0)})</td><td className="text-right font-bold">{fmt(bd.evScore)}</td></tr>
                <tr><td className="pr-3">Badge ({c.health})</td><td className="text-right font-bold">{fmt(bd.badgeScore)}</td></tr>
                <tr><td className="pr-3">Liquidity ({c.liquidity})</td><td className="text-right font-bold">{fmt(bd.liqScore)}</td></tr>
                <tr><td className="pr-3">Limit feasibility ({(bd.feasibility * 100).toFixed(0)}%)</td><td className="text-right font-bold">{fmt(bd.limitScore)}</td></tr>
                <tr><td className="pr-3">POP tiebreak ({(c.pop_pct ?? 0).toFixed(0)}%)</td><td className="text-right font-bold">{fmt(bd.popScore)}</td></tr>
              </tbody>
              <tfoot>
                <tr><td className="pr-3 pt-1 border-t border-stone-400">Total</td><td className="text-right pt-1 font-bold border-t border-stone-400">{bd.total?.toFixed(1)}</td></tr>
              </tfoot>
            </table>
          </div>
        );
        return (
          <Tooltip content={tip}>
            <div className={`mb-4 p-3 rounded-lg border ${cls} flex items-center justify-between cursor-help`}>
              <div>
                <div className="text-[11px] font-num uppercase tracking-widest font-bold">Composite score</div>
                <div className="text-2xl font-num font-bold leading-none mt-1">{c.composite_score.toFixed(1)}<span className="text-sm opacity-60"> / 100</span></div>
              </div>
              <div className="text-right">
                <div className="text-[11px] font-num uppercase tracking-widest font-bold">Verdict</div>
                <div className="text-sm font-num font-bold uppercase tracking-wider mt-1">{v.label}</div>
              </div>
            </div>
          </Tooltip>
        );
      })()}

      <div className="space-y-1.5 text-sm font-num">
        <Row label="Max profit" value={`$${c.max_profit?.toFixed(2)}`} positive />
        <Row label="Max loss" value={`$${c.max_loss?.toFixed(2)}`} negative />
        <Row label="R/R" value={`1 : ${(c.max_loss / c.max_profit).toFixed(2)}`} />
        <Row label="EV" value={`${c.ev >= 0 ? '+' : ''}$${c.ev?.toFixed(2)}`} positive={c.ev >= 0} negative={c.ev < 0} />
        {c.wall_penalty?.factor != null && c.wall_penalty.factor !== 1.0 && (
          <Row
            label="EV (wall-adj)"
            value={`${c.ev_adjusted >= 0 ? '+' : ''}$${c.ev_adjusted?.toFixed(2)} · ×${c.wall_penalty.factor.toFixed(2)}`}
            positive={c.ev_adjusted >= 0}
            negative={c.ev_adjusted < 0}
          />
        )}
        <Row label="Net Δ" value={c.net_spread_delta?.toFixed(3)} />
        <Row label="Net θ/day" value={`$${c.net_theta_dollar?.toFixed(2)}`} positive={c.net_theta_dollar > 0} />
        <Row label="Width" value={`$${c.width?.toFixed(2)}`} />
        <Row label="Capital" value={`$${(c.capital_required * 100)?.toFixed(0)}`} />
        <Row label="Breakeven" value={Array.isArray(c.breakevens) ? c.breakevens.map(b => `$${b.toFixed(2)}`).join(' / ') : '—'} />
      </div>

      {/* Trade-action buttons (v4.7.7) */}
      <CardActions candidate={c} symbol={symbol} expiration={expiration} onPushBroker={onPushBroker} hasActiveBroker={hasActiveBroker} />

      {/* GEX wall verdict — only render when bias data provided a numeric wall */}
      {c.wall_penalty?.reason && (() => {
        const v = c.wall_penalty.verdict;
        const cls = v === 'good' ? 'bg-emerald-950/40 border-emerald-800/50 text-emerald-300'
          : v === 'warn' ? 'bg-amber-950/40 border-amber-800/50 text-amber-300'
          : v === 'bad' ? 'bg-rose-950/40 border-rose-800/50 text-rose-300'
          : 'bg-stone-900/40 border-stone-800/50 text-stone-300';
        const label = v === 'good' ? '◎ Wall · Aligned' : v === 'warn' ? '◎ Wall · Skewed' : v === 'bad' ? '◎ Wall · Adverse' : '◎ Wall';
        return (
          <div className={`mt-4 p-3 rounded-lg border ${cls}`}>
            <div className="text-xs font-num uppercase tracking-widest font-bold mb-1">{label}</div>
            <div className="text-[13px] text-stone-400 leading-snug">{c.wall_penalty.reason}</div>
          </div>
        );
      })()}

      {/* v4.7.29: 3-lens positioning strip — only when GEX/VEX/TEX walls
          diverge by more than 5 strikes. Hidden by default to avoid clutter. */}
      {lenses && (
        <Tooltip content={
          <div className="text-left text-[12px] leading-relaxed max-w-xs">
            <div className="font-bold uppercase tracking-wider mb-1">Lenses diverge</div>
            <div className="mb-1">GEX (structural magnet), VEX (vol-magnet), and TEX (decay-burn zone) wall strikes don\'t line up. Price has more than one gravity well — your spread is structured around the GEX wall only.</div>
            <div className="text-stone-300 mt-1">Either tighten the short strike toward the VEX/TEX side, cut size, or skip and wait for the lenses to re-converge.</div>
          </div>
        }>
          <div className="mt-3 px-3 py-2 border border-dashed border-amber-800/70 rounded-lg bg-amber-950/15 text-[11px] font-num text-amber-200 cursor-help leading-snug">
            <span className="text-amber-400 font-bold">⚠ lenses diverge</span>
            <span className="text-stone-500 mx-2">·</span>
            <span className="text-stone-300">GEX <span className="font-bold text-stone-100">${lenses.gex.toFixed(2)}</span></span>
            <span className="text-stone-500 mx-2">·</span>
            <span className="text-amber-200">VEX <span className="font-bold">${lenses.vex.toFixed(2)}</span><span className="text-amber-700">{lenses.vexTag}</span></span>
            <span className="text-stone-500 mx-2">·</span>
            <span className="text-amber-200">TEX <span className="font-bold">${lenses.tex.toFixed(2)}</span><span className="text-amber-700">{lenses.texTag}</span></span>
          </div>
        </Tooltip>
      )}

      {/* Limit-order targets — scenario-aware tier table (v4.7.17) */}
      {c.limit_premiums && (() => {
        const lp = c.limit_premiums;
        const isCredit = lp.side === 'credit';
        const fmt = (v) => `$${v.toFixed(2)}`;
        const signed = (v) => `${v >= 0 ? '+' : '−'}$${Math.abs(v).toFixed(2)}`;
        const verb = isCredit ? 'Sell @' : 'Buy @';
        const dir  = isCredit ? '≥' : '≤';

        // Live edge over fair value. Positive = market is giving us free edge.
        // CREDIT: edge = current − breakeven (collecting more than fair is good)
        // DEBIT : edge = breakeven − current (paying less than fair is good)
        const currentEV = isCredit ? (lp.current - lp.breakeven) : (lp.breakeven - lp.current);

        // Annotate each tier vs live: met (already satisfied at market),
        // reachable (set a limit at target to lock this in), infeasible (math
        // says it can't physically happen on this spread — hide).
        const annotated = lp.tiers.map(t => {
          if (currentEV >= t.targetEV) return { ...t, status: 'met' };
          if (t.feasible)              return { ...t, status: 'reachable' };
          return                              { ...t, status: 'infeasible' };
        });
        const visible = annotated.filter(t => t.status !== 'infeasible');
        const metTiers       = annotated.filter(t => t.status === 'met');
        const bestMet        = metTiers[metTiers.length - 1];   // tiers come modest→strong; last met = strongest met
        const reachableTiers = annotated.filter(t => t.status === 'reachable');
        const nextReachable  = reachableTiers[0];

        // Headline + container tone, four scenarios
        let headline, tone;
        if (bestMet && bestMet.name === 'strong') {
          headline = `◎ Already at ${signed(currentEV)} edge — market beats every tier. Take at market.`;
          tone = 'bg-emerald-950/40 border-emerald-800/60 text-emerald-200';
        } else if (bestMet) {
          headline = `◎ Already at ${signed(currentEV)} edge — beats ${bestMet.name.toUpperCase()}. Patient limit can unlock the next tier.`;
          tone = 'bg-emerald-950/25 border-emerald-900/50 text-emerald-200';
        } else if (currentEV > 0) {
          headline = `◎ Slight ${signed(currentEV)} edge — under the modest threshold. Limit at one of these locks meaningful edge.`;
          tone = 'bg-amber-950/25 border-amber-800/50 text-amber-200';
        } else {
          headline = `◎ Sub-fair by ${fmt(Math.abs(currentEV))} — limit-only setup. Need the market to come to you.`;
          tone = 'bg-stone-900/40 border-stone-800/50 text-stone-300';
        }

        return (
          <div className={`mt-3 p-3 rounded-lg border ${tone}`}>
            <div className="text-xs font-num uppercase tracking-widest font-bold mb-1">{headline}</div>
            <div className="text-[11px] font-num text-stone-400 mb-2">
              breakeven (EV=0): <span className="font-bold text-stone-300">{fmt(lp.breakeven)}</span>
              <span className="mx-2">·</span>
              live: <span className="font-bold text-stone-300">{fmt(lp.current)}</span>
              {bestMet && nextReachable && (
                <span className="ml-2 text-stone-500">→ next tier at {fmt(nextReachable.target)}</span>
              )}
            </div>
            <div className="grid grid-cols-1 gap-1 font-num text-[12px]">
              {visible.map(t => {
                const tierTone = t.status === 'met' ? 'text-emerald-300' : 'text-stone-300';
                const icon     = t.status === 'met' ? '✓' : '○';
                const right    = t.status === 'met'
                  ? `EV ${signed(currentEV)} actual · met at market`
                  : `EV +$${t.targetEV.toFixed(2)} · ${t.hint}`;
                return (
                  <div key={t.name} className={`flex items-center justify-between gap-3 ${tierTone}`}>
                    <span className="uppercase tracking-widest text-[10px] font-bold w-24">{icon} {t.name}</span>
                    <span className="text-stone-500 text-[10px] w-16">{(t.pctOfWidth * 100).toFixed(2)}% width</span>
                    <span>{verb} <span className="font-bold text-[13px]">{fmt(t.target)}</span> {dir}</span>
                    <span className="text-[10px] text-stone-500 italic flex-1 text-right">{right}</span>
                  </div>
                );
              })}
            </div>
            <div className="text-[11px] text-stone-500 leading-snug mt-2 italic">
              ✓ = live price already gives at least this edge — no waiting. ○ = set a limit at the target to lock this tier in. Tiers scale with spread width.
            </div>
          </div>
        );
      })()}

      <div className="mt-4 pt-4 border-t border-stone-800/60 text-xs text-stone-400 italic leading-relaxed">{c.rationale}</div>
    </div>
  );
}

// ==============================================
// MAIN
// ==============================================
export default function SpreadOptimizer() {
  // v4.7.30: auth + per-user broker connections + push-to-broker dialog state
  const auth = useAuth();
  const broker = useBrokerConnections(auth.session?.user?.id);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [orderDialog, setOrderDialog] = useState(null); // { candidate } | null
  // v4.7.36: detect sandbox env from window.TRADIER_BASE_URL (build script sets
  // this from DEVMODE). Used to brand the action buttons with a red (SANDBOX)
  // tag so users never accidentally fire a live order thinking they're paper.
  const isSandbox = (typeof window !== 'undefined') &&
    String(window.TRADIER_BASE_URL || '').toLowerCase().includes('sandbox');

  // v4.7.35/.37: chainStale is DERIVED from timestamps rather than imperative
  // state — eliminates a closure bug where detectBias\'s useCallback deps
  // didn\'t include chainCache, so the staleness check fired against a stale
  // closure copy. Now: if the last bias fetch is newer than the last chain
  // fetch AND a chain exists, the chain is stale. Refresh chain advances
  // lastChainFetch, which flips the derived flag back automatically.

  const [symbol, setSymbol] = useState('SPY');
  const [strategy, setStrategy] = useState(null);
  const [expiration, setExpiration] = useState('');
  const [targetDelta, setTargetDelta] = useState(20);
  const [widthPref, setWidthPref] = useState('balanced');

  // Caches
  const [biasCache, setBiasCache] = useState(null); // { key, data }
  const [chainCache, setChainCache] = useState(null); // { key, data: { spot, dte, expectedMove, atmIV, contracts } }

  const [biasLoading, setBiasLoading] = useState(false);
  const [chainLoading, setChainLoading] = useState(false);
  const [error, setError] = useState(null);
  const [lastBiasFetch, setLastBiasFetch] = useState(null);     // Date of last successful bias
  const [lastChainFetch, setLastChainFetch] = useState(null);    // Date of last successful chain
  const [quota, setQuota] = useState(null);                      // Parsed Atlas subscription quota
  const [quotaLoading, setQuotaLoading] = useState(false);
  // Live spot, updated on every quote/chain fetch. Decoupled from biasResult
  // (which only refreshes when detectBias completes end-to-end through Gemini)
  // so the displayed spot is always the freshest value we've seen, even if a
  // downstream step (LLM prose, etc.) errors out mid-reload.
  const [spotPrice, setSpotPrice] = useState(null);
  const [lastSpotFetch, setLastSpotFetch] = useState(null);
  // v4.7.31: single update path — guarantees timestamp AND value tick together
  const updateSpot = useCallback((next, source) => {
    const n = Number(next);
    if (!isFinite(n) || n <= 0) {
      console.warn('[spot] refused invalid update', { next, source });
      return;
    }
    setSpotPrice(n);
    setLastSpotFetch(new Date());
    if (typeof window !== 'undefined') console.info(`[spot] ${source} → $${n.toFixed(2)} @ ${new Date().toLocaleTimeString()}`);
  }, []);
  // Tab in the results area: 'tickets' (default — the three candidate cards)
  // or 'lookup' (BS projection grid across hypothetical spot × time). Phase 2
  // will let a Lookup selection rewire the ticket math; for now it's read-only.
  const [resultsTab, setResultsTab] = useState('tickets');
  // Bias narrative + signal panels collapse — initial state from config flag
  const [biasNarrativeOpen, setBiasNarrativeOpen] = useState(
    (typeof window !== 'undefined' && window.BIAS_NARRATIVE_OPEN_DEFAULT === true)
  );

  // Refresh quota from Atlas (manual button click on the pill, or page load).
  const refreshQuota = useCallback(async () => {
    setQuotaLoading(true);
    try {
      const resp = await dataProvider.subscriptionStatus();
      // tradier returns a pre-parsed shape (has scalar .limit/.used); atlas
      // returns a raw envelope. Skip the recursive walker when already parsed.
      const parsed = resp && resp.limit != null ? resp : _parseAtlasQuota(resp);
      setQuota({ parsed, fetchedAt: new Date() });
    } catch (e) {
      setQuota({ parsed: null, error: e.message, fetchedAt: new Date() });
    } finally {
      setQuotaLoading(false);
    }
  }, []);

  // Fetch on mount (so user sees current quota before doing anything).
  // Skipped entirely when DATA_PROVIDER=tradier — Tradier exposes per-minute
  // rate limits via X-Ratelimit-* response headers, not a monthly quota, so
  // the quota pill concept doesn't apply. UI hides the pill in that case too.
  useEffect(() => {
    const isTradier = typeof window !== 'undefined' && String(window.DATA_PROVIDER).toLowerCase() === 'tradier';
    if (isTradier) return;
    const hasKey = (typeof window !== 'undefined' && window.ATLAS_KEY) ||
                   (typeof localStorage !== 'undefined' && localStorage.getItem('ATLAS_KEY'));
    if (hasKey) refreshQuota();
  }, [refreshQuota]);
  const [logSteps, setLogSteps] = useState([]);

  const cacheKey = `${symbol}_${expiration}`;
  const biasResult = biasCache?.key === cacheKey ? biasCache.data : null;
  const chainData = chainCache?.key === cacheKey ? chainCache.data : null;
  // v4.7.37: derived chain-stale flag. True when:
  //   (a) we have BOTH timestamps (so at least one bias detect AND one chain
  //       fetch have completed in this session),
  //   (b) the bias is newer than the chain, AND
  //   (c) a chain currently exists (collapse only makes sense when there\'s
  //       something to collapse).
  // Refresh chain advances lastChainFetch → this flips back to false.
  const chainStale = !!(chainData && lastBiasFetch && lastChainFetch
    && lastBiasFetch.getTime() > lastChainFetch.getTime());

  // Set wait cursor on body when loading
  useEffect(() => {
    document.body.style.cursor = (biasLoading || chainLoading) ? 'wait' : 'auto';
    return () => { document.body.style.cursor = 'auto'; };
  }, [biasLoading, chainLoading]);

  // Auto re-rank when strategy/delta/width/wall changes (using cached chain)
  const candidates = useMemo(() => {
    if (!chainData || !strategy) return [];
    const td = targetDelta / 100;
    const wf = WIDTH_PREFS[widthPref].factor;
    // Pull dominant gamma wall from cached bias data. If the LLM didn't return
    // numeric wall data (older responses or low-confidence reads), wall penalty
    // is a no-op (factor 1.0) and behavior matches pre-feature ranking.
    const walls = biasResult?.gex_wall_strike != null
      ? { strike: biasResult.gex_wall_strike, strength: biasResult.gex_wall_strength || 'medium' }
      : null;
    return generateCandidates(strategy, chainData.contracts, chainData.dte, chainData.expectedMove, td, wf, walls);
  }, [chainData, strategy, targetDelta, widthPref, biasResult]);

  const bestPick = useMemo(() => pickBestCandidate(candidates), [candidates]);

  // Bias detection — v4.7 hybrid: deterministic JS engine + Gemini prose only.
  // 1) Parallel Atlas REST: spot + greek exposures
  // 2) JS rule table computes wall, score, confidence, bias_label, recommendations
  //    (every structured field that drives the optimizer is now reproducible)
  // 3) Gemini Flash writes a 1-paragraph summary + 4 short signal sentences
  //    given the JS-computed values. No more arithmetic via LLM.
  const detectBias = useCallback(async (force = false) => {
    if (!symbol || !expiration) { setError('Symbol and expiration required'); return; }
    if (!force && biasCache?.key === cacheKey) return;
    setBiasLoading(true); setError(null);
    setLogSteps(['Fetching provider data (Stock-Quote + Greek-Exposures, parallel)...']);

    const sym = symbol.toUpperCase();
    try {
      // STEP 1: REST fetch — quote in parallel with adaptive greek-exposures
      // (fast path for liquid symbols, chunked+cached for heavy chains).
      const [quote, greeksRaw] = await Promise.all([
        dataProvider.stockQuote(sym),
        dataProvider.greekExposures(sym, 3),
      ]);
      const spot = Number(quote.price);
      if (!spot) throw new Error('Could not determine spot price.');
      // Update the live spot price IMMEDIATELY — before Gemini/JS engine work.
      // This way, even if downstream steps fail, the visible spot reflects the
      // fresh quote we just pulled. v4.7.31: routed through updateSpot which
      // also bumps lastSpotFetch and logs to console for verifiability.
      updateSpot(spot, 'detectBias');

      // Find the right expiration in exposures_by_date (target if present, else closest)
      const ebd = greeksRaw?.exposures_by_date || {};
      const dates = Object.keys(ebd);
      let chosen = expiration;
      if (!ebd[chosen] && dates.length) {
        const target = +new Date(expiration);
        chosen = dates.reduce((best, d) =>
          Math.abs(+new Date(d) - target) < Math.abs(+new Date(best) - target) ? d : best, dates[0]);
      }
      const exposuresForChosen = ebd[chosen] || null;
      setLogSteps(s => [...s, `Spot ${spot.toFixed(2)}, exposures for ${chosen}. Running JS bias engine...`]);

      // STEP 2: deterministic JS bias engine
      const computed = _computeBiasSignals(sym, expiration, spot, greeksRaw, exposuresForChosen);
      setLogSteps(s => [...s, `Bias: ${computed.bias_label} (score ${computed.directional_score}, ${computed.confidence} conf). Asking Gemini for narrative...`]);

      // STEP 3: Gemini prose-only call. Pass JS-computed values as facts; the
      // model only rephrases them in plain English.
      const f = computed._facts;
      const prosePrompt = `Write a brief plain-English narrative explaining the dealer-positioning setup for ${sym} options expiring ${expiration}. The facts below are already computed — do NOT recompute, just rephrase clearly for a trader.

FACTS:
- Spot price: ${spot}
- Directional bias: ${computed.bias_label.replace('_',' ')} (score ${computed.directional_score} on -100..100 scale, confidence ${computed.confidence})
- Dominant gamma wall strike: ${computed.gex_wall_strike} (${computed.gex_wall_strength} strength)
- Spot is ${f.wallSide} the wall by ${computed.gex_wall_strike != null ? Math.abs(spot - computed.gex_wall_strike).toFixed(2) : 'N/A'}
- Net dealer gamma (chosen expiration): ${f.gammaRegime} (raw: ${f.netGex.toFixed(0)})
- Net dealer delta exposure: ${f.dexSkewSide} (raw: ${f.netDex.toFixed(0)})
- Recommended strategies (in order): ${computed.recommended_strategies.join(', ')}

OUTPUT JSON only:
{
  "summary": "<one paragraph, 3-4 sentences. Cover what the wall implies, the regime (pinning vs amplifying), and what kind of trade fits>",
  "signals": {
    "dex_skew":     "<one sentence about the DEX skew implication>",
    "gex_wall":     "<one sentence about the gamma wall and its support/resistance role>",
    "gamma_regime": "<one sentence about positive/negative gamma regime impact>",
    "spot_vs_wall": "<one sentence about where spot sits relative to wall>"
  }
}`;

      const prose = await _callGemini({
        prompt: prosePrompt,
        label: 'Bias prose',
        temperature: 0.2,
        responseSchema: _BIAS_PROSE_SCHEMA,
      });

      // STEP 4: stitch JS facts + Gemini prose into the bias result
      const finalBias = {
        spot: computed.spot,
        directional_score: computed.directional_score,
        bias_label: computed.bias_label,
        confidence: computed.confidence,
        gex_wall_strike: computed.gex_wall_strike,
        gex_wall_strength: computed.gex_wall_strength,
        // v4.7.29: VEX/TEX walls drive the optional 3-lens divergence strip on
        // candidate cards. Only render when all three are present AND they
        // disagree by more than 5 strikes (handled in CandidateCard).
        vex_wall_strike: computed.vex_wall_strike,
        vex_wall_value:  computed.vex_wall_value,
        tex_wall_strike: computed.tex_wall_strike,
        tex_wall_value:  computed.tex_wall_value,
        recommended_strategies: computed.recommended_strategies,
        signals: prose.signals,
        summary: prose.summary,
      };

      setBiasCache({ key: cacheKey, data: finalBias }); setLastBiasFetch(new Date());
      const rec = finalBias.recommended_strategies?.[0];
      if (rec && STRATEGIES[rec]) setStrategy(rec);
      else setStrategy(BIAS_TO_STRATEGY[finalBias.bias_label] || 'iron_condor');
      setLogSteps(s => [...s, 'Bias detected (JS engine + Gemini prose).']);
    } catch (e) { setError(e.message); }
    finally { setBiasLoading(false); }
  }, [symbol, expiration, cacheKey, biasCache]);

  // Chain fetch — pure Atlas REST, single call. No LLM, no MCP routing.
  // atlas.getOptionsChain pulls chain + spot in parallel, filters to target
  // expiration + ±30% strike band, normalises contract shape, computes ATM
  // IV / expected move / DTE. Optimize then runs purely client-side over
  // chainCache.contracts. Rate-limit errors surface cleanly from REST.
  const fetchChain = useCallback(async (force = false) => {
    if (!symbol || !expiration) return;
    if (!force && chainCache?.key === cacheKey) return;
    setChainLoading(true); setError(null);
    setLogSteps(s => [...s, 'Pulling chain via provider REST (one call, ±30% strike band)...']);

    try {
      const data = await dataProvider.optionsChain(symbol.toUpperCase(), expiration, { strikeBandPct: 30, maxExpirations: 12 });
      // Refresh the live spot from the chain's parallel quote — guarantees the
      // displayed spot tracks every chain refresh, independent of bias.
      if (data?.spot) updateSpot(data.spot, 'fetchChain');
      setChainCache({ key: cacheKey, data }); setLastChainFetch(new Date());
      // v4.7.37: chainStale is now derived from lastBiasFetch > lastChainFetch,
      // so advancing lastChainFetch above is all that's needed to re-expand.
      setLogSteps(s => [...s, `Chain cached: ${data.contracts?.length || 0} contracts (filtered), spot $${Number(data.spot).toFixed(2)}.`]);
    } catch (e) {
      setError(e.message);
    } finally {
      setChainLoading(false);
    }
  }, [symbol, expiration, cacheKey, chainCache]);

  const optimize = async () => {
    // v4.7.3: always force-refetch the chain. Bias is left alone (it changes
    // on a slower timescale than option premiums). v4.7.35: for a fresh bias
    // read, click "Re-detect bias" — the chain auto-collapses
    // after a 2nd+ detect so you naturally come back to this button.
    await fetchChain(true);
  };

  const biasColor = (label) => {
    if (!label) return 'stone';
    if (label.includes('bull')) return 'emerald';
    if (label.includes('bear')) return 'rose';
    return 'amber';
  };

  const deltaFloat = (targetDelta / 100).toFixed(2);
  const popEstimate = Math.round((1 - targetDelta / 100) * 100);
  const isLoading = biasLoading || chainLoading;

  // v4.7.30: gate the entire app on a valid session
  if (!auth.session) {
    return <AuthDialog onSignedIn={auth.signIn} />;
  }

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Manrope:wght@300;400;500;600;700;800&display=swap');
        .font-display { font-family: 'Manrope', system-ui, sans-serif; letter-spacing: -0.02em; }
        .font-num { font-family: 'JetBrains Mono', ui-monospace, monospace; font-variant-numeric: tabular-nums; }
        .grain::before { content: ''; position: absolute; inset: 0; background-image: radial-gradient(rgba(255,255,255,0.015) 1px, transparent 1px); background-size: 3px 3px; pointer-events: none; }
        .glow-emerald { box-shadow: 0 0 24px -4px rgba(16, 185, 129, 0.4); }
        .glow-rose { box-shadow: 0 0 24px -4px rgba(244, 63, 94, 0.4); }
        .glow-amber { box-shadow: 0 0 24px -4px rgba(245, 158, 11, 0.4); }
        .glow-rose-strong { box-shadow: 0 0 32px -4px rgba(244, 63, 94, 0.55); }
        @keyframes pulse-fade { 0%,100% { opacity: 0.4 } 50% { opacity: 1 } }
        .pulse-dot { animation: pulse-fade 1.4s ease-in-out infinite; }
        @keyframes shimmer { 0% { background-position: -200% 0 } 100% { background-position: 200% 0 } }
        .skeleton { background: linear-gradient(90deg, #1a1a24 0%, #25252f 50%, #1a1a24 100%); background-size: 200% 100%; animation: shimmer 1.6s ease-in-out infinite; }
        .gauge-fill { transition: width 0.8s cubic-bezier(0.16, 1, 0.3, 1); }
        @keyframes spin { to { transform: rotate(360deg); } }
        .spin { animation: spin 1s linear infinite; }
        body.loading { cursor: wait !important; }
      `}</style>

      <div className="min-h-screen bg-[#0a0a0f] text-stone-200 font-display relative grain p-4 sm:p-8">
        <div className="fixed inset-0 pointer-events-none opacity-30" aria-hidden>
          <div className="absolute -top-40 -left-40 w-[500px] h-[500px] rounded-full bg-emerald-900/20 blur-3xl" />
          <div className="absolute top-1/2 -right-40 w-[600px] h-[600px] rounded-full bg-violet-900/20 blur-3xl" />
        </div>

        <div className="relative max-w-6xl mx-auto">
          <header className="mb-8 pb-6 border-b border-stone-800/60 flex items-start justify-between flex-wrap gap-4">
            <div>
              <div className="font-num uppercase mb-2 flex items-baseline gap-2 flex-wrap">
                <span
                  className="text-[20px] font-extrabold tracking-[0.22em] text-emerald-400"
                  style={{ textShadow: '0 1px 0 rgba(167,243,208,0.35), 0 2px 2px rgba(0,0,0,0.6)' }}
                >EDGELANE</span>
                <span className="text-xs tracking-[0.3em] text-stone-500">· OPTIONS OPTIMIZATION LAB · v__EDGE_LANE_VERSION__</span>
              </div>
              <h1 className="text-4xl sm:text-5xl font-display font-extrabold text-stone-50">
                Spread <span className="italic text-emerald-400 font-light">Optimizer</span>
              </h1>
              <p className="mt-3 text-stone-400 max-w-2xl text-sm leading-relaxed">
                Cut the noise. Trade the decided multi-leg options edge.
              </p>
            </div>

            {/* v4.7.30: profile menu top-right */}
            <ProfileMenu
              session={auth.session}
              onSignOut={auth.signOut}
              onOpenSettings={() => setSettingsOpen(true)}
            />
          </header>

          {/* Cache status indicator + timestamps + Reload Data button (v4.7+) */}
          {(biasResult || chainData || quota) && (
            <div className="mb-4 flex items-center gap-3 text-[10px] font-num uppercase tracking-widest text-stone-500 flex-wrap">
              <span className={biasResult ? 'text-emerald-400' : 'text-stone-600'}>
                ● Bias {biasResult ? 'cached' : 'pending'}
                {lastBiasFetch && <span className="normal-case text-stone-400 ml-1" title={lastBiasFetch.toLocaleString()}>· {lastBiasFetch.toLocaleTimeString()}</span>}
              </span>
              <span className={chainData ? 'text-emerald-400' : 'text-stone-600'}>
                ● Chain {chainData ? `cached (${chainData.contracts?.length || 0} contracts)` : 'pending'}
                {lastChainFetch && <span className="normal-case text-stone-400 ml-1" title={lastChainFetch.toLocaleString()}>· {lastChainFetch.toLocaleTimeString()}</span>}
              </span>
              {/* Atlas quota pill (v4.7+) — hidden when provider is tradier
                  since Tradier uses per-minute rate-limit headers, not a
                  monthly quota. */}
              {(typeof window === 'undefined' || String(window.DATA_PROVIDER).toLowerCase() !== 'tradier') && quota && quota.parsed && quota.parsed.limit > 0 && (() => {
                const q = quota.parsed;
                const pct = (q.used / q.limit) * 100;
                const color = pct < 75 ? 'emerald' : pct < 95 ? 'amber' : 'rose';
                const cls = color === 'emerald' ? 'bg-emerald-100 border-emerald-700 text-emerald-900'
                          : color === 'amber'   ? 'bg-amber-100 border-amber-700 text-amber-900'
                                                : 'bg-rose-100 border-rose-700 text-rose-900';
                return (
                  <Tooltip content={`Plan: ${q.plan || 'unknown'}. ${q.used} of ${q.limit} calls used this period${q.resetsAt ? ` (resets ${q.resetsAt})` : ''}. 1 bias = 2 calls, 1 chain = 1 call.`}>
                    <button onClick={refreshQuota} disabled={quotaLoading}
                      className={`ml-auto border px-3 py-1.5 rounded-md text-[10px] font-num uppercase tracking-widest font-bold flex items-center gap-1.5 transition-colors ${cls} hover:opacity-80 disabled:opacity-50`}>
                      <span className={quotaLoading ? 'spin inline-block' : 'inline-block'}>◎</span>
                      <span>Quota: {Math.round(q.used)}/{Math.round(q.limit)} · {pct.toFixed(0)}%</span>
                    </button>
                  </Tooltip>
                );
              })()}
              {(typeof window === 'undefined' || String(window.DATA_PROVIDER).toLowerCase() !== 'tradier') && quota && (!quota.parsed || !(quota.parsed.limit > 0)) && (
                <Tooltip content={
                  quota.error
                    ? `Quota fetch failed: ${quota.error}`
                    : `Quota response missing 'limit' field. Provider returned 200 but the parser couldn't find usage/limit in the JSON. Open DevTools console — '[Provider quota] raw response' shows the actual shape.`
                }>
                  <button onClick={refreshQuota} className="ml-auto bg-stone-200 border border-stone-500 px-3 py-1.5 rounded-md text-[10px] font-num uppercase tracking-widest text-stone-700 flex items-center gap-1.5">
                    <span>◎</span><span>Quota: unavailable</span>
                  </button>
                </Tooltip>
              )}
            </div>
          )}

          {/* Inputs */}
          <section className="mb-6 p-6 sm:p-8 bg-[#13131c] border border-stone-800/60 rounded-2xl">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-5 mb-5">
              <div>
                <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-2">Symbol</label>
                <input type="text" value={symbol} onChange={(e) => { setSymbol(e.target.value.toUpperCase()); setBiasCache(null); setChainCache(null); }} placeholder="SPY"
                  className="w-full bg-black/40 border border-stone-700/60 rounded-lg px-4 py-3 text-lg font-num font-bold text-stone-100 focus:outline-none focus:border-emerald-500/60 transition-colors" />
              </div>
              <div>
                <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-2">Expiration</label>
                <input type="date" value={expiration} onChange={(e) => { setExpiration(e.target.value); setBiasCache(null); setChainCache(null); }}
                  className="w-full bg-black/40 border border-stone-700/60 rounded-lg px-4 py-3 text-base font-num text-stone-100 focus:outline-none focus:border-emerald-500/60 transition-colors" />
              </div>
              <div>
                <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-2">
                  Target Δ: <span className="text-emerald-400 font-bold">{deltaFloat}</span>
                  <span className="text-stone-600 ml-2">≈ {popEstimate}% POP</span>
                </label>
                <input type="range" min="5" max="40" value={targetDelta} onChange={(e) => setTargetDelta(parseInt(e.target.value))} className="w-full mt-3 accent-emerald-500" />
                <div className="text-[10px] font-num text-stone-500 mt-1 flex justify-between"><span>Δ 0.05</span><span>Δ 0.40</span></div>
              </div>
            </div>

            <div className="mb-5">
              <label className="block text-[10px] font-num uppercase tracking-widest text-stone-500 mb-3">Wing Width Preference</label>
              <div className="grid grid-cols-3 gap-2">
                {Object.entries(WIDTH_PREFS).map(([key, w]) => (
                  <button key={key} onClick={() => setWidthPref(key)}
                    className={`p-3 rounded-lg text-left transition-all border ${
                      widthPref === key ? 'bg-emerald-950/60 border-emerald-700/60 text-emerald-200'
                        : 'bg-black/30 border-stone-800/60 text-stone-400 hover:border-stone-700'
                    }`}>
                    <div className="font-num text-xs uppercase tracking-wider font-bold mb-1">{w.name}</div>
                    <div className="text-[11px] leading-snug">{w.desc}</div>
                  </button>
                ))}
              </div>
            </div>

            <button
              onClick={() => detectBias(biasResult != null)}
              disabled={biasLoading || !symbol || !expiration}
              title={biasResult ? 'Bias already cached for this symbol/expiration — click to refetch from provider' : 'Run bias engine + narrative'}
              className="w-full bg-emerald-500 hover:bg-emerald-400 disabled:bg-stone-700 disabled:text-stone-500 cursor-pointer disabled:cursor-not-allowed text-stone-900 font-bold py-4 rounded-lg uppercase tracking-widest text-sm transition-colors">
              {biasLoading
                ? '◌ Reading tape...'
                : (
                  <>
                    {biasResult ? '↻ Re-detect bias' : '⟶ Detect bias'}
                    {isSandbox && <span className="ml-2 text-rose-700">(SANDBOX)</span>}
                  </>
                )}
            </button>

            {logSteps.length > 0 && isLoading && (
              <div className="mt-4 space-y-1 font-num text-xs text-stone-500">
                {logSteps.map((s, i) => <div key={i} className="flex items-center gap-2"><span className="pulse-dot text-emerald-400">●</span><span>{s}</span></div>)}
              </div>
            )}

            {error && <div className="mt-4 p-4 bg-rose-950/40 border border-rose-800/50 rounded-lg text-rose-300 text-sm font-num">{error}</div>}
          </section>

          {/* BIAS RESULT */}
          {biasLoading && !biasResult && <div className="mb-6 p-6 bg-[#13131c] border border-stone-800/60 rounded-2xl skeleton h-48" />}

          {biasResult && (
            <section className={`mb-6 p-6 sm:p-8 bg-[#13131c] border rounded-2xl ${
              biasColor(biasResult.bias_label) === 'emerald' ? 'border-emerald-800/50 glow-emerald' :
              biasColor(biasResult.bias_label) === 'rose' ? 'border-rose-800/50 glow-rose' : 'border-amber-800/50 glow-amber'
            }`}>
              <div className="flex items-center justify-between mb-5 flex-wrap gap-3">
                <div>
                  <div className="text-[10px] font-num uppercase tracking-widest text-stone-400 mb-1">Tape Bias · Confidence: {biasResult.confidence}</div>
                  <div className={`text-3xl font-display font-extrabold ${
                    biasColor(biasResult.bias_label) === 'emerald' ? 'text-emerald-400' :
                    biasColor(biasResult.bias_label) === 'rose' ? 'text-rose-400' : 'text-amber-400'
                  }`}>{BIAS_LABEL[biasResult.bias_label] || biasResult.bias_label}</div>
                </div>
                <div className="text-right">
                  <div className="text-[10px] font-num uppercase tracking-widest text-stone-400 mb-1 flex items-center gap-2">
                    <span>Spot</span>
                    {lastSpotFetch && (
                      <span className="text-stone-500 normal-case font-normal tracking-normal" title={lastSpotFetch.toLocaleString()}>
                        · {lastSpotFetch.toLocaleTimeString()}
                      </span>
                    )}
                  </div>
                  <div className="text-2xl font-num font-bold text-stone-100">${(spotPrice ?? biasResult.spot)?.toFixed(2)}</div>
                </div>
              </div>

              <div className="mb-5">
                <div className="flex justify-between text-[10px] font-num text-stone-400 mb-1.5 uppercase tracking-wider">
                  <span>Bearish</span><span className="text-score font-num font-bold text-stone-400">Score: {biasResult.directional_score}</span><span>Bullish</span>
                </div>
                <div className="relative h-2 bg-stone-900 rounded-full overflow-hidden">
                  <div className="absolute inset-y-0 left-1/2 w-px bg-stone-700" />
                  <div className={`absolute inset-y-0 gauge-fill ${
                    biasResult.directional_score > 0 ? 'left-1/2 bg-gradient-to-r from-amber-500 to-emerald-400' : 'right-1/2 bg-gradient-to-l from-amber-500 to-rose-400'
                  }`} style={{ width: `${Math.abs(biasResult.directional_score) / 2}%` }} />
                </div>
              </div>

              {/* Collapsible bias narrative + 4 Greek signals (v4.7+) */}
              <div className="mb-5">
                <button
                  onClick={() => setBiasNarrativeOpen(o => !o)}
                  className="w-full flex items-center justify-between text-left bg-stone-200/60 hover:bg-stone-200 border border-stone-500/60 rounded-md px-3 py-2 transition-colors"
                  aria-expanded={biasNarrativeOpen}
                >
                  <span className="text-[11px] font-num uppercase tracking-widest font-bold text-stone-700">
                    Bias narrative &amp; Greek signals
                  </span>
                  <span className={`text-stone-700 font-bold text-sm transition-transform ${biasNarrativeOpen ? 'rotate-90' : ''}`}>▸</span>
                </button>
                {biasNarrativeOpen && (
                  <div className="mt-3 space-y-3">
                    <p className="text-stone-200 leading-relaxed italic">"{biasResult.summary}"</p>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <Signal label="DEX Skew" text={biasResult.signals?.dex_skew} />
                      <Signal label="GEX Wall" text={biasResult.signals?.gex_wall} />
                      <Signal label="Gamma Regime" text={biasResult.signals?.gamma_regime} />
                      <Signal label="Spot vs. Wall" text={biasResult.signals?.spot_vs_wall} />
                    </div>
                  </div>
                )}
              </div>

              <div className="pt-5 border-t border-stone-800/60">
                {/* Header row with legend */}
                <div className="flex items-start justify-between mb-3 flex-wrap gap-3">
                  <div className="text-[10px] font-num uppercase tracking-widest text-stone-400">Strategy</div>
                  <div className="flex items-center gap-3 text-[9px] font-num uppercase tracking-wider text-stone-400 flex-wrap">
                    <span className="flex items-center gap-1.5"><span className="text-emerald-400 font-bold">★</span><span>Engine pick</span></span>
                    <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-3 rounded-full bg-stone-100 ring-1 ring-stone-300"></span><span>Selected</span></span>
                    <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-3 rounded-full bg-emerald-950 ring-1 ring-emerald-700"></span><span>Recommended</span></span>
                    <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-3 rounded-full bg-stone-900 ring-1 ring-stone-700"></span><span>Fits bias</span></span>
                    <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-3 rounded-full bg-stone-900/40 ring-1 ring-stone-800"></span><span>Off-bias</span></span>
                  </div>
                </div>

                <div className="flex flex-wrap gap-2 mb-4">
                  {Object.entries(STRATEGIES).map(([key, s]) => {
                    const isRecommended = biasResult.recommended_strategies?.includes(key);
                    const isSelected = strategy === key;
                    const fits = s.fits.includes(biasResult.bias_label);
                    const reason = STRATEGY_FIT_REASONS[biasResult.bias_label]?.[key] || 'No specific fit data for this combination.';
                    const biasName = BIAS_LABEL[biasResult.bias_label] || biasResult.bias_label;

                    let stateLabel, stateColor;
                    if (isRecommended) { stateLabel = '★ Engine Pick'; stateColor = 'text-emerald-300'; }
                    else if (fits) { stateLabel = 'Fits Bias'; stateColor = 'text-stone-300'; }
                    else { stateLabel = 'Off-Bias'; stateColor = 'text-stone-500'; }

                    const tipContent = (
                      <>
                        <div className={`text-[10px] font-bold uppercase tracking-wider mb-1.5 ${stateColor}`}>
                          {stateLabel} · {biasName}
                        </div>
                        <div className="text-stone-200">{reason}</div>
                      </>
                    );

                    return (
                      <Tooltip key={key} content={tipContent}>
                        <button onClick={() => setStrategy(key)}
                          className={`px-4 py-2 rounded-full text-xs font-num uppercase tracking-wider transition-all ${
                            isSelected ? 'bg-stone-100 text-stone-900 font-bold'
                              : isRecommended ? 'bg-emerald-950/60 text-emerald-300 border border-emerald-700/60 hover:bg-emerald-900/60'
                              : fits ? 'bg-stone-900/60 text-stone-300 border border-stone-700 hover:text-stone-100'
                              : 'bg-stone-900/40 text-stone-600 border border-stone-800/60 hover:text-stone-400'
                          }`}>
                          {isRecommended && '★ '}{s.short}
                        </button>
                      </Tooltip>
                    );
                  })}
                </div>

                <button onClick={optimize} disabled={chainLoading || !strategy}
                  className="w-full bg-emerald-200 hover:bg-emerald-100 disabled:bg-stone-300 disabled:text-stone-500 disabled:cursor-not-allowed border border-emerald-700 text-emerald-900 font-bold py-3 rounded-lg uppercase tracking-widest text-sm transition-colors">
                  {chainLoading
                    ? '◌ Refreshing chain...'
                    : (
                      <>
                        {chainData ? `↻ Refresh chain & re-rank ${STRATEGIES[strategy]?.short || 'Spreads'}` : `→ Optimize ${STRATEGIES[strategy]?.short || 'Spreads'}`}
                        {isSandbox && <span className="ml-2 text-rose-700">(SANDBOX)</span>}
                      </>
                    )}
                </button>
              </div>
            </section>
          )}

          {/* RESULTS */}
          {chainLoading && !candidates.length && (
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
              {[0, 1, 2].map(i => <div key={i} className="bg-[#13131c] border border-stone-800/60 rounded-2xl p-6 h-[28rem] skeleton" />)}
            </div>
          )}

          {/* v4.7.35: chain-stale banner shown after a 2nd+ bias detect.
              Prompts the user to refresh chain & re-rank for fresh repaints. */}
          {chainStale && chainData && (
            <div className="mb-5 p-4 rounded-lg border bg-amber-950/30 border-amber-800/60 text-amber-200 flex items-center justify-between flex-wrap gap-3">
              <div>
                <div className="text-[11px] font-num uppercase tracking-widest font-bold text-amber-300">◎ Bias updated · candidates collapsed</div>
                <div className="text-sm mt-1 leading-snug">Chain prices are now stale relative to the new bias. Hit <span className="font-bold">Refresh chain &amp; re-rank</span> below to repaint the candidate cards and Lookup grids against the fresh read.</div>
              </div>
            </div>
          )}

          {chainData && candidates.length > 0 && !chainStale && (
            <div className="space-y-6">
              <div className="grid grid-cols-2 sm:grid-cols-6 gap-3">
                <Stat label="Spot" value={`$${(spotPrice ?? chainData.spot)?.toFixed(2)}`} sub={lastSpotFetch ? `updated ${lastSpotFetch.toLocaleTimeString()}` : null} />
                <Stat label="Exp Move" value={`±$${chainData.expectedMove?.toFixed(2)}`} sub={`±${chainData.expectedMovePct?.toFixed(1)}%`} />
                <Stat label="ATM IV" value={`${chainData.atmIV?.toFixed(0)}%`} />
                <Stat label="DTE" value={_formatDteForDisplay(chainData.dte)} />
                <Stat label="Width Pref" value={WIDTH_PREFS[widthPref].name} />
                <Stat
                  label="GEX Wall"
                  value={biasResult?.gex_wall_strike != null ? `$${Number(biasResult.gex_wall_strike).toFixed(2)}` : '—'}
                  sub={biasResult?.gex_wall_strike != null ? `${biasResult.gex_wall_strength || 'medium'} strength` : 'no wall data'}
                />
              </div>

              <div className="bg-stone-900/40 border border-stone-800/40 rounded-lg p-3 text-[13px] text-stone-200 italic tracking-wide leading-relaxed">
                <span className="font-num font-bold uppercase tracking-widest text-stone-300 mr-2 not-italic">Width Logic:</span>
                {chainData.dte < 1 ? `DTE ${_formatDteForDisplay(chainData.dte)} → 0.4× expected move base` : chainData.dte <= 7 ? `DTE ${Math.floor(chainData.dte)} → 1.0× expected move base` : `DTE ${Math.floor(chainData.dte)} → 1.5× expected move base`}, multiplied by {WIDTH_PREFS[widthPref].factor}× ({WIDTH_PREFS[widthPref].name}).
                {biasResult?.gex_wall_strike != null && (
                  <span className="ml-2 text-stone-300 font-bold not-italic">· Wall-aware ranking active.</span>
                )}
              </div>

              {/* Tab switcher (v4.7.20) — Tickets vs Lookup */}
              <div className="flex items-center gap-1 border-b border-stone-800/60 mb-1">
                {[
                  { key: 'tickets', label: `Tickets (${candidates.length})`, hint: 'Three ranked candidate spreads with composite scores and limit-order tiers.' },
                  { key: 'lookup',  label: 'Lookup',                          hint: 'Project each candidate\'s premium across hypothetical spot × time. Read-only.' },
                ].map(t => {
                  const active = resultsTab === t.key;
                  return (
                    <button key={t.key} onClick={() => setResultsTab(t.key)} title={t.hint}
                      className={`px-4 py-2 text-[11px] font-num uppercase tracking-widest font-bold cursor-pointer transition-colors border-b-2 -mb-px ${
                        active ? 'border-emerald-500 text-emerald-300' : 'border-transparent text-stone-500 hover:text-stone-300'
                      }`}>
                      {t.label}
                    </button>
                  );
                })}
              </div>

              {/* TICKETS TAB — composite banner + 3-card grid */}
              {resultsTab === 'tickets' && (
                <>
                  {candidates.length > 0 && (() => {
                    const best = [...candidates].sort((a, b) => (b.composite_score ?? 0) - (a.composite_score ?? 0))[0];
                    const v = best.composite_verdict;
                    const cls = v.color === 'emerald' ? 'bg-emerald-50 border-emerald-700 text-emerald-900'
                              : v.color === 'amber'   ? 'bg-amber-50 border-amber-700 text-amber-900'
                                                      : 'bg-rose-50 border-rose-700 text-rose-900';
                    return (
                      <div className={`mb-5 p-4 rounded-lg border ${cls} flex items-center justify-between flex-wrap gap-3`}>
                        <div>
                          <div className="text-[11px] font-num uppercase tracking-widest font-bold">Top composite</div>
                          <div className="font-num text-lg font-bold mt-0.5">{best.composite_score.toFixed(1)} / 100 — {v.label}</div>
                        </div>
                        <div className="text-sm font-bold max-w-md text-right leading-snug">
                          {v.mode === 'market' && 'Engine pick is tradeable at market — EV positive, structure healthy, liquidity workable.'}
                          {v.mode === 'limit'  && 'Top pick qualifies on a limit order — set a GTC at the limit-target price below and wait for fill.'}
                          {v.mode === 'wait'   && 'No setup is clearly tradeable. Consider patience on a limit order, or move to a different expiration.'}
                          {v.mode === 'skip'   && 'Skip — no candidate has both EV edge and acceptable structure. Try a different ticker or expiration.'}
                        </div>
                      </div>
                    );
                  })()}

                  <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
                    {candidates.map((c, idx) => (
                      <CandidateCard
                        key={idx}
                        candidate={c}
                        isBest={c.label === bestPick}
                        symbol={symbol}
                        expiration={expiration}
                        biasResult={biasResult}
                        contracts={chainData?.contracts}
                        onPushBroker={(cand) => setOrderDialog({ candidate: cand })}
                        hasActiveBroker={!!broker.active}
                      />
                    ))}
                  </div>
                </>
              )}

              {/* LOOKUP TAB — projection grids (v4.7.20 phase 1; v4.7.29 adds
                  VEX/TEX cluster annotations + tooltip P&L decomposition) */}
              {resultsTab === 'lookup' && (
                <div className="space-y-5">
                  <div className="bg-stone-900/40 border border-stone-800/40 rounded-lg p-3 text-[12px] text-stone-300 leading-relaxed">
                    <span className="font-num font-bold uppercase tracking-widest text-stone-400 mr-2">Lookup:</span>
                    Premium over the next 3 hours at 15-minute steps, assuming today\'s implied volatility. Rows = hypothetical spot. <span className="text-emerald-300">Green</span> = profitable to close at that price; <span className="text-rose-300">red</span> = at a loss.
                    <span className="text-stone-400"> Hover any cell to see the theta / delta / vega breakdown.</span>
                    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-stone-500">
                      <span><span className="text-emerald-400">▸</span> current spot</span>
                      <span><span className="text-amber-400">●</span> VEX cluster — vol re-prices fastest here</span>
                      <span><span className="text-amber-400">⌛</span> TEX cluster — theta-burn window (≤1 DTE only)</span>
                    </div>
                    {/* v4.7.39: cell color legend — same 7 levels the heatmap renders */}
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-stone-400">
                      <span className="text-stone-500 uppercase tracking-widest font-num font-bold mr-1">Cell color:</span>
                      <span className="inline-flex items-center gap-1.5"><span className="inline-block w-4 h-3 rounded-sm bg-emerald-700/70 border border-emerald-600/50"></span>≥55% max profit</span>
                      <span className="inline-flex items-center gap-1.5"><span className="inline-block w-4 h-3 rounded-sm bg-emerald-800/55"></span>25–55%</span>
                      <span className="inline-flex items-center gap-1.5"><span className="inline-block w-4 h-3 rounded-sm bg-emerald-900/40"></span>8–25%</span>
                      <span className="inline-flex items-center gap-1.5"><span className="inline-block w-4 h-3 rounded-sm bg-stone-800/40 border border-stone-700/50"></span>±8% (near breakeven)</span>
                      <span className="inline-flex items-center gap-1.5"><span className="inline-block w-4 h-3 rounded-sm bg-rose-900/40"></span>−8 to −25%</span>
                      <span className="inline-flex items-center gap-1.5"><span className="inline-block w-4 h-3 rounded-sm bg-rose-800/55"></span>−25 to −55%</span>
                      <span className="inline-flex items-center gap-1.5"><span className="inline-block w-4 h-3 rounded-sm bg-rose-700/70 border border-rose-600/50"></span>≥55% max loss</span>
                    </div>
                  </div>
                  {candidates.map((c, idx) => {
                    const liveSpot = spotPrice ?? chainData.spot;
                    const grid = _buildProjectionGrid(c, liveSpot, chainData.expectedMove, { expiration });
                    if (!grid) {
                      return (
                        <div key={idx} className="bg-[#13131c] border border-stone-800/60 rounded-2xl p-4 text-stone-400 text-sm">
                          <span className="font-bold text-stone-200">{c.label}</span> — projection unavailable (missing leg IV or DTE data).
                        </div>
                      );
                    }
                    const isCredit = grid.isCredit;
                    const live = grid.currentPremium;
                    const annot = _buildLookupAnnotations(c, chainData?.contracts || [], grid);
                    return (
                      <div key={idx} className="bg-[#13131c] border border-stone-800/60 rounded-2xl p-4">
                        <div className="flex items-baseline justify-between mb-3 flex-wrap gap-2">
                          <div>
                            <span className={`text-xs font-num uppercase tracking-widest font-bold ${
                              c.label === 'Conservative' ? 'text-emerald-400' : c.label === 'Balanced' ? 'text-amber-400' : 'text-rose-400'
                            }`}>{c.label}</span>
                            <span className="text-stone-500 text-[12px] font-num ml-3">{c.structure_text}</span>
                          </div>
                          <div className="text-[11px] font-num text-stone-400">
                            Live {isCredit ? 'credit' : 'debit'}: <span className="font-bold text-stone-200">{isCredit ? '+' : '−'}${live.toFixed(2)}</span>
                            {grid.missingIv && <span className="ml-2 text-amber-400 italic">(some legs missing IV — intrinsic fallback)</span>}
                          </div>
                        </div>
                        <div className="overflow-x-auto">
                          <table className="w-full font-num text-[12px] border-separate" style={{borderSpacing: '2px'}}>
                            <thead>
                              <tr>
                                <th className="text-left text-[10px] font-num uppercase tracking-widest text-stone-500 px-2 py-1">Spot ↓ / Time →</th>
                                {grid.timeAxis.map((t, j) => {
                                  const isTexCol = annot.texClusterCols.has(j);
                                  return (
                                    <th key={j} className={`text-center text-[10px] font-num uppercase tracking-widest px-2 py-1 ${
                                      isTexCol ? 'bg-amber-900/30 text-amber-200 rounded' : 'text-stone-500'
                                    }`}>
                                      {t.label} {isTexCol && <span className="text-amber-400 ml-0.5">⌛</span>}
                                    </th>
                                  );
                                })}
                              </tr>
                            </thead>
                            <tbody>
                              {grid.grid.map((row, i) => {
                                const isCenter = i === grid.centerRow;
                                const isVexRow = annot.vexClusterRows.has(i);
                                return (
                                  <tr key={i}>
                                    <td className={`px-2 py-1 text-right ${isCenter ? 'text-stone-100 font-bold' : 'text-stone-400'}`}>
                                      {isVexRow && <span className="text-amber-400 mr-1">●</span>}
                                      ${grid.spotAxis[i].toFixed(2)}
                                      {isCenter && <span className="ml-1 text-[9px] text-emerald-400">▸</span>}
                                    </td>
                                    {row.map((px, j) => {
                                      if (px == null) return <td key={j} className="px-2 py-1 text-stone-600">—</td>;
                                      const pnl = isCredit ? (live - px) : (px - live);
                                      const denom = pnl > 0 ? Math.max(0.05, c.max_profit) : Math.max(0.05, c.max_loss);
                                      const norm = Math.max(-1, Math.min(1, pnl / denom));
                                      let level;
                                      if (Math.abs(norm) < 0.08) level = 0;
                                      else if (norm > 0) level = norm < 0.25 ? 1 : norm < 0.55 ? 2 : 3;
                                      else               level = norm > -0.25 ? -1 : norm > -0.55 ? -2 : -3;
                                      const cls = ({
                                         3: 'bg-emerald-700/70 text-emerald-50 font-bold',
                                         2: 'bg-emerald-800/55 text-emerald-100',
                                         1: 'bg-emerald-900/40 text-emerald-200',
                                         0: 'bg-stone-800/40 text-stone-300',
                                        '-1': 'bg-rose-900/40 text-rose-200',
                                        '-2': 'bg-rose-800/55 text-rose-100',
                                        '-3': 'bg-rose-700/70 text-rose-50 font-bold',
                                      })[level];
                                      const dc = _decomposeCellPnl(c, grid, i, j);
                                      const ringCls = isCenter && j === 0
                                        ? 'ring-1 ring-emerald-400'
                                        : (isVexRow || annot.texClusterCols.has(j))
                                          ? 'ring-1 ring-amber-700/40'
                                          : '';
                                      const sgn = (v) => (v >= 0 ? '+' : '−') + '$' + Math.abs(v).toFixed(0);
                                      const tipContent = (
                                        <div className="text-left text-[11px] leading-relaxed">
                                          <div className="text-stone-300 font-bold uppercase tracking-wider text-[10px] mb-1">
                                            Spot ${grid.spotAxis[i].toFixed(2)} · {grid.timeAxis[j].label}
                                          </div>
                                          <div>premium <span className={isCredit ? 'text-emerald-300' : 'text-rose-300'}>{isCredit ? '+' : '−'}${Math.abs(px).toFixed(2)}</span> · close-now P&L <span className={pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{sgn(pnl * 100)}/contract</span></div>
                                          {dc && (
                                            <div className="mt-1 text-stone-300 text-[10px]">
                                              composed: <span className={dc.theta >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{sgn(dc.theta)} theta</span> · <span className={dc.delta >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{sgn(dc.delta)} delta</span> · <span className={dc.residual >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{sgn(dc.residual)} γ/vega</span>
                                            </div>
                                          )}
                                          {isVexRow && (
                                            <div className="mt-1 text-amber-300 text-[10px]">⚠ strike near VEX cluster — vol shock could move premium ~1.3× this estimate</div>
                                          )}
                                          {annot.texClusterCols.has(j) && (
                                            <div className="mt-1 text-amber-300 text-[10px]">⚠ horizon in TEX cluster — actual decay may be ~1.4× this estimate</div>
                                          )}
                                        </div>
                                      );
                                      return (
                                        <td key={j} className={`px-2 py-1 text-center rounded ${cls} ${ringCls}`}>
                                          <Tooltip content={tipContent}>
                                            <div className="cursor-help">{isCredit ? '+' : '−'}${Math.abs(px).toFixed(2)}</div>
                                          </Tooltip>
                                        </td>
                                      );
                                    })}
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                        <div className="text-[10px] text-stone-500 mt-2 italic leading-snug">
                          Current spot ${liveSpot.toFixed(2)} marks the center row (green ▸). Color intensity scales with how close P&amp;L is to this spread\'s max profit (deep green) or max loss (deep red). Amber rings indicate VEX or TEX cluster proximity.
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}


          {!biasLoading && !biasResult && !error && (
            <div className="text-center py-16 text-stone-500">
              <div className="font-num text-xs uppercase tracking-widest mb-2">Awaiting input</div>
              <div className="text-2xl font-display font-light italic">Symbol → expiration → detect bias →</div>
            </div>
          )}

          <footer className="mt-12 pt-6 border-t border-stone-800/60 text-xs text-stone-600 font-num text-center">
            Not financial advice. Refresh to get the latest data.
            <span className="mx-2 text-stone-700">·</span>
            © {new Date().getFullYear()} EdgeLane. All rights reserved.
          </footer>
        </div>
      </div>

      {/* v4.7.30: settings + push-to-broker modals */}
      {settingsOpen && (
        <SettingsModal
          session={auth.session}
          broker={broker}
          onClose={() => setSettingsOpen(false)}
        />
      )}
      {orderDialog && (
        <OrderDialog
          candidate={orderDialog.candidate}
          symbol={symbol}
          expiration={expiration}
          connection={broker.active}
          onClose={() => setOrderDialog(null)}
        />
      )}
    </>
  );
}
