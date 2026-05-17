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
    strike: _num(c.strike ?? c.strike_price ?? c.strikePrice),
    side,
    expiration: expiration ?? c.expiration ?? c.expiry ?? c.exp_date ?? null,
    bid: bid ?? 0,
    ask: ask ?? 0,
    mid: mid ?? 0,
    delta: _num(c.delta ?? c.greeks?.delta),
    gamma: _num(c.gamma ?? c.greeks?.gamma),
    theta: _num(c.theta ?? c.greeks?.theta),
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

    const dte = Math.max(0, Math.round((new Date(`${expiration}T21:00:00Z`) - new Date()) / 86400000));
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
    portfolio_totals: { net_gex: 0, net_dex: 0 },
    key_levels: { call_wall: null, put_wall: null },
  };
}

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
  let portNetGex = 0, portNetDex = 0;
  const allStrikesGex = new Map();

  for (const [exp, sm] of buckets) {
    const sortedStrikes = [...sm.keys()].sort((a, b) => a - b);
    const by_strike = [];
    let expNetGex = 0, expNetDex = 0;
    for (const strike of sortedStrikes) {
      const sides = sm.get(strike);
      const call = sides.call || {};
      const put = sides.put || {};
      const cG = Number(call.gamma) || 0;
      const pG = Number(put.gamma) || 0;
      const cD = Number(call.delta) || 0;
      const pD = Number(put.delta) || 0;
      const cOi = Number(call.open_interest) || 0;
      const pOi = Number(put.open_interest) || 0;
      const callGex = cG * cOi * _DEALER_CONTRACT_MULT * spotSq;
      const putGex  = pG * pOi * _DEALER_CONTRACT_MULT * spotSq;
      const netGex  = putGex - callGex;
      const callDex = cD * cOi * _DEALER_CONTRACT_MULT * spot;
      const putDex  = pD * pOi * _DEALER_CONTRACT_MULT * spot;
      const netDex  = callDex - putDex;
      by_strike.push({
        strike, call_gex: callGex, put_gex: putGex, net_gex: netGex,
        call_dex: callDex, put_dex: putDex, net_dex: netDex,
        call_oi: cOi, put_oi: pOi,
      });
      expNetGex += netGex;
      expNetDex += netDex;
      allStrikesGex.set(strike, (allStrikesGex.get(strike) || 0) + netGex);
    }
    exposures_by_date[exp] = { by_strike, totals: { net_gex: expNetGex, net_dex: expNetDex } };
    portNetGex += expNetGex;
    portNetDex += expNetDex;
  }

  let callWall = null, putWall = null;
  let bestCallGex = 0, bestPutGex = 0;
  for (const [strike, gex] of allStrikesGex) {
    if (strike > spot && gex < bestCallGex) { bestCallGex = gex; callWall = { strike, gex }; }
    if (strike < spot && gex > bestPutGex)  { bestPutGex  = gex; putWall  = { strike, gex }; }
  }

  return {
    exposures_by_date,
    portfolio_totals: { net_gex: portNetGex, net_dex: portNetDex },
    key_levels: { call_wall: callWall, put_wall: putWall },
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

    const dte = Math.max(0, Math.round((new Date(`${expiration}T21:00:00Z`) - new Date()) / 86400000));
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
function _computeBiasSignals(symbol, expiration, spot, greeksRaw, exposuresForChosen) {
  const wall = _findGexWall(greeksRaw, exposuresForChosen);
  const { netGex, netDex } = _aggregateExposures(exposuresForChosen, greeksRaw?.portfolio_totals);
  const score = _computeDirectionalScore(spot, wall, netGex, netDex);
  const biasLabel = _scoreToBiasLabel(score);
  const confidence = _computeConfidence(score, wall, netDex, spot);
  const recommended = _recommendStrategies(biasLabel);

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
    recommended_strategies: recommended,
    // structured facts the prose call will rephrase
    _facts: { wallSide, gammaRegime, dexSkewSide, netGex, netDex, wall },
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
  if (dte <= 0) return 0.4 * expectedMove;
  if (dte <= 7) return 1.0 * expectedMove;
  return 1.5 * expectedMove;
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

  // Intraday-focused time axis (v4.7.21): 15-minute steps up to 3 hours
  // ahead, capped at expiration. Matches how traders actually use this —
  // "what's premium worth in 30 min if spot ticks to $X" — not multi-day
  // theta strategy. For longer-DTE spreads, the user re-checks later.
  const MIN_STEP = 15;        // minutes per cell
  const MAX_COLS = 12;        // hard cap (3 hours of projection)
  const totalMin = candidate.dte * 1440;
  const maxStepsToExp = Math.floor(totalMin / MIN_STEP);
  const effectiveCols = Math.max(2, Math.min(MAX_COLS, maxStepsToExp + 1));
  const timeAxis = [];
  for (let j = 0; j < effectiveCols; j++) {
    const elapsedMin = j * MIN_STEP;
    const dteRem = Math.max(0, candidate.dte - elapsedMin / 1440);
    let label;
    if (j === 0) label = 'now';
    else if (dteRem === 0) label = 'exp';
    else if (elapsedMin < 60) label = `+${elapsedMin}m`;
    else {
      const h = Math.floor(elapsedMin / 60);
      const m = elapsedMin % 60;
      label = m === 0 ? `+${h}h` : `+${h}h${m}`;
    }
    timeAxis.push({ label, dteRem });
  }

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
      { strike: long.strike,  side: long.side,  longShort:  1, iv: long.iv  > 0 ? long.iv  / 100 : null },
      { strike: short.strike, side: short.side, longShort: -1, iv: short.iv > 0 ? short.iv / 100 : null },
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
      { strike: lp.strike, side: 'put',  longShort:  1, iv: lp.iv > 0 ? lp.iv / 100 : null },
      { strike: sp.strike, side: 'put',  longShort: -1, iv: sp.iv > 0 ? sp.iv / 100 : null },
      { strike: sc.strike, side: 'call', longShort: -1, iv: sc.iv > 0 ? sc.iv / 100 : null },
      { strike: lc.strike, side: 'call', longShort:  1, iv: lc.iv > 0 ? lc.iv / 100 : null },
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
      { strike: low.strike,  side, longShort:  1, iv: low.iv  > 0 ? low.iv  / 100 : null },
      { strike: mid.strike,  side, longShort: -2, iv: mid.iv  > 0 ? mid.iv  / 100 : null },
      { strike: high.strike, side, longShort:  1, iv: high.iv > 0 ? high.iv / 100 : null },
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

function CardActions({ candidate, symbol, expiration }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      const ticket = _buildTradeTicket(candidate, symbol, expiration);
      await navigator.clipboard.writeText(ticket);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (e) { console.error('Copy failed:', e); }
  };
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
      <Tooltip content="Push order to broker — configure under user settings (coming in v4.8+)">
        <button disabled aria-label="Push to broker (disabled)"
          className="w-8 h-8 rounded-md border border-stone-400 bg-stone-200/50 text-stone-500 flex items-center justify-center cursor-not-allowed opacity-60">
          <span className="text-base leading-none">↗</span>
        </button>
      </Tooltip>
    </div>
  );
}

function CandidateCard({ candidate, isBest, symbol, expiration }) {
  const c = candidate;
  const isCredit = c.type === 'credit';
  const labelColor = c.label === 'Conservative' ? 'emerald' : c.label === 'Balanced' ? 'amber' : 'rose';
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
      <CardActions candidate={c} symbol={symbol} expiration={expiration} />

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
      // fresh quote we just pulled. (See spotPrice state declaration.)
      setSpotPrice(spot);

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
      if (data?.spot) setSpotPrice(Number(data.spot));
      setChainCache({ key: cacheKey, data }); setLastChainFetch(new Date());
      setLogSteps(s => [...s, `Chain cached: ${data.contracts?.length || 0} contracts (filtered), spot $${Number(data.spot).toFixed(2)}.`]);
    } catch (e) {
      setError(e.message);
    } finally {
      setChainLoading(false);
    }
  }, [symbol, expiration, cacheKey, chainCache]);

  const reloadAll = async () => {
    // Don't pre-clear caches — leave the previous bias/strategies visible
    // while the refetch is in flight. Successful fetches will overwrite
    // (force=true bypasses the early-return cache check); failures keep
    // the prior state so the page doesn't wipe to a blank on a CORS/rate
    // limit error.
    setLogSteps(['Reloading all data...']);
    await detectBias(true);
    await fetchChain(true);
  };

  const optimize = async () => {
    // v4.7.3: always force-refetch the chain. Bias is left alone (it changes
    // on a slower timescale than option premiums, and refetching it would
    // burn 2× the Atlas calls for unchanged data). For a full reset, use
    // the Reload all data button instead.
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
                Reads dealer hedging structure for tape bias, caches the chain, then ranks spreads
                client-side. Slider tweaks re-rank instantly without re-fetching.
              </p>
            </div>


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
              <Tooltip content="Clears cached bias + chain. Refetches both from provider. Use after major market move or when switching tickers.">
                <button
                  onClick={reloadAll}
                  disabled={isLoading || !symbol || !expiration}
                  className="bg-stone-200 hover:bg-stone-100 disabled:opacity-50 disabled:cursor-not-allowed border border-stone-500 px-3 py-1.5 rounded-md text-[10px] font-num uppercase tracking-widest text-stone-800 flex items-center gap-1.5 transition-colors"
                >
                  <span className={isLoading ? 'spin inline-block' : 'inline-block'}>↻</span>
                  <span>Reload all data</span>
                </button>
              </Tooltip>
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
                : biasResult
                  ? '↻ Re-detect bias from Greeks'
                  : '⟶ Detect bias from Greeks'}
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
                  <div className="text-[10px] font-num uppercase tracking-widest text-stone-400 mb-1">Spot</div>
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
                  {chainLoading ? '◌ Refreshing chain...' : chainData ? `↻ Refresh chain & re-rank ${STRATEGIES[strategy]?.short || 'Spreads'}` : `→ Optimize ${STRATEGIES[strategy]?.short || 'Spreads'}`}
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

          {chainData && candidates.length > 0 && (
            <div className="space-y-6">
              <div className="grid grid-cols-2 sm:grid-cols-6 gap-3">
                <Stat label="Spot" value={`$${(spotPrice ?? chainData.spot)?.toFixed(2)}`} />
                <Stat label="Exp Move" value={`±$${chainData.expectedMove?.toFixed(2)}`} sub={`±${chainData.expectedMovePct?.toFixed(1)}%`} />
                <Stat label="ATM IV" value={`${chainData.atmIV?.toFixed(0)}%`} />
                <Stat label="DTE" value={chainData.dte} />
                <Stat label="Width Pref" value={WIDTH_PREFS[widthPref].name} />
                <Stat
                  label="GEX Wall"
                  value={biasResult?.gex_wall_strike != null ? `$${Number(biasResult.gex_wall_strike).toFixed(2)}` : '—'}
                  sub={biasResult?.gex_wall_strike != null ? `${biasResult.gex_wall_strength || 'medium'} strength` : 'no wall data'}
                />
              </div>

              <div className="bg-stone-900/40 border border-stone-800/40 rounded-lg p-3 text-[13px] text-stone-200 italic tracking-wide leading-relaxed">
                <span className="font-num font-bold uppercase tracking-widest text-stone-300 mr-2 not-italic">Width Logic:</span>
                {chainData.dte === 0 ? 'DTE 0 → 0.4× expected move base' : chainData.dte <= 7 ? `DTE ${chainData.dte} → 1.0× expected move base` : `DTE ${chainData.dte} → 1.5× expected move base`}, multiplied by {WIDTH_PREFS[widthPref].factor}× ({WIDTH_PREFS[widthPref].name}).
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
                    {candidates.map((c, idx) => <CandidateCard key={idx} candidate={c} isBest={c.label === bestPick} symbol={symbol} expiration={expiration} />)}
                  </div>
                </>
              )}

              {/* LOOKUP TAB — projection grids (v4.7.20 phase 1, read-only) */}
              {resultsTab === 'lookup' && (
                <div className="space-y-5">
                  <div className="bg-stone-900/40 border border-stone-800/40 rounded-lg p-3 text-[12px] text-stone-300 leading-relaxed">
                    <span className="font-num font-bold uppercase tracking-widest text-stone-400 mr-2">Lookup:</span>
                    Premium over the next 3 hours at 15-minute steps, assuming today\'s implied volatility. Rows = hypothetical spot. <span className="text-emerald-300">Green</span> = profitable to close at that price; <span className="text-rose-300">red</span> = at a loss. Hover any cell for the P&amp;L number.
                  </div>
                  {candidates.map((c, idx) => {
                    const liveSpot = spotPrice ?? chainData.spot;
                    const grid = _buildProjectionGrid(c, liveSpot, chainData.expectedMove);
                    if (!grid) {
                      return (
                        <div key={idx} className="bg-[#13131c] border border-stone-800/60 rounded-2xl p-4 text-stone-400 text-sm">
                          <span className="font-bold text-stone-200">{c.label}</span> — projection unavailable (missing leg IV or DTE data).
                        </div>
                      );
                    }
                    const isCredit = grid.isCredit;
                    const live = grid.currentPremium;
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
                                {grid.timeAxis.map((t, j) => (
                                  <th key={j} className="text-center text-[10px] font-num uppercase tracking-widest text-stone-500 px-2 py-1">{t.label}</th>
                                ))}
                              </tr>
                            </thead>
                            <tbody>
                              {grid.grid.map((row, i) => {
                                const isCenter = i === grid.centerRow;
                                return (
                                  <tr key={i}>
                                    <td className={`px-2 py-1 text-right ${isCenter ? 'text-stone-100 font-bold' : 'text-stone-400'}`}>
                                      ${grid.spotAxis[i].toFixed(2)}
                                      {isCenter && <span className="ml-1 text-[9px] text-emerald-400">●</span>}
                                    </td>
                                    {row.map((px, j) => {
                                      if (px == null) return <td key={j} className="px-2 py-1 text-stone-600">—</td>;
                                      // Close-now P&L per contract, in dollars.
                                      //   CREDIT (sold-to-open):  pnl = entry_credit − buyback_cost
                                      //   DEBIT  (bought-to-open): pnl = sell_value − entry_debit
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
                                      const tip = `Spot $${grid.spotAxis[i].toFixed(2)} · ${grid.timeAxis[j].label} → premium ${isCredit ? '+' : '−'}$${Math.abs(px).toFixed(2)} · close-now P&L ${pnl >= 0 ? '+' : '−'}$${Math.abs(pnl * 100).toFixed(0)}/contract`;
                                      return (
                                        <td key={j} title={tip}
                                            className={`px-2 py-1 text-center rounded ${cls} ${isCenter && j === 0 ? 'ring-1 ring-emerald-400' : ''}`}>
                                          {isCredit ? '+' : '−'}${Math.abs(px).toFixed(2)}
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
                          Current spot ${liveSpot.toFixed(2)} marks the center row (green dot). Color intensity scales with how close P&amp;L is to this spread\'s max profit (deep green) or max loss (deep red).
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
            Not financial advice. Scoring is client-side from cached chain data. Reload after major price moves.
          </footer>
        </div>
      </div>
    </>
  );
}
