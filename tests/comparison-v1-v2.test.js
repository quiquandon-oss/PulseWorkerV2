// Read-only V1/V2 comparison API (CryptoPulseV2 Phase 1 contract §7): bounds, alignment, no look-ahead, missing V1,
// GLM scope, metrics and read-only proof. Runs the real SQL on node:sqlite with the production table definitions.
import { describe, it, expect } from 'vitest';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import {
  comparisonSql, ASSET_TABLES,
  handleComparisonRoutes, parseComparisonParams, alignRecord, summarize, wilson, v1Direction, V1_BANDS,
  COMPARISON_SQL, COMPARISON_LIMITS, loadComparisonRows,
} from '../comparison/v1-v2.js';

// Loaded via require: Vite does not resolve the node:sqlite builtin (Node >= 22.5) as an ESM import.
const { DatabaseSync } = createRequire(import.meta.url)('node:sqlite');

const H = 3600 * 1000;
const NOW = Date.UTC(2026, 10, 20, 12, 0, 0);

// Production definitions (sqlite_master, 2026-11-20), trimmed to the columns the route reads plus required ones.
const SCHEMA = `
CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, target_ts INTEGER NOT NULL,
  btc_price_at_prediction REAL NOT NULL DEFAULT 0, p_up REAL NOT NULL, n_analogs INTEGER NOT NULL DEFAULT 0,
  realized_up INTEGER, resolved_ts INTEGER, horizon_hours INTEGER DEFAULT 24, calibrated_p_up REAL, git_commit_sha TEXT);
CREATE INDEX idx_predictions_horizon_ts ON predictions(horizon_hours, ts);
CREATE TABLE eth_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, target_ts INTEGER NOT NULL, eth_price_at_prediction REAL NOT NULL DEFAULT 0,
  p_up REAL NOT NULL, n_analogs INTEGER NOT NULL DEFAULT 0, realized_up INTEGER, resolved_ts INTEGER, horizon_hours INTEGER DEFAULT 24, calibrated_p_up REAL, git_commit_sha TEXT);
CREATE INDEX idx_eth_predictions_horizon_ts ON eth_predictions(horizon_hours, ts);
CREATE TABLE link_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, target_ts INTEGER NOT NULL, link_price_at_prediction REAL NOT NULL DEFAULT 0,
  p_up REAL NOT NULL, n_analogs INTEGER NOT NULL DEFAULT 0, realized_up INTEGER, resolved_ts INTEGER, horizon_hours INTEGER DEFAULT 24, calibrated_p_up REAL, git_commit_sha TEXT);
CREATE INDEX idx_link_predictions_horizon_ts ON link_predictions(horizon_hours, ts);
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL, btc_price REAL);
CREATE INDEX idx_ts ON history(ts);
CREATE TABLE server_composite_readings (slot_ts INTEGER PRIMARY KEY, computed_ts INTEGER NOT NULL, version TEXT NOT NULL,
  weights_version TEXT NOT NULL DEFAULT 'w', composite INTEGER, composite_exact REAL, completeness REAL NOT NULL DEFAULT 1,
  below_floor INTEGER NOT NULL DEFAULT 0, n_ok INTEGER NOT NULL DEFAULT 0, n_missing INTEGER NOT NULL DEFAULT 0, provenance_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE selection_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, coin TEXT NOT NULL, horizon_hours INTEGER NOT NULL,
  chosen_variant TEXT NOT NULL, chosen_p_up REAL NOT NULL, prediction_ts INTEGER);
CREATE TABLE selection_provenance (id INTEGER PRIMARY KEY AUTOINCREMENT, selection_table TEXT NOT NULL, selection_row_id INTEGER,
  recorded_ts INTEGER NOT NULL, coin TEXT NOT NULL, horizon_hours INTEGER NOT NULL, prediction_ts INTEGER, chosen_variant TEXT,
  chosen_p_up_source_ts INTEGER, cycle_p_up REAL, cycle_p_up_ts INTEGER, selection_trigger TEXT NOT NULL, semantics_version TEXT NOT NULL);
CREATE INDEX idx_selection_provenance_cycle ON selection_provenance (coin, horizon_hours, prediction_ts);
CREATE TABLE glm_adjustments (coin TEXT NOT NULL, horizon_hours INTEGER NOT NULL, prediction_ts INTEGER NOT NULL, baseline_p_up REAL NOT NULL,
  baseline_source TEXT NOT NULL, chosen_variant TEXT, signal_id TEXT, signal_status TEXT NOT NULL, would_apply REAL NOT NULL, applied REAL NOT NULL,
  cap REAL NOT NULL, final_p_up REAL NOT NULL, mode TEXT NOT NULL, reason TEXT, rule_version TEXT NOT NULL, created_ts INTEGER NOT NULL,
  PRIMARY KEY (coin, horizon_hours, prediction_ts));
`;

// D1-shaped wrapper that records every statement (read-only proof).
function d1(db) {
  const log = [];
  return {
    log,
    prepare(sql) {
      const st = { sql, args: [] };
      return {
        bind(...a) { st.args = a; return this; },
        async all() { log.push(st.sql); const rows = db.prepare(st.sql).all(...st.args); return { results: rows, meta: { rows_read: rows.length } }; },
        async first() { log.push(st.sql); return db.prepare(st.sql).get(...st.args) ?? null; },
        async run() { log.push(st.sql); throw new Error('write attempted'); },
      };
    },
    async batch() { throw new Error('batch not expected'); },
  };
}

function fixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(SCHEMA);
  const P = db.prepare('INSERT INTO predictions (ts, target_ts, p_up, realized_up, resolved_ts, horizon_hours, git_commit_sha) VALUES (?,?,?,?,?,?,?)');
  const S = db.prepare("INSERT INTO server_composite_readings (slot_ts, computed_ts, version, composite) VALUES (?,?,'scomp-v1',?)");
  const Hh = db.prepare('INSERT INTO history (ts, score) VALUES (?,?)');
  return { db, P, S, Hh };
}

const call = async (env, path, now = NOW) => {
  const res = await handleComparisonRoutes(new Request(`https://w.example${path}`), env, new URL(`https://w.example${path}`), {}, () => now);
  return { res, body: res ? await res.json() : null };
};

describe('parameters and bounds', () => {
  const q = (s) => parseComparisonParams(new URLSearchParams(s));
  it('defaults and validation', () => {
    expect(q('')).toEqual({ asset: 'BTC', horizon: 24, period: 'all', limit: 200 });
    expect(q('limit=9999').limit).toBe(500);
    expect(q('asset=DOGE').error).toMatch(/asset/);
    expect(q('asset=eth').asset).toBe('ETH');
    expect(q('horizon=6').error).toMatch(/horizon/);
    expect(q('period=90d').error).toMatch(/period/);
    expect(q('limit=0').error).toMatch(/limit/);
    expect(q('limit=abc').error).toMatch(/limit/);
  });
  it('rejects non-GET and ignores other paths', async () => {
    const env = { DB: d1(fixture().db) };
    const post = await handleComparisonRoutes(new Request('https://w.example/api/comparison/v1-v2', { method: 'POST' }), env, new URL('https://w.example/api/comparison/v1-v2'));
    expect(post.status).toBe(405);
    expect(await handleComparisonRoutes(new Request('https://w.example/api/glm/status'), env, new URL('https://w.example/api/glm/status'))).toBeNull();
    expect((await call(env, '/api/comparison/v1-v2?period=1y')).res.status).toBe(400);
  });
  it('one SELECT per uncached request, newest first, LIMIT n+1 and a truncated flag', async () => {
    const { db, P } = fixture();
    for (let i = 0; i < 12; i++) P.run(NOW - (i + 1) * 3 * H, NOW + 21 * H - i * 3 * H, 0.6, null, null, 24, 'abc');
    const fake = d1(db);
    const { body } = await call({ DB: fake }, '/api/comparison/v1-v2?limit=5');
    expect(fake.log).toHaveLength(1);
    expect(fake.log[0]).toBe(COMPARISON_SQL);
    expect(body.count).toBe(5);
    expect(body.truncated).toBe(true);
    expect(body.records.map((r) => r.decision_ts)).toEqual([...body.records.map((r) => r.decision_ts)].sort((a, b) => b - a));
    const all = await call({ DB: d1(db) }, '/api/comparison/v1-v2?limit=500');
    expect(all.body.truncated).toBe(false);
    expect(all.body.count).toBe(12);
  });
  it('period filters on decision time', async () => {
    const { db, P } = fixture();
    P.run(NOW - 2 * H, NOW + 22 * H, 0.6, null, null, 24, 'a');
    P.run(NOW - 3 * 86400000, NOW - 2 * 86400000, 0.6, 1, NOW - 2 * 86400000, 24, 'a');
    P.run(NOW - 40 * 86400000, NOW - 39 * 86400000, 0.6, 1, NOW - 39 * 86400000, 24, 'a');
    const c = async (p) => (await call({ DB: d1(db) }, `/api/comparison/v1-v2?period=${p}`)).body.count;
    expect([await c('24h'), await c('7d'), await c('30d'), await c('all')]).toEqual([1, 2, 2, 3]);
  });
  it('the query plan uses the indexes (no full scan of predictions, history or readings)', () => {
    const { db } = fixture();
    const plan = db.prepare(`EXPLAIN QUERY PLAN ${COMPARISON_SQL}`).all(24, 0, NOW, 501, 3 * H).map((r) => r.detail).join('\n');
    expect(plan).toMatch(/predictions USING INDEX idx_predictions_horizon_ts/);
    expect(plan).toMatch(/h USING (COVERING )?INDEX idx_ts/);
    expect(plan).toMatch(/s USING INTEGER PRIMARY KEY/);
    expect(plan).not.toMatch(/SCAN (predictions|history|server_composite_readings|selection_provenance|glm_adjustments)\b/);
  });
});

describe('alignment and no look-ahead', () => {
  it('V1 = latest server composite at or before the decision, never a later one; history fallback; >3 h is missing', async () => {
    const { db, P, S, Hh } = fixture();
    const t1 = NOW - 10 * H, t2 = NOW - 20 * H, t3 = NOW - 30 * H;
    P.run(t1, t1 + 24 * H, 0.7, null, null, 24, 'a');
    P.run(t2, t2 + 24 * H, 0.3, 0, t2 + 24 * H, 24, 'a');
    P.run(t3, t3 + 24 * H, 0.6, 1, t3 + 24 * H, 24, 'a');
    S.run(t1 - H, t1 - H + 60000, 62);          // valid for t1
    S.run(t1 + 30 * 60000, t1 + 31 * 60000, 10); // after t1: must never be used
    S.run(t1, t1 + 13000, 11);                   // slot at t1 but computed after t1: look-ahead, must be skipped
    Hh.run(t2 - 2 * H, 41);                      // t2 has no composite: history fallback
    Hh.run(t2 + 60000, 90);                      // later history: never used
    S.run(t3 - 4 * H, t3 - 4 * H + 1000, 70);    // too old for t3
    const { body } = await call({ DB: d1(db) }, '/api/comparison/v1-v2');
    const [r1, r2, r3] = body.records;
    expect(r1.v1).toMatchObject({ score: 62, source: 'server_composite_readings', reading_ts: t1 - H, direction: 'UP' });
    expect(r2.v1).toMatchObject({ score: 41, source: 'history', reading_ts: t2 - 2 * H, direction: 'DOWN' });
    expect(r3.v1).toMatchObject({ score: null, source: null, direction: null, missing_reason: 'no_v1_reading_within_3h' });
    for (const r of body.records) if (r.v1.reading_ts != null) expect(r.v1.reading_ts).toBeLessThanOrEqual(r.decision_ts);
  });
  it('outcome only after resolution; an outcome resolved after "now" is pending', () => {
    const base = { ts: NOW - 30 * H, target_ts: NOW - 6 * H, p_up: 0.7 };
    expect(alignRecord({ ...base, realized_up: 1, resolved_ts: NOW - 5 * H }, { asset: 'BTC', horizon: 24 }, NOW).outcome).toMatchObject({ status: 'resolved', direction: 'UP' });
    expect(alignRecord({ ...base, realized_up: null, resolved_ts: null }, { asset: 'BTC', horizon: 24 }, NOW).outcome.status).toBe('pending');
    expect(alignRecord({ ...base, realized_up: 1, resolved_ts: NOW + H }, { asset: 'BTC', horizon: 24 }, NOW).outcome.status).toBe('pending');
  });
  it('a reading after the decision is rejected even if the SQL ever returned it', () => {
    const r = alignRecord({ ts: NOW, target_ts: NOW + 24 * H, p_up: 0.6, sc_slot: NOW + H, sc_computed_ts: NOW + H, sc_value: 80, h_ts: NOW + 1, h_value: 80 }, { asset: 'BTC', horizon: 24 }, NOW);
    expect(r.v1.score).toBeNull();
  });
});

describe('V2 baseline and final', () => {
  const opts24 = { asset: 'BTC', horizon: 24 }, opts12 = { asset: 'BTC', horizon: 12 };
  const row = { ts: NOW - H, target_ts: NOW + 23 * H, p_up: 0.40 };
  it('headlineOf semantics for the baseline', () => {
    expect(alignRecord({ ...row }, opts24, NOW).v2_baseline).toMatchObject({ p_up: 0.4, source: 'p_up_no_selection_record', variant: 'original' });
    expect(alignRecord({ ...row, chosen_variant: 'original', cycle_p_up: 0.4, chosen_p_up: 0.9 }, opts24, NOW).v2_baseline).toMatchObject({ p_up: 0.4, source: 'p_up' });
    expect(alignRecord({ ...row, chosen_variant: 'calibrated', cycle_p_up: 0.62, chosen_p_up: 0.9 }, opts24, NOW).v2_baseline).toMatchObject({ p_up: 0.62, source: 'cycle_p_up', direction: 'UP' });
    expect(alignRecord({ ...row, chosen_variant: 'calibrated', cycle_p_up: null, chosen_p_up: 0.55 }, opts24, NOW).v2_baseline).toMatchObject({ p_up: 0.55, source: 'chosen_p_up' });
    expect(alignRecord({ ...row, chosen_variant: 'calibrated', cycle_p_up: 0.62, g_baseline: 0.61, g_source: 'cycle_p_up', g_mode: 'shadow', g_final: 0.63, g_applied: 0.02 }, opts24, NOW).v2_baseline)
      .toMatchObject({ p_up: 0.61, source: 'glm_adjustments.cycle_p_up' });
  });
  it('final: active GLM row, shadow row, no row, and out of scope (12 h)', () => {
    const active = alignRecord({ ...row, g_baseline: 0.56, g_source: 'p_up', g_mode: 'active', g_final: 0.58, g_applied: 0.02 }, opts24, NOW).v2_final;
    expect(active).toMatchObject({ p_up: 0.58, reason: 'glm_applied', glm_applied: true, adjustment: 0.02 });
    const zero = alignRecord({ ...row, g_baseline: 0.56, g_source: 'p_up', g_mode: 'active', g_final: 0.56, g_applied: 0, g_reason: 'no_eligible_signal' }, opts24, NOW).v2_final;
    expect(zero).toMatchObject({ p_up: 0.56, reason: 'no_eligible_signal', glm_applied: false });
    const shadow = alignRecord({ ...row, g_baseline: 0.56, g_source: 'p_up', g_mode: 'shadow', g_final: 0.58, g_applied: 0.02 }, opts24, NOW).v2_final;
    expect(shadow).toMatchObject({ p_up: 0.56, reason: 'no_adjustment', glm_applied: false });
    expect(alignRecord({ ...row }, opts24, NOW).v2_final).toMatchObject({ p_up: 0.4, reason: 'no_adjustment' });
    expect(alignRecord({ ...row, g_mode: 'active', g_final: 0.9, g_applied: 0.03 }, opts12, NOW).v2_final).toMatchObject({ p_up: 0.4, reason: 'glm_out_of_scope', glm_applied: false });
  });
  it('the route joins selection provenance and GLM rows by decision time', async () => {
    const { db, P } = fixture();
    const t = NOW - 5 * H;
    P.run(t, t + 24 * H, 0.45, null, null, 24, 'sha');
    db.prepare("INSERT INTO selection_decisions (ts, coin, horizon_hours, chosen_variant, chosen_p_up, prediction_ts) VALUES (?, 'BTC', 24, 'calibrated', 0.8, ?)").run(t + 1000, t);
    db.prepare("INSERT INTO selection_provenance (selection_table, selection_row_id, recorded_ts, coin, horizon_hours, prediction_ts, chosen_variant, cycle_p_up, selection_trigger, semantics_version) VALUES ('selection_decisions', 1, ?, 'BTC', 24, ?, 'calibrated', 0.57, 'cron', 'sel-v1')").run(t + 2000, t);
    db.prepare("INSERT INTO glm_adjustments VALUES ('BTC', 24, ?, 0.57, 'cycle_p_up', 'calibrated', 's', 'ELIGIBLE', 0.02, 0.02, 0.03, 0.59, 'active', NULL, 'r', ?)").run(t, t + 3000);
    const { body } = await call({ DB: d1(db) }, '/api/comparison/v1-v2');
    expect(body.records[0].v2_baseline).toMatchObject({ p_up: 0.57, variant: 'calibrated' });
    expect(body.records[0].v2_final).toMatchObject({ p_up: 0.59, glm_applied: true });
  });
});

describe('metrics', () => {
  it('Wilson 95% interval', () => {
    expect(wilson(0, 0)).toBeNull();
    const [lo, hi] = wilson(50, 100);
    expect(lo).toBeCloseTo(0.4038, 3); expect(hi).toBeCloseTo(0.5962, 3);
  });
  it('V1 bands: 50/50 has no neutral zone except exactly 50; 45/55 is the proposed convention', () => {
    const [b50, b45, b40] = V1_BANDS;
    expect(b45.proposed).toBe(true);
    expect([v1Direction(51, b50), v1Direction(50, b50), v1Direction(49, b50)]).toEqual(['UP', 'NO_CALL', 'DOWN']);
    expect([v1Direction(56), v1Direction(55), v1Direction(45), v1Direction(44)]).toEqual(['UP', 'NO_CALL', 'NO_CALL', 'DOWN']);
    expect([v1Direction(61, b40), v1Direction(59, b40)]).toEqual(['UP', 'NO_CALL']);
  });
  const rec = (v1score, p, up, resolved = true) => alignRecord({ ts: NOW - 30 * H, target_ts: NOW - 6 * H, p_up: p, realized_up: resolved ? up : null, resolved_ts: resolved ? NOW - 6 * H : null,
    sc_slot: v1score == null ? null : NOW - 31 * H, sc_computed_ts: NOW - 31 * H, sc_value: v1score }, { asset: 'BTC', horizon: 24 }, NOW);
  it('below 30 resolved aligned pairs: insufficient_sample and no claim', () => {
    const s = summarize([...Array(29)].map(() => rec(60, 0.6, 1)));
    expect(s.status).toBe('insufficient_sample');
    expect(s.claim).toBe('insufficient_sample');
    expect(s.n_resolved_pairs).toBe(29);
  });
  it('counts, coverage, no-call, agreement, accuracy with CI and Brier (V2 only)', () => {
    const recs = [
      ...[...Array(20)].map(() => rec(60, 0.7, 1)),   // all agree, correct
      ...[...Array(10)].map(() => rec(40, 0.7, 1)),   // V1 down (wrong), V2 up (right)
      ...[...Array(5)].map(() => rec(50, 0.3, 0)),    // V1 no call, V2 right
      ...[...Array(5)].map(() => rec(null, 0.6, 0)),  // V1 missing
      rec(60, 0.6, null, false),                       // pending
    ];
    const s = summarize(recs);
    expect(s.n_aligned).toBe(41);
    expect(s.n_resolved).toBe(40);
    expect(s.n_resolved_pairs).toBe(35);
    expect(s.status).toBe('ok');
    expect(s.v1_coverage).toBeCloseTo(36 / 41, 4);
    expect(s.v1_sources.missing).toBe(5);
    expect(s.no_call_rate.v1).toBeCloseTo(5 / 36, 4);
    expect(s.accuracy.v1).toMatchObject({ n: 30, correct: 20 });
    expect(s.accuracy.v2_final).toMatchObject({ n: 40, correct: 35 });
    expect(s.accuracy.v2_final.ci95[0]).toBeLessThan(0.875);
    expect(s.agreement.v1_v2_baseline).toMatchObject({ n: 31, rate: expect.any(Number) });
    expect(s.agreement.v2_baseline_v2_final.rate).toBe(1);
    expect(s.brier.v1).toBeNull();
    expect(s.brier.v2_baseline.n).toBe(40);
    expect(s.v1_bands.map((b) => b.band)).toEqual(['50/50', '45/55', '40/60']);
    expect(s.glm).toMatchObject({ adjusted: 0, changed_calls: 0, mean_abs_adjustment: null });
    expect(s.claim).toBe('no_supported_difference'); // n < 100
  });
  it('summary route returns metrics with methodology labels and the proposed-convention note', async () => {
    const { db, P } = fixture();
    P.run(NOW - 30 * H, NOW - 6 * H, 0.6, 1, NOW - 6 * H, 24, 'a');
    const { body } = await call({ DB: d1(db) }, '/api/comparison/v1-v2/summary?period=7d');
    expect(body.ok).toBe(true);
    expect(body.summary.status).toBe('insufficient_sample');
    expect(body.methodology.v1_direction).toMatch(/PROPOSED/);
    expect(body.limit).toBe(COMPARISON_LIMITS.maxLimit);
  });
});

describe('degraded states', () => {
  it('missing table → 503 missing_table, no-store, no raw error text', async () => {
    const db = new DatabaseSync(':memory:');
    const { res, body } = await call({ DB: d1(db) }, '/api/comparison/v1-v2');
    expect(res.status).toBe(503);
    expect(res.headers.get('Cache-Control')).toBe('no-store');
    expect(body).toEqual({ ok: false, error: 'comparison data unavailable', reason: 'missing_table' });
  });
  it('no DB binding → 503', async () => {
    expect((await call({}, '/api/comparison/v1-v2')).res.status).toBe(503);
  });
  it('loadComparisonRows passes the bounded parameters', async () => {
    const seen = [];
    const fake = { prepare: () => ({ bind: (...a) => { seen.push(a); return { all: async () => ({ results: [], meta: { rows_read: 0 } }) }; } }) };
    await loadComparisonRows(fake, { horizon: 12, period: '7d', limit: 500 }, NOW);
    expect(seen[0]).toEqual([12, NOW - 7 * 86400000, NOW, 501, 3 * H, 'BTC']);
  });
});

describe('read-only proof (static)', () => {
  const src = readFileSync(new URL('../comparison/v1-v2.js', import.meta.url), 'utf8');
  it('the module contains no write statement and no batch/run/exec call', () => {
    expect(src).not.toMatch(/\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b/);
    expect(src).not.toMatch(/\.(run|batch|exec)\(/);
  });
  it('the route is not referenced from the scheduled (tick/cron) handler', () => {
    const w = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const sched = w.slice(w.indexOf('async scheduled('));
    expect(w).toContain('handleComparisonRoutes(request, env, url, corsHeaders)');
    expect(sched.slice(0, sched.indexOf('\n  },\n'))).not.toMatch(/Comparison/);
  });
});

describe('ETH and LINK (same V2 engine, one ledger per asset; V1 is market-wide)', () => {
  it('each asset reads its own ledger through its index, with its own selection rows; GLM stays BTC 24 h only', async () => {
    const { db, S } = fixture();
    expect(Object.keys(ASSET_TABLES)).toEqual(['BTC', 'ETH', 'LINK']);
    for (const [a, table] of Object.entries(ASSET_TABLES)) {
      const plan = db.prepare(`EXPLAIN QUERY PLAN ${comparisonSql(a)}`).all(24, 0, NOW, 501, 3 * H, a).map((r) => r.detail).join('\n');
      expect(plan).toMatch(new RegExp(`${table} USING INDEX idx_${table}_horizon_ts`));
    }
    expect(() => comparisonSql('DOGE')).toThrow();
    db.prepare('INSERT INTO eth_predictions (ts, target_ts, p_up, realized_up, resolved_ts, horizon_hours) VALUES (?,?,?,?,?,24)').run(NOW - 30 * H, NOW - 6 * H, 0.3, 0, NOW - 6 * H);
    db.prepare("INSERT INTO selection_provenance (selection_table, selection_row_id, recorded_ts, coin, horizon_hours, prediction_ts, chosen_variant, cycle_p_up, selection_trigger, semantics_version) VALUES ('selection_decisions', NULL, ?, 'ETH', 24, ?, 'calibrated', 0.42, 'cron', 'sel-v1')").run(NOW, NOW - 30 * H);
    db.prepare("INSERT INTO selection_provenance (selection_table, selection_row_id, recorded_ts, coin, horizon_hours, prediction_ts, chosen_variant, cycle_p_up, selection_trigger, semantics_version) VALUES ('selection_decisions', NULL, ?, 'BTC', 24, ?, 'experimental', 0.9, 'cron', 'sel-v1')").run(NOW, NOW - 30 * H);
    S.run(NOW - 31 * H, NOW - 31 * H + 1000, 40);
    const { body } = await call({ DB: d1(db) }, '/api/comparison/v1-v2?asset=ETH');
    expect(body.asset).toBe('ETH');
    expect(body.records).toHaveLength(1);
    expect(body.records[0].v2_baseline).toMatchObject({ p_up: 0.42, variant: 'calibrated' });   // ETH selection row, not BTC's
    expect(body.records[0].v2_final.reason).toBe('glm_out_of_scope');
    expect(body.records[0].v1).toMatchObject({ score: 40, direction: 'DOWN' });
    expect(body.methodology.v1).toMatch(/market-wide/);
    const link = await call({ DB: d1(db) }, '/api/comparison/v1-v2/summary?asset=LINK');
    expect(link.body.summary.n_aligned).toBe(0);
  });
});

