// GLM evidence intelligence, Phase D: the 07:00 run (flags, idempotency, inference gating, quota/timeout/invalid
// output, persistence), the per-tick bounded adjustment (shadow by default, idempotent, scope-isolated), the read-only
// API, migration 0031 and the worker/config wiring. Real SQLite (node:sqlite).
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeD1 } from './replay/d1-sqlite.mjs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';
import { H, CUT, V1_DDL, V2_DDL, seed, LEDGER, mig } from './helpers/glm-fixture.js';
import { glmDailySignalRun, glmTickAdjust, handleGlmRoutes, glmCutoff, glmFlags } from '../glm/runner.js';
import { GLM_MODEL, GLM_ADJUST } from '../glm/contract.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const WORKER = readFileSync(join(ROOT, 'worker.js'), 'utf8');
const M0031 = mig('0031_glm_signal.sql');
const SNAP_DDL = 'CREATE TABLE prediction_snapshots (coin TEXT NOT NULL, horizon_hours INTEGER NOT NULL, computed_ts INTEGER, prediction_ts INTEGER, body_json TEXT, PRIMARY KEY (coin, horizon_hours))';

const W = evalInScope(extractConstants('SV2_AI_RESERVE_SQL', 'SV2_AI_PRICING') + '\n' + extractFunctions('sv2UngroundedNumbers', 'sv2AiBudget'));
const deps = (env, extra = {}) => ({ reserveSql: W.SV2_AI_RESERVE_SQL, pricing: W.SV2_AI_PRICING, budget: W.sv2AiBudget(env),
  ungroundedNumbers: W.sv2UngroundedNumbers, fetchImpl: async () => ({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(LEDGER) }), ...extra });

const ON = { GLM_SIGNAL_ENABLED: '1', GLM_INFERENCE_ENABLED: '0', GLM_TICK_HOOK_ENABLED: '1', GLM_INFLUENCE_CAP: '0' };
// GLM's model switch is independent of SENTIMENT_V2_AI_MODEL (the per-cycle V2 annotation stays off in production).
const LIVE = { ...ON, GLM_INFERENCE_ENABLED: '1', SENTIMENT_V2_AI_MODEL: '', SV2_GLM_DAILY_NEURONS: '300' };

let d1, nowTs;
const clock = () => nowTs;
function mkEnv(vars, ai) { return { DB: d1.binding, ...(ai ? { AI: ai } : {}), ...vars }; }
function putSnapshot(db, body, computed = CUT - 59 * 60e3) {
  db.prepare('INSERT OR REPLACE INTO prediction_snapshots VALUES (?,?,?,?,?)').run('BTC', 24, computed, computed, JSON.stringify(body));
}
const SNAP_BODY = { status: 'ok', ts: CUT - H, p_up: 0.58, selection: { ok: true, chosen_variant: 'flat', cycle_p_up: 0.52, chosen_p_up: 0.5 } };

function validAnswer(over = {}) {
  return {
    status: 'completed',
    horizons: [{ horizon_hours: 24, direction: 'DOWN', strength: 'moderate', supporting_refs: ['tfm:1', 'chr:BTC_24h_live', 'ev:ev_c'], contradicting_refs: [], rationale: 'Both models point down' }],
    findings: [{ type: 'model_forecast', claim: 'TimesFM and Chronos both forecast DOWN', refs: ['tfm:1', 'chr:BTC_24h_live'] }],
    forecast_comparison: { timesfm_direction: 'DOWN', chronos_direction: 'DOWN', agreement: 'agree', note: 'aligned' },
    gemini_vs_original: [{ gemini_ref: 'gem:foufi:vid1', original_ref: 'src:youtube:vid1', relation: 'unverifiable', assessment: 'discounted', reason: 'no transcript to check against' }],
    uncertainty: 'medium', limitations: ['thin evidence'], ...over,
  };
}
const aiReturning = (answer, usage = { prompt_tokens: 5000, completion_tokens: 600 }) => ({ run: vi.fn(async () => ({ response: typeof answer === 'string' ? answer : JSON.stringify(answer), usage })) });

beforeEach(() => {
  d1 = makeD1([...V1_DDL, ...V2_DDL, SNAP_DDL, M0031]);
  seed(d1.db);
  putSnapshot(d1.db, SNAP_BODY);
  nowTs = CUT + 30e3;
});

const count = (t) => d1.db.prepare(`SELECT COUNT(*) n FROM ${t}`).get().n;
const writes = () => d1.log.filter((l) => !/^(SELECT|WITH)/i.test(l.sql));

describe('flags and cutoff', () => {
  it('defaults are off/shadow; cap is clamped to the hard maximum', () => {
    expect(glmFlags({})).toEqual({ signal: false, inference: false, tick: false, cap: 0, mode: 'shadow', allowFlip: false });
    expect(glmFlags({ GLM_INFLUENCE_CAP: '0.5' }).cap).toBe(GLM_ADJUST.hardMaxCap);
    expect(glmFlags({ GLM_INFLUENCE_CAP: 'x' }).cap).toBe(0);
  });
  it('cutoff is 07:00:00 UTC of the run day (previous day before 07:00)', () => {
    expect(glmCutoff(CUT + 5e3)).toBe(CUT);
    expect(glmCutoff(CUT - 1)).toBe(CUT - 24 * H);
  });
});

describe('07:00 daily run', () => {
  it('signal flag off: nothing read, nothing written', async () => {
    const r = await glmDailySignalRun(mkEnv({}), deps({}), { scheduledTs: CUT, now: clock });
    expect(r).toEqual({ status: 'disabled' });
    expect(d1.log).toEqual([]);
  });

  it('inference off: stores the full lineage run (model_disabled) and NO_SIGNAL for 24 h and 12 h; no AI call; bounded D1', async () => {
    const ai = aiReturning(validAnswer());
    const r = await glmDailySignalRun(mkEnv(ON, ai), deps(ON), { scheduledTs: CUT, now: clock });
    expect(r.status).toBe('model_disabled');
    expect(ai.run).not.toHaveBeenCalled();
    expect(count('glm_runs')).toBe(1);
    const run = d1.db.prepare('SELECT * FROM glm_runs').get();
    expect(run.model).toBe(null);
    expect(run.n_items).toBeGreaterThan(10);
    expect(JSON.parse(run.input_refs_json).some((x) => x.class === 'gemini_interpretation')).toBe(true);
    const sigs = d1.db.prepare('SELECT horizon_hours, direction, eligibility, reasons_json FROM glm_signals ORDER BY horizon_hours').all();
    expect(sigs).toEqual([
      { horizon_hours: 12, direction: 'NO_SIGNAL', eligibility: 'NO_SIGNAL', reasons_json: '["no_live_model_forecast"]' },
      { horizon_hours: 24, direction: 'NO_SIGNAL', eligibility: 'NO_SIGNAL', reasons_json: '["model_disabled"]' },
    ]);
    expect(r.usage.d1_calls).toBeLessThanOrEqual(5);
    expect(r.usage.d1_rows_written).toBe(3);
    expect(r.usage.d1_rows_read).toBeLessThanOrEqual(r.usage.read_rows_bound + 2);
    // only GLM tables are written
    for (const w of writes()) expect(w.sql).toMatch(/glm_runs|glm_signals/);
  });

  it('idempotent: an identical package is never processed twice', async () => {
    await glmDailySignalRun(mkEnv(ON), deps(ON), { scheduledTs: CUT, now: clock });
    const r2 = await glmDailySignalRun(mkEnv(ON), deps(ON), { scheduledTs: CUT, now: clock });
    expect(r2.status).toBe('duplicate_package');
    expect(count('glm_runs')).toBe(1);
    expect(count('glm_signals')).toBe(2);
  });

  it('missing GLM tables: inputs_degraded, nothing written, no throw', async () => {
    d1.db.exec('DROP TABLE glm_signals');
    const r = await glmDailySignalRun(mkEnv(ON), deps(ON), { scheduledTs: CUT, now: clock });
    expect(r.status).toBe('inputs_degraded');
    expect(count('glm_runs')).toBe(0);
  });

  it('inference on: one metered call, validated output, CANDIDATE signal with would_apply; Neurons recorded', async () => {
    const ai = aiReturning(validAnswer());
    const r = await glmDailySignalRun(mkEnv(LIVE, ai), deps(LIVE), { scheduledTs: CUT, now: clock });
    expect(r.status).toBe('completed');
    expect(ai.run).toHaveBeenCalledTimes(1);
    const [model, opts] = ai.run.mock.calls[0];
    expect(model).toBe(GLM_MODEL);
    expect(opts.chat_template_kwargs).toEqual({ enable_thinking: false });
    expect(opts.max_completion_tokens).toBe(1500);
    const s = d1.db.prepare("SELECT * FROM glm_signals WHERE horizon_hours = 24").get();
    expect(s.direction).toBe('DOWN');
    expect(s.eligibility).toBe('CANDIDATE');
    expect(s.would_apply).toBeCloseTo(-(2 / 3) * GLM_ADJUST.referenceCap, 6);
    const n = d1.db.prepare('SELECT * FROM sv2_ai_neurons').all();
    expect(n).toHaveLength(1);
    expect(n[0].status).toBe('ok');
    expect(n[0].actual_neurons).toBeCloseTo((5000 * 5500 + 600 * 36400) / 1e6, 2);
    expect(n[0].reserved_neurons).toBeLessThanOrEqual(300);
    const run = d1.db.prepare('SELECT model, output_json FROM glm_runs').get();
    expect(run.model).toBe(GLM_MODEL);
    expect(JSON.parse(run.output_json).status).toBe('completed');
  });

  it('next day with only stale model forecasts -> NO_SIGNAL (no_live_model_forecast)', async () => {
    await glmDailySignalRun(mkEnv(LIVE, aiReturning(validAnswer())), deps(LIVE), { scheduledTs: CUT, now: clock });
    // next day: shift the clock and cutoff by 24 h; data identical but cutoff differs -> new package
    nowTs = CUT + 24 * H + 30e3;
    d1.db.prepare('UPDATE glm_signals SET decision_cutoff_ts = decision_cutoff_ts').run();
    // At the later cutoff the newest TimesFM row is 38 h old (> 36 h stale limit) and the newest Chronos target equals the
    // cutoff: no live model forecast, so even a well-supported view cannot qualify.
    const out = validAnswer({ horizons: [{ horizon_hours: 24, direction: 'DOWN', strength: 'moderate', supporting_refs: ['tfm:1', 'chr:BTC_24h_future_issue', 'ev:ev_c'], contradicting_refs: [], rationale: 'x' }],
      findings: [{ type: 'model_forecast', claim: 'models', refs: ['tfm:1'] }] });
    const r = await glmDailySignalRun(mkEnv(LIVE, aiReturning(out)), deps(LIVE), { scheduledTs: CUT + 24 * H, now: clock });
    expect(r.status).toBe('completed');
    const s = d1.db.prepare("SELECT eligibility, reasons_json FROM glm_signals WHERE horizon_hours = 24 ORDER BY decision_cutoff_ts DESC LIMIT 1").get();
    expect(s.eligibility).toBe('NO_SIGNAL');
    expect(JSON.parse(s.reasons_json)).toContain('no_live_model_forecast');
  });

  it('persistent direction with live forecasts on the next day -> ELIGIBLE', async () => {
    await glmDailySignalRun(mkEnv(LIVE, aiReturning(validAnswer())), deps(LIVE), { scheduledTs: CUT, now: clock });
    // add next day's fresh forecasts
    d1.db.prepare(`INSERT INTO experiment_4_timesfm (coin, horizon_hours, ts, input_end_ts, target_ts, price_at_prediction, forecast_price, predicted_return_pct,
      direction, model_version, checkpoint, inference_backend, context_length, forecast_horizon_steps, features_used) VALUES ('BTC',24,?,?,?,63000,62500,-0.8,'DOWN','timesfm-2.5','c','torch-cpu',128,8,'u')`)
      .run(CUT + 10 * H, CUT + 8 * H, CUT + 32 * H);
    const ledger2 = { ...LEDGER, forecasts: [...LEDGER.forecasts, { id: 'BTC_24h_next', coin: 'BTC', horizon_hours: 24, issued_ts: CUT + 10 * H, input_end_ts: CUT + 8 * H, target_ts: CUT + 32 * H, direction: 'DOWN' }] };
    d1.db.prepare("INSERT INTO sv2_evidence (evidence_id, source_id, family, canonical_url, title, retrieved_ts, title_hash, content_hash, collector_version) VALUES ('ev_n','fed_press','macro','https://fed.gov/n','Fed again',?,'thn','cn','v')").run(CUT + 20 * H);
    nowTs = CUT + 24 * H + 30e3;
    const out = validAnswer({ horizons: [{ horizon_hours: 24, direction: 'DOWN', strength: 'moderate', supporting_refs: ['tfm:4', 'chr:BTC_24h_next', 'ev:ev_n'], contradicting_refs: [], rationale: 'x' }],
      findings: [{ type: 'model_forecast', claim: 'models down', refs: ['tfm:4'] }], gemini_vs_original: [] });
    const r = await glmDailySignalRun(mkEnv(LIVE, aiReturning(out)), deps(LIVE, { fetchImpl: async () => ({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(ledger2) }) }), { scheduledTs: CUT + 24 * H, now: clock });
    expect(r.status).toBe('completed');
    const s = d1.db.prepare("SELECT eligibility, reasons_json FROM glm_signals WHERE horizon_hours = 24 ORDER BY decision_cutoff_ts DESC LIMIT 1").get();
    expect(s.eligibility).toBe('ELIGIBLE');
    expect(JSON.parse(s.reasons_json)).toContain('persistent_2_days');
  });

  it.each([
    ['invalid output', () => aiReturning(validAnswer({ score: 1 })), 'invalid_output', 'invalid_output'],
    ['malformed JSON', () => aiReturning('{"status":'), 'invalid_output', 'invalid_output'],
    ['quota exhausted (3036)', () => ({ run: vi.fn(async () => { throw new Error('3036: daily free allocation of neurons exceeded'); }) }), 'quota_exhausted', 'account_limited'],
    ['model unavailable (3040)', () => ({ run: vi.fn(async () => { throw new Error('3040: out of capacity'); }) }), 'model_unavailable', 'rate_limited'],
    ['unknown error', () => ({ run: vi.fn(async () => { throw new Error('boom'); }) }), 'failed', 'error'],
  ])('%s -> run %s, NO_SIGNAL, ledger %s, no retry', async (_n, mkAi, runStatus, ledgerStatus) => {
    const ai = mkAi();
    const r = await glmDailySignalRun(mkEnv(LIVE, ai), deps(LIVE), { scheduledTs: CUT, now: clock });
    expect(r.status).toBe(runStatus);
    expect(ai.run).toHaveBeenCalledTimes(1);
    expect(d1.db.prepare("SELECT direction FROM glm_signals WHERE horizon_hours = 24").get().direction).toBe('NO_SIGNAL');
    expect(d1.db.prepare('SELECT status FROM sv2_ai_neurons').get().status).toBe(ledgerStatus);
    expect(d1.db.prepare('SELECT output_json FROM glm_runs').get().output_json).toBe(null);
  });

  it('timeout -> timeout status, NO_SIGNAL, never retried', async () => {
    const ai = { run: vi.fn(() => new Promise(() => {})) };
    const r = await glmDailySignalRun(mkEnv(LIVE, ai), deps(LIVE, { aiTimeoutMs: 20 }), { scheduledTs: CUT, now: clock });
    expect(r.status).toBe('timeout');
    expect(ai.run).toHaveBeenCalledTimes(1);
  });

  it('after an account_limited day, later runs are refused without calling the model', async () => {
    d1.db.prepare("INSERT INTO sv2_ai_neurons (day, slot, reserved_ts, model, reserved_neurons, status) VALUES (?,1,?,?,50,'account_limited')").run(new Date(nowTs).toISOString().slice(0, 10), nowTs, GLM_MODEL);
    const ai = aiReturning(validAnswer());
    const r = await glmDailySignalRun(mkEnv(LIVE, ai), deps(LIVE), { scheduledTs: CUT, now: clock });
    expect(r.status).toBe('quota_exhausted');
    expect(ai.run).not.toHaveBeenCalled();
  });

  it.each([
    ['budget 0', { ...LIVE, SV2_GLM_DAILY_NEURONS: '0' }, 'skipped_budget'],
    ['budget below one call', { ...LIVE, SV2_GLM_DAILY_NEURONS: '5' }, 'skipped_budget'],
    ['wrong model', { ...LIVE, GLM_MODEL: '@cf/zai-org/glm-5.2' }, 'model_disabled'],
    ['blank model', { ...LIVE, GLM_MODEL: ' ' }, 'model_disabled'],
  ])('%s -> %s, no AI call', async (_n, vars, st) => {
    const ai = aiReturning(validAnswer());
    const r = await glmDailySignalRun(mkEnv(vars, ai), deps(vars), { scheduledTs: CUT, now: clock });
    expect(r.status).toBe(st);
    expect(ai.run).not.toHaveBeenCalled();
  });

  it('no AI binding -> model_unavailable', async () => {
    const r = await glmDailySignalRun(mkEnv(LIVE), deps(LIVE), { scheduledTs: CUT, now: clock });
    expect(r.status).toBe('model_unavailable');
  });

  it('baseline is the displayed headline at the cutoff (cycle_p_up for a non-original variant)', async () => {
    const ai = aiReturning(validAnswer());
    await glmDailySignalRun(mkEnv(LIVE, ai), deps(LIVE), { scheduledTs: CUT, now: clock });
    const user = ai.run.mock.calls[0][1].messages[1].content;
    expect(JSON.parse(user.slice(6)).header.v2_baseline).toEqual({ baseline_p_up: 0.52, baseline_source: 'cycle_p_up' });
  });

  it('a snapshot computed after the cutoff is not used as the baseline (no look-ahead)', async () => {
    putSnapshot(d1.db, SNAP_BODY, CUT + 60e3);
    const ai = aiReturning(validAnswer());
    await glmDailySignalRun(mkEnv(LIVE, ai), deps(LIVE), { scheduledTs: CUT, now: clock });
    expect(JSON.parse(ai.run.mock.calls[0][1].messages[1].content.slice(6)).header.v2_baseline).toBe(null);
  });

  it('never writes outside the GLM tables and the shared Neuron ledger; V1 and prediction tables untouched', async () => {
    const before = ['predictions', 'history', 'foufi_digest', 'sv2_evidence', 'experiment_4_timesfm', 'prediction_snapshots'].map(count);
    await glmDailySignalRun(mkEnv(LIVE, aiReturning(validAnswer())), deps(LIVE), { scheduledTs: CUT, now: clock });
    expect(['predictions', 'history', 'foufi_digest', 'sv2_evidence', 'experiment_4_timesfm', 'prediction_snapshots'].map(count)).toEqual(before);
    for (const w of writes()) expect(w.sql).toMatch(/glm_runs|glm_signals|sv2_ai_neurons/);
  });
});

describe('tick hook: bounded adjustment, shadow by default', () => {
  const pr = (o = {}) => ({ status: 'ok', persisted: true, ts: CUT + 2 * H, p_up: 0.6, ...o });
  const sel = { ok: true, chosen_variant: 'original' };
  function putSignal(o = {}) {
    d1.db.prepare(`INSERT INTO glm_signals (signal_id, run_id, asset, horizon_hours, decision_cutoff_ts, valid_from_ts, valid_until_ts, direction, strength,
      eligibility, reasons_json, reference_cap, rule_version, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
      .run(o.id || 'glm_x:24', 'glm_x', 'BTC', 24, CUT, CUT + 30e3, CUT + 24 * H, o.direction || 'UP', o.strength || 'strong', o.eligibility || 'ELIGIBLE', '[]', 0.05, 'glm-qual-0.1', CUT);
  }

  it('flag off: returns null, zero queries', async () => {
    expect(await glmTickAdjust(mkEnv({}), { coin: 'BTC', horizon: 24, predictResult: pr(), selection: sel })).toBe(null);
    expect(d1.log).toEqual([]);
  });

  it('shadow: final = baseline, would_apply recorded, one idempotent ledger row', async () => {
    putSignal();
    const g = await glmTickAdjust(mkEnv(ON), { coin: 'BTC', horizon: 24, predictResult: pr(), selection: sel, now: CUT + 2 * H });
    expect(g).toMatchObject({ mode: 'shadow', baseline_p_up: 0.6, baseline_source: 'p_up', adjustment: 0, final_p_up: 0.6, signal_status: 'ELIGIBLE', signal_id: 'glm_x:24' });
    expect(g.would_apply_adjustment).toBeCloseTo(0.05, 6);
    await glmTickAdjust(mkEnv(ON), { coin: 'BTC', horizon: 24, predictResult: pr(), selection: sel });
    expect(count('glm_adjustments')).toBe(1);
    expect(d1.log.length).toBe(6);   // 2 x (signal read + explanation read + insert)
  });

  it('active cap applies to ELIGIBLE only, bounded, baseline preserved alongside', async () => {
    putSignal();
    const g = await glmTickAdjust(mkEnv({ ...ON, GLM_INFLUENCE_CAP: '0.04' }), { coin: 'BTC', horizon: 24, predictResult: pr(), selection: sel });
    expect(g.mode).toBe('active');
    expect(g.final_p_up).toBeCloseTo(0.64, 6);
    expect(g.baseline_p_up).toBe(0.6);
    const row = d1.db.prepare('SELECT * FROM glm_adjustments').get();
    expect(row.baseline_p_up).toBe(0.6);
    expect(row.applied).toBeCloseTo(0.04, 6);
  });

  it('CANDIDATE / expired signals never apply', async () => {
    putSignal({ eligibility: 'CANDIDATE' });
    expect((await glmTickAdjust(mkEnv({ ...ON, GLM_INFLUENCE_CAP: '0.05' }), { coin: 'BTC', horizon: 24, predictResult: pr(), selection: sel })).adjustment).toBe(0);
    const g = await glmTickAdjust(mkEnv({ ...ON, GLM_INFLUENCE_CAP: '0.05' }), { coin: 'BTC', horizon: 24, predictResult: pr({ ts: CUT + 25 * H }), selection: sel });
    expect(g.adjustment).toBe(0);
    expect(g.reason).toBe('expired_or_not_yet_valid');
  });

  it('ETH / LINK / 12 h: NO_SIGNAL with zero DB queries', async () => {
    putSignal();
    d1.log.length = 0;
    for (const [coin, h] of [['ETH', 24], ['LINK', 24], ['BTC', 12]]) {
      const g = await glmTickAdjust(mkEnv({ ...ON, GLM_INFLUENCE_CAP: '0.1' }), { coin, horizon: h, predictResult: pr(), selection: sel });
      expect(g.signal_status).toBe('NO_SIGNAL');
      expect(g.adjustment).toBe(0);
    }
    expect(d1.log).toEqual([]);
  });

  it('non-persisted prediction -> null; DB failure -> NO_SIGNAL with error, never throws', async () => {
    expect(await glmTickAdjust(mkEnv(ON), { coin: 'BTC', horizon: 24, predictResult: pr({ persisted: false }), selection: sel })).toBe(null);
    d1.db.exec('DROP TABLE glm_signals');
    const g = await glmTickAdjust(mkEnv(ON), { coin: 'BTC', horizon: 24, predictResult: pr(), selection: sel });
    expect(g.signal_status).toBe('NO_SIGNAL');
    expect(g.error).toBeTruthy();
  });
});

describe('read-only research API', () => {
  const call = (path, env, method = 'GET') => { const url = new URL('https://w.example' + path); return handleGlmRoutes(new Request(url, { method }), env, url); };
  beforeEach(async () => {
    await glmDailySignalRun(mkEnv(LIVE, aiReturning(validAnswer())), deps(LIVE), { scheduledTs: CUT, now: clock });
    await glmTickAdjust(mkEnv(ON), { coin: 'BTC', horizon: 24, predictResult: { status: 'ok', persisted: true, ts: CUT + 2 * H, p_up: 0.6 }, selection: { ok: true, chosen_variant: 'original' } });
    d1.log.length = 0;
  });

  it('status, signals, run and adjustments are served; no writes; ids validated; unknown/POST rejected', async () => {
    const env = mkEnv(ON);
    const st = await (await call('/api/glm/status', env)).json();
    expect(st.flags).toMatchObject({ signal_enabled: true, inference_enabled: false, mode: 'shadow' });
    expect(st.blockers.join(' ')).toMatch(/inference disabled/);
    expect(st.last_run.status).toBe('completed');
    const sg = await (await call('/api/glm/signals?limit=500', env)).json();
    expect(sg.signals).toHaveLength(1);
    expect(sg.signals[0].direction).toBe('DOWN');
    expect(Array.isArray(sg.signals[0].reasons)).toBe(true);
    const id = st.last_run.run_id;
    const run = await (await call('/api/glm/run?id=' + id, env)).json();
    expect(run.run.input_refs.length).toBeGreaterThan(5);
    expect(run.run.output.status).toBe('completed');
    expect((await call("/api/glm/run?id=x' OR 1=1", env)).status).toBe(400);
    const adj = await (await call('/api/glm/adjustments', env)).json();
    expect(adj.adjustments[0]).toMatchObject({ baseline_p_up: 0.6, final_p_up: 0.6, mode: 'shadow' });
    expect((await call('/api/glm/nope', env)).status).toBe(404);
    expect((await call('/api/glm/status', env, 'POST')).status).toBe(405);
    expect(await call('/predict', env)).toBe(null);
    expect(writes()).toEqual([]);
  });

  it('every API read is index-backed (no full scan of GLM tables)', async () => {
    const env = mkEnv(ON);
    await call('/api/glm/signals', env);
    await call('/api/glm/adjustments', env);
    await call('/api/glm/status', env);
    for (const l of d1.log) if (/glm_/.test(l.sql)) expect((l.plan || []).join(' '), l.sql).not.toMatch(/SCAN (glm_signals|glm_adjustments|glm_runs)\b(?! USING)/);
  });

  it('tables missing -> 503/ok:false, never a crash', async () => {
    d1.db.exec('DROP TABLE glm_signals');
    expect((await call('/api/glm/signals', mkEnv(ON))).status).toBe(503);
    const st = await (await call('/api/glm/status', mkEnv(ON))).json();
    expect(st.ok).toBe(false);
  });
});

describe('migration 0031 and wiring', () => {
  it('0031 is additive only and its rollback drops exactly the three GLM tables', () => {
    const stmts = M0031.replace(/--.*$/gm, '').split(';').map((x) => x.trim()).filter(Boolean);
    for (const s of stmts) expect(s).toMatch(/^CREATE (TABLE|INDEX) IF NOT EXISTS (glm_|idx_glm_)/);
    const down = readFileSync(join(ROOT, '.ai/rollback/0031_glm_signal.down.sql'), 'utf8');
    expect(down.match(/DROP TABLE IF EXISTS (\w+)/g)).toEqual(['DROP TABLE IF EXISTS glm_adjustments', 'DROP TABLE IF EXISTS glm_signals', 'DROP TABLE IF EXISTS glm_runs']);
    const fresh = makeD1([M0031]);
    expect(fresh.db.prepare("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name LIKE 'glm_%'").get().n).toBe(3);
  });

  it('worker.js: GLM wired only at the 07:00 cron, the read-only routes and the tick chain; no Gemini call added', () => {
    expect(WORKER).toMatch(/import \{ glmDailySignalRun, glmTickAdjust, handleGlmRoutes, glmTickProbe, glmStagingProbesEnabled, glmForecastTrigger, glmRecordCoveredByDaily \} from '\.\/glm\/runner\.js';/);
    // forecast-triggered GLM (change request 2026-11-21) runs only in the hourly non-tick chain
    expect((WORKER.match(/glmForecastTrigger\(/g) || []).length).toBe(1);
    expect(WORKER).toMatch(/\.then\(\(\) => glmForecastTrigger\(env, glmDeps\(env\), \{ now: event\.scheduledTime, queryBudget: SV2_D1_QUERIES_PER_INVOCATION - SV2_D1_QUERY_RESERVE - sv2Q\.n \}\)\)/);
    // audit review 2026-11-21 (decision 3): baseline V2 and the collector run BEFORE GLM, and nothing defers them
    const chain = WORKER.slice(WORKER.indexOf("} else if (event.cron === '0 * * * *') {"), WORKER.indexOf('// No catch-all else'));
    const order = ['runAutomaticDetection', 'syncV1ArchiveFromHistory', 'runSentimentV2Cycle', 'sv2CollectGuarded', 'glmForecastTrigger'].map((f) => chain.indexOf(f));
    expect(order.every((x, i) => x > 0 && (i === 0 || x > order[i - 1]))).toBe(true);
    expect(chain).not.toMatch(/glmTriggerRan|deferred one hour/);
    const at07 = WORKER.slice(WORKER.indexOf("if (event.cron === '0 7 * * *') {"), WORKER.indexOf("if (event.cron === '0 7 * * *') {") + 2500);
    expect(at07).toMatch(/glmDailySignalRun\(env, glmDeps\(env\)/);
    // exactly two call sites: the 07:00 cron and the staging-only probe step (gated by GLM_STAGING_PROBES)
    expect((WORKER.match(/glmDailySignalRun\(/g) || []).length).toBe(2);
    expect(WORKER).toMatch(/glmStagingProbesEnabled\(env\)\n\s+\? await glmDailySignalRun\(env, glmDeps\(env\), \{ scheduledTs: now \}\)/);
    expect(WORKER).toMatch(/glmTickAdjust\(env, \{ coin, horizon, predictResult, selection: selectionForSnapshot \}\)/);
    const glmSrc = ['runner.js', 'adapters.js', 'package.js', 'signal.js', 'contract.js'].map((f) => readFileSync(join(ROOT, 'glm', f), 'utf8')).join('\n');
    expect(glmSrc).not.toMatch(/generativelanguage|callGeminiGenerateContent|GEMINI_API_KEY/);
    // stored p_up, selection and calibration inputs are never written by GLM code
    expect(glmSrc).not.toMatch(/(INSERT|UPDATE)[^;]*\b(predictions|calibration_curve|selection_decisions|prediction_snapshots)\b/);
  });

  it('production config (owner mandate 2026-11-20): inference, tick hook and a bounded cap on; V2 annotation off', () => {
    const toml = readFileSync(join(ROOT, 'wrangler.toml'), 'utf8');
    expect(toml).toMatch(/^GLM_SIGNAL_ENABLED = "1"$/m);
    expect(toml).toMatch(/^GLM_INFERENCE_ENABLED = "1"$/m);
    expect(toml).toMatch(/^GLM_TICK_HOOK_ENABLED = "1"$/m);
    expect(toml).toMatch(/^GLM_INFLUENCE_CAP = "0.03"$/m);
    expect(toml).toMatch(/^GLM_ALLOW_FLIP = "0"$/m);
    expect(toml).toMatch(/^GLM_DAILY_NEURONS = "300"$/m);
    expect(toml).toMatch(/^SENTIMENT_V2_AI_MODEL = ""$/m);
    expect(toml).toMatch(/^SV2_GLM_DAILY_NEURONS = "0"$/m);
    expect(toml.match(/crons = \[[^\]]*\]/)[0]).toBe('crons = ["0 * * * *", "0 7 * * *"]');
    const deploy = readFileSync(join(ROOT, '.github/workflows/deploy.yml'), 'utf8');
    expect(deploy).toMatch(/- 'glm\/\*\*'/);
  });
});

// ---------- Production influence (owner mandate 2026-11-20): glm-qual-0.4 ----------
import { computeAdjustment } from '../glm/signal.js';
import { glmEvaluationSummary } from '../glm/runner.js';

describe('production influence: bounded, persistent, explained, fail-safe', () => {
  const ACTIVE = { GLM_SIGNAL_ENABLED: '1', GLM_INFERENCE_ENABLED: '1', GLM_TICK_HOOK_ENABLED: '1', GLM_INFLUENCE_CAP: '0.03', GLM_ALLOW_FLIP: '0', SV2_GLM_DAILY_NEURONS: '300' };
  const sel = { ok: true, chosen_variant: 'original' };
  const pr = (o = {}) => ({ status: 'ok', persisted: true, ts: CUT + 26 * H, p_up: 0.6, ...o });
  async function twoDays(day2Ai) {
    await glmDailySignalRun(mkEnv(ACTIVE, aiReturning(validAnswer())), deps(ACTIVE), { scheduledTs: CUT, now: clock });
    d1.db.prepare(`INSERT INTO experiment_4_timesfm (coin, horizon_hours, ts, input_end_ts, target_ts, price_at_prediction, forecast_price, predicted_return_pct,
      direction, model_version, checkpoint, inference_backend, context_length, forecast_horizon_steps, features_used) VALUES ('BTC',24,?,?,?,63000,62500,-0.8,'DOWN','timesfm-2.5','c','torch-cpu',128,8,'u')`)
      .run(CUT + 10 * H, CUT + 8 * H, CUT + 32 * H);
    const ledger2 = { ...LEDGER, forecasts: [...LEDGER.forecasts, { id: 'BTC_24h_next', coin: 'BTC', horizon_hours: 24, issued_ts: CUT + 10 * H, input_end_ts: CUT + 8 * H, target_ts: CUT + 32 * H, direction: 'DOWN' }] };
    d1.db.prepare("INSERT INTO sv2_evidence (evidence_id, source_id, family, canonical_url, title, retrieved_ts, title_hash, content_hash, collector_version) VALUES ('ev_n','fed_press','macro','https://fed.gov/n','Fed again',?,'thn','cn','v')").run(CUT + 20 * H);
    nowTs = CUT + 24 * H + 30e3;
    const out = validAnswer({ horizons: [{ horizon_hours: 24, direction: 'DOWN', strength: 'moderate', supporting_refs: ['tfm:4', 'chr:BTC_24h_next', 'ev:ev_n'], contradicting_refs: [], rationale: 'Fed release and both models point down' }],
      findings: [{ type: 'model_forecast', claim: 'TimesFM and Chronos forecast DOWN', refs: ['tfm:4'] }], gemini_vs_original: [] });
    return glmDailySignalRun(mkEnv(ACTIVE, day2Ai ? day2Ai() : aiReturning(out)),
      deps(ACTIVE, { aiTimeoutMs: 50, fetchImpl: async () => ({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(ledger2) }) }), { scheduledTs: CUT + 24 * H, now: clock });
  }

  it('day 1 CANDIDATE never moves p_up; day 2 ELIGIBLE moves it by strength x cap, with baseline, cap, rule and explanation recorded', async () => {
    await glmDailySignalRun(mkEnv(ACTIVE, aiReturning(validAnswer())), deps(ACTIVE), { scheduledTs: CUT, now: clock });
    const g1 = await glmTickAdjust(mkEnv(ACTIVE), { coin: 'BTC', horizon: 24, predictResult: pr({ ts: CUT + 2 * H }), selection: sel, now: CUT + 2 * H });
    expect(g1).toMatchObject({ mode: 'active', signal_status: 'CANDIDATE', adjustment: 0, final_p_up: 0.6, reason: 'candidate_not_applied' });
    const r = await twoDays();
    expect(r.status).toBe('completed');
    expect(r.signal.eligibility).toBe('ELIGIBLE');
    const g = await glmTickAdjust(mkEnv(ACTIVE), { coin: 'BTC', horizon: 24, predictResult: pr(), selection: sel, now: CUT + 26 * H });
    expect(g).toMatchObject({ mode: 'active', baseline_p_up: 0.6, signal_status: 'ELIGIBLE', direction: 'DOWN', strength: 'moderate', cap: 0.03, reason: 'applied', rule_version: 'glm-qual-0.4' });
    expect(g.adjustment).toBeCloseTo(-0.02, 6);
    expect(g.final_p_up).toBeCloseTo(0.58, 6);
    expect(g.explanation).toMatchObject({ rationale: 'Fed release and both models point down', supporting_refs: ['tfm:4', 'chr:BTC_24h_next', 'ev:ev_n'] });
    expect(g.explanation.findings[0].claim).toBe('TimesFM and Chronos forecast DOWN');
    const row = d1.db.prepare('SELECT baseline_p_up, applied, cap, final_p_up, mode, reason, rule_version, signal_id FROM glm_adjustments WHERE prediction_ts = ?').get(CUT + 26 * H);
    expect(row).toMatchObject({ baseline_p_up: 0.6, cap: 0.03, mode: 'active', reason: 'applied', rule_version: 'glm-qual-0.4' });
    expect(row.applied).toBeCloseTo(-0.02, 6);
    expect(row.signal_id).toMatch(/:24$/);
    // the predictions table is never touched by GLM: the baseline stays the stored prediction
    expect(d1.db.prepare('SELECT COUNT(*) AS n FROM predictions WHERE ts = ?').get(CUT + 26 * H).n).toBe(0);
  });

  it('per-asset / per-horizon safeguard: ETH, LINK and 12 h stay at baseline in active mode with zero queries', async () => {
    await twoDays();
    d1.log.length = 0;
    for (const [coin, h] of [['ETH', 24], ['LINK', 24], ['BTC', 12]]) {
      const g = await glmTickAdjust(mkEnv(ACTIVE), { coin, horizon: h, predictResult: pr(), selection: sel, now: CUT + 26 * H });
      expect(g).toMatchObject({ mode: 'active', adjustment: 0, final_p_up: 0.6, signal_status: 'NO_SIGNAL' });
    }
    expect(d1.log).toEqual([]);
  });

  it.each([
    ['model timeout', () => ({ run: vi.fn(() => new Promise(() => {})) }), 'timeout'],
    ['quota exhausted', () => ({ run: vi.fn(async () => { throw new Error('3036: daily free allocation exhausted'); }) }), 'quota_exhausted'],
    ['model unavailable', () => ({ run: vi.fn(async () => { throw new Error('5028 capacity'); }) }), 'model_unavailable'],
    ['invalid output', () => aiReturning(validAnswer({ score: 1 })), 'invalid_output'],
    ['malformed JSON', () => aiReturning('{not json'), 'invalid_output'],
  ])('%s on day 2 -> NO_SIGNAL, served p_up falls back to the baseline', async (_n, mk, st) => {
    const r = await twoDays(mk);
    expect(r.status).toBe(st);
    expect(r.signal).toMatchObject({ direction: 'NO_SIGNAL', eligibility: 'NO_SIGNAL' });
    const g = await glmTickAdjust(mkEnv(ACTIVE), { coin: 'BTC', horizon: 24, predictResult: pr(), selection: sel, now: CUT + 26 * H });
    expect(g.final_p_up).toBe(0.6);
    expect(g.adjustment).toBe(0);
  });

  it('a failing ledger write or signal read never throws into the tick chain', async () => {
    const env = { ...ACTIVE, DB: { prepare: () => { throw new Error('D1 down'); } } };
    const g = await glmTickAdjust(env, { coin: 'BTC', horizon: 24, predictResult: pr(), selection: sel });
    expect(g).toMatchObject({ signal_status: 'NO_SIGNAL', adjustment: 0 });
  });
});

describe('no-flip safeguard and cap bounds', () => {
  const sig = (o = {}) => ({ asset: 'BTC', horizon_hours: 24, direction: 'DOWN', strength: 'strong', eligibility: 'ELIGIBLE', valid_from_ts: 0, valid_until_ts: 10, ...o });
  const adj = (o) => computeAdjustment({ signal: sig(o.sig), baseline_p_up: o.b, predictionTs: 5, coin: 'BTC', horizonHours: 24, influenceCap: o.cap ?? 0.03, allowFlip: !!o.flip });
  it('DOWN cannot pull an UP call below 0.5 (and UP cannot lift a DOWN call above 0.5)', () => {
    expect(adj({ b: 0.51 })).toMatchObject({ final_p_up: 0.5, reason: 'applied_no_flip_capped' });
    expect(adj({ b: 0.49, sig: { direction: 'UP' } })).toMatchObject({ final_p_up: 0.5, reason: 'applied_no_flip_capped' });
    expect(adj({ b: 0.51, flip: true }).final_p_up).toBeCloseTo(0.48, 6);
  });
  it('strength scales the cap; the hard max 0.10 bounds any configuration', () => {
    expect(adj({ b: 0.7, sig: { strength: 'weak' } }).applied).toBeCloseTo(-0.01, 6);
    expect(adj({ b: 0.7, sig: { strength: 'moderate' } }).applied).toBeCloseTo(-0.02, 6);
    expect(adj({ b: 0.7 }).applied).toBeCloseTo(-0.03, 6);
    expect(adj({ b: 0.7, cap: 5 }).applied).toBeCloseTo(-0.1, 6);
  });
  it('the shipped production cap equals the documented initial cap', () => {
    expect(readFileSync(join(ROOT, 'wrangler.toml'), 'utf8')).toMatch(new RegExp(`^GLM_INFLUENCE_CAP = "${GLM_ADJUST.initialCap}"$`, 'm'));
  });
});

describe('prospective evaluation: baseline vs GLM-influenced', () => {
  const row = (o) => ({ coin: 'BTC', horizon_hours: 24, mode: 'active', signal_status: 'NO_SIGNAL', applied: 0, baseline_p_up: 0.6, final_p_up: 0.6, realized_up: null, volatility_percentile: 0.5, ...o });
  it('coverage, abstention, accuracy, Brier, false activations, adjustment size and groups', () => {
    const e = glmEvaluationSummary([
      row({ realized_up: 1 }),
      row({ signal_status: 'ELIGIBLE', applied: -0.02, final_p_up: 0.58, realized_up: 0 }),            // helpful
      row({ signal_status: 'ELIGIBLE', applied: 0.03, baseline_p_up: 0.55, final_p_up: 0.58, realized_up: 0, volatility_percentile: 0.9 }), // false
      row({ signal_status: 'CANDIDATE' }),
    ]);
    expect(e.overall).toMatchObject({ ticks: 4, resolved: 3, coverage: 0.5, abstention: 0.25, signal_ticks: 3, applied_ticks: 2,
      activations: { helpful: 1, false: 1 }, adjustment: { mean_abs: 0.025, max_abs: 0.03 } });
    expect(e.overall.directional_accuracy).toEqual({ baseline: 0.3333, final: 0.3333 });
    expect(e.overall.brier.final).toBeLessThan(e.overall.brier.baseline + 0.01);
    expect(e.groups['BTC:24'].ticks).toBe(4);
    expect(e.groups['regime:high_vol'].applied_ticks).toBe(1);
    expect(e.sufficient_for_claims).toBe(false);
  });
  it('empty ledger -> nulls, never a claim', () => {
    const e = glmEvaluationSummary([]);
    expect(e.overall).toMatchObject({ ticks: 0, coverage: null, directional_accuracy: { baseline: null, final: null } });
    expect(e.sufficient_for_claims).toBe(false);
  });
});

describe('GLM call timeout (2026-11-21 production timeout at 25 s)', () => {
  it('allows the full package enough wall time but stays bounded', async () => {
    const { GLM_LIMITS } = await import('../glm/contract.js');
    expect(GLM_LIMITS.aiTimeoutMs).toBeGreaterThanOrEqual(90000);
    expect(GLM_LIMITS.aiTimeoutMs).toBeLessThanOrEqual(180000);
  });
});

// ---------- Forecast-triggered GLM + budget gate (change request 2026-11-21, owner budget decision) ----------
import { glmForecastTrigger, glmBudgetGate } from '../glm/runner.js';

describe('forecast-triggered GLM: independent triggers, idempotent, honest, budget-gated', () => {
  const ledgerFetch = async () => ({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(LEDGER) });
  const at = CUT + 2 * H;
  it('each forecast gets its own record: one analysis for the package, the other forecast recorded as covered; later calls find nothing due', async () => {
    nowTs = at + 30e3;
    const ai = aiReturning(validAnswer());
    const env = mkEnv(LIVE, ai);
    const r1 = await glmForecastTrigger(env, deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at, fetchImpl: ledgerFetch });
    const r2 = await glmForecastTrigger(env, deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at, fetchImpl: ledgerFetch });
    const r3 = await glmForecastTrigger(env, deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at, fetchImpl: ledgerFetch });
    expect([r1.ran, r2.ran, r3.ran]).toEqual([true, false, false]);
    // both forecasts precede the same cutoff: ONE package holds both, so the second is recorded as covered in the same call
    expect(r1.covered.length).toBeGreaterThanOrEqual(1);
    expect([r2.status, r3.status]).toEqual(['nothing_due', 'nothing_due']);
    expect(ai.run).toHaveBeenCalledTimes(1);
    const runs = d1.db.prepare("SELECT reason, decision_cutoff_ts, status FROM glm_runs WHERE reason LIKE 'trigger:%' ORDER BY rowid").all();
    expect(runs).toHaveLength(1 + r1.covered.length);
    for (const r of runs) { expect(r.decision_cutoff_ts).toBe(at); expect(r.reason).toMatch(/^trigger:(tfm|chr):[^;]+; counterpart (timesfm|chronos) /); }
    expect(runs[1].reason).toMatch(/covered by run glm_/);
    // a later cutoff with a new forecast gets its own analysis (independent trigger)
    d1.db.prepare(`INSERT INTO experiment_4_timesfm (coin, horizon_hours, ts, input_end_ts, target_ts, price_at_prediction, direction, model_version, checkpoint, inference_backend, context_length, forecast_horizon_steps, features_used)
      VALUES ('BTC',24,?,?,?,63000,'UP','timesfm-2.5','c','cpu',128,8,'u')`).run(at + 2 * H, at + H, at + 26 * H);
    const r4 = await glmForecastTrigger(env, deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at + 3 * H, fetchImpl: ledgerFetch });
    expect(r4).toMatchObject({ ran: true, trigger: { source: 'timesfm' } });
    expect(r4.trigger.counterpart).toMatch(/^chronos chr:/);
    expect(ai.run).toHaveBeenCalledTimes(2);
    // the Neuron ledger reconciled each call to its actual use
    const led = d1.db.prepare('SELECT reserved_neurons, actual_neurons FROM sv2_ai_neurons ORDER BY slot').all();
    expect(led).toHaveLength(2);
    for (const l of led) expect(l.actual_neurons).toBeLessThan(l.reserved_neurons);
  });

  it('a trigger whose model output is invalid records the run but writes no signal (the day signal is not overwritten)', async () => {
    nowTs = at + 30e3;
    const before = d1.db.prepare('SELECT COUNT(*) AS n FROM glm_signals').get().n;
    const r = await glmForecastTrigger(mkEnv(LIVE, aiReturning('{not json')), deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at, fetchImpl: ledgerFetch });
    expect(r).toMatchObject({ ran: true, status: 'invalid_output' });
    expect(d1.db.prepare('SELECT COUNT(*) AS n FROM glm_signals').get().n).toBe(before);
  });

  it.each([
    ['budget below one maximum call', { ...LIVE, SV2_GLM_DAILY_NEURONS: '100' }, null, /BUDGET_EXHAUSTED: 0 of 100 GLM Neurons used, one call needs up to/],
    ['account allocation exhausted today (3036)', LIVE, 'account_limited', /BUDGET_EXHAUSTED: account free allocation reported exhausted/],
    ['budget already used today', LIVE, 'used', /BUDGET_EXHAUSTED: 250 of 300 GLM Neurons used/],
  ])('%s -> BUDGET_EXHAUSTED recorded, no package, no model call', async (_n, vars, ledgerState, re) => {
    const day = new Date(at).toISOString().slice(0, 10);
    if (ledgerState === 'account_limited') d1.db.prepare("INSERT INTO sv2_ai_neurons (day, slot, reserved_ts, model, reserved_neurons, status) VALUES (?,1,?,?,10,'account_limited')").run(day, at, GLM_MODEL);
    if (ledgerState === 'used') d1.db.prepare("INSERT INTO sv2_ai_neurons (day, slot, reserved_ts, model, reserved_neurons, actual_neurons, status) VALUES (?,1,?,?,260,250,'ok')").run(day, at, GLM_MODEL);
    const ai = aiReturning(validAnswer());
    const r = await glmForecastTrigger(mkEnv(vars, ai), deps(vars, { fetchImpl: ledgerFetch }), { now: at, fetchImpl: ledgerFetch });
    expect(r).toMatchObject({ ran: false, status: 'skipped_budget' });
    expect(r.reason).toMatch(re);
    expect(ai.run).not.toHaveBeenCalled();
    const row = d1.db.prepare("SELECT status, reason, n_items, package_hash FROM glm_runs WHERE reason LIKE 'trigger:%'").get();
    expect(row).toMatchObject({ status: 'skipped_budget', n_items: 0 });
    expect(row.package_hash).toMatch(/^none:/);
    expect(row.reason).toMatch(re);
    // idempotent: the same forecast is not retried in the same window
    const again = await glmForecastTrigger(mkEnv(vars, ai), deps(vars, { fetchImpl: ledgerFetch }), { now: at, fetchImpl: ledgerFetch });
    expect(again.trigger?.ref).not.toBe(r.trigger.ref);
  });

  it('an unreadable ledger is BUDGET_EXHAUSTED; the next UTC day starts from zero', async () => {
    const broken = { prepare: () => { throw new Error('D1 down'); } };
    const g = await glmBudgetGate(broken, deps(LIVE), at);
    expect(g).toMatchObject({ ok: false });
    expect(g.reason).toMatch(/^BUDGET_EXHAUSTED: GLM Neuron ledger cannot be read/);
    d1.db.prepare("INSERT INTO sv2_ai_neurons (day, slot, reserved_ts, model, reserved_neurons, actual_neurons, status) VALUES (?,1,?,?,260,250,'ok')").run(new Date(at).toISOString().slice(0, 10), at, GLM_MODEL);
    expect((await glmBudgetGate(d1.binding, deps(LIVE), at)).ok).toBe(false);
    expect((await glmBudgetGate(d1.binding, deps(LIVE), at + 24 * H)).ok).toBe(true);
  });

  it('completeness: every unprocessed forecast of the same provider in 36 h is recorded; budget skips record all due forecasts at once', async () => {
    nowTs = at + 30e3;
    // a second, older TimesFM forecast that was never processed
    d1.db.prepare(`INSERT INTO experiment_4_timesfm (coin, horizon_hours, ts, input_end_ts, target_ts, price_at_prediction, direction, model_version, checkpoint, inference_backend, context_length, forecast_horizon_steps, features_used)
      VALUES ('BTC',24,?,?,?,63000,'DOWN','timesfm-2.5','c','cpu',128,8,'u')`).run(at - 5 * H, at - 6 * H, at + 19 * H);
    const vars = { ...LIVE, SV2_GLM_DAILY_NEURONS: '100', GLM_DAILY_NEURONS: '100' };
    const ai = aiReturning(validAnswer());
    const r = await glmForecastTrigger(mkEnv(vars, ai), deps(vars, { fetchImpl: ledgerFetch }), { now: at, fetchImpl: ledgerFetch });
    expect(r.status).toBe('skipped_budget');
    expect(r.recorded.length).toBeGreaterThanOrEqual(3);                 // both TimesFM forecasts + Chronos
    const refs = d1.db.prepare("SELECT reason FROM glm_runs WHERE status = 'skipped_budget'").all().map((x) => x.reason.split(';')[0]);
    expect(new Set(refs).size).toBe(refs.length);                       // one record per forecast, no duplicates
    expect(ai.run).not.toHaveBeenCalled();
    // same UTC day: not offered again; after the 00:00 reset: offered once more, then never again
    const again = await glmForecastTrigger(mkEnv(vars, ai), deps(vars, { fetchImpl: ledgerFetch }), { now: at + H, fetchImpl: ledgerFetch });
    expect(again.status).toBe('nothing_due');
  });

  it('short D1 budget: defers without writing any marker, so the forecast is offered again next hour', async () => {
    nowTs = at + 30e3;
    const ai = aiReturning(validAnswer());
    const r = await glmForecastTrigger(mkEnv(LIVE, ai), deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at, fetchImpl: ledgerFetch, queryBudget: 6 });
    expect(r.status).toBe('deferred_d1_budget');
    expect(d1.db.prepare("SELECT COUNT(*) AS n FROM glm_runs WHERE reason LIKE 'trigger:%'").get().n).toBe(0);
    expect(ai.run).not.toHaveBeenCalled();
    const later = await glmForecastTrigger(mkEnv(LIVE, ai), deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at, fetchImpl: ledgerFetch });
    expect(later.ran).toBe(true);
  });

  it('due rule: interrupted runs do not count; one more chance after the daily reset only after a single budget skip', async () => {
    const { glmDueForecasts } = await import('../glm/runner.js');
    const c = [{ ref: 'tfm:1', forecast_ts: 1000 }];
    const now = Date.UTC(2026, 10, 22, 2), cutoff = Math.floor(now / H) * H, yday = Date.UTC(2026, 10, 21, 23);
    const m = (status, ts, started_ts = ts) => ({ reason: 'trigger:tfm:1; x', status, ts, started_ts });
    expect(glmDueForecasts(c, [], cutoff, now)).toHaveLength(1);
    expect(glmDueForecasts(c, [m('ok', yday)], cutoff, now)).toHaveLength(0);
    expect(glmDueForecasts(c, [m('started', yday, yday)], cutoff, now)).toHaveLength(1);           // cut short, > 1 h old
    expect(glmDueForecasts(c, [m('skipped_budget', yday)], cutoff, now)).toHaveLength(1);          // re-offered after reset
    expect(glmDueForecasts(c, [m('skipped_budget', cutoff)], cutoff, now)).toHaveLength(0);        // same day: no
    expect(glmDueForecasts(c, [m('skipped_budget', yday - 5 * H), m('skipped_budget', yday)], cutoff, now)).toHaveLength(0);  // once only
  });

  it('nothing due -> exactly one D1 query (2026-11-21: the hourly chain shares 50 queries with detection and V2)', async () => {
    nowTs = at + 30e3;
    const env = mkEnv(LIVE, aiReturning(validAnswer()));
    await glmForecastTrigger(env, deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at, fetchImpl: ledgerFetch });
    await glmForecastTrigger(env, deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at, fetchImpl: ledgerFetch });
    d1.log.length = 0;
    const r = await glmForecastTrigger(env, deps(LIVE, { fetchImpl: ledgerFetch, now: clock }), { now: at + H, fetchImpl: ledgerFetch });
    expect(r.status).toBe('nothing_due');
    expect(d1.log).toHaveLength(1);
  });

  it('inference off -> disabled with zero queries; stale forecasts never trigger', async () => {
    d1.log.length = 0;
    expect(await glmForecastTrigger(mkEnv(ON), deps(ON), { now: at, fetchImpl: ledgerFetch })).toMatchObject({ ran: false, status: 'disabled' });
    expect(d1.log).toEqual([]);
    const late = CUT + 80 * H;
    const r = await glmForecastTrigger(mkEnv(LIVE, aiReturning(validAnswer())), deps(LIVE, { fetchImpl: ledgerFetch }), { now: late, fetchImpl: ledgerFetch });
    expect(r).toMatchObject({ ran: false, status: 'nothing_due' });
  });

  it('GLM daily budget is clamped to 1,000 Neurons whatever the configuration says', () => {
    const G = evalInScope(extractConstants('GLM_DAILY_NEURONS_MAX', 'SV2_AI_RESERVE_SQL', 'SV2_AI_PRICING') + '\n' + extractFunctions('glmDeps', 'sv2UngroundedNumbers'));
    expect(G.glmDeps({ GLM_DAILY_NEURONS: '300' }).budget).toBe(300);
    expect(G.glmDeps({ GLM_DAILY_NEURONS: '50000' }).budget).toBe(1000);
    expect(G.glmDeps({}).budget).toBe(0);
    expect(readFileSync(join(ROOT, 'wrangler.toml'), 'utf8')).toMatch(/^GLM_DAILY_NEURONS = "300"$/m);
  });
});

describe('GET /api/glm/runs (audit review 2026-11-21, P2-3)', () => {
  it('lists runs and trigger records newest first, bounded, with origin, forecast ref and covering run', async () => {
    const { glmRuns } = await import('../glm/runner.js');
    const rows = [
      { run_id: 'glm_' + 'a'.repeat(24) + '_1795276800000', decision_cutoff_ts: 1795276800000, started_ts: 1, finished_ts: 2, status: 'skipped_budget', reason: 'trigger:tfm:162; counterpart chronos x; BUDGET_EXHAUSTED: 206 of 300', n_items: 0, n_excluded: 0, package_bytes: 0, usage_json: '{}' },
      { run_id: 'glm_' + 'b'.repeat(24) + '_1795244400000', decision_cutoff_ts: 1795244400000, started_ts: 1, finished_ts: 2, status: 'duplicate_package', reason: 'trigger:chr:BTC_24h_x; counterpart timesfm y; covered by daily run glm_' + 'c'.repeat(24) + '_1795244400000', n_items: 0, n_excluded: 0, package_bytes: 0, usage_json: '{}' },
      { run_id: 'glm_' + 'c'.repeat(24) + '_1795244400000', decision_cutoff_ts: 1795244400000, started_ts: 1, finished_ts: 2, status: 'timeout', reason: 'no answer within the time limit', n_items: 37, n_excluded: 18, package_bytes: 23627, usage_json: '{"reserved_neurons":206.05}' }];
    let bound = null;
    const env = { DB: { prepare: (sql) => ({ bind: (n) => { bound = n; return { all: async () => ({ results: rows }) }; } }) } };
    const out = await glmRuns(env, 50);
    expect(bound).toBe(50);
    expect(out.map((r) => [r.origin, r.forecast_ref, r.status])).toEqual([['timesfm', 'tfm:162', 'skipped_budget'], ['chronos', 'chr:BTC_24h_x', 'duplicate_package'], ['daily_0700', null, 'timeout']]);
    expect(out[1].covered_by).toBe('glm_' + 'c'.repeat(24) + '_1795244400000');
    expect(out[2].neurons.reserved).toBe(206.05);
  });
});
