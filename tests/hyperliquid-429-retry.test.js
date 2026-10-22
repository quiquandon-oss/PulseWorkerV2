// Bounded Hyperliquid /info 429 retry for the prediction snapshot (fetchHyperliquidPrice).
// Failure-path focus: retries stay bounded, Retry-After is honoured, and a 429 that
// persists still fails the chain exactly as before (no stale/partial data as success).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const SOURCE = extractConstants('HL_INFO_RETRY') + '\n\n' +
  extractFunctions('parseRetryAfterMs', 'fetchHyperliquidInfo', 'fetchHyperliquidPrice');

function res(status, { body = null, retryAfter = null } = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k) => (k.toLowerCase() === 'retry-after' ? retryAfter : null) },
    json: async () => body,
    text: async () => '',
  };
}

const SNAPSHOT = [
  { universe: [{ name: 'BTC' }, { name: 'ETH' }, { name: 'LINK' }] },
  [{ markPx: '100000.5', funding: '0.00001' }, { markPx: '4000.25', funding: '0.00002' }, { markPx: '20.1', funding: '0.00003' }],
];

// Scripted fetch + recorded sleeps; no real waiting.
function harness(responses, { random = () => 0.5 } = {}) {
  const calls = [];
  const sleeps = [];
  const warns = [];
  const fetchImpl = async (url, init) => { calls.push({ url, init }); return responses[Math.min(calls.length - 1, responses.length - 1)]; };
  const scope = evalInScope(SOURCE, {
    fetch: fetchImpl,
    setTimeout: (fn) => fn(),
    console: { ...console, warn: (m) => warns.push(JSON.parse(m)) },
  });
  const opts = { fetchImpl, sleep: async (ms) => { sleeps.push(ms); }, random };
  return { scope, calls, sleeps, warns, opts };
}

describe('fetchHyperliquidInfo: bounded 429 retry', () => {
  it('a 200 on the first attempt makes exactly one request and never waits', async () => {
    const h = harness([res(200, { body: SNAPSHOT })]);
    const r = await h.scope.fetchHyperliquidInfo({ type: 'metaAndAssetCtxs' }, h.opts);
    expect(r.status).toBe(200);
    expect(h.calls).toHaveLength(1);
    expect(h.sleeps).toEqual([]);
  });

  it('429 then 200: retries once after a jittered backoff within [base/2, base]', async () => {
    const h = harness([res(429), res(200, { body: SNAPSHOT })]);
    const r = await h.scope.fetchHyperliquidInfo({ type: 'metaAndAssetCtxs' }, h.opts);
    expect(r.status).toBe(200);
    expect(h.calls).toHaveLength(2);
    expect(h.sleeps).toHaveLength(1);
    expect(h.sleeps[0]).toBeGreaterThanOrEqual(500);
    expect(h.sleeps[0]).toBeLessThanOrEqual(1000);
    expect(h.warns[0]).toMatchObject({ evt: 'hyperliquid_429_retry', attempt: 1, retry_after: false });
  });

  it('jitter bounds: random=0 gives backoff/2, random=1 gives the full backoff; the second wait doubles', async () => {
    const lo = harness([res(429), res(429), res(200)], { random: () => 0 });
    await lo.scope.fetchHyperliquidInfo({}, lo.opts);
    expect(lo.sleeps).toEqual([500, 1000]);
    const hi = harness([res(429), res(429), res(200)], { random: () => 1 });
    await hi.scope.fetchHyperliquidInfo({}, hi.opts);
    expect(hi.sleeps).toEqual([1000, 2000]);
  });

  it('a persistent 429 stops after 3 attempts (at most 2 extra subrequests) and returns the 429 unchanged', async () => {
    const h = harness([res(429)]);
    const r = await h.scope.fetchHyperliquidInfo({}, h.opts);
    expect(r.status).toBe(429);
    expect(h.calls).toHaveLength(3);
    expect(h.sleeps).toHaveLength(2);
    expect(h.sleeps.reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(8000);
  });

  it('honours Retry-After in seconds', async () => {
    const h = harness([res(429, { retryAfter: '2' }), res(200)]);
    await h.scope.fetchHyperliquidInfo({}, h.opts);
    expect(h.sleeps).toEqual([2000]);
    expect(h.warns[0]).toMatchObject({ retry_after: true, delay_ms: 2000 });
  });

  it('a Retry-After longer than the per-wait cap means no retry at all (never retries earlier than asked)', async () => {
    const h = harness([res(429, { retryAfter: '30' }), res(200)]);
    const r = await h.scope.fetchHyperliquidInfo({}, h.opts);
    expect(r.status).toBe(429);
    expect(h.calls).toHaveLength(1);
    expect(h.sleeps).toEqual([]);
  });

  it('never exceeds the total wait budget even when each Retry-After is within the per-wait cap', async () => {
    const h = harness([res(429, { retryAfter: '4' }), res(429, { retryAfter: '4' }), res(429, { retryAfter: '4' }), res(200)]);
    const cfg = { maxAttempts: 5, baseDelayMs: 1000, maxDelayMs: 4000, maxTotalWaitMs: 8000 };
    const r = await h.scope.fetchHyperliquidInfo({}, { ...h.opts, cfg });
    expect(r.status).toBe(429);
    expect(h.sleeps).toEqual([4000, 4000]);
    expect(h.calls).toHaveLength(3);
  });

  it('only 429 is retried: 500, 403 and 200 are returned after a single request', async () => {
    for (const status of [500, 403, 418]) {
      const h = harness([res(status), res(200)]);
      const r = await h.scope.fetchHyperliquidInfo({}, h.opts);
      expect(r.status).toBe(status);
      expect(h.calls).toHaveLength(1);
    }
  });

  it('sends the same request every attempt (POST JSON to /info)', async () => {
    const h = harness([res(429), res(200)]);
    await h.scope.fetchHyperliquidInfo({ type: 'metaAndAssetCtxs' }, h.opts);
    for (const c of h.calls) {
      expect(c.url).toBe('https://api.hyperliquid.xyz/info');
      expect(c.init).toMatchObject({ method: 'POST', body: '{"type":"metaAndAssetCtxs"}' });
    }
  });

  it('parseRetryAfterMs: seconds, HTTP date, junk', () => {
    const { parseRetryAfterMs } = harness([res(200)]).scope;
    expect(parseRetryAfterMs('0')).toBe(0);
    expect(parseRetryAfterMs('1.5')).toBe(1500);
    expect(parseRetryAfterMs(null)).toBeNull();
    expect(parseRetryAfterMs('')).toBeNull();
    expect(parseRetryAfterMs('soon')).toBeNull();
    const now = Date.parse('2026-10-22T00:00:00Z');
    expect(parseRetryAfterMs('Thu, 22 Oct 2026 00:00:03 GMT', now)).toBe(3000);
    expect(parseRetryAfterMs('Thu, 22 Oct 2026 00:00:00 GMT', now + 5000)).toBe(0);
  });
});

describe('fetchHyperliquidPrice: unchanged result and failure semantics', () => {
  it('a 429 that persists still throws "Hyperliquid info 429" -- the chain fails and writes nothing, as before', async () => {
    const h = harness([res(429)]);
    await expect(h.scope.fetchHyperliquidPrice('BTC')).rejects.toThrow('Hyperliquid info 429');
    expect(h.calls).toHaveLength(3);
  });

  it('a transient 429 followed by 200 yields exactly the snapshot a first-try 200 would', async () => {
    const retried = await harness([res(429), res(200, { body: SNAPSHOT })]).scope.fetchHyperliquidPrice('LINK');
    const direct = await harness([res(200, { body: SNAPSHOT })]).scope.fetchHyperliquidPrice('LINK');
    expect(retried).toEqual(direct);
    expect(direct).toEqual({ price: 20.1, funding: 0.00003 });
  });

  it('non-429 errors are not retried and keep their exact error text', async () => {
    const h = harness([res(500)]);
    await expect(h.scope.fetchHyperliquidPrice('ETH')).rejects.toThrow('Hyperliquid info 500');
    expect(h.calls).toHaveLength(1);
  });

  it('parsing of the snapshot is unchanged (missing coin and unparseable markPx still throw)', async () => {
    await expect(harness([res(200, { body: SNAPSHOT })]).scope.fetchHyperliquidPrice('DOGE')).rejects.toThrow('DOGE not found in Hyperliquid universe');
    const bad = [SNAPSHOT[0], [{ markPx: 'x' }, SNAPSHOT[1][1], SNAPSHOT[1][2]]];
    await expect(harness([res(200, { body: bad })]).scope.fetchHyperliquidPrice('BTC')).rejects.toThrow('BTC markPx not parseable');
  });
});

describe('scope: the retry touches only the metaAndAssetCtxs snapshot', () => {
  const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
  it('fetchHyperliquidInfo has exactly one caller, fetchHyperliquidPrice', () => {
    expect(src.match(/fetchHyperliquidInfo\(/g)).toHaveLength(2); // definition + one call
    expect(extractFunctions('fetchHyperliquidPrice')).toContain("fetchHyperliquidInfo({ type: 'metaAndAssetCtxs' })");
  });
  it('the candleSnapshot backfill keeps its own single-attempt fetch', () => {
    const backfill = extractFunctions('backfillCoinHistory');
    expect(backfill).toContain("await fetch('https://api.hyperliquid.xyz/info'");
    expect(backfill).not.toContain('fetchHyperliquidInfo');
  });
  it('retry limits are the reviewed ones', () => {
    const { HL_INFO_RETRY } = evalInScope(extractConstants('HL_INFO_RETRY'));
    expect(HL_INFO_RETRY).toEqual({ maxAttempts: 3, baseDelayMs: 1000, maxDelayMs: 4000, maxTotalWaitMs: 8000 });
  });
});
