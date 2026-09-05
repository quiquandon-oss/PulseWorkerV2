import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions } from './helpers/extract.js';

function evalInScope(src) {
  const fn = new Function(`${src}\nreturn { getCachedDailyReport, refreshDailyReportCache };`);
  return fn();
}

// Fake DB tracking both reads and the upsert write, plus a spy on
// whether buildDailyReport's own expensive queries would have run --
// achieved here by making buildDailyReport itself a cheap stub, since
// this file is only testing the caching layer, not the report contents
// (already covered in tests/learning-engine.test.js).
function makeCacheFakeDb({ cachedRow = null } = {}) {
  const writes = [];
  return {
    db: {
      prepare(sql) {
        if (sql.includes('SELECT report_json')) {
          return { first: async () => cachedRow };
        }
        if (sql.includes('INSERT INTO daily_report_cache')) {
          return { bind: (...args) => ({ run: async () => { writes.push(args); return {}; } }) };
        }
        throw new Error('Unexpected query in cache test fake: ' + sql);
      },
    },
    writes,
  };
}

describe('getCachedDailyReport — read-only', () => {
  let scope;
  beforeAll(() => {
    // buildDailyReport itself is stubbed -- not under test here.
    const stub = `async function buildDailyReport(env, opts) { return { ok: true, generated_at: 1, stub: true }; }`;
    scope = evalInScope(stub + '\n\n' + extractFunctions('getCachedDailyReport', 'refreshDailyReportCache'));
  });

  it('returns null when no cache row exists yet -- not an error, a legitimate first-run state', async () => {
    const { db } = makeCacheFakeDb({ cachedRow: null });
    const result = await scope.getCachedDailyReport({ DB: db });
    expect(result).toBeNull();
  });

  it('returns the parsed report and generatedTs when a real cache row exists', async () => {
    const report = { ok: true, generated_at: 123, dataset_health: { BTC: { total_rows: 5 } } };
    const { db } = makeCacheFakeDb({ cachedRow: { report_json: JSON.stringify(report), generated_ts: 999 } });
    const result = await scope.getCachedDailyReport({ DB: db });
    expect(result.report).toEqual(report);
    expect(result.generatedTs).toBe(999);
  });

  it('a corrupt cache row (should never happen) reads as a miss, not a crash', async () => {
    const { db } = makeCacheFakeDb({ cachedRow: { report_json: 'not json', generated_ts: 999 } });
    const result = await scope.getCachedDailyReport({ DB: db });
    expect(result).toBeNull();
  });
});

describe('refreshDailyReportCache — recomputes and upserts', () => {
  let scope;
  beforeAll(() => {
    const stub = `async function buildDailyReport(env, opts) { return { ok: true, generated_at: 1, computed_fresh: true }; }`;
    scope = evalInScope(stub + '\n\n' + extractFunctions('getCachedDailyReport', 'refreshDailyReportCache'));
  });

  it('writes a real upsert with the freshly computed report and returns it', async () => {
    const { db, writes } = makeCacheFakeDb();
    const result = await scope.refreshDailyReportCache({ DB: db });
    expect(result).toEqual({ ok: true, generated_at: 1, computed_fresh: true });
    expect(writes).toHaveLength(1);
    const [reportJson] = writes[0];
    expect(JSON.parse(reportJson)).toEqual(result);
  });

  it('the write uses id=1 (single-row table) so it always upserts the one current cache entry, never accumulates rows', async () => {
    const src = extractFunctions('refreshDailyReportCache');
    expect(src).toMatch(/VALUES \(1, \?, \?\)/);
    expect(src).toMatch(/ON CONFLICT\(id\) DO UPDATE/);
  });
});

describe('regression: the cron dispatch refreshes the cache after predictions settle', () => {
  it('each of the three per-coin scheduled() branches calls refreshDailyReportCache, sequenced after its own runCoinBatch resolves', () => {
    // scheduled() is an object-method shorthand (`async scheduled(...) {`),
    // not `async function scheduled(...)` -- extractFunctions can't find
    // it, so this reads the raw source directly for this one structural check.
    // Updated 2026-09-05: runPredictionCronTick (3 sequential batches in
    // one invocation) was removed entirely -- BTC/LINK/ETH now each get
    // their own scheduled() invocation and their own runCoinBatch call,
    // per the coin-invocation-isolation fix (confirmed exceededCpu
    // production evidence). refreshDailyReportCache now runs once per
    // coin invocation (3x per 3h window instead of once) -- a minor,
    // accepted redundancy, since it's an idempotent read-and-cache
    // refresh and there's no cheap way to know "am I the last of the
    // three" across separate invocations.
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const refreshCalls = src.match(/runCoinBatch\(env, \w+_BATCH\)\.then\(\s*\(\) => refreshDailyReportCache\(env\)/g) || [];
    expect(refreshCalls).toHaveLength(3);
    expect(src).not.toContain('function runPredictionCronTick(');
  });
});
