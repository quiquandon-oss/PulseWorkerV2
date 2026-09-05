// Tests for tail-logger/index.js -- the Tail Worker that forwards
// LINK_* diagnostic checkpoint logs (PR #34) into D1's
// link_diagnostic_log table. This is a genuinely separate script from
// worker.js, imported directly rather than via the extract/eval sandbox
// used elsewhere in this repo.
import { describe, it, expect } from 'vitest';
import { extractLinkCheckpoint, processEvents } from '../tail-logger/index.js';

describe('extractLinkCheckpoint — filters to LINK_* checkpoints only', () => {
  it('extracts a valid LINK checkpoint log', () => {
    const log = { message: [JSON.stringify({ evt: 'LINK_CORE_START', coin: 'LINK', horizon: 24, elapsed_ms: 0 })], level: 'log', timestamp: 123 };
    const result = extractLinkCheckpoint(log);
    expect(result).toEqual({ evt: 'LINK_CORE_START', coin: 'LINK', horizon: 24, elapsed_ms: 0 });
  });

  it('ignores non-LINK console output entirely (e.g. BTC/ETH chain logs, batch_complete lines)', () => {
    const notLink = [
      { message: [JSON.stringify({ evt: 'chain_start', coin: 'BTC', horizon: 24 })] },
      { message: [JSON.stringify({ evt: 'batch_complete', coin: 'ETH' })] },
      { message: ['some unrelated plain string log'] },
    ];
    for (const log of notLink) {
      expect(extractLinkCheckpoint(log)).toBeNull();
    }
  });

  it('returns null for malformed/non-JSON message content without throwing', () => {
    expect(extractLinkCheckpoint({ message: ['LINK_ something not valid json {'] })).toBeNull();
    expect(extractLinkCheckpoint({ message: [] })).toBeNull();
    expect(extractLinkCheckpoint({})).toBeNull();
    expect(extractLinkCheckpoint(null)).toBeNull();
  });

  it('returns null for a JSON object that happens to mention LINK_ in a string field but has no evt field', () => {
    const log = { message: [JSON.stringify({ note: 'this mentions LINK_CORE_START but is not a checkpoint object' })] };
    expect(extractLinkCheckpoint(log)).toBeNull();
  });
});

describe('processEvents — end-to-end batching into D1', () => {
  function makeFakeEnv() {
    const inserted = [];
    const env = {
      DB: {
        prepare(sql) {
          return { bind: (...args) => ({ __sql: sql, __args: args }) };
        },
        batch: async (statements) => {
          for (const s of statements) inserted.push({ sql: s.__sql, args: s.__args });
          return statements.map(() => ({ success: true }));
        },
      },
    };
    return { env, inserted };
  }

  it('forwards only the LINK_* logs from a mixed batch, correctly associated with their own event', async () => {
    const { env, inserted } = makeFakeEnv();
    const events = [
      {
        scriptName: 'pulseworker-v2', outcome: 'ok', eventTimestamp: 1000,
        logs: [
          { message: [JSON.stringify({ evt: 'chain_start', coin: 'BTC', horizon: 24 })] },
          { message: [JSON.stringify({ evt: 'LINK_CORE_START', coin: 'LINK', horizon: 12, elapsed_ms: 0 })] },
          { message: [JSON.stringify({ evt: 'LINK_DATA_READ_DONE', coin: 'LINK', horizon: 12, elapsed_ms: 15 })] },
        ],
      },
    ];
    await processEvents(events, env);
    expect(inserted).toHaveLength(2);
    expect(inserted[0].sql).toContain('INSERT INTO link_diagnostic_log');
    // (received_ts, event_ts, coin, horizon, checkpoint, elapsed_ms, outcome, script_name)
    expect(inserted[0].args[2]).toBe('LINK');
    expect(inserted[0].args[3]).toBe(12);
    expect(inserted[0].args[4]).toBe('LINK_CORE_START');
    expect(inserted[0].args[1]).toBe(1000); // event_ts from the tail event
    expect(inserted[1].args[4]).toBe('LINK_DATA_READ_DONE');
  });

  it('writes nothing and does not throw when a batch has no LINK_* logs at all', async () => {
    const { env, inserted } = makeFakeEnv();
    const events = [{ scriptName: 'pulseworker-v2', outcome: 'ok', eventTimestamp: 2000, logs: [{ message: ['unrelated'] }] }];
    await expect(processEvents(events, env)).resolves.not.toThrow();
    expect(inserted).toHaveLength(0);
  });

  it('handles multiple events (multiple invocations) in one tail batch independently', async () => {
    const { env, inserted } = makeFakeEnv();
    const events = [
      { scriptName: 'pulseworker-v2', outcome: 'ok', eventTimestamp: 1000, logs: [{ message: [JSON.stringify({ evt: 'LINK_CORE_START', coin: 'LINK', horizon: 24 })] }] },
      { scriptName: 'pulseworker-v2', outcome: 'ok', eventTimestamp: 4000, logs: [{ message: [JSON.stringify({ evt: 'LINK_CORE_START', coin: 'LINK', horizon: 12 })] }] },
    ];
    await processEvents(events, env);
    expect(inserted).toHaveLength(2);
    expect(inserted[0].args[1]).toBe(1000);
    expect(inserted[1].args[1]).toBe(4000);
  });

  it('a single malformed log line does not prevent other valid lines in the same event from being forwarded', async () => {
    const { env, inserted } = makeFakeEnv();
    const events = [{
      scriptName: 'pulseworker-v2', outcome: 'ok', eventTimestamp: 1000,
      logs: [
        { message: ['LINK_ malformed { not json'] },
        { message: [JSON.stringify({ evt: 'LINK_CORE_START', coin: 'LINK', horizon: 24 })] },
      ],
    }];
    await processEvents(events, env);
    expect(inserted).toHaveLength(1);
    expect(inserted[0].args[4]).toBe('LINK_CORE_START');
  });

  it('does not crash if env.DB.batch itself rejects (Tail Worker has no caller to propagate to)', async () => {
    const env = { DB: { prepare: () => ({ bind: () => ({}) }), batch: async () => { throw new Error('simulated D1 failure'); } } };
    const events = [{ scriptName: 'pulseworker-v2', outcome: 'ok', eventTimestamp: 1000, logs: [{ message: [JSON.stringify({ evt: 'LINK_CORE_START', coin: 'LINK', horizon: 24 })] }] }];
    await expect(processEvents(events, env)).resolves.not.toThrow();
  });
});
