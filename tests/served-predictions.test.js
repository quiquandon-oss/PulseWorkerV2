// Behavioural tests for served predictions (worker.js "Served predictions: snapshots and selection pairing").
// They run the real worker.js against a local SQLite D1 stand-in (tests/replay/world.mjs): synthetic data,
// controllable clock, stubbed network. Nothing here touches production.
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeWorld, summarise, H } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const T0 = Date.UTC(2026, 10, 1, 0, 0, 0);
const WARMUP = 72; // 9 days of 3-hourly ticks, so predictions resolve and selection really runs
const ROUTES = [['/predict?horizon=24', 'BTC', 'predictions'], ['/link-predict?horizon=24', 'LINK', 'link_predictions'], ['/eth-predict?horizon=24', 'ETH', 'eth_predictions']];
const RESEARCH_TABLES = ['predictions', 'link_predictions', 'eth_predictions', 'challenger_predictions', 'selection_decisions', 'btc_data', 'link_data', 'eth_data'];
const counts = (w) => Object.fromEntries(RESEARCH_TABLES.map((t) => [t, w.count(t)]));

describe('served predictions, snapshot table present', () => {
  let w;
  beforeAll(async () => { w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: WARMUP }); }, 120000);
  afterAll(() => w && w.close());

  it('the scheduled tick stores one snapshot per coin and horizon, and really ran selection', () => {
    expect(w.count('prediction_snapshots')).toBe(6);
    expect(w.count('selection_decisions')).toBeGreaterThan(0);
  });

  it.each(ROUTES)('GET %s serves the snapshot with one read and no writes', async (path, coin) => {
    const before = counts(w);
    w.d1.log.length = 0;
    const res = await w.get(path, T0 - 2 * H); // one hour after the last tick
    const d1 = summarise(w.d1.log);
    expect(res.status).toBe(200);
    expect(d1).toMatchObject({ statements: 1, writes: 0, full_scans: 0 });
    expect(counts(w)).toEqual(before);
    const body = JSON.parse(res.body);
    expect(body.served_from).toBe('snapshot');
    expect(body.status).toBe('ok');
    expect(body.persisted).toBe(true);
    expect(body.selection).toBeTruthy();
    expect(body.snapshot_computed_ts).toBeLessThanOrEqual(T0 - 2 * H);
    void coin;
  });

  it('the served body is exactly what the tick produced, plus the two serving fields', async () => {
    const stored = JSON.parse(w.d1.db.prepare("SELECT body_json FROM prediction_snapshots WHERE coin='BTC' AND horizon_hours=24").get().body_json);
    const { served_from, snapshot_computed_ts, ...served } = JSON.parse((await w.get('/predict?horizon=24', T0 - 2 * H)).body);
    expect(served).toEqual(stored);
    expect(served_from).toBe('snapshot');
    expect(typeof snapshot_computed_ts).toBe('number');
  });

  it('the snapshot describes the prediction row the tick wrote', () => {
    const snap = w.d1.db.prepare("SELECT prediction_ts, body_json FROM prediction_snapshots WHERE coin='BTC' AND horizon_hours=24").get();
    const latest = w.d1.db.prepare('SELECT ts, p_up FROM predictions WHERE horizon_hours=24 ORDER BY ts DESC LIMIT 1').get();
    expect(snap.prediction_ts).toBe(latest.ts);
    expect(JSON.parse(snap.body_json).p_up).toBeCloseTo(latest.p_up, 3); // the response rounds to 3 decimals
  });

  it('repeated dashboard loads add no selection rows (the duplicate-row defect)', async () => {
    const before = w.count('selection_decisions');
    for (let i = 0; i < 25; i++) for (const [path] of ROUTES) await w.get(path, T0 - 2 * H + i * 60000);
    expect(w.count('selection_decisions')).toBe(before);
  });

  it('PREDICT_SNAPSHOTS="0" disables the snapshot and falls back to the live path, still without a selection row', async () => {
    const before = w.count('selection_decisions');
    w.env.PREDICT_SNAPSHOTS = '0';
    try {
      const body = JSON.parse((await w.get('/predict?horizon=24', T0 - 2 * H)).body);
      expect(body.served_from).toBeUndefined();
      expect(body.persisted).toBe(false);
      expect(body.selection.served_from).toBe('stored_selection');
      expect(w.count('selection_decisions')).toBe(before);
    } finally { delete w.env.PREDICT_SNAPSHOTS; }
  });

  it('a stale snapshot is not served: with the cron stalled, the live path self-heals and pairs exactly one selection', async () => {
    const later = T0 + 7 * H; // last tick was T0 - 3h, so data and snapshot are 10 h old (> 6 h)
    const before = counts(w);
    const body = JSON.parse((await w.get('/predict?horizon=24', later)).body);
    expect(body.served_from).toBeUndefined();
    expect(body.persisted).toBe(true); // stale-refresh claim won
    const after = counts(w);
    expect(after.predictions - before.predictions).toBe(1);
    expect(after.selection_decisions - before.selection_decisions).toBe(1); // H1 pairing
    const sel = w.d1.db.prepare("SELECT prediction_ts FROM selection_decisions WHERE coin='BTC' AND horizon_hours=24 ORDER BY ts DESC LIMIT 1").get();
    const pred = w.d1.db.prepare('SELECT ts FROM predictions WHERE horizon_hours=24 ORDER BY ts DESC LIMIT 1').get();
    expect(sel.prediction_ts).toBe(pred.ts);
  });
});

describe('served predictions, snapshot table not migrated yet', () => {
  let w;
  beforeAll(async () => { w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: WARMUP, omitTables: ['prediction_snapshots'] }); }, 120000);
  afterAll(() => w && w.close());

  it('the scheduled tick still completes and writes its predictions and selections', () => {
    expect(w.count('predictions')).toBe(WARMUP * 2);
    expect(w.count('selection_decisions')).toBeGreaterThan(0);
  });

  it.each(ROUTES)('GET %s answers from the live path and writes no prediction and no selection row', async (path, coin, table) => {
    const before = counts(w);
    const res = await w.get(path, T0 - 2 * H);
    const body = JSON.parse(res.body);
    expect(res.status).toBe(200);
    expect(body.status).toBe('ok');
    expect(body.served_from).toBeUndefined();
    expect(body.persisted).toBe(false);
    expect(body.selection.served_from).toBe('stored_selection');
    expect(body.selection.coin).toBe(coin);
    const after = counts(w);
    expect(after[table]).toBe(before[table]);
    expect(after.selection_decisions).toBe(before.selection_decisions);
  });

  it('the stored selection returned is the tick\'s latest row, unchanged', async () => {
    const row = w.d1.db.prepare("SELECT ts, chosen_variant, chosen_p_up FROM selection_decisions WHERE coin='BTC' AND horizon_hours=24 ORDER BY ts DESC LIMIT 1").get();
    const body = JSON.parse((await w.get('/predict?horizon=24', T0 - 2 * H)).body);
    expect(body.selection).toMatchObject({ ts: row.ts, chosen_variant: row.chosen_variant, chosen_p_up: row.chosen_p_up });
  });
});
