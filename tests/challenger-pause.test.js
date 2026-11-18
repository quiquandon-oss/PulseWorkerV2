// Challenger pause (CHALLENGER_ENABLED = "0"; owner decision 2026-11-17). Replay evidence on the real worker.js: two
// identically seeded worlds, 72 warm-up ticks with the challenger on, then 8 more ticks with it on in one world and
// paused in the other. Required: the core prediction tables are byte-identical and every served prediction keeps the
// same p_up, chosen variant and chosen p_up. Expected differences: no new challenger rows, selection compares 3
// variants instead of 6, and served bodies carry challenger status "paused".
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';
import { makeWorld, H } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = readFileSync(join(ROOT, 'worker.js'), 'utf8');
const T0 = Date.UTC(2026, 10, 10, 0, 0, 0);
const S = evalInScope(extractConstants('SELECTION_VARIANTS') + '\n' + extractFunctions('challengerEnabled', 'selectionVariantsFor'), {});

describe('flag semantics', () => {
  it('enabled unless exactly "0"', () => {
    for (const v of [undefined, null, '1', '', 'true', 'no']) expect(S.challengerEnabled({ CHALLENGER_ENABLED: v })).toBe(true);
    expect(S.challengerEnabled({ CHALLENGER_ENABLED: '0' })).toBe(false);
    expect(S.challengerEnabled(undefined)).toBe(true);
  });
  it('paused selection drops exactly the three challenger variants; enabled keeps all six', () => {
    for (const c of ['BTC', 'ETH', 'LINK']) {
      expect(S.selectionVariantsFor({}, c).map(v => v.key)).toEqual(S.SELECTION_VARIANTS[c].map(v => v.key));
      expect(S.selectionVariantsFor({ CHALLENGER_ENABLED: '0' }, c).map(v => v.key)).toEqual(['original', 'experimental', 'calibrated']);
    }
  });
  it('ships ENABLED in production (activation is a separate owner decision)', () => {
    expect(readFileSync(join(ROOT, 'wrangler.toml'), 'utf8')).toMatch(/^CHALLENGER_ENABLED = "1"$/m);
  });
  it('every challenger call site and the daily calibration refresh are gated; pending rows still resolve', () => {
    expect((SRC.match(/if \(!challengerEnabled\(env\)\) \{\n\s*result\.challenger = \{ ok: true, status: 'paused'/g) || []).length).toBe(3);
    expect(SRC).toMatch(/if \(challengerEnabled\(env\)\) ctx\.waitUntil\(refreshChallengerCalibrationCurve/);
    expect((SRC.match(/const challengerResolvedCount = await backfillChallengerPredictions\(env\);/g) || []).length).toBe(3);
    expect(SRC).toMatch(/async function selectBestVariant\(env, coin, horizonHours\) \{\n\s*const variantDefs = selectionVariantsFor\(env, coin\);/);
  });
});

async function run(pause) {
  const w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 72 });
  try {
    const selBefore = w.count('selection_decisions');
    const chBefore = w.count('challenger_predictions');
    if (pause) w.env.CHALLENGER_ENABLED = '0';
    for (let k = 0; k < 8; k++) await w.cronTick(T0 + k * 3 * H);
    const sel = w.d1.db.prepare('SELECT coin, horizon_hours, chosen_variant, chosen_p_up, cleared_gate, comparison_count FROM selection_decisions ORDER BY id').all()
      .slice(selBefore).map(r => ({ ...r }));
    const served = [];
    for (const [c, path] of [['BTC', '/predict'], ['ETH', '/eth-predict'], ['LINK', '/link-predict']]) for (const h of [12, 24]) {
      const r = await w.get(`${path}?horizon=${h}`, T0 + 7 * 3 * H + 600000);
      const b = JSON.parse(r.body);
      served.push({ status: r.status, p_up: b.p_up, chosen: b.selection && b.selection.chosen_variant, chosen_p_up: b.selection && b.selection.chosen_p_up,
        challenger_status: b.challenger && (b.challenger.status || null) });
    }
    return { tables: w.tableHashes(), sel, served, chBefore, chAfter: w.count('challenger_predictions') };
  } finally { w.close(); }
}

describe('replay: challenger on vs paused', () => {
  it('core predictions byte-identical; served predictions semantically identical; only challenger and selection metadata differ', async () => {
    const on = await run(false), off = await run(true);
    for (const t of ['predictions', 'eth_predictions', 'link_predictions', 'btc_data', 'eth_data', 'link_data']) expect(off.tables[t], t).toBe(on.tables[t]);
    expect(off.sel.length).toBe(on.sel.length);
    off.sel.forEach((s, i) => {
      expect([s.coin, s.horizon_hours, s.chosen_variant, s.chosen_p_up, s.cleared_gate]).toEqual([on.sel[i].coin, on.sel[i].horizon_hours, on.sel[i].chosen_variant, on.sel[i].chosen_p_up, on.sel[i].cleared_gate]);
    });
    expect(new Set(on.sel.map(s => s.comparison_count))).toEqual(new Set([6]));
    expect(new Set(off.sel.map(s => s.comparison_count))).toEqual(new Set([3]));
    off.served.forEach((s, i) => {
      expect(s.status).toBe(200);
      expect([s.p_up, s.chosen, s.chosen_p_up]).toEqual([on.served[i].p_up, on.served[i].chosen, on.served[i].chosen_p_up]);
    });
    expect(on.chAfter).toBeGreaterThan(on.chBefore);
    expect(off.chAfter).toBe(off.chBefore);                      // no new challenger predictions while paused
    // Synthetic-data limit, stated: no challenger variant won selection in this world, so this replay cannot show the
    // production case where a challenger would have been chosen (checked separately on production selection_decisions).
    expect(on.sel.some(s => String(s.chosen_variant).startsWith('challenger_'))).toBe(false);
  }, 600000);
});
