// Liquidation velocity. Two adapters, one normalised shape; kept separate from funding and hloi.
//
// 1) OKX (primary, REST, pollable): GET /api/v5/public/liquidation-orders?instType=SWAP&instFamily=BTC-USDT&state=filled
//    Newest first; `after` returns records older than the cursor (OKX v5 pagination convention, the same one the
//    EXP-23 OKX funding collector verified). Contract size is read live from /api/v5/public/instruments (ctVal), never
//    assumed. Coverage is ONE venue and ONE instrument family -- never total-market liquidations.
// 2) Binance USDS-M (secondary, WebSocket only): stream `!forceOrder@arr`. Binance documents it as a snapshot that pushes
//    only the latest liquidation per symbol in each 1000 ms window, so it under-counts bursts by construction. There is
//    no public REST history. It is therefore stored as coverage SAMPLED, is excluded from every baseline, and produces no
//    events. Disabled unless EVENT_BINANCE_SAMPLE_MS is set (Binance also answers HTTP 451 to some regions).
import { SOURCE_STATUS, SourceError, robustBaseline, severityFor, clamp01, makeEvent, stressCorrelationKey } from '../event-core.js';

const H = 3600_000;
export const OKX_BASE = 'https://www.okx.com';
export const OKX_LIQ_PATH = '/api/v5/public/liquidation-orders';

export const LIQ_CONFIG = {
  instFamily: 'BTC-USDT',
  instId: 'BTC-USDT-SWAP',
  asset: 'BTC',
  bucketMs: H,
  lookbackMs: 8 * H,          // longest window one run re-reads; normally it starts after the last stored bucket
  minLookbackMs: 2 * H,        // always re-read at least this much (overlaps are idempotent)
  settleMs: 5 * 60_000,        // a bucket is evaluated only 5 min after it ends
  maxPages: 8,                 // bounded subrequests per run (Workers Free: 50 per invocation)
  baselineBuckets: 168,        // 7 days of hourly buckets
  minBaselineBuckets: 48,
  k: 4,                        // robust z threshold
  multipleOfMedian: 3,
  minAbsUsd: 1_000_000,
  dominantShare: 0.65,
};

function okxCheck(payload) {
  if (!payload || typeof payload !== 'object' || payload.code === undefined) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'OKX: malformed payload');
  if (String(payload.code) === '50011' || String(payload.code) === '50061') throw new SourceError(SOURCE_STATUS.RATE_LIMITED, `OKX code ${payload.code}`);
  if (String(payload.code) !== '0') throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, `OKX code ${payload.code}: ${String(payload.msg || '').slice(0, 120)}`);
  if (!Array.isArray(payload.data)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'OKX: data is not a list');
  return payload.data;
}

// One OKX liquidation detail -> normalised record. Throws on anything malformed (never guessed).
export function normalizeOkxDetail(d, ctVal, instId) {
  const ts = Number(d.ts), px = Number(d.bkPx), sz = Number(d.sz);
  if (!Number.isInteger(ts) || ts < 1_000_000_000_000) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'OKX: bad ts');
  if (!(px > 0) || !(sz > 0)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'OKX: bad bkPx/sz');
  if (!['buy', 'sell'].includes(d.side)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'OKX: bad side');
  // The liquidated position: posSide long/short when the account is in long/short mode; in net mode a forced
  // SELL closes a long and a forced BUY closes a short.
  const liquidated = d.posSide === 'long' || d.posSide === 'short' ? d.posSide : (d.side === 'sell' ? 'long' : 'short');
  return {
    venue: 'okx', instrument: instId, ts, side: d.side, pos_side: d.posSide || 'net', liquidated,
    price: px, contracts: sz, qty_base: sz * ctVal, notional_usd: sz * ctVal * px,
    key: `${ts}|${d.side}|${d.posSide || 'net'}|${d.bkPx}|${d.sz}`,
  };
}

export async function fetchOkxCtVal(http, instId) {
  const data = okxCheck(await http.json(`${OKX_BASE}/api/v5/public/instruments?instType=SWAP&instId=${encodeURIComponent(instId)}`));
  const inst = data.find((x) => x.instId === instId);
  const ctVal = inst ? Number(inst.ctVal) : NaN;
  if (!(ctVal > 0) || !inst || inst.ctValCcy !== 'BTC') throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'OKX: ctVal/ctValCcy unavailable');
  return ctVal;
}

// Pages backwards from now until the window start is covered. Every page must be strictly older than the cursor
// (fails closed otherwise). Duplicates across pages are counted once.
export async function fetchOkxLiquidations(http, { instFamily, instId, startTs, ctVal, maxPages }) {
  const seen = new Map();
  let cursor = null, pages = 0, oldest = null, reachedStart = false, exhausted = false;
  while (pages < maxPages) {
    const q = `instType=SWAP&instFamily=${encodeURIComponent(instFamily)}&state=filled&limit=100${cursor ? `&after=${cursor}` : ''}`;
    const data = okxCheck(await http.json(`${OKX_BASE}${OKX_LIQ_PATH}?${q}`));
    pages += 1;
    const recs = [];
    for (const group of data) {
      if (group.instId && group.instId !== instId) continue;
      for (const d of group.details || []) recs.push(normalizeOkxDetail(d, ctVal, instId));
    }
    if (!recs.length) { exhausted = true; break; }
    const pageOldest = Math.min(...recs.map((r) => r.ts));
    if (cursor !== null && Math.max(...recs.map((r) => r.ts)) > cursor) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'OKX pagination: page not older than cursor');
    for (const r of recs) if (!seen.has(r.key)) seen.set(r.key, r);
    oldest = oldest === null ? pageOldest : Math.min(oldest, pageOldest);
    if (pageOldest <= startTs) { reachedStart = true; break; }
    if (cursor !== null && pageOldest >= cursor) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'OKX pagination: cursor did not advance');
    cursor = pageOldest;
  }
  return { records: [...seen.values()], pages, oldest, reachedStart, exhausted };
}

// Aggregates records into fixed buckets. A bucket is COMPLETE only when the fetched data provably covers it
// (its start is at or after the oldest record we know the history reached) and it ended settleMs before now.
export function bucketize(records, { bucketMs, coveredFromTs, nowTs, settleMs }) {
  const buckets = new Map();
  for (const r of records) {
    const b = Math.floor(r.ts / bucketMs) * bucketMs;
    if (!buckets.has(b)) buckets.set(b, { bucket_ts: b, long_usd: 0, short_usd: 0, count: 0 });
    const x = buckets.get(b);
    x[r.liquidated === 'long' ? 'long_usd' : 'short_usd'] += r.notional_usd;
    x.count += 1;
  }
  const out = [];
  const first = Math.ceil(coveredFromTs / bucketMs) * bucketMs;
  for (let b = first; b + bucketMs + settleMs <= nowTs; b += bucketMs) {
    out.push(buckets.get(b) || { bucket_ts: b, long_usd: 0, short_usd: 0, count: 0 });
  }
  return out;
}

export function liquidationDirection(longUsd, shortUsd, dominantShare) {
  const total = longUsd + shortUsd;
  if (!(total > 0)) return 'UNKNOWN';
  if (longUsd / total >= dominantShare) return 'DOWN';   // forced selling of liquidated longs
  if (shortUsd / total >= dominantShare) return 'UP';    // forced buying of liquidated shorts
  return 'MIXED';
}

// Rule ed-liq-burst-v1: total liquidation notional in one complete bucket vs a robust baseline of prior buckets.
export async function evaluateLiquidationBucket(bucket, history, cfg, { nowTs, source = 'okx_liquidations', sourceUrl }) {
  const prior = history.filter((h) => h.bucket_ts < bucket.bucket_ts).slice(-cfg.baselineBuckets).map((h) => h.value);
  const base = robustBaseline(prior, { minCount: cfg.minBaselineBuckets });
  if (!base) return { status: SOURCE_STATUS.INSUFFICIENT_BASELINE, baselineCount: prior.length, event: null };
  const total = bucket.long_usd + bucket.short_usd;
  const threshold = Math.max(cfg.minAbsUsd, base.median + cfg.k * base.scale, cfg.multipleOfMedian * base.median);
  if (!(total >= threshold)) return { status: SOURCE_STATUS.OK, baselineCount: prior.length, event: null };
  const ratio = total / threshold;
  const direction = liquidationDirection(bucket.long_usd, bucket.short_usd, cfg.dominantShare);
  const end = bucket.bucket_ts + cfg.bucketMs;
  // Confidence describes data quality (complete bucket, baseline depth, single venue), NOT predictive value.
  const confidence = clamp01(0.4 + 0.3 * Math.min(1, prior.length / cfg.baselineBuckets));
  const event = await makeEvent({
    event_type: 'LIQUIDATION_BURST', asset: cfg.asset, market: `${cfg.instId} (OKX USDT-margined perpetual)`, scope: 'single venue',
    source, source_url: sourceUrl, source_ts: end, detected_ts: nowTs, direction, severity: severityFor(ratio), confidence,
    rule_id: 'ed-liq-burst-v1', observed_value: total, baseline_value: base.median, threshold_value: threshold,
    unit: 'USD notional per hour (bankruptcy price x size)',
    evidence: [{ kind: 'bucket', bucket_start: bucket.bucket_ts, bucket_end: end, long_liquidated_usd: round(bucket.long_usd), short_liquidated_usd: round(bucket.short_usd), orders: bucket.count, baseline_buckets: prior.length, baseline_median_usd: round(base.median), baseline_scale_usd: round(base.scale) }],
    explanation: `OKX ${cfg.instId} liquidations of $${fmt(total)} in the hour ending ${new Date(end).toISOString()} exceeded the threshold $${fmt(threshold)} (max of $${fmt(cfg.minAbsUsd)}, median + ${cfg.k}x robust scale, ${cfg.multipleOfMedian}x median over ${prior.length} prior hours). ${direction === 'DOWN' ? 'Mostly long positions were liquidated (forced selling).' : direction === 'UP' ? 'Mostly short positions were liquidated (forced buying).' : direction === 'MIXED' ? 'Long and short liquidations were both substantial.' : ''} One venue only; not total-market liquidations.`,
    dedup_parts: ['LIQUIDATION_BURST', cfg.asset, source, bucket.bucket_ts],
    expires_ts: end + 24 * H,
    correlation_key: stressCorrelationKey(cfg.asset, end),
  });
  return { status: SOURCE_STATUS.OK, baselineCount: prior.length, event };
}

// Stored rows win over this run's rows for the same bucket (first write wins, as in the table).
export function mergeHistory(stored, runRows) {
  const byTs = new Map(runRows.map((r) => [r.bucket_ts, r]));
  for (const r of stored) byTs.set(r.bucket_ts, r);
  return [...byTs.values()].sort((a, b) => a.bucket_ts - b.bucket_ts);
}

function round(x) { return Math.round(x * 100) / 100; }
function fmt(x) { return x >= 1e6 ? (x / 1e6).toFixed(2) + 'M' : x >= 1e3 ? (x / 1e3).toFixed(1) + 'k' : x.toFixed(0); }

export const okxLiquidationsAdapter = {
  id: 'okx_liquidations',
  label: 'OKX BTC-USDT perpetual liquidations',
  kind: 'liquidations',
  freshnessLimitMs: 4 * H,
  async run({ http, nowTs, history, config }) {
    const cfg = { ...LIQ_CONFIG, ...(config || {}) };
    // ONE history read per run (baseline window + lookback), sliced per bucket below.
    const stored = await history('liq_total_usd', nowTs - cfg.lookbackMs - cfg.baselineBuckets * cfg.bucketMs, nowTs);
    const lastStored = stored.length ? stored[stored.length - 1].bucket_ts : null;
    const startTs = Math.max(nowTs - cfg.lookbackMs, Math.min(lastStored !== null ? lastStored + cfg.bucketMs : -Infinity, nowTs - cfg.minLookbackMs));
    const ctVal = await fetchOkxCtVal(http, cfg.instId);
    const res = await fetchOkxLiquidations(http, { instFamily: cfg.instFamily, instId: cfg.instId, startTs, ctVal, maxPages: cfg.maxPages });
    // History is provably covered only back to the oldest record when the window start was not reached.
    const coveredFromTs = res.reachedStart ? startTs : (res.oldest === null ? nowTs : res.oldest);
    const buckets = bucketize(res.records, { bucketMs: cfg.bucketMs, coveredFromTs, nowTs, settleMs: cfg.settleMs });
    const observations = [];
    for (const b of buckets) {
      observations.push({ source: this.id, series: 'liq_total_usd', bucket_ts: b.bucket_ts, value: b.long_usd + b.short_usd, unit: 'USD/hour', coverage: 'COMPLETE', observed_ts: nowTs });
      observations.push({ source: this.id, series: 'liq_long_usd', bucket_ts: b.bucket_ts, value: b.long_usd, unit: 'USD/hour', coverage: 'COMPLETE', observed_ts: nowTs });
      observations.push({ source: this.id, series: 'liq_short_usd', bucket_ts: b.bucket_ts, value: b.short_usd, unit: 'USD/hour', coverage: 'COMPLETE', observed_ts: nowTs });
    }
    const sourceUrl = `${OKX_BASE}${OKX_LIQ_PATH}?instType=SWAP&instFamily=${cfg.instFamily}&state=filled`;
    const events = [];
    let status = buckets.length ? SOURCE_STATUS.OK : SOURCE_STATUS.INSUFFICIENT_BASELINE;
    let baselineCount = 0;
    const runRows = buckets.map((x) => ({ bucket_ts: x.bucket_ts, value: x.long_usd + x.short_usd }));
    for (const b of buckets) {
      const from = b.bucket_ts - cfg.baselineBuckets * cfg.bucketMs;
      const hist = mergeHistory(stored.filter((r) => r.bucket_ts >= from && r.bucket_ts < b.bucket_ts), runRows.filter((r) => r.bucket_ts >= from && r.bucket_ts < b.bucket_ts));
      const r = await evaluateLiquidationBucket(b, hist, cfg, { nowTs, source: this.id, sourceUrl });
      baselineCount = r.baselineCount;
      if (r.status === SOURCE_STATUS.INSUFFICIENT_BASELINE) status = SOURCE_STATUS.INSUFFICIENT_BASELINE;
      if (r.event) events.push(r.event);
    }
    const lastRecordTs = res.records.length ? Math.max(...res.records.map((r) => r.ts)) : null;
    return {
      status, observations, events,
      coverage: `single venue (OKX ${cfg.instId}); ${res.reachedStart ? 'window fully covered' : 'history reached only ' + (res.oldest ? new Date(res.oldest).toISOString() : 'no records')}`,
      last_observation_ts: lastRecordTs,
      details: { window_start: new Date(startTs).toISOString(), pages: res.pages, records: res.records.length, reached_window_start: res.reachedStart, complete_buckets: buckets.length, baseline_buckets: baselineCount, ct_val: ctVal, requests: { ...http.counters } },
    };
  },
};

// ---- Binance USDS-M `!forceOrder@arr` (sampled stream) ----
export const BINANCE_FORCE_ORDER_URL = 'https://fstream.binance.com/ws/!forceOrder@arr';

export function normalizeBinanceForceOrder(msg) {
  const o = msg && msg.e === 'forceOrder' ? msg.o : null;
  if (!o) return null;
  const ts = Number(o.T), qty = Number(o.z ?? o.q), price = Number(o.ap ?? o.p);
  if (!Number.isInteger(ts) || !(qty > 0) || !(price > 0) || !['BUY', 'SELL'].includes(o.S)) return null;
  return {
    venue: 'binance', instrument: o.s, ts, side: o.S.toLowerCase(), liquidated: o.S === 'SELL' ? 'long' : 'short',
    price, qty_base: qty, notional_usd: qty * price, status: o.X, key: `${o.s}|${ts}|${o.S}|${o.z ?? o.q}|${o.ap ?? o.p}`,
  };
}

// Opens the stream for a bounded time and collects (deduplicated) snapshot messages. connect() must return an
// object with addEventListener('message'|'close'|'error') and close(); the default uses the Workers WebSocket
// client (fetch with Upgrade: websocket). Disconnects end the sample early; nothing is retried within a run.
export async function sampleBinanceForceOrders({ durationMs, connect, url = BINANCE_FORCE_ORDER_URL, symbols = ['BTCUSDT'] }) {
  const ws = await connect(url);
  const seen = new Map();
  let messages = 0, closedEarly = false;
  await new Promise((resolve) => {
    const done = setTimeout(resolve, durationMs);
    ws.addEventListener('message', (ev) => {
      messages += 1;
      let parsed;
      try { parsed = JSON.parse(typeof ev.data === 'string' ? ev.data : ''); } catch (_e) { return; }
      for (const m of Array.isArray(parsed) ? parsed : [parsed]) {
        const r = normalizeBinanceForceOrder(m);
        if (r && symbols.includes(r.instrument) && !seen.has(r.key)) seen.set(r.key, r);
      }
    });
    const end = () => { closedEarly = true; clearTimeout(done); resolve(); };
    ws.addEventListener('close', end);
    ws.addEventListener('error', end);
  });
  try { ws.close(); } catch (_e) { /* already closed */ }
  return { records: [...seen.values()], messages, closedEarly };
}

export async function workersWebSocketConnect(url, fetchImpl = fetch) {
  const resp = await fetchImpl(url, { headers: { Upgrade: 'websocket' } });
  if (resp.status === 451 || resp.status === 403) throw new SourceError(SOURCE_STATUS.BLOCKED, `Binance HTTP ${resp.status} (region refused)`);
  const ws = resp.webSocket;
  if (!ws) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, `Binance: no WebSocket upgrade (HTTP ${resp.status})`);
  ws.accept();
  return ws;
}

export const binanceLiquidationSampleAdapter = {
  id: 'binance_liquidation_sample',
  label: 'Binance USDS-M liquidation stream (sampled)',
  kind: 'liquidations',
  freshnessLimitMs: 4 * H,
  enabled: (env) => Number(env.EVENT_BINANCE_SAMPLE_MS) > 0,
  async run({ env, nowTs, connect }) {
    const durationMs = Math.min(Number(env.EVENT_BINANCE_SAMPLE_MS) || 0, 30_000);
    const res = await sampleBinanceForceOrders({ durationMs, connect: connect || ((u) => workersWebSocketConnect(env.EVENT_BINANCE_WS_URL || u)) });
    const bucket = Math.floor(nowTs / H) * H;
    const total = res.records.reduce((s, r) => s + r.notional_usd, 0);
    return {
      status: SOURCE_STATUS.OK,
      observations: [{ source: this.id, series: 'liq_sample_usd', bucket_ts: bucket, value: total, unit: `USD in a ${durationMs} ms sample`, coverage: 'SAMPLED', observed_ts: nowTs }],
      events: [],
      coverage: `sampled snapshot stream (<= 1 order per symbol per second), ${durationMs} ms per run; BTCUSDT only; no events produced`,
      last_observation_ts: res.records.length ? Math.max(...res.records.map((r) => r.ts)) : null,
      details: { sample_ms: durationMs, messages: res.messages, records: res.records.length, closed_early: res.closedEarly },
    };
  },
};
