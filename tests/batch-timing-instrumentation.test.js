// Regression tests for the diagnostic-only batch timing instrumentation
// added to runCoinBatch (2026-09-02), per the forensic investigation into
// whether cumulative resource exhaustion across sequential batches
// explains the BTC=complete / LINK=partial / ETH=none pattern.
//
// Scope is deliberately narrow: this instrumentation only adds a single
// console.log per batch, capturing Promise.allSettled's own (previously
// discarded) return value. It changes nothing about what runs, in what
// order, or how failures are handled. These tests verify the logging
// itself, not prediction/selection/Challenger behavior, which is already
// covered exhaustively elsewhere (challenger-cron-stall-fix.test.js,
// link-eth-selection-starvation-fix.test.js, etc.) and is untouched here.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { extractFunctions, evalInScope } from './helpers/extract.js';

describe('runCoinBatch — diagnostic timing instrumentation', () => {
  let logSpy;
  beforeEach(() => { logSpy = vi.spyOn(console, 'log').mockImplementation(() => {}); });
  afterEach(() => { logSpy.mockRestore(); });

  function buildScope({ chainImpl } = {}) {
    const source = extractFunctions('runCoinBatch');
    // runCoinBatch calls the real runCoinHorizonChain by name -- supplying
    // a stub of the same name via evalInScope's sandbox is how this test
    // isolates the instrumentation from the real prediction/selection
    // machinery (already covered elsewhere) without touching that code.
    async function runCoinHorizonChain(env, predictFn, coin, horizon) {
      if (chainImpl) return chainImpl(coin, horizon);
      return undefined; // matches the real function's implicit undefined return
    }
    return evalInScope(source, { runCoinHorizonChain });
  }

  it('1. emits exactly one log line per batch call, regardless of how many chains are in it', async () => {
    const scope = buildScope();
    await scope.runCoinBatch({}, [{ predictFn: null, coin: 'BTC', horizon: 24 }, { predictFn: null, coin: 'BTC', horizon: 12 }]);
    const batchLogs = logSpy.mock.calls.filter(([arg]) => { try { return JSON.parse(arg).evt === 'batch_complete'; } catch { return false; } });
    expect(batchLogs).toHaveLength(1);
  });

  it('2. logged fields are correct: coin, batch_start, batch_end, elapsed_ms, and one chains[] entry per input chain with horizon', async () => {
    const scope = buildScope();
    await scope.runCoinBatch({}, [{ predictFn: null, coin: 'LINK', horizon: 24 }, { predictFn: null, coin: 'LINK', horizon: 12 }]);
    const logged = JSON.parse(logSpy.mock.calls.find(([arg]) => JSON.parse(arg).evt === 'batch_complete')[0]);
    expect(logged.coin).toBe('LINK');
    expect(typeof logged.batch_start).toBe('number');
    expect(typeof logged.batch_end).toBe('number');
    expect(typeof logged.elapsed_ms).toBe('number');
    expect(logged.chains).toHaveLength(2);
    expect(logged.chains.map(c => c.horizon).sort()).toEqual([12, 24]);
  });

  it('3. elapsed_ms is non-negative and consistent with batch_start/batch_end', async () => {
    const scope = buildScope();
    await scope.runCoinBatch({}, [{ predictFn: null, coin: 'ETH', horizon: 24 }]);
    const logged = JSON.parse(logSpy.mock.calls.find(([arg]) => JSON.parse(arg).evt === 'batch_complete')[0]);
    expect(logged.elapsed_ms).toBeGreaterThanOrEqual(0);
    expect(logged.batch_end).toBeGreaterThanOrEqual(logged.batch_start);
    expect(logged.elapsed_ms).toBe(logged.batch_end - logged.batch_start);
  });

  it('4. a rejected chain is represented correctly: status="rejected" and a non-null error string, without affecting the other chain\'s reported status', async () => {
    const scope = buildScope({
      chainImpl: async (coin, horizon) => { if (horizon === 24) throw new Error('simulated chain rejection'); },
    });
    await scope.runCoinBatch({}, [{ predictFn: null, coin: 'BTC', horizon: 24 }, { predictFn: null, coin: 'BTC', horizon: 12 }]);
    const logged = JSON.parse(logSpy.mock.calls.find(([arg]) => JSON.parse(arg).evt === 'batch_complete')[0]);
    const h24 = logged.chains.find(c => c.horizon === 24);
    const h12 = logged.chains.find(c => c.horizon === 12);
    expect(h24.status).toBe('rejected');
    expect(h24.error).toContain('simulated chain rejection');
    expect(h12.status).toBe('fulfilled');
    expect(h12.error).toBeNull();
  });

  it('5. existing batch behavior is unchanged: both chains are still started, both still run to completion (settlement), and a rejection in one does not stop or skip the other', async () => {
    const started = [];
    const scope = buildScope({
      chainImpl: async (coin, horizon) => {
        started.push(horizon);
        if (horizon === 24) throw new Error('simulated');
      },
    });
    await scope.runCoinBatch({}, [{ predictFn: null, coin: 'BTC', horizon: 24 }, { predictFn: null, coin: 'BTC', horizon: 12 }]);
    // Both chains were invoked (Promise.allSettled semantics preserved --
    // a rejection in one does not prevent or cancel the other).
    expect(started.sort()).toEqual([12, 24]);
  });

  it('structural: Promise.allSettled is still the mechanism used, and its call is unchanged in shape (called once, with the mapped tasks array)', () => {
    const src = extractFunctions('runCoinBatch');
    expect(src).toContain('await Promise.allSettled(tasks)');
    expect(src.match(/Promise\.allSettled/g)).toHaveLength(1);
  });
});
