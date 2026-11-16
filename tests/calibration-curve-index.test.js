// Migration 0030 (calibration-curve lookup indexes): the real worker.js replayed on two identical seeded worlds, one
// with the 0030 indexes and one without, produces byte-identical tables and identical served responses; with the
// indexes the lookup's plan uses them instead of scanning. Nothing here touches production.
// Note: the replay world creates tables from the inferred schema AFTER running .ai/migrations and skips statements
// that fail, so an index on an inferred table is not created by makeWorld; this test applies 0030 itself and asserts
// the index exists (and is absent in the control world) before comparing.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { makeWorld, H } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const MIG_SQL = readFileSync(join(ROOT, '.ai/migrations/0030_calibration_curve_lookup_indexes.sql'), 'utf8');
const MIG_STMTS = MIG_SQL.replace(/^\s*--.*$/gm, '').split(/;\s*/).map((s) => s.trim()).filter(Boolean);
const INDEXES = ['idx_calibration_curve_coin_h_ts', 'idx_challenger_calibration_curve_coin_h_ts'];
const LOOKUP = (t) => `SELECT decile, predicted_p_up_mid, empirical_up_rate, n_samples FROM ${t}
     WHERE coin = ? AND horizon_hours = ? AND computed_ts = (
       SELECT MAX(computed_ts) FROM ${t} WHERE coin = ? AND horizon_hours = ?
     )`;
const T0 = Date.UTC(2026, 10, 10, 6, 0, 0);

async function run(withIndex) {
  const w = await makeWorld({ root: ROOT, t0: T0 });
  try {
    for (const i of INDEXES) w.d1.db.exec(`DROP INDEX IF EXISTS ${i}`);
    if (withIndex) for (const s of MIG_STMTS) w.d1.db.exec(s);
    const present = w.d1.db.prepare(`SELECT name FROM sqlite_master WHERE type = 'index' AND name IN (${INDEXES.map(() => '?').join(',')}) ORDER BY name`).all(...INDEXES).map((r) => r.name);
    // Several append-only curve batches per coin/horizon (as the daily refresh leaves them): each lookup must find the
    // latest batch among older ones. Deterministic values, identical in both worlds.
    for (const table of ['calibration_curve', 'challenger_calibration_curve']) {
      const ins = w.d1.db.prepare(`INSERT INTO ${table} (coin, horizon_hours, decile, predicted_p_up_mid, empirical_up_rate, n_samples, computed_ts) VALUES (?, ?, ?, ?, ?, ?, ?)`);
      for (let b = 0; b < 5; b++) for (const coin of ['BTC', 'ETH', 'LINK']) for (const h of [12, 24]) for (let d = 0; d < 10; d++)
        ins.run(coin, h, d, (d + 0.5) / 10, Math.min(1, Math.max(0, (d + 0.5) / 10 + ((b + d + h) % 7 - 3) / 40)), 12 + ((b * 7 + d) % 30), T0 - (5 - b) * 24 * H + (coin.length + h));
    }
    for (let k = 1; k <= 6; k++) await w.cronTick(T0 + 3 * k * H);   // prediction ticks read the curves
    const served = [];
    for (const p of ['/calibration?horizon=24', '/challenger-calibration?coin=BTC&horizon=24', '/chart-data?horizon=24']) served.push(await w.get(p, T0 + 19 * H));
    const lookupsRan = w.d1.log.filter((l) => /FROM (challenger_)?calibration_curve\s+WHERE coin = \? AND horizon_hours = \? AND computed_ts = \(/.test(l.sql)).length;
    const plans = Object.fromEntries(['calibration_curve', 'challenger_calibration_curve'].map((t) =>
      [t, w.d1.db.prepare('EXPLAIN QUERY PLAN ' + LOOKUP(t)).all('BTC', 24, 'BTC', 24).map((r) => r.detail)]));
    const latest = w.d1.db.prepare(LOOKUP('calibration_curve')).all('ETH', 12, 'ETH', 12).map((r) => ({ ...r }));
    return { present, tables: w.tableHashes(), served, lookupsRan, plans, latest };
  } finally { w.close(); }
}

describe('migration 0030: calibration-curve lookup indexes', () => {
  it('is index-only and additive (no table, column or data change)', () => {
    expect(MIG_STMTS).toHaveLength(2);
    for (const s of MIG_STMTS) expect(s).toMatch(/^CREATE INDEX IF NOT EXISTS idx_\w+ ON (challenger_)?calibration_curve \(coin, horizon_hours, computed_ts\)$/);
  });

  it('replay with and without the indexes: identical tables and responses; the lookup uses the index instead of a scan', async () => {
    const without = await run(false);
    const withIdx = await run(true);
    expect(without.present).toEqual([]);
    expect(withIdx.present).toEqual(INDEXES);
    expect(withIdx.lookupsRan).toBeGreaterThan(0);                 // prediction ticks really ran the lookups
    expect(withIdx.lookupsRan).toBe(without.lookupsRan);
    expect(withIdx.latest).toHaveLength(10);                       // the latest batch only
    expect(withIdx.latest).toEqual(without.latest);
    expect(Object.keys(withIdx.tables)).toEqual(Object.keys(without.tables));
    for (const t of Object.keys(without.tables)) expect(withIdx.tables[t], `table ${t}`).toBe(without.tables[t]);
    expect(withIdx.served.map((r) => r.status)).toEqual([200, 200, 200]);
    expect(withIdx.served).toEqual(without.served);
    for (const t of ['calibration_curve', 'challenger_calibration_curve']) {
      expect(without.plans[t]).toContain(`SCAN ${t}`);
      expect(withIdx.plans[t].some((p) => p === `SCAN ${t}`), withIdx.plans[t].join(' | ')).toBe(false);
      expect(withIdx.plans[t].join(' | ')).toMatch(new RegExp(`SEARCH ${t} USING (COVERING )?INDEX idx_${t}_coin_h_ts`));
    }
  }, 240000);
});
