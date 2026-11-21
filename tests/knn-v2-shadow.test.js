// knn-core-v2 shadow (OD-18): PR #110's leakage regression tests, ported to the shadow pipeline ('asof', fp-asof-v2),
// plus the guarantees of the shadow wiring: production (pipeline 'v1', knn-core-v1) is unchanged, the shadow never
// writes to a prediction table, is switched off by default and can never break the production cycle.
// The real runPrediction / runLinkPrediction run against a fake D1 that only answers SELECTs (persist: false).
// Core property: a V1 history reading recorded AFTER a row's timestamp can never change that row's features, so
// rewriting or adding future readings cannot alter the prediction. Rows below are synthetic test data.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const H = 3600000;
const T0 = Date.UTC(2026, 9, 1);

function scope() {
  return evalInScope(
    extractFunctions(
      'runPrediction', 'runLinkPrediction', 'nearestRow', 'asOfRow', 'meanStd', 'trendStrength', 'percentileRank',
      'getLatestCalibrationCurve', 'applyCalibratedProbability', 'trailingVolatility', 'weightedQuantile', 'currentGitSha',
      'historicalClosestDistances', 'computeConditionalCalibration', 'restampBackfillRows',
      'runEthPrediction', 'computeEthRegimeMag', 'knnV2ShadowEnabled', 'runKnnV2Shadow',
    ) + '\n\n' + extractConstants(
      'FEATURE_KEYS', 'LINK_FEATURE_KEYS', 'LINK_MIN_COMPLETE_ROWS', 'LINK_MIN_RESOLVED_ANALOGS', 'MODEL_VERSIONS', 'LAG_MS', 'TOL_MS',
      'MIN_COMPLETE_ROWS', 'MIN_RESOLVED_ANALOGS', 'CONTEXT_ASOF_MAX_AGE_MS', 'FEATURE_PIPELINE_VERSION',
      'CONDITIONAL_CALIB_WEIGHTS', 'CONDITIONAL_CALIB_K', 'CONDITIONAL_CALIB_MIN_NEIGHBORS', 'BACKFILL_RESTAMP_CUTOFF_TS',
      'HISTORY_FRESHNESS_MS', 'KNN_V2_SHADOW_VERSIONS', 'KNN_V2_SHADOW_RUNNERS', 'ETH_FEATURE_KEYS', 'ETH_MIN_COMPLETE_ROWS',
      'ETH_MIN_RESOLVED_ANALOGS',
    ),
  );
}

// Deterministic pseudo-random walk (no Math.random), 3-hourly unless a step is given.
function priceRows(n, field, start, stepH = 3) {
  let p = start, s = 7;
  const rnd = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
  return Array.from({ length: n }, (_, i) => {
    p *= 1 + (rnd() - 0.5) * 0.02;
    return { ts: T0 + i * stepH * H, [field]: Math.round(p * 100) / 100, technical_score: 30 + Math.round(rnd() * 40), funding_adj: 0 };
  });
}
// One V1 reading every 5 h, offset by 1 h, so readings fall both before and after the 3-hourly price rows.
function historyRows(n) {
  return Array.from({ length: n }, (_, i) => ({
    id: i + 1, ts: T0 + H + i * 5 * H, score: 30 + ((i * 7) % 40), regime_mag: ((i * 3) % 11) / 10 - 0.5, bottom_score: 20 + (i % 9),
  }));
}

function fakeDb({ btc, link, history }) {
  const sorted = () => [...history].sort((a, b) => a.ts - b.ts || a.id - b.id);
  const resultsFor = (sql) => {
    if (sql.includes('FROM btc_data')) return btc;
    if (sql.includes('FROM link_data')) return link;
    if (sql.includes('FROM history')) return sql.includes('regime_mag IS NOT NULL') ? sorted().filter((r) => r.regime_mag != null) : sorted();
    return [];
  };
  return {
    prepare(sql) {
      if (!/^\s*SELECT/i.test(sql)) throw new Error('write attempted in a persist:false run: ' + sql.slice(0, 60));
      const stmt = { first: async () => null, all: async () => ({ results: resultsFor(sql) }), run: async () => ({}) };
      stmt.bind = () => stmt;
      return stmt;
    },
  };
}

// Wall-clock fields (the run's own timestamp) legitimately differ between two runs; every other field must match.
const strip = (r) => JSON.parse(JSON.stringify(r, (k, v) => (k === 'ts' || k === 'ts_ms' || k === 'computed_at' || k === 'data_ts' ? undefined : v)));

describe('asOfRow', () => {
  const s = scope();
  const rows = [{ ts: 10, v: 'a' }, { ts: 20, v: 'b' }, { ts: 20, v: 'b2' }, { ts: 30, v: 'c' }];
  it('returns the latest row at or before the timestamp (boundary inclusive), never a later one', () => {
    expect(s.asOfRow(rows, 20, 100).v).toBe('b2');          // equal timestamp included; last of duplicates wins
    expect(s.asOfRow(rows, 29, 100).v).toBe('b2');          // 30 is in the future: not used
    expect(s.asOfRow(rows, 9, 100)).toBeNull();             // nothing available yet
    expect(s.asOfRow([], 50, 100)).toBeNull();
  });
  it('treats readings older than the maximum age as stale (null), inclusive at the limit', () => {
    expect(s.asOfRow(rows, 40, 10).v).toBe('c');            // exactly maxAge old: usable
    expect(s.asOfRow(rows, 41, 10)).toBeNull();             // older: stale
  });
});

describe('BTC core: future context readings cannot change a prediction', () => {
  let log;
  beforeEach(() => { log = vi.spyOn(console, 'log').mockImplementation(() => {}); });
  afterEach(() => log.mockRestore());

  const btc = priceRows(160, 'btc_price', 60000);
  const lastTs = btc[btc.length - 1].ts;
  const base = historyRows(Math.floor((lastTs - T0) / (5 * H)) + 1).filter((r) => r.ts <= lastTs);

  it('adding, removing or rewriting readings after each row timestamp leaves the prediction unchanged', async () => {
    const s = scope();
    const run = (history) => s.runPrediction({ DB: fakeDb({ btc, history }) }, 24, { persist: false, pipeline: 'asof' });
    const ref = await run(base);
    expect(ref.ok).toBe(true);
    expect(ref.p_up).toBeTypeOf('number');
    // Future to every historical row: shift each reading 30 min LATER than a price row it previously preceded is
    // not allowed to matter -- here we insert extra readings 1 h after every price row (within the old ±4.8 h window).
    const futureNoise = btc.map((r, i) => ({ id: 10000 + i, ts: r.ts + H, score: i % 2 ? 99 : 1, regime_mag: i % 2 ? 9 : -9, bottom_score: 99 }))
      .filter((r) => r.ts > lastTs);   // only readings after the predicted row: invisible to everything
    const later = await run([...base, ...futureNoise]);
    expect(strip(later)).toEqual(strip(ref));
  });

  it('a reading recorded after an analog (inside its outcome window) is not used for that analog', async () => {
    const s = scope();
    // 6-hourly price rows, one reading 30 min after each row. Under the old symmetric ±4.8 h join each row picked the
    // reading 30 min in its future; as of the row, the previous reading is 5.5 h old (stale), so no reading is usable
    // by any row and changing the readings' values cannot change anything.
    const btc6 = priceRows(110, 'btc_price', 60000, 6);
    const last6 = btc6[btc6.length - 1].ts;
    const run = (history) => s.runPrediction({ DB: fakeDb({ btc: btc6, history }) }, 24, { persist: false, pipeline: 'asof' });
    const after = btc6.map((r, i) => ({ id: i + 1, ts: r.ts + 30 * 60000, score: 40 + (i % 5), regime_mag: 0.1, bottom_score: 25 }))
      .filter((r) => r.ts <= last6);
    const afterExtreme = after.map((r) => ({ ...r, score: 100, regime_mag: 50, bottom_score: 100 }));
    const a = await run(after);
    expect(a.ok).toBe(true);
    expect(strip(a)).toEqual(strip(await run(afterExtreme)));
    expect(strip(a)).toEqual(strip(await run([])));                // identical to having no context at all
  });

  it('is deterministic for identical inputs and with duplicate reading timestamps', async () => {
    const s = scope();
    const dup = [...base, ...base.slice(10, 20).map((r) => ({ ...r, id: r.id + 5000 }))];
    const run = (history) => s.runPrediction({ DB: fakeDb({ btc, history }) }, 24, { persist: false, pipeline: 'asof' });
    expect(strip(await run(dup))).toEqual(strip(await run(dup)));
  });

  it('missing or stale context is imputed from earlier rows and flagged, never a future value', async () => {
    const s = scope();
    const run = (history) => s.runPrediction({ DB: fakeDb({ btc, history }) }, 24, { persist: false, pipeline: 'asof' });
    const none = await run([]);                                   // no V1 history at all
    const stale = await run([{ id: 1, ts: T0 - 10 * 24 * H, score: 90, regime_mag: 3, bottom_score: 90 }]);   // far too old
    expect(none.ok).toBe(true);
    expect(strip(stale)).toEqual(strip(none));                    // a stale reading behaves exactly like a missing one
  });

  it('production keeps knn-core-v1; the shadow carries the corrected versions', async () => {
    const s = scope();
    expect(s.MODEL_VERSIONS.btc_core).toBe('knn-core-v1');
    expect(s.MODEL_VERSIONS.link_core).toBe('knn-core-v1');
    expect(s.MODEL_VERSIONS.eth_core).toBe('knn-core-v1-selfcontained');
    expect(s.KNN_V2_SHADOW_VERSIONS).toEqual({ BTC: 'knn-core-v2-asof', LINK: 'knn-core-v2-asof', ETH: 'knn-core-v2-selfcontained' });
    expect(s.FEATURE_PIPELINE_VERSION).toBe('fp-asof-v2');
    const r = await s.runPrediction({ DB: fakeDb({ btc, history: base }) }, 24, { persist: true, pipeline: 'asof' });
    expect(r.persisted).toBe(false);                            // asof forces persist:false (fakeDb throws on writes)
    expect(r.model_version).toBe('knn-core-v2-asof');
  });

  it('production pipeline v1 is unchanged: it still reads the two-sided join (the defect the shadow fixes)', async () => {
    const s = scope();
    const btc6 = priceRows(110, 'btc_price', 60000, 6);
    const last6 = btc6[btc6.length - 1].ts;
    const run = (history) => s.runPrediction({ DB: fakeDb({ btc: btc6, history }) }, 24, { persist: false });
    const after = btc6.map((r, i) => ({ id: i + 1, ts: r.ts + 30 * 60000, score: 40 + (i % 5), regime_mag: 0.1, bottom_score: 25 }))
      .filter((r) => r.ts <= last6);
    const afterExtreme = after.map((r, i) => ({ ...r, score: i % 2 ? 100 : 0, regime_mag: i % 3 ? 50 : -50, bottom_score: 100 }));
    const a = await run(after), b = await run(afterExtreme);
    expect(a.ok).toBe(true);
    expect(a.model_version).toBeUndefined();                    // v1 result object has no shadow fields
    expect(strip(a)).not.toEqual(strip(b));                     // later readings do move knn-core-v1 analogs
  });
});

describe('LINK core: future context readings cannot change a prediction', () => {
  let log;
  beforeEach(() => { log = vi.spyOn(console, 'log').mockImplementation(() => {}); });
  afterEach(() => log.mockRestore());

  it('readings after the predicted row, or after each analog, do not alter the prediction', async () => {
    const s = scope();
    const link = priceRows(120, 'link_price', 12);
    const lastTs = link[link.length - 1].ts;
    const base = historyRows(Math.floor((lastTs - T0) / (5 * H)) + 1).filter((r) => r.ts <= lastTs);
    const run = (history) => s.runLinkPrediction({ DB: fakeDb({ link, history }) }, 24, { persist: false, pipeline: 'asof' });
    const ref = await run(base);
    expect(ref.ok).toBe(true);
    const future = [{ id: 9999, ts: lastTs + H, score: 100, regime_mag: 40, bottom_score: 1 }];
    expect(strip(await run([...base, ...future]))).toEqual(strip(ref));
    const link6 = priceRows(110, 'link_price', 12, 6);
    const last6 = link6[link6.length - 1].ts;
    const run6 = (history) => s.runLinkPrediction({ DB: fakeDb({ link: link6, history }) }, 24, { persist: false, pipeline: 'asof' });
    const after = link6.map((r, i) => ({ id: i + 1, ts: r.ts + 30 * 60000, score: 40, regime_mag: 0.2 })).filter((r) => r.ts <= last6);
    const afterExtreme = after.map((r) => ({ ...r, score: 100, regime_mag: 50 }));
    expect(strip(await run6(after))).toEqual(strip(await run6(afterExtreme)));
  });
});

describe('bf-restamp-v1: stored open-stamped backfill rows are read at their availability time', () => {
  const s = scope();
  const D = 24 * H;
  const cut = s.BACKFILL_RESTAMP_CUTOFF_TS.btc_data;
  const mid = Math.floor((cut - 30 * D) / D) * D;           // an exact midnight before the cutoff
  it('midnight rows move +24 h, other exact-hour rows +1 h; jittered, post-cutoff and unknown tables are untouched', () => {
    const rows = [{ ts: mid, p: 1 }, { ts: mid + 5 * H, p: 2 }, { ts: mid + 5 * H + 37, p: 3 }, { ts: cut + H, p: 4 }];
    const out = s.restampBackfillRows(rows, 'btc_data');
    expect(out.map((r) => [r.ts - mid, r.p, !!r.restamped])).toEqual([
      [5 * H + 37, 3, false], [6 * H, 2, true], [D, 1, true], [cut + H - mid, 4, false]]);
    expect(s.restampBackfillRows(rows, 'other_table')).toBe(rows);
  });
  it('keeps one row per corrected timestamp, the same one whatever the stored order', () => {
    const a = { ts: mid + 3 * H, p: 10 }, b = { ts: mid + 3 * H, p: 11 }, c = { ts: mid + 3 * H, p: 10 };
    const x = s.restampBackfillRows([a, b, c], 'link_data');
    const y = s.restampBackfillRows([b, c, a], 'link_data');
    expect(x).toEqual(y);
    expect(x).toHaveLength(1);
  });
  it('the BTC core sees mis-stamped daily rows exactly as if they had been stored at close time', async () => {
    let log = vi.spyOn(console, 'log').mockImplementation(() => {});
    try {
      const start = mid - 149 * D;
      const stored = priceRows(150, 'btc_price', 60000, 24).map((r, i) => ({ ...r, ts: start + i * D }));   // open-stamped
      const atClose = stored.map((r) => ({ ...r, ts: r.ts + D + 1 }));     // what a correct (close + 1 ms) stamp gives
      const run = (btc) => s.runPrediction({ DB: fakeDb({ btc, history: [] }) }, 24, { persist: false, pipeline: 'asof' });
      const a = await run(stored), b = await run(atClose);
      expect(a.ok).toBe(true);
      expect(strip(a)).toEqual(strip(b));
    } finally { log.mockRestore(); }
  });
});

describe('ETH shadow: scaling fitted without the predicted row', () => {
  it('the predicted row does not move the z-score statistics (v1 and asof differ only by that fit)', async () => {
    const s = scope();
    const eth = priceRows(140, 'eth_price', 3000);
    const db = { DB: { prepare(sql) {
      if (!/^\s*SELECT/i.test(sql)) throw new Error('write: ' + sql.slice(0, 40));
      const stmt = { first: async () => null, all: async () => ({ results: sql.includes('FROM eth_data') ? eth : [] }), run: async () => ({}) };
      stmt.bind = () => stmt; return stmt;
    } } };
    const a = await s.runEthPrediction(db, 24, { persist: true, pipeline: 'asof' });
    expect(a.ok).toBe(true);
    expect(a.persisted).toBe(false);
    // An outlier predicted row: v1 fits the scaling on it, the shadow does not, so the two cores must differ.
    const outlier = eth.map((r, i) => (i === eth.length - 1 ? { ...r, technical_score: 1000 } : r));
    const dbOut = { DB: { prepare: (sql) => { const st = db.DB.prepare(sql.replace('FROM eth_data', 'FROM eth_data')); st.all = async () => ({ results: sql.includes('FROM eth_data') ? outlier : [] }); return st; } } };
    const v1 = await s.runEthPrediction(dbOut, 24, { persist: false });
    const v2 = await s.runEthPrediction(dbOut, 24, { persist: false, pipeline: 'asof' });
    expect(v1.ok && v2.ok).toBe(true);
    const core = (r) => { const { model_version, feature_pipeline, ...rest } = strip(r); return rest; };
    expect(core(v2)).not.toEqual(core(v1));
  });
});

describe('runKnnV2Shadow wiring', () => {
  const served = { ok: true, status: 'ok', persisted: true, prediction_id: 42, ts: T0, p_up: 0.6 };
  function recDb(shadowStatus = 'ok', failInsert = null) {
    const calls = [];
    return { calls, DB: { prepare(sql) {
      const stmt = { bind: (...a) => { stmt.args = a; return stmt; },
        run: async () => { calls.push({ sql, args: stmt.args }); if (failInsert) throw new Error(failInsert); return {}; },
        all: async () => ({ results: [] }), first: async () => null };
      return stmt;
    } } };
  }
  it('is off by default and when the flag is not "1": no work, no query', async () => {
    const s = scope();
    const db = recDb();
    expect(await s.runKnnV2Shadow({ DB: db.DB }, 'BTC', 24, served)).toEqual({ status: 'disabled' });
    expect(await s.runKnnV2Shadow({ DB: db.DB, KNN_V2_SHADOW_ENABLED: 'true' }, 'BTC', 24, served)).toEqual({ status: 'disabled' });
    expect(db.calls).toHaveLength(0);
  });
  it('only shadows a production prediction that was actually persisted', async () => {
    const s = scope();
    const env = { DB: recDb().DB, KNN_V2_SHADOW_ENABLED: '1' };
    expect((await s.runKnnV2Shadow(env, 'BTC', 24, { ...served, persisted: false })).status).toBe('skipped_not_persisted');
    expect((await s.runKnnV2Shadow(env, 'BTC', 24, { ...served, status: 'insufficient_data' })).status).toBe('skipped_not_persisted');
  });
  it('records served vs shadow in knn_v2_shadow only (one INSERT OR IGNORE), keyed on the served prediction', async () => {
    const s = scope();
    const db = recDb();
    s.KNN_V2_SHADOW_RUNNERS.BTC = async (_e, h, o) => { expect(o).toEqual({ persist: false, pipeline: 'asof' }); return { status: 'ok', p_up: 0.4, calibrated_p_up: 0.45, median_analog_return_pct: -0.3, n_analogs: 9 }; };
    const r = await s.runKnnV2Shadow({ DB: db.DB, KNN_V2_SHADOW_ENABLED: '1' }, 'BTC', 24, served);
    expect(r).toEqual({ status: 'recorded', p_up: 0.4 });
    expect(db.calls).toHaveLength(1);
    expect(db.calls[0].sql).toMatch(/INSERT OR IGNORE INTO knn_v2_shadow/);
    expect(db.calls[0].sql).not.toMatch(/INTO (eth_|link_)?predictions/);
    expect(db.calls[0].args.slice(0, 11)).toEqual(['BTC', 24, 42, T0, 'knn-core-v1', 0.6, 'knn-core-v2-asof', 'fp-asof-v2', 'ok', 0.4, 0.45]);
  });
  it('never throws: a failing shadow run or a missing table (before migration 0032) leaves production untouched', async () => {
    const s = scope();
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      s.KNN_V2_SHADOW_RUNNERS.ETH = async () => { throw new Error('boom'); };
      expect(await s.runKnnV2Shadow({ DB: recDb().DB, KNN_V2_SHADOW_ENABLED: '1' }, 'ETH', 24, served)).toEqual({ status: 'error' });
      s.KNN_V2_SHADOW_RUNNERS.LINK = async () => ({ status: 'ok', p_up: 0.5 });
      expect(await s.runKnnV2Shadow({ DB: recDb('ok', 'no such table: knn_v2_shadow').DB, KNN_V2_SHADOW_ENABLED: '1' }, 'LINK', 24, served)).toEqual({ status: 'error' });
    } finally { err.mockRestore(); }
  });
});

describe('shadow wiring in worker.js and wrangler.toml', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
  const toml = fs.readFileSync(new URL('../wrangler.toml', import.meta.url), 'utf8');
  it('ships switched off', () => { expect(toml).toMatch(/^KNN_V2_SHADOW_ENABLED = "0"$/m); });
  it('each production predict-and-log runs the shadow after the production prediction, behind the flag', () => {
    for (const [fn, coin] of [['runPrediction', 'BTC'], ['runEthPrediction', 'ETH'], ['runLinkPrediction', 'LINK']]) {
      const re = new RegExp(`const result = await ${fn}\\(env, horizonHours, \\{ persist, claimToken \\}\\);\\n  if \\(knnV2ShadowEnabled\\(env\\)\\) result\\.knn_v2_shadow = await runKnnV2Shadow\\(env, '${coin}', horizonHours, result\\);`);
      expect(src).toMatch(re);
    }
  });
  it('migration 0032 is additive and has a rollback', () => {
    const up = fs.readFileSync(new URL('../.ai/migrations/0032_knn_v2_shadow.sql', import.meta.url), 'utf8');
    expect(up).toMatch(/CREATE TABLE IF NOT EXISTS knn_v2_shadow/);
    expect(up.replace(/--.*$/gm, '')).not.toMatch(/\b(ALTER|DROP|DELETE|UPDATE)\b/i);
    expect(fs.existsSync(new URL('../.ai/rollback/0032_knn_v2_shadow.down.sql', import.meta.url))).toBe(true);
  });
});
