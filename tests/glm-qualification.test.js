// GLM shadow-mode operational qualification (owner instruction 2026-11-19): bounded sentiment read, measured write rows,
// machine-readable eligibility/blockers, and the staging-only tick probe (gated, idempotent, shadow, zero-cost scope).
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeD1 } from './replay/d1-sqlite.mjs';
import { CUT, H, V1_DDL, V2_DDL, seed, mig } from './helpers/glm-fixture.js';
import { GLM_READ_QUERIES } from '../glm/adapters.js';
import { GLM_ELIGIBILITY, glmInferenceBlockers } from '../glm/contract.js';
import { glmTickProbe, glmStatus, glmStagingProbesEnabled } from '../glm/runner.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SNAP_DDL = 'CREATE TABLE prediction_snapshots (coin TEXT NOT NULL, horizon_hours INTEGER NOT NULL, computed_ts INTEGER, prediction_ts INTEGER, body_json TEXT, PRIMARY KEY (coin, horizon_hours))';
let d1;
beforeEach(() => { d1 = makeD1([...V1_DDL, ...V2_DDL, SNAP_DDL, mig('0031_glm_signal.sql')]); seed(d1.db); });

describe('bounded sentiment read', () => {
  it('v2_sentiment never sorts the whole asset in a temp B-tree (reverse rowid scan, LIMIT 1)', () => {
    const q = GLM_READ_QUERIES.find((x) => x.key === 'v2_sentiment');
    expect(q.sql).toMatch(/NOT INDEXED/);
    expect(q.sql).toMatch(/ORDER BY result_id DESC LIMIT 1$/);
    const plan = d1.db.prepare('EXPLAIN QUERY PLAN ' + q.sql).all(...q.args(CUT)).map((r) => r.detail).join(' | ');
    expect(plan).not.toMatch(/TEMP B-TREE/);
  });
});

describe('eligibility evidence and machine-readable blockers', () => {
  it('model, availability, free plan and MIT licence verified with sources; V1 Neurons and CPU are blockers', () => {
    for (const k of ['model_id', 'availability', 'free_plan', 'licence', 'pricing', 'free_allocation', 'rate_limit']) {
      expect(GLM_ELIGIBILITY[k].verified, k).toBe(true);
      expect(GLM_ELIGIBILITY[k].source, k).toBeTruthy();
    }
    expect(GLM_ELIGIBILITY.licence.value).toBe('MIT');
    expect(glmInferenceBlockers()).toEqual(['v1_neuron_usage_unmeasured', 'worker_cpu_unverified']);
  });
  it('status API exposes eligibility and blocker codes (inference and tick hook off in production)', async () => {
    const st = await glmStatus({ DB: d1.binding, GLM_SIGNAL_ENABLED: '1' });
    expect(st.blocker_codes).toEqual(['v1_neuron_usage_unmeasured', 'worker_cpu_unverified', 'inference_disabled', 'tick_hook_disabled']);
    expect(st.eligibility.licence.value).toBe('MIT');
    expect(st.blockers[0]).toMatch(/MIT licence are verified/);
  });
});

describe('staging-only tick probe', () => {
  it('is refused unless GLM_STAGING_PROBES = "1"', async () => {
    const r = await glmTickProbe({ DB: d1.binding }, { now: CUT + 2 * H });
    expect(r).toMatchObject({ ok: false, skipped: true });
    expect(d1.log).toEqual([]);
    expect(glmStagingProbesEnabled({ GLM_STAGING_PROBES: '1' })).toBe(true);
  });
  it('measures calls/rows, is idempotent, keeps the baseline (shadow forced), and ETH costs zero queries', async () => {
    const r = await glmTickProbe({ DB: d1.binding, GLM_STAGING_PROBES: '1', GLM_INFLUENCE_CAP: '0.1' }, { now: CUT + 2 * H, baseline_p_up: 0.53 });
    expect(r.ok).toBe(true);
    expect(r.first.d1.calls).toBe(2);
    expect(r.first.d1.first_calls).toBe(0);
    expect(r.second.d1.calls).toBe(2);
    expect(r.second.d1.rows_written).toBe(0);
    expect(r.out_of_scope_eth.d1.calls).toBe(0);
    expect(r.ledger_rows_for_ts).toBe(1);
    expect(r.first.glm).toMatchObject({ mode: 'shadow', baseline_p_up: 0.53, baseline_source: 'cycle_p_up', adjustment: 0, final_p_up: 0.53 });
    const row = d1.db.prepare('SELECT chosen_variant, mode, applied FROM glm_adjustments').get();
    expect(row).toEqual({ chosen_variant: 'staging_probe', mode: 'shadow', applied: 0 });
  });
  it('never touches predictions or snapshots', async () => {
    const before = d1.db.prepare('SELECT (SELECT COUNT(*) FROM predictions) a, (SELECT COUNT(*) FROM prediction_snapshots) b').get();
    await glmTickProbe({ DB: d1.binding, GLM_STAGING_PROBES: '1' }, { now: CUT + 2 * H });
    expect(d1.db.prepare('SELECT (SELECT COUNT(*) FROM predictions) a, (SELECT COUNT(*) FROM prediction_snapshots) b').get()).toEqual(before);
  });
});

describe('configuration guards', () => {
  it('GLM_STAGING_PROBES exists only in the staging config; production keeps inference, tick hook and cap off', () => {
    const prod = readFileSync(join(ROOT, 'wrangler.toml'), 'utf8');
    const stg = readFileSync(join(ROOT, 'wrangler.staging.toml'), 'utf8');
    expect(prod).not.toMatch(/GLM_STAGING_PROBES/);
    expect(stg).toMatch(/^GLM_STAGING_PROBES = "1"$/m);
    for (const [k, v] of [['GLM_INFERENCE_ENABLED', '0'], ['GLM_TICK_HOOK_ENABLED', '0'], ['GLM_INFLUENCE_CAP', '0']]) expect(prod).toMatch(new RegExp(`^${k} = "${v}"$`, 'm'));
  });
  it('the admin run route gates glm_daily behind the staging flag', () => {
    const w = readFileSync(join(ROOT, 'worker.js'), 'utf8');
    expect(w).toMatch(/if \(steps\.includes\('glm_daily'\)\) out\.glm_daily = glmStagingProbesEnabled\(env\)/);
  });
});

describe('coverage accounting', () => {
  it('run usage records kept/trimmed per class and exclusion reasons', async () => {
    const { readGlmInputs } = await import('../glm/adapters.js');
    const { buildPackage } = await import('../glm/package.js');
    const { LEDGER } = await import('./helpers/glm-fixture.js');
    const raw = await readGlmInputs(d1.binding, CUT);
    const pkg = await buildPackage({ raw, cutoffTs: CUT, chronosLedger: LEDGER, baseline: null });
    const kept = Object.values(pkg.coverage.kept_by_class).reduce((a, b) => a + b, 0);
    const trimmed = Object.values(pkg.coverage.trimmed_by_class).reduce((a, b) => a + b, 0);
    expect(kept).toBe(pkg.items.length);
    expect(trimmed).toBe(pkg.trimmed);
    expect(Object.values(pkg.coverage.excluded_by_reason).reduce((a, b) => a + b, 0)).toBe(pkg.excluded.length);
    expect(pkg.coverage.excluded_by_reason.after_cutoff).toBeGreaterThan(0);
  });
});
