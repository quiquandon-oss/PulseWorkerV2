// Sentiment V2 forecast family — RESEARCH STAGE (owner change request 2026-11-21, decisions of 2026-11-21).
//
// Computes what the V2 Sentiment score WOULD be if the approved independent prediction models, TimesFM 2.5, Chronos-2
// and a bounded GLM adjustment entered the existing calculation. It never changes the production sentiment_score; the
// result is stored beside it (family_json.forecast_research) for audit and comparison.
//
// Owner-approved rules:
//   - One new family `model_forecasts` joins the existing equal-weight family average (rule H1). Its score is the mean of
//     the available source readings (the existing family rule: mean of independent readings).
//   - Approved sources: production `original` (p_up) and `calibrated` (calibrated_p_up) BTC 24 h models, TimesFM 2.5 and
//     Chronos-2 BTC 24 h forecasts. No winner selection: every available source contributes, each is reported.
//   - Each source's reading comes from its OWN measured directional accuracy (per model, asset, horizon and model
//     version; resolved outcomes only, resolved at or before the decision time): UP = adjusted accuracy x 100,
//     DOWN = 100 - adjusted accuracy x 100, a neutral call = 50.
//   - Shrinkage towards 50 with a Beta(k, k) prior: adjusted = (hits + k) / (n + 2k). Smooth in n (no step at 30);
//     with little evidence the reading stays near 50. k = SV2F_PRIOR_K is a PROPOSED parameter (documented, not validated).
//   - GLM: a bounded adjustment like events, at most +/- SV2F_GLM_MAX_POINTS points x strength (1/3, 2/3, 1), only from an
//     ELIGIBLE signal valid at the decision time.
// Pure functions only (no I/O); worker.js loads the point-in-time rows and calls sv2ForecastResearch.

export const SV2F_VERSION = 'sv2f-research-0.1';
export const SV2F_PRIOR_K = 10;                 // PROPOSED: equivalent to 20 prior pseudo-forecasts split 50/50
export const SV2F_EVAL_WINDOW_MS = 90 * 86400000; // only outcomes resolved in the 90 days before the decision
export const SV2F_FRESH_PREDICTION_MS = 6 * 3600000;   // production models run every 3 h
export const SV2F_STALE_FORECAST_MS = 36 * 3600000;    // TimesFM daily / Chronos twice daily
export const SV2F_GLM_MAX_POINTS = 5;
export const SV2F_STRENGTH_K = Object.freeze({ weak: 1 / 3, moderate: 2 / 3, strong: 1 });
export const SV2F_SOURCES = Object.freeze(['model_original', 'model_calibrated', 'forecast_timesfm', 'forecast_chronos']);

const r2 = (x) => Math.round(x * 100) / 100;
const r4 = (x) => Math.round(x * 1e4) / 1e4;

// Directional reading from measured accuracy. dir: 'UP' | 'DOWN' | 'NEUTRAL'.
export function sv2fReading(dir, hits, n, k = SV2F_PRIOR_K) {
  const nn = Math.max(0, Number(n) || 0), hh = Math.max(0, Math.min(nn, Number(hits) || 0));
  const raw = nn > 0 ? hh / nn : null;
  const adjusted = (hh + k) / (nn + 2 * k);
  const reading = dir === 'UP' ? 100 * adjusted : dir === 'DOWN' ? 100 - 100 * adjusted : 50;
  return { direction: dir, resolved_n: nn, hits: hh, raw_hit_rate: raw === null ? null : r4(raw), adjusted_hit_rate: r4(adjusted), reading: r2(reading), prior_k: k };
}

const dirOfP = (p) => (p > 0.5 ? 'UP' : p < 0.5 ? 'DOWN' : 'NEUTRAL');

// Production model (original / calibrated). rows: BTC 24 h predictions {ts, p_up, calibrated_p_up, model_version,
// realized_up, resolved_ts}, any order. Only rows made at or before t; accuracy from rows resolved at or before t.
export function sv2fModelSource(rows, t, field) {
  const usable = (rows || []).filter((r) => Number(r.ts) <= t && r[field] !== null && r[field] !== undefined && r[field] !== '' && Number.isFinite(Number(r[field])));
  const latest = usable.reduce((a, r) => (!a || Number(r.ts) > Number(a.ts) ? r : a), null);
  if (!latest) return { state: 'missing', reason: 'no_prediction_at_or_before_decision' };
  if (t - Number(latest.ts) > SV2F_FRESH_PREDICTION_MS) return { state: 'stale', reason: 'latest_prediction_older_than_6h', prediction_ts: Number(latest.ts) };
  const version = latest.model_version ?? null;
  let n = 0, hits = 0;
  for (const r of usable) {
    if ((r.model_version ?? null) !== version) continue;
    if (r.realized_up !== 0 && r.realized_up !== 1) continue;
    const rt = Number(r.resolved_ts);
    if (!Number.isFinite(rt) || rt > t || rt < t - SV2F_EVAL_WINDOW_MS) continue;
    const d = dirOfP(Number(r[field]));
    if (d === 'NEUTRAL') continue;
    n += 1; if ((d === 'UP' ? 1 : 0) === Number(r.realized_up)) hits += 1;
  }
  const p = Number(latest[field]);
  return { state: 'fresh', model_version: version, prediction_ts: Number(latest.ts), p_up: r4(p), ...sv2fReading(dirOfP(p), hits, n) };
}

// TimesFM / Chronos. rows: {id, issued_ts, target_ts, direction, model_version, correct (1/0/true/false/null), resolved_ts}.
export function sv2fForecastSource(rows, t) {
  const usable = (rows || []).filter((r) => r.issued_ts !== null && r.issued_ts !== undefined && Number.isFinite(Number(r.issued_ts)) && Number(r.issued_ts) <= t);
  const live = usable.filter((r) => Number(r.target_ts) > t);
  const latest = live.reduce((a, r) => (!a || Number(r.issued_ts) > Number(a.issued_ts) ? r : a), null);
  if (!latest) return { state: 'missing', reason: 'no_live_forecast_at_decision' };
  if (t - Number(latest.issued_ts) > SV2F_STALE_FORECAST_MS) return { state: 'stale', reason: 'forecast_older_than_36h', forecast_id: String(latest.id) };
  const dir = String(latest.direction || '').toUpperCase();
  if (dir !== 'UP' && dir !== 'DOWN') return { state: 'invalid', reason: 'direction_missing', forecast_id: String(latest.id) };
  const version = latest.model_version ?? null;
  let n = 0, hits = 0;
  for (const r of usable) {
    if ((r.model_version ?? null) !== version) continue;
    const c = r.correct === true || r.correct === 1 ? 1 : r.correct === false || r.correct === 0 ? 0 : null;
    if (c === null) continue;
    const rt = Number(r.resolved_ts);
    if (!Number.isFinite(rt) || rt > t || rt < t - SV2F_EVAL_WINDOW_MS) continue;
    n += 1; hits += c;
  }
  return { state: 'fresh', model_version: version, forecast_id: String(latest.id), issued_ts: Number(latest.issued_ts), target_ts: Number(latest.target_ts), ...sv2fReading(dir, hits, n) };
}

// GLM: latest BTC 24 h signal with decision_cutoff <= t; contributes only if ELIGIBLE, directional and valid at t.
export function sv2fGlm(signals, t) {
  const usable = (signals || []).filter((s) => Number(s.decision_cutoff_ts) <= t);
  const s = usable.reduce((a, x) => (!a || Number(x.decision_cutoff_ts) > Number(a.decision_cutoff_ts) ? x : a), null);
  if (!s) return { state: 'missing', reason: 'no_glm_signal_at_or_before_decision', adjustment: 0 };
  const base = { signal_id: s.signal_id, eligibility: s.eligibility, direction: s.direction, strength: s.strength ?? null, decision_cutoff_ts: Number(s.decision_cutoff_ts) };
  if (!(t >= Number(s.valid_from_ts) && t < Number(s.valid_until_ts))) return { state: 'expired', ...base, adjustment: 0 };
  if (s.direction !== 'UP' && s.direction !== 'DOWN') return { state: 'no_signal', ...base, adjustment: 0 };
  if (s.eligibility !== 'ELIGIBLE') return { state: 'not_eligible', ...base, adjustment: 0 };
  const k = SV2F_STRENGTH_K[s.strength] ?? 0;
  return { state: 'applied', ...base, adjustment: r2((s.direction === 'UP' ? 1 : -1) * k * SV2F_GLM_MAX_POINTS) };
}

// Baseline result (sv2Compute output) + sources -> research-stage enhanced score. Never mutates `result`.
export function sv2ForecastResearch(result, sources, glm) {
  const out = { version: SV2F_VERSION, baseline_score: result.sentiment_score ?? null, sources, glm };
  if (result.sentiment_score === null || result.sentiment_score === undefined) return { ...out, status: 'baseline_unavailable', enhanced_score: null };
  const fresh = Object.entries(sources).filter(([, s]) => s.state === 'fresh');
  const famScores = Object.values(result.families || {}).map((f) => f.score).filter((x) => x !== null && x !== undefined);
  const missing = Object.entries(sources).filter(([, s]) => s.state !== 'fresh').map(([k, s]) => `${k}:${s.state}${s.reason ? ':' + s.reason : ''}`);
  const evAdj = Number(result.events?.adjustment) || 0;
  // The baseline composite (before events) is reused exactly, so with no forecast input the enhanced score equals the
  // baseline score (plus a GLM adjustment only when one applies).
  const nBase = famScores.length;
  const baseComposite = Number.isFinite(Number(result.composite_before_events)) ? Number(result.composite_before_events)
    : famScores.reduce((a, x) => a + x, 0) / Math.max(1, nBase);
  let familyScore = null, composite = baseComposite;
  const contributions = {};
  if (fresh.length) {
    familyScore = fresh.reduce((a, [, s]) => a + s.reading, 0) / fresh.length;
    const nFam = nBase + 1;
    composite = (baseComposite * nBase + familyScore) / nFam;
    // Contribution of each source to the final score, in points: its share of the forecast family, which has weight 1/nFam.
    for (const [k, s] of fresh) contributions[k] = r2((s.reading - 50) / fresh.length / nFam);
  }
  const glmAdj = glm && glm.state === 'applied' ? glm.adjustment : 0;
  const enhanced = fresh.length || glmAdj ? Math.max(0, Math.min(100, composite + evAdj + glmAdj)) : Number(result.sentiment_score);
  return { ...out, status: fresh.length || glmAdj ? 'ok' : 'no_forecast_inputs', enhanced_score: r2(enhanced), delta: r2(enhanced - result.sentiment_score),
    family_score: familyScore === null ? null : r2(familyScore), n_families_with_forecast: famScores.length + (fresh.length ? 1 : 0),
    contributions: { ...contributions, glm: glmAdj }, missing,
    agreement: { up: fresh.filter(([, s]) => s.direction === 'UP').map(([k]) => k), down: fresh.filter(([, s]) => s.direction === 'DOWN').map(([k]) => k),
      neutral: fresh.filter(([, s]) => s.direction === 'NEUTRAL').map(([k]) => k) } };
}
