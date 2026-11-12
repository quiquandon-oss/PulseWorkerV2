// Selection provenance (worker.js "Selection provenance"): runs the real worker on the local SQLite D1
// stand-in. Checks that provenance is recorded without recalculating or altering any selection row.
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeWorld, H } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const T0 = Date.UTC(2026, 10, 1, 0, 0, 0);
const FIELD = { original: 'p_up', experimental: 'p_up_experimental', calibrated: 'calibrated_p_up' };
const CORE = { BTC: 'predictions', LINK: 'link_predictions', ETH: 'eth_predictions' };

describe('selection provenance, table present', () => {
  let w; let rows;
  beforeAll(async () => {
    w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 72 });
    rows = w.d1.db.prepare(`SELECT p.*, s.chosen_variant AS s_variant, s.chosen_p_up, s.prediction_ts AS s_pred_ts, s.coin AS s_coin, s.horizon_hours AS s_h
      FROM selection_provenance p JOIN selection_decisions s ON s.id = p.selection_row_id WHERE p.selection_table = 'selection_decisions'`).all();
  }, 120000);
  afterAll(() => w && w.close());

  it('every selection row written by a tick has exactly one provenance row', () => {
    const selections = w.count('selection_decisions');
    expect(selections).toBeGreaterThan(0);
    expect(rows.length).toBe(selections);
    const perRow = w.d1.db.prepare("SELECT selection_row_id, COUNT(*) n FROM selection_provenance GROUP BY selection_table, selection_row_id HAVING n > 1").all();
    expect(perRow).toEqual([]);
  });

  it('provenance copies the selection key and records the trigger and semantics version', () => {
    for (const r of rows) {
      expect(r.coin).toBe(r.s_coin);
      expect(r.horizon_hours).toBe(r.s_h);
      expect(r.prediction_ts).toBe(r.s_pred_ts);
      expect(r.chosen_variant).toBe(r.s_variant);
      expect(r.selection_trigger).toBe('cron');
      expect(r.semantics_version).toMatch(/^sel-v1: chosen_p_up = chosen variant p_up on its latest resolved prediction/);
    }
  });

  it('chosen_p_up_source_ts points at a RESOLVED prediction whose value is exactly the stored chosen_p_up', () => {
    let checked = 0;
    for (const r of rows) {
      const field = FIELD[r.chosen_variant]; if (!field) continue;
      const src = w.d1.db.prepare(`SELECT ${field} AS p, realized_up FROM ${CORE[r.coin]} WHERE horizon_hours = ? AND ts = ?`).get(r.horizon_hours, r.chosen_p_up_source_ts);
      expect(src).toBeTruthy();
      expect(src.realized_up).not.toBeNull();
      expect(src.p).toBe(r.chosen_p_up);
      expect(r.chosen_p_up_source_ts).toBeLessThan(r.prediction_ts);
      checked++;
    }
    expect(checked).toBeGreaterThan(0);
  });

  it('cycle_p_up is the chosen variant value on the row written for the cycle itself', () => {
    let checked = 0;
    for (const r of rows) {
      const field = FIELD[r.chosen_variant]; if (!field) continue;
      const own = w.d1.db.prepare(`SELECT ${field} AS p FROM ${CORE[r.coin]} WHERE horizon_hours = ? AND ts = ?`).get(r.horizon_hours, r.prediction_ts);
      expect(r.cycle_p_up_ts).toBe(r.prediction_ts);
      expect(r.cycle_p_up).toBe(own.p);
      checked++;
    }
    expect(checked).toBeGreaterThan(0);
  });

  it('the selection served from the snapshot carries the cycle value recorded in provenance', async () => {
    const body = JSON.parse((await w.get('/predict?horizon=24', T0 - 2 * H)).body);
    const prov = w.d1.db.prepare("SELECT cycle_p_up, cycle_p_up_ts FROM selection_provenance WHERE selection_table = 'selection_decisions' AND selection_row_id = ?").get(body.selection.selection_row_id);
    expect(body.served_from).toBe('snapshot');
    expect(body.selection.cycle_p_up).toBe(prov.cycle_p_up);
    expect(body.selection.cycle_p_up_ts).toBe(prov.cycle_p_up_ts);
    expect(body.selection.prediction_ts).toBe(body.ts);
  });

  it('a request that persists a prediction records trigger request_refresh', async () => {
    w.env.PREDICT_SNAPSHOTS = '0';
    try { await w.get('/predict?horizon=24', T0 + 7 * H); } finally { delete w.env.PREDICT_SNAPSHOTS; }
    const last = w.d1.db.prepare("SELECT selection_trigger FROM selection_provenance WHERE selection_table = 'selection_decisions' ORDER BY id DESC LIMIT 1").get();
    expect(last.selection_trigger).toBe('request_refresh');
  });

  it('the daily shadow job records cron_daily provenance for an anomaly-gate row', async () => {
    const latest = w.d1.db.prepare('SELECT id FROM predictions WHERE horizon_hours = 24 ORDER BY ts DESC LIMIT 1').get();
    w.d1.db.prepare('UPDATE predictions SET is_regime_anomaly = 1 WHERE id = ?').run(latest.id); // local fixture only
    const before = w.count('selection_decisions_anomaly');
    await w.cronTick(T0 + 31 * H, '0 7 * * *');
    const after = w.count('selection_decisions_anomaly');
    expect(after).toBeGreaterThan(before);
    const prov = w.d1.db.prepare(`SELECT p.*, a.chosen_variant_anomaly, a.prediction_ts AS a_pred_ts FROM selection_provenance p
      JOIN selection_decisions_anomaly a ON a.id = p.selection_row_id WHERE p.selection_table = 'selection_decisions_anomaly'`).all();
    expect(prov.length).toBe(after);
    for (const r of prov) {
      expect(r.selection_trigger).toBe('cron_daily');
      expect(r.chosen_variant).toBe(r.chosen_variant_anomaly);
      expect(r.prediction_ts).toBe(r.a_pred_ts);
    }
  });
});

describe('selection provenance, table not migrated', () => {
  it('selection rows are byte-identical with and without the provenance table', async () => {
    const dump = (w) => JSON.stringify(w.d1.db.prepare('SELECT * FROM selection_decisions ORDER BY id').all());
    const withTable = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 72 });
    const a = dump(withTable); withTable.close();
    const without = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 72, omitTables: ['selection_provenance'] });
    const b = dump(without); without.close();
    expect(a.length).toBeGreaterThan(10);
    expect(b).toBe(a);
  }, 180000);
});
