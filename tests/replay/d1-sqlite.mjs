// Minimal D1 binding over node:sqlite, with a per-statement log for replay budgets.
// The log records: normalised SQL, rows returned, and the query plan class (SCAN/SEARCH)
// from EXPLAIN QUERY PLAN against this local schema (production indexes may differ: see A3).
import { createRequire } from 'node:module';
const { DatabaseSync } = createRequire(import.meta.url)('node:sqlite');

export function makeD1(setupSql) {
  const db = new DatabaseSync(':memory:');
  for (const s of setupSql) { try { db.exec(s); } catch (e) { /* additive migrations may not apply to the inferred schema */ } }
  const log = [];
  const planCache = new Map();
  const plan = (sql) => {
    if (!/^\s*(SELECT|WITH)/i.test(sql)) return null;
    if (planCache.has(sql)) return planCache.get(sql);
    let p;
    try { p = db.prepare('EXPLAIN QUERY PLAN ' + sql).all(...new Array((sql.match(/\?/g) || []).length).fill(null)).map((r) => r.detail); }
    catch { p = ['(plan unavailable)']; }
    planCache.set(sql, p);
    return p;
  };
  const norm = (sql) => sql.replace(/\s+/g, ' ').trim();
  function stmt(sql, args = []) {
    const o = {
      sql, args,
      bind: (...a) => stmt(sql, a),
      first: async (col) => {
        const r = db.prepare(sql).get(...args); log.push({ sql: norm(sql), rows: r ? 1 : 0, plan: plan(sql) });
        return r ? (col ? r[col] : { ...r }) : null;
      },
      all: async () => {
        const rs = db.prepare(sql).all(...args).map((r) => ({ ...r })); log.push({ sql: norm(sql), rows: rs.length, plan: plan(sql) });
        return { results: rs, success: true, meta: { rows_read: rs.length } };
      },
      raw: async () => { const rs = db.prepare(sql).all(...args); log.push({ sql: norm(sql), rows: rs.length, plan: plan(sql) }); return rs.map((r) => Object.values(r)); },
      run: async () => {
        const r = db.prepare(sql).run(...args); log.push({ sql: norm(sql), rows: 0, changes: Number(r.changes), plan: plan(sql) });
        return { success: true, meta: { changes: Number(r.changes), last_row_id: Number(r.lastInsertRowid) } };
      },
    };
    return o;
  }
  return {
    db, log,
    binding: {
      prepare: (sql) => stmt(sql),
      batch: async (stmts) => { const out = []; db.exec('BEGIN'); try { for (const s of stmts) out.push(/^\s*(SELECT|WITH)/i.test(s.sql) ? await s.all() : await s.run()); db.exec('COMMIT'); } catch (e) { db.exec('ROLLBACK'); throw e; } return out; },
      exec: async (sql) => { db.exec(sql); return { count: 1 }; },
    },
  };
}
