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
