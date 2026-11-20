// Read-only V1 / V2 outcome comparison (CryptoPulseV2 Phase 1 contract §7; owner decision OD-12).
//
//   GET /api/comparison/v1-v2?asset=BTC&horizon=24&period=24h|7d|30d|all&limit<=500   aligned records
//   GET /api/comparison/v1-v2/summary?asset=BTC&horizon=24&period=...                 metrics per slice
//
// READ-ONLY: one SELECT per uncached request, no write statement anywhere in this module (a static test enforces it),
// never called from the tick or cron paths, never fed back into V1 or V2. Results are cached 5 min (Cache API).
//
// Three series per V2 decision (key = asset, horizon, decision_ts = V2 prediction ts):
//   V1        server_composite_readings.composite (latest slot <= decision_ts, computed <= decision_ts, age <= 3 h),
//             else V1 history.score (latest ts <= decision_ts, age <= 3 h), else missing. Passed through as stored.
//   V2 base   the served headline before GLM (headlineOf semantics): glm_adjustments.baseline_p_up when the tick
//             recorded one; else the chosen non-original variant's cycle_p_up (else its chosen_p_up); else p_up.
//   V2 final  glm_adjustments.final_p_up for an active-mode row; otherwise the baseline with reason
//             'glm_out_of_scope' (not BTC 24 h) or 'no_adjustment'.
// Outcome: predictions.realized_up, used only once resolved_ts is set (the existing V2 resolution rule).
// V1 direction is a PROPOSED comparison convention (OD-12): > 55 UP, < 45 DOWN, else no call. Never production logic.

export const COMPARISON_VERSION = 'cmp-v1-v2-1';
export const COMPARISON_LIMITS = Object.freeze({ maxLimit: 500, defaultLimit: 200, v1MaxAgeMs: 3 * 3600 * 1000, cacheSeconds: 300, minResolvedPairs: 30, claimMinN: 100 });
export const PERIODS = Object.freeze({ '24h': 24 * 3600 * 1000, '7d': 7 * 86400 * 1000, '30d': 30 * 86400 * 1000, all: null });
export const V1_BANDS = Object.freeze([
  { key: '50/50', lo: 50, hi: 50, proposed: false },
  { key: '45/55', lo: 45, hi: 55, proposed: true },
  { key: '40/60', lo: 40, hi: 60, proposed: false },
]);
const PROPOSED_BAND = V1_BANDS.find((b) => b.proposed);
const SUPPORTED_ASSETS = ['BTC'];       // V2 `predictions` is the BTC ledger; LINK/ETH have no V1 counterpart reading
const SUPPORTED_HORIZONS = [12, 24];
const GLM_SCOPE = { asset: 'BTC', horizon: 24 };
const JSON_HEADERS = { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${COMPARISON_LIMITS.cacheSeconds}` };

// ---- parameters -------------------------------------------------------------------------------------------------
export function parseComparisonParams(searchParams) {
  const get = (k) => (searchParams.get(k) ?? '').trim();
  const asset = (get('asset') || 'BTC').toUpperCase();
  if (!SUPPORTED_ASSETS.includes(asset)) return { error: `unsupported asset (supported: ${SUPPORTED_ASSETS.join(', ')})` };
  const horizon = get('horizon') === '' ? 24 : Number(get('horizon'));
  if (!SUPPORTED_HORIZONS.includes(horizon)) return { error: `unsupported horizon (supported: ${SUPPORTED_HORIZONS.join(', ')})` };
  const period = (get('period') || 'all').toLowerCase();
  if (!Object.prototype.hasOwnProperty.call(PERIODS, period)) return { error: 'unsupported period (24h, 7d, 30d, all)' };
  let limit = COMPARISON_LIMITS.defaultLimit;
  if (get('limit') !== '') {
    limit = Number(get('limit'));
    if (!Number.isInteger(limit) || limit < 1) return { error: 'limit must be a positive integer' };
    limit = Math.min(limit, COMPARISON_LIMITS.maxLimit);
  }
  return { asset, horizon, period, limit };
}

// ---- the single bounded read ------------------------------------------------------------------------------------
// predictions via idx_predictions_horizon_ts (newest first, LIMIT n+1 for the truncation flag); selection_provenance,
// selection_decisions and glm_adjustments by indexed equality; V1 readings by correlated indexed range lookups.
// Measured on production 2026-11-20: 1,671 rows read for LIMIT 501 (budget 5,000).
export const COMPARISON_SQL = `WITH base AS (
  SELECT id, ts, target_ts, p_up, realized_up, resolved_ts, git_commit_sha, horizon_hours
  FROM predictions WHERE horizon_hours = ?1 AND ts >= ?2 AND ts <= ?3 ORDER BY ts DESC LIMIT ?4
), j AS (
  SELECT b.*, sp.chosen_variant, sp.cycle_p_up, sd.chosen_p_up,
    g.baseline_p_up AS g_baseline, g.baseline_source AS g_source, g.final_p_up AS g_final, g.applied AS g_applied,
    g.mode AS g_mode, g.reason AS g_reason,
    (SELECT s.slot_ts FROM server_composite_readings s WHERE s.slot_ts <= b.ts AND s.slot_ts >= b.ts - ?5
       AND s.computed_ts <= b.ts AND s.composite IS NOT NULL ORDER BY s.slot_ts DESC LIMIT 1) AS sc_slot,
    (SELECT h.id FROM history h WHERE h.ts <= b.ts AND h.ts >= b.ts - ?5 ORDER BY h.ts DESC LIMIT 1) AS h_id
  FROM base b
  LEFT JOIN selection_provenance sp ON sp.coin = 'BTC' AND sp.horizon_hours = b.horizon_hours AND sp.prediction_ts = b.ts
    AND sp.selection_table = 'selection_decisions'
  LEFT JOIN selection_decisions sd ON sd.id = sp.selection_row_id
  LEFT JOIN glm_adjustments g ON g.coin = 'BTC' AND g.horizon_hours = b.horizon_hours AND g.prediction_ts = b.ts
)
SELECT j.*, sc.composite AS sc_value, sc.computed_ts AS sc_computed_ts, sc.version AS sc_version,
  h.ts AS h_ts, h.score AS h_value
FROM j
LEFT JOIN server_composite_readings sc ON sc.slot_ts = j.sc_slot
LEFT JOIN history h ON h.id = j.h_id
ORDER BY j.ts DESC`;

export async function loadComparisonRows(db, { horizon, period, limit }, nowMs) {
  const span = PERIODS[period];
  const fromTs = span == null ? 0 : nowMs - span;
  const res = await db.prepare(COMPARISON_SQL)
    .bind(horizon, fromTs, nowMs, limit + 1, COMPARISON_LIMITS.v1MaxAgeMs).all();
  const rows = (res && res.results) || [];
  // A prediction can only have one selection_decisions provenance row today; keep the first if that ever changes.
  const seen = new Set(); const unique = [];
  for (const r of rows) { if (!seen.has(r.id)) { seen.add(r.id); unique.push(r); } }
  return { rows: unique.slice(0, limit), truncated: unique.length > limit, rows_read: Number(res?.meta?.rows_read ?? NaN) };
}

// ---- alignment (pure) -------------------------------------------------------------------------------------------
const num = (x) => (typeof x === 'number' && Number.isFinite(x) ? x : null);
export const v2Direction = (p) => (p == null ? null : p > 0.5 ? 'UP' : p < 0.5 ? 'DOWN' : 'NO_CALL');
export function v1Direction(score, band = PROPOSED_BAND) {
  if (score == null) return null;
  return score > band.hi ? 'UP' : score < band.lo ? 'DOWN' : 'NO_CALL';
}

export function alignRecord(r, { asset, horizon }, nowMs) {
  const decisionTs = r.ts;
  // V1: server composite first, history fallback; both already bounded to [decision - 3 h, decision] in SQL.
  // The guards below re-check it so a wrong row can never leak a later reading into an earlier decision.
  let v1 = { score: null, source: null, reading_ts: null, age_ms: null, version: null, direction: null, missing_reason: 'no_v1_reading_within_3h' };
  const scOk = num(r.sc_value) != null && r.sc_slot != null && r.sc_slot <= decisionTs && r.sc_computed_ts != null && r.sc_computed_ts <= decisionTs
    && decisionTs - r.sc_slot <= COMPARISON_LIMITS.v1MaxAgeMs;
  const hOk = num(r.h_value) != null && r.h_ts != null && r.h_ts <= decisionTs && decisionTs - r.h_ts <= COMPARISON_LIMITS.v1MaxAgeMs;
  if (scOk) v1 = { score: r.sc_value, source: 'server_composite_readings', reading_ts: r.sc_slot, age_ms: decisionTs - r.sc_slot, version: r.sc_version ?? null };
  else if (hOk) v1 = { score: r.h_value, source: 'history', reading_ts: r.h_ts, age_ms: decisionTs - r.h_ts, version: null };
  if (v1.score != null) { v1.direction = v1Direction(v1.score); v1.missing_reason = null; }

  // V2 baseline (headlineOf semantics).
  let base = { p_up: null, source: null, variant: null };
  if (num(r.g_baseline) != null) base = { p_up: r.g_baseline, source: `glm_adjustments.${r.g_source || 'baseline_p_up'}`, variant: r.chosen_variant || null };
  else if (r.chosen_variant && r.chosen_variant !== 'original' && num(r.cycle_p_up) != null) base = { p_up: r.cycle_p_up, source: 'cycle_p_up', variant: r.chosen_variant };
  else if (r.chosen_variant && r.chosen_variant !== 'original' && num(r.chosen_p_up) != null) base = { p_up: r.chosen_p_up, source: 'chosen_p_up', variant: r.chosen_variant };
  else base = { p_up: num(r.p_up), source: r.chosen_variant ? 'p_up' : 'p_up_no_selection_record', variant: r.chosen_variant || 'original' };
  base.direction = v2Direction(base.p_up);

  // V2 final.
  const inScope = asset === GLM_SCOPE.asset && horizon === GLM_SCOPE.horizon;
  let fin;
  if (inScope && r.g_mode === 'active' && num(r.g_final) != null) {
    const adj = num(r.g_applied) ?? 0;
    fin = { p_up: r.g_final, reason: adj !== 0 ? 'glm_applied' : (r.g_reason || 'no_adjustment'), glm_applied: adj !== 0, adjustment: adj };
  } else {
    fin = { p_up: base.p_up, reason: inScope ? 'no_adjustment' : 'glm_out_of_scope', glm_applied: false, adjustment: 0 };
  }
  fin.direction = v2Direction(fin.p_up);

  // Outcome: used only after resolution, and only when resolution happened by now.
  const resolved = r.resolved_ts != null && r.resolved_ts <= nowMs && (r.realized_up === 0 || r.realized_up === 1);
  const outcome = resolved ? { status: 'resolved', realized_up: r.realized_up, direction: r.realized_up === 1 ? 'UP' : 'DOWN', resolved_ts: r.resolved_ts }
    : { status: 'pending', realized_up: null, direction: null, resolved_ts: null };

  return { asset, horizon_hours: horizon, decision_ts: decisionTs, target_ts: r.target_ts, git_commit_sha: r.git_commit_sha ?? null,
    v1, v2_baseline: base, v2_final: fin, outcome };
}

// ---- metrics (pure) ---------------------------------------------------------------------------------------------
export function wilson(k, n, z = 1.959963984540054) {
  if (!n) return null;
  const p = k / n, z2 = z * z, den = 1 + z2 / n;
  const centre = (p + z2 / (2 * n)) / den;
  const half = (z * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n))) / den;
  return [Math.max(0, centre - half), Math.min(1, centre + half)];
}
const rate = (k, n) => (n ? k / n : null);
const r4 = (x) => (x == null ? null : Math.round(x * 1e4) / 1e4);

function accuracy(records, dirOf) {
  let n = 0, k = 0;
  for (const x of records) {
    if (x.outcome.status !== 'resolved') continue;
    const d = dirOf(x);
    if (d !== 'UP' && d !== 'DOWN') continue;
    n += 1; if (d === x.outcome.direction) k += 1;
  }
  const ci = wilson(k, n);
  return { n, correct: k, rate: r4(rate(k, n)), ci95: ci ? ci.map(r4) : null };
}
function agreement(records, a, b) {
  let n = 0, k = 0;
  for (const x of records) {
    const da = a(x), db = b(x);
    if ((da !== 'UP' && da !== 'DOWN') || (db !== 'UP' && db !== 'DOWN')) continue;
    n += 1; if (da === db) k += 1;
  }
  return { n, rate: r4(rate(k, n)) };
}
function brier(records, pOf) {
  let n = 0, s = 0;
  for (const x of records) {
    const p = pOf(x);
    if (x.outcome.status !== 'resolved' || p == null) continue;
    n += 1; s += (p - x.outcome.realized_up) ** 2;
  }
  return { n, score: r4(n ? s / n : null) };
}
const overlap = (a, b) => !a || !b || !(a[1] < b[0] || b[1] < a[0]);

export function summarize(records) {
  const n = records.length;
  const withV1 = records.filter((x) => x.v1.score != null);
  const resolvedPairs = withV1.filter((x) => x.outcome.status === 'resolved').length;
  const v1 = (x) => x.v1.direction, v2b = (x) => x.v2_baseline.direction, v2f = (x) => x.v2_final.direction;
  const bands = V1_BANDS.map((band) => {
    const dir = (x) => v1Direction(x.v1.score, band);
    const calls = withV1.filter((x) => dir(x) === 'UP' || dir(x) === 'DOWN').length;
    return { band: band.key, proposed: band.proposed, coverage: r4(rate(calls, withV1.length)), no_call_rate: r4(rate(withV1.length - calls, withV1.length)), accuracy: accuracy(withV1, dir) };
  });
  const acc = { v1: accuracy(withV1, v1), v2_baseline: accuracy(records, v2b), v2_final: accuracy(records, v2f) };
  const glmRows = records.filter((x) => x.v2_final.reason !== 'glm_out_of_scope');
  const adjusted = records.filter((x) => x.v2_final.glm_applied);
  const insufficient = resolvedPairs < COMPARISON_LIMITS.minResolvedPairs;
  let claim = 'insufficient_sample';
  if (!insufficient) {
    const a = acc.v1, b = acc.v2_final;
    if (a.n >= COMPARISON_LIMITS.claimMinN && b.n >= COMPARISON_LIMITS.claimMinN && !overlap(a.ci95, b.ci95)) claim = a.rate > b.rate ? 'v1_higher_observational' : 'v2_higher_observational';
    else claim = 'no_supported_difference';
  }
  return {
    status: insufficient ? 'insufficient_sample' : 'ok',
    n_aligned: n,
    n_resolved: records.filter((x) => x.outcome.status === 'resolved').length,
    n_resolved_pairs: resolvedPairs,
    v1_coverage: r4(rate(withV1.length, n)),
    v1_sources: { server_composite_readings: withV1.filter((x) => x.v1.source === 'server_composite_readings').length, history: withV1.filter((x) => x.v1.source === 'history').length, missing: n - withV1.length },
    no_call_rate: { v1: r4(rate(withV1.filter((x) => v1(x) === 'NO_CALL').length, withV1.length)), v2_baseline: r4(rate(records.filter((x) => v2b(x) === 'NO_CALL').length, n)), v2_final: r4(rate(records.filter((x) => v2f(x) === 'NO_CALL').length, n)) },
    agreement: { v1_v2_baseline: agreement(records, v1, v2b), v1_v2_final: agreement(records, v1, v2f), v2_baseline_v2_final: agreement(records, v2b, v2f) },
    accuracy: acc,
    brier: { v2_baseline: brier(records, (x) => x.v2_baseline.p_up), v2_final: brier(records, (x) => x.v2_final.p_up), v1: null },
    v1_bands: bands,
    glm: { rows_in_scope: glmRows.length, adjusted: adjusted.length, changed_calls: records.filter((x) => x.v2_baseline.direction !== x.v2_final.direction).length,
      mean_abs_adjustment: r4(adjusted.length ? adjusted.reduce((s, x) => s + Math.abs(x.v2_final.adjustment), 0) / adjusted.length : null) },
    claim,
  };
}

// ---- route ------------------------------------------------------------------------------------------------------
const METHODOLOGY = {
  v1: 'server_composite_readings.composite, else history.score; latest reading at or before the decision, at most 3 h old, else missing',
  v1_direction: 'PROPOSED comparison convention (OD-12): score > 55 UP, < 45 DOWN, 45-55 no call; sensitivity bands 50/50, 45/55, 40/60. Not production logic',
  v2_baseline: 'served headline before GLM (glm_adjustments.baseline_p_up, else chosen variant cycle_p_up, else p_up)',
  v2_final: 'glm_adjustments.final_p_up (active mode), else baseline with reason glm_out_of_scope / no_adjustment',
  outcome: 'predictions.realized_up after resolved_ts (existing V2 resolution rule); unresolved stays pending',
  claims: 'below 30 resolved aligned pairs: insufficient_sample; a difference is reported only with n >= 100 and non-overlapping Wilson 95% intervals, and is observational, not pre-registered',
};

export async function handleComparisonRoutes(request, env, url, corsHeaders = {}, nowFn = Date.now) {
  const p = url.pathname;
  if (p !== '/api/comparison/v1-v2' && p !== '/api/comparison/v1-v2/summary') return null;
  const json = (obj, status = 200, headers = JSON_HEADERS) => new Response(JSON.stringify(obj), { status, headers: { ...corsHeaders, ...headers } });
  const noStore = { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' };
  if (request.method !== 'GET') return json({ ok: false, error: 'method not allowed (read-only route)' }, 405, noStore);
  const params = parseComparisonParams(url.searchParams);
  if (params.error) return json({ ok: false, error: params.error }, 400, noStore);
  const summary = p.endsWith('/summary');
  if (summary) params.limit = COMPARISON_LIMITS.maxLimit;
  const key = `${url.origin}${p}?asset=${params.asset}&horizon=${params.horizon}&period=${params.period}${summary ? '' : `&limit=${params.limit}`}`;
  const edge = typeof caches !== 'undefined' && caches && caches.default ? caches.default : null;
  const cacheKey = edge ? new Request(key, { method: 'GET' }) : null;
  try {
    if (edge) { const hit = await edge.match(cacheKey); if (hit) return new Response(hit.body, { headers: { ...corsHeaders, ...JSON_HEADERS, 'X-Comparison-Cache': 'edge' } }); }
    if (!env || !env.DB) return json({ ok: false, error: 'comparison data unavailable' }, 503, noStore);
    const nowMs = nowFn();
    const { rows, truncated, rows_read } = await loadComparisonRows(env.DB, params, nowMs);
    const records = rows.map((r) => alignRecord(r, params, nowMs));
    const head = { ok: true, version: COMPARISON_VERSION, asset: params.asset, horizon_hours: params.horizon, period: params.period,
      generated_ts: nowMs, truncated, limit: params.limit, d1_rows_read: Number.isFinite(rows_read) ? rows_read : null, methodology: METHODOLOGY };
    const body = summary ? { ...head, summary: summarize(records) } : { ...head, count: records.length, records };
    const text = JSON.stringify(body);
    if (edge) { try { await edge.put(cacheKey, new Response(text, { headers: JSON_HEADERS })); } catch (_e) { /* best effort */ } }
    return new Response(text, { headers: { ...corsHeaders, ...JSON_HEADERS } });
  } catch (e) {
    const msg = String((e && e.message) || e);
    const reason = /no such table/i.test(msg) ? 'missing_table' : /no such column/i.test(msg) ? 'schema_mismatch' : 'transient';
    return json({ ok: false, error: 'comparison data unavailable', reason }, 503, noStore);
  }
}
