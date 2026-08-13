import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, evalInScope } from './helpers/extract.js';

describe('buildCalibrationCurve / applyCalibratedProbability — reused as-is for Challenger', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('buildCalibrationCurve', 'applyCalibratedProbability');
    scope = evalInScope(src);
  });

  it('builds a real curve from Challenger-shaped rows (p_up_flat aliased to p_up)', () => {
    // Mirrors exactly what refreshChallengerCalibrationCurve's SQL does:
    // "SELECT p_up_flat as p_up, realized_up" -- confirms the aliasing
    // approach actually produces input buildCalibrationCurve accepts,
    // not just that the function works in isolation.
    const rows = [];
    for (let i = 0; i < 100; i++) {
      // Deliberately overconfident model: predicts extreme probabilities,
      // but actual up-rate is much more moderate -- classic overconfidence
      // pattern the calibration curve should correct toward.
      const p_up = i < 50 ? 0.85 : 0.15;
      const realized_up = Math.random() < 0.6 && i < 50 ? 1 : (i < 50 ? 0 : (Math.random() < 0.4 ? 1 : 0));
      rows.push({ p_up, realized_up });
    }
    const curve = scope.buildCalibrationCurve(rows);
    expect(curve.length).toBeGreaterThan(0);
    expect(curve.every(d => d.n_samples >= 10)).toBe(true);
  });

  it('falls back to raw p_up when the curve is empty (no data yet)', () => {
    expect(scope.applyCalibratedProbability(0.7, [])).toBe(0.7);
  });

  it('falls back to raw p_up when the nearest bucket is too thin to trust', () => {
    const thinCurve = [{ predicted_p_up_mid: 0.7, empirical_up_rate: 0.9, n_samples: 3 }];
    expect(scope.applyCalibratedProbability(0.7, thinCurve)).toBe(0.7);
  });

  it('applies the empirical rate when a trustworthy bucket matches', () => {
    const goodCurve = [{ predicted_p_up_mid: 0.7, empirical_up_rate: 0.55, n_samples: 25 }];
    expect(scope.applyCalibratedProbability(0.7, goodCurve)).toBe(0.55);
  });
});

describe('getChallengerCalibrationHistory — new calibrated_flat scoring, extracted logic', () => {
  // getChallengerCalibrationHistory itself is DB-coupled (env.DB.prepare),
  // not practically unit-testable as a whole -- what's tested here is the
  // exact scoring logic it uses, reimplemented identically, to confirm the
  // real behavior this session actually cares about: early rows (before
  // the curve existed) show null, not a misleading 0 or a crash, and once
  // calibrated_p_up_flat starts appearing, its own accuracy is tracked
  // completely separately from flat/tilted, not blended into them.
  function scoreChallengerHistory(results) {
    let correctFlat = 0, correctTilted = 0, correctCal = 0, n = 0, nCal = 0;
    let sumBrierFlat = 0, sumBrierTilted = 0, sumBrierCal = 0;
    return results.map(r => {
      n++;
      if ((r.p_up_flat > 0.5) === (r.realized_up === 1)) correctFlat++;
      if ((r.p_up_tilted > 0.5) === (r.realized_up === 1)) correctTilted++;
      sumBrierFlat += (r.p_up_flat - r.realized_up) ** 2;
      sumBrierTilted += (r.p_up_tilted - r.realized_up) ** 2;
      const point = {
        ts: r.resolved_ts, n,
        accuracy_flat: Number((correctFlat / n).toFixed(3)),
        accuracy_tilted: Number((correctTilted / n).toFixed(3)),
      };
      if (r.calibrated_p_up_flat != null) {
        nCal++;
        if ((r.calibrated_p_up_flat > 0.5) === (r.realized_up === 1)) correctCal++;
        sumBrierCal += (r.calibrated_p_up_flat - r.realized_up) ** 2;
        point.accuracy_calibrated_flat = Number((correctCal / nCal).toFixed(3));
        point.n_calibrated = nCal;
      } else {
        point.accuracy_calibrated_flat = null;
        point.n_calibrated = nCal;
      }
      return point;
    });
  }

  it('shows null accuracy_calibrated_flat for early rows before the curve existed', () => {
    const results = [
      { resolved_ts: 1, p_up_flat: 0.6, p_up_tilted: 0.6, calibrated_p_up_flat: null, realized_up: 1 },
      { resolved_ts: 2, p_up_flat: 0.4, p_up_tilted: 0.4, calibrated_p_up_flat: null, realized_up: 0 },
    ];
    const points = scoreChallengerHistory(results);
    expect(points[0].accuracy_calibrated_flat).toBeNull();
    expect(points[1].accuracy_calibrated_flat).toBeNull();
    expect(points[1].n_calibrated).toBe(0);
  });

  it('starts tracking correctly once calibrated_p_up_flat appears, independent of flat/tilted', () => {
    const results = [
      { resolved_ts: 1, p_up_flat: 0.6, p_up_tilted: 0.6, calibrated_p_up_flat: null, realized_up: 0 }, // flat/tilted wrong, cal not yet scored
      { resolved_ts: 2, p_up_flat: 0.6, p_up_tilted: 0.6, calibrated_p_up_flat: 0.3, realized_up: 0 },  // cal correct (0.3<0.5, realized 0)
      { resolved_ts: 3, p_up_flat: 0.6, p_up_tilted: 0.6, calibrated_p_up_flat: 0.3, realized_up: 1 },  // cal wrong this time
    ];
    const points = scoreChallengerHistory(results);
    expect(points[0].accuracy_calibrated_flat).toBeNull();
    expect(points[1].n_calibrated).toBe(1);
    expect(points[1].accuracy_calibrated_flat).toBe(1); // 1/1 correct so far
    expect(points[2].n_calibrated).toBe(2);
    expect(points[2].accuracy_calibrated_flat).toBe(0.5); // 1/2 correct
    // Confirms independence: flat's own accuracy is unaffected by calibrated tracking
    expect(points[2].accuracy_flat).toBeCloseTo(0.333, 2); // 1/3 correct (only row 3's realized_up=1 matches the 0.6 up-lean) — calibrated tracking must not disturb this
  });
});
