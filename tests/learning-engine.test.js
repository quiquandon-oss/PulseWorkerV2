import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

describe('Learning Engine — scoring primitives', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('computeBrierScore', 'computeLogLoss', 'computeDirectionalAccuracy');
    scope = evalInScope(src);
  });

  it('computeBrierScore is 0 for perfect, confident predictions', () => {
    const rows = [{ p: 1, realized_up: 1 }, { p: 0, realized_up: 0 }];
    expect(scope.computeBrierScore(rows)).toBe(0);
  });

  it('computeBrierScore penalizes confident wrong calls more than timid ones', () => {
    const confidentWrong = scope.computeBrierScore([{ p: 0.95, realized_up: 0 }]);
    const timidWrong = scope.computeBrierScore([{ p: 0.55, realized_up: 0 }]);
    expect(confidentWrong).toBeGreaterThan(timidWrong);
  });

  it('computeBrierScore returns null for empty input', () => {
    expect(scope.computeBrierScore([])).toBeNull();
  });

  it('computeLogLoss returns a small positive number for a correct confident call', () => {
    const ll = scope.computeLogLoss([{ p: 0.99, realized_up: 1 }]);
    expect(ll).toBeGreaterThan(0);
    expect(ll).toBeLessThan(0.02);
  });

  it('computeLogLoss does not blow up (Infinity/NaN) at the p=0/p=1 edges', () => {
    const ll = scope.computeLogLoss([{ p: 1, realized_up: 0 }, { p: 0, realized_up: 1 }]);
    expect(Number.isFinite(ll)).toBe(true);
  });

  it('computeDirectionalAccuracy counts correct-direction calls regardless of confidence', () => {
    const rows = [
      { p: 0.51, realized_up: 1 }, // correct, barely
      { p: 0.99, realized_up: 0 }, // wrong, very confident
      { p: 0.2, realized_up: 0 },  // correct
    ];
    expect(scope.computeDirectionalAccuracy(rows)).toBeCloseTo(2 / 3, 5);
  });
});

describe('Learning Engine — calibration error (reuses buildCalibrationCurve)', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('buildCalibrationCurve', 'weightedQuantile', 'computeCalibrationError')
      + '\n\n' + extractConstants('LEARNING_MIN_SAMPLE');
    scope = evalInScope(src);
  });

  it('returns null below the minimum sample threshold rather than a noisy estimate', () => {
    const rows = Array.from({ length: 5 }, (_, i) => ({ p: 0.6, realized_up: i % 2 }));
    expect(scope.computeCalibrationError(rows)).toBeNull();
  });

  it('is near zero when predicted probabilities match realized rates closely', () => {
    // 150 rows, p=0.8, ~80% realized up -- well-calibrated by construction.
    // buildCalibrationCurve sorts by p_up (a stable no-op here, all tied)
    // then buckets sequentially, so the 20% "down" outcomes must be spread
    // evenly through the array (not clustered at the end) for every
    // resulting bucket to actually see an ~80% up-rate. Needs >=10 samples
    // per decile bucket to count at all (150/10 buckets = 15 each).
    const rows = Array.from({ length: 150 }, (_, i) => ({ p: 0.8, realized_up: i % 5 === 4 ? 0 : 1 }));
    const err = scope.computeCalibrationError(rows);
    expect(err).not.toBeNull();
    expect(err).toBeLessThan(0.1);
  });
});

describe('Learning Engine — confidence buckets', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('bucketByConfidence') + '\n\n' + extractConstants('CONFIDENCE_BUCKET_BOUNDS');
    scope = evalInScope(src);
  });

  it('buckets by DISTANCE from 0.5, so a p_up=0.2 call lands in the 0.75-0.80 confidence bucket', () => {
    const rows = [{ p: 0.2, realized_up: 0 }]; // confidence = max(0.2, 0.8) = 0.8 -> falls in 0.80+ actually
    const buckets = scope.bucketByConfidence(rows);
    const hit = buckets.find(b => b.n > 0);
    expect(hit.range).toBe('0.80+');
  });

  it('a bucket with zero samples reports null accuracy, not a misleading 0 or NaN', () => {
    const buckets = scope.bucketByConfidence([]);
    expect(buckets.every(b => b.n === 0 && b.accuracy === null)).toBe(true);
  });

  it('flags overconfidence when realized accuracy falls well short of the bucket midpoint', () => {
    // 10 predictions all at p=0.85 (confidence bucket 0.80+, midpoint ~0.90), only 5 correct -> 50% real accuracy
    const rows = Array.from({ length: 10 }, (_, i) => ({ p: 0.85, realized_up: i < 5 ? 1 : 0 }));
    const buckets = scope.bucketByConfidence(rows);
    const topBucket = buckets.find(b => b.range === '0.80+');
    expect(topBucket.overconfident_flag).toBe(true);
  });
});

describe('Learning Engine — most confident mistakes', () => {
  let scope;

  beforeAll(() => {
    scope = evalInScope(extractFunctions('mostConfidentMistakes'));
  });

  it('only includes wrong-direction calls, sorted by confidence descending', () => {
    const rows = [
      { ts: 1, p: 0.9, realized_up: 0, horizon_hours: 24 },  // wrong, very confident
      { ts: 2, p: 0.55, realized_up: 0, horizon_hours: 24 }, // wrong, barely confident
      { ts: 3, p: 0.9, realized_up: 1, horizon_hours: 24 },  // correct -- excluded
    ];
    const out = scope.mostConfidentMistakes(rows, 5);
    expect(out.length).toBe(2);
    expect(out[0].ts).toBe(1);
    expect(out[1].ts).toBe(2);
  });

  it('respects the limit parameter', () => {
    const rows = Array.from({ length: 10 }, (_, i) => ({ ts: i, p: 0.9, realized_up: 0, horizon_hours: 24 }));
    expect(scope.mostConfidentMistakes(rows, 3).length).toBe(3);
  });
});

describe('Learning Engine — regime classification (heuristic, no prediction-time effect)', () => {
  let scope;

  beforeAll(() => {
    scope = evalInScope(extractFunctions('classifyRegime'));
  });

  it('labels strong positive trend as bullish', () => {
    expect(scope.classifyRegime({ trend_strength: 0.5, volatility_percentile: 0.5, is_regime_anomaly: 0 }).trend_regime).toBe('bullish');
  });

  it('labels strong negative trend as bearish', () => {
    expect(scope.classifyRegime({ trend_strength: -0.5, volatility_percentile: 0.5, is_regime_anomaly: 0 }).trend_regime).toBe('bearish');
  });

  it('labels missing trend data as neutral rather than guessing', () => {
    expect(scope.classifyRegime({ trend_strength: null, volatility_percentile: null, is_regime_anomaly: 0 }).trend_regime).toBe('neutral');
  });

  it('passes through is_regime_anomaly unchanged', () => {
    expect(scope.classifyRegime({ trend_strength: 0, volatility_percentile: 0.5, is_regime_anomaly: 1 }).is_anomaly).toBe(true);
  });
});

describe('Learning Engine — summarizeRows (insufficient-data gate)', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions(
      'summarizeRows', 'computeBrierScore', 'computeLogLoss', 'computeDirectionalAccuracy',
      'computeCalibrationError', 'buildCalibrationCurve', 'weightedQuantile'
    ) + '\n\n' + extractConstants('LEARNING_MIN_SAMPLE');
    scope = evalInScope(src);
  });

  it('reports insufficient_data below the minimum sample, and never fabricates metrics', () => {
    const result = scope.summarizeRows([{ p: 0.6, realized_up: 1 }]);
    expect(result.status).toBe('insufficient_data');
    expect(result.accuracy).toBeUndefined();
  });

  it('computes real metrics once the minimum sample is met', () => {
    const rows = Array.from({ length: 25 }, (_, i) => ({ p: 0.6, realized_up: i % 3 === 0 ? 0 : 1 }));
    const result = scope.summarizeRows(rows);
    expect(result.status).toBe('ok');
    expect(result.n).toBe(25);
    expect(typeof result.accuracy).toBe('number');
  });
});

describe('Learning Engine — catalyst timestamp integrity', () => {
  let scope;

  beforeAll(() => {
    scope = evalInScope(extractFunctions('classifyCatalystTiming'));
  });

  it('marks a catalyst published before the prediction as available_before_prediction=true', () => {
    expect(scope.classifyCatalystTiming(1000, 2000)).toBe(true);
  });

  it('marks a catalyst discovered AFTER the prediction as false -- never lets hindsight in', () => {
    expect(scope.classifyCatalystTiming(3000, 2000)).toBe(false);
  });

  it('a catalyst timestamped exactly at prediction time counts as available (T1 <= T0)', () => {
    expect(scope.classifyCatalystTiming(2000, 2000)).toBe(true);
  });

  it('returns null rather than guessing when either timestamp is missing', () => {
    expect(scope.classifyCatalystTiming(null, 2000)).toBeNull();
    expect(scope.classifyCatalystTiming(1000, null)).toBeNull();
  });
});

describe('Learning Engine — model drift windows', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions(
      'computeDrift', 'summarizeRows', 'computeBrierScore', 'computeLogLoss',
      'computeDirectionalAccuracy', 'computeCalibrationError', 'buildCalibrationCurve', 'weightedQuantile'
    ) + '\n\n' + extractConstants('LEARNING_MIN_SAMPLE');
    scope = evalInScope(src);
  });

  it('each window reports insufficient_data independently when it lacks samples', () => {
    const nowTs = 10_000_000;
    const rows = [{ p: 0.6, realized_up: 1, resolved_ts: nowTs - 1000 }]; // 1 row, recent only
    const drift = scope.computeDrift(rows, nowTs);
    expect(drift.windows.last_24h.status).toBe('insufficient_data');
    expect(drift.windows.full_history.status).toBe('insufficient_data');
    expect(drift.flag).toBeNull();
  });

  it('does not flag drift when both comparison windows lack sufficient evidence', () => {
    const nowTs = 10_000_000;
    const drift = scope.computeDrift([], nowTs);
    expect(drift.flag).toBeNull();
  });
});

describe('Learning Engine — model identity capture', () => {
  let scope;

  beforeAll(() => {
    const src = extractConstants('MODEL_VERSIONS') + '\n\n' + extractFunctions('currentGitSha');
    scope = evalInScope(src);
  });

  it('defines a version tag for every model that writes to the ledger', () => {
    expect(scope.MODEL_VERSIONS.btc_core).toBeTruthy();
    expect(scope.MODEL_VERSIONS.link_core).toBeTruthy();
    expect(scope.MODEL_VERSIONS.eth_core).toBeTruthy();
    expect(scope.MODEL_VERSIONS.challenger).toBeTruthy();
  });

  it('falls back to "unknown" rather than throwing when GIT_COMMIT_SHA is not injected (local/test env)', () => {
    expect(scope.currentGitSha({})).toBe('unknown');
    expect(scope.currentGitSha(undefined)).toBe('unknown');
  });

  it('returns the injected value when present (real deploy env)', () => {
    expect(scope.currentGitSha({ GIT_COMMIT_SHA: 'abc123' })).toBe('abc123');
  });
});

describe('Learning Engine — catalyst logging helpers', () => {
  let scope;

  beforeAll(() => {
    scope = evalInScope(extractFunctions('recordCatalyst', 'fetchCatalystsForPeriod'));
  });

  it('recordCatalyst writes the full contract row shape and returns the new id', async () => {
    const calls = [];
    const fakeEnv = {
      DB: {
        prepare(sql) {
          return {
            bind(...args) {
              calls.push({ sql, args });
              return { run: async () => ({ meta: { last_row_id: 42 } }) };
            },
          };
        },
      },
    };
    const result = await scope.recordCatalyst(fakeEnv, {
      coin: 'BTC', ts: 1000, category: 'MACRO', direction: 'bearish',
      priceMovePct: -5.2, sourceUrl: 'https://example.com/a', discoveryTimestamp: 900, confidence: 'HIGH',
      marketClassification: 'MARKET_WIDE',
    });
    expect(result).toEqual({ ok: true, id: 42 });
    expect(calls[0].sql).toMatch(/INSERT INTO coin_catalyst_log/);
    expect(calls[0].args).toContain('MACRO');
    expect(calls[0].args).toContain('MARKET_WIDE');
  });

  it('fetchCatalystsForPeriod filters by ts range when both bounds are given', async () => {
    let capturedSql = null, capturedArgs = null;
    const fakeEnv = {
      DB: {
        prepare(sql) {
          capturedSql = sql;
          return { bind: (...args) => { capturedArgs = args; return { all: async () => ({ results: [] }) }; } };
        },
      },
    };
    await scope.fetchCatalystsForPeriod(fakeEnv, 1000, 2000);
    expect(capturedSql).toMatch(/WHERE ts >= \? AND ts < \?/);
    expect(capturedArgs).toEqual([1000, 2000]);
  });

  it('fetchCatalystsForPeriod skips the WHERE clause for an all-time query', async () => {
    let capturedSql = null;
    const fakeEnv = {
      DB: {
        prepare(sql) {
          capturedSql = sql;
          return { bind: () => ({ all: async () => ({ results: [] }) }) };
        },
      },
    };
    await scope.fetchCatalystsForPeriod(fakeEnv, null, null);
    expect(capturedSql).not.toMatch(/WHERE/);
  });
});

describe('Learning Engine — fetchResolvedRows per-table column handling', () => {
  let scope;

  beforeAll(() => {
    scope = evalInScope(extractFunctions('fetchResolvedRows'));
  });

  function fakeEnv() {
    let capturedSql = null;
    return {
      env: {
        DB: {
          prepare(sql) {
            capturedSql = sql;
            return { bind: () => ({ all: async () => ({ results: [] }) }) };
          },
        },
      },
      getSql: () => capturedSql,
    };
  }

  // Regression: challenger_predictions has no volatility_percentile column
  // (only predictions/link_predictions/eth_predictions do). The query used
  // to select it unconditionally, which threw a real SQLITE_ERROR ("no
  // such column: volatility_percentile") on every call for this table --
  // confirmed live against production D1. buildDailyReport's Challenger
  // vs Production comparison calls fetchResolvedRows(env,
  // 'challenger_predictions', ...) unconditionally for BTC and LINK, so
  // this made /api/learning/daily (and /api/learning/chatgpt, same
  // underlying call) 500 on every single request -- not intermittent, not
  // a connectivity issue, a genuine column mismatch on every call.
  it('regression: challenger_predictions is queried WITHOUT the raw volatility_percentile column', async () => {
    const { env, getSql } = fakeEnv();
    await scope.fetchResolvedRows(env, 'challenger_predictions', { coin: 'BTC', probColumn: 'p_up_tilted', calibratedColumn: 'calibrated_p_up_flat' });
    const sql = getSql();
    expect(sql).not.toMatch(/SELECT[^,]*,\s*volatility_percentile\b/);
    expect(sql).toMatch(/NULL as volatility_percentile/);
  });

  it('predictions/link_predictions/eth_predictions still select the real volatility_percentile column', async () => {
    for (const table of ['predictions', 'link_predictions', 'eth_predictions']) {
      const { env, getSql } = fakeEnv();
      await scope.fetchResolvedRows(env, table, {});
      expect(getSql()).toMatch(/,\s*volatility_percentile\s+as\s+volatility_percentile/);
    }
  });

  it('the mapped row shape is identical either way (volatility_percentile key always present, just null for challenger_predictions)', async () => {
    const env = {
      DB: {
        prepare() {
          return { bind: () => ({ all: async () => ({ results: [{ ts: 1, resolved_ts: 2, horizon_hours: 24, realized_up: 1, volatility_percentile: null, trend_strength: 0.1, is_regime_anomaly: 0, raw_p: 0.6, calibrated_p: null }] }) }) };
        },
      },
    };
    const rows = await scope.fetchResolvedRows(env, 'challenger_predictions', { coin: 'BTC', probColumn: 'p_up_tilted', calibratedColumn: 'calibrated_p_up_flat' });
    expect(rows[0]).toHaveProperty('volatility_percentile', null);
    expect(rows[0].p).toBe(0.6); // falls back to raw_p when calibrated_p is null, same as before
  });
});

describe('Immutability — outcome backfill only ever touches outcome columns', () => {
  // Structural check on the actual UPDATE statements, not just a re-test of
  // their behavior -- this is what .ai/DATA_CONTRACT.md's "DO NOT update
  // original features/probability/..." rule is actually protecting against.
  const OUTCOME_COLUMNS = ['realized_btc_price', 'realized_eth_price', 'realized_price', 'realized_return', 'realized_up', 'resolved_ts'];

  it('backfillPredictions / backfillEthPredictions / backfillChallengerPredictions only SET outcome columns', () => {
    const src = extractFunctions('backfillPredictions', 'backfillEthPredictions', 'backfillChallengerPredictions');
    const updateStatements = src.match(/UPDATE\s+\w+\s+SET\s+([^W]+?)\s+WHERE/gs) || [];
    expect(updateStatements.length).toBeGreaterThan(0);
    for (const stmt of updateStatements) {
      const setClause = stmt.match(/SET\s+([^W]+?)\s+WHERE/s)[1];
      const columns = setClause.split(',').map(c => c.trim().split('=')[0].trim());
      for (const col of columns) {
        expect(OUTCOME_COLUMNS).toContain(col);
      }
    }
  });
});
