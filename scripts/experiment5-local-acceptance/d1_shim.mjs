// Minimal Cloudflare-D1-compatible wrapper over node:sqlite, used ONLY by the
// local acceptance harness. It implements exactly the surface worker.js uses:
// prepare(sql).bind(...).first()/all()/run(). It touches no network and no
// Cloudflare resource -- the database is a local file.
import { DatabaseSync } from 'node:sqlite';

const clean = (args) => args.map((a) => (a === undefined ? null : a));

class Statement {
  constructor(db, sql, args = []) { this.db = db; this.sql = sql; this.args = args; }
  bind(...args) { return new Statement(this.db, this.sql, clean(args)); }
  async first() { return this.db.prepare(this.sql).get(...this.args) ?? null; }
  async all() { return { results: this.db.prepare(this.sql).all(...this.args), success: true }; }
  async run() {
    const info = this.db.prepare(this.sql).run(...this.args);
    return { success: true, meta: { changes: Number(info.changes), last_row_id: Number(info.lastInsertRowid) } };
  }
}

export class D1Shim {
  constructor(path) { this.db = new DatabaseSync(path); this.db.exec('PRAGMA foreign_keys = OFF'); }
  prepare(sql) { return new Statement(this.db, sql); }
  async batch(statements) { return Promise.all(statements.map((s) => s.run())); }
  exec(sql) { this.db.exec(sql); }
  close() { this.db.close(); }
}
