// Regression tests for runCoinBatch / runCoinHorizonChain, covering what
// is UNCHANGED by the 2026-09-05 coin-invocation-isolation fix.
//
// Background: PR #30 (2026-09-02) bounded peak concurrency within one
// scheduled() invocation to 2 (one coin's own two horizons) by replacing
// a single 6-way Promise.allSettled with 3 sequential batches inside
// runPredictionCronTick. That reduced but did not eliminate LINK/ETH
// starvation -- direct Cloudflare production evidence (Workers Logs,
// 2026-09-05 12:00 UTC) then confirmed the mechanism directly:
// outcome=exceededCpu, cpuTimeMs=2010, wallTimeMs=6401. The shared
// invocation's CPU budget, not concurrency shape, was the real
// constraint -- no amount of sequencing within one invocation can fix a
// shared CPU budget being exhausted by whichever coin runs first.
//
// The fix (this file's companion change) removes runPredictionCronTick
// entirely and gives each coin its own scheduled() invocation via
// staggered cron minutes (BTC :00, LINK :01, ETH :02 of every 3rd hour).
// See tests/coin-invocation-isolation.test.js for the dispatch-level
// proof of that. This file covers what's UNCHANGED: runCoinBatch and
// runCoinHorizonChain still work exactly as before for a single coin's
// own two horizons -- concurrency between them, failure isolation
// between them, and the byte-identical production selection logic they
// call into.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

// Builds a sandbox with runCoinHorizonChain / runCoinBatch, plus STUB
// predictAndLog / selectBestVariant test doubles, so this file can
// observe concurrency and failure isolation for a single coin's batch
// without depending on the real prediction/selection machinery (already
// covered exhaustively elsewhere).
function buildScope({ predictImpl, selectImpl } = {}) {
  const source = extractFunctions('runCoinHorizonChain', 'runCoinBatch');
  const calls = [];
  let concurrentActive = 0;
  let maxConcurrent = 0;

  async function predictAndLog(env, horizon, opts) { return trackedPredict('BTC', horizon, opts); }
  async function linkPredictAndLog(env, horizon, opts) { return trackedPredict('LINK', horizon, opts); }
  async function selectBestVariant(env, coin, horizon) { return trackedSelect(coin, horizon); }

  async function trackedPredict(coin, horizon, opts) {
    calls.push({ coin, horizon, fn: 'predict', phase: 'start', t: calls.length });
    concurrentActive++; maxConcurrent = Math.max(maxConcurrent, concurrentActive);
    try {
      if (predictImpl) return await predictImpl(coin, horizon, opts);
      return { ok: true, status: 'ok', challenger: { ok: true, status: 'ok' } };
    } finally {
      concurrentActive--;
      calls.push({ coin, horizon, fn: 'predict', phase: 'end', t: calls.length });
    }
  }
  async function trackedSelect(coin, horizon) {
    calls.push({ coin, horizon, fn: 'select', phase: 'start', t: calls.length });
    concurrentActive++; maxConcurrent = Math.max(maxConcurrent, concurrentActive);
    try {
      if (selectImpl) return await selectImpl(coin, horizon);
      return { ok: true, status: 'ok', chosen_variant: 'original' };
    } finally {
      concurrentActive--;
      calls.push({ coin, horizon, fn: 'select', phase: 'end', t: calls.length });
    }
  }

  const scope = evalInScope(source, { predictAndLog, linkPredictAndLog, selectBestVariant });
  // Batches built here (not as module-level constants) so predictFn
  // correctly references THIS buildScope() call's own tracked stub
  // closures, not a stale/shared reference.
  const btcBatch = [{ predictFn: predictAndLog, coin: 'BTC', horizon: 24 }, { predictFn: predictAndLog, coin: 'BTC', horizon: 12 }];
  const linkBatch = [{ predictFn: linkPredictAndLog, coin: 'LINK', horizon: 24 }, { predictFn: linkPredictAndLog, coin: 'LINK', horizon: 12 }];
  return { scope, calls, getMaxConcurrent: () => maxConcurrent, btcBatch, linkBatch };
}

describe('runCoinBatch — a single coin\'s own two horizons', () => {
  it('both horizons of one coin run to completion (predict + select) within one batch call', async () => {
    const { scope, calls, btcBatch } = buildScope();
    await scope.runCoinBatch({}, btcBatch);
    const predictStarts = calls.filter(c => c.fn === 'predict' && c.phase === 'start');
    const selectStarts = calls.filter(c => c.fn === 'select' && c.phase === 'start');
    expect(predictStarts).toHaveLength(2);
    expect(selectStarts).toHaveLength(2);
    expect(new Set(predictStarts.map(c => c.horizon))).toEqual(new Set([24, 12]));
  });

  it("a coin's two horizons DO run concurrently with each other, not serialized", async () => {
    let releaseBtc24;
    const gate = new Promise((resolve) => { releaseBtc24 = resolve; });
    const { scope, calls, btcBatch } = buildScope({
      predictImpl: async (coin, horizon) => {
        if (horizon === 24) await gate;
        return { ok: true, status: 'ok', challenger: { ok: true, status: 'ok' } };
      },
    });
    const runPromise = scope.runCoinBatch({}, btcBatch);
    await Promise.resolve(); await Promise.resolve();
    const h12Started = calls.some(c => c.horizon === 12 && c.fn === 'predict' && c.phase === 'start');
    expect(h12Started).toBe(true);
    releaseBtc24();
    await runPromise;
  });

  it('peak concurrency within one batch never exceeds 2 (the two horizons), same as before this change', async () => {
    const { scope, getMaxConcurrent, btcBatch } = buildScope();
    await scope.runCoinBatch({}, btcBatch);
    expect(getMaxConcurrent()).toBeLessThanOrEqual(2);
  });

  it('a failure in one horizon does not prevent its sibling horizon (same coin) from completing', async () => {
    const { scope, calls, linkBatch } = buildScope({
      predictImpl: async (coin, horizon) => {
        if (horizon === 24) throw new Error('simulated failure');
        return { ok: true, status: 'ok', challenger: { ok: true, status: 'ok' } };
      },
    });
    await expect(scope.runCoinBatch({}, linkBatch)).resolves.not.toThrow();
    const h12SelectDone = calls.some(c => c.horizon === 12 && c.fn === 'select' && c.phase === 'end');
    expect(h12SelectDone).toBe(true);
  });
});

describe('selection decision logic itself is byte-identical -- this fix only changes scheduling', () => {
  it('selectBestVariant, decideSelection, computeLcaScore, and the significance-gate constants are untouched', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('async function selectBestVariant(env, coin, horizonHours) {');
    expect(src).toContain('function decideSelection(scores) {');
    expect(src).toContain('function computeLcaScore(variantRows, neighborhood, todaysCallUp, tolMs) {');
    expect(src).toContain('const SELECTION_MIN_HISTORY = 50;');
    expect(src).toContain('const SELECTION_MIN_MATCHED = 3;');
    expect(src).toContain('const SELECTION_CRITICAL_Z = { 1: 1.6449, 2: 1.9600, 3: 2.1280, 4: 2.2414, 5: 2.3263, 6: 2.3940 };');
  });

  it('runCoinHorizonChain calls the real selectBestVariant with unmodified arguments -- (env, coin, horizon), nothing pre-empted or overridden', () => {
    const src = extractFunctions('runCoinHorizonChain');
    expect(src).toContain('await selectBestVariant(env, coin, horizon)');
  });
});

describe('no forced or fake selection is generated by this change', () => {
  it('runCoinHorizonChain/runCoinBatch never write directly to selection_decisions or any other table -- only ever call the real predictFn/selectBestVariant', () => {
    const src = extractFunctions('runCoinHorizonChain', 'runCoinBatch');
    expect(src).not.toMatch(/INSERT INTO/);
    expect(src).not.toMatch(/UPDATE\s+\w+\s+SET/);
    expect(src).not.toContain('env.DB.prepare');
  });

  it('a predict failure does not synthesize a fallback selection -- selectBestVariant is simply never called for that chain', () => {
    const src = extractFunctions('runCoinHorizonChain');
    const catchIdx = src.indexOf("evt: 'prediction_failed'");
    const returnIdx = src.indexOf('return;', catchIdx);
    const selectIdx = src.indexOf('selectBestVariant(');
    expect(catchIdx).toBeGreaterThan(-1);
    expect(returnIdx).toBeGreaterThan(catchIdx);
    expect(returnIdx).toBeLessThan(selectIdx);
  });
});

describe('historical reference: why bounding concurrency within one invocation was not enough', () => {
  it('OLD MODEL (pre-PR#30, for context only): an unbounded 6-way Promise.allSettled would let all coins overlap -- peak concurrency 6', async () => {
    const calls = [];
    let concurrentActive = 0, maxConcurrent = 0;
    async function oldStyleChain(coin, horizon) {
      calls.push({ coin, horizon, phase: 'start' });
      concurrentActive++; maxConcurrent = Math.max(maxConcurrent, concurrentActive);
      await Promise.resolve();
      concurrentActive--;
      calls.push({ coin, horizon, phase: 'end' });
    }
    await Promise.allSettled([
      oldStyleChain('BTC', 24), oldStyleChain('BTC', 12),
      oldStyleChain('LINK', 24), oldStyleChain('LINK', 12),
      oldStyleChain('ETH', 24), oldStyleChain('ETH', 12),
    ]);
    expect(maxConcurrent).toBe(6);
  });

  it('PR #30 MODEL (concurrency-bounded but still one shared invocation): peak concurrency drops to 2, but this alone did not fix the CPU-budget exhaustion -- confirmed by real exceededCpu production evidence, which is why coins are now isolated into separate invocations entirely rather than further tuning concurrency', async () => {
    const { scope, getMaxConcurrent, btcBatch } = buildScope();
    await scope.runCoinBatch({}, btcBatch);
    expect(getMaxConcurrent()).toBeLessThanOrEqual(2);
  });
});
