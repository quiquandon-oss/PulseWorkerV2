// Sentiment V2 forecasts (SV2F-1): signal, stored analogue, walk-forward k-NN; read-only; no look-ahead.
import { describe, it, expect } from 'vitest';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { handleSv2ForecastRoute, outcomeAt, analogueOf, knnPredict, buildRecords, summarizeSv2, SV2_READINGS_SQL, BTC_PRICES_SQL, V1_READINGS_SQL, SV2F_LIMITS } from '../comparison/sv2-forecast.js';
const { DatabaseSync } = createRequire(import.meta.url)('node:sqlite');

const H = 3600 * 1000, T0 = Date.UTC(2026, 10, 1);
const SCHEMA = `
CREATE TABLE sentiment_v2_results (result_id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT NOT NULL, decision_ts INTEGER NOT NULL, calc_version TEXT NOT NULL,
  status TEXT NOT NULL, sentiment_score REAL, confidence REAL, evidence_coverage REAL, event_intensity REAL, predictive_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE (asset, calc_version, decision_ts));
CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL NOT NULL, technical_score INTEGER);
CREATE INDEX idx_btc_data_ts ON btc_data(ts);
CREATE TABLE server_composite_readings (slot_ts INTEGER PRIMARY KEY, computed_ts INTEGER NOT NULL, composite INTEGER);
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL);
CREATE INDEX idx_ts ON history(ts);`;
function d1(db) { const log = []; return { log, prepare(sql) { const st = { sql, args: [] }; return {
  bind(...a) { st.args = a; return this; }, async all() { log.push(st.sql); const r = db.prepare(st.sql).all(...st.args); return { results: r, meta: { rows_read: r.length } }; },
  async run() { throw new Error('write attempted'); } }; }, async batch() { throw new Error('batch'); } }; }
// Synthetic world: price rises after high-score readings and falls after low ones (a learnable V2 signal).
function world(nReadings = 60) {
  const db = new DatabaseSync(':memory:'); db.exec(SCHEMA);
  let price = 100;
  for (let i = 0; i < nReadings * 3 + 40; i++) db.prepare('INSERT INTO btc_data (ts, btc_price) VALUES (?,?)').run(T0 + i * H, price += (i % 7 === 0 ? 0.1 : 0));
  const prices = db.prepare('SELECT ts, btc_price FROM btc_data ORDER BY ts').all();
  db.exec('DELETE FROM btc_data');
  let p = 100; const byTs = new Map();
  for (let i = 0; i < nReadings; i++) {
    const t = T0 + i * 3 * H, high = i % 2 === 0, score = high ? 70 + (i % 5) : 30 - (i % 5);
    db.prepare("INSERT INTO sentiment_v2_results (asset, decision_ts, calc_version, status, sentiment_score, confidence, evidence_coverage, event_intensity, predictive_json) VALUES ('BTC',?,'sv2-1.1.0','OK',?,0.5,?,?,?)")
      .run(t, score, high ? 0.8 : 0.3, high ? 0.6 : 0.2, JSON.stringify({ status: 'ok', horizon_h: 24, p_up: 0.1, n: 20, method: 'family_profile_analogues', validation: 'candidate_unvalidated' }));
    byTs.set(t, high);
  }
  // prices: hourly; the 24 h move after each reading follows its score
  for (let i = 0; i < nReadings * 3 + 40; i++) { const t = T0 + i * H; const k = Math.floor(i / 3) * 3 * H + T0;
    const dir = byTs.has(k) ? (byTs.get(k) ? 1 : -1) : 0; p = Math.max(1, p + dir * 0.5); db.prepare('INSERT INTO btc_data (ts, btc_price) VALUES (?,?)').run(t, p); }
  void prices; return db;
}
const call = async (db, q = '', now) => { const u = new URL('https://w.example/api/sv2-forecast' + q); const res = await handleSv2ForecastRoute(new Request(u), { DB: d1(db) }, u, {}, () => now); return { res, body: await res.json() }; };

describe('outcome and analogue', () => {
  const prices = [{ ts: 0, btc_price: 100 }, { ts: 24 * H, btc_price: 101 }, { ts: 40 * H, btc_price: 99 }];
  it('resolves on the first price at or after t + h, only once it exists; old start price = no_price', () => {
    expect(outcomeAt(prices, 0, 24, 30 * H)).toMatchObject({ status: 'resolved', direction: 'UP', resolved_ts: 24 * H });
    expect(outcomeAt(prices, 0, 24, 23 * H).status).toBe('pending');
    expect(outcomeAt(prices, 5 * H, 24, 50 * H).status).toBe('no_price');          // start price 5 h old
    expect(outcomeAt(prices, 1 * H, 12, 50 * H).status).toBe('no_price');          // target 13 h, next price 24 h: beyond 6 h
  });
  it('reads the stored analogue only for its horizon and status', () => {
    expect(analogueOf('{"status":"ok","horizon_h":24,"p_up":0.3,"n":20}', 24)).toMatchObject({ p_up: 0.3, n: 20 });
    expect(analogueOf('{"status":"ok","horizon_h":24,"p_up":0.3}', 12)).toBeNull();
    expect(analogueOf('{"status":"insufficient"}', 24)).toBeNull();
    expect(analogueOf('not json', 24)).toBeNull();
  });
});

describe('walk-forward k-NN', () => {
  it('needs 20 training rows; learns a separable signal', () => {
    expect(knnPredict([], { score: 50, coverage: 0.5, intensity: 0.5 }).status).toBe('insufficient_history');
    const train = Array.from({ length: 40 }, (_, i) => ({ x: { score: i % 2 ? 70 : 30, coverage: i % 2 ? 0.8 : 0.3, intensity: 0.4 }, up: i % 2 }));
    expect(knnPredict(train, { score: 72, coverage: 0.8, intensity: 0.4 }).p_up).toBeGreaterThan(0.8);
    expect(knnPredict(train, { score: 28, coverage: 0.3, intensity: 0.4 }).p_up).toBeLessThan(0.2);
  });
  it('no look-ahead: each forecast trains only on outcomes known at its decision time', () => {
    const db = world(60), now = T0 + 400 * H;
    const rd = db.prepare(SV2_READINGS_SQL).all(2000), pr = db.prepare(BTC_PRICES_SQL).all(0, 20000);
    const { records } = buildRecords(rd, pr, [], 24, now);
    // a decision at t has a known outcome for readings at t - 24 h or earlier: 3 h spacing -> the first 8 readings + itself never train it
    for (const r of records) if (r.v2_knn.status === 'ok') expect(r.v2_knn.n_train).toBeLessThanOrEqual(Math.floor((r.decision_ts - T0) / (3 * H)) - 7);
    const first = records.find((r) => r.v2_knn.status === 'ok');
    expect(first.decision_ts).toBe(T0 + (20 + 7) * 3 * H);   // reading j is known 8 readings later: the 20th known outcome arrives at reading 27
    // changing a FUTURE outcome never changes an earlier forecast
    const cut = records[40].decision_ts;
    const pr2 = pr.map((p) => (p.ts > cut + 24 * H ? { ...p, btc_price: p.btc_price * (p.ts % 2 ? 2 : 0.5) } : p));
    const again = buildRecords(rd, pr2, [], 24, now).records;
    for (let i = 0; i <= 40; i++) expect(again[i].v2_knn.p_up).toBe(records[i].v2_knn.p_up);
  });
});

describe('route', () => {
  it('three SELECTs, read-only, scores all series', async () => {
    const db = world(80), now = T0 + 400 * H, fake = d1(db);
    const u = new URL('https://w.example/api/sv2-forecast?horizon=24');
    const body = await (await handleSv2ForecastRoute(new Request(u), { DB: fake }, u, {}, () => now)).json();
    expect(fake.log).toEqual([SV2_READINGS_SQL, BTC_PRICES_SQL, V1_READINGS_SQL]);
    expect(body).toMatchObject({ ok: true, protocol: 'SV2F-1', asset: 'BTC', horizon_hours: 24, calc_version: 'sv2-1.1.0' });
    expect(body.records[0].decision_ts).toBeGreaterThan(body.records[body.records.length - 1].decision_ts);   // newest first
    expect(body.summary.analogue).toMatchObject({ distinct_values: 1, min: 0.1, max: 0.1 });
    expect(body.summary.accuracy.v2_analogue.n).toBeGreaterThan(30);   // scored like any forecast
    expect(body.summary.accuracy.v2_signal.n).toBeGreaterThan(30);
    expect(body.summary.accuracy.v2_knn.rate).toBeGreaterThan(0.8);    // the synthetic signal is learnable
    expect(body.current).toMatchObject({ v2_knn: { status: 'ok' } });
    expect(body.summary.head_to_head.v2_signal_vs_v1.claim).toBe('insufficient_sample');   // no V1 rows in this world
  });
  it('validation, method, degraded and the static read-only proof', async () => {
    const db = world(10);
    expect((await call(db, '?horizon=6', T0)).res.status).toBe(400);
    const u = new URL('https://w.example/api/sv2-forecast');
    expect((await handleSv2ForecastRoute(new Request(u, { method: 'POST' }), { DB: d1(db) }, u)).status).toBe(405);
    expect(await handleSv2ForecastRoute(new Request('https://w.example/x'), {}, new URL('https://w.example/x'))).toBeNull();
    const empty = await call(new DatabaseSync(':memory:'), '', T0);
    expect([empty.res.status, empty.body.reason]).toEqual([503, 'missing_table']);
    const src = readFileSync(new URL('../comparison/sv2-forecast.js', import.meta.url), 'utf8');
    expect(src).not.toMatch(/\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b/);
    expect(src).not.toMatch(/\.(run|batch|exec)\(/);
    const w = readFileSync(new URL('../worker.js', import.meta.url), 'utf8'), sched = w.slice(w.indexOf('async scheduled('));
    expect(sched.slice(0, sched.indexOf('\n  },\n'))).not.toMatch(/Sv2Forecast|sv2-forecast/);
    expect(SV2F_LIMITS.maxEval).toBe(500);
  });
});
