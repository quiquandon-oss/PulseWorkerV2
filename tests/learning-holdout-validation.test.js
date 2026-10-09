// Track C: candidates are validated only on observations made after they were created (holdout); what the
// researcher could already see is reported as exploratory and never decides the status.
import { describe, it, expect } from 'vitest';
import { validateRecalculation } from '../learning/learning-method.js';

const H = 3600000, D = 24 * H;

// One observation per day; the proposed call is right every day, the reconstructed baseline wrong every day.
function fixture(days, { storedEqualsRecon = true } = {}) {
  const btc = [], points = [];
  for (let d = 0; d <= days; d++) btc.push({ ts: d * D, btc_price: 100 + d });            // BTC rises every day
  for (let d = 0; d < days; d++) points.push({ ts: d * D, stored: storedEqualsRecon ? 40 : 60, reconstructed: 40, proposed: 60 });
  return { recalc: { possible: true, availability: { status: 'HISTORY_AVAILABLE' }, all_points: points }, btc };
}

describe('holdout validation (discoveredAt)', () => {
  it('without discoveredAt the result is exactly the previous behaviour', () => {
    const { recalc, btc } = fixture(12);
    const v = validateRecalculation(recalc, btc);
    expect(v.status).toBe('SUPPORTED');
    expect(v.validation_scope).toBeUndefined();
    expect(v.exploratory_in_sample).toBeUndefined();
  });

  it('a strong in-sample result cannot validate itself', () => {
    const { recalc, btc } = fixture(12);
    const v = validateRecalculation(recalc, btc, { discoveredAt: 20 * D });                // created after all observations
    expect(v.status).toBe('AWAITING_HOLDOUT');
    expect(v.exploratory_in_sample.status).toBe('SUPPORTED');
    expect(v.exploratory_in_sample.label).toMatch(/EXPLORATORY/);
    expect(v.resolved).toBe(0);
  });

  it('only observations after discovery decide the verdict', () => {
    const { recalc, btc } = fixture(20);
    const v = validateRecalculation(recalc, btc, { discoveredAt: 9 * D + 1 });            // days 0-9 seen, 10-19 holdout
    expect(v.exploratory_in_sample.resolved).toBe(10);
    expect(v.resolved).toBe(10);
    expect(v.status).toBe('SUPPORTED');
    expect(v.independent.days).toBe(10);
    const few = validateRecalculation(recalc, btc, { discoveredAt: 16 * D + 1 });          // only 3 holdout days
    expect(few.status).toBe('AWAITING_HOLDOUT');
    expect(few.headline).toMatch(/3 independent disagreement day\(s\) so far, 5 needed/);
  });

  it('reports the stored V1 call next to the reconstructed baseline', () => {
    const { recalc, btc } = fixture(6, { storedEqualsRecon: false });
    const v = validateRecalculation(recalc, btc);
    expect(v.stored_v1).toMatchObject({ hits: 6, total: 6, reconstruction_call_disagreements: 6 });
    expect(v.current_v1.hits).toBe(0);
  });
});

describe('holdout safeguards (product path: requireHoldout)', () => {
  it('a missing or invalid creation time fails closed, never in-sample', () => {
    const { recalc, btc } = fixture(12);
    for (const bad of [null, undefined, NaN, 'x', Infinity]) {
      const v = validateRecalculation(recalc, btc, { discoveredAt: bad, requireHoldout: true });
      expect(v.status).toBe('AWAITING_HOLDOUT');
      expect(v.resolved).toBe(0);
      expect(v.exploratory_in_sample).toBeUndefined();
    }
    // even without requireHoldout, a provided-but-invalid time fails closed
    expect(validateRecalculation(recalc, btc, { discoveredAt: NaN }).status).toBe('AWAITING_HOLDOUT');
  });

  it('an observation at exactly the creation time is in-sample; invalid timestamps are excluded and counted', () => {
    const { recalc, btc } = fixture(20);
    const v = validateRecalculation(recalc, btc, { discoveredAt: 10 * D, requireHoldout: true });
    expect(v.exploratory_in_sample.resolved).toBe(11);                       // days 0..10 inclusive
    expect(v.resolved).toBe(9);
    const withBad = { ...recalc, all_points: [...recalc.all_points, { ts: NaN, stored: 40, reconstructed: 40, proposed: 60 }] };
    const w = validateRecalculation(withBad, btc, { discoveredAt: 10 * D, requireHoldout: true });
    expect(w.invalid_timestamps_excluded).toBe(1);
    expect(w.resolved).toBe(9);
  });

  it('input order cannot change the verdict', () => {
    const { recalc, btc } = fixture(20);
    const shuffled = { ...recalc, all_points: [...recalc.all_points].reverse() };
    const a = validateRecalculation(recalc, btc, { discoveredAt: 5 * D, requireHoldout: true });
    const b = validateRecalculation(shuffled, btc, { discoveredAt: 5 * D, requireHoldout: true });
    expect(b).toEqual(a);
  });

  it('a win that only exists against a reconstruction mismatch is not counted', () => {
    // stored V1 already called UP (right) every day; only the reconstructed baseline was wrong
    const { recalc, btc } = fixture(20, { storedEqualsRecon: false });
    const v = validateRecalculation(recalc, btc, { discoveredAt: 5 * D, requireHoldout: true });
    expect(v.status).toBe('INCONCLUSIVE');
    expect(v.headline).toMatch(/Not counted as a win/);
    expect(v.reconstruction_mismatch.independent_days).toBe(14);
    expect(v.vs_stored_v1).toMatchObject({ adjusted_right_stored_wrong: 0, adjusted_wrong_stored_right: 0 });
    expect(v.baseline_used).toMatch(/^RECONSTRUCTED_V1/);
  });

  it('a real out-of-sample win names its baseline and survives the mismatch check', () => {
    const { recalc, btc } = fixture(20);                                     // stored == reconstructed (both wrong)
    const v = validateRecalculation(recalc, btc, { discoveredAt: 5 * D, requireHoldout: true });
    expect(v.status).toBe('SUPPORTED');
    expect(v.headline).toMatch(/reconstructed V1/);
    expect(v.headline).toMatch(/holds with reconstruction-mismatch days removed/);
    expect(v.reconstruction_mismatch.independent_days).toBe(0);
  });

  it('legacy path keeps its status and headline (stored_v1 is an additive field)', () => {
    const { recalc, btc } = fixture(12);
    const v = validateRecalculation(recalc, btc);
    expect(v.status).toBe('SUPPORTED');
    expect(v.headline).toBe('The adjusted V1 was right more often on the days it disagreed with current V1 (12 vs 0).');
    expect(v.validation_scope).toBeUndefined();
  });
});
