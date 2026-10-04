// Learning loop -- V1 methodology, source adjustments, recalculation and validation. Pure: no D1, no clock.
//
// Three V1 numbers are kept strictly apart and never compared across formulas:
//   STORED        the score V1 actually wrote at the time (history/archive `score`)
//   RECONSTRUCTED the base methodology version applied to the stored per-source readings
//   PROPOSED      the same readings through the base version + the proposed adjustment
// Impact and validation always compare PROPOSED with RECONSTRUCTED (same formula, same readings). STORED is shown
// for reference only, together with how far it is from RECONSTRUCTED (V1's writer applied unpersisted browser-local
// weight overrides and a temporary x1.5 Foufi driver boost, so STORED is not always reproducible).
import { V1_METHODOLOGY_V1, SOURCE_GROUP_LABELS, nearestAtOrBefore } from './learning-core.js';

export const FORMULA_ID = 'v1-weighted-mean@1';
export const BASELINE_VERSION_ID = 'v1.0';
const HOUR = 3600000;

export function baselineConfig() {
  return {
    formula: FORMULA_ID,
    based_on: V1_METHODOLOGY_V1.version,
    sources: V1_METHODOLOGY_V1.sources.map((s) => ({ id: s.id, label: s.label, group: s.group, weight: s.weight, confidence: s.confidence, invert: false, regime: null })),
    signals: [],
  };
}

export const CANDIDATE_TYPES = Object.freeze(['NEW_SOURCE', 'NEW_TREND', 'NEW_SIGNAL', 'SOURCE_RECLASSIFICATION', 'SOURCE_WEIGHT_ADJUSTMENT', 'REGIME_SPECIFIC_SIGNAL']);
export const CANDIDATE_STATUSES = Object.freeze(['DRAFT', 'PENDING_REVIEW', 'ACCEPTED', 'REJECTED', 'NEEDS_MORE_RESEARCH']);
export const ADJUSTMENT_TYPES = Object.freeze(['ADD_SOURCE', 'REMOVE_SOURCE', 'CHANGE_WEIGHT', 'CHANGE_CLASSIFICATION', 'ADD_SIGNAL', 'ADD_REGIME_CONDITION']);
export const DEFAULT_ADJUSTMENT_FOR_TYPE = Object.freeze({
  NEW_SOURCE: 'ADD_SOURCE', NEW_TREND: 'ADD_SIGNAL', NEW_SIGNAL: 'ADD_SIGNAL', SOURCE_RECLASSIFICATION: 'CHANGE_CLASSIFICATION',
  SOURCE_WEIGHT_ADJUSTMENT: 'CHANGE_WEIGHT', REGIME_SPECIFIC_SIGNAL: 'ADD_REGIME_CONDITION',
});
export const SIGNAL_TRANSFORMS = Object.freeze({ MOMENTUM_24H: 'momentum_24h', LEVEL: 'level' });
export const REGIME_METRICS = Object.freeze({ BTC_24H_CHANGE_PCT: 'btc_24h_change_pct' });

// New sources start at low weight/confidence -- the same discipline V1 applied to its own latest additions
// (STRC weight 5 / confidence 0.35, Foufi 6 / 0.4).
export const NEW_SOURCE_DEFAULTS = Object.freeze({ weight: 5, confidence: 0.4 });

const DRIVER_TO_GROUP = { OPTIONS: 'DERIVATIVES', LIQUIDATIONS: 'DERIVATIVES', STABLECOIN_FLOWS: 'FLOWS', WHALES_MINERS: 'ONCHAIN', SPECIFIC_CATALYST: 'NEWS', OTHER: 'NEWS' };

export function slugId(text) {
  const s = String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40);
  return s || 'new_source';
}

function num(v, lo, hi) { const n = Number(v); return Number.isFinite(n) && n >= lo && n <= hi ? n : null; }
function clean(v, max = 200) { return typeof v === 'string' ? v.trim().slice(0, max) : ''; }

// A finding -> the default candidate + adjustment, editable by the human before review.
export function candidateFromFinding(finding, config = baselineConfig()) {
  const f = finding || {};
  const covered = config.sources.find((s) => s.id === f.covered_by_existing_v1_source);
  let candidateType;
  if (f.finding_type === 'NEW_SIGNAL') candidateType = 'NEW_SIGNAL';
  else if (f.finding_type === 'NEW_TREND') candidateType = 'NEW_TREND';
  else if ((f.finding_type === 'EXISTING_SOURCE_MISREAD' || f.finding_type === 'SOURCE_WEIGHTING') && covered) candidateType = 'SOURCE_WEIGHT_ADJUSTMENT';
  else if (f.finding_type === 'SOURCE_CLASSIFICATION' && covered) candidateType = 'SOURCE_RECLASSIFICATION';
  else if (f.finding_type === 'REGIME_SPECIFIC' && covered) candidateType = 'REGIME_SPECIFIC_SIGNAL';
  else candidateType = 'NEW_SOURCE';
  const src = f.proposed_new_source || {};
  let adjustment;
  if (candidateType === 'SOURCE_WEIGHT_ADJUSTMENT') {
    adjustment = { type: 'CHANGE_WEIGHT', source_id: covered.id, weight: covered.weight, confidence: covered.confidence };
  } else if (candidateType === 'SOURCE_RECLASSIFICATION') {
    adjustment = { type: 'CHANGE_CLASSIFICATION', source_id: covered.id, group: covered.group, invert: false };
  } else if (candidateType === 'REGIME_SPECIFIC_SIGNAL') {
    adjustment = { type: 'ADD_REGIME_CONDITION', source_id: covered.id, metric: REGIME_METRICS.BTC_24H_CHANGE_PCT, op: '<=', value: -2, multiplier: 2 };
  } else if (candidateType === 'NEW_SOURCE') {
    adjustment = {
      type: 'ADD_SOURCE', source_id: slugId(src.name || f.primary_driver), label: clean(src.name || f.primary_driver, 120) || 'New source',
      group: DRIVER_TO_GROUP[f.driver_category] || (SOURCE_GROUP_LABELS[f.driver_category] ? f.driver_category : 'NEWS'),
      weight: NEW_SOURCE_DEFAULTS.weight, confidence: NEW_SOURCE_DEFAULTS.confidence, url: src.url || '', what_it_measures: clean(src.what_it_measures, 300),
    };
  } else {
    adjustment = {
      type: 'ADD_SIGNAL', signal_id: slugId(f.proposed_signal || f.primary_driver), label: clean(f.primary_driver || f.proposed_signal, 120) || 'New signal',
      derived_from: covered ? covered.id : '', transform: covered ? SIGNAL_TRANSFORMS.MOMENTUM_24H : '', group: covered ? covered.group : 'NEWS',
      weight: NEW_SOURCE_DEFAULTS.weight, confidence: NEW_SOURCE_DEFAULTS.confidence, description: clean(f.proposed_signal, 300),
    };
  }
  return {
    candidate_type: candidateType,
    title: clean(f.primary_driver, 140) || 'Learning candidate',
    reason: clean(f.explanation, 2000),
    expected_effect: candidateType === 'NEW_SOURCE'
      ? `Give V1 a reading on "${adjustment.label}", so moves driven by it are no longer missed.`
      : candidateType === 'SOURCE_WEIGHT_ADJUSTMENT' ? `Change how much "${covered.label}" counts in V1.`
        : candidateType === 'SOURCE_RECLASSIFICATION' ? `Reclassify how V1 reads "${covered.label}".`
          : candidateType === 'REGIME_SPECIFIC_SIGNAL' ? `Make "${covered.label}" count more in a specific market regime.` : `Add "${adjustment.label}" as a V1 signal.`,
    confidence: f.confidence || 'LOW',
    evidence: Array.isArray(f.evidence) ? f.evidence.slice(0, 20) : [],
    adjustment,
  };
}

// Validates and normalizes an adjustment against a base config. Unknown fields are dropped.
export function normalizeAdjustment(adj, config = baselineConfig()) {
  const errors = [];
  const a = adj && typeof adj === 'object' ? adj : {};
  const type = ADJUSTMENT_TYPES.includes(a.type) ? a.type : null;
  if (!type) return { ok: false, errors: ['Choose what to change.'], adjustment: null };
  const has = (id) => config.sources.some((s) => s.id === id);
  const out = { type };
  if (type === 'ADD_SOURCE') {
    out.source_id = slugId(a.source_id || a.label);
    out.label = clean(a.label, 120) || out.source_id;
    out.group = SOURCE_GROUP_LABELS[a.group] ? a.group : 'NEWS';
    out.weight = num(a.weight, 0.1, 50); out.confidence = num(a.confidence, 0.05, 1);
    out.url = /^https?:\/\//i.test(a.url || '') ? clean(a.url, 500) : '';
    out.what_it_measures = clean(a.what_it_measures, 300);
    if (has(out.source_id)) errors.push(`"${out.source_id}" is already a V1 source; change its weight instead.`);
  } else if (type === 'REMOVE_SOURCE') {
    out.source_id = clean(a.source_id, 40);
    if (!has(out.source_id)) errors.push('Pick an existing V1 source to remove.');
  } else if (type === 'CHANGE_WEIGHT') {
    out.source_id = clean(a.source_id, 40);
    out.weight = num(a.weight, 0, 50); out.confidence = num(a.confidence, 0.05, 1);
    if (!has(out.source_id)) errors.push('Pick an existing V1 source.');
  } else if (type === 'CHANGE_CLASSIFICATION') {
    out.source_id = clean(a.source_id, 40);
    out.group = SOURCE_GROUP_LABELS[a.group] ? a.group : null;
    out.invert = a.invert === true;
    if (!has(out.source_id)) errors.push('Pick an existing V1 source.');
    if (!out.group) errors.push('Pick the new classification.');
  } else if (type === 'ADD_SIGNAL') {
    out.signal_id = slugId(a.signal_id || a.label);
    out.label = clean(a.label, 120) || out.signal_id;
    out.derived_from = has(a.derived_from) ? a.derived_from : '';
    out.transform = Object.values(SIGNAL_TRANSFORMS).includes(a.transform) ? a.transform : (out.derived_from ? SIGNAL_TRANSFORMS.MOMENTUM_24H : '');
    out.group = SOURCE_GROUP_LABELS[a.group] ? a.group : 'NEWS';
    out.weight = num(a.weight, 0.1, 50); out.confidence = num(a.confidence, 0.05, 1);
    out.description = clean(a.description, 300);
  } else if (type === 'ADD_REGIME_CONDITION') {
    out.source_id = clean(a.source_id, 40);
    out.metric = REGIME_METRICS.BTC_24H_CHANGE_PCT;
    out.op = a.op === '<=' ? '<=' : '>=';
    out.value = num(a.value, -50, 50);
    out.multiplier = num(a.multiplier, 0, 5);
    if (!has(out.source_id)) errors.push('Pick the V1 source the condition applies to.');
    if (out.value === null) errors.push('Give the BTC 24h move threshold (in %).');
    if (out.multiplier === null) errors.push('Give the weight multiplier (0-5).');
  }
  for (const k of ['weight', 'confidence']) if (k in out && out[k] === null) errors.push(`${k} is out of range.`);
  return { ok: errors.length === 0, errors, adjustment: out };
}

export function applyAdjustment(config, adjustment) {
  const c = JSON.parse(JSON.stringify(config));
  const a = adjustment;
  const src = (id) => c.sources.find((s) => s.id === id);
  switch (a.type) {
    case 'ADD_SOURCE': c.sources.push({ id: a.source_id, label: a.label, group: a.group, weight: a.weight, confidence: a.confidence, invert: false, regime: null, url: a.url || '' }); break;
    case 'REMOVE_SOURCE': c.sources = c.sources.filter((s) => s.id !== a.source_id); break;
    case 'CHANGE_WEIGHT': Object.assign(src(a.source_id), { weight: a.weight, confidence: a.confidence }); break;
    case 'CHANGE_CLASSIFICATION': Object.assign(src(a.source_id), { group: a.group, invert: a.invert }); break;
    case 'ADD_SIGNAL': c.signals.push({ id: a.signal_id, label: a.label, group: a.group, derived_from: a.derived_from, transform: a.transform, weight: a.weight, confidence: a.confidence }); break;
    case 'ADD_REGIME_CONDITION': src(a.source_id).regime = { metric: a.metric, op: a.op, value: a.value, multiplier: a.multiplier }; break;
    default: throw new Error('unknown adjustment');
  }
  return c;
}

// Plain-language description of an adjustment ("what V1 would change").
export function describeAdjustment(a, config = baselineConfig()) {
  const s = config.sources.find((x) => x.id === a.source_id);
  const name = s ? s.label : a.source_id;
  switch (a.type) {
    case 'ADD_SOURCE': return `Add a new source "${a.label}" (${SOURCE_GROUP_LABELS[a.group]}) with weight ${a.weight} and confidence ${a.confidence}.`;
    case 'REMOVE_SOURCE': return `Remove "${name}" from V1.`;
    case 'CHANGE_WEIGHT': return `Change "${name}" from weight ${s ? s.weight : '?'} / confidence ${s ? s.confidence : '?'} to weight ${a.weight} / confidence ${a.confidence}.`;
    case 'CHANGE_CLASSIFICATION': return `Reclassify "${name}" as ${SOURCE_GROUP_LABELS[a.group]}${a.invert ? ' and read it inverted (high = bearish)' : ''}.`;
    case 'ADD_SIGNAL': return a.derived_from
      ? `Add signal "${a.label}": the 24h change of "${(config.sources.find((x) => x.id === a.derived_from) || {}).label || a.derived_from}", weight ${a.weight}, confidence ${a.confidence}.`
      : `Add signal "${a.label}" (needs its own data feed), weight ${a.weight}, confidence ${a.confidence}.`;
    case 'ADD_REGIME_CONDITION': return `Multiply "${name}"'s weight by ${a.multiplier} whenever BTC's 24h move is ${a.op} ${a.value}%.`;
    default: return '';
  }
}

// ---- Scoring ----
// ctx: { readings, prevReadings, btc24hPct }. Returns null when nothing resolved.
export function scoreWithConfig(config, ctx) {
  let total = 0, acc = 0, used = 0;
  const add = (value, weight) => { if (typeof value === 'number' && Number.isFinite(value) && weight > 0) { total += weight; acc += value * weight; used++; } };
  for (const s of config.sources) {
    const raw = ctx.readings ? ctx.readings[s.id] : undefined;
    if (typeof raw !== 'number') continue;
    let w = s.weight * (s.confidence ?? 1);
    if (s.regime && s.regime.metric === REGIME_METRICS.BTC_24H_CHANGE_PCT && typeof ctx.btc24hPct === 'number') {
      const hit = s.regime.op === '>=' ? ctx.btc24hPct >= s.regime.value : ctx.btc24hPct <= s.regime.value;
      if (hit) w *= s.regime.multiplier;
    }
    add(s.invert ? 100 - raw : raw, w);
  }
  for (const g of config.signals || []) {
    const v = signalValue(g, ctx);
    add(v, g.weight * (g.confidence ?? 1));
  }
  return used ? { score: Math.round(acc / total), exact: acc / total, used } : null;
}

function signalValue(g, ctx) {
  if (!g.derived_from) return undefined;
  const now = ctx.readings ? ctx.readings[g.derived_from] : undefined;
  if (typeof now !== 'number') return undefined;
  if (g.transform === SIGNAL_TRANSFORMS.LEVEL) return now;
  const prev = ctx.prevReadings ? ctx.prevReadings[g.derived_from] : undefined;
  if (typeof prev !== 'number') return undefined;
  return Math.max(0, Math.min(100, 50 + (now - prev)));
}

// Which inputs the adjustment needs, and whether stored history has them.
export function dataAvailability(adjustment, observations) {
  const a = adjustment;
  let need = null;
  if (a.type === 'ADD_SOURCE') need = a.source_id;
  else if (a.type === 'ADD_SIGNAL') need = a.derived_from || null;
  if (a.type === 'ADD_SIGNAL' && !a.derived_from) {
    return { status: 'NO_HISTORY', input: a.signal_id, observations_with_data: 0, total_observations: observations.length,
      message: 'New signal discovered. It needs its own data feed, and no historical data exists for it yet.' };
  }
  if (!need) return { status: 'HISTORY_AVAILABLE', input: a.source_id || null, observations_with_data: observations.length, total_observations: observations.length, message: 'Uses readings V1 already stores.' };
  const withData = observations.filter((o) => o.readings && typeof o.readings[need] === 'number').length;
  if (!withData) {
    return { status: 'NO_HISTORY', input: need, observations_with_data: 0, total_observations: observations.length,
      message: 'New source discovered. Historical source data is not yet available for recalculation.' };
  }
  return { status: 'HISTORY_AVAILABLE', input: need, observations_with_data: withData, total_observations: observations.length,
    message: `Historical readings exist for ${withData} of ${observations.length} stored V1 observations.` };
}

// observations: [{ ts, stored, readings }] ascending. btcRows: [{ ts, btc_price }] ascending.
export function buildContexts(observations, btcRows) {
  return observations.map((o) => {
    const prev = nearestAtOrBefore(observations, o.ts - 24 * HOUR, 'ts', 8 * HOUR);
    const bNow = nearestAtOrBefore(btcRows, o.ts, 'ts', 4 * HOUR);
    const bPrev = nearestAtOrBefore(btcRows, o.ts - 24 * HOUR, 'ts', 4 * HOUR);
    return { ...o, prevReadings: prev ? prev.readings : null, btc24hPct: bNow && bPrev ? ((bNow.btc_price - bPrev.btc_price) / bPrev.btc_price) * 100 : null };
  });
}

export function recalculate(baseConfig, adjustment, contexts, { eventTs = null } = {}) {
  const availability = dataAvailability(adjustment, contexts);
  const proposedConfig = applyAdjustment(baseConfig, adjustment);
  const points = [];
  for (const c of contexts) {
    const r = scoreWithConfig(baseConfig, c);
    const p = scoreWithConfig(proposedConfig, c);
    if (!r || !p) continue;
    points.push({ ts: c.ts, stored: typeof c.stored === 'number' ? Math.round(c.stored) : null, reconstructed: r.score, proposed: p.score, reconstructed_exact: r.exact, proposed_exact: p.exact });
  }
  const gaps = points.filter((x) => x.stored !== null).map((x) => x.stored - x.reconstructed);
  const reconstruction = {
    observations: gaps.length,
    exact_matches: gaps.filter((g) => g === 0).length,
    mean_abs_gap: gaps.length ? Math.round((gaps.reduce((s, g) => s + Math.abs(g), 0) / gaps.length) * 10) / 10 : null,
    max_abs_gap: gaps.length ? Math.max(...gaps.map(Math.abs)) : null,
  };
  const base = {
    possible: availability.status === 'HISTORY_AVAILABLE',
    availability,
    adjustment_text: describeAdjustment(adjustment, baseConfig),
    proposed_config: proposedConfig,
    observations: points.length,
    from_ts: points.length ? points[0].ts : null,
    to_ts: points.length ? points[points.length - 1].ts : null,
    reconstruction,
  };
  if (!base.possible) return { ...base, event: null, summary: null, series: [] };
  const deltas = points.map((x) => x.proposed - x.reconstructed);
  const changedCalls = points.filter((x) => (x.reconstructed >= 50) !== (x.proposed >= 50)).length;
  const ev = typeof eventTs === 'number' ? nearestAtOrBefore(points, eventTs, 'ts', 8 * HOUR) : null;
  return {
    ...base,
    event: ev ? { ts: ev.ts, stored: ev.stored, reconstructed: ev.reconstructed, proposed: ev.proposed, delta: ev.proposed - ev.reconstructed } : null,
    summary: {
      mean_delta: Math.round((deltas.reduce((s, d) => s + d, 0) / deltas.length) * 10) / 10,
      mean_abs_delta: Math.round((deltas.reduce((s, d) => s + Math.abs(d), 0) / deltas.length) * 10) / 10,
      max_abs_delta: Math.max(...deltas.map(Math.abs)),
      changed_scores: deltas.filter((d) => d !== 0).length,
      changed_calls: changedCalls,
    },
    series: points.slice(-60).map(({ ts, stored, reconstructed, proposed }) => ({ ts, stored, reconstructed, proposed })),
    all_points: points,
  };
}

// ---- Validation: EXP-005's own outcome rule (research/experiment5_agent.py + outcome_engine.py):
// V1 call = UP if score >= 50 else DOWN; realized direction = sign of BTC's return from the nearest price at or before
// the observation to the nearest price at or before observation + 24h, accepted only if that price is after the
// observation and at most 6h short of the target. ----
export const VALIDATION_RULES = Object.freeze({
  method: 'EXP-005 outcome rule (24h realized BTC direction, >=50 = UP)', horizonMs: 24 * HOUR, toleranceMs: 6 * HOUR,
  minIndependentChanged: 5, alpha: 0.1, excludeAroundEventMs: 24 * HOUR,
});

function binomTailAtLeast(k, n) { // P(X >= k), X ~ Bin(n, 0.5)
  let p = 0, c = 1;
  for (let i = 0; i <= n; i++) { if (i > 0) c = (c * (n - i + 1)) / i; if (i >= k) p += c; }
  return p / 2 ** n;
}

export function realizedDirection(btcRows, ts, rules = VALIDATION_RULES) {
  const now = nearestAtOrBefore(btcRows, ts, 'ts', 4 * HOUR);
  const fut = nearestAtOrBefore(btcRows, ts + rules.horizonMs, 'ts', rules.toleranceMs);
  if (!now || !fut || fut.ts <= ts) return null;
  const r = fut.btc_price - now.btc_price;
  return r > 0 ? 'UP' : r < 0 ? 'DOWN' : 'FLAT';
}

export function validateRecalculation(recalc, btcRows, { eventTs = null, rules = VALIDATION_RULES } = {}) {
  const head = { method: rules.method, rules: { horizon_hours: 24, min_independent_changed_calls: rules.minIndependentChanged, alpha: rules.alpha, excluded_window_hours: rules.excludeAroundEventMs / HOUR } };
  if (!recalc.possible) {
    return { ...head, status: 'NOT_ENOUGH_DATA', headline: 'Cannot be validated yet: no historical data for the new input.', resolved: 0 };
  }
  const rows = [];
  for (const p of recalc.all_points || []) {
    if (eventTs !== null && Math.abs(p.ts - eventTs) <= rules.excludeAroundEventMs) continue; // never validate on the event that inspired it
    const realized = realizedDirection(btcRows, p.ts, rules);
    if (!realized || realized === 'FLAT') continue;
    const base = p.reconstructed >= 50 ? 'UP' : 'DOWN';
    const prop = p.proposed >= 50 ? 'UP' : 'DOWN';
    rows.push({ ts: p.ts, day: new Date(p.ts).toISOString().slice(0, 10), realized, base_hit: base === realized, prop_hit: prop === realized, changed: base !== prop });
  }
  const pct = (a, b) => (b ? Math.round((a / b) * 1000) / 10 : null);
  const baseHits = rows.filter((r) => r.base_hit).length;
  const propHits = rows.filter((r) => r.prop_hit).length;
  const changed = rows.filter((r) => r.changed);
  // Observations overlap (hourly, 24h horizon), so significance uses at most one changed call per UTC day.
  const perDay = new Map();
  for (const r of changed) if (!perDay.has(r.day)) perDay.set(r.day, r);
  const indep = [...perDay.values()];
  const improved = indep.filter((r) => r.prop_hit).length;
  const worsened = indep.length - improved;
  const pBetter = indep.length ? binomTailAtLeast(improved, indep.length) : null;
  const pWorse = indep.length ? binomTailAtLeast(worsened, indep.length) : null;
  let status, headline;
  if (!rows.length) { status = 'NOT_ENOUGH_DATA'; headline = 'No resolved outcomes yet.'; }
  else if (!changed.length) { status = 'INCONCLUSIVE'; headline = 'The adjustment never changes V1\'s up/down call, so it cannot make V1 more or less right.'; }
  else if (indep.length < rules.minIndependentChanged) { status = 'VALIDATING'; headline = `Only ${indep.length} independent day(s) where the adjustment changes V1's call. At least ${rules.minIndependentChanged} are needed.`; }
  else if (improved > worsened && pBetter <= rules.alpha) { status = 'SUPPORTED'; headline = `The adjusted V1 was right more often on the days it disagreed with current V1 (${improved} vs ${worsened}).`; }
  else if (worsened > improved && pWorse <= rules.alpha) { status = 'NOT_SUPPORTED'; headline = `The adjusted V1 was wrong more often on the days it disagreed with current V1 (${worsened} vs ${improved}).`; }
  else { status = 'INCONCLUSIVE'; headline = `No clear difference on the days the two disagreed (${improved} better, ${worsened} worse).`; }
  return {
    ...head, status, headline, resolved: rows.length,
    current_v1: { hits: baseHits, total: rows.length, accuracy_pct: pct(baseHits, rows.length) },
    adjusted_v1: { hits: propHits, total: rows.length, accuracy_pct: pct(propHits, rows.length) },
    changed_calls: changed.length,
    independent: { days: indep.length, improved, worsened, p_better: pBetter === null ? null : Math.round(pBetter * 1000) / 1000, p_worse: pWorse === null ? null : Math.round(pWorse * 1000) / 1000 },
  };
}

export const VALIDATION_TEXT = Object.freeze({
  NOT_ENOUGH_DATA: 'Not enough data', VALIDATING: 'Validating', SUPPORTED: 'Supported', NOT_SUPPORTED: 'Not supported', INCONCLUSIVE: 'Inconclusive',
});

export function nextVersionId(existingIds) {
  let max = 0;
  for (const id of existingIds) { const m = /^v1\.(\d+)$/.exec(id); if (m) max = Math.max(max, Number(m[1])); }
  return `v1.${max + 1}`;
}
