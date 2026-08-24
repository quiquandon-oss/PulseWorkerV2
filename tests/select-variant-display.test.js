import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions } from './helpers/extract.js';

// Fake D1 for getLatestSelection: only needs .prepare().bind().first(),
// no writes -- proving this function is genuinely read-only is the whole
// point of this fix.
function makeSelectionFakeDb({ row = null } = {}) {
  const queries = [];
  return {
    db: {
      prepare(sql) {
        queries.push(sql);
        return {
          bind: (...args) => ({
            async first() { return row; },
            async run() { throw new Error('getLatestSelection must never write -- this is the exact regression this fix exists to prevent'); },
          }),
        };
      },
    },
    queries,
  };
}

describe('getLatestSelection — read-only display, never recomputes or writes', () => {
  let scope;

  function evalInScope(src) {
    // Minimal local eval matching the shared helper's contract, since this
    // file only needs one function and no shared constants.
    const fn = new Function(`${src}\nreturn { getLatestSelection };`);
    return fn();
  }

  beforeAll(() => {
    scope = evalInScope(extractFunctions('getLatestSelection'));
  });

  it('reads the query as a SELECT only -- structurally cannot write', async () => {
    const { db, queries } = makeSelectionFakeDb({ row: null });
    await scope.getLatestSelection({ DB: db }, 'BTC', 24);
    expect(queries.every(q => q.trim().toUpperCase().startsWith('SELECT'))).toBe(true);
  });

  it('no row yet: status no_selection_yet, honest about why, not an error', async () => {
    const { db } = makeSelectionFakeDb({ row: null });
    const result = await scope.getLatestSelection({ DB: db }, 'ETH', 24);
    expect(result.ok).toBe(true);
    expect(result.status).toBe('no_selection_yet');
    expect(result.chosen_variant).toBe('original');
    expect(result.cleared_gate).toBe(false);
  });

  it('a real row: fields mapped correctly, scores_json parsed into the scores array', async () => {
    const row = {
      coin: 'ETH', horizon_hours: 24, chosen_variant: 'challenger_calibrated', chosen_p_up: 0.657,
      cleared_gate: 1, comparison_count: 3, k_sel: 15, reason: 'challenger_calibrated locally outperformed',
      ts: 1787572851819,
      scores_json: JSON.stringify([
        { variant: 'original', p_up: 0.6, lca: 0.75, n_matched: 8 },
        { variant: 'challenger_calibrated', p_up: 0.657, lca: 0.9, n_matched: 10 },
      ]),
    };
    const { db } = makeSelectionFakeDb({ row });
    const result = await scope.getLatestSelection({ DB: db }, 'ETH', 24);
    expect(result.ok).toBe(true);
    expect(result.status).toBe('ok');
    expect(result.chosen_variant).toBe('challenger_calibrated');
    expect(result.cleared_gate).toBe(true); // real boolean, not the raw D1 integer 1
    expect(result.scores).toHaveLength(2);
    expect(result.scores[1]).toEqual({ variant: 'challenger_calibrated', p_up: 0.657, lca: 0.9, n_matched: 10 });
  });

  it('an older row that predates scores_json: empty scores array, never fabricated', async () => {
    const row = {
      coin: 'BTC', horizon_hours: 24, chosen_variant: 'original', chosen_p_up: 0.5,
      cleared_gate: 0, comparison_count: 2, k_sel: 12, reason: 'no edge cleared',
      ts: 1787000000000,
      scores_json: null, // this column didn't exist when this row was written
    };
    const { db } = makeSelectionFakeDb({ row });
    const result = await scope.getLatestSelection({ DB: db }, 'BTC', 24);
    expect(result.ok).toBe(true);
    expect(result.scores).toEqual([]);
  });

  it('malformed scores_json (should never happen, but never crashes the route): empty array, not a thrown error', async () => {
    const row = { coin: 'BTC', horizon_hours: 24, chosen_variant: 'original', cleared_gate: 0, comparison_count: 1, k_sel: 7, reason: 'x', ts: 1, scores_json: 'not json' };
    const { db } = makeSelectionFakeDb({ row });
    const result = await scope.getLatestSelection({ DB: db }, 'BTC', 24);
    expect(result.ok).toBe(true);
    expect(result.scores).toEqual([]);
  });
});
