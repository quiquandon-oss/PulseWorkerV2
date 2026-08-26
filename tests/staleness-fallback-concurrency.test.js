import { describe, it, expect } from 'vitest';
import { extractFunctions, evalInScope } from './helpers/extract.js';

// This suite uses the REAL claimStaleRefresh and isRecentDataStale --
// unlike read-only-ingestion.test.js (which stubs claimStaleRefresh to
// isolate predictAndLog's own orchestration logic), proving atomicity
// requires exercising the actual reservation code, not a stand-in for it.
// Everything else (backfill*, logXData, runXPrediction,
// runChallengerPrediction) is still stubbed -- this suite is about the
// concurrency behavior of the write-gating decision, not a
// re-verification of k-NN correctness.

function makeRealisticFakeDb({ staleTables = ['btc_data', 'link_data', 'eth_data'], networkDelayMs = 4 } = {}) {
  // Simulates the two tables the real fix actually touches:
  //  - btc_data/link_data/eth_data: only ever read here (MAX(ts)), fixed
  //    to always report a stale (7h old) timestamp for whichever tables
  //    are listed in staleTables, so isRecentDataStale reliably returns
  //    true for every concurrent caller -- proving the claim step, not
  //    the staleness detection, is what prevents duplicate writes.
  //  - stale_refresh_claim: real, mutable, per-coin state -- this is the
  //    one table whose correctness under concurrency is actually being
  //    tested. Each fake statement is written to mutate this state
  //    SYNCHRONOUSLY (the async wait happens before the mutation, not
  //    split across it), mirroring the atomicity a single real SQL
  //    statement has even under concurrent callers -- exactly the
  //    property this test needs to be a fair simulation of D1 rather
  //    than an accidental testing artifact.
  const claimState = new Map(); // coin -> claimed_ts
  const dataWrites = { BTC: 0, LINK: 0, ETH: 0 };
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const sevenHoursAgo = Date.now() - 7 * 60 * 60 * 1000;

  const db = {
    prepare(sql) {
      const handle = {
        bind(...args) {
          return makeHandle(args);
        },
        first: async () => makeHandle([]).first(),
        run: async () => makeHandle([]).run(),
        all: async () => makeHandle([]).all(),
      };
      function makeHandle(args) {
        return {
          first: async () => {
            await wait(networkDelayMs);
            if (/FROM (btc_data|link_data|eth_data)/.test(sql)) {
              const table = sql.match(/FROM (\w+)/)[1];
              return { latest: staleTables.includes(table) ? sevenHoursAgo : Date.now() };
            }
            return null;
          },
          run: async () => {
            await wait(networkDelayMs);
            if (sql.includes('INSERT INTO stale_refresh_claim')) {
              const [coin] = args;
              if (!claimState.has(coin)) claimState.set(coin, 0); // ON CONFLICT DO NOTHING semantics
            }
            return { meta: { last_row_id: 1 } };
          },
          all: async () => {
            await wait(networkDelayMs);
            if (sql.includes('UPDATE stale_refresh_claim')) {
              const [nowTs, coin, cutoff] = args;
              // The atomic check-and-mutate: read current state and
              // decide+write in one synchronous stretch, no await in
              // between -- this is what makes it a fair stand-in for a
              // single real SQL statement's atomicity.
              const current = claimState.get(coin) ?? 0;
              if (current <= cutoff) {
                claimState.set(coin, nowTs);
                return { results: [{ coin }] };
              }
              return { results: [] };
            }
            return { results: [] };
          },
        };
      }
      return handle;
    },
  };

  return { db, dataWrites, claimState };
}

describe('FIX VERIFICATION: staleness fallback is now atomic across concurrent requests', () => {
  let source;
  const source_ = extractFunctions('predictAndLog', 'linkPredictAndLog', 'ethPredictAndLog', 'isRecentDataStale', 'claimStaleRefresh');
  source = source_;

  function makeScope() {
    const state = { logBtcData: 0, logLinkData: 0, logEthData: 0 };
    const stubs = {
      backfillPredictions: async () => 0,
      backfillGeminiBiasShort: async () => 0,
      backfillLinkPredictions: async () => 0,
      backfillEthPredictions: async () => 0,
      backfillChallengerPredictions: async () => 0,
      logBtcData: async () => { state.logBtcData++; },
      logLinkData: async () => { state.logLinkData++; },
      logEthData: async () => { state.logEthData++; },
      runPrediction: async (env, h, opts) => ({ ok: true, status: 'ok', btc_price_now: 100, persisted: opts?.persist }),
      runLinkPrediction: async (env, h, opts) => ({ ok: true, status: 'ok', link_price_now: 10, persisted: opts?.persist }),
      runEthPrediction: async (env, h, opts) => ({ ok: true, status: 'ok', eth_price_now: 3000, persisted: opts?.persist }),
      runChallengerPrediction: async (env, args, opts) => ({ ok: true, status: 'ok', persisted: opts?.persist }),
    };
    const scope = evalInScope(source, stubs);
    return { scope, state };
  }

  it('BTC: 2 concurrent live requests during a genuine stale window -> exactly 1 write', async () => {
    const { db } = makeRealisticFakeDb();
    const { scope, state } = makeScope();
    await Promise.all([
      scope.predictAndLog({ DB: db }, 24, { allowWrite: false }),
      scope.predictAndLog({ DB: db }, 24, { allowWrite: false }),
    ]);
    expect(state.logBtcData).toBe(1);
  });

  it('BTC: 5 concurrent live requests during a genuine stale window -> exactly 1 write', async () => {
    const { db } = makeRealisticFakeDb();
    const { scope, state } = makeScope();
    await Promise.all(Array.from({ length: 5 }, () => scope.predictAndLog({ DB: db }, 24, { allowWrite: false })));
    expect(state.logBtcData).toBe(1);
  });

  it('LINK: 2 concurrent live requests during a genuine stale window -> exactly 1 write', async () => {
    const { db } = makeRealisticFakeDb();
    const { scope, state } = makeScope();
    await Promise.all([
      scope.linkPredictAndLog({ DB: db }, 24, { allowWrite: false }),
      scope.linkPredictAndLog({ DB: db }, 24, { allowWrite: false }),
    ]);
    expect(state.logLinkData).toBe(1);
  });

  it('LINK: 5 concurrent live requests during a genuine stale window -> exactly 1 write', async () => {
    const { db } = makeRealisticFakeDb();
    const { scope, state } = makeScope();
    await Promise.all(Array.from({ length: 5 }, () => scope.linkPredictAndLog({ DB: db }, 24, { allowWrite: false })));
    expect(state.logLinkData).toBe(1);
  });

  it('ETH: 2 concurrent live requests during a genuine stale window -> exactly 1 write', async () => {
    const { db } = makeRealisticFakeDb();
    const { scope, state } = makeScope();
    await Promise.all([
      scope.ethPredictAndLog({ DB: db }, 24, { allowWrite: false }),
      scope.ethPredictAndLog({ DB: db }, 24, { allowWrite: false }),
    ]);
    expect(state.logEthData).toBe(1);
  });

  it('ETH: 5 concurrent live requests during a genuine stale window -> exactly 1 write', async () => {
    const { db } = makeRealisticFakeDb();
    const { scope, state } = makeScope();
    await Promise.all(Array.from({ length: 5 }, () => scope.ethPredictAndLog({ DB: db }, 24, { allowWrite: false })));
    expect(state.logEthData).toBe(1);
  });

  it('ALL THREE COINS simultaneously, 5 concurrent requests each (15 total in flight) -> exactly 1 write per coin, no cross-coin interference', async () => {
    const { db } = makeRealisticFakeDb();
    const { scope, state } = makeScope();
    await Promise.all([
      ...Array.from({ length: 5 }, () => scope.predictAndLog({ DB: db }, 24, { allowWrite: false })),
      ...Array.from({ length: 5 }, () => scope.linkPredictAndLog({ DB: db }, 24, { allowWrite: false })),
      ...Array.from({ length: 5 }, () => scope.ethPredictAndLog({ DB: db }, 24, { allowWrite: false })),
    ]);
    expect(state.logBtcData).toBe(1);
    expect(state.logLinkData).toBe(1);
    expect(state.logEthData).toBe(1);
  });

  it('a SECOND genuinely later stale window (>60s claim window later) is allowed to claim again -- the fix prevents duplicate writes within one race, not all future fallback writes forever', async () => {
    const { db, claimState } = makeRealisticFakeDb();
    const { scope, state } = makeScope();
    await scope.predictAndLog({ DB: db }, 24, { allowWrite: false });
    expect(state.logBtcData).toBe(1);
    // Simulate real time passing well beyond the 60s claim window by
    // directly rolling back the claim's own recorded timestamp -- the
    // claim table only ever needs to block callers arriving within the
    // SAME race, not suppress every future legitimate fallback.
    claimState.set('BTC', Date.now() - 61 * 1000);
    await scope.predictAndLog({ DB: db }, 24, { allowWrite: false });
    expect(state.logBtcData).toBe(2);
  });

  it('cron (allowWrite:true) is completely unaffected -- never calls claimStaleRefresh or isRecentDataStale at all, confirmed via a DB that would throw if queried for staleness', async () => {
    const throwingDb = {
      prepare(sql) {
        if (/btc_data|stale_refresh_claim/.test(sql)) {
          throw new Error('cron path must never query staleness or the claim table -- allowWrite:true should short-circuit before either');
        }
        return { bind: () => ({ run: async () => ({ meta: { last_row_id: 1 } }), all: async () => ({ results: [] }), first: async () => null }) };
      },
    };
    const { scope, state } = makeScope();
    // logBtcData itself is stubbed (doesn't touch the DB), so only
    // predictAndLog's OWN queries (staleness/claim) would trip the throw.
    await expect(scope.predictAndLog({ DB: throwingDb }, 24, { allowWrite: true })).resolves.toMatchObject({ ok: true });
    expect(state.logBtcData).toBe(1);
  });
});
