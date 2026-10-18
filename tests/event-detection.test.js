// Event Detection Layer: core record, adapters (mocked providers, no network), rules, persistence (real SQLite with
// migration 0023), API routes through the real worker.js fetch handler, and the cron gate.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { describe, it, expect, vi } from 'vitest';
import {
  makeEvent, dedupKey, eventIdFor, robustBaseline, robustZ, severityFor, textFingerprint, makeHttp, SOURCE_STATUS, stressCorrelationKey,
  makeBudget, countedDb, EVENT_DETECTION_USER_AGENT, FETCH_ALLOCATION, DEFAULT_BUDGET, childBudget, scrubSecrets,
} from '../events/event-core.js';
import { evaluateDetections } from '../events/event-eval.js';
import {
  normalizeOkxDetail, fetchOkxLiquidations, bucketize, liquidationDirection, evaluateLiquidationBucket, LIQ_CONFIG,
  okxLiquidationsAdapter, normalizeBinanceForceOrder, sampleBinanceForceOrders, binanceLiquidationSampleAdapter, mergeHistory,
} from '../events/adapters/liquidations.js';
import { fetchDvol, changes24h, evaluateDvol, summarizeOptionsBook, evaluateOptionsOi, deribitAdapter, DERIBIT_CONFIG } from '../events/adapters/deribit.js';
import { parseFredCsv, parseFredApi, evaluateYieldShock, macroAdapter, fredReleaseCalendar, MACRO_CONFIG, FRED_HEADERS } from '../events/adapters/macro.js';
import { classifyFilingText, htmlToText, recentFilings, secEdgarAdapter, filingUrl } from '../events/adapters/sec-edgar.js';
import { runEventDetection, DETECTORS } from '../events/event-runner.js';
import { listEvents, observationsBefore, insertObservations, insertEvents, processedDocuments, markDocuments } from '../events/event-store.js';
import worker from '../worker.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const { DatabaseSync } = createRequire(import.meta.url)('node:sqlite');
const MIGRATION_0023 = readFileSync(join(__dirname, '..', '.ai', 'migrations', '0023_event_detection.sql'), 'utf8');
const H = 3600_000, D = 24 * H;
const NOW = Date.UTC(2026, 9, 17, 12, 30);

function makeDb({ withEvents = true } = {}) {
  const db = new DatabaseSync(':memory:');
  if (withEvents) db.exec(MIGRATION_0023);
  const prep = (sql) => {
    const o = { args: [] };
    o.bind = (...a) => { o.args = a; return o; };
    o.first = async () => { const r = db.prepare(sql).get(...o.args); return r ? { ...r } : null; };
    o.all = async () => ({ results: db.prepare(sql).all(...o.args).map((r) => ({ ...r })) });
    o.run = async () => { const r = db.prepare(sql).run(...o.args); return { meta: { changes: r.changes } }; };
    return o;
  };
  return { db, env: { DB: { prepare: prep } } };
}

const jsonRes = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } });
const textRes = (body, status = 200) => new Response(body, { status });
const noSleep = () => Promise.resolve();
function fakeFetch(routes) {
  const calls = [];
  const f = async (url, init) => {
    calls.push(String(url));
    for (const [re, h] of routes) if (re.test(String(url))) return typeof h === 'function' ? h(String(url), init) : h.clone ? h.clone() : h;
    return textRes('not found', 404);
  };
  f.calls = calls;
  return f;
}
const http = (routes) => makeHttp({ fetchImpl: fakeFetch(routes), sleep: noSleep, retries: 2, backoffMs: 1 });

// ------------------------------------------------------------------ core
describe('canonical event record', () => {
  const base = { event_type: 'X', source: 's', source_ts: NOW - H, detected_ts: NOW, rule_id: 'r', dedup_parts: ['X', 'BTC', 's', 1] };
  it('validates direction, severity, timestamps and confidence and computes freshness', async () => {
    const ev = await makeEvent({ ...base, direction: 'DOWN', severity: 'HIGH', confidence: 0.4 });
    expect(ev.event_id).toMatch(/^ev_[0-9a-f]{24}$/);
    expect(ev.freshness_ms).toBe(H);
    expect(ev.status).toBe('ACTIVE');
    await expect(makeEvent({ ...base, direction: 'SIDEWAYS' })).rejects.toThrow(/direction/);
    await expect(makeEvent({ ...base, severity: 'EXTREME' })).rejects.toThrow(/severity/);
    await expect(makeEvent({ ...base, confidence: 1.5 })).rejects.toThrow(/confidence/);
    await expect(makeEvent({ ...base, source_ts: 1700 })).rejects.toThrow(/epoch ms/);
    await expect(makeEvent({ ...base, source_ts: NOW + 1 })).rejects.toThrow(/look-ahead/);
    await expect(makeEvent({ ...base, rule_id: undefined })).rejects.toThrow(/rule_id/);
  });
  it('defaults direction UNKNOWN and keeps severity, confidence and direction independent', async () => {
    const ev = await makeEvent({ ...base, severity: 'CRITICAL', confidence: 0.2 });
    expect(ev.direction).toBe('UNKNOWN');
    expect(ev.severity).toBe('CRITICAL');
    expect(ev.confidence).toBe(0.2);
  });
  it('derives a stable id from what happened, not when it was noticed', async () => {
    const a = await makeEvent({ ...base, detected_ts: NOW });
    const b = await makeEvent({ ...base, detected_ts: NOW + 5 * H });
    expect(a.event_id).toBe(b.event_id);
    expect(dedupKey(['A', ' Btc ', null, 3])).toBe('a|btc||3');
    expect(await eventIdFor('a|b')).toBe(await eventIdFor('a|b'));
  });
  it('robust baseline refuses to guess with too little history', () => {
    expect(robustBaseline([1, 2, 3], { minCount: 5 })).toBeNull();
    const b = robustBaseline([1, 2, 3, 4, 100], { minCount: 5 });
    expect(b.median).toBe(3);
    expect(b.mad).toBe(1);
    expect(robustZ(10, b)).toBeCloseTo(7 / 1.4826, 5);
    expect(robustBaseline([5, 5, 5, 5], { minCount: 4, scaleFloor: 0.5 }).scale).toBe(0.5);
  });
  it('maps threshold ratios to severity', () => {
    expect([0.9, 1, 1.5, 2.5, 5].map(severityFor)).toEqual([null, 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL']);
  });
  it('fingerprints syndicated text identically', () => {
    expect(textFingerprint('<b>BTC</b> Falls 10%!')).toBe(textFingerprint('btc falls 10%'));
  });
  it('correlates stress events by asset and 6 h window', () => {
    expect(stressCorrelationKey('BTC', 6 * H + 5)).toBe(stressCorrelationKey('btc', 11 * H));
    expect(stressCorrelationKey('BTC', 6 * H)).not.toBe(stressCorrelationKey('BTC', 12 * H));
  });
});

describe('HTTP: timeouts, bounded retries, rate limits, blocks', () => {
  it('retries 429 and 5xx with backoff, then succeeds', async () => {
    let n = 0;
    const h = makeHttp({ fetchImpl: async () => (++n < 3 ? textRes('busy', n === 1 ? 429 : 503) : jsonRes({ ok: 1 })), sleep: noSleep, retries: 2, backoffMs: 1 });
    expect(await h.json('https://x')).toEqual({ ok: 1 });
    expect(h.counters).toMatchObject({ requests: 3, retries: 2, rate_limited: 1 });
  });
  it('gives up after bounded retries with RATE_LIMITED / PROVIDER_ERROR', async () => {
    const h1 = makeHttp({ fetchImpl: async () => textRes('', 429), sleep: noSleep, retries: 1, backoffMs: 1 });
    await expect(h1.json('https://x')).rejects.toMatchObject({ status: SOURCE_STATUS.RATE_LIMITED });
    const h2 = makeHttp({ fetchImpl: async () => { throw new Error('reset'); }, sleep: noSleep, retries: 1, backoffMs: 1 });
    await expect(h2.json('https://x')).rejects.toMatchObject({ status: SOURCE_STATUS.PROVIDER_ERROR });
  });
  it('reports a geo block (451/403) as BLOCKED without retrying', async () => {
    let n = 0;
    const h = makeHttp({ fetchImpl: async () => { n += 1; return textRes('', 451); }, sleep: noSleep });
    await expect(h.json('https://x')).rejects.toMatchObject({ status: SOURCE_STATUS.BLOCKED });
    expect(n).toBe(1);
  });
  it('rejects malformed JSON', async () => {
    const h = makeHttp({ fetchImpl: async () => textRes('<html>'), sleep: noSleep });
    await expect(h.json('https://x')).rejects.toMatchObject({ status: SOURCE_STATUS.PROVIDER_ERROR });
  });
});

// ------------------------------------------------------------------ liquidations
const det = (ts, side, posSide, px, sz) => ({ ts: String(ts), side, posSide, bkPx: String(px), sz: String(sz), bkLoss: '0', ccy: '' });
const okxPage = (details) => jsonRes({ code: '0', msg: '', data: details.length ? [{ instId: 'BTC-USDT-SWAP', instFamily: 'BTC-USDT', details }] : [] });
const okxInst = jsonRes({ code: '0', data: [{ instId: 'BTC-USDT-SWAP', ctVal: '0.01', ctValCcy: 'BTC' }] });

describe('OKX liquidations adapter', () => {
  it('normalises size with the live contract value and identifies the liquidated side', () => {
    const l = normalizeOkxDetail(det(NOW, 'sell', 'long', 80000, 100), 0.01, 'BTC-USDT-SWAP');
    expect(l).toMatchObject({ liquidated: 'long', qty_base: 1, notional_usd: 80000 });
    expect(normalizeOkxDetail(det(NOW, 'buy', 'net', 80000, 10), 0.01, 'X').liquidated).toBe('short');
    expect(normalizeOkxDetail(det(NOW, 'sell', 'net', 80000, 10), 0.01, 'X').liquidated).toBe('long');
    for (const bad of [det('abc', 'sell', 'long', 1, 1), det(NOW, 'hold', 'long', 1, 1), det(NOW, 'sell', 'long', 0, 1), det(NOW, 'sell', 'long', 1, -1)]) {
      expect(() => normalizeOkxDetail(bad, 0.01, 'X')).toThrow();
    }
  });
  it('pages backwards with `after`, dedupes, stops at the window start and fails closed on a bad cursor', async () => {
    const p1 = [det(NOW - 1000, 'sell', 'long', 80000, 10), det(NOW - 2 * H, 'buy', 'short', 80000, 10)];
    const p2 = [det(NOW - 2 * H, 'buy', 'short', 80000, 10), det(NOW - 9 * H, 'sell', 'long', 80000, 10)];
    const f = fakeFetch([[/after=/, okxPage(p2)], [/liquidation-orders/, okxPage(p1)]]);
    const r = await fetchOkxLiquidations(makeHttp({ fetchImpl: f, sleep: noSleep }), { instFamily: 'BTC-USDT', instId: 'BTC-USDT-SWAP', startTs: NOW - 8 * H, ctVal: 0.01, maxPages: 5 });
    expect(r.reachedStart).toBe(true);
    expect(r.records).toHaveLength(3);                    // duplicate across pages counted once
    expect(f.calls[1]).toContain(`after=${NOW - 2 * H + 1}`);   // exclusive cursor: re-read the boundary millisecond
    const loop = fakeFetch([[/liquidation-orders/, okxPage(p1)]]);   // ignores the cursor
    await expect(fetchOkxLiquidations(makeHttp({ fetchImpl: loop, sleep: noSleep }), { instFamily: 'BTC-USDT', instId: 'BTC-USDT-SWAP', startTs: NOW - 8 * H, ctVal: 0.01, maxPages: 5 })).rejects.toThrow(/pagination/);
  });
  // A simulated OKX: newest first, `after` exclusive (records strictly older), at most 100 records per page.
  const okxServer = (dataset) => async (url) => {
    const m = /after=(\d+)/.exec(String(url));
    const before = m ? Number(m[1]) : Infinity;
    return okxPage(dataset.filter((d) => Number(d.ts) < before).sort((a, b) => Number(b.ts) - Number(a.ts)).slice(0, 100));
  };
  const okxOpts = (startTs) => ({ instFamily: 'BTC-USDT', instId: 'BTC-USDT-SWAP', startTs, ctVal: 0.01, maxPages: 8 });
  it('a burst split across a page boundary at one millisecond is counted exactly (no loss, no double count)', async () => {
    const T1 = NOW - 10 * 60_000, T0 = NOW - 20 * 60_000;
    const dataset = [
      ...Array.from({ length: 60 }, () => det(T1, 'sell', 'long', 80000, 1)),
      ...Array.from({ length: 50 }, () => det(T0, 'sell', 'long', 79000, 2)),       // 50 identical-looking, distinct liquidations
      ...Array.from({ length: 30 }, (_x, i) => det(NOW - 3 * H - i * 1000, 'buy', 'short', 81000, 1)),
      det(NOW - 9 * H, 'sell', 'long', 80000, 1),
    ];
    const f = fakeFetch([[/liquidation-orders/, okxServer(dataset)]]);
    const r = await fetchOkxLiquidations(makeHttp({ fetchImpl: f, sleep: noSleep }), okxOpts(NOW - 8 * H));
    expect(r.reachedStart).toBe(true);
    expect(r.records).toHaveLength(dataset.length);                        // 141: nothing lost at the 100-record boundary
    expect(r.records.filter((x) => x.ts === T0)).toHaveLength(50);
    expect(f.calls[1]).toContain(`after=${T0 + 1}`);
  });
  it('identical records away from page boundaries are kept (no content-based de-duplication)', async () => {
    const dataset = [...Array.from({ length: 7 }, () => det(NOW - H, 'sell', 'long', 80000, 3)), det(NOW - 9 * H, 'buy', 'short', 80000, 1)];
    const r = await fetchOkxLiquidations(makeHttp({ fetchImpl: fakeFetch([[/liquidation-orders/, okxServer(dataset)]]), sleep: noSleep }), okxOpts(NOW - 8 * H));
    expect(r.records).toHaveLength(8);
    expect(r.records.reduce((s2, x) => s2 + x.notional_usd, 0)).toBeCloseTo(7 * 80000 * 3 * 0.01 + 80000 * 0.01, 6);
  });
  it('a full page inside one millisecond cannot be paged past: fails closed instead of undercounting', async () => {
    const T0 = NOW - 30 * 60_000;
    const dataset = [det(NOW - 1000, 'sell', 'long', 80000, 1), ...Array.from({ length: 130 }, () => det(T0, 'sell', 'long', 80000, 1)), det(NOW - 9 * H, 'sell', 'long', 80000, 1)];
    await expect(fetchOkxLiquidations(makeHttp({ fetchImpl: fakeFetch([[/liquidation-orders/, okxServer(dataset)]]), sleep: noSleep }), okxOpts(NOW - 8 * H))).rejects.toThrow(/cursor did not advance/);
  });
  it('history that ends before the window start (exhausted) never marks the uncovered hours COMPLETE', async () => {
    const oldestTs = Date.UTC(2026, 9, 17, 9, 54);                      // NOW is 12:30Z: the 09:00 hour is only partly covered
    const dataset = [det(NOW - 30 * 60_000, 'sell', 'long', 80000, 1), det(oldestTs, 'buy', 'short', 80000, 1)];
    const r = await fetchOkxLiquidations(makeHttp({ fetchImpl: fakeFetch([[/liquidation-orders/, okxServer(dataset)]]), sleep: noSleep }), okxOpts(NOW - 8 * H));
    expect(r).toMatchObject({ reachedStart: false, exhausted: true, oldest: oldestTs });
    const b = bucketize(r.records, { bucketMs: H, coveredFromTs: r.oldest, nowTs: NOW, settleMs: 5 * 60_000 });
    expect(b.map((x) => new Date(x.bucket_ts).toISOString().slice(11, 16))).toEqual(['10:00', '11:00']);  // 09:00 excluded; 12:00 not settled
  });
  it('rejects OKX error codes and rate-limit codes', async () => {
    await expect(fetchOkxLiquidations(http([[/liquidation/, jsonRes({ code: '50011', msg: 'too many', data: [] })]]), { instFamily: 'BTC-USDT', instId: 'BTC-USDT-SWAP', startTs: 0, ctVal: 0.01, maxPages: 1 })).rejects.toMatchObject({ status: SOURCE_STATUS.RATE_LIMITED });
    await expect(fetchOkxLiquidations(http([[/liquidation/, jsonRes({ code: '51000', msg: 'bad', data: [] })]]), { instFamily: 'BTC-USDT', instId: 'BTC-USDT-SWAP', startTs: 0, ctVal: 0.01, maxPages: 1 })).rejects.toMatchObject({ status: SOURCE_STATUS.PROVIDER_ERROR });
    await expect(fetchOkxLiquidations(http([[/liquidation/, jsonRes({ nope: 1 })]]), { instFamily: 'BTC-USDT', instId: 'BTC-USDT-SWAP', startTs: 0, ctVal: 0.01, maxPages: 1 })).rejects.toThrow(/malformed/);
  });
  it('only emits COMPLETE buckets that are covered and settled; empty covered hours are real zeros', () => {
    const t0 = Date.UTC(2026, 9, 17, 6);
    const recs = [{ ts: t0 + 10, liquidated: 'long', notional_usd: 5 }, { ts: t0 + 2 * H + 10, liquidated: 'short', notional_usd: 7 }];
    const b = bucketize(recs, { bucketMs: H, coveredFromTs: t0, nowTs: t0 + 3 * H + 6 * 60_000, settleMs: 5 * 60_000 });
    expect(b.map((x) => [x.bucket_ts - t0, x.long_usd, x.short_usd])).toEqual([[0, 5, 0], [H, 0, 0], [2 * H, 0, 7]]);
    const partial = bucketize(recs, { bucketMs: H, coveredFromTs: t0 + 30 * 60_000, nowTs: t0 + 3 * H + 6 * 60_000, settleMs: 5 * 60_000 });
    expect(partial[0].bucket_ts).toBe(t0 + H);            // the partly covered first hour is never COMPLETE
  });
  it('classifies direction only when one side dominates', () => {
    expect(liquidationDirection(90, 10, 0.65)).toBe('DOWN');
    expect(liquidationDirection(10, 90, 0.65)).toBe('UP');
    expect(liquidationDirection(50, 50, 0.65)).toBe('MIXED');
    expect(liquidationDirection(0, 0, 0.65)).toBe('UNKNOWN');
  });
  const hist = (n, v = 200_000) => Array.from({ length: n }, (_x, i) => ({ bucket_ts: NOW - (n - i) * H - 2 * H, value: v + (i % 5) * 10_000 }));
  const bucket = (long, short) => ({ bucket_ts: NOW - 2 * H, long_usd: long, short_usd: short, count: 40 });
  it('INSUFFICIENT_BASELINE below the minimum history, no event', async () => {
    const r = await evaluateLiquidationBucket(bucket(9e6, 0), hist(LIQ_CONFIG.minBaselineBuckets - 1), LIQ_CONFIG, { nowTs: NOW });
    expect(r).toMatchObject({ status: SOURCE_STATUS.INSUFFICIENT_BASELINE, event: null });
  });
  it('detects a burst above max(abs floor, robust z, multiple of median) with direction and evidence', async () => {
    const r = await evaluateLiquidationBucket(bucket(8e6, 1e6), hist(100), LIQ_CONFIG, { nowTs: NOW });
    expect(r.event).toMatchObject({ event_type: 'LIQUIDATION_BURST', direction: 'DOWN', severity: 'CRITICAL', rule_id: 'ed-liq-burst-v1', source_ts: NOW - H, threshold_value: 1e6 });
    expect(r.event.evidence[0]).toMatchObject({ long_liquidated_usd: 8e6, short_liquidated_usd: 1e6, baseline_buckets: 100 });
    expect(r.event.explanation).toMatch(/One venue only/);
    expect(r.event.confidence).toBeLessThanOrEqual(0.7);
    const quiet = await evaluateLiquidationBucket(bucket(300_000, 100_000), hist(100), LIQ_CONFIG, { nowTs: NOW });
    expect(quiet.event).toBeNull();
  });
  it('baseline maturity boundary: 47 prior complete buckets refuse, 48 evaluate; only the prior 168 h count', async () => {
    expect((await evaluateLiquidationBucket(bucket(9e6, 0), hist(47), LIQ_CONFIG, { nowTs: NOW })).status).toBe(SOURCE_STATUS.INSUFFICIENT_BASELINE);
    const at48 = await evaluateLiquidationBucket(bucket(9e6, 0), hist(48), LIQ_CONFIG, { nowTs: NOW });
    expect(at48.status).toBe(SOURCE_STATUS.OK);
    expect(at48.baselineCount).toBe(48);
    const old = hist(48).map((h) => ({ ...h, bucket_ts: h.bucket_ts - 200 * H }));   // all older than the 168 h window
    const okxStore = old.map((h) => ({ ...h }));
    // the adapter slices history to [bucket - 168 h, bucket); emulate that slice here
    const inWindow = okxStore.filter((h) => h.bucket_ts >= NOW - 2 * H - LIQ_CONFIG.baselineBuckets * H);
    expect((await evaluateLiquidationBucket(bucket(9e6, 0), inWindow, LIQ_CONFIG, { nowTs: NOW })).status).toBe(SOURCE_STATUS.INSUFFICIENT_BASELINE);
  });
  it('threshold boundary: a bucket exactly at the threshold is an event, one dollar below is not', async () => {
    const thr = (await evaluateLiquidationBucket(bucket(5e7, 0), hist(100), LIQ_CONFIG, { nowTs: NOW })).event.threshold_value;
    expect((await evaluateLiquidationBucket(bucket(thr, 0), hist(100), LIQ_CONFIG, { nowTs: NOW })).event).not.toBeNull();
    expect((await evaluateLiquidationBucket(bucket(thr - 1, 0), hist(100), LIQ_CONFIG, { nowTs: NOW })).event).toBeNull();
  });
  it('stored history wins over this run for the same bucket', () => {
    expect(mergeHistory([{ bucket_ts: 1, value: 5 }], [{ bucket_ts: 1, value: 9 }, { bucket_ts: 2, value: 3 }])).toEqual([{ bucket_ts: 1, value: 5 }, { bucket_ts: 2, value: 3 }]);
  });
  it('reads ctVal live and refuses an instrument without it', async () => {
    const h = http([[/instruments/, jsonRes({ code: '0', data: [{ instId: 'BTC-USDT-SWAP', ctVal: '', ctValCcy: 'BTC' }] })]]);
    await expect(okxLiquidationsAdapter.run({ http: h, nowTs: NOW, history: async () => [], config: {} })).rejects.toThrow(/ctVal/);
  });
});

describe('Binance forceOrder stream (sampled, secondary)', () => {
  const msg = (T, S, z = '0.5', ap = '80000') => ({ e: 'forceOrder', E: T, o: { s: 'BTCUSDT', S, o: 'LIMIT', f: 'IOC', q: z, p: ap, ap, X: 'FILLED', l: z, z, T } });
  it('normalises the documented payload and rejects others', () => {
    expect(normalizeBinanceForceOrder(msg(NOW, 'SELL'))).toMatchObject({ liquidated: 'long', notional_usd: 40000, instrument: 'BTCUSDT' });
    expect(normalizeBinanceForceOrder({ e: 'trade' })).toBeNull();
    expect(normalizeBinanceForceOrder(msg('x', 'SELL'))).toBeNull();
  });
  function fakeSocket(script) {
    const l = { message: [], close: [], error: [] };
    const ws = { addEventListener: (t, fn) => l[t].push(fn), close: vi.fn() };
    setTimeout(() => { for (const s of script) { if (s === 'close') l.close.forEach((f) => f()); else l.message.forEach((f) => f({ data: JSON.stringify(s) })); } }, 0);
    return ws;
  }
  it('dedupes repeated snapshots and ends early on disconnect', async () => {
    const r = await sampleBinanceForceOrders({ durationMs: 2000, connect: async () => fakeSocket([msg(NOW, 'SELL'), msg(NOW, 'SELL'), msg(NOW + 1, 'BUY'), 'close']) });
    expect(r.records).toHaveLength(2);
    expect(r.closedEarly).toBe(true);
  });
  it('is disabled by default and never produces events (SAMPLED coverage)', async () => {
    expect(binanceLiquidationSampleAdapter.enabled({})).toBe(false);
    const res = await binanceLiquidationSampleAdapter.run({ env: { EVENT_BINANCE_SAMPLE_MS: '50' }, nowTs: NOW, connect: async () => fakeSocket([msg(NOW, 'SELL')]) });
    expect(res.events).toEqual([]);
    expect(res.observations[0].coverage).toBe('SAMPLED');
  });
});

// ------------------------------------------------------------------ Deribit
const dvolRows = (n, f, start = NOW - n * H) => Array.from({ length: n }, (_x, i) => [Math.floor(start / H) * H + i * H, 1, 1, 1, f(i)]);
describe('Deribit DVOL and options OI', () => {
  it('follows continuation backwards and validates candles', async () => {
    const f = fakeFetch([
      [/end_timestamp=111/, jsonRes({ result: { data: [[0, 1, 1, 1, 40]], continuation: null } })],
      [/get_volatility_index_data/, jsonRes({ result: { data: [[H, 1, 1, 1, 41]], continuation: 111 } })],
    ]);
    const rows = await fetchDvol(makeHttp({ fetchImpl: f, sleep: noSleep }), 'BTC', 0, 999999);
    expect(rows.map((r) => r.close)).toEqual([40, 41]);
    await expect(fetchDvol(http([[/vol/, jsonRes({ result: { data: [[5, 1, 1, 1, 'x']], continuation: null } })]]), 'BTC', 0, 1)).rejects.toThrow(/malformed/);
    await expect(fetchDvol(http([[/vol/, jsonRes({ error: { code: 10028, message: 'too_many_requests' } })]]), 'BTC', 0, 1)).rejects.toMatchObject({ status: SOURCE_STATUS.RATE_LIMITED });
  });
  it('computes 24 h changes only where both ends exist', () => {
    expect(changes24h([{ bucket_ts: 0, value: 1 }, { bucket_ts: 24 * H, value: 4 }, { bucket_ts: 25 * H, value: 9 }])).toEqual([{ ts: 24 * H, delta: 3 }]);
  });
  const T0 = Date.UTC(2026, 8, 1);
  const series = (n, f) => Array.from({ length: n }, (_x, i) => ({ bucket_ts: T0 + i * H, value: f(i) }));
  const wiggle = (i) => 40 + Math.sin(i / 5) * 0.5;
  it('VOL_EXPANSION / VOL_COMPRESSION with direction UNKNOWN, INSUFFICIENT_BASELINE when history is short', async () => {
    const n = 20 * 24;
    const up = series(n, (i) => (i === n - 1 ? 52 : wiggle(i)));
    const r = await evaluateDvol('BTC', up, DERIBIT_CONFIG, { nowTs: T0 + n * H + 1000, source: 'deribit_options' });
    expect(r.event).toMatchObject({ event_type: 'VOL_EXPANSION', direction: 'UNKNOWN', rule_id: 'ed-dvol-24h-v1' });
    expect(r.event.explanation).toMatch(/not directional/);
    const down = series(n, (i) => (i === n - 1 ? 28 : wiggle(i)));
    expect((await evaluateDvol('BTC', down, DERIBIT_CONFIG, { nowTs: T0 + n * H + 1000, source: 'd' })).event.event_type).toBe('VOL_COMPRESSION');
    const calm = series(n, wiggle);
    expect((await evaluateDvol('BTC', calm, DERIBIT_CONFIG, { nowTs: T0 + n * H + 1000, source: 'd' })).event).toBeNull();
    const short = series(5 * 24, (i) => (i === 119 ? 60 : 40));
    expect((await evaluateDvol('BTC', short, DERIBIT_CONFIG, { nowTs: T0 + 120 * H + 1, source: 'd' })).status).toBe(SOURCE_STATUS.INSUFFICIENT_BASELINE);
  });
  it('summarises options OI by put/call/expiry and ignores other currencies and junk', () => {
    const s = summarizeOptionsBook([
      { instrument_name: 'BTC-19OCT26-71000-C', open_interest: 10, creation_timestamp: NOW },
      { instrument_name: 'BTC-19OCT26-60000-P', open_interest: 5, creation_timestamp: NOW - 5 },
      { instrument_name: 'BTC-PERPETUAL', open_interest: 999 },
      { instrument_name: 'ETH-19OCT26-3000-C', open_interest: 7 },
      { instrument_name: 'BTC-26DEC26-90000-C', open_interest: 'x' },
    ], 'BTC');
    expect(s).toMatchObject({ instruments: 2, total: 15, puts: 5, calls: 10, snapshotTs: NOW, byExpiry: { '19OCT26': 15 } });
    expect(() => summarizeOptionsBook([], 'BTC')).toThrow(/no BTC option/);
  });
  it('OPTIONS_OI_SHIFT needs a ~24 h reference and a baseline of past changes', async () => {
    const T0 = Date.UTC(2026, 8, 1);
    const snaps = Array.from({ length: 160 }, (_x, i) => ({ bucket_ts: T0 + i * 3 * H, value: 100_000 * (1 + 0.005 * Math.sin(i)) }));
    const cur = { bucket_ts: T0 + 160 * 3 * H, value: 130_000, snapshot_ts: T0 + 160 * 3 * H };
    const r = await evaluateOptionsOi('BTC', snaps, cur, DERIBIT_CONFIG, { nowTs: cur.bucket_ts + 1, source: 'd' });
    expect(r.event).toMatchObject({ event_type: 'OPTIONS_OI_SHIFT', direction: 'UNKNOWN' });
    expect(r.event.explanation).toMatch(/no dealer gamma/);
    const none = await evaluateOptionsOi('BTC', [], cur, DERIBIT_CONFIG, { nowTs: cur.bucket_ts + 1, source: 'd' });
    expect(none.status).toBe(SOURCE_STATUS.INSUFFICIENT_BASELINE);
  });
  it('marks a stale options snapshot STALE and writes no OI observation for it', async () => {
    const f = [
      [/get_volatility_index_data/, () => jsonRes({ result: { data: dvolRows(48, () => 40), continuation: null } })],
      [/get_book_summary_by_currency/, () => jsonRes({ result: [{ instrument_name: 'BTC-19OCT26-71000-C', open_interest: 1, creation_timestamp: NOW - 2 * H }, { instrument_name: 'ETH-19OCT26-3000-C', open_interest: 1, creation_timestamp: NOW - 2 * H }] })],
    ];
    const res = await deribitAdapter.run({ http: http(f), nowTs: NOW, history: async () => [], config: {} });
    expect(res.details.currencies.BTC.options_oi).toBe(SOURCE_STATUS.STALE);
    expect(res.observations.some((o) => o.series.startsWith('opt_oi'))).toBe(false);
    expect(res.observations.every((o) => o.bucket_ts + H <= NOW)).toBe(true);   // only closed DVOL candles
  });
});

// ------------------------------------------------------------------ macro
describe('Macro: FRED yields, calendar, consensus unavailable', () => {
  it('parses FRED CSV (both header styles), skips missing "." values, rejects malformed rows', () => {
    expect(parseFredCsv('observation_date,DGS10\n2026-10-14,4.10\n2026-10-15,.\n2026-10-16,4.25\n', 'DGS10').map((r) => r.value)).toEqual([4.1, 4.25]);
    expect(parseFredCsv('DATE,DGS2\n2026-10-16,3.9\n', 'DGS2')).toHaveLength(1);
    expect(() => parseFredCsv('foo,bar\n1,2\n', 'DGS10')).toThrow(/header/);
    expect(() => parseFredCsv('DATE,DGS10\nnot-a-date,4\n', 'DGS10')).toThrow(/malformed/);
    expect(parseFredApi({ observations: [{ date: '2026-10-16', value: '.' }, { date: '2026-10-15', value: '4.2' }] }, 'DGS10')).toHaveLength(1);
  });
  const rows = (n, lastJump) => Array.from({ length: n }, (_x, i) => ({ date: new Date(Date.UTC(2026, 0, 1) + i * D).toISOString().slice(0, 10), ts: Date.UTC(2026, 0, 1) + i * D, value: 4 + (i % 2) * 0.02 + (i === n - 1 ? lastJump : 0) }));
  it('YIELD_SHOCK on an outsized daily move, direction UNKNOWN; none on a normal day', async () => {
    const big = rows(200, 0.3);
    const r = await evaluateYieldShock('DGS10', big, MACRO_CONFIG, { nowTs: big[199].ts + D, source: 'macro_fred', via: 'test' });
    expect(r.event).toMatchObject({ event_type: 'YIELD_SHOCK', direction: 'UNKNOWN', unit: 'basis points, 1 business day' });
    const normal = rows(200, 0);
    expect((await evaluateYieldShock('DGS10', normal, MACRO_CONFIG, { nowTs: normal[199].ts + D, source: 'm', via: 't' })).event).toBeNull();
    expect((await evaluateYieldShock('DGS10', rows(50, 1), MACRO_CONFIG, { nowTs: NOW, source: 'm', via: 't' })).status).toBe(SOURCE_STATUS.INSUFFICIENT_BASELINE);
  });
  it('calendar without a key is NOT_CONFIGURED; with a key it lists matched releases with consensus null', async () => {
    const none = await fredReleaseCalendar({ http: http([]), apiKey: null, releases: MACRO_CONFIG.releases }).upcoming(NOW, 7 * D);
    expect(none.status).toBe(SOURCE_STATUS.NOT_CONFIGURED);
    const cal = fredReleaseCalendar({ http: http([[/releases\/dates/, jsonRes({ release_dates: [
      { release_id: 10, release_name: 'Consumer Price Index', date: '2026-10-20' },
      { release_id: 999, release_name: 'Some Other Release', date: '2026-10-20' },
      { release_id: 50, release_name: 'Employment Situation', date: '2026-10-01' },
    ] })]]), apiKey: 'k', releases: MACRO_CONFIG.releases });
    const up = await cal.upcoming(NOW, 7 * D);
    expect(up.items).toHaveLength(1);
    expect(up.items[0]).toMatchObject({ key: 'CPI', consensus: null, actual: null, time_known: false });
  });
  it('adapter: keyless CSV path, scheduled-release events say surprise DATA_UNAVAILABLE, never compute one', async () => {
    const csv = (sid) => `observation_date,${sid}\n` + rows(200, 0).map((r) => `${r.date},${r.value}`).join('\n');
    const h = http([[/fredgraph\.csv\?id=DGS2/, () => textRes(csv('DGS2'))], [/fredgraph\.csv\?id=DGS10/, () => textRes(csv('DGS10'))]]);
    const calendar = { id: 'fake', upcoming: async () => ({ status: 'OK', items: [{ key: 'CPI', name: 'Consumer Price Index', release_id: 10, scheduled_date: '2026-10-20', time_known: false, consensus: null, actual: null, provenance: 'test' }] }) };
    const lastTs = rows(200, 0)[199].ts;
    const res = await macroAdapter.run({ env: {}, http: h, nowTs: lastTs + D, config: {}, calendar });
    expect(res.details.consensus_surprise.status).toBe(SOURCE_STATUS.DATA_UNAVAILABLE);
    expect(res.details.series.DGS10.via).toMatch(/CSV/);
    const sched = res.events.find((e) => e.event_type === 'MACRO_RELEASE_SCHEDULED');
    expect(sched.evidence[0]).toMatchObject({ consensus: 'DATA_UNAVAILABLE', actual: 'DATA_UNAVAILABLE' });
    expect(res.events.some((e) => /SURPRISE/.test(e.event_type))).toBe(false);
    expect(res.observations.length).toBeLessThanOrEqual(20);
  });
  // Regression for the 2026-10-18 production run (edr_c46163d70fa2f878): FRED's edge resets requests that carry no
  // User-Agent (a Worker's default), which a Worker sees as HTTP 520. Reproduced from a GitHub runner (run 39114185612).
  const fredEdge = (body) => async (url, init = {}) => {
    const ua = (init.headers && (init.headers['User-Agent'] || init.headers['user-agent'])) || '';
    if (!ua || /Mozilla|Cloudflare-Workers/.test(ua)) return new Response('error code: 520', { status: 520, headers: { server: 'cloudflare', 'cf-ray': 'x' } });
    return textRes(body(String(url)));
  };
  const fredCsv = (u) => `observation_date,${/id=DGS10/.test(u) ? 'DGS10' : 'DGS2'}\n` + rows(200, 0).map((r) => `${r.date},${r.value}`).join('\n');
  it('FRED requests send the descriptive User-Agent (CSV, keyed API, calendar) and no contact email', async () => {
    expect(FRED_HEADERS['User-Agent']).toBe(EVENT_DETECTION_USER_AGENT);
    expect(EVENT_DETECTION_USER_AGENT).not.toMatch(/@/);
    const seen = [];
    const rec = (res) => async (url, init = {}) => { seen.push([String(url), init.headers && init.headers['User-Agent']]); return res(String(url)); };
    const calRes = () => jsonRes({ release_dates: [] });
    const apiRes = () => jsonRes({ observations: rows(200, 0).map((r) => ({ date: r.date, value: String(r.value) })) });
    const csvH = makeHttp({ fetchImpl: rec((u) => textRes(fredCsv(u))), sleep: noSleep });
    await macroAdapter.run({ env: {}, http: csvH, nowTs: rows(200, 0)[199].ts + D, config: {} });
    const apiH = makeHttp({ fetchImpl: rec((u) => (/releases/.test(u) ? calRes() : apiRes())), sleep: noSleep });
    await macroAdapter.run({ env: { FRED_API_KEY: 'k'.repeat(32) }, http: apiH, nowTs: rows(200, 0)[199].ts + D, config: {} });
    expect(seen.length).toBe(5);                                    // 2 CSV + 2 API + 1 calendar
    expect(seen.every(([, ua]) => ua === EVENT_DETECTION_USER_AGENT)).toBe(true);
  });
  it('the observed failure mode: no UA -> 520 with bounded retries and a diagnosable error; with the UA -> OK', async () => {
    const bare = makeHttp({ fetchImpl: fredEdge(fredCsv), sleep: noSleep, retries: 2 });
    await expect(bare.text(`https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2`)).rejects.toMatchObject({ status: SOURCE_STATUS.PROVIDER_ERROR, message: expect.stringMatching(/^HTTP 520 \(server=cloudflare cf-ray=yes body="error code: 520"\)$/) });
    expect(bare.counters).toMatchObject({ requests: 3, retries: 2, errors: 1 });   // 1 + 2 retries, then stop
    const h = makeHttp({ fetchImpl: fredEdge(fredCsv), sleep: noSleep });
    const res = await macroAdapter.run({ env: {}, http: h, nowTs: rows(200, 0)[199].ts + D, config: {}, calendar: { id: 'none', upcoming: async () => ({ status: 'NOT_CONFIGURED', items: [] }) } });
    expect(res.status).toBe(SOURCE_STATUS.OK);
    expect(Object.keys(res.details.series)).toEqual(['DGS2', 'DGS10']);
    expect(h.counters).toMatchObject({ requests: 2, retries: 0, errors: 0 });
  });
  it('series are isolated: one failing series is reported, the other is still stored; all failing raises', async () => {
    const half = makeHttp({ sleep: noSleep, retries: 0, fetchImpl: async (u) => (/id=DGS2&/.test(String(u)) ? new Response('down', { status: 503 }) : textRes(fredCsv(String(u)))) });
    const cal = { id: 'none', upcoming: async () => ({ status: 'NOT_CONFIGURED', items: [] }) };
    const res = await macroAdapter.run({ env: {}, http: half, nowTs: rows(200, 0)[199].ts + D, config: {}, calendar: cal });
    expect(res.status).toBe(SOURCE_STATUS.PROVIDER_ERROR);
    expect(res.details.series.DGS2).toMatchObject({ status: SOURCE_STATUS.PROVIDER_ERROR, error: expect.stringMatching(/^HTTP 503/) });
    expect(res.details.series.DGS10.rule).toBe(SOURCE_STATUS.OK);
    expect(res.observations.every((o) => o.series === 'DGS10')).toBe(true);
    expect(res.observations.length).toBe(10);
    const down = makeHttp({ sleep: noSleep, retries: 0, fetchImpl: async () => new Response('down', { status: 503 }) });
    await expect(macroAdapter.run({ env: {}, http: down, nowTs: NOW, config: {}, calendar: cal })).rejects.toMatchObject({ status: SOURCE_STATUS.PROVIDER_ERROR });
  });
  it('a failing release calendar does not discard the yield series', async () => {
    const h = makeHttp({ sleep: noSleep, retries: 0, fetchImpl: async (u) => (/releases/.test(String(u)) ? new Response('x', { status: 500 }) : jsonRes({ observations: rows(200, 0).map((r) => ({ date: r.date, value: String(r.value) })) })) });
    const res = await macroAdapter.run({ env: { FRED_API_KEY: 'k'.repeat(32) }, http: h, nowTs: rows(200, 0)[199].ts + D, config: {} });
    expect(res.status).toBe(SOURCE_STATUS.OK);
    expect(res.details.calendar).toMatchObject({ status: SOURCE_STATUS.PROVIDER_ERROR, upcoming: 0 });
    expect(res.observations.length).toBe(20);
  });
  it('a stale daily series is STALE and evaluates no rule', async () => {
    const csv = (sid) => `observation_date,${sid}\n` + rows(200, 0.5).map((r) => `${r.date},${r.value}`).join('\n');
    const h = http([[/fredgraph/, (u) => textRes(csv(/DGS2/.test(u) && !/DGS20/.test(u) ? 'DGS2' : 'DGS10'))]]);
    const calendar = { id: 'fake', upcoming: async () => ({ status: 'NOT_CONFIGURED', items: [] }) };
    const res = await macroAdapter.run({ env: {}, http: h, nowTs: rows(200, 0)[199].ts + 30 * D, config: {}, calendar });
    expect(res.status).toBe(SOURCE_STATUS.STALE);
    expect(res.events).toEqual([]);
  });
});

// ------------------------------------------------------------------ SEC EDGAR
describe('SEC EDGAR filings', () => {
  it('COMPLETED purchase with quantity; intention is PROPOSED, never a purchase', () => {
    const done = classifyFilingText('Between October 6 and October 12, 2026, the Company acquired approximately 7,390 bitcoins for an aggregate purchase price of $550 million. Other text here for padding.');
    expect(done[0]).toMatchObject({ event_type: 'CRYPTO_TREASURY_PURCHASE', transaction_status: 'COMPLETED', quantity_btc: 7390 });
    const plan = classifyFilingText('The Company intends to use the net proceeds to purchase additional bitcoin and for general corporate purposes.');
    expect(plan[0]).toMatchObject({ event_type: 'CRYPTO_TREASURY_PURCHASE', transaction_status: 'PROPOSED', quantity_btc: null });
  });
  it('mixed completed + conditional language in one sentence is UNKNOWN; disagreeing sentences degrade to UNKNOWN', () => {
    const mixed = classifyFilingText('The Company purchased 100 bitcoin and may purchase additional bitcoin if market conditions permit.');
    expect(mixed[0].transaction_status).toBe('UNKNOWN');
    const two = classifyFilingText('The Company acquired 50 bitcoin during the quarter. The Company plans to acquire more bitcoin over time going forward.');
    expect(two[0].transaction_status).toBe('UNKNOWN');
  });
  it('classifies sales, convertible and equity financing, policy changes and liquidity risk; dedupes boilerplate', () => {
    const t = [
      'On October 10, 2026 the Company sold 1,200 bitcoins at an average price of $80,000 per bitcoin.',
      'On October 11, 2026 the Company closed its offering of $1.0 billion aggregate principal amount of 0% convertible senior notes due 2031.',
      'The Company entered into a sales agreement for an at-the-market offering under which it may sell up to $2 billion of common stock from time to time.',
      'The Board approved an amended treasury reserve policy that now includes bitcoin as the primary treasury reserve asset.',
      'These conditions raise substantial doubt about the Company’s ability to continue as a going concern.',
      'These conditions raise substantial doubt about the Company’s ability to continue as a going concern.',
    ].join(' ');
    const by = Object.fromEntries(classifyFilingText(t).map((f) => [f.event_type, f]));
    expect(by.CRYPTO_TREASURY_SALE).toMatchObject({ transaction_status: 'COMPLETED', quantity_btc: 1200 });
    expect(by.FINANCING_CONVERTIBLE.transaction_status).toBe('COMPLETED');
    expect(by.FINANCING_EQUITY.transaction_status).toBe('PROPOSED');
    expect(by.TREASURY_POLICY_CHANGE.transaction_status).toBe('COMPLETED');
    expect(by.LIQUIDITY_RISK.evidence).toHaveLength(1);
  });
  it('strips HTML and selects recent filings of tracked forms by acceptance time', () => {
    expect(htmlToText('<p>Acquired&nbsp;<b>10</b> bitcoins</p><script>x</script>')).toBe('Acquired 10 bitcoins.');
    const sub = { filings: { recent: { accessionNumber: ['0001-26-1', '0001-26-2', '0001-26-3'], form: ['8-K', '4', '8-K'], filingDate: ['2026-10-14', '2026-10-14', '2026-09-01'], acceptanceDateTime: ['2026-10-14T16:05:00.000Z', '2026-10-14T17:00:00.000Z', '2026-09-01T12:00:00.000Z'], primaryDocument: ['a.htm', 'b.xml', 'c.htm'], items: ['8.01', '', '8.01'] } } };
    const f = recentFilings(sub, { sinceTs: NOW - 7 * D });
    expect(f.map((x) => x.accession)).toEqual(['0001-26-1']);
    expect(filingUrl('0001050446', '0001050446-26-000123', 'x.htm')).toBe('https://www.sec.gov/Archives/edgar/data/1050446/000105044626000123/x.htm');
    expect(() => recentFilings({}, { sinceTs: 0 })).toThrow(/malformed/);
  });
  it('NOT_CONFIGURED without a declared User-Agent; skips a CIK whose tickers do not match; sends the UA', async () => {
    expect((await secEdgarAdapter.run({ env: {}, http: http([]), nowTs: NOW })).status).toBe(SOURCE_STATUS.NOT_CONFIGURED);
    const seenUA = [];
    const sub = (tickers) => jsonRes({ name: 'Strategy Inc', tickers, filings: { recent: { accessionNumber: ['0001050446-26-000123'], form: ['8-K'], filingDate: ['2026-10-14'], acceptanceDateTime: ['2026-10-14T16:05:00.000Z'], primaryDocument: ['mstr-8k.htm'], items: ['8.01'] } } });
    const f = fakeFetch([
      [/CIK0001050446/, (u, init) => { seenUA.push(init.headers['User-Agent']); return sub(['MSTR']); }],
      [/CIK0001507605/, () => sub(['NOPE'])],
      [/mstr-8k\.htm/, () => textRes('<p>During the period, Strategy acquired 7,390 bitcoins for $550 million.</p>')],
    ]);
    const env = { EVENT_SEC_USER_AGENT: 'CryptoPulse research owner@example.com' };
    const res = await secEdgarAdapter.run({ env, http: makeHttp({ fetchImpl: f, sleep: noSleep }), nowTs: NOW, sleep: noSleep, config: { companies: [{ cik: 1050446, ticker: 'MSTR' }, { cik: 1507605, ticker: 'MARA' }] } });
    expect(seenUA[0]).toBe('CryptoPulse research owner@example.com');
    expect(res.details.companies.MARA.status).toBe('SKIPPED_IDENTITY_MISMATCH');
    expect(res.events).toHaveLength(1);
    expect(res.events[0]).toMatchObject({ event_type: 'CRYPTO_TREASURY_PURCHASE', direction: 'UNKNOWN', source_ts: Date.parse('2026-10-14T16:05:00.000Z'), severity: 'HIGH' });
    expect(res.events[0].evidence[0]).toMatchObject({ accession: '0001050446-26-000123', form: '8-K', transaction_status: 'COMPLETED' });
  });
});

// ------------------------------------------------------------------ persistence, runner, idempotency, isolation
describe('migration 0023 constraints', () => {
  it('rejects invalid direction/severity/confidence and source_ts after detected_ts', () => {
    const { db } = makeDb();
    const ins = (o) => db.prepare(`INSERT INTO event_ledger (event_id, dedup_key, event_type, source, source_ts, detected_ts, direction, severity, confidence, rule_id, rule_version, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`)
      .run(o.id, o.key, 'X', 's', o.src ?? 1, o.det ?? 2, o.dir ?? 'UP', o.sev ?? 'LOW', o.conf ?? 0.5, 'r', 'v', 3);
    ins({ id: 'a', key: 'a' });
    expect(() => ins({ id: 'b', key: 'b', dir: 'SIDEWAYS' })).toThrow();
    expect(() => ins({ id: 'c', key: 'c', sev: 'HUGE' })).toThrow();
    expect(() => ins({ id: 'd', key: 'd', conf: 2 })).toThrow();
    expect(() => ins({ id: 'e', key: 'e', src: 5, det: 4 })).toThrow();
    expect(() => db.prepare(`INSERT INTO event_observations (source, series, bucket_ts, value, coverage, observed_ts) VALUES ('s','x',1,1,'GUESSED',1)`).run()).toThrow();
  });
});

function scriptedAdapter(id, impl) { return { id, label: id, kind: 'test', freshnessLimitMs: 4 * H, run: impl }; }

describe('runner: idempotent replay, failure isolation, health, point-in-time reads', () => {
  const evInput = (bucket) => ({ event_type: 'T', asset: 'BTC', source: 'a', source_ts: bucket + H, detected_ts: NOW, rule_id: 'r', dedup_parts: ['T', 'BTC', 'a', bucket], severity: 'HIGH', direction: 'MIXED', expires_ts: NOW + D });
  it('replaying the same detection writes nothing new and keeps the first detection time', async () => {
    const { env, db } = makeDb();
    const a = scriptedAdapter('a', async ({ nowTs }) => ({ status: 'OK', observations: [{ source: 'a', series: 's', bucket_ts: NOW - 2 * H, value: 1, observed_ts: nowTs }], events: [await makeEvent({ ...evInput(NOW - 2 * H), detected_ts: nowTs })], last_observation_ts: nowTs - H }));
    const r1 = await runEventDetection(env, { nowTs: NOW, adapters: [a] });
    const r2 = await runEventDetection(env, { nowTs: NOW + H, adapters: [a] });
    expect(r1.summary.adapters.a).toMatchObject({ events_new: 1, observations_written: 1 });
    expect(r2.summary.adapters.a).toMatchObject({ events_new: 0, observations_written: 0 });
    const rows = db.prepare('SELECT detected_ts FROM event_ledger').all();
    expect(rows).toEqual([{ detected_ts: NOW }]);
    expect(db.prepare('SELECT observed_ts FROM event_observations').all()).toEqual([{ observed_ts: NOW }]);
    expect(db.prepare('SELECT COUNT(*) n FROM event_runs').get().n).toBe(2);
  });
  it('one failing provider never blocks the others; failures are recorded in health', async () => {
    const { env } = makeDb();
    const bad = scriptedAdapter('bad', async () => { throw Object.assign(new Error('HTTP 451 (provider refused access)'), { status: SOURCE_STATUS.BLOCKED }); });
    const hang = scriptedAdapter('hang', () => new Promise(() => {}));
    const good = scriptedAdapter('good', async () => ({ status: 'OK', observations: [], events: [], last_observation_ts: NOW - 10 }));
    const r = await runEventDetection(env, { nowTs: NOW, adapters: [bad, hang, good], adapterTimeoutMs: 50 });
    expect(r.summary.adapters).toMatchObject({ bad: { status: SOURCE_STATUS.BLOCKED }, hang: { status: SOURCE_STATUS.PROVIDER_ERROR }, good: { status: 'OK' } });
    const health = (await env.DB.prepare('SELECT source, status, last_error, last_success_ts FROM event_source_health ORDER BY source').all()).results;
    expect(health.find((h) => h.source === 'bad')).toMatchObject({ status: SOURCE_STATUS.BLOCKED, last_success_ts: null });
    expect(health.find((h) => h.source === 'hang').last_error).toMatch(/timeout/);
    expect(health.find((h) => h.source === 'good').last_success_ts).toBe(NOW);
  });
  it('marks an OK source whose newest observation is too old as STALE', async () => {
    const { env } = makeDb();
    const old = scriptedAdapter('old', async () => ({ status: 'OK', observations: [], events: [], last_observation_ts: NOW - 5 * H }));
    const r = await runEventDetection(env, { nowTs: NOW, adapters: [old] });
    expect(r.summary.adapters.old.status).toBe(SOURCE_STATUS.STALE);
  });
  it('observationsBefore never returns rows first observed after the as-of time (no look-ahead)', async () => {
    const { env } = makeDb();
    await insertObservations(env, [
      { source: 's', series: 'x', bucket_ts: NOW - 3 * H, value: 1, observed_ts: NOW - 3 * H + 60_000 },
      { source: 's', series: 'x', bucket_ts: NOW - 2 * H, value: 2, observed_ts: NOW + H },
    ]);
    const rows = await observationsBefore(env, 's', 'x', { fromTs: 0, beforeTs: NOW, asOfTs: NOW });
    expect(rows.map((r) => r.value)).toEqual([1]);
  });
  it('expires events past expires_ts but keeps them', async () => {
    const { env } = makeDb();
    const a = scriptedAdapter('a', async ({ nowTs }) => ({ status: 'OK', observations: [], events: [await makeEvent({ ...evInput(NOW - 2 * H), detected_ts: nowTs })] }));
    await runEventDetection(env, { nowTs: NOW, adapters: [a] });
    await runEventDetection(env, { nowTs: NOW + 2 * D, adapters: [] });
    const evs = await listEvents(env, { sinceTs: 0 });
    expect(evs).toHaveLength(1);
    expect(evs[0].status).toBe('EXPIRED');
  });
  it('end-to-end OKX run on an empty store: observations stored, INSUFFICIENT_BASELINE, no invented event', async () => {
    const { env, db } = makeDb();
    const t = NOW - 30 * 60_000;
    const f = fakeFetch([
      [/instruments/, () => okxInst.clone()],
      [/after=/, () => okxPage([])],
      [/liquidation-orders/, () => okxPage([det(t, 'sell', 'long', 80000, 50000), det(NOW - 7 * H, 'buy', 'short', 80000, 10)])],
    ]);
    const r = await runEventDetection(env, { nowTs: NOW, adapters: [okxLiquidationsAdapter], fetchImpl: f, sleep: noSleep });
    expect(r.summary.adapters.okx_liquidations.status).toBe(SOURCE_STATUS.INSUFFICIENT_BASELINE);
    expect(db.prepare('SELECT COUNT(*) n FROM event_ledger').get().n).toBe(0);
    expect(db.prepare("SELECT COUNT(*) n FROM event_observations WHERE series='liq_total_usd'").get().n).toBeGreaterThan(0);
  });
  it('skips cleanly when migration 0023 is not applied', async () => {
    const { env } = makeDb({ withEvents: false });
    expect(await runEventDetection(env, { nowTs: NOW, adapters: DETECTORS })).toMatchObject({ ok: false, available: false });
  });
});

// ------------------------------------------------------------------ API through worker.fetch
describe('API routes', () => {
  const get = (env, path) => worker.fetch(new Request(`https://w.example${path}`), env);
  it('reports available:false (503) before the migration, with detectors NEVER_RUN', async () => {
    const { env } = makeDb({ withEvents: false });
    const r = await get(env, '/api/events');
    expect(r.status).toBe(503);
    expect(await r.json()).toMatchObject({ ok: false, available: false });
    const h = await (await get(env, '/api/events/health')).json();
    expect(h.detectors.every((d) => d.status === SOURCE_STATUS.NEVER_RUN)).toBe(true);
    expect(h.existing_sources.find((s) => s.id === 'gdelt_geo_shock').event_detector).toBe(false);
  });
  it('lists events with evidence, filters, one event with related events, and validates input', async () => {
    const { env } = makeDb();
    const mk = async (type, bucket) => makeEvent({ event_type: type, asset: 'BTC', source: 'okx_liquidations', source_ts: bucket, detected_ts: NOW, rule_id: 'r', dedup_parts: [type, bucket], correlation_key: 'stress|btc|1', evidence: [{ kind: 'bucket', v: 1 }] });
    const a = scriptedAdapter('okx_liquidations', async () => ({ status: 'OK', observations: [], events: [await mk('LIQUIDATION_BURST', NOW - H), await mk('VOL_EXPANSION', NOW - H)] }));
    await runEventDetection(env, { nowTs: NOW, adapters: [a] });
    const all = await (await get(env, `/api/events?since=${NOW - D}`)).json();
    expect(all).toMatchObject({ ok: true, available: true, count: 2 });
    expect(all.note).toMatch(/not V1 composite inputs/);
    expect(all.events[0].evidence[0].kind).toBe('bucket');
    const one = (await (await get(env, `/api/events?type=VOL_EXPANSION&since=0`)).json()).events;
    expect(one).toHaveLength(1);
    const detail = await (await get(env, `/api/event?id=${one[0].event_id}`)).json();
    expect(detail.event.related.map((x) => x.event_type)).toEqual(['LIQUIDATION_BURST']);
    expect((await get(env, '/api/event?id=nope')).status).toBe(400);
    expect((await get(env, '/api/event?id=ev_000000000000000000000000')).status).toBe(404);
    expect((await get(env, '/api/events?since=yesterday-ish')).status).toBe(400);
    const health = await (await get(env, '/api/events/health')).json();
    expect(health.detectors.find((d) => d.id === 'okx_liquidations').status).toBe('OK');
    expect(health.detectors.find((d) => d.id === 'binance_liquidation_sample').status).toBe(SOURCE_STATUS.DISABLED);
    expect(health.last_run.summary.adapters.okx_liquidations.events_new).toBe(2);
  });
});

// ------------------------------------------------------------------ scheduling, budgets and V1 separation
describe('scheduling: detection never shares the prediction cron invocation', () => {
  const src = readFileSync(join(__dirname, '..', 'worker.js'), 'utf8');
  it('the scheduled handler does not run event detection at all; wrangler.toml adds no cron and no flag', () => {
    const sched = src.slice(src.indexOf('async scheduled(event, env, ctx) {'));
    expect(sched.slice(0, sched.indexOf('\n  },'))).not.toMatch(/runEventDetection|EVENT_DETECTION|events\//);
    expect(src).not.toMatch(/runEventDetection/);
    const toml = readFileSync(join(__dirname, '..', 'wrangler.toml'), 'utf8');
    expect(toml).toMatch(/crons = \["0 \*\/3 \* \* \*", "0 7 \* \* \*"\]/);
    expect(toml).not.toMatch(/EVENT_/);
  });
  it('the scheduler template is inert, schedule-only, tokenless for GitHub and secret-scoped to one step', () => {
    const t = readFileSync(join(__dirname, '..', 'events', 'scheduler', 'event-detection-schedule.yml'), 'utf8').split('\n').map((l) => l.replace(/\s+#.*$|^#.*$/, '')).join('\n');
    expect(() => readFileSync(join(__dirname, '..', '.github', 'workflows', 'event-detection-schedule.yml'))).toThrow();
    expect(t).toMatch(/^on:\n  schedule:\n    - cron: '23 \* \* \* \*'/m);
    expect(t).not.toMatch(/workflow_dispatch|pull_request|push:|workflow_run/);
    expect(t).toMatch(/^permissions: \{\}$/m);
    expect(t).not.toMatch(/actions\/checkout|wrangler|CLOUDFLARE|github\.token|STAGE7_ADMIN_TOKEN/);
    expect(t.match(/secrets\./g)).toHaveLength(1);
    expect(t).toContain("-H 'X-Event-Trigger: schedule'");
    expect(t).toContain('https://pulseworker-v2.quiquandon.workers.dev/api/events/run');
  });
});

describe('per-invocation budgets (Workers Free: 50 subrequests, 50 D1 queries)', () => {
  it('the fetch budget fails only the adapter that exceeds it, with BUDGET_EXHAUSTED in health', async () => {
    const { env } = makeDb();
    const greedy = scriptedAdapter('greedy', async ({ http }) => { for (let i = 0; i < 10; i += 1) await http.json('https://x'); return { status: 'OK', observations: [], events: [] }; });
    const after = scriptedAdapter('after', async () => ({ status: 'OK', observations: [], events: [] }));
    const r = await runEventDetection(env, { nowTs: NOW, adapters: [greedy, after], fetchImpl: async () => jsonRes({}), limits: { fetches: 3, queries: 44 } });
    expect(r.summary.adapters.greedy.status).toBe(SOURCE_STATUS.BUDGET_EXHAUSTED);
    expect(r.summary.adapters.after.status).toBe('OK');
    expect(r.summary.budget.fetches).toBe(3);
  });
  it('the query budget keeps a reserve so every health row and the run record are still written', async () => {
    const { env, db } = makeDb();
    const hog = scriptedAdapter('hog', async ({ history }) => { for (let i = 0; i < 100; i += 1) await history('s', 0, NOW); return { status: 'OK', observations: [], events: [] }; });
    const b = scriptedAdapter('b', async () => ({ status: 'OK', observations: [], events: [] }));
    const r = await runEventDetection(env, { nowTs: NOW, adapters: [hog, b], limits: { fetches: 40, queries: 20 } });
    expect(r.summary.adapters.hog.status).toBe(SOURCE_STATUS.BUDGET_EXHAUSTED);
    expect(r.summary.adapters.b.status).toBe('OK');
    expect(r.summary.budget.queries).toBeLessThanOrEqual(20);
    expect(db.prepare('SELECT COUNT(*) n FROM event_source_health').get().n).toBe(2);
    expect(db.prepare('SELECT finished_ts FROM event_runs').get().finished_ts).toBeGreaterThan(0);
  });
  it('countedDb counts every executed statement', async () => {
    const { env } = makeDb();
    const budget = makeBudget({ fetches: 1, queries: 2 });
    const db = countedDb(env.DB, budget);
    await db.prepare('SELECT 1').all();
    await db.prepare('SELECT 1').bind().first();
    await expect(db.prepare('SELECT 1').all()).rejects.toMatchObject({ status: SOURCE_STATUS.BUDGET_EXHAUSTED });
  });
  it('multi-row inserts are chunked under the 100 bound-parameter cap and stay idempotent', async () => {
    const { env, db } = makeDb();
    const rows = Array.from({ length: 40 }, (_x, i) => ({ source: 's', series: 'x', bucket_ts: i, value: i, observed_ts: NOW }));
    expect(await insertObservations(env, rows)).toBe(40);
    expect(await insertObservations(env, rows)).toBe(0);
    const evs = await Promise.all([1, 2, 3, 4, 5].map((k) => makeEvent({ event_type: 'T', source: 's', source_ts: NOW - k, detected_ts: NOW, rule_id: 'r', dedup_parts: ['T', k] })));
    expect(await insertEvents(env, evs, NOW)).toBe(5);
    expect(await insertEvents(env, evs, NOW + 1)).toBe(0);
    expect(db.prepare('SELECT COUNT(*) n FROM event_ledger').get().n).toBe(5);
    await markDocuments(env, [{ source: 'sec', doc_id: 'a', processed_ts: NOW, findings: 0 }]);
    expect([...(await processedDocuments(env, 'sec', ['a', 'b']))]).toEqual(['a']);
  });
  it('a full run of every real adapter (mocked providers, SEC configured) stays within both limits', async () => {
    const { env } = makeDb();
    const t = NOW - 30 * 60_000;
    const csv = (sid) => `observation_date,${sid}\n` + Array.from({ length: 200 }, (_x, i) => `${new Date(NOW - (200 - i) * D).toISOString().slice(0, 10)},${4 + (i % 2) * 0.02}`).join('\n');
    const sub = (cik) => jsonRes({ name: 'Co', tickers: [{ 1050446: 'MSTR', 1507605: 'MARA', 1167419: 'RIOT', 1679788: 'COIN', 1318605: 'TSLA', 1512673: 'XYZ', 1326380: 'GME', 1554859: 'SMLR' }[cik]],
      filings: { recent: { accessionNumber: [`${cik}-26-1`, `${cik}-26-2`], form: ['8-K', '8-K'], filingDate: ['2026-10-14', '2026-10-15'], acceptanceDateTime: [new Date(NOW - D).toISOString(), new Date(NOW - 2 * D).toISOString()], primaryDocument: ['a.htm', 'b.htm'], items: ['8.01', '8.01'] } } });
    const f = fakeFetch([
      [/instruments/, () => okxInst.clone()],
      [/after=/, () => okxPage([])],
      [/liquidation-orders/, () => okxPage([det(t, 'sell', 'long', 80000, 500)])],
      [/get_volatility_index_data/, () => jsonRes({ result: { data: dvolRows(744, (i) => 40 + Math.sin(i / 5)), continuation: null } })],
      [/get_book_summary_by_currency\?currency=BTC/, () => jsonRes({ result: [{ instrument_name: 'BTC-19OCT26-71000-C', open_interest: 1, creation_timestamp: NOW - 1000 }] })],
      [/get_book_summary_by_currency\?currency=ETH/, () => jsonRes({ result: [{ instrument_name: 'ETH-19OCT26-3000-C', open_interest: 1, creation_timestamp: NOW - 1000 }] })],
      [/fredgraph\.csv\?id=DGS2&/, () => textRes(csv('DGS2'))],
      [/fredgraph\.csv\?id=DGS10&/, () => textRes(csv('DGS10'))],
      [/CIK0*(\d+)\.json/, (u) => sub(Number(/CIK0*(\d+)\.json/.exec(u)[1]))],
      [/Archives/, () => textRes('<p>The Company acquired 10 bitcoins.</p>')],
    ]);
    const env2 = { ...env, EVENT_SEC_USER_AGENT: 'CryptoPulse research owner@example.com' };
    const r1 = await runEventDetection(env2, { nowTs: NOW, fetchImpl: f, sleep: noSleep });
    expect(r1.summary.budget.queries).toBeLessThanOrEqual(44);
    expect(r1.summary.budget.fetches).toBeLessThanOrEqual(40);
    expect(Object.values(r1.summary.adapters).some((a) => a.status === SOURCE_STATUS.BUDGET_EXHAUSTED)).toBe(false);
    const firstSecFetches = f.calls.filter((u) => /Archives/.test(u)).length;
    expect(firstSecFetches).toBe(6);                                   // capped per run
    const r2 = await runEventDetection(env2, { nowTs: NOW + 3 * H, fetchImpl: f, sleep: noSleep });
    expect(f.calls.filter((u) => /Archives/.test(u)).length - firstSecFetches).toBe(6);   // only the 10 deferred ones remain, 6 more read
    const r3 = await runEventDetection(env2, { nowTs: NOW + 6 * H, fetchImpl: f, sleep: noSleep });
    expect(f.calls.filter((u) => /Archives/.test(u)).length - firstSecFetches).toBe(10);  // already-classified filings are never re-read
    expect(r3.summary.budget.queries).toBeLessThanOrEqual(44);
    expect(r2.summary.adapters.sec_edgar.status).toBe('OK');
  });
  it('worst case: every provider at its page cap (OKX 8 pages, DVOL continuation, FRED key, 8 SEC filers) fits 40 fetches', async () => {
    const { env } = makeDb();
    const apiObs = Array.from({ length: 200 }, (_x, i) => ({ date: new Date(NOW - (200 - i) * D).toISOString().slice(0, 10), value: String(4 + (i % 2) * 0.02) }));
    const sub = (cik) => jsonRes({ name: 'Co', tickers: [{ 1050446: 'MSTR', 1507605: 'MARA', 1167419: 'RIOT', 1679788: 'COIN', 1318605: 'TSLA', 1512673: 'XYZ', 1326380: 'GME', 1554859: 'SMLR' }[cik]],
      filings: { recent: { accessionNumber: [`${cik}-26-1`], form: ['8-K'], filingDate: ['2026-10-15'], acceptanceDateTime: [new Date(NOW - D).toISOString()], primaryDocument: ['a.htm'], items: ['8.01'] } } });
    const f = fakeFetch([
      [/instruments/, () => okxInst.clone()],
      // every page is older than the cursor but never reaches the 8 h window start -> all 8 pages are read
      [/liquidation-orders/, (u) => { const c = /after=(\d+)/.exec(u); const top = c ? Number(c[1]) - 1 : NOW - 1000; return okxPage([det(top, 'sell', 'long', 80000, 1), det(top - 60_000, 'buy', 'short', 80000, 1)]); }],
      // DVOL never completes: continuation always moves one hour back
      [/get_volatility_index_data/, (u) => { const end = Number(/end_timestamp=(\d+)/.exec(u)[1]); return jsonRes({ result: { data: [[Math.floor((end - H) / H) * H, 1, 1, 1, 40]], continuation: end - H } }); }],
      [/get_book_summary_by_currency\?currency=BTC/, () => jsonRes({ result: [{ instrument_name: 'BTC-19OCT26-71000-C', open_interest: 1, creation_timestamp: NOW - 1000 }] })],
      [/get_book_summary_by_currency\?currency=ETH/, () => jsonRes({ result: [{ instrument_name: 'ETH-19OCT26-3000-C', open_interest: 1, creation_timestamp: NOW - 1000 }] })],
      [/series\/observations/, () => jsonRes({ observations: apiObs })],
      [/releases\/dates/, () => jsonRes({ release_dates: [] })],
      [/CIK0*(\d+)\.json/, (u) => sub(Number(/CIK0*(\d+)\.json/.exec(u)[1]))],
      [/Archives/, () => textRes('<p>The Company acquired 10 bitcoins.</p>')],
    ]);
    const env2 = { ...env, EVENT_SEC_USER_AGENT: 'CryptoPulse research owner@example.com', FRED_API_KEY: 'k'.repeat(32) };
    const r = await runEventDetection(env2, { nowTs: NOW, fetchImpl: f, sleep: noSleep });
    const n = (re) => f.calls.filter((u) => re.test(u)).length;
    expect(n(/liquidation-orders/)).toBe(LIQ_CONFIG.maxPages);
    expect(n(/get_volatility_index_data/)).toBe(2 * DERIBIT_CONFIG.dvolMaxPages);
    expect(n(/fred/)).toBe(3);
    expect(n(/CIK0*\d+\.json/)).toBe(8);
    expect(n(/Archives/)).toBe(6);
    expect(f.calls.length).toBe(1 + 8 + 8 + 2 + 3 + 8 + 6);           // 36: worst case without retries
    expect(r.summary.budget.fetches).toBe(f.calls.length);
    expect(r.summary.budget.fetches).toBeLessThanOrEqual(40);
    expect(r.summary.budget.queries).toBeLessThanOrEqual(44);
    expect(Object.values(r.summary.adapters).some((a) => a.status === SOURCE_STATUS.BUDGET_EXHAUSTED)).toBe(false);
  });
  it('per-detector allocation sums to the hard limit and covers each detector\'s own logical maximum', () => {
    expect(Object.values(FETCH_ALLOCATION).reduce((a, b) => a + b, 0)).toBe(DEFAULT_BUDGET.fetches);
    expect(Object.keys(FETCH_ALLOCATION).sort()).toEqual(DETECTORS.map((d) => d.id).sort());
    expect(FETCH_ALLOCATION.okx_liquidations).toBeGreaterThanOrEqual(1 + LIQ_CONFIG.maxPages);
    expect(FETCH_ALLOCATION.deribit_options).toBeGreaterThanOrEqual(2 * DERIBIT_CONFIG.dvolMaxPages + 2);
    expect(FETCH_ALLOCATION.macro_fred).toBeGreaterThanOrEqual(MACRO_CONFIG.yieldSeries.length + 1);
    expect(FETCH_ALLOCATION.sec_edgar).toBeGreaterThanOrEqual(8 + 6);
    const parent = makeBudget({ fetches: 5, queries: 9 });
    parent.fetches = 3;
    const c = childBudget(parent, 10);
    expect(c.fetchLimit).toBe(2);                                         // capped by what the parent has left
    c.fetches += 1;
    expect([c.fetches, parent.fetches]).toEqual([1, 4]);                  // spending the share spends the parent
  });
  it('worst case with every provider failing on every attempt: <= 40 subrequests, no detector exceeds its share, nothing stored', async () => {
    const { env, db } = makeDb();
    const env2 = { ...env, EVENT_SEC_USER_AGENT: 'CryptoPulse research owner@example.com', FRED_API_KEY: 'k'.repeat(32), EVENT_BINANCE_SAMPLE_MS: '1000' };
    const f = fakeFetch([[/./, () => new Response('upstream down', { status: 503 })]]);
    const per = {};
    const counting = async (url, init) => { const u = String(url); const k = /okx/.test(u) ? 'okx_liquidations' : /deribit/.test(u) ? 'deribit_options' : /stlouisfed/.test(u) ? 'macro_fred' : /sec\.gov/.test(u) ? 'sec_edgar' : 'other'; per[k] = (per[k] || 0) + 1; return f(url, init); };
    const r = await runEventDetection(env2, { nowTs: NOW, fetchImpl: counting, sleep: noSleep, deps: { binance_liquidation_sample: { connect: async () => { throw new Error('ws refused'); } } } });
    expect(r.summary.budget.fetches).toBeLessThanOrEqual(DEFAULT_BUDGET.fetches);
    for (const [id, n] of Object.entries(per)) if (id !== 'other') expect(n).toBeLessThanOrEqual(FETCH_ALLOCATION[id]);
    expect(per.sec_edgar).toBeGreaterThan(0);                              // the last detector still got its share
    expect(Object.values(r.summary.adapters).every((a) => [SOURCE_STATUS.PROVIDER_ERROR, SOURCE_STATUS.BUDGET_EXHAUSTED, SOURCE_STATUS.OK].includes(a.status))).toBe(true);
    expect(r.summary.adapters.okx_liquidations.status).toBe(SOURCE_STATUS.PROVIDER_ERROR);
    expect(db.prepare('SELECT COUNT(*) n FROM event_observations').get().n).toBe(0);   // no partial coverage recorded
    expect(db.prepare('SELECT COUNT(*) n FROM event_source_health').get().n).toBe(DETECTORS.length);
  });
  it('FRED failing on both series with retries uses only its share; SEC (after it) still completes', async () => {
    const { env } = makeDb();
    const sub = () => jsonRes({ name: 'Co', tickers: ['X'], filings: { recent: { accessionNumber: [], form: [], filingDate: [], acceptanceDateTime: [], primaryDocument: [], items: [] } } });
    const per = {};
    const f = async (url) => { const u = String(url); const k = /stlouisfed/.test(u) ? 'fred' : /sec\.gov/.test(u) ? 'sec' : 'other'; per[k] = (per[k] || 0) + 1;
      if (k === 'fred') return new Response('error code: 520', { status: 520, headers: { server: 'cloudflare', 'cf-ray': 'x' } });
      if (k === 'sec') return sub();
      return new Response('nope', { status: 404 }); };
    const r = await runEventDetection({ ...env, EVENT_SEC_USER_AGENT: 'CryptoPulse research owner@example.com' }, { nowTs: NOW, fetchImpl: f, sleep: noSleep, adapters: [macroAdapter, secEdgarAdapter] });
    expect(per.fred).toBeLessThanOrEqual(FETCH_ALLOCATION.macro_fred);
    expect(r.summary.adapters.macro_fred).toMatchObject({ status: SOURCE_STATUS.PROVIDER_ERROR, error: expect.stringMatching(/^HTTP 520 \(server=cloudflare cf-ray=yes/) });
    expect(per.sec).toBe(8);
    expect(r.summary.adapters.sec_edgar.status).not.toBe(SOURCE_STATUS.BUDGET_EXHAUSTED);
  });
  it('a budget exhausted in the middle of OKX paging stores no OKX observations (never partial coverage as COMPLETE)', async () => {
    const { env, db } = makeDb();
    const page = (u) => { const m = /after=(\d+)/.exec(u); const top = m ? Number(m[1]) - 1 : NOW - 1000; return okxPage(Array.from({ length: 100 }, (_x, i) => det(top - i * 1000, 'sell', 'long', 80000, 1))); };
    const f = fakeFetch([[/instruments/, () => okxInst.clone()], [/liquidation-orders/, page]]);
    const r = await runEventDetection(env, { nowTs: NOW, fetchImpl: f, sleep: noSleep, adapters: [okxLiquidationsAdapter], limits: { fetches: 40, queries: 44, allocation: { okx_liquidations: 4 } } });
    expect(r.summary.adapters.okx_liquidations.status).toBe(SOURCE_STATUS.BUDGET_EXHAUSTED);
    expect(f.calls.length).toBe(4);
    expect(db.prepare('SELECT COUNT(*) n FROM event_observations').get().n).toBe(0);
  });
  it('the Binance WebSocket upgrade is charged to the budget when the sample is enabled', async () => {
    const { env } = makeDb();
    const conn = async () => { const l = {}; setTimeout(() => l.close && l.close(), 5); return { addEventListener: (k, fn) => { l[k] = fn; }, close: () => {} }; };
    const r = await runEventDetection({ ...env, EVENT_BINANCE_SAMPLE_MS: '10' }, { nowTs: NOW, adapters: [binanceLiquidationSampleAdapter], deps: { binance_liquidation_sample: { connect: conn } } });
    expect(r.summary.budget.fetches).toBe(1);
  });
  it('credentials are scrubbed from error text before it reaches health rows or the public API', async () => {
    expect(scrubSecrets('GET https://api.stlouisfed.org/fred/x?series_id=DGS2&api_key=SECRET123&file_type=json failed')).toBe('GET https://api.stlouisfed.org/fred/x?series_id=DGS2&api_key=***&file_type=json failed');
    const { env, db } = makeDb();
    const leaky = async () => { throw new TypeError('fetch failed for https://api.stlouisfed.org/fred/series/observations?api_key=SECRET123'); };
    const r = await runEventDetection({ ...env, FRED_API_KEY: 'SECRET123'.padEnd(32, 'x') }, { nowTs: NOW, fetchImpl: leaky, sleep: noSleep, adapters: [macroAdapter] });
    expect(JSON.stringify(r)).not.toMatch(/SECRET123/);
    expect(db.prepare('SELECT last_error FROM event_source_health').get().last_error).not.toMatch(/SECRET123/);
  });
  it('OKX: the next run starts after the last stored bucket (fewer pages), never before the lookback', async () => {
    const { env } = makeDb();
    const hist = Array.from({ length: 30 }, (_x, i) => ({ source: 'okx_liquidations', series: 'liq_total_usd', bucket_ts: Math.floor(NOW / H) * H - (31 - i) * H, value: 1, observed_ts: NOW - 2 * H }));
    await insertObservations(env, hist);
    const urls = [];
    const f = fakeFetch([[/instruments/, () => okxInst.clone()], [/liquidation-orders/, (u) => { urls.push(u); return okxPage([det(NOW - 3 * H, 'sell', 'long', 80000, 10)]); }]]);
    const r = await runEventDetection(env, { nowTs: NOW, adapters: [okxLiquidationsAdapter], fetchImpl: f, sleep: noSleep });
    expect(r.summary.adapters.okx_liquidations.status).toBe(SOURCE_STATUS.INSUFFICIENT_BASELINE);
    const health = (await env.DB.prepare("SELECT details_json FROM event_source_health WHERE source='okx_liquidations'").first());
    expect(JSON.parse(health.details_json).window_start).toBe(new Date(NOW - 2 * H).toISOString());
  });
});

describe('POST /api/events/run authorisation and the schedule gate', () => {
  const post = (env, headers = {}) => worker.fetch(new Request('https://w.example/api/events/run', { method: 'POST', headers }), env);
  const TOKEN = 'run-token-0123456789abcdef';
  it('503 without any token configured, 401 for a wrong or short token', async () => {
    const { env } = makeDb();
    expect((await post(env)).status).toBe(503);
    expect((await post({ ...env, EVENT_RUN_TOKEN: 'short' }, { Authorization: 'Bearer short' })).status).toBe(503);
    expect((await post({ ...env, EVENT_RUN_TOKEN: TOKEN }, { Authorization: 'Bearer nope' })).status).toBe(401);
  });
  it('a scheduled trigger is refused (409 DISABLED) unless EVENT_DETECTION_ENABLED is "1"; a manual owner run is allowed', async () => {
    const { env, db } = makeDb();
    vi.stubGlobal('fetch', async () => new Response('blocked in tests', { status: 404 }));
    try {
      const e = { ...env, EVENT_RUN_TOKEN: TOKEN };
      const s1 = await post(e, { Authorization: `Bearer ${TOKEN}`, 'X-Event-Trigger': 'schedule' });
      expect(s1.status).toBe(409);
      expect((await s1.json()).status).toBe(SOURCE_STATUS.DISABLED);
      expect(db.prepare('SELECT COUNT(*) n FROM event_runs').get().n).toBe(0);
      const m = await post(e, { Authorization: `Bearer ${TOKEN}` });
      expect(m.status).toBe(200);
      const body = await m.json();
      expect(body.summary.trigger).toBe('manual');
      expect(body.summary.adapters.okx_liquidations.status).toBe(SOURCE_STATUS.PROVIDER_ERROR);   // providers unreachable -> fail closed
      expect(body.summary.adapters.sec_edgar.status).toBe(SOURCE_STATUS.NOT_CONFIGURED);
      const again = await post(e, { Authorization: `Bearer ${TOKEN}` });
      expect(again.status).toBe(409);                                                            // overlap guard
      db.exec('DELETE FROM event_runs');
      const s2 = await post({ ...e, EVENT_DETECTION_ENABLED: '1' }, { Authorization: `Bearer ${TOKEN}`, 'X-Event-Trigger': 'schedule' });
      expect(s2.status).toBe(200);
      expect((await s2.json()).summary.trigger).toBe('schedule');
    } finally { vi.unstubAllGlobals(); }
  });
});

describe('evaluation metrics (detection vs early warning; never "prediction")', () => {
  const ep = (id, onset, dir) => ({ id, onset_ts: onset, end_ts: onset + 12 * H, direction: dir });
  const ev = (id, t, dir = 'DOWN', type = 'LIQUIDATION_BURST') => ({ event_id: id, detected_ts: t, direction: dir, event_type: type });
  const W = { start_ts: NOW - 30 * D, end_ts: NOW };
  it('computes recall, precision, false positives, latency and early-warning vs detection from detected_ts only', () => {
    const r = evaluateDetections({
      events: [ev('a', NOW - 20 * D - 2 * H), ev('b', NOW - 10 * D + 3 * H, 'UP'), ev('c', NOW - 5 * D), { ...ev('d', NOW - 40 * D) }],
      episodes: [ep('E1', NOW - 20 * D, 'DOWN'), ep('E2', NOW - 10 * D, 'DOWN'), ep('E3', NOW - 2 * D, 'UP')],
      window: W, leadMs: 6 * H, lagMs: 6 * H,
    });
    expect(r).toMatchObject({ episodes_watched: 3, events_total: 3, early_warnings: 1, detections_after_onset: 1 });
    expect(r.recall).toBeCloseTo(2 / 3);
    expect(r.precision).toBeCloseTo(2 / 3);
    expect(r.false_positives_per_30d).toBeCloseTo(1);
    expect(r.matches.find((m) => m.episode === 'E1')).toMatchObject({ kind: 'EARLY_WARNING', latency_ms: -2 * H, direction_agreement: 1 });
    expect(r.matches.find((m) => m.episode === 'E2')).toMatchObject({ kind: 'DETECTION', latency_ms: 3 * H, direction_agreement: 0 });
    expect(r.caveat).toMatch(/Not evidence of predictive value/);
  });
  it('excludes episodes the detector was not watching from recall', () => {
    const r = evaluateDetections({ events: [], episodes: [ep('E1', NOW - 20 * D, 'UP')], window: W, leadMs: 0, lagMs: 0, coverage: [{ start_ts: NOW - 5 * D, end_ts: NOW }] });
    expect(r).toMatchObject({ episodes_total: 1, episodes_watched: 0, episodes_unwatched: 1, recall: null });
  });
  it('rejects an invalid window', () => {
    expect(() => evaluateDetections({ events: [], episodes: [], window: { start_ts: 2, end_ts: 1 }, leadMs: 0, lagMs: 0 })).toThrow();
  });
});

describe('separation from V1 scoring', () => {
  it('the events modules never touch V1 scoring, weights, sentiment or prediction tables', () => {
    for (const f of ['event-core.js', 'event-store.js', 'event-runner.js', 'event-api.js', 'event-eval.js', 'adapters/liquidations.js', 'adapters/deribit.js', 'adapters/macro.js', 'adapters/sec-edgar.js']) {
      const s = readFileSync(join(__dirname, '..', 'events', f), 'utf8');
      expect(s).not.toMatch(/learning-core|v1Composite|V1_METHODOLOGY|sources_json|\bhistory\b\s+(SET|WHERE)|INSERT INTO (history|predictions|sentiment)|UPDATE (history|predictions)/);
    }
  });
});
