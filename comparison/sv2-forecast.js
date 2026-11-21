// Sentiment V2 forecasts, evaluated read-only (owner request 2026-11-21, steps 1-3; frozen protocol SV2F-1).
//
//   GET /api/sv2-forecast?horizon=12|24
//
// For every stored Sentiment V2 reading (BTC, current calc_version) at decision time t, three V2 forecasts and the V1
// baseline, all scored against the same BTC outcome:
//   1. v2_signal    the V2 sentiment score as a direction (same PROPOSED convention as V1: > 55 UP, < 45 DOWN, else no
//                   call; bands 50/50, 45/55, 40/60 reported)
//   2. v2_analogue  the candidate p_up stored in the reading itself (predictive_json, "family_profile_analogues",
//                   24 h only, labelled candidate_unvalidated by its author; n = 20 analogues, so steps of 0.05).
//   3. v2_knn       a k-NN on V2 features (score, evidence coverage, event intensity), computed WALK-FORWARD: at t it
//                   trains only on earlier readings whose outcome was already known at t (outcome price time <= t).
//                   Nothing is stored; the same inputs always give the same answer (model version SV2_KNN_VERSION).
//   V1 baseline     V1 composite (server_composite_readings, else history) at or before t, at most 3 h old.
// Outcome: BTC price at t (latest btc_data price <= t, at most 3 h old) vs the first btc_data price at or after t + h
// (within 6 h), used only once that price exists. No look-ahead: features and V1 come from at or before t.
//
// READ-ONLY: three SELECTs per uncached request (V2 readings, BTC prices, V1 readings), cached 5 min, never called
// from tick/cron paths, no write. Not production logic: nothing here feeds V1, the V2 engine or GLM.

import { wilson, v1Direction, V1_BANDS } from './v1-v2.js';

export const SV2F_PROTOCOL = 'SV2F-1';
export const SV2_KNN_VERSION = 'sv2-knn-0.1';
export const SV2F_LIMITS = Object.freeze({
  maxReadings: 2000, maxEval: 500, maxTrain: 500, maxPrices: 20000, priceMaxAgeMs: 3 * 3600 * 1000, targetToleranceMs: 6 * 3600 * 1000,
  v1MaxAgeMs: 3 * 3600 * 1000, minTrain: 20, kMin: 5, kMax: 15, minResolvedPairs: 30, claimMinN: 100, cacheSeconds: 300,
});
const HORIZONS = [12, 24];
const FEATURES = ['score', 'coverage', 'intensity'];
const JSON_HEADERS = { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${SV2F_LIMITS.cacheSeconds}` };
const num = (x) => (typeof x === 'number' && Number.isFinite(x) ? x : (typeof x === 'string' && x.trim() !== '' && Number.isFinite(Number(x)) ? Number(x) : null));
const r4 = (x) => (x == null ? null : Math.round(x * 1e4) / 1e4);
export const pDirection = (p) => (p == null ? null : p > 0.5 ? 'UP' : p < 0.5 ? 'DOWN' : 'NO_CALL');

// ---- SQL (bounded, indexed) ----------------------------------------------------------------------------------------
export const SV2_READINGS_SQL = `SELECT decision_ts, calc_version, status, sentiment_score, evidence_coverage, event_intensity, confidence, predictive_json
FROM sentiment_v2_results WHERE asset = 'BTC' AND status != 'FAILED' ORDER BY decision_ts ASC LIMIT ?1`;
export const BTC_PRICES_SQL = `SELECT ts, btc_price FROM btc_data WHERE ts >= ?1 ORDER BY ts ASC LIMIT ?2`;
export const V1_READINGS_SQL = `SELECT 'scr' AS src, slot_ts AS ts, computed_ts AS avail_ts, composite AS score FROM server_composite_readings
  WHERE slot_ts >= ?1 AND composite IS NOT NULL
UNION ALL SELECT 'history' AS src, ts, ts AS avail_ts, score FROM history WHERE ts >= ?1
ORDER BY ts ASC LIMIT ?2`;

// ---- pure helpers ---------------------------------------------------------------------------------------------------
// Latest element with key <= t (array sorted ascending by key), or null.
function lastAtOrBefore(arr, t, key = 'ts') {
  let lo = 0, hi = arr.length - 1, best = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (arr[mid][key] <= t) { best = mid; lo = mid + 1; } else hi = mid - 1; }
  return best < 0 ? null : arr[best];
}
function firstAtOrAfter(arr, t, key = 'ts') {
  let lo = 0, hi = arr.length - 1, best = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (arr[mid][key] >= t) { best = mid; hi = mid - 1; } else lo = mid + 1; }
  return best < 0 ? null : arr[best];
}

// Outcome of a decision at t over h hours from the BTC price series (sorted). `resolvedTs` is when it became known.
export function outcomeAt(prices, t, h, nowMs) {
  const p0 = lastAtOrBefore(prices, t);
  if (!p0 || t - p0.ts > SV2F_LIMITS.priceMaxAgeMs) return { status: 'no_price', direction: null, resolved_ts: null };
  const target = t + h * 3600 * 1000, p1 = firstAtOrAfter(prices, target);
  if (!p1 || p1.ts > nowMs) return { status: 'pending', direction: null, resolved_ts: null };
  if (p1.ts - target > SV2F_LIMITS.targetToleranceMs) return { status: 'no_price', direction: null, resolved_ts: null };
  const up = p1.btc_price > p0.btc_price ? 1 : 0;
  return { status: 'resolved', direction: up ? 'UP' : 'DOWN', realized_up: up, resolved_ts: p1.ts, ret: r4(p1.btc_price / p0.btc_price - 1) };
}

// The candidate p_up stored in the reading (24 h only), or null.
export function analogueOf(predictiveJson, h) {
  let pj; try { pj = JSON.parse(predictiveJson || '{}'); } catch { return null; }
  if (!pj || pj.status !== 'ok' || num(pj.horizon_h) !== h || num(pj.p_up) == null) return null;
  return { p_up: num(pj.p_up), n: num(pj.n), method: pj.method || null, validation: pj.validation || null };
}

const featuresOf = (r) => ({ score: num(r.sentiment_score), coverage: num(r.evidence_coverage), intensity: num(r.event_intensity) });

// Walk-forward k-NN: training = readings with a known outcome at time t (resolved_ts <= t) and complete features.
export function knnPredict(train, x) {
  const rows = train.filter((r) => FEATURES.every((f) => r.x[f] != null));
  if (rows.length < SV2F_LIMITS.minTrain || FEATURES.some((f) => x[f] == null)) return { status: 'insufficient_history', p_up: null, n_train: rows.length, k: null };
  const mean = {}, sd = {};
  for (const f of FEATURES) { const v = rows.map((r) => r.x[f]); mean[f] = v.reduce((a, b) => a + b, 0) / v.length;
    const s = Math.sqrt(v.reduce((a, b) => a + (b - mean[f]) ** 2, 0) / v.length); sd[f] = s > 1e-9 ? s : 1; }
  const z = (o) => FEATURES.map((f) => (o[f] - mean[f]) / sd[f]), zx = z(x);
  const d = rows.map((r) => ({ up: r.up, dist: Math.sqrt(z(r.x).reduce((a, v, i) => a + (v - zx[i]) ** 2, 0)) })).sort((a, b) => a.dist - b.dist);
  const k = Math.max(SV2F_LIMITS.kMin, Math.min(SV2F_LIMITS.kMax, Math.round(Math.sqrt(rows.length))));
  const nn = d.slice(0, k); let wu = 0, w = 0;
  for (const q of nn) { const wt = 1 / (q.dist + 0.1); w += wt; wu += wt * q.up; }
  return { status: 'ok', p_up: r4((wu + 1) / (w + 2)), n_train: rows.length, k };   // Laplace-smoothed weighted vote
}

// Builds one record per reading. readings/prices/v1 sorted ascending.
export function buildRecords(readings, prices, v1, h, nowMs) {
  const latestCalc = readings.length ? readings[readings.length - 1].calc_version : null;
  const rs = readings.filter((r) => r.calc_version === latestCalc);
  const base = rs.map((r) => ({ t: r.decision_ts, r, x: featuresOf(r), out: outcomeAt(prices, r.decision_ts, h, nowMs) }));
  const analogue = base.map((b) => analogueOf(b.r.predictive_json, h));
  // CPU bound: score the newest maxEval readings, each trained on at most the maxTrain most recent known outcomes.
  const startIdx = Math.max(0, base.length - SV2F_LIMITS.maxEval);
  const records = base.slice(startIdx).map((b, j) => { const i = startIdx + j;
    const train = base.filter((o) => o.t < b.t && o.out.status === 'resolved' && o.out.resolved_ts <= b.t).slice(-SV2F_LIMITS.maxTrain).map((o) => ({ x: o.x, up: o.out.realized_up }));
    const knn = knnPredict(train, b.x);
    const a = analogue[i];
    const vr = (() => {
      const cands = v1.filter((x) => x.ts <= b.t && x.avail_ts <= b.t && b.t - x.ts <= SV2F_LIMITS.v1MaxAgeMs);
      const scr = cands.filter((x) => x.src === 'scr'), hist = cands.filter((x) => x.src === 'history');
      const pick = scr.length ? scr[scr.length - 1] : hist.length ? hist[hist.length - 1] : null;
      return pick ? { score: num(pick.score), source: pick.src === 'scr' ? 'server_composite_readings' : 'history', reading_ts: pick.ts, direction: v1Direction(num(pick.score)) }
        : { score: null, source: null, reading_ts: null, direction: null };
    })();
    const score = num(b.r.sentiment_score);
    return {
      decision_ts: b.t, horizon_hours: h, calc_version: b.r.calc_version, status: b.r.status,
      features: { score, evidence_coverage: b.x.coverage, event_intensity: b.x.intensity, confidence: num(b.r.confidence) },
      v2_signal: { score, direction: v1Direction(score) },
      v2_analogue: a ? { p_up: a.p_up, direction: pDirection(a.p_up), n: a.n, validation: a.validation } : null,
      v2_knn: { ...knn, direction: pDirection(knn.p_up) },
      v1: vr,
      outcome: b.out,
    };
  });
  return { records, calc_version: latestCalc, excluded_older_calc: readings.length - rs.length, not_evaluated_older: startIdx };
}

function acc(records, dirOf) {
  let n = 0, k = 0;
  for (const x of records) { if (x.outcome.status !== 'resolved') continue; const d = dirOf(x); if (d !== 'UP' && d !== 'DOWN') continue; n++; if (d === x.outcome.direction) k++; }
  const ci = wilson(k, n);
  return { n, correct: k, rate: r4(n ? k / n : null), ci95: ci ? ci.map(r4) : null };
}
function brier(records, pOf) {
  let n = 0, s = 0;
  for (const x of records) { const p = pOf(x); if (x.outcome.status !== 'resolved' || p == null) continue; n++; s += (p - x.outcome.realized_up) ** 2; }
  return { n, score: r4(n ? s / n : null) };
}
const overlap = (a, b) => !a || !b || !(a[1] < b[0] || b[1] < a[0]);

export function summarizeSv2(records) {
  const dir = { v1: (x) => x.v1.direction, v2_signal: (x) => x.v2_signal.direction, v2_analogue: (x) => (x.v2_analogue ? x.v2_analogue.direction : null), v2_knn: (x) => x.v2_knn.direction };
  const accuracy = Object.fromEntries(Object.entries(dir).map(([k, f]) => [k, acc(records, f)]));
  // Head-to-head on the same resolved decisions where both called a direction.
  const pair = (a, b) => { const both = records.filter((x) => x.outcome.status === 'resolved' && ['UP', 'DOWN'].includes(dir[a](x)) && ['UP', 'DOWN'].includes(dir[b](x)));
    const A = acc(both, dir[a]), B = acc(both, dir[b]);
    let claim = 'insufficient_sample';
    if (both.length >= SV2F_LIMITS.minResolvedPairs) claim = both.length >= SV2F_LIMITS.claimMinN && !overlap(A.ci95, B.ci95) ? (A.rate > B.rate ? `${a}_higher_observational` : `${b}_higher_observational`) : 'no_supported_difference';
    return { n: both.length, [a]: A, [b]: B, claim }; };
  const analogueVals = records.filter((x) => x.v2_analogue).map((x) => x.v2_analogue.p_up);
  return {
    n_readings: records.length, n_resolved: records.filter((x) => x.outcome.status === 'resolved').length,
    n_pending: records.filter((x) => x.outcome.status === 'pending').length, n_no_price: records.filter((x) => x.outcome.status === 'no_price').length,
    accuracy,
    brier: { v2_analogue: brier(records, (x) => (x.v2_analogue ? x.v2_analogue.p_up : null)), v2_knn: brier(records, (x) => x.v2_knn.p_up) },
    v2_signal_bands: V1_BANDS.map((band) => { const d = (x) => v1Direction(x.v2_signal.score, band); const calls = records.filter((x) => ['UP', 'DOWN'].includes(d(x))).length;
      return { band: band.key, proposed: band.proposed, coverage: r4(records.length ? calls / records.length : null), accuracy: acc(records, d) }; }),
    head_to_head: { v2_signal_vs_v1: pair('v2_signal', 'v1'), v2_knn_vs_v1: pair('v2_knn', 'v1') },
    knn_status: { ok: records.filter((x) => x.v2_knn.status === 'ok').length, insufficient_history: records.filter((x) => x.v2_knn.status !== 'ok').length },
    analogue: { n: analogueVals.length, distinct_values: new Set(analogueVals).size, min: analogueVals.length ? Math.min(...analogueVals) : null, max: analogueVals.length ? Math.max(...analogueVals) : null },
  };
}

// The forecast for the latest reading, trained on every outcome known now.
export function currentForecast(records, readings, prices, h, nowMs) {
  const last = records[records.length - 1];
  if (!last) return null;
  const known = records.filter((x) => x.outcome.status === 'resolved' && x.outcome.resolved_ts <= nowMs).slice(-SV2F_LIMITS.maxTrain)
    .map((x) => ({ x: { score: x.features.score, coverage: x.features.evidence_coverage, intensity: x.features.event_intensity }, up: x.outcome.realized_up }));
  const knn = knnPredict(known, { score: last.features.score, coverage: last.features.evidence_coverage, intensity: last.features.event_intensity });
  return { decision_ts: last.decision_ts, target_ts: last.decision_ts + h * 3600 * 1000, score: last.features.score, v2_signal: last.v2_signal,
    v2_knn: { ...knn, direction: pDirection(knn.p_up) }, v2_analogue: last.v2_analogue, age_ms: nowMs - last.decision_ts };
}

export const SV2F_METHODOLOGY = {
  protocol: `${SV2F_PROTOCOL}: frozen 2026-11-21 before any outcome was examined. Claims need >= 30 resolved pairs to be reported and n >= 100 with non-overlapping Wilson 95% intervals to be called; observational, not production logic`,
  v2_signal: 'Sentiment V2 score as a direction with the PROPOSED convention used for V1 (> 55 UP, < 45 DOWN, else no call); bands 50/50 and 40/60 reported',
  v2_analogue: 'p_up stored in each V2 reading (predictive_json, family_profile_analogues, 24 h only, labelled candidate_unvalidated by its author). With n = 20 analogues its p_up moves in steps of 0.05',
  v2_knn: `${SV2_KNN_VERSION}: k-NN on V2 score, evidence coverage and event intensity (z-scored on the training set), k = clamp(round(sqrt n), 5, 15), inverse-distance weights, Laplace-smoothed p_up; walk-forward: trained only on readings whose outcome was known at the decision time; needs >= 20 such readings`,
  v1: 'V1 composite (server_composite_readings, else history) at or before the decision, at most 3 h old, proposed 45/55 direction',
  outcome: 'BTC price (btc_data) at the decision vs the first price at or after decision + horizon (within 6 h); used only once that price exists',
  caveats: 'BTC only (Sentiment V2 covers BTC). Readings about hourly with 12/24 h horizons overlap, so the effective sample is smaller than n. Only the current calc_version is used. CPU bound: the newest 500 readings are scored, each trained on at most the 500 most recent known outcomes',
};

// ---- route ----------------------------------------------------------------------------------------------------------
export async function handleSv2ForecastRoute(request, env, url, corsHeaders = {}, nowFn = Date.now) {
  if (url.pathname !== '/api/sv2-forecast') return null;
  const noStore = { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' };
  const json = (o, s = 200, h = JSON_HEADERS) => new Response(JSON.stringify(o), { status: s, headers: { ...corsHeaders, ...h } });
  if (request.method !== 'GET') return json({ ok: false, error: 'method not allowed (read-only route)' }, 405, noStore);
  const h = url.searchParams.get('horizon') === null ? 24 : Number(url.searchParams.get('horizon'));
  if (!HORIZONS.includes(h)) return json({ ok: false, error: 'unsupported horizon (supported: 12, 24)' }, 400, noStore);
  const edge = typeof caches !== 'undefined' && caches && caches.default ? caches.default : null;
  const cacheKey = edge ? new Request(`${url.origin}/api/sv2-forecast?horizon=${h}`, { method: 'GET' }) : null;
  try {
    if (edge) { const hit = await edge.match(cacheKey); if (hit) return new Response(hit.body, { headers: { ...corsHeaders, ...JSON_HEADERS, 'X-Sv2f-Cache': 'edge' } }); }
    if (!env || !env.DB) return json({ ok: false, error: 'sv2 forecast data unavailable' }, 503, noStore);
    const nowMs = nowFn();
    const rd = await env.DB.prepare(SV2_READINGS_SQL).bind(SV2F_LIMITS.maxReadings).all();
    const readings = (rd && rd.results) || [];
    const from = readings.length ? readings[0].decision_ts - SV2F_LIMITS.priceMaxAgeMs : nowMs;
    const pr = await env.DB.prepare(BTC_PRICES_SQL).bind(from, SV2F_LIMITS.maxPrices).all();
    const vr = await env.DB.prepare(V1_READINGS_SQL).bind(from, SV2F_LIMITS.maxPrices).all();
    const prices = ((pr && pr.results) || []).map((p) => ({ ts: p.ts, btc_price: num(p.btc_price) })).filter((p) => p.btc_price != null);
    const v1 = ((vr && vr.results) || []).map((x) => ({ src: x.src, ts: x.ts, avail_ts: x.avail_ts, score: x.score }));
    const { records, calc_version, excluded_older_calc, not_evaluated_older } = buildRecords(readings, prices, v1, h, nowMs);
    const rowsRead = [rd, pr, vr].reduce((s, r) => s + (Number(r?.meta?.rows_read) || 0), 0);
    const body = { ok: true, protocol: SV2F_PROTOCOL, model_version: SV2_KNN_VERSION, asset: 'BTC', horizon_hours: h, generated_ts: nowMs, calc_version,
      excluded_older_calc, not_evaluated_older, truncated: readings.length >= SV2F_LIMITS.maxReadings, d1_rows_read: rowsRead || null,
      current: currentForecast(records, readings, prices, h, nowMs), summary: summarizeSv2(records), records: records.slice().reverse(), methodology: SV2F_METHODOLOGY };
    const text = JSON.stringify(body);
    if (edge) { try { await edge.put(cacheKey, new Response(text, { headers: JSON_HEADERS })); } catch (_e) { /* best effort */ } }
    return new Response(text, { headers: { ...corsHeaders, ...JSON_HEADERS } });
  } catch (e) {
    const msg = String((e && e.message) || e);
    return json({ ok: false, error: 'sv2 forecast data unavailable', reason: /no such table/i.test(msg) ? 'missing_table' : /no such column/i.test(msg) ? 'schema_mismatch' : 'transient' }, 503, noStore);
  }
}
