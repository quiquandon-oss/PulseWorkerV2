// Read-route cache (worker.js "Read-route cache"). Runs the real worker on the local SQLite D1 stand-in.
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync } from 'node:fs';
import { makeWorld, summarise, H } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const T0 = Date.UTC(2026, 10, 1, 0, 0, 0);
const SRC = readFileSync(join(ROOT, 'worker.js'), 'utf8');
const CACHED = JSON.parse('[' + SRC.slice(SRC.indexOf('const ROUTE_CACHE_PATHS = new Set([') + 35, SRC.indexOf(']);', SRC.indexOf('const ROUTE_CACHE_PATHS'))).replace(/'/g, '"').replace(/,\s*$/, '') + ']');
const SAMPLE = { '/challenger-calibration': '?coin=BTC&horizon=24', '/challenger-calibration-history': '?coin=BTC&horizon=24', '/variant-calibration': '?coin=BTC&horizon=24&variant=original', '/selection-history': '?coin=BTC&horizon=24', '/select-variant': '?coin=BTC&horizon=24', '/challenger-recent': '?limit=50', '/calibration-curve': '?coin=BTC&horizon=24' };
const url = (p) => p + (SAMPLE[p] || '?horizon=24');

async function call(w, path, ts) {
  w.clock.now = ts;
  const res = await w.worker.fetch(new Request('https://pulseworker-v2.replay' + path), w.env, { waitUntil: () => {} });
  return { status: res.status, cache: res.headers.get('X-Route-Cache'), cors: res.headers.get('Access-Control-Allow-Origin'), body: await res.text() };
}

describe('read-route cache', () => {
  let w;
  beforeAll(async () => { w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 72 }); }, 120000);
  afterAll(() => w && w.close());

  it('lists exactly the routes verified write-free', () => {
    expect(CACHED).toHaveLength(18);
    expect(CACHED).not.toContain('/prediction-freshness'); // clock-dependent
    expect(CACHED).not.toContain('/predict');               // served from snapshots instead
  });

  it.each(CACHED)('%s: the second request is a hit with a byte-identical body, CORS intact, and no table scans', async (path) => {
    const t = T0 - 2 * H;
    const first = await call(w, url(path), t);
    w.d1.log.length = 0;
    const second = await call(w, url(path), t + 60000);
    const d1 = summarise(w.d1.log);
    expect(first.status).toBe(200);
    expect(first.cache).toBe('miss');
    expect(second.cache).toBe('hit');
    expect(second.body).toBe(first.body);
    expect(second.cors).toBe(first.cors);
    expect(d1).toMatchObject({ statements: 1, full_scans: 0, writes: 0 }); // only the epoch read
  });

  it('a scheduled tick invalidates cached data, and the fresh body matches an uncached computation', async () => {
    const before = await call(w, '/chart-data?horizon=24', T0 - 2 * H);
    await w.cronTick(T0);
    const after = await call(w, '/chart-data?horizon=24', T0 + 60000);
    expect(after.cache).toBe('miss');
    expect(after.body).not.toBe(before.body);
    w.env.ROUTE_CACHE = '0';
    try { expect((await call(w, '/chart-data?horizon=24', T0 + 120000)).body).toBe(after.body); } finally { delete w.env.ROUTE_CACHE; }
  });

  it('a request that writes clears the cache in this isolate', async () => {
    await call(w, '/calibration?horizon=24', T0 + 60000);
    expect((await call(w, '/calibration?horizon=24', T0 + 90000)).cache).toBe('hit');
    w.env.PREDICT_SNAPSHOTS = '0';
    let wrote;
    try { wrote = await call(w, '/predict?horizon=24', T0 + 7 * H); } finally { delete w.env.PREDICT_SNAPSHOTS; } // stalled-cron path writes
    expect(JSON.parse(wrote.body).persisted).toBe(true);
    expect((await call(w, '/calibration?horizon=24', T0 + 7 * H + 1000)).cache).toBe('miss');
  });

  it('entries expire at the hour boundary even without a tick', async () => {
    await call(w, '/selection-history?coin=BTC&horizon=24', T0 + 8 * H + 10 * 60000);
    expect((await call(w, '/selection-history?coin=BTC&horizon=24', T0 + 8 * H + 50 * 60000)).cache).toBe('hit');
    expect((await call(w, '/selection-history?coin=BTC&horizon=24', T0 + 9 * H + 60000)).cache).toBe('miss');
  });

  it('ROUTE_CACHE="0" bypasses the cache entirely', async () => {
    w.env.ROUTE_CACHE = '0';
    try {
      const a = await call(w, '/calibration?horizon=24', T0 + 9 * H + 120000);
      const b = await call(w, '/calibration?horizon=24', T0 + 9 * H + 180000);
      expect(a.cache).toBeNull(); expect(b.cache).toBeNull(); expect(b.body).toBe(a.body);
    } finally { delete w.env.ROUTE_CACHE; }
  });

  it('different query strings are different entries; parameter order does not matter', async () => {
    const t = T0 + 9 * H + 240000;
    const a = await call(w, '/challenger-calibration?coin=BTC&horizon=24', t);
    const b = await call(w, '/challenger-calibration?horizon=24&coin=BTC', t + 1000);
    const c = await call(w, '/challenger-calibration?coin=LINK&horizon=24', t + 2000);
    expect(b.cache).toBe('hit'); expect(b.body).toBe(a.body);
    expect(c.cache).toBe('miss');
  });

  it('non-GET requests and uncached routes are untouched', async () => {
    const res = await call(w, '/prediction-freshness', T0 + 9 * H + 300000);
    expect(res.cache).toBeNull();
  });
});
