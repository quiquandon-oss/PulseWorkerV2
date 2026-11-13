// Read-once per scheduled run (worker.js "Read-once per scheduled run").
import { describe, it, expect } from 'vitest';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';
import { makeWorld, H } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const T0 = Date.UTC(2026, 10, 1, 0, 0, 0);
const scope = evalInScope(extractConstants('SQL_TABLE_RE', 'SQL_WRITE_TARGET_RE') + '\n' + extractFunctions('sqlReadTables', 'isReadOnlySql', 'makeTickReadMemo'), {});

function fakeDb(state) {
  const calls = [];
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    all: async () => { calls.push(sql); const m = sql.match(/FROM (\w+)/); return { results: (state[m[1]] || []).map((v) => ({ v })) }; },
    first: async () => { calls.push(sql); return { n: (state.t || []).length }; },
    run: async () => { calls.push(sql); const m = sql.match(/INTO (\w+)/); if (m) (state[m[1]] = state[m[1]] || []).push(args[0]); return { meta: {} }; },
    raw: async () => [],
  });
  return { calls, prepare: (sql) => stmt(sql), batch: async (ss) => ss.map(() => ({ meta: {} })) };
}

describe('makeTickReadMemo (unit)', () => {
  it('repeats of the same read hit the memo; different bound values do not', async () => {
    const db = fakeDb({ t: [1, 2] }); const { db: m, stats } = scope.makeTickReadMemo(db);
    await m.prepare('SELECT v FROM t WHERE x=?').bind(1).all();
    await m.prepare('SELECT v FROM t WHERE x=?').bind(1).all();
    await m.prepare('SELECT v FROM t WHERE x=?').bind(2).all();
    expect(db.calls).toHaveLength(2);
    expect(stats).toMatchObject({ hits: 1, misses: 2 });
  });

  it('a write to a table invalidates reads of that table only', async () => {
    const db = fakeDb({ t: [1], u: [9] }); const { db: m } = scope.makeTickReadMemo(db);
    await m.prepare('SELECT v FROM t').all(); await m.prepare('SELECT v FROM u').all();
    await m.prepare('INSERT INTO t (v) VALUES (?)').bind(2).run();
    const t = await m.prepare('SELECT v FROM t').all(); const u = await m.prepare('SELECT v FROM u').all();
    expect(t.results).toEqual([{ v: 1 }, { v: 2 }]);
    expect(u.results).toEqual([{ v: 9 }]);
    expect(db.calls.filter((c) => c === 'SELECT v FROM u')).toHaveLength(1);
    expect(db.calls.filter((c) => c === 'SELECT v FROM t')).toHaveLength(2);
  });

  it('a write it cannot attribute clears everything', async () => {
    const db = fakeDb({ t: [1] }); const { db: m } = scope.makeTickReadMemo(db);
    await m.prepare('SELECT v FROM t').all();
    await m.prepare('PRAGMA something').run();
    await m.prepare('SELECT v FROM t').all();
    expect(db.calls.filter((c) => c === 'SELECT v FROM t')).toHaveLength(2);
  });

  it('a caller mutating its result cannot affect the next caller', async () => {
    const db = fakeDb({ t: [1, 2, 3] }); const { db: m } = scope.makeTickReadMemo(db);
    const a = await m.prepare('SELECT v FROM t').all(); a.results.reverse(); a.results[0].v = 99;
    const b = await m.prepare('SELECT v FROM t').all();
    expect(b.results).toEqual([{ v: 1 }, { v: 2 }, { v: 3 }]);
  });

  it('a result read while a write to its table was in flight is not stored', async () => {
    let release; const gate = new Promise((r) => { release = r; });
    const db = fakeDb({ t: [1] });
    const slow = db.prepare; db.prepare = (sql) => { const s = slow(sql); if (sql.startsWith('SELECT')) { const all = s.all; s.all = async () => { await gate; return all(); }; } return s; };
    const { db: m } = scope.makeTickReadMemo(db);
    const pending = m.prepare('SELECT v FROM t').all();
    await m.prepare('INSERT INTO t (v) VALUES (?)').bind(2).run();
    release(); await pending;
    const again = await m.prepare('SELECT v FROM t').all();
    expect(again.results).toEqual([{ v: 1 }, { v: 2 }]);
    expect(db.calls.filter((c) => c === 'SELECT v FROM t')).toHaveLength(2);
  });

  it('write statements in a batch invalidate', async () => {
    const db = fakeDb({ t: [1] }); const { db: m } = scope.makeTickReadMemo(db);
    await m.prepare('SELECT v FROM t').all();
    await m.batch([m.prepare('UPDATE t SET v=1')]);
    await m.prepare('SELECT v FROM t').all();
    expect(db.calls.filter((c) => c === 'SELECT v FROM t')).toHaveLength(2);
  });
});

describe('read memo in the real scheduled tick (integration)', () => {
  it('24 ticks with and without the memo leave every table byte-identical, with fewer statements', async () => {
    const on = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 24 });
    const hashesOn = on.tableHashes(); on.d1.log.length = 0; await on.cronTick(T0); const stmtsOn = on.d1.log.length; const afterOn = on.tableHashes(); on.close();
    const off = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 24, env: { TICK_READ_MEMO: '0' } });
    const hashesOff = off.tableHashes(); off.d1.log.length = 0; await off.cronTick(T0); const stmtsOff = off.d1.log.length; const afterOff = off.tableHashes(); off.close();
    // d1_usage_daily records how many rows each run read, which is exactly what the memo changes.
    for (const t of [hashesOn, hashesOff, afterOn, afterOff]) delete t.d1_usage_daily;
    expect(hashesOn).toEqual(hashesOff);
    expect(afterOn).toEqual(afterOff);
    expect(stmtsOn).toBeLessThan(stmtsOff);
  }, 120000);
});
