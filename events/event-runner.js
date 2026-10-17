// Event Detection Layer -- orchestration. Sources -> adapters -> normalised observations -> rules -> deduplicated
// events -> ledger. Each adapter is isolated (its own timeout and try/catch): one provider failing never stops the
// others. Nothing here reads or writes V1 sentiment, weights, composite or predictions.
import { RULESET_VERSION, SOURCE_STATUS, makeHttp, logEvent, sha256Hex } from './event-core.js';
import { eventTablesPresent, insertEvent, insertObservations, observationsBefore, upsertHealth, recordRun, expireEvents, EVENT_TABLES_MISSING } from './event-store.js';
import { okxLiquidationsAdapter, binanceLiquidationSampleAdapter } from './adapters/liquidations.js';
import { deribitAdapter } from './adapters/deribit.js';
import { macroAdapter } from './adapters/macro.js';
import { secEdgarAdapter } from './adapters/sec-edgar.js';

export const DETECTORS = [okxLiquidationsAdapter, binanceLiquidationSampleAdapter, deribitAdapter, macroAdapter, secEdgarAdapter];

// Existing sources, for the inventory shown next to the detectors. None of them is re-collected here.
export const EXISTING_SOURCES = [
  { id: 'gdelt_geo_shock', label: 'GDELT geopolitical component (Risk Regime Shock research)', where: 'PulseWorkerV2 branch claude/gdelt-risk-regime-shock (Python research, offline archive; not merged, not deployed)', provides: 'Historical GDELT event categories, breadth, acceleration, persistence for research episodes', event_detector: false },
  { id: 'news_feeds', label: 'CoinTelegraph, CoinDesk, The Block, BBC World, Investing.com, SoSoValue', where: 'V1 frontend composite sources (cryptonews / macrogeo / geopolitics / regulatory / sosovalue)', provides: 'Headline sentiment scores; no structured, deduplicated event ledger', event_detector: false },
  { id: 'funding', label: 'Hyperliquid funding (BTC/ETH, HYPE)', where: 'V1 composite sources funding / hypefunding; EXP-23 research collectors', provides: 'Funding rates (positioning cost), hourly', event_detector: false },
  { id: 'hloi', label: 'Hyperliquid BTC open interest', where: 'V1 composite source hloi (PulseWorker /hl-oi, hourly snapshots)', provides: 'Open-interest level/changes; not liquidations', event_detector: false },
  { id: 'riskregime', label: 'Risk Regime Shock (cross-asset)', where: 'V1 composite source riskregime (PulseWorker /risk-regime, rrs-v1)', provides: 'Breadth of abnormal 6 h cross-asset moves (scored 0-100)', event_detector: false },
  { id: 'macro_proxies', label: 'FRED yields/oil/USD, gold, equity indices, EUR/USD', where: 'V1 composite sources via PulseWorker /macro-proxy and direct APIs', provides: 'Daily levels/changes used as sentiment inputs', event_detector: false },
  { id: 'etfflows', label: 'Spot BTC ETF flows', where: 'V1 composite source etfflows', provides: 'Daily net flows', event_detector: false },
  { id: 'onchain', label: 'mempool.space difficulty adjustment', where: 'V1 composite source onchain', provides: 'Difficulty-change estimate; slow-moving, not an event feed', event_detector: false },
];

export function detectorRegistry(env) {
  return DETECTORS.map((a) => ({ id: a.id, label: a.label, kind: a.kind, freshness_limit_ms: a.freshnessLimitMs, enabled: a.enabled ? !!a.enabled(env || {}) : true }));
}

function withTimeout(promise, ms, label) {
  let t;
  return Promise.race([
    promise.finally(() => clearTimeout(t)),
    new Promise((_r, rej) => { t = setTimeout(() => rej(Object.assign(new Error(`${label}: adapter timeout after ${ms} ms`), { status: SOURCE_STATUS.PROVIDER_ERROR })), ms); }),
  ]);
}

export async function runEventDetection(env, { nowTs = Date.now(), trigger = 'cron', fetchImpl, adapters = DETECTORS, adapterTimeoutMs = 45_000, sleep, deps = {} } = {}) {
  if (!(await eventTablesPresent(env))) { logEvent('run_skipped', { reason: 'tables missing' }); return { ...EVENT_TABLES_MISSING }; }
  const run = { run_id: 'edr_' + (await sha256Hex(`${nowTs}|${trigger}|${Math.random()}`)).slice(0, 16), started_ts: nowTs, trigger, summary: {} };
  await recordRun(env, run);
  const summary = {};
  for (const a of adapters) {
    const base = { source: a.id, last_attempt_ts: nowTs };
    if (a.enabled && !a.enabled(env)) {
      await upsertHealth(env, { ...base, status: SOURCE_STATUS.DISABLED, coverage: null, details: { reason: 'disabled by configuration' } }, nowTs);
      summary[a.id] = { status: SOURCE_STATUS.DISABLED };
      continue;
    }
    const http = makeHttp({ fetchImpl: fetchImpl || ((...x) => fetch(...x)), sleep, timeoutMs: 10_000, retries: 2 });
    const history = (series, fromTs, beforeTs) => observationsBefore(env, a.id, series, { fromTs, beforeTs, asOfTs: nowTs });
    try {
      const res = await withTimeout(a.run({ env, http, nowTs, history, sleep, ...(deps[a.id] || {}) }), adapterTimeoutMs, a.id);
      const written = await insertObservations(env, res.observations || []);
      let newEvents = 0;
      for (const ev of res.events || []) if (await insertEvent(env, ev, nowTs)) newEvents += 1;
      let status = res.status;
      if (status === SOURCE_STATUS.OK && res.last_observation_ts && nowTs - res.last_observation_ts > a.freshnessLimitMs) status = SOURCE_STATUS.STALE;
      const healthy = ![SOURCE_STATUS.PROVIDER_ERROR, SOURCE_STATUS.BLOCKED, SOURCE_STATUS.RATE_LIMITED, SOURCE_STATUS.NOT_CONFIGURED].includes(status);
      await upsertHealth(env, { ...base, status, last_success_ts: healthy ? nowTs : null, last_observation_ts: res.last_observation_ts || null, last_error: null, coverage: res.coverage || null, details: res.details || {} }, nowTs);
      summary[a.id] = { status, observations_written: written, events_detected: (res.events || []).length, events_new: newEvents };
      logEvent('adapter_done', { run_id: run.run_id, source: a.id, ...summary[a.id] });
    } catch (e) {
      const status = (e && e.status) || SOURCE_STATUS.PROVIDER_ERROR;
      await upsertHealth(env, { ...base, status, last_error: String((e && e.message) || e).slice(0, 300), coverage: null, details: { requests: { ...http.counters } } }, nowTs);
      summary[a.id] = { status, error: String((e && e.message) || e).slice(0, 300) };
      logEvent('adapter_failed', { run_id: run.run_id, source: a.id, status, error: summary[a.id].error });
    }
  }
  const expired = await expireEvents(env, nowTs);
  run.finished_ts = Math.max(nowTs, Date.now());
  run.summary = { ruleset: RULESET_VERSION, adapters: summary, expired };
  await recordRun(env, run);
  return { ok: true, run_id: run.run_id, summary: run.summary };
}
