// T-A1 / T-A2 prospective fixed-horizon evaluator (research/v1_prospective_eval.mjs). Synthetic data only.
import { describe, it, expect } from 'vitest';
import { readFileSync, mkdtempSync, mkdirSync, copyFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  evaluate, verifyPins, mergeObservations, clopperPearson, BOUNDARY_MS, UNITS_REQUIRED, ADDENDUM_FILE, REGISTRATION_FILE, PINNED_BLOBS,
} from '../research/v1_prospective_eval.mjs';
import { V1_METHODOLOGY_V1 } from '../learning/learning-core.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const HOUR = 3600000, DAY = 24 * HOUR;
const DAY0 = Date.parse('2026-10-10T00:00:00Z');

// All sources neutral (50) except fng = 80: reconstructed V1 = 53 (UP); T-A1 (fng inverted -> 20) = 50 - 0.48 -> 49.5 (DOWN).
// T-A2 (remove etfflows at 50) leaves the call UP, so T-A2 never disagrees in this fixture.
function readings(over = {}) {
  const r = {};
  for (const s of V1_METHODOLOGY_V1.sources) r[s.id] = 50;
  return { ...r, fng: 80, ...over };
}

// days: number of days starting 2026-10-10; per day one 12:00 observation (plus `extraPerDay` more), realized
// direction DOWN on the first `improved` days (T-A1's DOWN call right) and UP afterwards.
function fixture({ days = 30, improved = 30, extraPerDay = 0, storedMismatch = false, asOf = null } = {}) {
  const archive = [];
  const btc = [];
  let price = 100;
  for (let d = 0; d <= days + 2; d++) {
    const t = DAY0 + d * DAY + 12 * HOUR;
    btc.push({ ts: t, btc_price: price });
    price = d < improved ? price * 0.99 : price * 1.01;
    if (d < days) {
      for (let j = 0; j <= extraPerDay; j++) {
        const ts = t + j * 60000;
        archive.push({ ts, score: storedMismatch ? 40 : 53, sources_json: JSON.stringify(readings()) });
      }
    }
  }
  const lastDayEnd = DAY0 + days * DAY;
  return {
    v1: { manifest: { as_of_ms: asOf ?? lastDayEnd + 40 * HOUR, source: 'synthetic' }, archive, history: [] },
    btc: { manifest: { as_of_ms: asOf ?? lastDayEnd + 40 * HOUR, source: 'synthetic' }, rows: btc },
  };
}
const ta1 = (out) => out.candidates.find((c) => c.id === 'T-A1');
const ta2 = (out) => out.candidates.find((c) => c.id === 'T-A2');

function keysDeep(o, acc = new Set()) {
  if (o && typeof o === 'object') for (const [k, v] of Object.entries(o)) { acc.add(k); keysDeep(v, acc); }
  return acc;
}
function deepFreeze(o) { if (o && typeof o === 'object') { Object.freeze(o); Object.values(o).forEach(deepFreeze); } return o; }

describe('plan and pins', () => {
  it('runs only on the frozen addendum, the unchanged registration and the bdfb2cb validator', () => {
    expect(verifyPins()).toBe(true);
    const dir = mkdtempSync(join(tmpdir(), 'pins-'));
    for (const f of [ADDENDUM_FILE, REGISTRATION_FILE, ...Object.keys(PINNED_BLOBS)]) {
      mkdirSync(join(dir, dirname(f)), { recursive: true });
      copyFileSync(join(ROOT, f), join(dir, f));
    }
    expect(verifyPins(dir)).toBe(true);
    writeFileSync(join(dir, ADDENDUM_FILE), readFileSync(join(ROOT, ADDENDUM_FILE), 'utf8').replace('"units_required": 30', '"units_required": 20'));
    expect(() => verifyPins(dir)).toThrow(/frozen addendum/);
    copyFileSync(join(ROOT, ADDENDUM_FILE), join(dir, ADDENDUM_FILE));
    writeFileSync(join(dir, 'learning/learning-method.js'), `${readFileSync(join(ROOT, 'learning/learning-method.js'), 'utf8')}\n`);
    expect(() => verifyPins(dir)).toThrow(/bdfb2cb/);
  });
  it('the addendum fixes the plan values the evaluator uses', () => {
    const a = JSON.parse(readFileSync(join(ROOT, ADDENDUM_FILE), 'utf8'));
    expect(a.holdout.boundary_ms).toBe(BOUNDARY_MS);
    expect(a.fixed_horizon.units_required).toBe(UNITS_REQUIRED);
    expect(a.evaluator_implementation.commit).toBe('bdfb2cb21a2e0814273d276a9bd84b09d74625a4');
    expect(a.evaluator_implementation.files_git_blob_sha1).toEqual(PINNED_BLOBS);
    expect(a.parent_registration.sha256_canonical).toBe('66067b324743bad052f4404d7ecbaaac76e6678116026ddd22dc8d73b22a266d');
    expect(a.not_changed).toContain('46c0d52b47350a7c7e83841e83ff646d8e5d0f5b9146d98908dab32411ff3e93');
  });
});

describe('holdout boundary and timestamps', () => {
  it('uses observations strictly after 2026-10-09T16:00:00Z only', () => {
    const r = JSON.stringify(readings());
    const { holdout, counts } = mergeObservations([{ ts: BOUNDARY_MS - 1, score: 53, sources_json: r }, { ts: BOUNDARY_MS, score: 53, sources_json: r }, { ts: BOUNDARY_MS + 1, score: 53, sources_json: r }], []);
    expect(holdout.map((o) => o.ts)).toEqual([BOUNDARY_MS + 1]);
    expect(counts.at_or_before_boundary).toBe(2);
  });
  it('excludes and counts invalid timestamps, duplicates and unparsable readings (archive row wins)', () => {
    const r = JSON.stringify(readings());
    const t = BOUNDARY_MS + HOUR;
    const { holdout, counts } = mergeObservations(
      [{ ts: null, score: 1, sources_json: r }, { ts: 'x', score: 1, sources_json: r }, { ts: Number.NaN, score: 1, sources_json: r }, { ts: t, score: 53, sources_json: r }, { ts: t + 1, score: 53, sources_json: '{bad' }],
      [{ ts: t, score: 99, sources_json: r }],
    );
    expect(counts.invalid_timestamp).toBe(3);
    expect(counts.duplicate_timestamp).toBe(1);
    expect(counts.unparsable_sources_json).toBe(1);
    expect(holdout.find((o) => o.ts === t).stored).toBe(53);
  });
  it('a candidate whose creation-time boundary data are all pre-boundary has no units', () => {
    const f = fixture({ days: 30 });
    f.v1.archive = f.v1.archive.map((o) => ({ ...o, ts: o.ts - 40 * DAY }));
    const out = evaluate(f);
    expect(out.observation_counts.after_boundary).toBe(0);
    expect(ta1(out).status).toBe('AWAITING_HOLDOUT');
  });
});

describe('independent disagreement-day units', () => {
  it('counts at most one unit per UTC day however many disagreeing observations it holds', () => {
    const out = evaluate(fixture({ days: 10, extraPerDay: 5 }));
    expect(out.days.every((d) => d.observations === 6)).toBe(true);
    expect(ta1(out).units_accumulated).toBe(10);
    expect(ta2(out).units_accumulated).toBe(0);           // its call never changes in this fixture
  });
  it('unsettled days supply no unit (incomplete days)', () => {
    const f = fixture({ days: 30 });
    const early = DAY0 + 30 * DAY + 20 * HOUR;               // < end(last day) + 36h
    f.v1.manifest.as_of_ms = early; f.btc.manifest.as_of_ms = early;
    const out = evaluate(f);
    expect(ta1(out).units_accumulated).toBe(29);
    expect(ta1(out).status).toBe('AWAITING_HOLDOUT');
    expect(out.days[29].settled).toBe(false);
  });
  it('observations with an unresolved outcome (BTC gap) are not units', () => {
    const f = fixture({ days: 30 });
    const gap = DAY0 + 5 * DAY + 12 * HOUR + DAY;            // day 5's 24h-ahead price is also day 6's starting price
    f.btc.rows = f.btc.rows.filter((r) => r.ts !== gap);
    const out = evaluate(f);
    expect(ta1(out).units_accumulated).toBe(28);
    expect(ta1(out).exclusions.outcome_unresolved_or_flat).toBe(2);
  });
  it('an observation missing the target source can never be a unit', () => {
    const f = fixture({ days: 3 });
    f.v1.archive = f.v1.archive.map((o) => { const r = JSON.parse(o.sources_json); delete r.fng; return { ...o, sources_json: JSON.stringify(r) }; });
    const out = evaluate(f);
    expect(ta1(out).units_accumulated).toBe(0);
    expect(ta1(out).exclusions.target_source_missing).toBe(3);
  });
});

describe('fixed horizon: exactly 30 units, no interim results', () => {
  const FORBIDDEN = ['hits', 'accuracy_pct', 'improved', 'worsened', 'p_better', 'p_worse', 'research_claim', 'product_level', 'research_level', 'vs_stored_v1', 'effect', 'current_v1', 'adjusted_v1', 'stored_v1'];
  it('29 units: AWAITING_HOLDOUT and no statistic anywhere in the output', () => {
    const out = evaluate(fixture({ days: 29 }));
    expect(ta1(out).units_accumulated).toBe(29);
    expect(ta1(out).status).toBe('AWAITING_HOLDOUT');
    expect(ta1(out).look).toBeNull();
    const keys = keysDeep(out);
    for (const k of FORBIDDEN) expect(keys.has(k)).toBe(false);
    expect(JSON.stringify(out)).not.toMatch(/SUPPORTED|INCONCLUSIVE|PASS|FAIL/);
  });
  it('30 units: evaluated once, on exactly 30 independent days', () => {
    const out = evaluate(fixture({ days: 30, improved: 30 }));
    const c = ta1(out);
    expect(c.status).toBe('EVALUATED');
    expect(c.look.product_level.independent.days).toBe(30);
    expect(c.look.research_level.independent.days).toBe(30);
    expect(c.look.effect).toMatchObject({ units: 30, improved: 30, worsened: 0 });
    expect(c.look.research_claim).toBe('SUPPORTED');
    expect(ta2(out).status).toBe('AWAITING_HOLDOUT');      // each candidate on its own horizon
  });
  it('more than 30 units: the look still uses only the days through the 30th unit', () => {
    const at30 = evaluate(fixture({ days: 30, improved: 30 }));
    const at40 = evaluate(fixture({ days: 40, improved: 30 }));   // days 31-40 are all T-A1 failures
    expect(ta1(at40).units_accumulated).toBe(30);
    expect(ta1(at40).look.analysis_window.to_exclusive_utc).toBe('2026-11-09T00:00:00.000Z');
    expect(ta1(at40).look).toEqual(ta1(at30).look);
  });
  it('no look-ahead: later observations and prices do not change the look', () => {
    const a = evaluate(fixture({ days: 30, improved: 20 }));
    const f = fixture({ days: 30, improved: 20 });
    f.v1.archive.push({ ts: DAY0 + 31 * DAY, score: 10, sources_json: JSON.stringify(readings({ fng: 5 })) });
    f.btc.rows.push({ ts: DAY0 + 40 * DAY, btc_price: 1 });
    f.v1.manifest.as_of_ms += 20 * DAY; f.btc.manifest.as_of_ms += 20 * DAY;
    expect(ta1(evaluate(f)).look).toEqual(ta1(a).look);
  });
});

describe('thresholds as registered', () => {
  it('20 of 30 passes the product rule (alpha 0.1) but not the research claim (alpha 0.1/3)', () => {
    const c = ta1(evaluate(fixture({ days: 30, improved: 20 })));
    expect(c.look.product_level.status).toBe('SUPPORTED');
    expect(c.look.research_level.status).toBe('INCONCLUSIVE');
    expect(c.look.research_claim).toBe('INCONCLUSIVE');
  });
  it('21 of 30 meets the research claim', () => {
    const c = ta1(evaluate(fixture({ days: 30, improved: 21 })));
    expect(c.look.research_claim).toBe('SUPPORTED');
    expect(c.look.effect.ci95_clopper_pearson.lower).toBeGreaterThan(0.5);
  });
  it('9 of 30 (21 worse) is NOT_SUPPORTED at the research level', () => {
    expect(ta1(evaluate(fixture({ days: 30, improved: 9 }))).look.research_claim).toBe('NOT_SUPPORTED');
  });
  it('Clopper-Pearson matches known values', () => {
    expect(clopperPearson(21, 30)).toEqual({ lower: 0.506, upper: 0.8527 });
    expect(clopperPearson(0, 30).lower).toBe(0);
    expect(clopperPearson(30, 30).upper).toBe(1);
  });
});

describe('reconstruction mismatch safeguard (deployed rule kept)', () => {
  it('a win that rests on days where the stored V1 call differs from the reconstruction is not counted', () => {
    const out = evaluate(fixture({ days: 30, improved: 30, storedMismatch: true }));
    const c = ta1(out);
    expect(out.days.every((d) => d.reconstruction_call_mismatch === 1)).toBe(true);
    expect(c.look.product_level.reconstruction_mismatch.independent_days).toBe(30);
    expect(c.look.product_level.status).toBe('INCONCLUSIVE');
    expect(c.look.research_claim).toBe('INCONCLUSIVE');
  });
});

describe('reproducibility and integrity', () => {
  it('identical immutable inputs give identical output; inputs are not mutated', () => {
    const f = deepFreeze(fixture({ days: 30, improved: 25 }));
    const a = JSON.stringify(evaluate(f));
    const b = JSON.stringify(evaluate(JSON.parse(JSON.stringify(f))));
    expect(a).toBe(b);
  });
  it('a settled day that changes between records is an INTEGRITY_CONFLICT and blocks the look', () => {
    const first = evaluate(fixture({ days: 30 }));
    const f = fixture({ days: 30 });
    f.v1.archive[3] = { ...f.v1.archive[3], score: 54 };
    const out = evaluate({ ...f, previous: first });
    expect(ta1(out).status).toBe('INTEGRITY_CONFLICT');
    expect(ta1(out).look).toBeNull();
  });
});

describe('isolation: no D1, no methodology activation', () => {
  it('the evaluator only reads files and imports the pinned validator', () => {
    const src = readFileSync(join(ROOT, 'research/v1_prospective_eval.mjs'), 'utf8');
    const imports = [...src.matchAll(/from '([^']+)'/g)].map((m) => m[1]);
    expect(imports.every((i) => i.startsWith('node:') || i === '../learning/learning-method.js')).toBe(true);
    for (const bad of ['env.DB', '.prepare(', 'INSERT', 'UPDATE ', 'DELETE ', 'fetch(', 'wrangler', 'v1_methodology_versions', 'learning_candidates', 'effective_ts', 'decideCandidate', 'createCandidate']) {
      expect(src.includes(bad), bad).toBe(false);
    }
  });
});
