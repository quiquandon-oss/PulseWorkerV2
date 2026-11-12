// D1 usage accounting (worker.js "D1 usage accounting"): unit tests on the wrapper, and integration tests
// through the real worker on the local SQLite D1 stand-in. Nothing here touches production.
import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { extractFunctions, evalInScope } from './helpers/extract.js';
import { makeWorld, H } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const T0 = Date.UTC(2026, 10, 1, 0, 0, 0);

function fakeDb() {
  const calls = [];
  const stmt = (sql, args = []) => ({
    sql, args,
    bind: (...a) => stmt(sql, a),
    first: async () => { calls.push(['first', sql, args]); return { n: 1 }; },
    all: async () => { calls.push(['all', sql, args]); return { results: [{ a: 1 }, { a: 2 }], meta: { rows_read: 40, rows_written: 0 } }; },
    run: async () => { calls.push(['run', sql, args]); return { success: true, meta: { rows_read: 3, rows_written: 2, changes: 1 } }; },
    raw: async () => { calls.push(['raw', sql, args]); return [[1]]; },
  });
  return {
    calls,
    prepare: (sql) => stmt(sql),
    batch: async (stmts) => { calls.push(['batch', stmts.map((s) => s.sql)]); if (stmts.some((s) => s.__d1Statement)) throw new Error('batch received a wrapped statement'); return stmts.map(() => ({ meta: { rows_read: 5, rows_written: 1 } })); },
    exec: async (sql) => { calls.push(['exec', sql]); return { count: 1 }; },
  };
}

describe('instrumentD1 (unit)', () => {
  const scope = evalInScope(extractFunctions('newD1Usage', 'noteD1Result', 'instrumentD1Statement', 'instrumentD1', 'withD1Usage'), {});

  it('returns results unchanged and sums rows read and written', async () => {
    const db = fakeDb(); const usage = scope.newD1Usage(); const w = scope.instrumentD1(db, usage);
    const all = await w.prepare('SELECT 1').bind(7).all();
    const run = await w.prepare('UPDATE t SET x=?').bind(1).run();
    expect(all).toEqual({ results: [{ a: 1 }, { a: 2 }], meta: { rows_read: 40, rows_written: 0 } });
    expect(run.meta.changes).toBe(1);
    expect(db.calls).toEqual([['all', 'SELECT 1', [7]], ['run', 'UPDATE t SET x=?', [1]]]);
    expect(usage).toEqual({ statements: 2, rows_read: 43, rows_written: 2, writes: 1, first_calls: 0, without_meta: 0 });
  });

  it('counts first() and raw() without inventing row figures', async () => {
    const db = fakeDb(); const usage = scope.newD1Usage(); const w = scope.instrumentD1(db, usage);
    expect(await w.prepare('SELECT n').first()).toEqual({ n: 1 });
    expect(await w.prepare('SELECT n').raw()).toEqual([[1]]);
    expect(usage).toEqual({ statements: 2, rows_read: 0, rows_written: 0, writes: 0, first_calls: 1, without_meta: 1 });
  });

  it('batch() hands the real statements to D1 and accounts for each result', async () => {
    const db = fakeDb(); const usage = scope.newD1Usage(); const w = scope.instrumentD1(db, usage);
    const results = await w.batch([w.prepare('A').bind(1), w.prepare('B')]);
    expect(results).toHaveLength(2);
    expect(db.calls).toEqual([['batch', ['A', 'B']]]);
    expect(usage).toMatchObject({ statements: 2, rows_read: 10, rows_written: 2 });
  });

  it('other binding methods pass straight through', async () => {
    const db = fakeDb(); const w = scope.instrumentD1(db, scope.newD1Usage());
    expect(await w.exec('PRAGMA x')).toEqual({ count: 1 });
  });

  it('withD1Usage leaves env alone without a DB, and still wraps when the log is silenced (writes must be seen)', async () => {
    const usage = scope.newD1Usage();
    const noDb = { A: 1 };
    expect(scope.withD1Usage(noDb, usage)).toBe(noDb);
    const off = scope.withD1Usage({ DB: fakeDb(), D1_USAGE_LOG: '0' }, usage);
    await off.DB.prepare('DELETE FROM t').run();
    expect(usage.writes).toBe(1);
    const on = { DB: fakeDb(), OTHER: 'kept' };
    const wrapped = scope.withD1Usage(on, usage);
    expect(wrapped).not.toBe(on);
    expect(wrapped.OTHER).toBe('kept');
  });
});

describe('d1_usage log line (integration)', () => {
  let w; let lines;
  const usageLines = () => lines.map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter((o) => o && o.evt === 'd1_usage');
  beforeAll(async () => {
    w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 12 });
    lines = [];
    vi.spyOn(console, 'log').mockImplementation((...a) => { lines.push(a.join(' ')); });
  }, 120000);
  afterAll(() => { vi.restoreAllMocks(); w && w.close(); });

  it('a GET logs one line with its path, status and statement count', async () => {
    lines.length = 0;
    const res = await w.get('/predict?horizon=24', T0 - 2 * H);
    const logged = usageLines();
    expect(res.status).toBe(200);
    expect(logged).toHaveLength(1);
    expect(logged[0]).toMatchObject({ kind: 'fetch', method: 'GET', path: '/predict', status: 200, statements: 1 });
  });

  it('a scheduled tick logs one line covering the work done inside waitUntil', async () => {
    lines.length = 0;
    await w.cronTick(T0);
    const logged = usageLines().filter((l) => l.kind === 'scheduled');
    expect(logged).toHaveLength(1);
    expect(logged[0].cron).toBe('0 * * * *');
    expect(logged[0].statements).toBeGreaterThan(50);
    expect(logged[0].rows_read).toBeGreaterThan(0);
  });

  it('D1_USAGE_LOG="0" turns the line off and the response is unchanged', async () => {
    const withLog = (await w.get('/prediction-freshness', T0 + H)).body;
    lines.length = 0;
    w.env.D1_USAGE_LOG = '0';
    try {
      const without = (await w.get('/prediction-freshness', T0 + H)).body;
      expect(usageLines()).toHaveLength(0);
      const strip = (b) => { const o = JSON.parse(b); delete o.generated_ts; return o; };
      expect(strip(without)).toEqual(strip(withLog));
    } finally { delete w.env.D1_USAGE_LOG; }
  });
});
