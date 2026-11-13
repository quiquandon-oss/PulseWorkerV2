// Daily D1 usage table (worker.js recordD1UsageDaily, migration 0026). Real worker on the local SQLite D1 stand-in.
import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeWorld, H } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const T0 = Date.UTC(2026, 10, 1, 0, 0, 0);
const settle = () => new Promise((r) => setImmediate(r));
const rows = (w) => w.d1.db.prepare('SELECT * FROM d1_usage_daily ORDER BY day, kind, key').all();

describe('d1_usage_daily, table present', () => {
  let w;
  beforeAll(async () => { vi.spyOn(console, 'log').mockImplementation(() => {}); w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 12 }); }, 120000);
  afterAll(() => { vi.restoreAllMocks(); w && w.close(); });

  it('every scheduled run is recorded under its cron, accumulated per day', () => {
    const sched = rows(w).filter((r) => r.kind === 'scheduled');
    expect(sched.length).toBeGreaterThan(0);
    expect(sched.every((r) => r.key === '0 * * * *')).toBe(true);
    expect(sched.reduce((a, r) => a + r.invocations, 0)).toBe(12);
    for (const r of sched) { expect(r.rows_read).toBeGreaterThan(0); expect(r.max_rows_read).toBeLessThanOrEqual(r.rows_read); }
  });

  it('a request that reads many rows is recorded by method and path, and repeats accumulate', async () => {
    w.env.ROUTE_CACHE = '0';
    try {
      await w.get('/chart-data?horizon=24', T0 - 2 * H); await settle();
      const one = rows(w).find((r) => r.kind === 'fetch' && r.key === 'GET /chart-data');
      expect(one).toMatchObject({ day: '2026-10-31', invocations: 1 });
      expect(one.rows_read).toBeGreaterThanOrEqual(50);
      await w.get('/chart-data?horizon=12', T0 - 2 * H + 1000); await settle();
      const two = rows(w).find((r) => r.kind === 'fetch' && r.key === 'GET /chart-data');
      expect(two.invocations).toBe(2);
      expect(two.rows_read).toBeGreaterThan(one.rows_read);
    } finally { delete w.env.ROUTE_CACHE; }
  });

  it('a cheap request (snapshot read) is not recorded and writes nothing', async () => {
    const before = JSON.stringify(rows(w));
    w.d1.log.length = 0;
    const res = await w.get('/predict?horizon=24', T0 - 2 * H); await settle();
    expect(JSON.parse(res.body).served_from).toBe('snapshot');
    expect(w.d1.log.filter((l) => !l.plan)).toHaveLength(0);
    expect(JSON.stringify(rows(w))).toBe(before);
  });

  it('GET /d1-usage returns per-day totals that match the table, and writes nothing', async () => {
    const table = rows(w);
    w.d1.log.length = 0;
    const res = await w.get('/d1-usage?days=31', T0 - 2 * H); await settle();
    expect(res.status).toBe(200);
    const body = JSON.parse(res.body);
    expect(body).toMatchObject({ ok: true, recording: true, daily_read_limit: 5000000 });
    expect(body.days.map((d) => d.day)).toEqual([...new Set(table.map((r) => r.day))].sort().reverse());
    for (const d of body.days) {
      const mine = table.filter((r) => r.day === d.day);
      expect(d.rows_read).toBe(mine.reduce((a, r) => a + r.rows_read, 0));
      expect(d.rows_written).toBe(mine.reduce((a, r) => a + r.rows_written, 0));
      expect(d.entries).toHaveLength(mine.length);
      expect(d.pct_of_daily_read_limit).toBeCloseTo((d.rows_read / 5000000) * 100, 1);
    }
    expect(w.d1.log.filter((l) => !l.plan && !/^\s*SELECT/i.test(l.sql))).toHaveLength(0);
  });

  it('GET /d1-usage limits the window to the days asked for', async () => {
    const one = JSON.parse((await w.get('/d1-usage?days=1', T0 - 2 * H)).body);
    expect(one.days.every((d) => d.day === '2026-10-31')).toBe(true);
  });

  it('D1_USAGE_TABLE="0" stops recording', async () => {
    const before = JSON.stringify(rows(w));
    w.env.D1_USAGE_TABLE = '0'; w.env.ROUTE_CACHE = '0';
    try { await w.get('/chart-data?horizon=24', T0 - 2 * H + 5000); await settle(); } finally { delete w.env.D1_USAGE_TABLE; delete w.env.ROUTE_CACHE; }
    expect(JSON.stringify(rows(w))).toBe(before);
  });
});

describe('d1_usage_daily, table not migrated', () => {
  it('requests and scheduled runs work exactly as before', async () => {
    vi.spyOn(console, 'log').mockImplementation(() => {});
    const w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 4, omitTables: ['d1_usage_daily'] });
    try {
      const res = await w.get('/chart-data?horizon=24', T0 - 2 * H); await settle();
      expect(res.status).toBe(200);
      expect(w.count('predictions')).toBe(8);
      expect(JSON.parse((await w.get('/d1-usage', T0 - 2 * H)).body)).toEqual({ ok: true, recording: false, days: [] });
    } finally { vi.restoreAllMocks(); w.close(); }
  }, 120000);
});
