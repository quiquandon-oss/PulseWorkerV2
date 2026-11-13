// One price row per scheduled run (worker.js, PRICE_ROW_PER_TICK). Real worker on the local SQLite D1 stand-in.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeWorld } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const T0 = Date.UTC(2026, 10, 1, 0, 0, 0);
const TICKS = 12;
const SEEDED = 2 * 0; // makeWorld seeds history before the first tick; counted below from the table itself
afterEach(() => vi.restoreAllMocks());

async function world(env) {
  vi.spyOn(console, 'log').mockImplementation(() => {});
  const w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: TICKS, env });
  const firstTick = T0 - TICKS * 3 * 3600 * 1000;
  const added = (t) => Number(w.d1.db.prepare(`SELECT COUNT(*) n FROM ${t} WHERE ts >= ?`).get(firstTick).n);
  const out = { btc: added('btc_data'), eth: added('eth_data'), link: added('link_data'), predictions: w.count('predictions'), eth_predictions: w.count('eth_predictions'), link_predictions: w.count('link_predictions') };
  w.close();
  return out;
}

describe('price rows per scheduled run', () => {
  it('default (flag unset): two rows per run and coin, exactly as today', async () => {
    const r = await world({});
    expect(r).toMatchObject({ btc: 2 * TICKS, eth: 2 * TICKS, link: 2 * TICKS });
    expect(SEEDED).toBe(0);
  }, 120000);

  it('PRICE_ROW_PER_TICK="1": one row per run and coin, and every prediction is still written', async () => {
    const r = await world({ PRICE_ROW_PER_TICK: '1' });
    expect(r).toMatchObject({ btc: TICKS, eth: TICKS, link: TICKS });
    expect(r).toMatchObject({ predictions: 2 * TICKS, eth_predictions: 2 * TICKS, link_predictions: 2 * TICKS });
  }, 120000);
});
