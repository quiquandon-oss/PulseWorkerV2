// Regression tests for the shared-cron dispatch (2026-09-05, second
// revision same day).
//
// History: an earlier same-day change tried isolating BTC/LINK/ETH into
// three separate scheduled() invocations via three staggered 3h Cron
// Triggers, directly addressing a confirmed Cloudflare exceededCpu
// termination (Workers Logs, 2026-09-05 12:00 UTC: cpuTimeMs=2010,
// wallTimeMs=6401). That deploy FAILED: Cron Triggers are capped at 5
// per ACCOUNT on Workers Free (not per Worker), and V1
// (sentiment-ff75) already uses 3 of those 5, leaving only 2 for this
// Worker -- not enough for 3 separate 3h triggers plus the daily one.
//
// Current, reverted design: ONE shared 3h Cron Trigger runs all three
// coins' batches sequentially in one invocation (runCoinCronTick,
// restoring PR #30's shape), fitting the account's real 2-trigger
// budget. Production coverage (BTC+LINK+ETH predictions/Challenger/
// selection) is completely unrestricted by this -- what's restricted
// instead is Experiment 2 and Experiment 3, both narrowed to BTC only
// (tested in their own describe blocks below and in
// tests/exp3-eth-removal.test.js), which reduces real per-invocation
// load without needing any additional Cron Trigger.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

describe('BTC_BATCH / LINK_BATCH / ETH_BATCH — each names exactly one coin, both its horizons', () => {
  function scopeFor(constName) {
    return evalInScope(extractConstants(constName), {
      predictAndLog: () => 'BTC_FN', linkPredictAndLog: () => 'LINK_FN', ethPredictAndLog: () => 'ETH_FN',
    });
  }

  it('BTC_BATCH is BTC only, 24h and 12h', () => {
    const scope = scopeFor('BTC_BATCH');
    expect(scope.BTC_BATCH).toHaveLength(2);
    expect(scope.BTC_BATCH.every((c) => c.coin === 'BTC')).toBe(true);
    expect(new Set(scope.BTC_BATCH.map((c) => c.horizon))).toEqual(new Set([24, 12]));
  });

  it('LINK_BATCH is LINK only, 24h and 12h', () => {
    const scope = scopeFor('LINK_BATCH');
    expect(scope.LINK_BATCH).toHaveLength(2);
    expect(scope.LINK_BATCH.every((c) => c.coin === 'LINK')).toBe(true);
  });

  it('ETH_BATCH is ETH only, 24h and 12h', () => {
    const scope = scopeFor('ETH_BATCH');
    expect(scope.ETH_BATCH).toHaveLength(2);
    expect(scope.ETH_BATCH.every((c) => c.coin === 'ETH')).toBe(true);
  });
});

describe('runCoinCronTick — all three coins run sequentially in one shared invocation', () => {
  function buildScope() {
    const source = extractFunctions('runCoinHorizonChain', 'runCoinBatch', 'runCoinCronTick') + '\n\n' +
      extractConstants('BTC_BATCH', 'LINK_BATCH', 'ETH_BATCH');
    const calls = [];
    async function predictAndLog(env, horizon) { return track('BTC', horizon); }
    async function linkPredictAndLog(env, horizon) { return track('LINK', horizon); }
    async function ethPredictAndLog(env, horizon) { return track('ETH', horizon); }
    async function selectBestVariant(env, coin, horizon) { calls.push({ coin, horizon, fn: 'select', t: calls.length }); return { ok: true, status: 'ok' }; }
    async function track(coin, horizon) { calls.push({ coin, horizon, fn: 'predict', t: calls.length }); return { ok: true, status: 'ok', challenger: { ok: true } }; }
    const scope = evalInScope(source, { predictAndLog, linkPredictAndLog, ethPredictAndLog, selectBestVariant });
    return { scope, calls };
  }

  it('all six coin/horizon chains execute exactly once each within a single runCoinCronTick call', async () => {
    const { scope, calls } = buildScope();
    await scope.runCoinCronTick({});
    const predictCalls = calls.filter((c) => c.fn === 'predict');
    expect(predictCalls).toHaveLength(6);
    const pairs = new Set(predictCalls.map((c) => `${c.coin}-${c.horizon}`));
    expect(pairs).toEqual(new Set(['BTC-24', 'BTC-12', 'LINK-24', 'LINK-12', 'ETH-24', 'ETH-12']));
  });

  it('BTC fully completes before LINK starts, and LINK fully completes before ETH starts -- batches run sequentially, not concurrently with each other', async () => {
    const { scope, calls } = buildScope();
    await scope.runCoinCronTick({});
    const lastBtc = Math.max(...calls.filter((c) => c.coin === 'BTC').map((c) => c.t));
    const firstLink = Math.min(...calls.filter((c) => c.coin === 'LINK').map((c) => c.t));
    const lastLink = Math.max(...calls.filter((c) => c.coin === 'LINK').map((c) => c.t));
    const firstEth = Math.min(...calls.filter((c) => c.coin === 'ETH').map((c) => c.t));
    expect(firstLink).toBeGreaterThan(lastBtc);
    expect(firstEth).toBeGreaterThan(lastLink);
  });
});

describe('scheduled() dispatch: the shared 3h cron runs all three coins; the daily cron does not touch production batches', () => {
  const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');

  it('the "0 */3 * * *" branch calls runCoinCronTick (all three coins), not any single coin batch directly', () => {
    const idx = src.indexOf("event.cron === '0 */3 * * *'");
    expect(idx).toBeGreaterThan(-1);
    const closeIdx = src.indexOf('\n    }\n', idx);
    const block = src.slice(idx, closeIdx);
    expect(block).toContain('runCoinCronTick(env)');
    expect(block).not.toContain('runCoinBatch(env, BTC_BATCH)');
    expect(block).not.toContain('runCoinBatch(env, LINK_BATCH)');
    expect(block).not.toContain('runCoinBatch(env, ETH_BATCH)');
  });

  it('only one Cron Trigger literal exists for the 3h production cycle -- no stray "1 */3 * * *" or "2 */3 * * *" branches remain', () => {
    expect(src).not.toContain("event.cron === '1 */3 * * *'");
    expect(src).not.toContain("event.cron === '2 */3 * * *'");
  });

  it('the daily (0 7 * * *) branch contains no call to runCoinBatch or runCoinCronTick -- it only runs Gemini analyses, calibration refreshes, and Experiments 2/3', () => {
    const dailyIdx = src.indexOf("event.cron === '0 7 * * *'");
    const threeHourIdx = src.indexOf("event.cron === '0 */3 * * *'");
    expect(dailyIdx).toBeGreaterThan(-1);
    expect(threeHourIdx).toBeGreaterThan(dailyIdx);
    const dailyBlock = src.slice(dailyIdx, threeHourIdx);
    expect(dailyBlock).not.toContain('runCoinBatch(');
    expect(dailyBlock).not.toContain('runCoinCronTick(');
    expect(dailyBlock).toContain('runGeminiDailyAnalysis');
    expect(dailyBlock).toContain('refreshCalibrationCurve');
    expect(dailyBlock).toContain('logAnomalyGateExperiment');
    expect(dailyBlock).toContain('logMomentumSelectionExperiment');
  });
});

describe('wrangler.toml cron configuration', () => {
  it('declares exactly 2 Cron Triggers: one shared 3h production cycle, one daily -- fitting the account\'s real Workers Free budget (5 total account-wide; V1 already uses 3)', () => {
    const toml = readFileSync(new URL('../wrangler.toml', import.meta.url), 'utf8');
    const match = toml.match(/crons\s*=\s*\[([^\]]*)\]/);
    expect(match).not.toBeNull();
    const crons = match[1].split(',').map((s) => s.trim().replace(/^"|"$/g, ''));
    expect(crons).toEqual(['0 */3 * * *', '0 7 * * *']);
  });
});

describe('Experiment 2 (logAnomalyGateExperiment) is restricted to BTC only', () => {
  function buildScope() {
    const source = extractFunctions('logAnomalyGateExperiment', 'coreTableForCoin') + '\n\n' +
      extractConstants('SELECTION_VARIANTS', 'EXPERIMENT_2_COINS');
    return evalInScope(source);
  }

  it('EXPERIMENT_2_COINS contains exactly BTC', () => {
    const scope = buildScope();
    expect(scope.EXPERIMENT_2_COINS).toEqual(['BTC']);
  });

  it('LINK and ETH return coin_not_in_experiment before touching env.DB, for either horizon', async () => {
    const scope = buildScope();
    for (const coin of ['LINK', 'ETH']) {
      for (const horizon of [12, 24]) {
        let dbTouched = false;
        const db = { prepare() { dbTouched = true; throw new Error('should be unreachable'); } };
        const result = await scope.logAnomalyGateExperiment({ DB: db }, coin, horizon);
        expect(result).toEqual({ ok: true, status: 'coin_not_in_experiment', logged: false });
        expect(dbTouched).toBe(false);
      }
    }
  });

  it('BTC is unaffected by the guard -- it proceeds past it and does touch env.DB', async () => {
    const scope = buildScope();
    let dbTouched = false;
    const db = { prepare() { dbTouched = true; return { bind: () => ({ first: async () => null, all: async () => ({ results: [] }) }) }; } };
    await scope.logAnomalyGateExperiment({ DB: db }, 'BTC', 24);
    expect(dbTouched).toBe(true);
  });
});
