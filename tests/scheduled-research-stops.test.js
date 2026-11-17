// 07:00 UTC scheduled research stops (owner decision 2026-11-17): Gemini daily narrative (BTC + LINK), EXP-002 anomaly
// gate, EXP-003 momentum overlay. Only the scheduled calls are skipped; calibration refreshes and everything else in the
// 07:00 run are unchanged; manual routes and stored rows stay.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { extractFunctions, evalInScope } from './helpers/extract.js';
import { makeWorld, H } from './replay/world.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = readFileSync(join(ROOT, 'worker.js'), 'utf8');
const S = evalInScope(extractFunctions('scheduledResearchEnabled'), {});
const block = SRC.slice(SRC.indexOf("if (event.cron === '0 7 * * *') {"), SRC.indexOf("} else if (event.cron === '0 * * * *') {"));

describe('switches', () => {
  it('enabled unless exactly "0"', () => {
    for (const v of [undefined, '1', '', 'yes']) expect(S.scheduledResearchEnabled({ X: v }, 'X')).toBe(true);
    expect(S.scheduledResearchEnabled({ X: '0' }, 'X')).toBe(false);
    expect(S.scheduledResearchEnabled(undefined, 'X')).toBe(true);
  });
  it('gates exactly the Gemini (BTC + LINK), EXP-002 and EXP-003 scheduled calls; calibration refreshes stay unconditional', () => {
    expect(block).toMatch(/if \(scheduledResearchEnabled\(env, 'GEMINI_DAILY_CRON_ENABLED'\)\) \{\n\s*ctx\.waitUntil\(runGeminiDailyAnalysis\(env, 'cron'\)[^\n]*\n\s*ctx\.waitUntil\(runLinkGeminiAnalysis\(env, 'cron'\)[^\n]*\n\s*\}/);
    expect(block).toMatch(/if \(scheduledResearchEnabled\(env, 'EXP002_CRON_ENABLED'\)\) ctx\.waitUntil\(logAnomalyGateExperiment\(/);
    expect(block).toMatch(/if \(scheduledResearchEnabled\(env, 'EXP003_CRON_ENABLED'\)\) ctx\.waitUntil\(logMomentumSelectionExperiment\(/);
    expect(block).toMatch(/\n\s*ctx\.waitUntil\(refreshCalibrationCurve\(env, coin, h\)/);
    expect((SRC.match(/runGeminiDailyAnalysis\(env, 'cron'\)/g) || []).length).toBe(1);   // no other scheduled caller
  });
  it('production ships all three stopped', () => {
    const t = readFileSync(join(ROOT, 'wrangler.toml'), 'utf8');
    for (const k of ['GEMINI_DAILY_CRON_ENABLED', 'EXP002_CRON_ENABLED', 'EXP003_CRON_ENABLED']) expect(t).toMatch(new RegExp(`^${k} = "0"$`, 'm'));
  });
});

describe('replay: the 07:00 run with the switches on vs off', () => {
  async function run(stop) {
    const T0 = Date.UTC(2026, 10, 10, 6, 0, 0);
    const w = await makeWorld({ root: ROOT, t0: T0, warmupTicks: 72, env: { GEMINI_API_KEY: '' } });
    try {
      if (stop) Object.assign(w.env, { GEMINI_DAILY_CRON_ENABLED: '0', EXP002_CRON_ENABLED: '0', EXP003_CRON_ENABLED: '0' });
      const before = { anomaly: w.count('selection_decisions_anomaly'), momentum: w.count('selection_decisions_momentum') };
      const quiet = console.error; const errs = []; console.error = (...a) => errs.push(a.map(String).join(' '));
      try { await w.cronTick(T0 + H, '0 7 * * *'); for (let k = 1; k <= 2; k++) await w.cronTick(T0 + 3 * k * H); } finally { console.error = quiet; }
      const sqls = w.d1.log.map(l => l.sql);
      // Gemini reached = its D1 audit/quota statements ran, or the replay's known Gemini error surfaced (no API key and
      // an inferred schema without the ledger's unique key make the call fail, which is enough to prove it was invoked).
      const geminiReached = sqls.filter(q => /gemini_quota_ledger|gemini_provider_calls|INSERT INTO (link_)?gemini_(daily_)?analysis/.test(q)).length
        + errs.filter(e => /Gemini analysis failed/.test(e)).length;
      return { tables: w.tableHashes(), before, anomaly: w.count('selection_decisions_anomaly'), momentum: w.count('selection_decisions_momentum'),
        geminiCalls: geminiReached,
        calibrationRefreshQueries: sqls.filter(q => /SELECT p_up, realized_up FROM (link_|eth_)?predictions WHERE realized_up IS NOT NULL AND horizon_hours = \?/.test(q)).length };
    } finally { w.close(); }
  }
  it('stopped: no EXP-002/003 rows and no Gemini call; production prediction tables identical; calibration refresh still runs', async () => {
    const on = await run(false), off = await run(true);
    for (const t of ['predictions', 'eth_predictions', 'link_predictions', 'challenger_predictions', 'selection_decisions', 'calibration_curve', 'btc_data'])
      expect(off.tables[t], t).toBe(on.tables[t]);
    expect(off.anomaly).toBe(off.before.anomaly);
    expect(on.momentum).toBeGreaterThan(on.before.momentum);              // EXP-003 really logs when enabled
    expect(off.momentum).toBe(off.before.momentum);
    expect(off.geminiCalls).toBe(0);
    expect(on.geminiCalls).toBeGreaterThan(0);                              // the switch really removed something
    expect(off.calibrationRefreshQueries).toBe(on.calibrationRefreshQueries);
    expect(off.calibrationRefreshQueries).toBeGreaterThan(0);
  }, 600000);
});
