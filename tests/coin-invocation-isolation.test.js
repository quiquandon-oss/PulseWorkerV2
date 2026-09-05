// Regression tests for the coin-invocation-isolation fix (2026-09-05).
//
// Direct Cloudflare production evidence (Workers Logs, 2026-09-05 12:00
// UTC scheduled tick) confirmed outcome=exceededCpu, cpuTimeMs=2010,
// wallTimeMs=6401 -- the shared per-invocation CPU budget, not
// concurrency shape, was starving whichever coin(s) hadn't finished
// before the platform terminated the invocation. Fix: BTC, LINK, and
// ETH now each get their own scheduled() invocation via staggered cron
// minutes (BTC "0 */3 * * *", LINK "1 */3 * * *", ETH "2 */3 * * *"),
// instead of one shared "0 */3 * * *" running all three sequentially.
//
// This file tests ONLY the dispatch mapping itself -- that each cron
// string invokes exactly the right coin's batch and no other. It does
// not re-test runCoinBatch/runCoinHorizonChain's own behavior (covered
// in tests/link-eth-selection-starvation-fix.test.js, unchanged) or
// selectBestVariant's decision logic (covered extensively elsewhere).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

describe('BTC_BATCH / LINK_BATCH / ETH_BATCH — each names exactly one coin, both its horizons', () => {
  // The batch constants reference predictAndLog/linkPredictAndLog/
  // ethPredictAndLog by name (real functions defined elsewhere in
  // worker.js) -- providing minimal stubs as sandbox globals here so the
  // constant can actually be evaluated, matching the pattern the fourth
  // test below already uses.
  function scopeFor(constName) {
    return evalInScope(extractConstants(constName), {
      predictAndLog: () => 'BTC_FN', linkPredictAndLog: () => 'LINK_FN', ethPredictAndLog: () => 'ETH_FN',
    });
  }

  it('BTC_BATCH is BTC only, 24h and 12h, using predictAndLog', () => {
    const scope = scopeFor('BTC_BATCH');
    expect(scope.BTC_BATCH).toHaveLength(2);
    expect(scope.BTC_BATCH.every((c) => c.coin === 'BTC')).toBe(true);
    expect(new Set(scope.BTC_BATCH.map((c) => c.horizon))).toEqual(new Set([24, 12]));
  });

  it('LINK_BATCH is LINK only, 24h and 12h', () => {
    const scope = scopeFor('LINK_BATCH');
    expect(scope.LINK_BATCH).toHaveLength(2);
    expect(scope.LINK_BATCH.every((c) => c.coin === 'LINK')).toBe(true);
    expect(new Set(scope.LINK_BATCH.map((c) => c.horizon))).toEqual(new Set([24, 12]));
  });

  it('ETH_BATCH is ETH only, 24h and 12h', () => {
    const scope = scopeFor('ETH_BATCH');
    expect(scope.ETH_BATCH).toHaveLength(2);
    expect(scope.ETH_BATCH.every((c) => c.coin === 'ETH')).toBe(true);
    expect(new Set(scope.ETH_BATCH.map((c) => c.horizon))).toEqual(new Set([24, 12]));
  });

  it('the three batches use three different predict functions (predictAndLog, linkPredictAndLog, ethPredictAndLog) -- no cross-wiring', () => {
    const scope = evalInScope(extractConstants('BTC_BATCH', 'LINK_BATCH', 'ETH_BATCH'), {
      predictAndLog: () => 'BTC_FN', linkPredictAndLog: () => 'LINK_FN', ethPredictAndLog: () => 'ETH_FN',
    });
    expect(scope.BTC_BATCH[0].predictFn()).toBe('BTC_FN');
    expect(scope.LINK_BATCH[0].predictFn()).toBe('LINK_FN');
    expect(scope.ETH_BATCH[0].predictFn()).toBe('ETH_FN');
  });
});

describe('scheduled() dispatch: each cron string maps to exactly one coin batch', () => {
  // scheduled() is an object-method shorthand, not extractable via
  // extractFunctions (same limitation noted in other test files that
  // inspect it) -- reads the raw source and locates each branch by its
  // exact `else if (event.cron === '...')` boundary instead.
  const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');

  function branchFor(cronLiteral) {
    const idx = src.indexOf(`event.cron === '${cronLiteral}'`);
    expect(idx).toBeGreaterThan(-1);
    // Bounded by the next `} else if` / `}` that closes this branch --
    // find the next occurrence of "} else if (event.cron" or the final
    // closing of the if/else chain, whichever comes first.
    const nextElseIf = src.indexOf('} else if (event.cron', idx + 10);
    const closeBrace = src.indexOf('\n    }\n', idx);
    const end = nextElseIf > -1 && nextElseIf < closeBrace ? nextElseIf : closeBrace;
    return src.slice(idx, end);
  }

  it('BTC\'s cron ("0 */3 * * *") calls runCoinBatch with BTC_BATCH only -- no mention of LINK_BATCH or ETH_BATCH', () => {
    const block = branchFor('0 */3 * * *');
    expect(block).toContain('runCoinBatch(env, BTC_BATCH)');
    expect(block).not.toContain('LINK_BATCH');
    expect(block).not.toContain('ETH_BATCH');
  });

  it('LINK\'s cron ("1 */3 * * *") calls runCoinBatch with LINK_BATCH only -- no mention of BTC_BATCH or ETH_BATCH', () => {
    const block = branchFor('1 */3 * * *');
    expect(block).toContain('runCoinBatch(env, LINK_BATCH)');
    expect(block).not.toContain('BTC_BATCH');
    expect(block).not.toContain('ETH_BATCH');
  });

  it('ETH\'s cron ("2 */3 * * *") calls runCoinBatch with ETH_BATCH only -- no mention of BTC_BATCH or LINK_BATCH', () => {
    const block = branchFor('2 */3 * * *');
    expect(block).toContain('runCoinBatch(env, ETH_BATCH)');
    expect(block).not.toContain('BTC_BATCH');
    expect(block).not.toContain('LINK_BATCH');
  });

  it('the old all-coins function (runPredictionCronTick) no longer exists as a callable function -- it cannot accidentally still be invoked (mentions in explanatory comments are fine and expected)', () => {
    expect(src).not.toContain('function runPredictionCronTick(');
    expect(src).not.toContain('runPredictionCronTick(env)');
  });

  it('no single scheduled() branch calls runCoinBatch more than once -- confirms one cron trigger can never dispatch multiple coins', () => {
    for (const cronLiteral of ['0 */3 * * *', '1 */3 * * *', '2 */3 * * *']) {
      const block = branchFor(cronLiteral);
      const calls = block.match(/runCoinBatch\(/g) || [];
      expect(calls).toHaveLength(1);
    }
  });

  it('the daily (0 7 * * *) branch is unchanged in position and content -- still runs Gemini analyses, calibration refreshes, and Experiments 2/3 for all three coins', () => {
    const dailyIdx = src.indexOf("event.cron === '0 7 * * *'");
    expect(dailyIdx).toBeGreaterThan(-1);
    const firstThreeHourIdx = src.indexOf("event.cron === '0 */3 * * *'");
    expect(firstThreeHourIdx).toBeGreaterThan(dailyIdx); // daily branch still comes first
    const dailyBlock = src.slice(dailyIdx, firstThreeHourIdx);
    expect(dailyBlock).toContain('runGeminiDailyAnalysis');
    expect(dailyBlock).toContain('runLinkGeminiAnalysis');
    expect(dailyBlock).toContain("for (const coin of ['BTC', 'LINK', 'ETH']) {");
    expect(dailyBlock).toContain('refreshCalibrationCurve');
    expect(dailyBlock).toContain('refreshChallengerCalibrationCurve');
    expect(dailyBlock).toContain('logAnomalyGateExperiment');
    expect(dailyBlock).toContain('logMomentumSelectionExperiment');
  });
});

describe('wrangler.toml cron configuration', () => {
  it('declares exactly 4 cron triggers: BTC (:00), LINK (:01), ETH (:02) every 3h, plus the unchanged daily 07:00', () => {
    const toml = readFileSync(new URL('../wrangler.toml', import.meta.url), 'utf8');
    const match = toml.match(/crons\s*=\s*\[([^\]]*)\]/);
    expect(match).not.toBeNull();
    const crons = match[1].split(',').map((s) => s.trim().replace(/^"|"$/g, ''));
    expect(crons).toEqual(['0 */3 * * *', '1 */3 * * *', '2 */3 * * *', '0 7 * * *']);
  });

  it('every 3h cron string is unique -- no duplicate that could double-dispatch the same coin', () => {
    const toml = readFileSync(new URL('../wrangler.toml', import.meta.url), 'utf8');
    const match = toml.match(/crons\s*=\s*\[([^\]]*)\]/);
    const crons = match[1].split(',').map((s) => s.trim().replace(/^"|"$/g, ''));
    expect(new Set(crons).size).toBe(crons.length);
  });
});
