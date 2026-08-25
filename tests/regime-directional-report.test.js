import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions } from './helpers/extract.js';

function evalInScope(src) {
  const fn = new Function(`${src}\nreturn { classifyRegimeBucket, computeRegimeDirectionalReport, coreTableForCoin };`);
  return fn();
}

describe('classifyRegimeBucket — pure', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('classifyRegimeBucket', 'computeRegimeDirectionalReport', 'coreTableForCoin'));
  });

  it('not anomalous -> normal, regardless of trend_strength', () => {
    expect(scope.classifyRegimeBucket(0, 0.5)).toBe('normal');
    expect(scope.classifyRegimeBucket(0, null)).toBe('normal');
    expect(scope.classifyRegimeBucket(0, -0.9)).toBe('normal');
  });

  it('anomalous + null trend_strength -> no_trend_data, not silently dropped or miscategorized', () => {
    expect(scope.classifyRegimeBucket(1, null)).toBe('no_trend_data');
  });

  it('anomalous + positive/negative/exact-zero trend_strength -> the three directional buckets', () => {
    expect(scope.classifyRegimeBucket(1, 0.05)).toBe('anomaly_pos_trend');
    expect(scope.classifyRegimeBucket(1, -0.05)).toBe('anomaly_neg_trend');
    expect(scope.classifyRegimeBucket(1, 0)).toBe('anomaly_zero_trend');
  });

  it('is_regime_anomaly values other than exactly 1 (e.g. undefined) are treated as not-anomalous, never crash', () => {
    expect(scope.classifyRegimeBucket(undefined, 0.5)).toBe('normal');
    expect(scope.classifyRegimeBucket(null, 0.5)).toBe('normal');
  });
});

// Fake DB: routes by table name in the SQL, returns pre-seeded rows either
// way. Confirms the function never writes (no .run() implemented at all --
// a stray write attempt throws, which is itself the regression test).
function makeReportFakeDb({ coreRows = [], challengerRows = [] } = {}) {
  return {
    prepare(sql) {
      return {
        bind: (...args) => ({
          all: async () => {
            if (sql.includes('FROM challenger_predictions')) return { results: challengerRows };
            return { results: coreRows };
          },
        }),
      };
    },
  };
}

describe('computeRegimeDirectionalReport — bucket summary', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('classifyRegimeBucket', 'computeRegimeDirectionalReport', 'coreTableForCoin'));
  });

  it('computes accuracy/pct_actually_up/baselines per bucket, matching the audit\'s hand-computed formulas', async () => {
    const coreRows = [
      // normal bucket: 2 rows, 1 correct, 1 up
      { ts: 1000, p_up: 0.7, realized_up: 1, is_regime_anomaly: 0, trend_strength: null },
      { ts: 2000, p_up: 0.7, realized_up: 0, is_regime_anomaly: 0, trend_strength: null },
      // anomaly_pos_trend: 2 rows, both up, 0 correct (both predicted down but went up)
      { ts: 3000, p_up: 0.3, realized_up: 1, is_regime_anomaly: 1, trend_strength: 0.1 },
      { ts: 4000, p_up: 0.2, realized_up: 1, is_regime_anomaly: 1, trend_strength: 0.2 },
    ];
    const db = makeReportFakeDb({ coreRows });
    const report = await scope.computeRegimeDirectionalReport({ DB: db }, 'BTC', 24);
    expect(report.ok).toBe(true);
    expect(report.raw_prediction_count).toBe(4);
    expect(report.bucket_summary.normal).toEqual({
      n: 2, original_accuracy: 50, pct_actually_up: 50,
      always_up_baseline_accuracy: 50, always_down_baseline_accuracy: 50,
    });
    expect(report.bucket_summary.anomaly_pos_trend).toEqual({
      n: 2, original_accuracy: 0, pct_actually_up: 100,
      always_up_baseline_accuracy: 100, always_down_baseline_accuracy: 0,
    });
  });

  it('ETH gets no challenger_bucket_summary at all -- Challenger does not run for ETH', async () => {
    const db = makeReportFakeDb({ coreRows: [{ ts: 1000, p_up: 0.6, realized_up: 1, is_regime_anomaly: 0, trend_strength: null }] });
    const report = await scope.computeRegimeDirectionalReport({ DB: db }, 'ETH', 24);
    expect(report.challenger_bucket_summary).toBeNull();
  });

  it('BTC/LINK get a real challenger_bucket_summary computed from challenger_predictions, not the core table', async () => {
    const coreRows = [{ ts: 1000, p_up: 0.6, realized_up: 1, is_regime_anomaly: 0, trend_strength: null }];
    const challengerRows = [
      { p_up_flat: 0.6, p_up_tilted: 0.7, realized_up: 1, is_regime_anomaly: 0, trend_strength: null },
      { p_up_flat: 0.4, p_up_tilted: 0.3, realized_up: 1, is_regime_anomaly: 0, trend_strength: null },
    ];
    const db = makeReportFakeDb({ coreRows, challengerRows });
    const report = await scope.computeRegimeDirectionalReport({ DB: db }, 'BTC', 24);
    expect(report.challenger_bucket_summary.normal.n).toBe(2);
    expect(report.challenger_bucket_summary.normal.challenger_flat_accuracy).toBe(50); // 1 of 2 correct
    expect(report.challenger_bucket_summary.normal.challenger_tilted_accuracy).toBe(50); // 1 of 2 correct
  });

  it('never attempts a write -- the fake DB has no .run() at all, so any write attempt throws', async () => {
    const db = makeReportFakeDb({ coreRows: [{ ts: 1000, p_up: 0.6, realized_up: 1, is_regime_anomaly: 0, trend_strength: null }] });
    await expect(scope.computeRegimeDirectionalReport({ DB: db }, 'BTC', 24)).resolves.toMatchObject({ ok: true });
  });
});

describe('computeRegimeDirectionalReport — episode detection (the actual point of this instrumentation)', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('classifyRegimeBucket', 'computeRegimeDirectionalReport', 'coreTableForCoin'));
  });

  function dayRow(dateStr, hourOffset, bucket) {
    const ts = new Date(dateStr).getTime() + hourOffset * 3600000;
    if (bucket === 'normal') return { ts, p_up: 0.6, realized_up: 1, is_regime_anomaly: 0, trend_strength: 0.01 };
    if (bucket === 'anomaly_pos_trend') return { ts, p_up: 0.6, realized_up: 1, is_regime_anomaly: 1, trend_strength: 0.1 };
    if (bucket === 'anomaly_neg_trend') return { ts, p_up: 0.6, realized_up: 1, is_regime_anomaly: 1, trend_strength: -0.1 };
  }

  it('regression: many correlated 3-hourly predictions across a multi-day episode collapse to ONE episode, not N', async () => {
    // 4 consecutive days, all anomaly_pos_trend, 8 predictions/day = 32
    // raw predictions -- this is exactly the shape that made the original
    // ad-hoc audit's "345 anomaly predictions" misleading before grouping.
    const coreRows = [];
    for (const day of ['2026-08-18', '2026-08-19', '2026-08-20', '2026-08-21']) {
      for (let h = 0; h < 24; h += 3) coreRows.push(dayRow(day, h, 'anomaly_pos_trend'));
    }
    const db = makeReportFakeDb({ coreRows });
    const report = await scope.computeRegimeDirectionalReport({ DB: db }, 'BTC', 24);
    expect(report.raw_prediction_count).toBe(32);
    expect(report.episode_count_by_bucket.anomaly_pos_trend).toBe(1); // ONE episode, not 32
    expect(report.episodes).toHaveLength(1);
    expect(report.episodes[0].n_days).toBe(4);
    expect(report.episodes[0].start_date).toBe('2026-08-18');
    expect(report.episodes[0].end_date).toBe('2026-08-21');
  });

  it('a bucket change on any day starts a genuinely new episode, even if it reverts back later', async () => {
    const coreRows = [
      ...['2026-08-01', '2026-08-02'].flatMap(d => [dayRow(d, 0, 'anomaly_pos_trend')]),
      ...['2026-08-03'].flatMap(d => [dayRow(d, 0, 'anomaly_neg_trend')]),
      ...['2026-08-04', '2026-08-05'].flatMap(d => [dayRow(d, 0, 'anomaly_pos_trend')]),
    ];
    const db = makeReportFakeDb({ coreRows });
    const report = await scope.computeRegimeDirectionalReport({ DB: db }, 'BTC', 24);
    // Two separate anomaly_pos_trend episodes (not merged across the
    // intervening anomaly_neg_trend day), plus one anomaly_neg_trend episode.
    expect(report.episode_count_by_bucket.anomaly_pos_trend).toBe(2);
    expect(report.episode_count_by_bucket.anomaly_neg_trend).toBe(1);
    expect(report.episodes.map(e => e.bucket)).toEqual([
      'anomaly_pos_trend', 'anomaly_neg_trend', 'anomaly_pos_trend',
    ]);
  });

  it('a day with a genuine mix of buckets across its own 3-hourly predictions is classified by majority vote, not the first or last row', async () => {
    const coreRows = [
      dayRow('2026-08-10', 0, 'normal'),
      dayRow('2026-08-10', 3, 'anomaly_pos_trend'),
      dayRow('2026-08-10', 6, 'anomaly_pos_trend'),
      dayRow('2026-08-10', 9, 'anomaly_pos_trend'),
    ];
    const db = makeReportFakeDb({ coreRows });
    const report = await scope.computeRegimeDirectionalReport({ DB: db }, 'BTC', 24);
    expect(report.episodes).toHaveLength(1);
    expect(report.episodes[0].bucket).toBe('anomaly_pos_trend'); // 3 of 4 rows, correct majority
  });
});
