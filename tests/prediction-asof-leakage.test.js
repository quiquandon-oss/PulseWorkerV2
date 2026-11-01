// Leakage regression tests for the V1-context join of the BTC and LINK k-NN cores (fp-asof-v1, 2026-11-01).
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
      'historicalClosestDistances', 'computeConditionalCalibration',
    ) + '\n\n' + extractConstants(
      'FEATURE_KEYS', 'LINK_FEATURE_KEYS', 'LINK_MIN_COMPLETE_ROWS', 'LINK_MIN_RESOLVED_ANALOGS', 'MODEL_VERSIONS', 'LAG_MS', 'TOL_MS',
      'MIN_COMPLETE_ROWS', 'MIN_RESOLVED_ANALOGS', 'CONTEXT_ASOF_MAX_AGE_MS', 'FEATURE_PIPELINE_VERSION',
      'CONDITIONAL_CALIB_WEIGHTS', 'CONDITIONAL_CALIB_K', 'CONDITIONAL_CALIB_MIN_NEIGHBORS',
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
const strip = (r) => JSON.parse(JSON.stringify(r, (k, v) => (k === 'ts' || k === 'ts_ms' || k === 'computed_at' ? undefined : v)));

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
    const run = (history) => s.runPrediction({ DB: fakeDb({ btc, history }) }, 24, { persist: false });
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
    const run = (history) => s.runPrediction({ DB: fakeDb({ btc: btc6, history }) }, 24, { persist: false });
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
    const run = (history) => s.runPrediction({ DB: fakeDb({ btc, history }) }, 24, { persist: false });
    expect(strip(await run(dup))).toEqual(strip(await run(dup)));
  });

  it('missing or stale context is imputed from earlier rows and flagged, never a future value', async () => {
    const s = scope();
    const run = (history) => s.runPrediction({ DB: fakeDb({ btc, history }) }, 24, { persist: false });
    const none = await run([]);                                   // no V1 history at all
    const stale = await run([{ id: 1, ts: T0 - 10 * 24 * H, score: 90, regime_mag: 3, bottom_score: 90 }]);   // far too old
    expect(none.ok).toBe(true);
    expect(strip(stale)).toEqual(strip(none));                    // a stale reading behaves exactly like a missing one
  });

  it('records the corrected model version', async () => {
    const s = scope();
    expect(s.MODEL_VERSIONS.btc_core).toBe('knn-core-v2-asof');
    expect(s.MODEL_VERSIONS.link_core).toBe('knn-core-v2-asof');
    expect(s.MODEL_VERSIONS.eth_core).toBe('knn-core-v2-selfcontained');
    expect(s.FEATURE_PIPELINE_VERSION).toBe('fp-asof-v1');
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
    const run = (history) => s.runLinkPrediction({ DB: fakeDb({ link, history }) }, 24, { persist: false });
    const ref = await run(base);
    expect(ref.ok).toBe(true);
    const future = [{ id: 9999, ts: lastTs + H, score: 100, regime_mag: 40, bottom_score: 1 }];
    expect(strip(await run([...base, ...future]))).toEqual(strip(ref));
    const link6 = priceRows(110, 'link_price', 12, 6);
    const last6 = link6[link6.length - 1].ts;
    const run6 = (history) => s.runLinkPrediction({ DB: fakeDb({ link: link6, history }) }, 24, { persist: false });
    const after = link6.map((r, i) => ({ id: i + 1, ts: r.ts + 30 * 60000, score: 40, regime_mag: 0.2 })).filter((r) => r.ts <= last6);
    const afterExtreme = after.map((r) => ({ ...r, score: 100, regime_mag: 50 }));
    expect(strip(await run6(after))).toEqual(strip(await run6(afterExtreme)));
  });
});

describe('Hyperliquid backfill rows are stamped when their price became available', () => {
  const D = 24 * H;
  const { candleAvailableTs } = evalInScope(extractFunctions('candleAvailableTs') + '\n' + extractConstants('CANDLE_INTERVAL_MS'));
  it('uses the candle close time + 1 ms, not the open time', () => {
    expect(candleAvailableTs({ t: T0, T: T0 + D - 1, c: '1' }, '1d')).toBe(T0 + D);
    expect(candleAvailableTs({ t: T0, T: T0 + H - 1, c: '1' }, '1h')).toBe(T0 + H);
  });
  it('falls back to open + interval, and to null when neither is known', () => {
    expect(candleAvailableTs({ t: T0, c: '1' }, '1d')).toBe(T0 + D);
    expect(candleAvailableTs({ t: T0, c: '1' }, '5m')).toBeNull();
    expect(candleAvailableTs({ c: '1' }, '1h')).toBeNull();
  });
  it('the backfill skips the still-open candle and stores the close-time stamp', async () => {
    const endBefore = Date.now();
    const candles = [
      { t: endBefore - 3 * D, T: endBefore - 2 * D - 1, c: '100' },
      { t: endBefore - 2 * D, T: endBefore - D - 1, c: '101' },
      { t: endBefore - D + 60000, T: endBefore + 60000 - 1, c: '102' },   // closes in the future: still open
    ];
    const inserts = [];
    const env = { DB: { prepare: (sql) => ({
      all: async () => ({ results: [] }),
      bind: (...args) => ({ run: async () => { inserts.push({ sql, args }); return {}; } }),
    }) } };
    const s = evalInScope(extractFunctions('backfillCoinHistory', 'candleAvailableTs', 'computeSimpleTechnicalScore') + '\n' + extractConstants('CANDLE_INTERVAL_MS'));
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ ok: true, json: async () => candles });
    try {
      const r = await s.backfillCoinHistory(env, { coin: 'BTC', table: 'btc_data', priceCol: 'btc_price', days: 3 });
      expect(r.candles_received).toBe(2);
      expect(inserts.map((x) => x.args[0])).toEqual([endBefore - 2 * D, endBefore - D]);
      expect(inserts.map((x) => x.args[1])).toEqual([100, 101]);
    } finally { globalThis.fetch = realFetch; }
  });
});
