import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, evalInScope } from './helpers/extract.js';

describe('isRecentDataStale — the staleness check itself, real D1-shaped queries, no stubbing', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('isRecentDataStale'));
  });

  function makeDb(latestTs) {
    return {
      prepare(sql) {
        expect(sql).toMatch(/SELECT MAX\(ts\) as latest FROM btc_data/);
        return { first: async () => (latestTs == null ? null : { latest: latestTs }) };
      },
    };
  }

  it('an empty table (no rows at all) is treated as stale -- never silently assumed fresh', async () => {
    const result = await scope.isRecentDataStale({ DB: makeDb(null) }, 'btc_data');
    expect(result).toBe(true);
  });

  it('data from 1 hour ago is NOT stale (well within the normal ~3h cadence)', async () => {
    const oneHourAgo = Date.now() - 1 * 3600 * 1000;
    const result = await scope.isRecentDataStale({ DB: makeDb(oneHourAgo) }, 'btc_data');
    expect(result).toBe(false);
  });

  it('data from exactly 5 hours ago is NOT YET stale (default threshold is 6h)', async () => {
    const fiveHoursAgo = Date.now() - 5 * 3600 * 1000;
    const result = await scope.isRecentDataStale({ DB: makeDb(fiveHoursAgo) }, 'btc_data');
    expect(result).toBe(false);
  });

  it('data from 7 hours ago IS stale (past the default 6h threshold -- cron has genuinely missed a cycle)', async () => {
    const sevenHoursAgo = Date.now() - 7 * 3600 * 1000;
    const result = await scope.isRecentDataStale({ DB: makeDb(sevenHoursAgo) }, 'btc_data');
    expect(result).toBe(true);
  });

  it('a custom maxAgeMs is honored, not just the 6h default', async () => {
    const twoHoursAgo = Date.now() - 2 * 3600 * 1000;
    const staleAtOneHour = await scope.isRecentDataStale({ DB: makeDb(twoHoursAgo) }, 'btc_data', 1 * 3600 * 1000);
    expect(staleAtOneHour).toBe(true);
    const freshAtThreeHours = await scope.isRecentDataStale({ DB: makeDb(twoHoursAgo) }, 'btc_data', 3 * 3600 * 1000);
    expect(freshAtThreeHours).toBe(false);
  });
});

// predictAndLog's own dependencies (backfillPredictions, logBtcData,
// runPrediction, etc.) are stubbed as call-tracking fakes rather than
// exercised for real -- this test suite is specifically about
// predictAndLog's OWN orchestration/gating logic (did it call the right
// things with the right persist/allowWrite values), not a re-verification
// of k-NN correctness, which is completely unchanged and already covered
// by the rest of the suite. evalInScope's extraGlobals injection makes
// this possible: predictAndLog's extracted source references these names
// exactly as it would in the real file, resolved here as stub parameters
// instead of the real functions.
describe('predictAndLog — orchestration and write-gating logic', () => {
  function makeStubs({ stale }) {
    const calls = { logBtcData: 0, runPredictionPersist: [], runChallengerPersist: [], isRecentDataStaleCalled: 0, claimStaleRefreshCalled: 0 };
    return {
      calls,
      stubs: {
        backfillPredictions: async () => 0,
        backfillGeminiBiasShort: async () => 0,
        backfillChallengerPredictions: async () => 0,
        logBtcData: async () => { calls.logBtcData++; },
        isRecentDataStale: async () => { calls.isRecentDataStaleCalled++; return stale; },
        claimStaleRefresh: async () => { calls.claimStaleRefreshCalled++; return true; }, // single caller, always wins -- concurrency itself is covered separately
        runPrediction: async (env, horizon, opts) => {
          calls.runPredictionPersist.push(opts ? opts.persist : undefined);
          return { ok: true, status: 'ok', btc_price_now: 100, persisted: opts ? opts.persist : undefined };
        },
        runChallengerPrediction: async (env, args, opts) => {
          calls.runChallengerPersist.push(opts ? opts.persist : undefined);
          return { ok: true, status: 'ok', persisted: opts ? opts.persist : undefined };
        },
      },
    };
  }

  let source;
  beforeAll(() => {
    source = extractFunctions('predictAndLog', 'resolveShouldWrite');
  });

  it('1. CRON (allowWrite:true) always writes -- logBtcData called, both persist flags true, isRecentDataStale never even queried (short-circuit)', async () => {
    const { calls, stubs } = makeStubs({ stale: false }); // stale:false on purpose -- proves the OR short-circuits before ever checking
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: true });
    expect(calls.logBtcData).toBe(1);
    expect(calls.isRecentDataStaleCalled).toBe(0);
    expect(calls.runPredictionPersist).toEqual([true]);
    expect(calls.runChallengerPersist).toEqual([true]);
  });

  it('2. LIVE traffic with FRESH data (allowWrite:false, not stale) creates NO new rows at all -- logBtcData not called, both persist flags false', async () => {
    const { calls, stubs } = makeStubs({ stale: false });
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    expect(calls.logBtcData).toBe(0);
    expect(calls.isRecentDataStaleCalled).toBe(1);
    expect(calls.runPredictionPersist).toEqual([false]);
    expect(calls.runChallengerPersist).toEqual([false]);
  });

  it('2b. LIVE traffic with the allowWrite option omitted entirely (matching the real /predict route\'s actual call site) defaults to the same read-only behavior', async () => {
    const { calls, stubs } = makeStubs({ stale: false });
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24); // no third argument at all
    expect(calls.logBtcData).toBe(0);
    expect(calls.runPredictionPersist).toEqual([false]);
  });

  it('3. STALENESS FALLBACK: live traffic (allowWrite:false) with genuinely stale data writes once, same as cron would', async () => {
    const { calls, stubs } = makeStubs({ stale: true });
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    expect(calls.logBtcData).toBe(1);
    expect(calls.isRecentDataStaleCalled).toBe(1);
    expect(calls.claimStaleRefreshCalled).toBe(1); // the atomic claim was actually attempted, not just the staleness read
    expect(calls.runPredictionPersist).toEqual([true]);
    expect(calls.runChallengerPersist).toEqual([true]);
  });

  it('3b. staleness fallback correctly does NOT write when the atomic claim is lost (another concurrent caller already won it)', async () => {
    const { calls, stubs } = makeStubs({ stale: true });
    stubs.claimStaleRefresh = async () => { calls.claimStaleRefreshCalled = (calls.claimStaleRefreshCalled || 0) + 1; return false; }; // lost the claim
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    expect(calls.logBtcData).toBe(0); // data WAS stale, but this caller lost the claim -- must not write
    expect(calls.runPredictionPersist).toEqual([false]);
  });

  it('claimStaleRefresh is never even called when data is fresh -- the common case stays a single cheap read, no claim-table writes', async () => {
    const { calls, stubs } = makeStubs({ stale: false });
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    expect(calls.claimStaleRefreshCalled).toBe(0);
  });

  it('4. NO DUPLICATES: across 5 consecutive live calls with fresh data, zero writes ever happen -- confirms repeated page views cannot each create a new row', async () => {
    const { calls, stubs } = makeStubs({ stale: false });
    const scope = evalInScope(source, stubs);
    for (let i = 0; i < 5; i++) {
      await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    }
    expect(calls.logBtcData).toBe(0);
    expect(calls.runPredictionPersist).toEqual([false, false, false, false, false]);
  });

  it('resolution (backfillPredictions/backfillGeminiBiasShort) still runs on every call regardless of allowWrite -- resolving existing rows is not "creating new training data"', async () => {
    let backfillCalls = 0, geminiBackfillCalls = 0;
    const { stubs } = makeStubs({ stale: false });
    stubs.backfillPredictions = async () => { backfillCalls++; return 2; };
    stubs.backfillGeminiBiasShort = async () => { geminiBackfillCalls++; return 1; };
    const scope = evalInScope(source, stubs);
    const result = await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    expect(backfillCalls).toBe(1);
    expect(geminiBackfillCalls).toBe(1);
    expect(result.backfilled_this_call).toBe(2);
    expect(result.gemini_bias_backfilled_this_call).toBe(1);
  });

  it('a Challenger failure never breaks the core prediction response, same resilience contract as before this change', async () => {
    const { stubs } = makeStubs({ stale: false });
    stubs.runChallengerPrediction = async () => { throw new Error('challenger boom'); };
    const scope = evalInScope(source, stubs);
    const result = await scope.predictAndLog({ DB: {} }, 24, { allowWrite: true });
    expect(result.ok).toBe(true);
    expect(result.challenger.ok).toBe(false);
    expect(result.challenger.error).toContain('challenger boom');
  });
});

describe('regression: runPrediction/runLinkPrediction/runEthPrediction/runChallengerPrediction all accept and honor a persist option', () => {
  it('all four functions have persist = true as their default (safe backward-compatible default for any other caller)', () => {
    for (const name of ['runPrediction', 'runLinkPrediction', 'runEthPrediction']) {
      const src = extractFunctions(name);
      expect(src).toMatch(/\{ persist = true \} = \{\}/);
      expect(src).toMatch(/if \(persist\) \{/);
    }
    const challengerSrc = extractFunctions('runChallengerPrediction');
    expect(challengerSrc).toMatch(/\{ persist = true \} = \{\}/);
    expect(challengerSrc).toMatch(/if \(persist\) \{/);
  });

  it('when not persisted, each function still returns prediction_id/id: null rather than throwing on a missing insert result', () => {
    for (const name of ['runPrediction', 'runLinkPrediction', 'runEthPrediction']) {
      const src = extractFunctions(name);
      expect(src).toMatch(/insert \? insert\.meta\.last_row_id : null/);
    }
  });
});

describe('regression: the cron dispatch explicitly passes allowWrite:true, the only place that does', () => {
  it('predictThenSelect passes { allowWrite: true } to whichever predict function it calls', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf('const predictThenSelect');
    expect(idx).toBeGreaterThan(-1);
    const nearby = src.slice(idx, idx + 300);
    expect(nearby).toContain('predictFn(env, horizon, { allowWrite: true })');
  });

  it('none of the three live routes (/predict, /link-predict, /eth-predict) pass allowWrite at all -- they rely on the safe read-only default', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toMatch(/const result = await predictAndLog\(env, horizon\);/);
    expect(src).toMatch(/const result = await linkPredictAndLog\(env, horizon\);/);
    expect(src).toMatch(/const result = await ethPredictAndLog\(env, horizon\);/);
  });
});
