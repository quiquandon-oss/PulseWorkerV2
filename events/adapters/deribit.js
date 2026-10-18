// Options volatility (Deribit, free public JSON-RPC over HTTP GET; verified live 2026-10-17 from the build container):
//  - public/get_volatility_index_data?currency=BTC&start_timestamp&end_timestamp&resolution=3600
//      -> result.data = [[candle_start_ms, open, high, low, close], ...], result.continuation (null when complete)
//  - public/get_book_summary_by_currency?currency=BTC&kind=option
//      -> result = [{ instrument_name 'BTC-19OCT26-71000-C', open_interest, mark_iv, underlying_price, creation_timestamp, ... }]
// What this measures: OBSERVED implied volatility (DVOL) and OBSERVED options open interest (Deribit only; option
// open interest is reported in the underlying base currency). It does NOT measure dealer gamma, options flow or
// directional positioning, so every event here has direction UNKNOWN.
import { SOURCE_STATUS, SourceError, robustBaseline, robustZ, severityFor, clamp01, makeEvent, stressCorrelationKey } from '../event-core.js';

const H = 3600_000;
export const DERIBIT_BASE = 'https://www.deribit.com/api/v2';

export const DERIBIT_CONFIG = {
  currencies: ['BTC', 'ETH'],
  dvolHistoryMs: 31 * 24 * H,
  dvolMinBaseline: 14 * 24,
  dvolK: 3,
  dvolMinAbsPoints: 3,             // vol points
  oiMinBaseline: 30,
  oiK: 3,
  oiMinAbsPct: 10,
  maxSnapshotAgeMs: 15 * 60_000,   // book summary older than this is STALE
  dvolMaxPages: 4,                 // per currency; one page held the full 31 days live (744 candles). Bounds subrequests.
};

function rpcResult(payload) {
  if (!payload || typeof payload !== 'object') throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'Deribit: malformed payload');
  if (payload.error) {
    const code = payload.error.code;
    throw new SourceError(code === 10028 ? SOURCE_STATUS.RATE_LIMITED : SOURCE_STATUS.PROVIDER_ERROR, `Deribit error ${code}: ${String(payload.error.message || '').slice(0, 120)}`);
  }
  if (payload.result === undefined) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'Deribit: no result');
  return payload.result;
}

// Pages backwards from endTs. If maxPages is reached first, the most recent part is kept and the baseline minimum
// (dvolMinBaseline) decides whether it is enough; nothing is filled.
export async function fetchDvol(http, currency, startTs, endTs, maxPages = DERIBIT_CONFIG.dvolMaxPages) {
  const rows = new Map();
  let end = endTs;
  for (let page = 0; page < maxPages; page += 1) {
    const r = rpcResult(await http.json(`${DERIBIT_BASE}/public/get_volatility_index_data?currency=${currency}&start_timestamp=${startTs}&end_timestamp=${end}&resolution=3600`));
    if (!r || !Array.isArray(r.data)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'Deribit DVOL: data is not a list');
    for (const c of r.data) {
      if (!Array.isArray(c) || c.length < 5 || !Number.isInteger(c[0]) || !Number.isFinite(c[4])) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'Deribit DVOL: malformed candle');
      if (c[0] % H !== 0) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'Deribit DVOL: candle not on the hour');
      rows.set(c[0], { ts: c[0], close: c[4] });
    }
    if (r.continuation === null || r.continuation === undefined) break;
    const next = Number(r.continuation);
    if (!(next < end)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'Deribit DVOL: continuation did not move back');
    end = next;
  }
  return [...rows.values()].sort((a, b) => a.ts - b.ts);
}

// 24-hour changes of an hourly series, only where both ends exist (no filling).
export function changes24h(rows) {
  const by = new Map(rows.map((r) => [r.bucket_ts ?? r.ts, r.value ?? r.close]));
  const out = [];
  for (const [t, v] of by) if (by.has(t - 24 * H)) out.push({ ts: t, delta: v - by.get(t - 24 * H) });
  return out.sort((a, b) => a.ts - b.ts);
}

// Rule ed-dvol-24h-v1 on the latest CLOSED candle.
export async function evaluateDvol(currency, closed, cfg, { nowTs, source }) {
  const deltas = changes24h(closed);
  if (!deltas.length) return { status: SOURCE_STATUS.INSUFFICIENT_BASELINE, event: null, baselineCount: 0 };
  const latest = deltas[deltas.length - 1];
  const prior = deltas.filter((d) => d.ts <= latest.ts - 24 * H).map((d) => d.delta);  // no overlap with the change being tested
  const base = robustBaseline(prior, { minCount: cfg.dvolMinBaseline, scaleFloor: 0.25 });
  if (!base) return { status: SOURCE_STATUS.INSUFFICIENT_BASELINE, event: null, baselineCount: prior.length };
  const up = Math.max(base.median + cfg.dvolK * base.scale, cfg.dvolMinAbsPoints);
  const down = Math.min(base.median - cfg.dvolK * base.scale, -cfg.dvolMinAbsPoints);
  let type = null, threshold = null, ratio = null;
  if (latest.delta >= up) { type = 'VOL_EXPANSION'; threshold = up; ratio = latest.delta / up; }
  else if (latest.delta <= down) { type = 'VOL_COMPRESSION'; threshold = down; ratio = latest.delta / down; }
  if (!type) return { status: SOURCE_STATUS.OK, event: null, baselineCount: prior.length };
  const closeTs = latest.ts + H;
  const level = closed.find((r) => (r.bucket_ts ?? r.ts) === latest.ts);
  const z = robustZ(latest.delta, base);
  const event = await makeEvent({
    event_type: type, asset: currency, market: `Deribit DVOL (${currency} 30-day implied volatility index)`, scope: 'single venue',
    source, source_url: `${DERIBIT_BASE}/public/get_volatility_index_data?currency=${currency}&resolution=3600`,
    source_ts: closeTs, detected_ts: nowTs, direction: 'UNKNOWN', severity: severityFor(ratio),
    confidence: clamp01(0.5 + 0.2 * Math.min(1, prior.length / (30 * 24))),
    rule_id: 'ed-dvol-24h-v1', observed_value: latest.delta, baseline_value: base.median, threshold_value: threshold,
    unit: 'DVOL points change over 24 h',
    evidence: [{ kind: 'dvol', candle_start: latest.ts, candle_close_ts: closeTs, dvol_close: level ? (level.value ?? level.close) : null, change_24h: round(latest.delta), robust_z: Number.isFinite(z) ? round(z) : null, baseline_changes: prior.length }],
    explanation: `Deribit ${currency} DVOL ${type === 'VOL_EXPANSION' ? 'rose' : 'fell'} ${Math.abs(round(latest.delta))} points in 24 h (hour closing ${new Date(closeTs).toISOString()}), beyond the threshold ${round(threshold)} (median ${round(base.median)} +/- ${cfg.dvolK}x robust scale over ${prior.length} prior 24 h changes, min ${cfg.dvolMinAbsPoints} points). Implied volatility is not directional.`,
    dedup_parts: [type, currency, source, Math.floor(closeTs / (6 * H))],
    expires_ts: closeTs + 24 * H,
    correlation_key: stressCorrelationKey(currency, closeTs),
  });
  return { status: SOURCE_STATUS.OK, event, baselineCount: prior.length };
}

export function summarizeOptionsBook(rows, currency) {
  if (!Array.isArray(rows)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'Deribit book summary: not a list');
  let total = 0, puts = 0, calls = 0, maxTs = 0, n = 0;
  const byExpiry = {};
  for (const r of rows) {
    const m = /^([A-Z]+)-(\d{1,2}[A-Z]{3}\d{2})-[\d.]+-([CP])$/.exec(String(r.instrument_name || ''));
    const oi = Number(r.open_interest);
    if (!m || m[1] !== currency || !Number.isFinite(oi) || oi < 0) continue;
    n += 1; total += oi;
    if (m[3] === 'P') puts += oi; else calls += oi;
    byExpiry[m[2]] = (byExpiry[m[2]] || 0) + oi;
    if (Number.isInteger(r.creation_timestamp)) maxTs = Math.max(maxTs, r.creation_timestamp);
  }
  if (!n) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, `Deribit: no ${currency} option instruments`);
  return { instruments: n, total, puts, calls, byExpiry, snapshotTs: maxTs || null };
}

// Rule ed-options-oi-24h-v1: % change of total options OI vs the snapshot closest to 24 h earlier (+/- 3 h).
export async function evaluateOptionsOi(currency, snapshots, current, cfg, { nowTs, source }) {
  const pct = (a, b) => (b > 0 ? ((a - b) / b) * 100 : null);
  const nearest = (t) => {
    let best = null;
    for (const s of snapshots) if (Math.abs(s.bucket_ts - t) <= 3 * H && (!best || Math.abs(s.bucket_ts - t) < Math.abs(best.bucket_ts - t))) best = s;
    return best;
  };
  const ref = nearest(current.bucket_ts - 24 * H);
  if (!ref) return { status: SOURCE_STATUS.INSUFFICIENT_BASELINE, event: null, baselineCount: 0 };
  const prior = [];
  for (const s of snapshots) {
    if (s.bucket_ts > current.bucket_ts - 24 * H) continue;
    const r = nearest(s.bucket_ts - 24 * H);
    const p = r ? pct(s.value, r.value) : null;
    if (p !== null) prior.push(p);
  }
  const change = pct(current.value, ref.value);
  const base = robustBaseline(prior, { minCount: cfg.oiMinBaseline, scaleFloor: 0.5 });
  if (!base || change === null) return { status: SOURCE_STATUS.INSUFFICIENT_BASELINE, event: null, baselineCount: prior.length };
  const thr = Math.max(Math.abs(base.median) + cfg.oiK * base.scale, cfg.oiMinAbsPct);
  if (!(Math.abs(change) >= thr)) return { status: SOURCE_STATUS.OK, event: null, baselineCount: prior.length };
  const event = await makeEvent({
    event_type: 'OPTIONS_OI_SHIFT', asset: currency, market: `Deribit ${currency} options (all expiries)`, scope: 'single venue',
    source, source_url: `${DERIBIT_BASE}/public/get_book_summary_by_currency?currency=${currency}&kind=option`,
    source_ts: current.snapshot_ts, detected_ts: nowTs, direction: 'UNKNOWN', severity: severityFor(Math.abs(change) / thr),
    confidence: clamp01(0.4 + 0.2 * Math.min(1, prior.length / 120)),
    rule_id: 'ed-options-oi-24h-v1', observed_value: change, baseline_value: base.median, threshold_value: thr,
    unit: '% change of total options open interest over ~24 h',
    evidence: [{ kind: 'options_oi', snapshot_ts: current.snapshot_ts, total_oi: round(current.value), reference_bucket: ref.bucket_ts, reference_oi: round(ref.value), baseline_changes: prior.length }],
    explanation: `Deribit ${currency} options open interest changed ${round(change)}% in ~24 h, beyond ${round(thr)}% (robust baseline of ${prior.length} prior 24 h changes). Observed open interest only -- no dealer gamma or directional positioning is inferred.`,
    dedup_parts: ['OPTIONS_OI_SHIFT', currency, source, current.bucket_ts],
    expires_ts: current.snapshot_ts + 24 * H,
    correlation_key: stressCorrelationKey(currency, current.snapshot_ts),
  });
  return { status: SOURCE_STATUS.OK, event, baselineCount: prior.length };
}

function round(x) { return Math.round(x * 100) / 100; }

export const deribitAdapter = {
  id: 'deribit_options',
  label: 'Deribit DVOL and options open interest',
  kind: 'options_volatility',
  freshnessLimitMs: 4 * H,
  async run({ http, nowTs, history, config }) {
    const cfg = { ...DERIBIT_CONFIG, ...(config || {}) };
    const observations = [], events = [], perCurrency = {};
    let status = SOURCE_STATUS.OK, lastObs = null;
    for (const cur of cfg.currencies) {
      // DVOL: closed candles only (start + 1 h <= now).
      const candles = (await fetchDvol(http, cur, nowTs - cfg.dvolHistoryMs, nowTs, cfg.dvolMaxPages)).filter((c) => c.ts + H <= nowTs);
      // Evaluation uses the fetched history; only the recent overlap is stored (bounded D1 writes per run).
      for (const c of candles.slice(-8)) observations.push({ source: this.id, series: `dvol_${cur.toLowerCase()}`, bucket_ts: c.ts, value: c.close, unit: 'DVOL index', coverage: 'COMPLETE', observed_ts: nowTs });
      const stored = await history(`dvol_${cur.toLowerCase()}`, nowTs - cfg.dvolHistoryMs, nowTs);
      const merged = new Map(candles.map((c) => [c.ts, { bucket_ts: c.ts, value: c.close }]));
      for (const s of stored) merged.set(s.bucket_ts, { bucket_ts: s.bucket_ts, value: s.value });
      const closed = [...merged.values()].sort((a, b) => a.bucket_ts - b.bucket_ts);
      const dv = await evaluateDvol(cur, closed, cfg, { nowTs, source: this.id });
      if (dv.event) events.push(dv.event);
      if (candles.length) lastObs = Math.max(lastObs || 0, candles[candles.length - 1].ts + H);

      // Options OI snapshot.
      const book = summarizeOptionsBook(rpcResult(await http.json(`${DERIBIT_BASE}/public/get_book_summary_by_currency?currency=${cur}&kind=option`)), cur);
      const stale = !book.snapshotTs || nowTs - book.snapshotTs > cfg.maxSnapshotAgeMs;
      const bucket = Math.floor(nowTs / H) * H;
      let oi = { status: SOURCE_STATUS.STALE, event: null, baselineCount: 0 };
      if (!stale) {
        for (const [series, v] of [['opt_oi_total', book.total], ['opt_oi_puts', book.puts], ['opt_oi_calls', book.calls]]) {
          observations.push({ source: this.id, series: `${series}_${cur.toLowerCase()}`, bucket_ts: bucket, value: v, unit: `${cur} (underlying, Deribit options)`, coverage: 'COMPLETE', observed_ts: nowTs });
        }
        const snaps = await history(`opt_oi_total_${cur.toLowerCase()}`, nowTs - 60 * 24 * H, bucket);
        oi = await evaluateOptionsOi(cur, snaps, { bucket_ts: bucket, value: book.total, snapshot_ts: Math.min(book.snapshotTs, nowTs) }, cfg, { nowTs, source: this.id });
        if (oi.event) events.push(oi.event);
        lastObs = Math.max(lastObs || 0, book.snapshotTs);
      }
      perCurrency[cur] = { dvol_candles: candles.length, dvol: dv.status, dvol_baseline: dv.baselineCount, options_instruments: book.instruments, options_oi_total: round(book.total), put_call_oi_ratio: book.calls > 0 ? round(book.puts / book.calls) : null, options_oi: oi.status, options_baseline: oi.baselineCount, snapshot_ts: book.snapshotTs };
      for (const s of [dv.status, oi.status]) if (s !== SOURCE_STATUS.OK && status === SOURCE_STATUS.OK) status = s;
    }
    return {
      status, observations, events, last_observation_ts: lastObs,
      coverage: `Deribit only; ${cfg.currencies.join(', ')}; DVOL hourly candles; options OI snapshot per run`,
      details: { currencies: perCurrency, requests: { ...http.counters } },
    };
  },
};
