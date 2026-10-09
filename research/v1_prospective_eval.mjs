// Prospective, fixed-horizon evaluation of the registered V1 candidates T-A1 and T-A2. RESEARCH ONLY.
//
// Plan: research/v1_candidate_registration_addendum_1.json (frozen before any post-boundary outcome was read).
// Parent registration: research/v1_candidate_registration.json (unchanged).
//
// Read-only. Inputs are SELECT-only extracts saved as files; this script never connects to D1, never creates a
// Learning candidate or methodology version, and changes no V1 source, weight or score. It imports the deployed
// validator (commit bdfb2cb) and refuses to run if those files differ from the pinned git blob hashes.
//
// Progress mode (default; the scheduled run): the output holds ONLY coverage, missingness, integrity and the unit
// count -- no hits, accuracies, improved/worsened counts, p-values or verdicts -- and is checked against a key
// whitelist before it is written. Outcome directions are never formed in this mode. A candidate with 30 units is
// LOOK_ELIGIBLE. Look mode (--mode look --candidate T-A1|T-A2, run deliberately by a person): evaluates that one
// candidate once, on the observations through the end of its 30th unit's UTC day, and only if it is eligible.
//
// Usage: node research/v1_prospective_eval.mjs --v1 v1_extract.json --btc btc_extract.json --out result.json
//          [--previous earlier_result.json] [--code-commit <sha of this checkout>]
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  baselineConfig, normalizeAdjustment, scoreWithConfig, buildContexts, recalculate, validateRecalculation, VALIDATION_RULES,
} from '../learning/learning-method.js';
import { nearestAtOrBefore } from '../learning/learning-core.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const HOUR = 3600000;
const DAY = 24 * HOUR;

export const ADDENDUM_FILE = 'research/v1_candidate_registration_addendum_1.json';
export const ADDENDUM_SHA256 = 'c2ebdfcb75a1690b25f3a327bf7537f8ee6853e16a0dc6295bebd18b8ac7ae9c';
export const ERRATUM_FILE = 'research/v1_candidate_registration_addendum_1_erratum_1.json';
export const ERRATUM_SHA256 = '5397fe70cf1415aa239beb944350340a48d711244e4d16b1f18edc8c313cb15d';
export const REGISTRATION_FILE = 'research/v1_candidate_registration.json';
export const REGISTRATION_SHA256_BYTES = 'ac88bf2df2968f8ddfe62f76aa10be76ee765cd13f7dc226e929aa54669a7455';
export const IMPLEMENTATION_COMMIT = 'bdfb2cb21a2e0814273d276a9bd84b09d74625a4';
export const PINNED_BLOBS = Object.freeze({
  'learning/learning-method.js': 'ae4b9e13726144334ea9b3975d0726338c0de22e',
  'learning/learning-core.js': 'd34ecdb0ea24ce458c310a851dc4a66f05d5ac74',
});
export const BOUNDARY_MS = 1791561600000;          // 2026-10-09T16:00:00Z; strictly after
export const UNITS_REQUIRED = 30;
export const SETTLE_MS = 36 * HOUR;                // after the end of a UTC day (addendum unit.settled_day)
export const OUTCOME_MS = 24 * HOUR;
export const RESEARCH_ALPHA = 0.1 / 3;
export const CANDIDATES = Object.freeze([
  Object.freeze({ id: 'T-A1', adjustment: Object.freeze({ type: 'CHANGE_CLASSIFICATION', source_id: 'fng', group: 'BREADTH', invert: true }), target: 'fng' }),
  Object.freeze({ id: 'T-A2', adjustment: Object.freeze({ type: 'REMOVE_SOURCE', source_id: 'etfflows' }), target: 'etfflows' }),
]);

const sha256 = (buf) => createHash('sha256').update(buf).digest('hex');
export const gitBlobSha1 = (buf) => createHash('sha1').update(Buffer.concat([Buffer.from(`blob ${buf.length}\0`), buf])).digest('hex');

// Refuses to run on anything but the frozen plan and the pinned validator.
export function verifyPins(root = ROOT) {
  const problems = [];
  if (sha256(readFileSync(join(root, ADDENDUM_FILE))) !== ADDENDUM_SHA256) problems.push(`${ADDENDUM_FILE} differs from the frozen addendum`);
  if (sha256(readFileSync(join(root, ERRATUM_FILE))) !== ERRATUM_SHA256) problems.push(`${ERRATUM_FILE} differs from erratum 1`);
  if (sha256(readFileSync(join(root, REGISTRATION_FILE))) !== REGISTRATION_SHA256_BYTES) problems.push(`${REGISTRATION_FILE} differs from the registration`);
  for (const [f, blob] of Object.entries(PINNED_BLOBS)) {
    if (gitBlobSha1(readFileSync(join(root, f))) !== blob) problems.push(`${f} is not the ${IMPLEMENTATION_COMMIT.slice(0, 7)} version`);
  }
  const reg = JSON.parse(readFileSync(join(root, REGISTRATION_FILE), 'utf8'));
  for (const c of CANDIDATES) {
    const r = reg.candidates.find((x) => x.id === c.id);
    if (!r || JSON.stringify(sortKeys(r.adjustment)) !== JSON.stringify(sortKeys(c.adjustment))) problems.push(`${c.id} adjustment differs from the registration`);
    const n = normalizeAdjustment(c.adjustment);
    if (!n.ok || JSON.stringify(sortKeys(n.adjustment)) !== JSON.stringify(sortKeys(c.adjustment))) problems.push(`${c.id} does not normalise unchanged`);
  }
  if (Date.parse(reg.registered_utc) !== BOUNDARY_MS) problems.push('boundary differs from registered_utc');
  if (problems.length) throw new Error(`Pin check failed: ${problems.join('; ')}`);
  return true;
}

function sortKeys(o) { return Object.fromEntries(Object.keys(o).sort().map((k) => [k, o[k]])); }
function parseJson(raw, fallback) { // the product's parser (learning-api.js)
  if (typeof raw !== 'string') return fallback;
  try { return JSON.parse(raw); } catch (_e) { return fallback; }
}
export const dayOf = (ts) => new Date(ts).toISOString().slice(0, 10);
export const dayEndMs = (day) => Date.parse(`${day}T00:00:00.000Z`) + DAY; // exclusive end

// The product's loadAllV1Observations rule: archive first, then history, de-duplicated by ts.
export function mergeObservations(archiveRows, historyRows) {
  const counts = { archive_rows: 0, history_rows: 0, invalid_timestamp: 0, duplicate_timestamp: 0, unparsable_sources_json: 0, at_or_before_boundary: 0, after_boundary: 0 };
  const byTs = new Map();
  for (const [rows, key] of [[archiveRows || [], 'archive_rows'], [historyRows || [], 'history_rows']]) {
    for (const r of rows) {
      counts[key]++;
      const ts = typeof r.ts === 'number' ? r.ts : Number.NaN;
      if (!Number.isFinite(ts)) { counts.invalid_timestamp++; continue; }
      if (byTs.has(ts)) { counts.duplicate_timestamp++; continue; }
      const readings = parseJson(r.sources_json, null);
      if (readings === null || typeof readings !== 'object' || Array.isArray(readings)) counts.unparsable_sources_json++;
      byTs.set(ts, { ts, stored: r.score, readings: readings && typeof readings === 'object' && !Array.isArray(readings) ? readings : {} });
    }
  }
  const all = [...byTs.values()].sort((a, b) => a.ts - b.ts);
  const holdout = all.filter((o) => o.ts > BOUNDARY_MS);
  counts.at_or_before_boundary = all.length - holdout.length;
  counts.after_boundary = holdout.length;
  return { holdout, counts };
}

export function cleanBtc(rows) {
  return (rows || []).filter((r) => typeof r.ts === 'number' && Number.isFinite(r.ts) && typeof r.btc_price === 'number' && Number.isFinite(r.btc_price) && r.btc_price > 0)
    .map((r) => ({ ts: r.ts, btc_price: r.btc_price })).sort((a, b) => a.ts - b.ts);
}

function dayDigest(obs) {
  return sha256(JSON.stringify(obs.map((o) => [o.ts, typeof o.stored === 'number' ? o.stored : null, o.readings])));
}

// Whether the validator's realizedDirection would return UP or DOWN, computed WITHOUT forming the direction: the same two price
// lookups (now: at or before ts within 4h; future: at or before ts + 24h within the 6h tolerance and after ts), and
// "not FLAT" as plain inequality. Progress runs use only this; the direction itself is formed only inside the
// deployed validator, at a candidate's fixed look.
export function isResolvable(btcRows, ts, rules = VALIDATION_RULES) {
  const now = nearestAtOrBefore(btcRows, ts, 'ts', 4 * HOUR);
  const fut = nearestAtOrBefore(btcRows, ts + rules.horizonMs, 'ts', rules.toleranceMs);
  if (!now || !fut || fut.ts <= ts) return false;
  return fut.btc_price !== now.btc_price;
}

// Per-candidate unit scan. Uses ONLY whether an outcome resolved (never its direction) and whether the calls differ.
function scanUnits(candidate, holdout, btc, isSettled) {
  const recalc = recalculate(baselineConfig(), candidate.adjustment, buildContexts(holdout, btc), { eventTs: null });
  const points = recalc.all_points || [];
  const unitDays = [];
  const perDay = new Map();
  const counts = { no_reconstruction: holdout.length - points.length, target_source_missing: 0, outcome_unresolved_or_flat: 0, unsettled_observations: 0 };
  for (const o of holdout) if (typeof o.readings[candidate.target] !== 'number') counts.target_source_missing++;
  for (const p of points) {
    const day = dayOf(p.ts);
    if (!isSettled(day)) { counts.unsettled_observations++; continue; }
    if (!isResolvable(btc, p.ts)) { counts.outcome_unresolved_or_flat++; continue; }
    const changed = (p.reconstructed >= 50) !== (p.proposed >= 50);
    if (changed && !perDay.has(day)) { perDay.set(day, p.ts); unitDays.push(day); }
  }
  unitDays.sort();
  return { unitDays, unitTs: unitDays.map((d) => perDay.get(d)), counts };
}

// Exact two-sided 95% Clopper-Pearson interval for k of n.
export function clopperPearson(k, n, level = 0.95) {
  const a = (1 - level) / 2;
  const logC = (nn, i) => { let s = 0; for (let j = 1; j <= i; j++) s += Math.log(nn - i + j) - Math.log(j); return s; };
  const tailGE = (p, kk) => { let s = 0; for (let i = kk; i <= n; i++) s += Math.exp(logC(n, i) + i * Math.log(p) + (n - i) * Math.log(1 - p)); return s; };
  const solve = (f) => { let lo = 0, hi = 1; for (let it = 0; it < 200; it++) { const m = (lo + hi) / 2; if (f(m)) hi = m; else lo = m; } return (lo + hi) / 2; };
  const lower = k === 0 ? 0 : solve((p) => tailGE(p, k) >= a);              // P(X >= k | p) = a
  const upper = k === n ? 1 : solve((p) => 1 - tailGE(p, k + 1) <= a);      // P(X <= k | p) = a
  const r = (x) => Math.round(x * 10000) / 10000;
  return { lower: r(lower), upper: r(upper) };
}

// True if the validator, run on only the given unit observations, reproduces its own independent result on the full set.
export function unitsAgree(recalc, btc, unitTsSet, fullResult, opts) {
  const onlyUnits = validateRecalculation({ ...recalc, all_points: (recalc.all_points || []).filter((p) => unitTsSet.has(p.ts)) }, btc, opts);
  const a = onlyUnits.independent, b = fullResult.independent;
  return !!(a && b && a.days === b.days && a.days === unitTsSet.size && a.improved === b.improved && a.worsened === b.worsened);
}

function look(candidate, holdout, btc, unitDays, unitTs) {
  const d30 = unitDays[UNITS_REQUIRED - 1];
  const units = new Set(unitTs.slice(0, UNITS_REQUIRED));
  const end = dayEndMs(d30);
  const analysis = holdout.filter((o) => o.ts < end);
  const recalc = recalculate(baselineConfig(), candidate.adjustment, buildContexts(analysis, btc), { eventTs: null });
  const opts = { eventTs: null, discoveredAt: BOUNDARY_MS, requireHoldout: true };
  const product = validateRecalculation(recalc, btc, opts);
  const research = validateRecalculation(recalc, btc, { ...opts, rules: { ...VALIDATION_RULES, alpha: RESEARCH_ALPHA } });
  if (!product.independent || product.independent.days !== UNITS_REQUIRED || research.independent.days !== UNITS_REQUIRED) {
    return { status: 'INTEGRITY_ERROR', detail: `validator found ${product.independent ? product.independent.days : 'no'} independent days, expected ${UNITS_REQUIRED}`, look: null };
  }
  // Same units: the validator run on ONLY the 30 frozen-rule unit observations must give the same independent result
  // as its run on the whole analysis set (it would not if it picked a different observation on any day).
  if (units.size !== UNITS_REQUIRED || !unitsAgree(recalc, btc, units, product, opts)) {
    return { status: 'INTEGRITY_ERROR', detail: 'the validator\'s independent units differ from the frozen-rule units', look: null };
  }
  const strip = (v) => { const { exploratory_in_sample: _x, ...rest } = v; return rest; };   // no pre-boundary rows were passed
  const { improved, worsened } = research.independent;
  const claim = research.status === 'SUPPORTED' || research.status === 'NOT_SUPPORTED' ? research.status : 'INCONCLUSIVE';
  return {
    status: 'EVALUATED',
    look: {
      analysis_window: { from_exclusive_utc: new Date(BOUNDARY_MS).toISOString(), to_exclusive_utc: new Date(end).toISOString(), observations: analysis.length, unit_30_day: d30 },
      research_claim: claim,
      research_alpha: '0.1/3',
      effect: { units: UNITS_REQUIRED, improved, worsened, improvement_proportion: Math.round((improved / UNITS_REQUIRED) * 10000) / 10000, ci95_clopper_pearson: clopperPearson(improved, UNITS_REQUIRED) },
      product_level: strip(product),
      research_level: strip(research),
    },
  };
}

// Pure: same parsed inputs -> same output. Inputs are not mutated.
// mode 'progress' (default): never evaluates a look; a candidate with 30 units is reported as LOOK_ELIGIBLE.
// mode 'look' with lookCandidate: evaluates that one candidate's fixed look, and only if it is eligible.
export function evaluate({ v1, btc: btcExtract, previous = null, inputHashes = null, codeCommit = null, mode = 'progress', lookCandidate = null }) {
  if (mode !== 'progress' && mode !== 'look') throw new Error(`unknown mode ${mode}`);
  if (mode === 'look' && !CANDIDATES.some((c) => c.id === lookCandidate)) throw new Error('look mode needs --candidate T-A1 or T-A2');
  const asOf = Math.min(Number(v1.manifest && v1.manifest.as_of_ms), Number(btcExtract.manifest && btcExtract.manifest.as_of_ms));
  if (!Number.isFinite(asOf)) throw new Error('Both extracts need manifest.as_of_ms');
  const { holdout, counts } = mergeObservations(v1.archive, v1.history);
  const btc = cleanBtc(btcExtract.rows);
  const btcMax = btc.length ? btc[btc.length - 1].ts : -Infinity;
  const isSettled = (day) => asOf >= dayEndMs(day) + SETTLE_MS && btcMax >= dayEndMs(day) + OUTCOME_MS;

  const byDay = new Map();
  for (const o of holdout) { const d = dayOf(o.ts); if (!byDay.has(d)) byDay.set(d, []); byDay.get(d).push(o); }
  const scans = CANDIDATES.map((c) => [c, scanUnits(c, holdout, btc, isSettled)]);
  const days = [...byDay.keys()].sort().map((d) => {
    const obs = byDay.get(d);
    return {
      day: d, settled: isSettled(d), observations: obs.length,
      with_fng: obs.filter((o) => typeof o.readings.fng === 'number').length,
      with_etfflows: obs.filter((o) => typeof o.readings.etfflows === 'number').length,
      stored_score_missing: obs.filter((o) => typeof o.stored !== 'number').length,
      reconstruction_call_mismatch: mismatchCount(obs),
      unit_for: scans.filter(([, s]) => s.unitDays.includes(d)).map(([c]) => c.id),
      digest: dayDigest(obs),
    };
  });

  // Integrity: a day settled in ANY earlier record must still have exactly the digest it had then.
  const conflicts = [];
  const now = new Map(days.map((d) => [d.day, d.digest]));
  for (const rec of [].concat(previous || [])) {
    for (const p of (rec && rec.days) || []) if (p.settled && now.get(p.day) !== p.digest && !conflicts.includes(p.day)) conflicts.push(p.day);
  }
  conflicts.sort();

  const candidates = scans.map(([c, s]) => {
    const base = { id: c.id, adjustment: c.adjustment, units_required: UNITS_REQUIRED, units_accumulated: Math.min(s.unitDays.length, UNITS_REQUIRED),
      look_eligible: !conflicts.length && s.unitDays.length >= UNITS_REQUIRED, exclusions: s.counts };
    if (conflicts.length) return { ...base, status: 'INTEGRITY_CONFLICT', detail: `settled day(s) changed since the previous record: ${conflicts.join(', ')}`, look: null };
    if (s.unitDays.length < UNITS_REQUIRED) return { ...base, status: 'AWAITING_HOLDOUT', look: null };
    if (mode === 'look' && lookCandidate === c.id) return { ...base, ...look(c, holdout, btc, s.unitDays, s.unitTs) };
    return { ...base, status: 'LOOK_ELIGIBLE', look: null };
  });
  if (mode === 'look') {
    const target = candidates.find((c) => c.id === lookCandidate);
    if (target.status === 'AWAITING_HOLDOUT') throw new Error(`${lookCandidate} is not eligible for its fixed look (${target.units_accumulated}/${UNITS_REQUIRED} units); nothing evaluated`);
  }

  const settledDays = days.filter((d) => d.settled).map((d) => d.day);
  return {
    evaluator: {
      name: 'v1_prospective_eval', plan: ADDENDUM_FILE, plan_sha256: ADDENDUM_SHA256, erratum: ERRATUM_FILE, erratum_sha256: ERRATUM_SHA256,
      registration_sha256_bytes: REGISTRATION_SHA256_BYTES,
      implementation_commit: IMPLEMENTATION_COMMIT, implementation_blobs: PINNED_BLOBS, code_commit: codeCommit || 'not recorded',
    },
    mode,
    scope: 'PROSPECTIVE: V1 observations strictly after 2026-10-09T16:00:00Z only',
    exploratory_reference: 'Exploratory historical findings (research/results/v1_failure_analysis.json; 575 frozen observations 2026-08-27..2026-10-06) are hypothesis-generating only and are not used here.',
    inputs: { hashes: inputHashes, v1_manifest: v1.manifest, btc_manifest: btcExtract.manifest, as_of_utc: new Date(asOf).toISOString(),
      btc_rows: btc.length, btc_last_utc: btc.length ? new Date(btcMax).toISOString() : null },
    observation_counts: counts,
    coverage: { holdout_days_with_observations: days.length, settled_days_with_observations: settledDays.length, settled_frontier: settledDays.length ? settledDays[settledDays.length - 1] : null },
    days,
    candidates,
  };
}

// Data quality only: observations whose stored V1 call differs from the reconstructed v1.0 call.
function mismatchCount(obs) {
  const cfg = baselineConfig();
  let n = 0;
  for (const o of obs) {
    const r = scoreWithConfig(cfg, { readings: o.readings, prevReadings: null, btc24hPct: null });
    if (r && typeof o.stored === 'number' && (Math.round(o.stored) >= 50) !== (r.score >= 50)) n++;
  }
  return n;
}

// Every key a progress record may contain (anywhere in the tree). Anything else -- hits, accuracies, improved/worsened
// counts, p-values, verdicts, a look -- makes the run fail before anything is written.
export const PROGRESS_KEYS = Object.freeze(new Set([
  'evaluator', 'mode', 'scope', 'exploratory_reference', 'inputs', 'observation_counts', 'coverage', 'days', 'candidates',
  'name', 'plan', 'plan_sha256', 'erratum', 'erratum_sha256', 'registration_sha256_bytes', 'implementation_commit', 'implementation_blobs', 'code_commit',
  ...Object.keys(PINNED_BLOBS),
  'hashes', 'v1_sha256', 'btc_sha256', 'v1_manifest', 'btc_manifest', 'as_of_utc', 'btc_rows', 'btc_last_utc',
  'as_of_ms', 'database', 'statements', 'rows', 'archive', 'history', 'changes_reported', 'd1_meta', 'changes', 'rows_written', 'changed_db', 'rows_read', 'source', 'extractor',
  'archive_rows', 'history_rows', 'invalid_timestamp', 'duplicate_timestamp', 'unparsable_sources_json', 'at_or_before_boundary', 'after_boundary',
  'holdout_days_with_observations', 'settled_days_with_observations', 'settled_frontier',
  'day', 'settled', 'observations', 'with_fng', 'with_etfflows', 'stored_score_missing', 'reconstruction_call_mismatch', 'unit_for', 'digest',
  'id', 'adjustment', 'type', 'source_id', 'group', 'invert', 'units_required', 'units_accumulated', 'look_eligible', 'exclusions',
  'no_reconstruction', 'target_source_missing', 'outcome_unresolved_or_flat', 'unsettled_observations', 'status', 'detail', 'look',
]));
export const PROGRESS_STATUSES = Object.freeze(['AWAITING_HOLDOUT', 'LOOK_ELIGIBLE', 'INTEGRITY_CONFLICT']);

export function assertProgressOnly(out) {
  const bad = [];
  const walk = (o, path) => {
    if (Array.isArray(o)) { o.forEach((v, i) => walk(v, `${path}[${i}]`)); return; }
    if (o && typeof o === 'object') for (const [k, v] of Object.entries(o)) { if (!PROGRESS_KEYS.has(k)) bad.push(`${path}.${k}`); walk(v, `${path}.${k}`); }
  };
  walk(out, '$');
  if (out.mode !== 'progress') bad.push('$.mode is not progress');
  for (const c of out.candidates || []) {
    if (!PROGRESS_STATUSES.includes(c.status)) bad.push(`${c.id}.status ${c.status}`);
    if (c.look !== null) bad.push(`${c.id}.look is not null`);
  }
  const text = JSON.stringify({ ...out, inputs: undefined });
  if (/SUPPORTED|INCONCLUSIVE|EVALUATED|\bPASS\b|\bFAIL\b/.test(text)) bad.push('verdict-like text present');
  if (bad.length) throw new Error(`Progress record contains non-permitted content: ${bad.slice(0, 10).join(', ')}`);
  return true;
}

export function renderRecord(result) { return `${JSON.stringify(result, null, 1)}\n`; }

function main(argv) {
  const arg = (k) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : null; };
  const v1Path = arg('--v1'), btcPath = arg('--btc'), out = arg('--out');
  const mode = arg('--mode') || 'progress';
  if (!v1Path || !btcPath || !out) { console.error('usage: --v1 <file> --btc <file> --out <file> [--previous <file>] [--code-commit <sha>] [--mode progress|look --candidate T-A1|T-A2]'); process.exit(2); }
  verifyPins();
  const v1Raw = readFileSync(v1Path), btcRaw = readFileSync(btcPath);
  const prev = argv.flatMap((a, i) => (a === '--previous' ? [JSON.parse(readFileSync(argv[i + 1], 'utf8'))] : []));
  const result = evaluate({ v1: JSON.parse(v1Raw), btc: JSON.parse(btcRaw), previous: prev, inputHashes: { v1_sha256: sha256(v1Raw), btc_sha256: sha256(btcRaw) },
    codeCommit: arg('--code-commit'), mode, lookCandidate: arg('--candidate') });
  if (mode === 'progress') assertProgressOnly(result);
  writeFileSync(out, renderRecord(result));
  for (const c of result.candidates) console.log(`${c.id}: ${c.status}, units ${c.units_accumulated}/${c.units_required}`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main(process.argv.slice(2));
