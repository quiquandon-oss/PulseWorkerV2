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
// exercised for real -- this suite is specifically about predictAndLog's
// OWN orchestration/gating logic, not a re-verification of k-NN
// correctness or the conditional-SQL mechanism itself (covered for real
// in staleness-fallback-concurrency.test.js).
describe('predictAndLog — orchestration and write-authorization logic', () => {
  function makeStubs({ stale, claimTokenResult = 'fake-token-123' }) {
    const calls = { logBtcData: [], runPredictionArgs: [], runChallengerArgs: [], isRecentDataStaleCalled: 0, claimStaleRefreshCalled: 0, backfillChallengerCalledBeforeClaim: false };
    let claimResolved = false;
    return {
      calls,
      stubs: {
        backfillPredictions: async () => 0,
        backfillGeminiBiasShort: async () => 0,
        backfillChallengerPredictions: async () => { calls.backfillChallengerCalledBeforeClaim = !claimResolved; return 0; },
        logBtcData: async (env, claimToken) => { calls.logBtcData.push(claimToken); },
        isRecentDataStale: async () => { calls.isRecentDataStaleCalled++; return stale; },
        claimStaleRefresh: async () => { calls.claimStaleRefreshCalled++; claimResolved = true; return claimTokenResult; },
        runPrediction: async (env, horizon, opts) => {
          calls.runPredictionArgs.push(opts);
          return { ok: true, status: 'ok', btc_price_now: 100, persisted: opts ? opts.persist : undefined };
        },
        runChallengerPrediction: async (env, args, opts) => {
          calls.runChallengerArgs.push(opts);
          return { ok: true, status: 'ok', persisted: opts ? opts.persist : undefined };
        },
      },
    };
  }

  let source;
  beforeAll(() => {
    source = extractFunctions('predictAndLog', 'resolveWriteAuthorization');
  });

  it('1. CRON (allowWrite:true) always writes with claimToken:null -- logBtcData called with null, isRecentDataStale never queried (short-circuit)', async () => {
    const { calls, stubs } = makeStubs({ stale: false }); // stale:false on purpose -- proves the short-circuit happens before ever checking
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: true });
    expect(calls.logBtcData).toEqual([null]);
    expect(calls.isRecentDataStaleCalled).toBe(0);
    expect(calls.runPredictionArgs).toEqual([{ persist: true, claimToken: null }]);
    expect(calls.runChallengerArgs).toEqual([{ persist: true, claimToken: null }]);
  });

  it('2. LIVE traffic with FRESH data (allowWrite:false, not stale) creates NO new rows -- logBtcData not called, persist:false everywhere', async () => {
    const { calls, stubs } = makeStubs({ stale: false });
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    expect(calls.logBtcData).toEqual([]);
    expect(calls.isRecentDataStaleCalled).toBe(1);
    expect(calls.claimStaleRefreshCalled).toBe(0); // never even attempts a claim when not stale
    expect(calls.runPredictionArgs).toEqual([{ persist: false, claimToken: null }]);
    expect(calls.runChallengerArgs).toEqual([{ persist: false, claimToken: null }]);
  });

  it('2b. LIVE traffic with allowWrite omitted entirely (matching the real /predict route) defaults to the same read-only behavior', async () => {
    const { calls, stubs } = makeStubs({ stale: false });
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24);
    expect(calls.logBtcData).toEqual([]);
    expect(calls.runPredictionArgs).toEqual([{ persist: false, claimToken: null }]);
  });

  it('3. STALENESS FALLBACK: live traffic with genuinely stale data writes using the real claim token, not a boolean', async () => {
    const { calls, stubs } = makeStubs({ stale: true, claimTokenResult: 'token-abc' });
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    expect(calls.logBtcData).toEqual(['token-abc']);
    expect(calls.claimStaleRefreshCalled).toBe(1);
    expect(calls.runPredictionArgs).toEqual([{ persist: true, claimToken: 'token-abc' }]);
    expect(calls.runChallengerArgs).toEqual([{ persist: true, claimToken: 'token-abc' }]);
  });

  it('3b. claim lost (claimStaleRefresh returns null) -- correctly does not write, persist:false, claimToken:null passed through', async () => {
    const { calls, stubs } = makeStubs({ stale: true, claimTokenResult: null });
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    expect(calls.logBtcData).toEqual([]);
    expect(calls.runPredictionArgs).toEqual([{ persist: false, claimToken: null }]);
  });

  it('4. NO DUPLICATES: across 5 consecutive live calls with fresh data, zero writes ever happen', async () => {
    const { calls, stubs } = makeStubs({ stale: false });
    const scope = evalInScope(source, stubs);
    for (let i = 0; i < 5; i++) {
      await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    }
    expect(calls.logBtcData).toEqual([]);
    expect(calls.runPredictionArgs.every((a) => a.persist === false)).toBe(true);
  });

  it('backfillChallengerPredictions now runs BEFORE the claim is resolved, not after -- shrinks the unprotected window per the design fix', async () => {
    const { calls, stubs } = makeStubs({ stale: true });
    const scope = evalInScope(source, stubs);
    await scope.predictAndLog({ DB: {} }, 24, { allowWrite: false });
    expect(calls.backfillChallengerCalledBeforeClaim).toBe(true);
  });

  it('resolution steps still run on every call regardless of allowWrite -- resolving existing rows is not "creating new training data"', async () => {
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

  it('a Challenger failure never breaks the core prediction response, same resilience contract as before', async () => {
    const { stubs } = makeStubs({ stale: false });
    stubs.runChallengerPrediction = async () => { throw new Error('challenger boom'); };
    const scope = evalInScope(source, stubs);
    const result = await scope.predictAndLog({ DB: {} }, 24, { allowWrite: true });
    expect(result.ok).toBe(true);
    expect(result.challenger.ok).toBe(false);
    expect(result.challenger.error).toContain('challenger boom');
  });
});

describe('regression: runPrediction/runLinkPrediction/runEthPrediction/runChallengerPrediction all accept persist AND claimToken', () => {
  it('all four functions default to persist:true, claimToken:null (safe, backward-compatible for any other caller)', () => {
    for (const name of ['runPrediction', 'runLinkPrediction', 'runEthPrediction']) {
      const src = extractFunctions(name);
      expect(src).toMatch(/\{ persist = true, claimToken = null \} = \{\}/);
      expect(src).toMatch(/if \(claimToken === null\) \{/);
    }
    const challengerSrc = extractFunctions('runChallengerPrediction');
    expect(challengerSrc).toMatch(/\{ persist = true, claimToken = null \} = \{\}/);
    expect(challengerSrc).toMatch(/if \(claimToken === null\) \{/);
  });

  it('each function\'s conditional (token-authorized) SQL shape is INSERT ... SELECT ... FROM stale_refresh_claim WHERE coin = ... AND claim_token = ..., not a plain VALUES insert', () => {
    for (const [name, coin] of [['runPrediction', "'BTC'"], ['runLinkPrediction', "'LINK'"], ['runEthPrediction', "'ETH'"]]) {
      const src = extractFunctions(name);
      expect(src).toMatch(/FROM stale_refresh_claim WHERE coin = /);
      expect(src).toContain('AND claim_token = ?');
    }
    const challengerSrc = extractFunctions('runChallengerPrediction');
    expect(challengerSrc).toMatch(/FROM stale_refresh_claim WHERE coin = \? AND claim_token = \?/);
  });

  it('when not persisted, each function still returns prediction_id/id: null rather than throwing on a missing insert result', () => {
    for (const name of ['runPrediction', 'runLinkPrediction', 'runEthPrediction']) {
      const src = extractFunctions(name);
      expect(src).toMatch(/insert \? insert\.meta\.last_row_id : null/);
    }
  });
});

describe('regression: the cron dispatch explicitly passes allowWrite:true, the only place that does', () => {
  it('runCoinHorizonChain passes { allowWrite: true } to whichever predict function it calls', () => {
    // Renamed from the old inline `predictThenSelect` closure to the
    // top-level runCoinHorizonChain as part of the 2026-09-02 batched-
    // sequential redesign (fixing the LINK/ETH selection starvation) --
    // the allowWrite:true contract itself is unchanged.
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf('async function runCoinHorizonChain');
    expect(idx).toBeGreaterThan(-1);
    const nearby = src.slice(idx, idx + 600);
    expect(nearby).toContain('predictFn(env, horizon, { allowWrite: true })');
  });

  it('none of the three live routes (/predict, /link-predict, /eth-predict) pass allowWrite at all -- they rely on the safe read-only default', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toMatch(/const result = await predictAndLog\(env, horizon\);/);
    expect(src).toMatch(/const result = await linkPredictAndLog\(env, horizon\);/);
    expect(src).toMatch(/const result = await ethPredictAndLog\(env, horizon\);/);
  });
});
