// GLM evidence intelligence: output validation, signal qualification and bounded adjustment (contract §7–§9).
// The model proposes; this code decides. Nothing invalid is repaired into a valid-looking result.
import { GLM_RULE_VERSION, GLM_ADJUST, GLM_LIMITS, GLM_SCOPE } from './contract.js';

const TOP_KEYS = ['status', 'horizons', 'findings', 'forecast_comparison', 'gemini_vs_original', 'uncertainty', 'limitations'];
const STATUS = ['completed', 'no_conclusion', 'insufficient_evidence'];
const DIR = ['UP', 'DOWN', 'NEUTRAL'];
const STRENGTH = ['weak', 'moderate', 'strong'];
const FTYPE = ['fact', 'source_claim', 'ai_interpretation', 'model_forecast', 'interpretation', 'hypothesis', 'unknown'];
const FDIR = ['UP', 'DOWN', 'FLAT', 'MISSING'];
const AGREE = ['agree', 'disagree', 'partial', 'unavailable'];
const REL = ['consistent', 'contradicts', 'omits', 'unsupported', 'unverifiable', 'no_original'];
const ASSESS = ['included', 'discounted', 'excluded'];
const UNC = ['low', 'medium', 'high'];

function isObj(x) { return x && typeof x === 'object' && !Array.isArray(x); }
function exactKeys(o, keys, path, errs) {
  for (const k of Object.keys(o)) if (!keys.includes(k)) errs.push(`${path}: unknown key ${k}`);
  for (const k of keys) if (!(k in o)) errs.push(`${path}: missing key ${k}`);
}
function str(x, max, path, errs, { nullable = false } = {}) {
  if (x === null && nullable) return;
  if (typeof x !== 'string') { errs.push(`${path}: not a string`); return; }
  if (x.length > max) errs.push(`${path}: longer than ${max}`);
}
function en(x, vals, path, errs) { if (!vals.includes(x)) errs.push(`${path}: invalid value ${JSON.stringify(x)?.slice(0, 40)}`); }
function refs(x, pkgRefs, path, errs, { min = 0, max = 40 } = {}) {
  if (!Array.isArray(x)) { errs.push(`${path}: not an array`); return; }
  if (x.length < min) errs.push(`${path}: needs at least ${min} ref`);
  if (x.length > max) errs.push(`${path}: more than ${max} refs`);
  for (const r of x) if (typeof r !== 'string' || !pkgRefs.has(r)) errs.push(`${path}: unknown ref ${JSON.stringify(r)?.slice(0, 60)}`);
}

// Returns { ok, value, errors }. `ungroundedNumbers(texts, pkgText)` is the worker's existing checker (injected so the
// grounding rule exists once). Strings longer than allowed, unknown keys/enums/refs, malformed JSON: all rejected.
export function validateGlmOutput(raw, pkg, { ungroundedNumbers } = {}) {
  const errs = [];
  let v = raw;
  if (typeof raw === 'string') {
    try { v = JSON.parse(raw); } catch { return { ok: false, value: null, errors: ['malformed_json'] }; }
  }
  if (!isObj(v)) return { ok: false, value: null, errors: ['not_an_object'] };
  exactKeys(v, TOP_KEYS, '$', errs);
  en(v.status, STATUS, '$.status', errs);
  const pr = pkg.refs;
  if (!Array.isArray(v.horizons)) errs.push('$.horizons: not an array');
  else {
    if (v.horizons.length > 1) errs.push('$.horizons: at most one horizon (24 h)');
    if (v.status === 'completed' && v.horizons.length !== 1) errs.push('$.horizons: completed requires the 24 h horizon');
    v.horizons.forEach((h, i) => {
      const p = `$.horizons[${i}]`;
      if (!isObj(h)) { errs.push(`${p}: not an object`); return; }
      exactKeys(h, ['horizon_hours', 'direction', 'strength', 'supporting_refs', 'contradicting_refs', 'rationale'], p, errs);
      if (h.horizon_hours !== 24) errs.push(`${p}.horizon_hours: only 24 is in scope`);
      en(h.direction, DIR, `${p}.direction`, errs);
      en(h.strength, STRENGTH, `${p}.strength`, errs);
      refs(h.supporting_refs, pr, `${p}.supporting_refs`, errs, { min: h.direction === 'NEUTRAL' ? 0 : 1 });
      refs(h.contradicting_refs, pr, `${p}.contradicting_refs`, errs);
      str(h.rationale, 400, `${p}.rationale`, errs);
    });
  }
  if (!Array.isArray(v.findings) || v.findings.length > 12) errs.push('$.findings: array of at most 12');
  else v.findings.forEach((f, i) => {
    const p = `$.findings[${i}]`;
    if (!isObj(f)) { errs.push(`${p}: not an object`); return; }
    exactKeys(f, ['type', 'claim', 'refs'], p, errs);
    en(f.type, FTYPE, `${p}.type`, errs);
    str(f.claim, 240, `${p}.claim`, errs);
    refs(f.refs, pr, `${p}.refs`, errs, { min: 1, max: 12 });
  });
  const fc = v.forecast_comparison;
  if (!isObj(fc)) errs.push('$.forecast_comparison: not an object');
  else {
    exactKeys(fc, ['timesfm_direction', 'chronos_direction', 'agreement', 'note'], '$.forecast_comparison', errs);
    en(fc.timesfm_direction, FDIR, '$.forecast_comparison.timesfm_direction', errs);
    en(fc.chronos_direction, FDIR, '$.forecast_comparison.chronos_direction', errs);
    en(fc.agreement, AGREE, '$.forecast_comparison.agreement', errs);
    str(fc.note, 240, '$.forecast_comparison.note', errs);
  }
  if (!Array.isArray(v.gemini_vs_original) || v.gemini_vs_original.length > 6) errs.push('$.gemini_vs_original: array of at most 6');
  else v.gemini_vs_original.forEach((g, i) => {
    const p = `$.gemini_vs_original[${i}]`;
    if (!isObj(g)) { errs.push(`${p}: not an object`); return; }
    exactKeys(g, ['gemini_ref', 'original_ref', 'relation', 'assessment', 'reason'], p, errs);
    if (typeof g.gemini_ref !== 'string' || !pr.has(g.gemini_ref)) errs.push(`${p}.gemini_ref: unknown ref`);
    else if (pkg.items && !pkg.items.some((it) => it.ref === g.gemini_ref && it.provenance_class === 'gemini_interpretation')) errs.push(`${p}.gemini_ref: not a Gemini interpretation`);
    if (g.original_ref !== null && (typeof g.original_ref !== 'string' || !pr.has(g.original_ref))) errs.push(`${p}.original_ref: unknown ref`);
    en(g.relation, REL, `${p}.relation`, errs);
    // A claim of consistency/contradiction needs an original; without one only unverifiable/no_original is allowed.
    if (g.original_ref === null && !['unverifiable', 'no_original'].includes(g.relation)) errs.push(`${p}.relation: needs an original_ref`);
    en(g.assessment, ASSESS, `${p}.assessment`, errs);
    str(g.reason, 160, `${p}.reason`, errs);
  });
  en(v.uncertainty, UNC, '$.uncertainty', errs);
  if (!Array.isArray(v.limitations) || v.limitations.length > 6) errs.push('$.limitations: array of at most 6');
  else v.limitations.forEach((l, i) => str(l, 160, `$.limitations[${i}]`, errs));

  if (!errs.length && typeof ungroundedNumbers === 'function') {
    const texts = [
      ...(v.horizons || []).map((h) => h.rationale), ...(v.findings || []).map((f) => f.claim),
      v.forecast_comparison?.note, ...(v.limitations || []), ...(v.gemini_vs_original || []).map((g) => g.reason),
    ].filter((t) => typeof t === 'string');
    const bad = ungroundedNumbers(texts, pkg.prompt.user);
    if (bad && bad.length) errs.push('ungrounded_numbers: ' + bad.slice(0, 5).join(','));
  }
  return errs.length ? { ok: false, value: null, errors: errs.slice(0, 20) } : { ok: true, value: v, errors: [] };
}

// AI-derived items: existing Gemini/other-AI interpretations and V1 scores computed from them. They are evidence to
// assess, never independent confirmation on their own.
export function isAiDerived(it) {
  return it.provenance_class === 'gemini_interpretation' || it.provenance_class === 'v1_ai_interpretation' || !!it.content?.ai_derived;
}

// Distinct corroboration families among refs, after collapsing shared independence keys (§4). glm-qual-0.2: a chain
// (independence key) counts only if it contains at least one non-AI item, so a Gemini summary without a checkable
// original never corroborates anything by itself; it still appears in the refs and in gemini_vs_original.
export function familiesOf(refList, itemsByRef) {
  const keys = new Map();               // independence key -> { family, nonAi }
  for (const r of refList) {
    const it = itemsByRef.get(r);
    if (!it) continue;
    const e = keys.get(it.independence_key) || { family: it.family, nonAi: false };
    if (!isAiDerived(it)) { e.nonAi = true; e.family = it.family; }
    keys.set(it.independence_key, e);
  }
  const counted = [...keys.values()].filter((e) => e.nonAi);
  return { families: [...new Set(counted.map((e) => e.family))].sort(), independentKeys: counted.length, aiOnlyKeys: keys.size - counted.length };
}

function forecastDir(it) {
  const d = String(it?.content?.direction || '').toUpperCase();
  return d === 'UP' || d === 'DOWN' ? d : null;
}

// Deterministic qualification (`glm-qual-0.1`, PROVISIONAL). previous = last stored BTC 24 h signal or null.
// Returns the signal record (without ids), always with reason codes; NO_SIGNAL is never mislabelled as a direction.
export function qualifySignal({ output, pkg, previous = null, runStatus = 'completed' }) {
  const cutoff = pkg?.cutoffTs;
  const base = {
    asset: 'BTC', horizon_hours: 24, rule_version: GLM_RULE_VERSION, decision_cutoff_ts: cutoff ?? null,
    valid_from_ts: null, valid_until_ts: cutoff != null ? cutoff + GLM_LIMITS.signalValidityMs : null,
    direction: 'NO_SIGNAL', proposed_direction: null, proposed_strength: null, strength: null, eligibility: 'NO_SIGNAL',
    reasons: [], supporting_families: [], contradicting_families: [], supporting_refs: [], contradicting_refs: [],
    timesfm_ref: null, chronos_ref: null, models_live: false,
  };
  if (runStatus !== 'completed' || !output) return { ...base, reasons: [runStatus === 'completed' ? 'no_output' : runStatus] };
  if (output.status !== 'completed') return { ...base, reasons: [output.status] };
  const h = (output.horizons || [])[0];
  if (!h) return { ...base, reasons: ['no_horizon'] };
  const s = { ...base, proposed_direction: h.direction, proposed_strength: h.strength,
    supporting_refs: h.supporting_refs, contradicting_refs: h.contradicting_refs };
  if (h.direction === 'NEUTRAL') return { ...s, reasons: ['neutral'] };

  const byRef = new Map(pkg.items.map((i) => [i.ref, i]));
  // Gemini items the model itself discounted or excluded do not support anything.
  const dropped = new Set((output.gemini_vs_original || []).filter((g) => g.assessment !== 'included').map((g) => g.gemini_ref));
  const supRefs = h.supporting_refs.filter((r) => !dropped.has(r));
  if (supRefs.length !== h.supporting_refs.length) s.reasons.push('gemini_support_discounted');
  const sup = familiesOf(supRefs, byRef);
  const con = familiesOf(h.contradicting_refs, byRef);
  s.supporting_families = sup.families;
  s.contradicting_families = con.families;

  const tfm = pkg.items.find((i) => i.provenance_class === 'forecast_timesfm') || null;
  const chr = pkg.items.find((i) => i.provenance_class === 'forecast_chronos') || null;
  s.timesfm_ref = tfm?.ref ?? null;
  s.chronos_ref = chr?.ref ?? null;
  const live = (it) => it && it.times.target_ts != null && it.times.target_ts > cutoff
    && (cutoff - it.times.first_available_ts) <= GLM_LIMITS.staleForecastMs;
  s.models_live = !!(live(tfm) || live(chr));

  const reasons = [];
  if (!s.models_live) reasons.push('no_live_model_forecast');
  if (sup.families.length < 2) reasons.push('corroboration_below_2_families');
  const fresh = supRefs.some((r) => { const it = byRef.get(r); return it && (cutoff - it.times.first_available_ts) <= GLM_LIMITS.freshMs; });
  if (!fresh) reasons.push('no_fresh_support');
  if (con.families.length >= sup.families.length && con.families.length > 0) reasons.push('contradicted');
  if (reasons.length) return { ...s, reasons: [...s.reasons, ...reasons] };

  let k = ['weak', 'moderate', 'strong'].indexOf(h.strength);
  const tD = live(tfm) ? forecastDir(tfm) : null;
  const cD = live(chr) ? forecastDir(chr) : null;
  if (tD && cD && tD !== h.direction && cD !== h.direction) {
    k -= 1;
    s.reasons.push('models_oppose_downgraded');
    if (k < 0) return { ...s, reasons: [...s.reasons, 'models_oppose'] };
  }
  s.strength = ['weak', 'moderate', 'strong'][k];
  s.direction = h.direction;
  const [gLo, gHi] = GLM_LIMITS.persistenceGapMs;
  const prevOk = previous && previous.direction === h.direction && ['CANDIDATE', 'ELIGIBLE'].includes(previous.eligibility)
    && cutoff - previous.decision_cutoff_ts >= gLo && cutoff - previous.decision_cutoff_ts <= gHi;
  s.eligibility = prevOk ? 'ELIGIBLE' : 'CANDIDATE';
  s.reasons.push(prevOk ? 'persistent_2_days' : 'first_day_candidate');
  return s;
}

// Bounded adjustment (§9). mode 'shadow' (cap 0) or 'active'. Only ELIGIBLE signals inside their validity window and
// matching asset/horizon apply; would_apply is measured on the reference cap for evaluation.
export function computeAdjustment({ signal, baseline_p_up, predictionTs, coin, horizonHours, influenceCap, allowFlip = false }) {
  const out = { baseline_p_up, would_apply: 0, applied: 0, cap: 0, final_p_up: baseline_p_up, signal_status: 'NO_SIGNAL', reason: null };
  if (!Number.isFinite(baseline_p_up)) return { ...out, reason: 'no_baseline' };
  if (!GLM_SCOPE.assets.includes(coin) || !GLM_SCOPE.horizons.includes(horizonHours)) {
    return { ...out, reason: GLM_SCOPE.noSignalReasons[horizonHours] || GLM_SCOPE.noSignalReasons[coin] || 'out_of_scope' };
  }
  if (!signal || !['UP', 'DOWN'].includes(signal.direction)) return { ...out, reason: 'no_signal' };
  if (signal.asset !== coin || Number(signal.horizon_hours) !== horizonHours) return { ...out, reason: 'scope_mismatch' };
  if (!(predictionTs >= signal.valid_from_ts && predictionTs < signal.valid_until_ts)) return { ...out, reason: 'expired_or_not_yet_valid' };
  const dir = signal.direction === 'UP' ? 1 : -1;
  const k = GLM_ADJUST.strengthK[signal.strength] ?? 0;
  const capNum = Number(influenceCap);
  const cap = Number.isFinite(capNum) && capNum > 0 ? Math.min(capNum, GLM_ADJUST.hardMaxCap) : 0;
  const would = dir * k * GLM_ADJUST.referenceCap;
  const applied = signal.eligibility === 'ELIGIBLE' ? dir * k * cap : 0;
  const lo = Math.min(GLM_ADJUST.clampLo, baseline_p_up);
  const hi = Math.max(GLM_ADJUST.clampHi, baseline_p_up);
  let final = Math.min(hi, Math.max(lo, baseline_p_up + applied));
  // No-flip safeguard (glm-qual-0.4): GLM can strengthen or weaken the baseline call but never reverse its side of 0.5.
  const mid = GLM_ADJUST.neutralPoint;
  let flipCapped = false;
  if (!allowFlip && baseline_p_up !== mid && (baseline_p_up - mid) * (final - mid) < 0) { final = mid; flipCapped = true; }
  const r6 = (x) => Math.round(x * 1e6) / 1e6;
  const reason = signal.eligibility !== 'ELIGIBLE' ? 'candidate_not_applied' : cap <= 0 ? 'shadow_cap_0' : flipCapped ? 'applied_no_flip_capped' : 'applied';
  return { baseline_p_up, would_apply: r6(would), applied: r6(final - baseline_p_up), cap, final_p_up: r6(final),
    signal_status: signal.eligibility, reason };
}
