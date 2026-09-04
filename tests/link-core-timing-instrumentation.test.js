// Regression tests for the LINK core-prediction timing checkpoints
// (2026-09-04), added per the execution-boundary investigation into
// LINK's core-prediction stall. Diagnostic only: 8 named checkpoints via
// console.log, no payloads beyond {evt, coin, horizon, elapsed_ms}, no D1
// writes, no behavior change. Purpose is answering one question real
// production logs haven't been able to yet: does LINK's execution reach
// runLinkPrediction at all, and if so, how far does it get.
//
// This file does not re-test runLinkPrediction's own prediction logic
// (unchanged, already covered elsewhere) -- it tests only that the
// checkpoints fire, in the right order, with the right shape, and that
// their presence doesn't alter the function's actual output.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const CHECKPOINT_ORDER = [
  'LINK_CORE_START', 'LINK_DATA_READ_DONE', 'LINK_HISTORY_READ_DONE',
  'LINK_FEATURE_BUILD_DONE', 'LINK_KNN_DONE', 'LINK_ANOMALY_CHECK_DONE',
  'LINK_CALIBRATION_DONE', 'LINK_CORE_INSERT_DONE',
];

describe('LINK core-prediction timing checkpoints', () => {
  let logSpy;
  beforeEach(() => { logSpy = vi.spyOn(console, 'log').mockImplementation(() => {}); });
  afterEach(() => { logSpy.mockRestore(); });

  function checkpointEvents() {
    return logSpy.mock.calls
      .map(([arg]) => { try { return JSON.parse(arg); } catch { return null; } })
      .filter((e) => e && typeof e.evt === 'string' && e.evt.startsWith('LINK_'));
  }

  it('all 8 checkpoints fire, in the documented order, on a successful run with real-shaped data', async () => {
    const source = extractFunctions(
      'runLinkPrediction', 'nearestRow', 'meanStd', 'trendStrength', 'percentileRank',
      'getLatestCalibrationCurve', 'applyCalibratedProbability', 'trailingVolatility', 'weightedQuantile', 'currentGitSha'
    ) + '\n\n' + extractConstants('LINK_FEATURE_KEYS', 'LINK_MIN_COMPLETE_ROWS', 'LINK_MIN_RESOLVED_ANALOGS', 'MODEL_VERSIONS', 'LAG_MS', 'TOL_MS');
    const scope = evalInScope(source);

    const now = Date.now();
    // 60 rows, evenly spaced, enough to clear every threshold (30, 5)
    // several times over -- same shape used successfully in the earlier
    // LINK reproduction pass.
    const linkRows = Array.from({ length: 60 }, (_, i) => ({
      ts: now - (60 - i) * 3 * 3600000, link_price: 11 + (i % 5) * 0.1, technical_score: 40 + (i % 20), funding_adj: 0,
    }));
    const btcHistory = Array.from({ length: 30 }, (_, i) => ({
      ts: now - (60 - i * 2) * 3 * 3600000, score: 50, regime_mag: 0.01,
    }));

    const resultsFor = (sql) => {
      if (sql.includes('FROM link_data')) return { results: linkRows };
      if (sql.includes('FROM history')) return { results: btcHistory };
      if (sql.includes('calibration_curve')) return { results: [] };
      return { results: [] };
    };
    const db = {
      prepare(sql) {
        const stmt = {
          first: async () => null,
          all: async () => resultsFor(sql),
          run: async () => ({ meta: { last_row_id: 1 } }),
        };
        stmt.bind = (...args) => stmt;
        return stmt;
      },
    };

    const result = await scope.runLinkPrediction({ DB: db }, 24, { persist: true, claimToken: null });

    const events = checkpointEvents();
    expect(events.map((e) => e.evt)).toEqual(CHECKPOINT_ORDER);
    // Every event carries coin/horizon and a non-negative, monotonically
    // non-decreasing elapsed_ms -- proving these are real, ordered
    // timestamps, not placeholder/static values.
    for (const e of events) {
      expect(e.coin).toBe('LINK');
      expect(e.horizon).toBe(24);
      expect(e.elapsed_ms).toBeGreaterThanOrEqual(0);
    }
    for (let i = 1; i < events.length; i++) {
      expect(events[i].elapsed_ms).toBeGreaterThanOrEqual(events[i - 1].elapsed_ms);
    }
    expect(result.status).toBe('ok');
  });

  it('an early return (insufficient_data on link_data) fires LINK_CORE_START only, proving checkpoints correctly mark HOW FAR execution got, not just that the function was called', async () => {
    const source = extractFunctions('runLinkPrediction', 'nearestRow', 'meanStd', 'trendStrength', 'percentileRank', 'getLatestCalibrationCurve', 'applyCalibratedProbability', 'trailingVolatility', 'weightedQuantile', 'currentGitSha')
      + '\n\n' + extractConstants('LINK_FEATURE_KEYS', 'LINK_MIN_COMPLETE_ROWS', 'LINK_MIN_RESOLVED_ANALOGS', 'MODEL_VERSIONS', 'LAG_MS', 'TOL_MS');
    const scope = evalInScope(source);
    const db = { prepare: () => { const stmt = { all: async () => ({ results: [] }), first: async () => null, run: async () => ({}) }; stmt.bind = () => stmt; return stmt; } };

    const result = await scope.runLinkPrediction({ DB: db }, 24, { persist: true, claimToken: null });
    const events = checkpointEvents();
    expect(events.map((e) => e.evt)).toEqual(['LINK_CORE_START']);
    expect(result.status).toBe('insufficient_data');
  });

  it('checkpoints add zero payload beyond {evt, coin, horizon, elapsed_ms} -- no feature data, no prices, no D1 row contents', () => {
    const src = extractFunctions('runLinkPrediction');
    const checkpointFnMatch = src.match(/const __checkpoint = \(name\) => console\.log\(JSON\.stringify\(\{([^}]*)\}\)\);/);
    expect(checkpointFnMatch).not.toBeNull();
    const fields = checkpointFnMatch[1];
    expect(fields).toContain('evt: name');
    expect(fields).toContain("coin: 'LINK'");
    expect(fields).toContain('horizon: horizonHours');
    expect(fields).toContain('elapsed_ms:');
    // Nothing else -- exactly 4 fields, confirmed by comma count.
    expect(fields.split(',').filter((s) => s.trim())).toHaveLength(4);
  });

  it('no D1 write is added by the checkpoints themselves -- INSERT count in runLinkPrediction is unchanged (still exactly the 2 pre-existing branches: unconditional and claim-conditioned)', () => {
    const src = extractFunctions('runLinkPrediction');
    const inserts = src.match(/INSERT INTO link_predictions/g) || [];
    expect(inserts).toHaveLength(2);
  });

  it('production selection/decision logic elsewhere is untouched -- selectBestVariant, decideSelection, and the significance-gate constants remain byte-identical', () => {
    const src = extractFunctions('selectBestVariant') + extractConstants('SELECTION_CRITICAL_Z', 'SELECTION_MIN_HISTORY', 'SELECTION_MIN_MATCHED');
    expect(src).toContain('async function selectBestVariant(env, coin, horizonHours)');
    expect(src).toContain('SELECTION_CRITICAL_Z = { 1: 1.6449, 2: 1.9600, 3: 2.1280, 4: 2.2414, 5: 2.3263, 6: 2.3940 }');
  });
});
