import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, evalInScope } from './helpers/extract.js';

describe('computeConditionalCalibration — direct fix for the confirmed blending bug', () => {
  let scope;
  const WEIGHTS = { score: 1.0, technical_score: 1.0, regime_mag: 1.5, bottom_score: 0.3 };

  beforeAll(() => {
    const src = extractFunctions('computeConditionalCalibration');
    scope = evalInScope(src);
  });

  it('reproduces the exact real finding: two populations with opposite calibration needs, correctly separated instead of blended', () => {
    // Deliberately mirrors the real audit numbers: a non-anomalous
    // population (regime_mag positive, "normal") where low p_up calls were
    // already accurate (mostly stayed down, matching the bearish lean),
    // and an anomalous population (regime_mag very negative, unusual)
    // where low p_up calls were actually wrong most of the time (price
    // went up despite the bearish lean) -- the exact opposite pattern.
    // A global decile curve blends these; conditional calibration should
    // correctly distinguish them via regime_mag.
    const historicalRows = [];
    // "Normal" population: regime_mag near 0, bearish calls were CORRECT (stayed down)
    for (let i = 0; i < 40; i++) {
      historicalRows.push({ features: { score: 50, technical_score: 40, regime_mag: 0.05 + (i%5)*0.01, bottom_score: 25 }, realized_up: i % 5 === 0 ? 1 : 0 }); // ~20% up rate
    }
    // "Anomalous" population: regime_mag very negative, bearish calls were WRONG (went up anyway)
    for (let i = 0; i < 40; i++) {
      historicalRows.push({ features: { score: 50, technical_score: 40, regime_mag: -0.85 - (i%5)*0.01, bottom_score: 25 }, realized_up: i % 5 < 4 ? 1 : 0 }); // ~80% up rate
    }

    // Query 1: today looks like the "normal" population (regime_mag near 0)
    const queryNormal = { score: 50, technical_score: 40, regime_mag: 0.06, bottom_score: 25 };
    const resultNormal = scope.computeConditionalCalibration(queryNormal, historicalRows, WEIGHTS, 30, 20);
    expect(resultNormal).not.toBeNull();
    expect(resultNormal.p_up).toBeLessThan(0.35); // should correctly reflect the LOW up-rate of its actual neighborhood

    // Query 2: today looks like the "anomalous" population (regime_mag very negative)
    const queryAnomalous = { score: 50, technical_score: 40, regime_mag: -0.87, bottom_score: 25 };
    const resultAnomalous = scope.computeConditionalCalibration(queryAnomalous, historicalRows, WEIGHTS, 30, 20);
    expect(resultAnomalous).not.toBeNull();
    expect(resultAnomalous.p_up).toBeGreaterThan(0.65); // should correctly reflect the HIGH up-rate of its actual neighborhood

    // The actual point: these two results must differ substantially,
    // proving the function distinguishes the two populations rather than
    // blending them into one global average (~50% either way).
    expect(resultAnomalous.p_up - resultNormal.p_up).toBeGreaterThan(0.3);
  });

  it('respects the regime_mag weight — a query matched primarily on regime_mag should still separate populations even when other features are identical', () => {
    // Already covered by the test above (all non-regime features are
    // identical across both populations), but asserted explicitly here as
    // its own regression: if regime_mag's weight were accidentally reset
    // to 0 or equal to bottom_score's, this same test would collapse to
    // ~50/50 for both queries since nothing else discriminates.
    const rows = [
      ...Array.from({length: 25}, () => ({ features: { regime_mag: 0.9, technical_score: 50, score: 50, bottom_score: 25 }, realized_up: 1 })),
      ...Array.from({length: 25}, () => ({ features: { regime_mag: -0.9, technical_score: 50, score: 50, bottom_score: 25 }, realized_up: 0 })),
    ];
    const resultHigh = scope.computeConditionalCalibration({ regime_mag: 0.85, technical_score: 50, score: 50, bottom_score: 25 }, rows, WEIGHTS, 20, 15);
    const resultLow = scope.computeConditionalCalibration({ regime_mag: -0.85, technical_score: 50, score: 50, bottom_score: 25 }, rows, WEIGHTS, 20, 15);
    expect(resultHigh.p_up).toBeGreaterThan(0.8);
    expect(resultLow.p_up).toBeLessThan(0.2);
  });

  it('returns null (not a crash, not a fabricated number) when fewer than minNeighbors are available', () => {
    const rows = Array.from({length: 10}, () => ({ features: { regime_mag: 0.1, technical_score: 50, score: 50, bottom_score: 25 }, realized_up: 1 }));
    const result = scope.computeConditionalCalibration({ regime_mag: 0.1, technical_score: 50, score: 50, bottom_score: 25 }, rows, WEIGHTS, 30, 20);
    expect(result).toBeNull();
  });

  it('handles missing feature values in historical rows without crashing', () => {
    const rows = [
      { features: { regime_mag: 0.1, technical_score: null, score: 50, bottom_score: 25 }, realized_up: 1 },
      ...Array.from({length: 25}, () => ({ features: { regime_mag: 0.1, technical_score: 50, score: 50, bottom_score: 25 }, realized_up: 1 })),
    ];
    expect(() => scope.computeConditionalCalibration({ regime_mag: 0.1, technical_score: 50, score: 50, bottom_score: 25 }, rows, WEIGHTS, 20, 15)).not.toThrow();
  });

  it('returns null gracefully when today\'s own feature vector is entirely missing/unusable', () => {
    const rows = Array.from({length: 25}, () => ({ features: { regime_mag: 0.1, technical_score: 50, score: 50, bottom_score: 25 }, realized_up: 1 }));
    const result = scope.computeConditionalCalibration({ regime_mag: null, technical_score: null, score: null, bottom_score: null }, rows, WEIGHTS, 20, 15);
    expect(result).toBeNull();
  });
});
