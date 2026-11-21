// Sentiment V2 forecast family, RESEARCH STAGE (owner change request + decisions 2026-11-21): measured-accuracy readings
// with smooth shrinkage, point-in-time inputs, per-source influence on the research score, disagreement kept, missing
// inputs never fabricated, baseline untouched.
import { describe, it, expect } from 'vitest';
import { makeD1 } from './replay/d1-sqlite.mjs';
import { extractFunctions, evalInScope } from './helpers/extract.js';
import { mig } from './helpers/glm-fixture.js';
import { fetchChronosLedger } from '../glm/adapters.js';
import * as F from '../sentiment-v2/forecast-family.js';

const H = 3600e3, D = 24 * H, T = Date.UTC(2026, 10, 21, 10);
const BASE = { sentiment_score: 46, composite_before_events: 48, events: { adjustment: -2 },
  families: { a: { score: 40 }, b: { score: 56 }, c: { score: null } } };

describe('measured-accuracy reading with smooth shrinkage', () => {
  it('no evidence -> 50; UP/DOWN symmetric; raw, adjusted and sample count exposed', () => {
    expect(F.sv2fReading('UP', 0, 0)).toMatchObject({ reading: 50, resolved_n: 0, raw_hit_rate: null, adjusted_hit_rate: 0.5 });
    const up = F.sv2fReading('UP', 18, 30), down = F.sv2fReading('DOWN', 18, 30);
    expect(up).toMatchObject({ resolved_n: 30, hits: 18, raw_hit_rate: 0.6, adjusted_hit_rate: 0.56, reading: 56 });
    expect(down.reading).toBe(44);
    expect(F.sv2fReading('NEUTRAL', 18, 30).reading).toBe(50);
  });
  it('no abrupt transition at 30 observations; small samples stay near neutral', () => {
    // one more correct forecast moves the reading by under a point on either side of n = 30 (no step at 30)
    const step = (n, h) => F.sv2fReading('UP', h + 1, n + 1).reading - F.sv2fReading('UP', h, n).reading;
    for (const n of [28, 29, 30, 31]) expect(step(n, Math.round(0.7 * n))).toBeLessThan(1);
    expect(Math.abs(step(29, 20) - step(30, 21))).toBeLessThan(0.1);
    expect(F.sv2fReading('UP', 2, 3).reading).toBeLessThan(54);   // 2/3 correct on 3 forecasts barely moves the reading
    expect(F.sv2fReading('UP', 1, 1).reading).toBeLessThan(53);
  });
});

describe('point-in-time sources', () => {
  const pred = (o) => ({ ts: T - H, p_up: 0.62, calibrated_p_up: 0.55, model_version: 'knn-core-v1', realized_up: null, resolved_ts: null, ...o });
  it('production model: only rows at or before t; only outcomes resolved at or before t; one model version', () => {
    const rows = [pred({}),
      ...Array.from({ length: 20 }, (_, i) => pred({ ts: T - (30 + i) * H, p_up: 0.6, realized_up: 1, resolved_ts: T - (6 + i) * H })),
      pred({ ts: T - 40 * H, p_up: 0.6, realized_up: 0, resolved_ts: T + H }),                      // resolved after t: ignored
      pred({ ts: T - 41 * H, p_up: 0.6, realized_up: 0, resolved_ts: T - 2 * H, model_version: 'old' }), // other version
      pred({ ts: T + H, p_up: 0.1 })];                                                               // made after t: ignored
    const s = F.sv2fModelSource(rows, T, 'p_up');
    expect(s).toMatchObject({ state: 'fresh', direction: 'UP', resolved_n: 20, hits: 20, p_up: 0.62, model_version: 'knn-core-v1' });
    expect(s.reading).toBeCloseTo(75, 1);    // (20+10)/(20+20)
    expect(F.sv2fModelSource(rows, T + 2 * H, 'p_up')).toMatchObject({ state: 'fresh', p_up: 0.1 });   // the T+H row is now usable
    expect(F.sv2fModelSource([pred({ ts: T - 7 * H })], T, 'p_up')).toMatchObject({ state: 'stale' });
    expect(F.sv2fModelSource([pred({ calibrated_p_up: null })], T, 'calibrated_p_up')).toMatchObject({ state: 'missing' });
  });
  it('forecast source: live only (target after t, issued <= t, <= 36 h old); accuracy from resolved rows', () => {
    const f = (o) => ({ id: 'x', issued_ts: T - 3 * H, target_ts: T + 21 * H, direction: 'DOWN', model_version: 'm', correct: null, resolved_ts: null, ...o });
    const rows = [f({}), f({ id: 'r1', issued_ts: T - 50 * H, target_ts: T - 26 * H, correct: 1, resolved_ts: T - 25 * H }),
      f({ id: 'r2', issued_ts: T - 74 * H, target_ts: T - 50 * H, correct: true, resolved_ts: T - 49 * H }),
      f({ id: 'fut', issued_ts: T + H, direction: 'UP' })];
    expect(F.sv2fForecastSource(rows, T)).toMatchObject({ state: 'fresh', forecast_id: 'x', direction: 'DOWN', resolved_n: 2, hits: 2 });
    expect(F.sv2fForecastSource([f({ issued_ts: T - 40 * H, target_ts: T + H })], T)).toMatchObject({ state: 'stale' });
    expect(F.sv2fForecastSource([f({ target_ts: T - H })], T)).toMatchObject({ state: 'missing' });
    expect(F.sv2fForecastSource([f({ direction: null })], T)).toMatchObject({ state: 'invalid' });
  });
  it('GLM: only an ELIGIBLE directional signal valid at t adjusts, bounded by 5 points x strength', () => {
    const s = (o) => ({ signal_id: 's', decision_cutoff_ts: T - 3 * H, valid_from_ts: T - 3 * H, valid_until_ts: T + 21 * H, direction: 'UP', strength: 'moderate', eligibility: 'ELIGIBLE', ...o });
    expect(F.sv2fGlm([s({})], T)).toMatchObject({ state: 'applied', adjustment: 3.33 });
    expect(F.sv2fGlm([s({ strength: 'strong', direction: 'DOWN' })], T).adjustment).toBe(-5);
    expect(F.sv2fGlm([s({ eligibility: 'CANDIDATE' })], T)).toMatchObject({ state: 'not_eligible', adjustment: 0 });
    expect(F.sv2fGlm([s({ direction: 'NO_SIGNAL' })], T)).toMatchObject({ state: 'no_signal', adjustment: 0 });
    expect(F.sv2fGlm([s({ valid_until_ts: T - 1 })], T)).toMatchObject({ state: 'expired', adjustment: 0 });
    expect(F.sv2fGlm([s({ decision_cutoff_ts: T + H })], T)).toMatchObject({ state: 'missing', adjustment: 0 });
  });
});

describe('research score: every source can move it; nothing fabricated; baseline untouched', () => {
  const fresh = (dir, hits, n) => ({ state: 'fresh', ...F.sv2fReading(dir, hits, n) });
  const none = { state: 'missing', reason: 'x' };
  const all = (o = {}) => ({ model_original: fresh('UP', 30, 40), model_calibrated: fresh('UP', 25, 40), forecast_timesfm: fresh('DOWN', 12, 20), forecast_chronos: fresh('UP', 6, 10), ...o });
  const noGlm = { state: 'missing', adjustment: 0 };
  it('no forecast input and no GLM -> enhanced equals the baseline exactly', () => {
    const r = F.sv2ForecastResearch(BASE, { model_original: none, model_calibrated: none, forecast_timesfm: none, forecast_chronos: none }, noGlm);
    expect(r).toMatchObject({ status: 'no_forecast_inputs', enhanced_score: 46, family_score: null });
    expect(r.missing).toHaveLength(4);
  });
  it.each(['model_original', 'model_calibrated', 'forecast_timesfm', 'forecast_chronos'])('changing %s changes the research score', (k) => {
    const a = F.sv2ForecastResearch(BASE, all(), noGlm).enhanced_score;
    const flipped = all(); flipped[k] = fresh(flipped[k].direction === 'UP' ? 'DOWN' : 'UP', flipped[k].hits, flipped[k].resolved_n);
    expect(F.sv2ForecastResearch(BASE, flipped, noGlm).enhanced_score).not.toBe(a);
  });
  it('the family joins the equal-weight average; contributions add up; disagreement is preserved', () => {
    const r = F.sv2ForecastResearch(BASE, all(), noGlm);
    const fam = r.family_score;
    expect(r.enhanced_score).toBeCloseTo((48 * 2 + fam) / 3 - 2, 1);
    const sumContrib = Object.entries(r.contributions).filter(([k]) => k !== 'glm').reduce((s, [, v]) => s + v, 0);
    expect(sumContrib).toBeCloseTo((r.family_score - 50) / 3, 1);
    expect(r.agreement).toEqual({ up: ['model_original', 'model_calibrated', 'forecast_chronos'], down: ['forecast_timesfm'], neutral: [] });
    expect(r.baseline_score).toBe(46);
  });
  it('GLM changes the research score only when it applies; result is clamped to 0-100', () => {
    const g = { state: 'applied', adjustment: -5 };
    expect(F.sv2ForecastResearch(BASE, all(), g).enhanced_score).toBeCloseTo(F.sv2ForecastResearch(BASE, all(), noGlm).enhanced_score - 5, 2);
    const only = F.sv2ForecastResearch(BASE, { model_original: none, model_calibrated: none, forecast_timesfm: none, forecast_chronos: none }, g);
    expect(only).toMatchObject({ status: 'ok', enhanced_score: 41, contributions: { glm: -5 } });
    expect(F.sv2ForecastResearch({ ...BASE, composite_before_events: 99 }, all({ forecast_timesfm: fresh('UP', 20, 20) }), { state: 'applied', adjustment: 5 }).enhanced_score).toBeLessThanOrEqual(100);
  });
  it('does not mutate the baseline result', () => {
    const b = JSON.parse(JSON.stringify(BASE));
    F.sv2ForecastResearch(b, all(), { state: 'applied', adjustment: 5 });
    expect(b).toEqual(BASE);
  });
});

describe('worker: point-in-time loading on real SQLite and the cycle wiring', () => {
  it('loads predictions, TimesFM and GLM in one batch and computes per decision', async () => {
    const d1 = makeD1([mig('0002_experiment_4_timesfm.sql'), mig('0003_experiment_4_fix_horizon_steps_column.sql'), mig('0031_glm_signal.sql'),
      'CREATE TABLE predictions (id INTEGER PRIMARY KEY, ts INTEGER, horizon_hours INTEGER, p_up REAL, calibrated_p_up REAL, model_version TEXT, realized_up INTEGER, resolved_ts INTEGER)']);
    d1.db.prepare('INSERT INTO predictions (ts, horizon_hours, p_up, calibrated_p_up, model_version, realized_up, resolved_ts) VALUES (?,?,?,?,?,?,?)').run(T - H, 24, 0.3, 0.45, 'knn-core-v1', null, null);
    d1.db.prepare('INSERT INTO predictions (ts, horizon_hours, p_up, calibrated_p_up, model_version, realized_up, resolved_ts) VALUES (?,?,?,?,?,?,?)').run(T - 30 * H, 24, 0.3, 0.45, 'knn-core-v1', 0, T - 5 * H);
    d1.db.prepare(`INSERT INTO experiment_4_timesfm (coin, horizon_hours, ts, input_end_ts, target_ts, price_at_prediction, direction, model_version, checkpoint, inference_backend, context_length, forecast_horizon_steps, features_used)
      VALUES ('BTC',24,?,?,?,63000,'UP','timesfm-2.5','c','cpu',128,8,'u')`).run(T - 2 * H, T - 3 * H, T + 22 * H);
    const S = evalInScope(extractFunctions('sv2LoadForecastInputs', 'sv2ForecastResearchFor'),
      { ...F, fetchChronosLedger, fetch: async () => ({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ forecasts: [
        { id: 'c1', coin: 'BTC', horizon_hours: 24, issued_ts: T - 4 * H, target_ts: T + 20 * H, direction: 'DOWN', model_id: 'amazon/chronos-2', model_revision_resolved: 'r' }] }) }) });
    const inp = await S.sv2LoadForecastInputs({ DB: d1.binding }, T, T);
    expect(inp).toMatchObject({ chronos_error: null });
    expect(inp.predictions).toHaveLength(2);
    const r = S.sv2ForecastResearchFor(BASE, T, inp);
    expect(r.sources).toMatchObject({ model_original: { state: 'fresh', direction: 'DOWN', resolved_n: 1, hits: 1 }, model_calibrated: { state: 'fresh', direction: 'DOWN' },
      forecast_timesfm: { state: 'fresh', direction: 'UP', resolved_n: 0, reading: 50 }, forecast_chronos: { state: 'fresh', direction: 'DOWN', model_version: 'amazon/chronos-2@r' } });
    expect(r.glm.state).toBe('missing');
    expect(r.enhanced_score).not.toBe(BASE.sentiment_score);
    // a failed load is recorded, never fabricated
    expect(S.sv2ForecastResearchFor(BASE, T, { error: 'D1 down' })).toMatchObject({ status: 'unavailable', enhanced_score: null, baseline_score: 46 });
  });
  it('the cycle stores forecast_research beside the unchanged sentiment_score', async () => {
    const { readFileSync } = await import('node:fs');
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const row = src.slice(src.indexOf('function sv2Row('), src.indexOf('const SV2_INSERT_SQL'));
    expect(row).toMatch(/result\.sentiment_score, result\.sentiment_class/);           // production score column = baseline
    expect(row).toMatch(/forecast_research: result\.forecast_research/);                 // research beside it, in family_json
    const cyc = src.slice(src.indexOf('async function runSentimentV2Cycle('), src.indexOf('async function runSentimentV2Cycle(') + 12000);
    expect(cyc).toMatch(/result\.forecast_research = sv2ForecastResearchFor\(result, ts, forecastInputs\)/);
    expect(cyc).not.toMatch(/result\.sentiment_score\s*=/);
  });
});

describe('comparison API: baseline vs research, GLM trigger and Neuron measurements', () => {
  it('reads bounded rows, keeps the baseline separate and summarises deltas, sources and GLM runs', async () => {
    const { extractConstants } = await import('./helpers/extract.js');
    const d1 = makeD1([mig('0027_sentiment_v2_results.sql'), mig('0029_sv2_evidence_and_ai_budget.sql'), mig('0031_glm_signal.sql')]);
    const ins = d1.db.prepare(`INSERT INTO sentiment_v2_results (asset, decision_ts, calc_version, status, sentiment_score, sentiment_class, confidence, ai_status, family_json, created_ts)
      VALUES ('BTC', ?, 'sv2-1.1.0', 'OK', ?, 'neutral', 0.5, 'disabled', ?, ?)`);
    ins.run(T - H, 50, JSON.stringify({ families: {}, forecast_research: { enhanced_score: 47.5, sources: { forecast_timesfm: { state: 'fresh' }, forecast_chronos: { state: 'missing' } }, glm: { state: 'applied' } } }), T);
    ins.run(T - 2 * H, 46, JSON.stringify({ families: {} }), T);   // older row without research: shown, not fabricated
    d1.db.prepare(`INSERT INTO glm_runs (run_id, asset, decision_cutoff_ts, started_ts, package_hash, input_version, prompt_version, schema_version, status, reason, n_items, n_excluded, package_bytes, input_refs_json, missing_json, rule_version)
      VALUES ('r1','BTC',?,?,'h1','i','p','s','skipped_budget','trigger:tfm:9; counterpart chronos missing; BUDGET_EXHAUSTED: x',0,0,0,'[]','[]','q')`).run(T - H, T);
    d1.db.prepare("INSERT INTO sv2_ai_neurons (day, slot, reserved_ts, model, reserved_neurons, actual_neurons, status) VALUES (?,1,?,?,206,70,'ok')").run(new Date(T).toISOString().slice(0, 10), T, 'm');
    const S = evalInScope(extractConstants('SENTIMENT_V2_VERSION', 'GLM_DAILY_NEURONS_MAX') + '\n' + extractFunctions('getSv2ForecastResearch', 'sv2Classify'), { SV2F_VERSION: F.SV2F_VERSION });
    const r = await S.getSv2ForecastResearch({ DB: d1.binding, GLM_DAILY_NEURONS: '300' }, T, 48);
    expect(r).toMatchObject({ ok: true, stage: 'research', summary: { decisions: 2, with_research: 1, mean_delta: -2.5, class_changes: 0, glm_applied_decisions: 1,
      source_states: { forecast_timesfm: { fresh: 1 }, forecast_chronos: { missing: 1 } } } });
    expect(r.points[0]).toMatchObject({ baseline_score: 50, research: { enhanced_score: 47.5 } });
    expect(r.points[1]).toMatchObject({ baseline_score: 46, research: null });
    expect(r.glm_runs_7d).toEqual([{ status: 'skipped_budget', origin: 'timesfm', n: 1 }]);
    expect(r.neurons_7d).toMatchObject({ glm_daily_budget: 300, days: [{ calls: 1, reserved: 206, actual: 70, committed: 70, account_limited: 0 }] });
  });
});
