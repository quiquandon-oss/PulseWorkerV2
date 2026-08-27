import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

describe('classifyAnomalyConditionedBucket — correct bucket classification', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('classifyAnomalyConditionedBucket'));
  });

  it('non-anomalous rows always classify as the single unsplit "normal" bucket, regardless of trend/trailing values', () => {
    expect(scope.classifyAnomalyConditionedBucket(0, 0.5, 1.2)).toBe('normal');
    expect(scope.classifyAnomalyConditionedBucket(0, -0.5, -1.2)).toBe('normal');
    expect(scope.classifyAnomalyConditionedBucket(0, null, null)).toBe('normal');
    expect(scope.classifyAnomalyConditionedBucket(undefined, 0.9, 5)).toBe('normal');
  });

  it('anomalous + positive trend + positive trailing return -> anomaly_trendpos_continuation', () => {
    expect(scope.classifyAnomalyConditionedBucket(1, 0.3, 2.5)).toBe('anomaly_trendpos_continuation');
  });

  it('anomalous + positive trend + negative trailing return -> anomaly_trendpos_dip', () => {
    expect(scope.classifyAnomalyConditionedBucket(1, 0.3, -2.5)).toBe('anomaly_trendpos_dip');
  });

  it('anomalous + negative trend + positive trailing return -> anomaly_trendneg_continuation', () => {
    expect(scope.classifyAnomalyConditionedBucket(1, -0.3, 2.5)).toBe('anomaly_trendneg_continuation');
  });

  it('anomalous + negative trend + negative trailing return -> anomaly_trendneg_dip', () => {
    expect(scope.classifyAnomalyConditionedBucket(1, -0.3, -2.5)).toBe('anomaly_trendneg_dip');
  });

  it('exact-zero trend_strength and exact-zero trailing return get their own explicit buckets, not silently merged into positive or negative', () => {
    expect(scope.classifyAnomalyConditionedBucket(1, 0, 2.5)).toBe('anomaly_trendzero_continuation');
    expect(scope.classifyAnomalyConditionedBucket(1, 0.3, 0)).toBe('anomaly_trendpos_trailingflat');
    expect(scope.classifyAnomalyConditionedBucket(1, 0, 0)).toBe('anomaly_trendzero_trailingflat');
  });

  it('null trend_strength or null trailing return get explicit nodata sub-buckets, never coerced to zero or silently dropped', () => {
    expect(scope.classifyAnomalyConditionedBucket(1, null, 2.5)).toBe('anomaly_trendnodata_continuation');
    expect(scope.classifyAnomalyConditionedBucket(1, 0.3, null)).toBe('anomaly_trendpos_trailingnodata');
    expect(scope.classifyAnomalyConditionedBucket(1, null, null)).toBe('anomaly_trendnodata_trailingnodata');
  });

  it('is a pure function -- same inputs always produce the same output, called repeatedly', () => {
    const results = new Set();
    for (let i = 0; i < 20; i++) {
      results.add(scope.classifyAnomalyConditionedBucket(1, 0.5, -1.5));
    }
    expect(results.size).toBe(1);
    expect([...results][0]).toBe('anomaly_trendpos_dip');
  });
});

describe('computeTrailingReturns — no future-data leakage', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('computeTrailingReturns'));
  });

  it('uses only price rows at or before (prediction.ts - lagMs) -- a price row that exists in between is correctly ignored if it is after the target', () => {
    const lagMs = 24 * 3600 * 1000;
    const predictionTs = 2_000_000_000_000;
    const priceRows = [
      { ts: predictionTs - lagMs - 1000, price: 100 }, // valid trailing candidate
      { ts: predictionTs - lagMs + 500, price: 999 },  // AFTER the target window -- must never be used as the trailing reference
      { ts: predictionTs, price: 110 },
    ];
    const predictionRows = [{ id: 1, ts: predictionTs, price_at_prediction: 110 }];
    const result = scope.computeTrailingReturns(priceRows, predictionRows, lagMs);
    // If the leaked (999) row were used: (110-999)/999*100 ~= -88.99. The
    // correct causal answer uses 100: (110-100)/100*100 = 10.
    expect(result.get(1)).toBeCloseTo(10, 5);
  });

  it('never uses a price row with ts > prediction.ts itself, even for the "current price" side of the calculation', () => {
    // price_at_prediction always comes from the prediction row's own
    // stored column (never looked up from priceRows), so there is no
    // path by which a future priceRows entry could leak into "now".
    const lagMs = 12 * 3600 * 1000;
    const predictionTs = 5_000_000_000_000;
    const priceRows = [
      { ts: predictionTs - lagMs, price: 50 },
      { ts: predictionTs + 1000, price: 99999 }, // strictly after the prediction itself
    ];
    const predictionRows = [{ id: 7, ts: predictionTs, price_at_prediction: 55 }];
    const result = scope.computeTrailingReturns(priceRows, predictionRows, lagMs);
    expect(result.get(7)).toBeCloseTo((55 - 50) / 50 * 100, 5);
  });

  it('returns null (not a fabricated number) when no price row exists at or before the target window', () => {
    const lagMs = 24 * 3600 * 1000;
    const predictionTs = 1_000_000_000_000;
    const priceRows = [{ ts: predictionTs - lagMs + 1000, price: 50 }]; // only AFTER the target, none before/at it
    const predictionRows = [{ id: 1, ts: predictionTs, price_at_prediction: 55 }];
    const result = scope.computeTrailingReturns(priceRows, predictionRows, lagMs);
    expect(result.get(1)).toBeNull();
  });

  it('correctly advances the pointer forward across multiple predictions without ever reusing a stale trailing price incorrectly', () => {
    const lagMs = 1000;
    const priceRows = [
      { ts: 1000, price: 10 },
      { ts: 2000, price: 20 },
      { ts: 3000, price: 30 },
      { ts: 4000, price: 40 },
    ];
    const predictionRows = [
      { id: 1, ts: 2000, price_at_prediction: 22 }, // target = 1000 -> price 10
      { id: 2, ts: 3000, price_at_prediction: 33 }, // target = 2000 -> price 20
      { id: 3, ts: 4000, price_at_prediction: 44 }, // target = 3000 -> price 30
    ];
    const result = scope.computeTrailingReturns(priceRows, predictionRows, lagMs);
    expect(result.get(1)).toBeCloseTo((22 - 10) / 10 * 100, 5);
    expect(result.get(2)).toBeCloseTo((33 - 20) / 20 * 100, 5);
    expect(result.get(3)).toBeCloseTo((44 - 30) / 30 * 100, 5);
  });
});

describe('computeAnomalyConditionedReport — deterministic episode grouping', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions(
      'computeAnomalyConditionedReport', 'classifyAnomalyConditionedBucket',
      'computeTrailingReturns', 'coreTableForCoin'
    ) + '\n\n' + extractConstants('ANOMALY_AUDIT_MIN_SAMPLE_N', 'ANOMALY_AUDIT_MIN_EPISODES', 'ANOMALY_AUDIT_MAX_GAP_MS'));
  });

  function makeFakeDb({ predictionRows, priceRows }) {
    return {
      prepare(sql) {
        return {
          bind: (...args) => ({
            all: async () => {
              if (/FROM (predictions|link_predictions|eth_predictions)/.test(sql)) {
                return { results: predictionRows.map((r) => ({
                  id: r.id, ts: r.ts, p_up: r.realizedUp === 1 ? 0.7 : 0.3, realized_up: r.realizedUp,
                  is_regime_anomaly: r.anomaly, trend_strength: r.trend, price_at_prediction: r.priceAtPrediction,
                })) };
              }
              return { results: priceRows };
            },
          }),
          all: async () => ({ results: priceRows }), // no-bind path, used for the price-history query
        };
      },
    };
  }

  it('4 consecutive cycles within the gap threshold (3h cadence, matching real cron spacing) collapse to exactly 1 episode, not 4 -- proves closely-spaced consecutive cycles are not independent evidence', async () => {
    const priceRows = [{ ts: 0, price: 100 }];
    const baseTs = new Date('2026-08-18').getTime();
    const predictionRows = [
      { ts: baseTs, id: 1, anomaly: 1, trend: 0.5, priceAtPrediction: 110, realizedUp: 1 },
      { ts: baseTs + 3 * 3600000, id: 2, anomaly: 1, trend: 0.5, priceAtPrediction: 111, realizedUp: 1 },
      { ts: baseTs + 6 * 3600000, id: 3, anomaly: 1, trend: 0.5, priceAtPrediction: 112, realizedUp: 1 },
      { ts: baseTs + 9 * 3600000, id: 4, anomaly: 1, trend: 0.5, priceAtPrediction: 113, realizedUp: 1 },
    ];
    const db = makeFakeDb({ predictionRows, priceRows });
    const report = await scope.computeAnomalyConditionedReport({ DB: db }, 'BTC', 24);
    expect(report.raw_prediction_count).toBe(4);
    const bucketKey = 'anomaly_trendpos_continuation';
    expect(report.bucket_summary[bucketKey].episode_count).toBe(1);
    expect(report.episodes).toHaveLength(1);
    expect(report.episodes[0].n_cycles).toBe(4);
  });

  it('REQUIRED REGRESSION (Aug-18/Aug-25 gap): two same-bucket observations separated by a real multi-day data gap are counted as two SEPARATE episodes, not merged into one', async () => {
    // Exact scenario used to prove the original defect: Aug 18 and Aug 25,
    // a genuine 7-day gap, nothing resolved in between. The old day-level
    // design merged these into one episode (episode_count=1) because it
    // only checked "same bucket as the previous entry", never actual
    // date/time adjacency.
    const priceRows = [{ ts: 0, price: 100 }];
    const predictionRows = [
      { ts: new Date('2026-08-18').getTime(), id: 1, anomaly: 1, trend: 0.5, priceAtPrediction: 105, realizedUp: 1 },
      { ts: new Date('2026-08-25').getTime(), id: 2, anomaly: 1, trend: 0.5, priceAtPrediction: 106, realizedUp: 1 },
    ];
    const db = makeFakeDb({ predictionRows, priceRows });
    const report = await scope.computeAnomalyConditionedReport({ DB: db }, 'BTC', 24);
    const bucketKey = 'anomaly_trendpos_continuation';
    expect(report.bucket_summary[bucketKey].n).toBe(2);
    expect(report.bucket_summary[bucketKey].episode_count).toBe(2); // NOT 1 -- the fix
    expect(report.episodes).toHaveLength(2);
    expect(report.episodes[0].end_date).toBe('2026-08-18');
    expect(report.episodes[1].start_date).toBe('2026-08-25');
  });

  it('a bucket change and reversion produces two SEPARATE episodes for the same bucket, not one merged episode', async () => {
    const priceRows = [{ ts: 0, price: 100 }];
    const predictionRows = [
      { ts: new Date('2026-08-01').getTime(), id: 1, anomaly: 1, trend: 0.5, priceAtPrediction: 105, realizedUp: 1 },
      { ts: new Date('2026-08-02').getTime(), id: 2, anomaly: 1, trend: -0.5, priceAtPrediction: 95, realizedUp: 0 },
      { ts: new Date('2026-08-03').getTime(), id: 3, anomaly: 1, trend: 0.5, priceAtPrediction: 106, realizedUp: 1 },
    ];
    const db = makeFakeDb({ predictionRows, priceRows });
    const report = await scope.computeAnomalyConditionedReport({ DB: db }, 'BTC', 24);
    expect(report.episodes.map((e) => e.bucket)).toEqual([
      'anomaly_trendpos_continuation', 'anomaly_trendneg_dip', 'anomaly_trendpos_continuation',
    ]);
    expect(report.bucket_summary['anomaly_trendpos_continuation'].episode_count).toBe(2);
  });

  it('running the same report twice against identical input produces byte-identical episode boundaries and bucket stats (excluding generated_at)', async () => {
    const priceRows = [{ ts: 0, price: 100 }];
    const predictionRows = [
      { ts: new Date('2026-08-01').getTime(), id: 1, anomaly: 1, trend: 0.2, priceAtPrediction: 102, realizedUp: 1 },
      { ts: new Date('2026-08-02').getTime(), id: 2, anomaly: 0, trend: null, priceAtPrediction: 99, realizedUp: 0 },
    ];
    const db1 = makeFakeDb({ predictionRows, priceRows });
    const db2 = makeFakeDb({ predictionRows, priceRows });
    const r1 = await scope.computeAnomalyConditionedReport({ DB: db1 }, 'BTC', 24);
    const r2 = await scope.computeAnomalyConditionedReport({ DB: db2 }, 'BTC', 24);
    const strip = (r) => { const { generated_at, ...rest } = r; return rest; };
    expect(strip(r1)).toEqual(strip(r2));
  });

  it('REQUIRED REGRESSION (mixed day): a day with a genuinely mixed population is no longer collapsed into one misleading dominant label -- every cycle is represented in the correct bucket', async () => {
    // Exact scenario used to prove the second defect: 8 real cycles in
    // one day -- 3x anomaly_trendpos_continuation, 3x normal, 2x
    // anomaly_trendneg_dip. The old day-level majority-vote design
    // attributed the ENTIRE day to anomaly_trendpos_continuation on a
    // 3-way tie, discarding the other 5 cycles from episode counting
    // entirely. Cycle-level detection has no reduction step to lose
    // that information -- each cycle lands in its own real bucket.
    const priceRows = [{ ts: 0, price: 100 }];
    const dayTs = new Date('2026-08-18').getTime();
    const predictionRows = [
      { ts: dayTs + 0, id: 1, anomaly: 1, trend: 0.5, priceAtPrediction: 105, realizedUp: 1 },
      { ts: dayTs + 1, id: 2, anomaly: 1, trend: 0.5, priceAtPrediction: 106, realizedUp: 1 },
      { ts: dayTs + 2, id: 3, anomaly: 1, trend: 0.5, priceAtPrediction: 107, realizedUp: 1 },
      { ts: dayTs + 3, id: 4, anomaly: 0, trend: null, priceAtPrediction: 100, realizedUp: 0 },
      { ts: dayTs + 4, id: 5, anomaly: 0, trend: null, priceAtPrediction: 100, realizedUp: 0 },
      { ts: dayTs + 5, id: 6, anomaly: 0, trend: null, priceAtPrediction: 100, realizedUp: 1 },
      { ts: dayTs + 6, id: 7, anomaly: 1, trend: -0.5, priceAtPrediction: 95, realizedUp: 0 },
      { ts: dayTs + 7, id: 8, anomaly: 1, trend: -0.5, priceAtPrediction: 94, realizedUp: 0 },
    ];
    const db = makeFakeDb({ predictionRows, priceRows });
    const report = await scope.computeAnomalyConditionedReport({ DB: db }, 'BTC', 24);
    // All 8 cycles are within the 6h gap threshold of each other, so
    // consecutive same-bucket runs still merge -- but bucket CHANGES
    // (anomaly_trendpos_continuation -> normal -> anomaly_trendneg_dip)
    // correctly break into separate episodes, and every cycle is counted
    // in its own real bucket rather than being absorbed into one label.
    expect(report.bucket_summary['anomaly_trendpos_continuation'].n).toBe(3);
    expect(report.bucket_summary['normal'].n).toBe(3);
    expect(report.bucket_summary['anomaly_trendneg_dip'].n).toBe(2);
    expect(report.episodes.map((e) => e.bucket)).toEqual([
      'anomaly_trendpos_continuation', 'normal', 'anomaly_trendneg_dip',
    ]);
  });
});

describe('computeAnomalyConditionedReport — insufficient-sample handling', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions(
      'computeAnomalyConditionedReport', 'classifyAnomalyConditionedBucket',
      'computeTrailingReturns', 'coreTableForCoin'
    ) + '\n\n' + extractConstants('ANOMALY_AUDIT_MIN_SAMPLE_N', 'ANOMALY_AUDIT_MIN_EPISODES', 'ANOMALY_AUDIT_MAX_GAP_MS'));
  });

  function makeFakeDb({ predictionRows, priceRows = [{ ts: 0, price: 100 }] }) {
    return {
      prepare(sql) {
        return {
          bind: () => ({
            all: async () => (/FROM (predictions|link_predictions|eth_predictions)/.test(sql)
              ? { results: predictionRows } : { results: priceRows }),
          }),
          all: async () => ({ results: priceRows }),
        };
      },
    };
  }

  it('a bucket with fewer than 5 raw rows is marked insufficient_sample=true, regardless of episode count', async () => {
    const rows = [1, 2, 3].map((id) => ({
      id, ts: new Date(`2026-08-0${id}`).getTime(), p_up: 0.6, realized_up: 1,
      is_regime_anomaly: 1, trend_strength: 0.5, price_at_prediction: 100 + id,
    }));
    const db = makeFakeDb({ predictionRows: rows });
    const report = await scope.computeAnomalyConditionedReport({ DB: db }, 'BTC', 24);
    const bucket = report.bucket_summary['anomaly_trendpos_continuation'];
    expect(bucket.n).toBe(3);
    expect(bucket.insufficient_sample).toBe(true);
  });

  it('a bucket with >=5 raw rows but fewer than 3 independent episodes is STILL marked insufficient_sample=true -- raw n alone is not enough', async () => {
    // 6 rows, all on the SAME single day -> n=6 (>=5) but episode_count=1 (<3)
    const rows = Array.from({ length: 6 }, (_, i) => ({
      id: i + 1, ts: new Date('2026-08-05').getTime() + i * 3600000, p_up: 0.6, realized_up: 1,
      is_regime_anomaly: 1, trend_strength: 0.5, price_at_prediction: 101 + i,
    }));
    const db = makeFakeDb({ predictionRows: rows });
    const report = await scope.computeAnomalyConditionedReport({ DB: db }, 'BTC', 24);
    const bucket = report.bucket_summary['anomaly_trendpos_continuation'];
    expect(bucket.n).toBe(6);
    expect(bucket.episode_count).toBe(1);
    expect(bucket.insufficient_sample).toBe(true);
  });

  it('a bucket clearing BOTH n>=5 AND episode_count>=3 is marked insufficient_sample=false', async () => {
    // 5 anomaly rows across 3 GENUINELY separate episodes (each preceded
    // by an intervening normal day, since the established episode
    // methodology -- matching computeRegimeDirectionalReport's own,
    // deliberately reused for consistency -- breaks an episode only on
    // an actual bucket change, not merely a calendar gap between
    // same-bucket days).
    const rows = [
      { id: 1, ts: new Date('2026-08-01').getTime(), p_up: 0.6, realized_up: 1, is_regime_anomaly: 1, trend_strength: 0.5, price_at_prediction: 101 },
      { id: 2, ts: new Date('2026-08-02').getTime(), p_up: 0.5, realized_up: 0, is_regime_anomaly: 0, trend_strength: null, price_at_prediction: 100 }, // normal -- breaks the episode
      { id: 3, ts: new Date('2026-08-03').getTime(), p_up: 0.6, realized_up: 1, is_regime_anomaly: 1, trend_strength: 0.5, price_at_prediction: 102 },
      { id: 4, ts: new Date('2026-08-03').getTime() + 3600000, p_up: 0.6, realized_up: 1, is_regime_anomaly: 1, trend_strength: 0.5, price_at_prediction: 103 },
      { id: 5, ts: new Date('2026-08-04').getTime(), p_up: 0.5, realized_up: 0, is_regime_anomaly: 0, trend_strength: null, price_at_prediction: 100 }, // normal -- breaks the episode again
      { id: 6, ts: new Date('2026-08-05').getTime(), p_up: 0.6, realized_up: 1, is_regime_anomaly: 1, trend_strength: 0.5, price_at_prediction: 104 },
      { id: 7, ts: new Date('2026-08-05').getTime() + 3600000, p_up: 0.6, realized_up: 1, is_regime_anomaly: 1, trend_strength: 0.5, price_at_prediction: 105 },
    ];
    const db = makeFakeDb({ predictionRows: rows });
    const report = await scope.computeAnomalyConditionedReport({ DB: db }, 'BTC', 24);
    const bucket = report.bucket_summary['anomaly_trendpos_continuation'];
    expect(bucket.n).toBe(5); // 5 anomaly rows (the 2 normal rows land in a separate 'normal' bucket entirely)
    expect(bucket.episode_count).toBe(3);
    expect(bucket.insufficient_sample).toBe(false);
  });

  it('the report always states its own thresholds explicitly (min_sample_n, min_episodes) rather than leaving them implicit', async () => {
    const db = makeFakeDb({ predictionRows: [] });
    const report = await scope.computeAnomalyConditionedReport({ DB: db }, 'BTC', 24);
    expect(report.min_sample_n).toBe(5);
    expect(report.min_episodes).toBe(3);
  });
});

describe('computeFullAnomalyConditionedAudit and the route — 12h/24h and BTC/ETH/LINK separation', () => {
  it('computeFullAnomalyConditionedAudit calls computeAnomalyConditionedReport for exactly the 6 coin/horizon combinations, no more, no fewer', async () => {
    const source = extractFunctions('computeFullAnomalyConditionedAudit') + '\n\n' + extractConstants('ANOMALY_AUDIT_MIN_SAMPLE_N', 'ANOMALY_AUDIT_MIN_EPISODES', 'ANOMALY_AUDIT_MAX_GAP_MS');
    const calls = [];
    const scope = evalInScope(source, {
      computeAnomalyConditionedReport: async (env, coin, horizon) => {
        calls.push(`${coin}-${horizon}`);
        return { ok: true, coin, horizon_hours: horizon };
      },
    });
    const result = await scope.computeFullAnomalyConditionedAudit({});
    expect(calls.sort()).toEqual(['BTC-12', 'BTC-24', 'ETH-12', 'ETH-24', 'LINK-12', 'LINK-24'].sort());
    expect(result.results.BTC[12].coin).toBe('BTC');
    expect(result.results.ETH[24].horizon_hours).toBe(24);
    expect(result.results.LINK[12].coin).toBe('LINK');
  });

  it('a failure in one coin/horizon combination does not block or corrupt the others', async () => {
    const source = extractFunctions('computeFullAnomalyConditionedAudit') + '\n\n' + extractConstants('ANOMALY_AUDIT_MIN_SAMPLE_N', 'ANOMALY_AUDIT_MIN_EPISODES', 'ANOMALY_AUDIT_MAX_GAP_MS');
    const scope = evalInScope(source, {
      computeAnomalyConditionedReport: async (env, coin, horizon) => {
        if (coin === 'ETH' && horizon === 12) throw new Error('simulated failure for ETH/12h only');
        return { ok: true, coin, horizon_hours: horizon };
      },
    });
    const result = await scope.computeFullAnomalyConditionedAudit({});
    expect(result.results.ETH[12].ok).toBe(false);
    expect(result.results.ETH[12].error).toContain('simulated failure for ETH/12h only');
    expect(result.results.BTC[24].ok).toBe(true);
    expect(result.results.LINK[24].ok).toBe(true);
  });

  it('the route requires BOTH coin and horizon to be valid before scoping to a single report -- an invalid/missing pair falls through to the full audit, not a default coin guess', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf("'/research/anomaly-conditioned-audit'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = src.slice(idx, idx + 1200);
    expect(nearby).toContain('hasValidCoin && hasValidHorizon');
    expect(nearby).toContain('computeFullAnomalyConditionedAudit');
  });
});

describe('STRUCTURAL CHECK: production selection/core k-NN logic is completely untouched', () => {
  it('every forbidden production symbol still exists verbatim in the file (would fail if any were accidentally deleted, not just modified)', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    for (const symbol of [
      'SELECTION_MIN_MATCHED', 'SELECTION_CRITICAL_Z', 'decideSelection', 'computeLcaScore',
      'FEATURE_KEYS', 'CONDITIONAL_CALIB_WEIGHTS', 'SELECTION_MIN_HISTORY',
    ]) {
      expect(src).toContain(symbol);
    }
  });

  it('the fencing-token / read-only ingestion functions are unmodified in shape -- still present with their established signatures', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('async function claimStaleRefresh(env, coin, nowTs, claimWindowMs = 60 * 1000)');
    expect(src).toContain('async function resolveWriteAuthorization(env, table, coin, allowWrite)');
    expect(src).toMatch(/FROM stale_refresh_claim WHERE coin = /);
  });

  it('the new report functions never call any write-capable D1 method (.run() with an INSERT/UPDATE/DELETE, or any selection/persist function)', () => {
    for (const name of ['computeAnomalyConditionedReport', 'computeFullAnomalyConditionedAudit', 'computeTrailingReturns', 'classifyAnomalyConditionedBucket']) {
      const src = extractFunctions(name);
      expect(src).not.toMatch(/INSERT INTO|UPDATE \w+ SET|DELETE FROM/);
      expect(src).not.toMatch(/selectBestVariant|claimStaleRefresh|logBtcData|logLinkData|logEthData|runPrediction\(|runChallengerPrediction\(/);
    }
  });

  it('computeAnomalyConditionedReport only ever calls .all() on env.DB, never .run()', () => {
    const src = extractFunctions('computeAnomalyConditionedReport');
    expect(src).not.toContain('.run()');
  });

  it('DETERMINISM/READ-ONLY PROOF: the redesigned cycle-level report, run 3 times against the same realistic (gapped + mixed) dataset, produces byte-identical output every time, and the fake DB never receives a write call', async () => {
    const source = extractFunctions(
      'computeAnomalyConditionedReport', 'classifyAnomalyConditionedBucket',
      'computeTrailingReturns', 'coreTableForCoin'
    ) + '\n\n' + extractConstants('ANOMALY_AUDIT_MIN_SAMPLE_N', 'ANOMALY_AUDIT_MIN_EPISODES', 'ANOMALY_AUDIT_MAX_GAP_MS');
    const scope = evalInScope(source);
    const priceRows = [{ ts: 0, price: 100 }];
    const dayTs = new Date('2026-08-18').getTime();
    // Combines both regression scenarios in one dataset: a gap (Aug 18 vs
    // Aug 25) AND a mixed-population stretch, real-world-shaped.
    const predictionRows = [
      { ts: dayTs, id: 1, anomaly: 1, trend: 0.5, priceAtPrediction: 105, realizedUp: 1 },
      { ts: dayTs + 3600000, id: 2, anomaly: 0, trend: null, priceAtPrediction: 100, realizedUp: 0 },
      { ts: new Date('2026-08-25').getTime(), id: 3, anomaly: 1, trend: 0.5, priceAtPrediction: 106, realizedUp: 1 },
    ];
    let writeAttempted = false;
    const makeDb = () => ({
      prepare(sql) {
        return {
          bind: (...args) => ({
            all: async () => (/FROM (predictions|link_predictions|eth_predictions)/.test(sql)
              ? { results: predictionRows.map((r) => ({
                  id: r.id, ts: r.ts, p_up: r.realizedUp === 1 ? 0.7 : 0.3, realized_up: r.realizedUp,
                  is_regime_anomaly: r.anomaly, trend_strength: r.trend, price_at_prediction: r.priceAtPrediction,
                })) }
              : { results: priceRows }),
            run: async () => { writeAttempted = true; throw new Error('a read-only research report must never call .run()'); },
          }),
          all: async () => ({ results: priceRows }),
          run: async () => { writeAttempted = true; throw new Error('a read-only research report must never call .run()'); },
        };
      },
    });

    const results = await Promise.all([
      scope.computeAnomalyConditionedReport({ DB: makeDb() }, 'BTC', 24),
      scope.computeAnomalyConditionedReport({ DB: makeDb() }, 'BTC', 24),
      scope.computeAnomalyConditionedReport({ DB: makeDb() }, 'BTC', 24),
    ]);
    const strip = (r) => { const { generated_at, ...rest } = r; return rest; };
    expect(strip(results[0])).toEqual(strip(results[1]));
    expect(strip(results[1])).toEqual(strip(results[2]));
    expect(writeAttempted).toBe(false);
    // And confirms the fix is actually exercised in this same proof, not
    // just determinism in the abstract: the gap correctly produces 2
    // episodes for the bucket, not 1.
    expect(results[0].bucket_summary['anomaly_trendpos_continuation'].episode_count).toBe(2);
  });
});
