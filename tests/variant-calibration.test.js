import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions } from './helpers/extract.js';

function evalInScope(src) {
  const fn = new Function(`${src}\nreturn { getVariantCalibrationSummary };`);
  return fn();
}

// Fake DB: routes SELECT queries by which table/column they touch, since
// getVariantCalibrationSummary's SQL is built dynamically per variant.
function makeVariantCalibFakeDb({ coreRows = [], challengerRows = [] } = {}) {
  return {
    prepare(sql) {
      return {
        bind: (...args) => ({
          all: async () => {
            if (sql.includes('FROM challenger_predictions')) return { results: challengerRows };
            return { results: coreRows }; // predictions / link_predictions / eth_predictions
          },
        }),
      };
    },
  };
}

describe('getVariantCalibrationSummary — original/calibrated/experimental (core table)', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('getVariantCalibrationSummary', 'coreTableForCoin'));
  });

  it('unknown variant returns ok:false, not a crash or a silent wrong answer', async () => {
    const db = makeVariantCalibFakeDb();
    const result = await scope.getVariantCalibrationSummary({ DB: db }, 'BTC', 24, 'not_a_real_variant');
    expect(result.ok).toBe(false);
    expect(result.error).toContain('unknown variant');
  });

  it('n_resolved:0 is a legitimate state, not an error, when nothing has resolved yet', async () => {
    const db = makeVariantCalibFakeDb({ coreRows: [] });
    const result = await scope.getVariantCalibrationSummary({ DB: db }, 'BTC', 24, 'original');
    expect(result.ok).toBe(true);
    expect(result.n_resolved).toBe(0);
  });

  it('computes real accuracy/brier for the "original" variant from p_up', async () => {
    // 3 correct, 1 wrong, out of 4 -- 75% accuracy
    const coreRows = [
      { p: 0.7, realized_up: 1 }, { p: 0.6, realized_up: 1 }, { p: 0.3, realized_up: 0 }, { p: 0.8, realized_up: 0 },
    ];
    const db = makeVariantCalibFakeDb({ coreRows });
    const result = await scope.getVariantCalibrationSummary({ DB: db }, 'BTC', 24, 'original');
    expect(result.ok).toBe(true);
    expect(result.variant).toBe('original');
    expect(result.n_resolved).toBe(4);
    expect(result.accuracy).toBe(0.75);
  });

  it('"calibrated" and "experimental" variants use the same core table, just a different probability column -- verified via the actual SQL text, not just behavior', () => {
    const src = extractFunctions('getVariantCalibrationSummary');
    expect(src).toMatch(/calibrated_p_up/);
    expect(src).toMatch(/p_up_experimental/);
  });

  it('beats_naive_baseline is false and the note says so honestly when the variant genuinely underperforms', async () => {
    // All predictions confidently wrong -- brier will be terrible, worse than any naive baseline
    const coreRows = [
      { p: 0.95, realized_up: 0 }, { p: 0.9, realized_up: 0 }, { p: 0.92, realized_up: 0 }, { p: 0.88, realized_up: 0 },
    ];
    const db = makeVariantCalibFakeDb({ coreRows });
    const result = await scope.getVariantCalibrationSummary({ DB: db }, 'BTC', 24, 'original');
    expect(result.beats_naive_baseline).toBe(false);
    expect(result.note).toContain('Does NOT beat');
  });
});

describe('getVariantCalibrationSummary — challenger_flat/tilted/calibrated (challenger_predictions table)', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('getVariantCalibrationSummary', 'coreTableForCoin'));
  });

  it('challenger_flat reads p_up_flat from challenger_predictions, not the core table', async () => {
    const challengerRows = [{ p: 0.8, realized_up: 1 }, { p: 0.4, realized_up: 0 }];
    const db = makeVariantCalibFakeDb({ coreRows: [{ p: 0.1, realized_up: 1 }], challengerRows });
    const result = await scope.getVariantCalibrationSummary({ DB: db }, 'BTC', 24, 'challenger_flat');
    expect(result.ok).toBe(true);
    expect(result.n_resolved).toBe(2); // from challengerRows, not coreRows
    expect(result.accuracy).toBe(1); // both correct
  });

  it('all three challenger variants (flat/tilted/calibrated) query the right column via the actual SQL text', () => {
    const src = extractFunctions('getVariantCalibrationSummary');
    expect(src).toMatch(/p_up_flat/);
    expect(src).toMatch(/p_up_tilted/);
    expect(src).toMatch(/calibrated_p_up_flat/);
  });
});

describe('regression: /variant-calibration and the fixed /challenger-calibration ETH handling', () => {
  it('the /challenger-calibration route now explicitly rejects coin=ETH instead of silently substituting BTC data', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toMatch(/coinParam === 'ETH'/);
    expect(src).toMatch(/Challenger does not run for ETH/);
  });

  it('/variant-calibration route exists and passes the variant query param through', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toMatch(/\/variant-calibration/);
    expect(src).toMatch(/getVariantCalibrationSummary\(env, coin, horizon, variant\)/);
  });
});
