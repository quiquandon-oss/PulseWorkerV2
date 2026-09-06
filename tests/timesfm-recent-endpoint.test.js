import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, evalInScope } from './helpers/extract.js';

// getTimesFmRecent is the read side of Experiment 4 (Google TimesFM
// research challenger). Read-only, zero writes -- proving that is the
// whole point of this suite, same rationale as select-variant-display's
// getLatestSelection tests.
describe('getTimesFmRecent — Experiment 4 (TimesFM) read-only summary + recent rows', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('getTimesFmRecent'));
  });

  function makeDb({ total = 0, resolved = 0, rows = [] } = {}) {
    const queries = [];
    return {
      queries,
      prepare(sql) {
        queries.push(sql);
        return {
          bind(...args) {
            return {
              async first() {
                expect(sql).toMatch(/SELECT COUNT\(\*\) AS total/);
                expect(sql).toMatch(/FROM experiment_4_timesfm WHERE coin = \? AND horizon_hours = \?/);
                expect(args).toEqual(['BTC', 12]);
                return { total, resolved };
              },
              async all() {
                expect(sql).toMatch(/FROM experiment_4_timesfm WHERE coin = \? AND horizon_hours = \?/);
                expect(sql).toMatch(/ORDER BY ts DESC LIMIT \?/);
                expect(args).toEqual(['BTC', 12, 20]);
                return { results: rows };
              },
            };
          },
        };
      },
    };
  }

  it('returns a bounded summary (total/resolved/unresolved) alongside the row list', async () => {
    const db = makeDb({ total: 4, resolved: 0, rows: [{ id: 1 }, { id: 2 }] });
    const result = await scope.getTimesFmRecent({ DB: db }, 'BTC', 12, 20);
    expect(result.ok).toBe(true);
    expect(result.coin).toBe('BTC');
    expect(result.horizon_hours).toBe(12);
    expect(result.summary).toEqual({ total: 4, resolved: 0, unresolved: 4 });
    expect(result.forecasts).toEqual([{ id: 1 }, { id: 2 }]);
  });

  it('zero rows in the table -- summary is all zeros, never a fabricated nonzero default', async () => {
    const db = makeDb({ total: 0, resolved: 0, rows: [] });
    const result = await scope.getTimesFmRecent({ DB: db }, 'BTC', 12, 20);
    expect(result.summary).toEqual({ total: 0, resolved: 0, unresolved: 0 });
    expect(result.forecasts).toEqual([]);
  });

  it('every query is scoped by coin AND horizon_hours -- never a bare full-table SELECT', async () => {
    const db = makeDb();
    await scope.getTimesFmRecent({ DB: db }, 'BTC', 12, 20);
    for (const sql of db.queries) {
      expect(sql).toMatch(/WHERE coin = \? AND horizon_hours = \?/);
    }
  });

  it('the row query always carries a bound LIMIT -- never an unbounded scan', async () => {
    const db = makeDb();
    await scope.getTimesFmRecent({ DB: db }, 'BTC', 12, 20);
    const rowQuery = db.queries.find((sql) => /SELECT \*/.test(sql));
    expect(rowQuery).toMatch(/LIMIT \?/);
  });
});
