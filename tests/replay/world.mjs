// A self-contained local "world" for replay and integration tests: the unmodified worker.js running
// against an in-memory SQLite D1 stand-in, with a controllable clock, seeded randomness, deterministic
// UUIDs and stubbed network. Nothing here can reach production.
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { collectSources, inferSchema, ddl } from './schema-infer.mjs';
import { makeD1 } from './d1-sqlite.mjs';
import { seed, prng, hyperliquidStub } from './fixtures.mjs';

export const H = 3600 * 1000;
let worldSeq = 0;
const RealDate = Date;
const realSetTimeout = globalThis.setTimeout;
const realRandom = Math.random;
const realUuid = crypto.randomUUID.bind(crypto);
const realFetch = globalThis.fetch;

export async function makeWorld({ root = '.', t0, warmupTicks = 72, omitTables = [], workerPath = null } = {}) {
  root = resolve(root);
  const clock = { now: t0 };
  globalThis.Date = class extends RealDate { constructor(...a) { super(...(a.length ? a : [clock.now])); } static now() { return clock.now; } };
  Math.random = prng(42);
  let uuidN = 0; crypto.randomUUID = () => `00000000-0000-4000-8000-${String(++uuidN).padStart(12, '0')}`;
  globalThis.setTimeout = (fn, ms, ...a) => realSetTimeout(fn, 0, ...a);   // retry back-offs run instantly

  const omit = new Set(omitTables);
  const tables = inferSchema(collectSources(root));
  for (const t of omit) tables.delete(t);
  // Migrations first, so a table a migration defines keeps its real constraints; the inferred DDL then fills in
  // every table that has no checked-in definition (production has no base schema file).
  const setup = [];
  for (const f of readdirSync(join(root, '.ai/migrations')).sort()) {
    for (const stmt of readFileSync(join(root, '.ai/migrations', f), 'utf8').replace(/^\s*--.*$/gm, '').split(/;\s*\n/).map((x) => x.trim()).filter(Boolean)) {
      if (![...omit].some((t) => new RegExp(`\\b${t}\\b`).test(stmt))) setup.push(stmt);
    }
  }
  setup.push(...ddl(tables), 'CREATE UNIQUE INDEX IF NOT EXISTS u_src_coin ON stale_refresh_claim(coin);');
  const d1 = makeD1(setup);
  seed(d1.db, t0 - warmupTicks * 3 * H);
  const env = { DB: d1.binding, ENVIRONMENT: 'replay' };
  const mod = await import(pathToFileURL(workerPath ? resolve(workerPath) : join(root, 'worker.js')).href + `?world=${++worldSeq}`);
  const worker = mod.default;

  async function cronTick(ts, cron = '0 * * * *') {
    clock.now = ts; globalThis.fetch = hyperliquidStub(ts);
    const pending = []; const ctx = { waitUntil: (p) => pending.push(Promise.resolve(p).catch((e) => ({ error: String(e) }))) };
    await worker.scheduled({ cron, scheduledTime: ts }, env, ctx);
    await Promise.all(pending);
  }
  async function get(path, ts) {
    clock.now = ts; globalThis.fetch = hyperliquidStub(ts);
    const res = await worker.fetch(new Request('https://pulseworker-v2.replay' + path), env, { waitUntil: () => {} });
    return { status: res.status, body: await res.text() };
  }
  for (let i = warmupTicks; i >= 1; i--) await cronTick(t0 - i * 3 * H);
  d1.log.length = 0;
  const count = (table) => Number(d1.db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get().n);
  const close = () => { globalThis.Date = RealDate; globalThis.setTimeout = realSetTimeout; Math.random = realRandom; crypto.randomUUID = realUuid; globalThis.fetch = realFetch; };
  return { env, d1, worker, clock, cronTick, get, count, close };
}

export function summarise(log) {
  const reads = log.filter((l) => l.plan);
  return { statements: log.length, selects: reads.length, rows_returned: reads.reduce((a, l) => a + l.rows, 0),
    full_scans: reads.filter((l) => l.plan.some((p) => /^SCAN \w+$|^SCAN \w+ USING (?!.*\()/.test(p) && !/USING INTEGER PRIMARY KEY \(/.test(p))).length,
    writes: log.filter((l) => !l.plan).length };
}
