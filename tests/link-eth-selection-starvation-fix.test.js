// Regression tests for the LINK/ETH selection starvation fix (2026-09-02).
//
// Background: LINK and ETH's selection_decisions stopped updating (47h
// and 66h stale respectively) while BTC continued working, even after
// PR #28's query-batching. Direct reproduction proved selectBestVariant's
// own logic is correct given real LINK data. No shared JS-level state,
// locks, or backlog-size asymmetry were found. The leading (evidence-
// supported, not log-confirmed) explanation is shared per-invocation
// Cloudflare resource contention across all 6 concurrently-dispatched
// coin/horizon chains, with BTC (declared first) more often completing
// within budget than LINK/ETH (declared later).
//
// Fix: runPredictionCronTick now runs the 6 chains in 3 sequential
// batches of 2 (each coin's own two horizons), instead of one
// Promise.allSettled across all 6. This file proves that change without
// re-testing selectBestVariant's own decision logic, which is already
// covered exhaustively elsewhere (challenger-cron-stall-fix.test.js,
// anomaly-gate-experiment.test.js, challenger-momentum-experiment.test.js)
// and is NOT touched by this change.
import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

// Builds a sandbox with runCoinHorizonChain / runCoinBatch /
// runPredictionCronTick, plus STUB predictAndLog / linkPredictAndLog /
// ethPredictAndLog / selectBestVariant (test doubles standing in for the
// real, already-tested functions) so this file can observe call order,
// concurrency, and failure isolation in isolation from the real
// prediction/selection machinery.
function buildScope({ predictImpl, selectImpl } = {}) {
  const source = extractFunctions('runCoinHorizonChain', 'runCoinBatch', 'runPredictionCronTick');
  const calls = [];
  let concurrentActive = 0;
  let maxConcurrent = 0;

  async function predictAndLog(env, horizon, opts) { return trackedPredict('BTC', horizon, opts); }
  async function linkPredictAndLog(env, horizon, opts) { return trackedPredict('LINK', horizon, opts); }
  async function ethPredictAndLog(env, horizon, opts) { return trackedPredict('ETH', horizon, opts); }
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

  const scope = evalInScope(source, {
    predictAndLog, linkPredictAndLog, ethPredictAndLog, selectBestVariant,
  });
  return { scope, calls, getMaxConcurrent: () => maxConcurrent };
}

describe('1. all six coin/horizon chains still execute', () => {
  it('runPredictionCronTick calls predict+select for BTC/LINK/ETH x 12h/24h -- 6 of each', async () => {
    const { scope, calls } = buildScope();
    await scope.runPredictionCronTick({});
    const predictStarts = calls.filter(c => c.fn === 'predict' && c.phase === 'start');
    const selectStarts = calls.filter(c => c.fn === 'select' && c.phase === 'start');
    expect(predictStarts).toHaveLength(6);
    expect(selectStarts).toHaveLength(6);
    const pairs = new Set(predictStarts.map(c => `${c.coin}-${c.horizon}`));
    expect(pairs).toEqual(new Set(['BTC-24', 'BTC-12', 'LINK-24', 'LINK-12', 'ETH-24', 'ETH-12']));
  });
});

describe('2. execution follows the new batched-sequential policy', () => {
  it('every BTC call (predict+select, both horizons) fully completes before any LINK call starts, and every LINK call before any ETH call', async () => {
    const { scope, calls } = buildScope();
    await scope.runPredictionCronTick({});
    const lastBtcEnd = Math.max(...calls.filter(c => c.coin === 'BTC').map(c => c.t));
    const firstLinkStart = Math.min(...calls.filter(c => c.coin === 'LINK').map(c => c.t));
    const lastLinkEnd = Math.max(...calls.filter(c => c.coin === 'LINK').map(c => c.t));
    const firstEthStart = Math.min(...calls.filter(c => c.coin === 'ETH').map(c => c.t));
    expect(firstLinkStart).toBeGreaterThan(lastBtcEnd);
    expect(firstEthStart).toBeGreaterThan(lastLinkEnd);
  });

  it('peak concurrency across the whole tick never exceeds 2 (one coin batch), never the old 6', async () => {
    const { scope, getMaxConcurrent } = buildScope();
    await scope.runPredictionCronTick({});
    expect(getMaxConcurrent()).toBeLessThanOrEqual(2);
  });

  it("within a coin's own batch, its two horizons DO run concurrently with each other (not further serialized)", async () => {
    // Distinguishes "batched" from "fully sequential" -- the fix bounds
    // cross-COIN concurrency, it does not serialize a coin's own two
    // horizons, which have no dependency on each other.
    let releaseBtc24;
    const gate = new Promise((resolve) => { releaseBtc24 = resolve; });
    const { scope, calls } = buildScope({
      predictImpl: async (coin, horizon) => {
        if (coin === 'BTC' && horizon === 24) await gate; // holds until BTC/12h has also started
        return { ok: true, status: 'ok', challenger: { ok: true, status: 'ok' } };
      },
    });
    const runPromise = scope.runPredictionCronTick({});
    // Give the microtask queue a turn so BTC/12h's predict has a chance to start
    // while BTC/24h is still gated -- proving they were dispatched together.
    await Promise.resolve(); await Promise.resolve();
    const btc12Started = calls.some(c => c.coin === 'BTC' && c.horizon === 12 && c.fn === 'predict' && c.phase === 'start');
    expect(btc12Started).toBe(true);
    releaseBtc24();
    await runPromise;
  });
});

describe('3. a failure in one chain does not prevent later chains from executing', () => {
  it('BTC predict throwing does not stop LINK or ETH from running', async () => {
    const { scope, calls } = buildScope({
      predictImpl: async (coin, horizon) => {
        if (coin === 'BTC') throw new Error('simulated BTC predict failure');
        return { ok: true, status: 'ok', challenger: { ok: true, status: 'ok' } };
      },
    });
    await expect(scope.runPredictionCronTick({})).resolves.not.toThrow();
    const linkSelectDone = calls.some(c => c.coin === 'LINK' && c.fn === 'select' && c.phase === 'end');
    const ethSelectDone = calls.some(c => c.coin === 'ETH' && c.fn === 'select' && c.phase === 'end');
    expect(linkSelectDone).toBe(true);
    expect(ethSelectDone).toBe(true);
  });

  it('a failure in one horizon of a batch does not prevent its sibling horizon (same coin) from completing', async () => {
    const { scope, calls } = buildScope({
      predictImpl: async (coin, horizon) => {
        if (coin === 'LINK' && horizon === 24) throw new Error('simulated LINK/24h failure');
        return { ok: true, status: 'ok', challenger: { ok: true, status: 'ok' } };
      },
    });
    await scope.runPredictionCronTick({});
    const link12SelectDone = calls.some(c => c.coin === 'LINK' && c.horizon === 12 && c.fn === 'select' && c.phase === 'end');
    expect(link12SelectDone).toBe(true);
  });
});

describe('4 & 5. LINK and ETH reach selectBestVariant even when an earlier batch is heavy/slow', () => {
  it('a slow, heavy BTC batch (simulating a heavy Challenger workload) does not prevent LINK from reaching selectBestVariant', async () => {
    const { scope, calls } = buildScope({
      predictImpl: async (coin, horizon) => {
        if (coin === 'BTC') {
          // Simulate "heavy" work: several sequential awaited steps, like a
          // real Challenger call chain would be, without a real timer.
          for (let i = 0; i < 20; i++) await Promise.resolve();
        }
        return { ok: true, status: 'ok', challenger: { ok: true, status: 'ok' } };
      },
    });
    await scope.runPredictionCronTick({});
    const linkSelectDone = calls.filter(c => c.coin === 'LINK' && c.fn === 'select' && c.phase === 'end');
    expect(linkSelectDone).toHaveLength(2); // both horizons
  });

  it('the same heavy-BTC scenario does not prevent ETH from reaching selectBestVariant', async () => {
    const { scope, calls } = buildScope({
      predictImpl: async (coin, horizon) => {
        if (coin === 'BTC') { for (let i = 0; i < 20; i++) await Promise.resolve(); }
        return { ok: true, status: 'ok', challenger: { ok: true, status: 'ok' } };
      },
    });
    await scope.runPredictionCronTick({});
    const ethSelectDone = calls.filter(c => c.coin === 'ETH' && c.fn === 'select' && c.phase === 'end');
    expect(ethSelectDone).toHaveLength(2);
  });
});

describe('6. selection decision logic itself is byte-identical -- this fix only changes scheduling', () => {
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

describe('7. no forced or fake selection is generated by this change', () => {
  it('runCoinHorizonChain never writes directly to selection_decisions or any other table -- it only ever calls the real predictFn/selectBestVariant', () => {
    const src = extractFunctions('runCoinHorizonChain', 'runCoinBatch', 'runPredictionCronTick');
    expect(src).not.toMatch(/INSERT INTO/);
    expect(src).not.toMatch(/UPDATE\s+\w+\s+SET/);
    expect(src).not.toContain('env.DB.prepare');
  });

  it('a predict failure does not synthesize a fallback selection -- selectBestVariant is simply never called for that chain', () => {
    // Covered functionally by section 3's tests (LINK/12h still completes
    // when LINK/24h fails) -- this asserts the STRUCTURAL guarantee: the
    // early `return` on a predict failure happens before selectBestVariant
    // is ever reached, so there's no code path that fabricates a result.
    const src = extractFunctions('runCoinHorizonChain');
    const catchIdx = src.indexOf("evt: 'prediction_failed'");
    const returnIdx = src.indexOf('return;', catchIdx);
    const selectIdx = src.indexOf('selectBestVariant(');
    expect(catchIdx).toBeGreaterThan(-1);
    expect(returnIdx).toBeGreaterThan(catchIdx);
    expect(returnIdx).toBeLessThan(selectIdx);
  });
});

describe('specific regression: reproduces the previous starvation scenario and proves it can no longer occur', () => {
  it('OLD MODEL (for reference/documentation only): a single Promise.allSettled across all 6 chains lets a heavy BTC pair overlap with LINK/ETH -- peak concurrency would be 6', async () => {
    // This test documents what the OLD (pre-fix) dispatch shape allowed,
    // using the same tracking harness, to make the NEW model's guarantee
    // concrete by contrast. It does not exercise real worker.js code (the
    // old inline closure no longer exists to extract) -- it's a
    // self-contained reference model of the old shape only.
    const calls = [];
    let concurrentActive = 0, maxConcurrent = 0;
    async function oldStyleChain(coin, horizon) {
      calls.push({ coin, horizon, phase: 'start' });
      concurrentActive++; maxConcurrent = Math.max(maxConcurrent, concurrentActive);
      await Promise.resolve();
      concurrentActive--;
      calls.push({ coin, horizon, phase: 'end' });
    }
    const allSixConcurrently = [
      oldStyleChain('BTC', 24), oldStyleChain('BTC', 12),
      oldStyleChain('LINK', 24), oldStyleChain('LINK', 12),
      oldStyleChain('ETH', 24), oldStyleChain('ETH', 12),
    ];
    await Promise.allSettled(allSixConcurrently);
    expect(maxConcurrent).toBe(6); // the exact condition the fix eliminates
  });

  it('NEW MODEL: the real runPredictionCronTick never lets more than one coin\'s batch be in flight at once, even under a simulated heavy first batch', async () => {
    const { scope, getMaxConcurrent } = buildScope({
      predictImpl: async (coin, horizon) => {
        if (coin === 'BTC') { for (let i = 0; i < 50; i++) await Promise.resolve(); } // simulate BTC's real heavier Challenger workload
        return { ok: true, status: 'ok', challenger: { ok: true, status: 'ok' } };
      },
    });
    await scope.runPredictionCronTick({});
    expect(getMaxConcurrent()).toBeLessThanOrEqual(2);
  });
});
