import { describe, it, expect, beforeAll, afterEach } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

// Covers the shared Gemini quota gate introduced to fix the root-cause
// finding that /run-analysis and /run-link-analysis had NO application-level
// budget at all, and could silently exhaust the provider-level quota that
// the (correctly-budgeted) market-intelligence investigation also draws
// from. See the root-cause audit and the shared-quota design comment above
// getGeminiInvestigationCounts in worker.js.

// ---------------------------------------------------------------------
// A faithful in-memory fake of the ONE table this whole mechanism relies
// on for atomicity: gemini_quota_ledger. Unlike the simpler canned-response
// fake in gemini-planning.test.js, this one actually implements the
// check-and-increment semantics of `UPDATE ... WHERE reserved < cap
// RETURNING reserved` against a real Map, because the entire point of
// these tests is to prove that semantics is race-safe. It works as a
// concurrency test because none of run()/all()/first() below contain an
// internal `await` -- each one's synchronous body executes as a single,
// uninterruptible microtask step, exactly mirroring how a single SQL
// statement is atomic against D1's actual single-writer storage layer.
// Two reservations racing for the same row can interleave at the `await`
// boundaries BETWEEN statements, but never in the middle of one.
// ---------------------------------------------------------------------
function makeLedgerDb() {
  const buckets = new Map(); // bucket_key -> { reserved, cap }
  const providerCalls = [];

  function run(sql, args) {
    if (/^INSERT INTO gemini_quota_ledger/i.test(sql)) {
      // No-op here on purpose: this fake relies on tests calling seedBucket()
      // directly rather than modeling ON CONFLICT DO NOTHING generically --
      // see the comment on seedBucket below for why.
      return { meta: {} };
    }
    if (/^UPDATE gemini_quota_ledger SET reserved = MAX\(0, reserved - 1\)/i.test(sql)) {
      const [, bucketKey] = args;
      const row = buckets.get(bucketKey);
      if (row) row.reserved = Math.max(0, row.reserved - 1);
      return { meta: {} };
    }
    if (/^INSERT INTO gemini_provider_calls/i.test(sql)) {
      providerCalls.push(args);
      return { meta: {} };
    }
    return { meta: {} };
  }

  function all(sql, args) {
    if (/^UPDATE gemini_quota_ledger SET reserved = reserved \+ 1/i.test(sql)) {
      const [, bucketKey] = args;
      const row = buckets.get(bucketKey);
      if (!row) return { results: [] };
      if (row.reserved < row.cap) {
        row.reserved += 1;
        return { results: [{ reserved: row.reserved }] };
      }
      return { results: [] };
    }
    return { results: [] };
  }

  function first(sql, args) {
    if (/^SELECT reserved, cap FROM gemini_quota_ledger/i.test(sql)) {
      const [bucketKey] = args;
      const row = buckets.get(bucketKey);
      return row ? { reserved: row.reserved, cap: row.cap } : null;
    }
    return null;
  }

  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async run() { return run(sql, args); },
            async all() { return all(sql, args); },
            async first() { return first(sql, args); },
          };
        },
      };
    },
    // Real D1 runs a batch as one implicit transaction. This fake only
    // needs to handle the idempotent bucket-creation INSERTs
    // reserveGeminiQuotaSlot sends through batch() -- it seeds the Map
    // directly here (ON CONFLICT DO NOTHING semantics: only seed if
    // absent), since the two statements in that batch always carry the
    // bucket's initial cap in a predictable argument position.
    async batch(stmts) {
      // stmts here are the two INSERT-OR-IGNORE bind() objects from
      // reserveGeminiQuotaSlot: (bucket_key, consumer, cap, updated_ts).
      // This fake doesn't model ON CONFLICT DO NOTHING generically -- tests
      // seed buckets directly via seedBucket() instead (see below).
      return Promise.all(stmts.map(s => s.run()));
    },
  };

  return { db, buckets, providerCalls };
}

// The fake above can't special-case bucket creation generically (it never
// sees the raw bound args for a batch()'d statement's INSERT from the
// outside). Instead of over-engineering a full SQL layer, seed buckets
// directly via this helper before exercising reserveGeminiQuotaSlot/
// peekGeminiQuotaRemaining in tests -- equivalent to the first call to
// reserveGeminiQuotaSlot having already run the idempotent bucket-creation
// step in production.
function seedBucket(buckets, bucketKey, cap, reserved = 0) {
  buckets.set(bucketKey, { reserved, cap });
}

describe('Shared Gemini quota gate — pure bucket-key helpers', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('utcDayBucket', 'utcHourBucket', 'buildQuotaBucketKeys'));
  });

  it('derives a stable UTC calendar-day key from a timestamp', () => {
    // 2026-08-21T09:00:54.816Z, one of the three real rate-limited attempts
    expect(scope.utcDayBucket(1787302854816)).toBe('2026-08-21');
  });

  it('derives a stable UTC calendar-hour key from a timestamp', () => {
    expect(scope.utcHourBucket(1787302854816)).toBe('2026-08-21T09');
  });

  it('two timestamps in the same UTC hour produce the same hour bucket', () => {
    const a = Date.parse('2026-08-21T09:00:01Z').valueOf();
    const b = Date.parse('2026-08-21T09:59:59Z').valueOf();
    expect(scope.utcHourBucket(a)).toBe(scope.utcHourBucket(b));
  });

  it('a timestamp one second into the next UTC hour produces a different hour bucket', () => {
    const a = Date.parse('2026-08-21T09:59:59Z').valueOf();
    const b = Date.parse('2026-08-21T10:00:00Z').valueOf();
    expect(scope.utcHourBucket(a)).not.toBe(scope.utcHourBucket(b));
  });

  it('a timestamp one second into the next UTC day produces a different day bucket', () => {
    const a = Date.parse('2026-08-21T23:59:59Z').valueOf();
    const b = Date.parse('2026-08-22T00:00:00Z').valueOf();
    expect(scope.utcDayBucket(a)).not.toBe(scope.utcDayBucket(b));
  });

  it('namespaces bucket keys by consumer, so lanes never collide in the shared table', () => {
    const ts = Date.parse('2026-08-21T09:00:00Z').valueOf();
    const inv = scope.buildQuotaBucketKeys('investigation', ts);
    const btc = scope.buildQuotaBucketKeys('btc_narrative_cron', ts);
    expect(inv.dayKey).not.toBe(btc.dayKey);
    expect(inv.hourKey).not.toBe(btc.hourKey);
    expect(inv.dayKey).toBe('investigation:day:2026-08-21');
    expect(inv.hourKey).toBe('investigation:hour:2026-08-21T09');
  });
});

describe('Shared Gemini quota gate — reserveGeminiQuotaSlot atomicity', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('reserveGeminiQuotaSlot', 'buildQuotaBucketKeys', 'utcDayBucket', 'utcHourBucket'));
  });

  const ts = Date.parse('2026-08-21T09:00:00Z').valueOf();

  it('admits a reservation when under both the day and hour cap', async () => {
    const { db, buckets } = makeLedgerDb();
    const { dayKey, hourKey } = scope.buildQuotaBucketKeys('test', ts);
    seedBucket(buckets, dayKey, 5);
    seedBucket(buckets, hourKey, 5);
    const result = await scope.reserveGeminiQuotaSlot({ DB: db }, 'test', { day: 5, hour: 5 }, ts);
    expect(result).toEqual({ admitted: true, reason: null });
    expect(buckets.get(dayKey).reserved).toBe(1);
    expect(buckets.get(hourKey).reserved).toBe(1);
  });

  it('rejects once the day cap is reached, even if the hour cap has room', async () => {
    const { db, buckets } = makeLedgerDb();
    const { dayKey, hourKey } = scope.buildQuotaBucketKeys('test', ts);
    seedBucket(buckets, dayKey, 1, /* reserved */ 1); // already at cap
    seedBucket(buckets, hourKey, 5, 0);
    const result = await scope.reserveGeminiQuotaSlot({ DB: db }, 'test', { day: 1, hour: 5 }, ts);
    expect(result).toEqual({ admitted: false, reason: 'daily_limit_reached' });
    expect(buckets.get(hourKey).reserved).toBe(0); // hour was never touched -- rejected before reaching it
  });

  it('rejects once the hour cap is reached, and COMPENSATES the day reservation it just made', async () => {
    const { db, buckets } = makeLedgerDb();
    const { dayKey, hourKey } = scope.buildQuotaBucketKeys('test', ts);
    seedBucket(buckets, dayKey, 5, 0); // plenty of daily room
    seedBucket(buckets, hourKey, 1, 1); // already at the hourly cap
    const result = await scope.reserveGeminiQuotaSlot({ DB: db }, 'test', { day: 5, hour: 1 }, ts);
    expect(result).toEqual({ admitted: false, reason: 'hourly_limit_reached' });
    // The day bucket was incremented (0 -> 1) then compensated back to 0 --
    // this is the "never leak a reserved-but-unused slot" guarantee.
    expect(buckets.get(dayKey).reserved).toBe(0);
  });

  it('counts a reservation the moment it is granted -- regardless of what the Gemini call that follows does', async () => {
    // This test exists to make explicit the root-cause audit's Phase 2
    // requirement: "a provider request happened whether it returned 200 or
    // 429". reserveGeminiQuotaSlot has no knowledge of what happens after
    // it returns admitted:true -- the accounting is already done by then,
    // which is exactly the point: a 429 downstream can never "give back"
    // the slot it consumed.
    const { db, buckets } = makeLedgerDb();
    const { dayKey, hourKey } = scope.buildQuotaBucketKeys('test', ts);
    seedBucket(buckets, dayKey, 1, 0);
    seedBucket(buckets, hourKey, 1, 0);
    const result = await scope.reserveGeminiQuotaSlot({ DB: db }, 'test', { day: 1, hour: 1 }, ts);
    expect(result.admitted).toBe(true);
    // Simulate the caller's Gemini call now failing with a 429 -- nothing
    // about the ledger changes as a result; the slot stays spent.
    expect(buckets.get(dayKey).reserved).toBe(1);
    expect(buckets.get(hourKey).reserved).toBe(1);
  });

  it('two callers racing for the LAST slot: exactly one is admitted, never both, never zero', async () => {
    const { db, buckets } = makeLedgerDb();
    const { dayKey, hourKey } = scope.buildQuotaBucketKeys('test', ts);
    seedBucket(buckets, dayKey, 1, 0); // exactly one slot available
    seedBucket(buckets, hourKey, 1, 0);

    const [a, b] = await Promise.all([
      scope.reserveGeminiQuotaSlot({ DB: db }, 'test', { day: 1, hour: 1 }, ts),
      scope.reserveGeminiQuotaSlot({ DB: db }, 'test', { day: 1, hour: 1 }, ts),
    ]);

    const admittedCount = [a, b].filter(r => r.admitted).length;
    expect(admittedCount).toBe(1); // never two, never zero
    expect(buckets.get(dayKey).reserved).toBe(1); // final ledger state matches: exactly one slot consumed
    expect(buckets.get(hourKey).reserved).toBe(1);
  });

  it('five callers racing for THREE slots: exactly three admitted, two rejected, ledger ends at exactly 3', async () => {
    const { db, buckets } = makeLedgerDb();
    const { dayKey, hourKey } = scope.buildQuotaBucketKeys('test', ts);
    seedBucket(buckets, dayKey, 3, 0);
    seedBucket(buckets, hourKey, 10, 0); // hour cap not the binding constraint here

    const results = await Promise.all(
      Array.from({ length: 5 }, () => scope.reserveGeminiQuotaSlot({ DB: db }, 'test', { day: 3, hour: 10 }, ts))
    );

    expect(results.filter(r => r.admitted).length).toBe(3);
    expect(results.filter(r => !r.admitted).length).toBe(2);
    expect(buckets.get(dayKey).reserved).toBe(3); // never overshoots the cap under concurrency
  });

  it('a new UTC day resets the day bucket independently of the hour bucket', async () => {
    const { db, buckets } = makeLedgerDb();
    const day1 = Date.parse('2026-08-21T23:00:00Z').valueOf();
    const day2 = Date.parse('2026-08-22T00:00:00Z').valueOf(); // next day, same-ish hour-of-day
    const keys1 = scope.buildQuotaBucketKeys('test', day1);
    const keys2 = scope.buildQuotaBucketKeys('test', day2);
    seedBucket(buckets, keys1.dayKey, 1, 1); // day1's bucket already exhausted
    seedBucket(buckets, keys1.hourKey, 1, 1);
    seedBucket(buckets, keys2.dayKey, 1, 0); // day2's bucket is fresh
    seedBucket(buckets, keys2.hourKey, 1, 0);

    const resultDay1 = await scope.reserveGeminiQuotaSlot({ DB: db }, 'test', { day: 1, hour: 1 }, day1);
    const resultDay2 = await scope.reserveGeminiQuotaSlot({ DB: db }, 'test', { day: 1, hour: 1 }, day2);
    expect(resultDay1.admitted).toBe(false);
    expect(resultDay2.admitted).toBe(true); // the next day's bucket is unaffected by the previous day's exhaustion
  });
});

describe('Shared Gemini quota gate — peekGeminiQuotaRemaining (non-mutating)', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('peekGeminiQuotaRemaining', 'buildQuotaBucketKeys', 'utcDayBucket', 'utcHourBucket'));
  });

  const ts = Date.parse('2026-08-21T09:00:00Z').valueOf();

  it('reads the full configured cap when no bucket row exists yet (never crashes, never reads as zero)', async () => {
    const { db } = makeLedgerDb();
    const remaining = await scope.peekGeminiQuotaRemaining({ DB: db }, 'investigation', { day: 1, hour: 1 }, ts);
    expect(remaining).toBe(1);
  });

  it('reflects the smaller of day/hour remaining, and does not itself reserve anything', async () => {
    const { db, buckets } = makeLedgerDb();
    const { dayKey, hourKey } = scope.buildQuotaBucketKeys('investigation', ts);
    seedBucket(buckets, dayKey, 5, 4); // 1 remaining today
    seedBucket(buckets, hourKey, 5, 0); // 5 remaining this hour
    const remaining = await scope.peekGeminiQuotaRemaining({ DB: db }, 'investigation', { day: 5, hour: 5 }, ts);
    expect(remaining).toBe(1); // bound by the day bucket
    expect(buckets.get(dayKey).reserved).toBe(4); // unchanged -- peek never mutates
  });
});

describe('Shared Gemini quota gate — callGeminiGenerateContent error classification', () => {
  let scope;
  let restoreFetch;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('callGeminiGenerateContent', 'extractGroundingMetadata') + '\n' + extractConstants('GEMINI_CALL_TIMEOUT_MS'));
  });
  afterEach(() => { if (restoreFetch) { restoreFetch(); restoreFetch = null; } });

  function mockFetch(impl) {
    const original = global.fetch;
    global.fetch = impl;
    restoreFetch = () => { global.fetch = original; };
  }

  it('classifies a provider 429 as rate_limited, without throwing', async () => {
    mockFetch(async () => ({ ok: false, status: 429, text: async () => 'rate limited body' }));
    const result = await scope.callGeminiGenerateContent({ GEMINI_API_KEY: 'k' }, { model: 'gemini-3.6-flash', prompt: 'p' });
    expect(result.ok).toBe(false);
    expect(result.status).toBe(429);
    expect(result.errorCategory).toBe('rate_limited');
  });

  it('classifies a provider 500 as a generic error, distinct from rate_limited', async () => {
    mockFetch(async () => ({ ok: false, status: 500, text: async () => 'internal error body' }));
    const result = await scope.callGeminiGenerateContent({ GEMINI_API_KEY: 'k' }, { model: 'gemini-3.6-flash', prompt: 'p' });
    expect(result.ok).toBe(false);
    expect(result.status).toBe(500);
    expect(result.errorCategory).toBe('error');
  });

  it('classifies an abort (timeout) distinctly from a network error', async () => {
    mockFetch(async (url, opts) => new Promise((resolve, reject) => {
      opts.signal.addEventListener('abort', () => {
        const err = new Error('The operation was aborted');
        err.name = 'AbortError';
        reject(err);
      });
    }));
    // Rather than waiting out the real 20s GEMINI_CALL_TIMEOUT_MS, build a
    // scope where that constant is substituted with a tiny value so the
    // abort fires almost immediately.
    const quickScope = evalInScope(
      extractFunctions('callGeminiGenerateContent', 'extractGroundingMetadata').replace('GEMINI_CALL_TIMEOUT_MS', '10')
    );
    const result = await quickScope.callGeminiGenerateContent({ GEMINI_API_KEY: 'k' }, { model: 'gemini-3.6-flash', prompt: 'p' });
    expect(result.ok).toBe(false);
    expect(result.errorCategory).toBe('timeout');
  }, 2000);

  it('never calls fetch at all when GEMINI_API_KEY is missing, and reports a plain error', async () => {
    let fetchCalled = false;
    mockFetch(async () => { fetchCalled = true; return { ok: true, status: 200, json: async () => ({}) }; });
    const result = await scope.callGeminiGenerateContent({}, { model: 'gemini-3.6-flash', prompt: 'p' });
    expect(fetchCalled).toBe(false);
    expect(result.ok).toBe(false);
    expect(result.errorCategory).toBe('error');
  });

  it('a genuinely empty response body is classified as malformed_response, not silently treated as success', async () => {
    mockFetch(async () => ({ ok: true, status: 200, json: async () => ({ candidates: [{ content: { parts: [{ text: '' }] } }] }) }));
    const result = await scope.callGeminiGenerateContent({ GEMINI_API_KEY: 'k' }, { model: 'gemini-3.6-flash', prompt: 'p' });
    expect(result.ok).toBe(false);
    expect(result.errorCategory).toBe('malformed_response');
  });

  it('a genuine success returns ok:true with the text and grounding metadata', async () => {
    mockFetch(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: 'hello' }] }, groundingMetadata: { webSearchQueries: ['q'], groundingChunks: [{ web: { uri: 'https://example.com', title: 't' } }] } }] }),
    }));
    const result = await scope.callGeminiGenerateContent({ GEMINI_API_KEY: 'k' }, { model: 'gemini-3.6-flash', prompt: 'p', useGrounding: true });
    expect(result.ok).toBe(true);
    expect(result.text).toBe('hello');
    expect(result.groundingMetadata.searchQueries).toEqual(['q']);
    expect(result.groundingMetadata.groundedSources).toEqual([{ url: 'https://example.com', title: 't' }]);
  });
});

describe('Shared Gemini quota gate — geminiStatusToHttpCode', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(extractFunctions('geminiStatusToHttpCode')); });

  it('maps ok to 200', () => { expect(scope.geminiStatusToHttpCode('ok')).toBe(200); });
  it('maps quota_deferred and rate_limited to 429 -- both mean "no answer was produced by policy/provider limit, not a server bug"', () => {
    expect(scope.geminiStatusToHttpCode('quota_deferred')).toBe(429);
    expect(scope.geminiStatusToHttpCode('rate_limited')).toBe(429);
  });
  it('maps timeout to 504', () => { expect(scope.geminiStatusToHttpCode('timeout')).toBe(504); });
  it('maps held_for_learning_focus to 503 -- distinct from quota_deferred/429, since this is a deliberate temporary hold, not a budget or provider limit', () => {
    expect(scope.geminiStatusToHttpCode('held_for_learning_focus')).toBe(503);
  });
  it('maps malformed_response and error to 502', () => {
    expect(scope.geminiStatusToHttpCode('malformed_response')).toBe(502);
    expect(scope.geminiStatusToHttpCode('error')).toBe(502);
  });
  it('falls back to 500 for an unrecognized status, rather than silently returning 200', () => {
    expect(scope.geminiStatusToHttpCode('something_new')).toBe(500);
  });
});

describe('GEMINI_LEARNING_FOCUS_HOLD — Market Intelligence is structurally untouched', () => {
  // Not a behavioral test of investigateMarketEvent's logic (already
  // covered extensively in gemini-planning.test.js, unchanged by this PR)
  // -- this proves the thing the "freeze non-learning consumers" task
  // explicitly required: investigateMarketEvent contains ZERO references
  // to the hold mechanism at all, so there is nothing in it that could
  // even conditionally skip its own quota reservation or fetch call. The
  // hold is entirely absent from Market Intelligence's code path, not
  // merely configured to evaluate false for it.
  it('investigateMarketEvent source contains no reference to the hold mechanism', () => {
    const src = extractFunctions('investigateMarketEvent');
    expect(src).not.toMatch(/isGeminiConsumerOnHold/);
    expect(src).not.toMatch(/GEMINI_LEARNING_FOCUS_HOLD/);
    expect(src).not.toMatch(/held_for_learning_focus/);
  });

  it('evaluateGeminiTriggers (which calls investigateMarketEvent) also contains no reference to the hold mechanism', () => {
    const src = extractFunctions('evaluateGeminiTriggers');
    expect(src).not.toMatch(/isGeminiConsumerOnHold/);
    expect(src).not.toMatch(/GEMINI_LEARNING_FOCUS_HOLD/);
  });

  it('reserveGeminiQuotaSlot itself is untouched by the hold -- the investigation consumer can still reserve quota exactly as before', async () => {
    const scope = evalInScope(extractFunctions('reserveGeminiQuotaSlot', 'buildQuotaBucketKeys', 'utcDayBucket', 'utcHourBucket'));
    const { db, buckets } = makeLedgerDb();
    const ts = Date.parse('2026-08-24T00:00:00Z').valueOf();
    const { dayKey, hourKey } = scope.buildQuotaBucketKeys('investigation', ts);
    seedBucket(buckets, dayKey, 1, 0);
    seedBucket(buckets, hourKey, 1, 0);
    const result = await scope.reserveGeminiQuotaSlot({ DB: db }, 'investigation', { day: 1, hour: 1 }, ts);
    expect(result.admitted).toBe(true);
  });
});

// ---------------------------------------------------------------------
// runGeminiDailyAnalysis / runLinkGeminiAnalysis: full-function tests
// against a fake D1 that combines the ledger semantics above with plain
// canned-response handling for the ordinary content queries/inserts these
// two functions also make (btc_data/history/link_data reads,
// gemini_daily_analysis/link_gemini_analysis/gemini_provider_calls writes).
// ---------------------------------------------------------------------
function makeNarrativeFakeDb({ quotaAdmitted = true } = {}) {
  const inserts = [];
  function statementFor(sql, args) {
    return {
      async all() {
        if (/UPDATE gemini_quota_ledger SET reserved = reserved \+ 1/i.test(sql)) {
          return quotaAdmitted ? { results: [{ reserved: 1 }] } : { results: [] };
        }
        return { results: [] };
      },
      async first() {
        // No btc_data/history/link_data rows in these tests -- the
        // functions already handle that gracefully (price/technical
        // read as null/'N/A'), which is itself part of what's being
        // exercised here without needing real fixture rows.
        return null;
      },
      async run() {
        const table = /INSERT INTO (\w+)/i.exec(sql)?.[1] || /UPDATE (\w+)/i.exec(sql)?.[1] || 'unknown';
        inserts.push({ table, sql, args });
        return { meta: {} };
      },
    };
  }
  const db = {
    prepare(sql) {
      // Real D1 prepared statements support .first()/.all()/.run()
      // directly (no params) as well as after .bind(...) -- both paths hit
      // statementFor with the eventual bound args.
      return { ...statementFor(sql, []), bind: (...args) => statementFor(sql, args) };
    },
    async batch(stmts) { return Promise.all(stmts.map(s => s.run())); },
  };
  return { db, inserts };
}

function mockFetchGlobal(impl) {
  const original = global.fetch;
  global.fetch = impl;
  return () => { global.fetch = original; };
}

describe('runGeminiDailyAnalysis — shared quota gate + non-throwing contract', () => {
  let scope;
  let holdOffScope; // GEMINI_LEARNING_FOCUS_HOLD forced false -- proves the underlying
                     // quota/network logic these pre-existing tests describe is fully
                     // intact and unchanged, i.e. this really is a clean, reversible
                     // gate rather than a rewrite of the original behavior.
  let restoreFetch;
  beforeAll(() => {
    const fnSrc = extractFunctions(
      'runGeminiDailyAnalysis', 'callGeminiGenerateContent', 'extractGroundingMetadata',
      'reserveGeminiQuotaSlot', 'buildQuotaBucketKeys', 'utcDayBucket', 'utcHourBucket',
      'recordGeminiProviderCall', 'normalizeBias', 'normalizeRisk', 'isGeminiConsumerOnHold'
    );
    const constSrc = extractConstants('GEMINI_CALL_TIMEOUT_MS', 'GEMINI_TRIGGER_CONFIG', 'GEMINI_SHARED_QUOTA_CONFIG', 'ANALYSIS_SECTIONS', 'GEMINI_LEARNING_FOCUS_HOLD');
    scope = evalInScope(fnSrc + '\n\n' + constSrc);
    holdOffScope = evalInScope(fnSrc + '\n\n' + constSrc.replace('GEMINI_LEARNING_FOCUS_HOLD = true', 'GEMINI_LEARNING_FOCUS_HOLD = false'));
  });
  afterEach(() => { if (restoreFetch) { restoreFetch(); restoreFetch = null; } });

  it('regression: while GEMINI_LEARNING_FOCUS_HOLD is active, BTC narrative makes ZERO fetch calls and reserves ZERO quota, for both cron and manual', async () => {
    let fetchCalled = false;
    restoreFetch = mockFetchGlobal(async () => { fetchCalled = true; return { ok: true, status: 200, json: async () => ({}) }; });
    // quotaAdmitted:true on purpose -- proves the hold short-circuits
    // BEFORE the reservation call is ever reached, not merely that a
    // denied reservation happens to look the same from the outside.
    const { db: cronDb, inserts: cronInserts } = makeNarrativeFakeDb({ quotaAdmitted: true });
    const cronResult = await scope.runGeminiDailyAnalysis({ DB: cronDb, GEMINI_API_KEY: 'k' }, 'cron');
    const { db: manualDb, inserts: manualInserts } = makeNarrativeFakeDb({ quotaAdmitted: true });
    const manualResult = await scope.runGeminiDailyAnalysis({ DB: manualDb, GEMINI_API_KEY: 'k' }, 'manual');

    expect(fetchCalled).toBe(false);
    for (const result of [cronResult, manualResult]) {
      expect(result.ok).toBe(false);
      expect(result.status).toBe('held_for_learning_focus');
    }
    // No reservation UPDATE/INSERT against gemini_quota_ledger was ever
    // attempted -- only the honest gemini_provider_calls audit row exists.
    for (const inserts of [cronInserts, manualInserts]) {
      expect(inserts.some(i => i.table === 'gemini_quota_ledger')).toBe(false);
      expect(inserts.filter(i => i.table === 'gemini_provider_calls')).toHaveLength(1);
    }
  });

  it('a held attempt is still honestly logged to gemini_provider_calls with quota_decision:held', async () => {
    const { db, inserts } = makeNarrativeFakeDb({ quotaAdmitted: true });
    await scope.runGeminiDailyAnalysis({ DB: db, GEMINI_API_KEY: 'k' }, 'cron');
    const logged = inserts.find(i => i.table === 'gemini_provider_calls');
    expect(logged).toBeDefined();
    expect(logged.args).toContain('held');
    expect(logged.args).toContain('held_for_learning_focus');
  });

  it('isGeminiConsumerOnHold: every btc_narrative lane is held, investigation is never held', () => {
    expect(scope.isGeminiConsumerOnHold('btc_narrative_cron')).toBe(true);
    expect(scope.isGeminiConsumerOnHold('btc_narrative_manual')).toBe(true);
    expect(scope.isGeminiConsumerOnHold('investigation')).toBe(false);
  });

  it('when the quota is deferred, returns ok:false/status:quota_deferred WITHOUT ever calling fetch, and does not throw (hold off)', async () => {
    let fetchCalled = false;
    restoreFetch = mockFetchGlobal(async () => { fetchCalled = true; return { ok: true, status: 200, json: async () => ({}) }; });
    const { db } = makeNarrativeFakeDb({ quotaAdmitted: false });
    const result = await holdOffScope.runGeminiDailyAnalysis({ DB: db, GEMINI_API_KEY: 'k' }, 'manual');
    expect(fetchCalled).toBe(false);
    expect(result.ok).toBe(false);
    expect(result.status).toBe('quota_deferred');
    expect(result.correlationId).toMatch(/^GA-\d+-BTC-manual$/);
  });

  it('when admitted and Gemini returns 429, returns ok:false/status:rate_limited without throwing (hold off)', async () => {
    restoreFetch = mockFetchGlobal(async () => ({ ok: false, status: 429, text: async () => 'nope' }));
    const { db } = makeNarrativeFakeDb({ quotaAdmitted: true });
    const result = await holdOffScope.runGeminiDailyAnalysis({ DB: db, GEMINI_API_KEY: 'k' }, 'cron');
    expect(result.ok).toBe(false);
    expect(result.status).toBe('rate_limited');
    expect(result.correlationId).toMatch(/^GA-\d+-BTC-cron$/);
  });

  it('happy path: writes gemini_daily_analysis and gemini_provider_calls, returns ok:true (hold off)', async () => {
    restoreFetch = mockFetchGlobal(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: 'SYNTHESIS: fine.\nANALYSIS_JSON: {"bias_short":"bullish"}' }] } }] }),
    }));
    const { db, inserts } = makeNarrativeFakeDb({ quotaAdmitted: true });
    const result = await holdOffScope.runGeminiDailyAnalysis({ DB: db, GEMINI_API_KEY: 'k' }, 'cron');
    expect(result.ok).toBe(true);
    expect(result.status).toBe('ok');
    expect(inserts.some(i => i.table === 'gemini_daily_analysis')).toBe(true);
    expect(inserts.some(i => i.table === 'gemini_provider_calls')).toBe(true);
  });

  it('cron and manual triggers use separate quota lanes (different consumer names, so one can never exhaust the other) (hold off)', async () => {
    restoreFetch = mockFetchGlobal(async () => ({ ok: false, status: 429, text: async () => 'nope' }));
    const { db: cronDb } = makeNarrativeFakeDb({ quotaAdmitted: true });
    const { db: manualDb } = makeNarrativeFakeDb({ quotaAdmitted: true });
    // Both admitted independently -- this test's fake always admits, so
    // what it actually verifies is that the two calls don't crash on
    // distinct consumer names and produce distinct correlation ids per
    // trigger, which is the observable half of "separate lanes" at the
    // function-call level (the ledger-level separation is covered by the
    // buildQuotaBucketKeys namespacing test above).
    const cronResult = await holdOffScope.runGeminiDailyAnalysis({ DB: cronDb, GEMINI_API_KEY: 'k' }, 'cron');
    const manualResult = await holdOffScope.runGeminiDailyAnalysis({ DB: manualDb, GEMINI_API_KEY: 'k' }, 'manual');
    expect(cronResult.correlationId).toContain('-cron');
    expect(manualResult.correlationId).toContain('-manual');
  });
});

describe('runLinkGeminiAnalysis — shared quota gate + non-throwing contract', () => {
  let scope;
  let holdOffScope;
  let restoreFetch;
  beforeAll(() => {
    const fnSrc = extractFunctions(
      'runLinkGeminiAnalysis', 'callGeminiGenerateContent', 'extractGroundingMetadata',
      'reserveGeminiQuotaSlot', 'buildQuotaBucketKeys', 'utcDayBucket', 'utcHourBucket',
      'recordGeminiProviderCall', 'normalizeBias', 'isGeminiConsumerOnHold'
    );
    const constSrc = extractConstants('GEMINI_CALL_TIMEOUT_MS', 'GEMINI_TRIGGER_CONFIG', 'GEMINI_SHARED_QUOTA_CONFIG', 'GEMINI_LEARNING_FOCUS_HOLD');
    scope = evalInScope(fnSrc + '\n\n' + constSrc);
    holdOffScope = evalInScope(fnSrc + '\n\n' + constSrc.replace('GEMINI_LEARNING_FOCUS_HOLD = true', 'GEMINI_LEARNING_FOCUS_HOLD = false'));
  });
  afterEach(() => { if (restoreFetch) { restoreFetch(); restoreFetch = null; } });

  it('regression: while GEMINI_LEARNING_FOCUS_HOLD is active, LINK analysis makes ZERO fetch calls and reserves ZERO quota', async () => {
    let fetchCalled = false;
    restoreFetch = mockFetchGlobal(async () => { fetchCalled = true; return { ok: true, status: 200, json: async () => ({}) }; });
    const { db, inserts } = makeNarrativeFakeDb({ quotaAdmitted: true });
    const result = await scope.runLinkGeminiAnalysis({ DB: db, GEMINI_API_KEY: 'k' }, 'cron');
    expect(fetchCalled).toBe(false);
    expect(result.ok).toBe(false);
    expect(result.status).toBe('held_for_learning_focus');
    expect(inserts.some(i => i.table === 'gemini_quota_ledger')).toBe(false);
    expect(inserts.filter(i => i.table === 'gemini_provider_calls')).toHaveLength(1);
  });

  it('isGeminiConsumerOnHold: every link_narrative lane is held, investigation is never held', () => {
    expect(scope.isGeminiConsumerOnHold('link_narrative_cron')).toBe(true);
    expect(scope.isGeminiConsumerOnHold('link_narrative_manual')).toBe(true);
    expect(scope.isGeminiConsumerOnHold('investigation')).toBe(false);
  });

  it('when the quota is deferred, returns ok:false/status:quota_deferred WITHOUT ever calling fetch (hold off)', async () => {
    let fetchCalled = false;
    restoreFetch = mockFetchGlobal(async () => { fetchCalled = true; return { ok: true, status: 200, json: async () => ({}) }; });
    const { db } = makeNarrativeFakeDb({ quotaAdmitted: false });
    const result = await holdOffScope.runLinkGeminiAnalysis({ DB: db, GEMINI_API_KEY: 'k' }, 'manual');
    expect(fetchCalled).toBe(false);
    expect(result.ok).toBe(false);
    expect(result.status).toBe('quota_deferred');
    expect(result.correlationId).toMatch(/^GA-\d+-LINK-manual$/);
  });

  it('happy path: writes link_gemini_analysis and gemini_provider_calls, returns ok:true (hold off)', async () => {
    restoreFetch = mockFetchGlobal(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: 'SYNTHESIS: fine.\nLINK_JSON: {"bias_short":"bullish"}' }] } }] }),
    }));
    const { db, inserts } = makeNarrativeFakeDb({ quotaAdmitted: true });
    const result = await holdOffScope.runLinkGeminiAnalysis({ DB: db, GEMINI_API_KEY: 'k' }, 'cron');
    expect(result.ok).toBe(true);
    expect(inserts.some(i => i.table === 'link_gemini_analysis')).toBe(true);
    expect(inserts.some(i => i.table === 'gemini_provider_calls')).toBe(true);
  });
});
