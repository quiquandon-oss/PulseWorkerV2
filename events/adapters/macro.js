// Macro: scheduled releases and yield shocks. Reuses FRED (the data V1 already uses through PulseWorker /macro-proxy).
//  - Yields: FRED series DGS2 / DGS10 (daily, percent). With FRED_API_KEY set: the FRED API
//    (fred/series/observations); without it: FRED's own key-free CSV download (fred.stlouisfed.org/graph/fredgraph.csv).
//  - Calendar: an EconomicCalendarProvider interface. The included FRED provider (fred/releases/dates, needs
//    FRED_API_KEY) supplies scheduled release DATES only -- no release time, no actual-vs-consensus.
//  - Consensus / surprise: no free provider with licensed consensus estimates is integrated, so every
//    consensus-dependent detection is reported DATA_UNAVAILABLE. A surprise value is never computed or invented.
import { SOURCE_STATUS, SourceError, robustBaseline, severityFor, clamp01, makeEvent, EVENT_DETECTION_USER_AGENT } from '../event-core.js';

const H = 3600_000, D = 24 * H;
export const FRED_API = 'https://api.stlouisfed.org/fred';
export const FRED_CSV = 'https://fred.stlouisfed.org/graph/fredgraph.csv';
// FRED's edge resets requests without a User-Agent (a Worker's default) or with a browser-like one; see EVENT_DETECTION_USER_AGENT.
export const FRED_HEADERS = { 'User-Agent': EVENT_DETECTION_USER_AGENT, Accept: 'text/csv, application/json;q=0.9, */*;q=0.1' };

export const MACRO_CONFIG = {
  yieldSeries: ['DGS2', 'DGS10'],
  historyDays: 420,
  minBaseline: 120,
  baselineObs: 250,
  k: 4,
  minAbsBp: 10,
  maxStaleDays: 6,          // daily series: weekend + a holiday is 4 days; older than 6 days is STALE
  calendarHorizonMs: 7 * D,
  // Matched on FRED release NAMES (not hard-coded ids), so a renamed/removed release fails visibly.
  releases: [
    { key: 'CPI', pattern: /^consumer price index$/i },
    { key: 'EMPLOYMENT', pattern: /^employment situation$/i },
    { key: 'PCE', pattern: /^personal income and outlays$/i },
    { key: 'GDP', pattern: /^gross domestic product$/i },
    { key: 'FOMC', pattern: /fomc/i },
  ],
};

export function parseFredCsv(text, seriesId) {
  const lines = String(text || '').trim().split(/\r?\n/);
  if (lines.length < 2) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, `FRED CSV ${seriesId}: empty`);
  const header = lines[0].split(',').map((h) => h.trim().replace(/"/g, ''));
  const dateIdx = header.findIndex((h) => /^(observation_date|date)$/i.test(h));
  const valIdx = header.findIndex((h) => h.toUpperCase() === seriesId.toUpperCase());
  if (dateIdx < 0 || valIdx < 0) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, `FRED CSV ${seriesId}: unexpected header`);
  const out = [];
  for (const line of lines.slice(1)) {
    const cells = line.split(',');
    const v = cells[valIdx] === undefined ? '' : cells[valIdx].trim();
    if (v === '' || v === '.') continue;                       // FRED marks missing values with '.' -- never filled
    const ts = dateToUtc(cells[dateIdx].trim());
    const value = Number(v);
    if (ts === null || !Number.isFinite(value)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, `FRED CSV ${seriesId}: malformed row`);
    out.push({ date: cells[dateIdx].trim(), ts, value });
  }
  return out.sort((a, b) => a.ts - b.ts);
}

export function parseFredApi(payload, seriesId) {
  if (!payload || !Array.isArray(payload.observations)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, `FRED API ${seriesId}: malformed`);
  const out = [];
  for (const o of payload.observations) {
    if (o.value === '.' || o.value === '') continue;
    const ts = dateToUtc(o.date), value = Number(o.value);
    if (ts === null || !Number.isFinite(value)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, `FRED API ${seriesId}: malformed row`);
    out.push({ date: o.date, ts, value });
  }
  return out.sort((a, b) => a.ts - b.ts);
}

function dateToUtc(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s));
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) : null;
}
function isoDate(ts) { return new Date(ts).toISOString().slice(0, 10); }

export async function fetchYieldSeries(http, seriesId, { apiKey, nowTs, historyDays }) {
  const start = isoDate(nowTs - historyDays * D);
  if (apiKey) {
    const p = await http.json(`${FRED_API}/series/observations?series_id=${seriesId}&api_key=${encodeURIComponent(apiKey)}&file_type=json&observation_start=${start}`, { headers: FRED_HEADERS, cf: { cacheTtl: 0, cacheEverything: false } });
    return { rows: parseFredApi(p, seriesId), via: 'FRED API' };
  }
  const t = await http.text(`${FRED_CSV}?id=${seriesId}&cosd=${start}`, { headers: FRED_HEADERS, cf: { cacheTtl: 0, cacheEverything: false } });
  return { rows: parseFredCsv(t, seriesId), via: 'FRED CSV (no key)' };
}

// Rule ed-yield-shock-v1: latest daily change (basis points) vs robust baseline of prior absolute changes.
// Direction is UNKNOWN: whether a yield move is risk-on or risk-off for crypto depends on why rates moved.
export async function evaluateYieldShock(seriesId, rows, cfg, { nowTs, source, via }) {
  if (rows.length < 2) return { status: SOURCE_STATUS.INSUFFICIENT_BASELINE, event: null, baselineCount: 0 };
  const changes = [];
  for (let i = 1; i < rows.length; i += 1) changes.push({ ts: rows[i].ts, date: rows[i].date, bp: (rows[i].value - rows[i - 1].value) * 100, level: rows[i].value });
  const latest = changes[changes.length - 1];
  const prior = changes.slice(0, -1).slice(-cfg.baselineObs).map((c) => Math.abs(c.bp));
  const base = robustBaseline(prior, { minCount: cfg.minBaseline, scaleFloor: 0.5 });
  if (!base) return { status: SOURCE_STATUS.INSUFFICIENT_BASELINE, event: null, baselineCount: prior.length };
  const thr = Math.max(cfg.minAbsBp, base.median + cfg.k * base.scale);
  if (!(Math.abs(latest.bp) >= thr)) return { status: SOURCE_STATUS.OK, event: null, baselineCount: prior.length };
  const event = await makeEvent({
    event_type: 'YIELD_SHOCK', asset: 'US_RATES', market: `US Treasury ${seriesId === 'DGS2' ? '2-year' : '10-year'} constant maturity (${seriesId})`, scope: 'US',
    source, source_url: `https://fred.stlouisfed.org/series/${seriesId}`, source_ts: latest.ts, detected_ts: nowTs,
    direction: 'UNKNOWN', severity: severityFor(Math.abs(latest.bp) / thr), confidence: clamp01(0.6 + 0.2 * Math.min(1, prior.length / cfg.baselineObs)),
    rule_id: 'ed-yield-shock-v1', observed_value: latest.bp, baseline_value: base.median, threshold_value: thr, unit: 'basis points, 1 business day',
    evidence: [{ kind: 'fred_daily', series: seriesId, observation_date: latest.date, level_pct: latest.level, change_bp: Math.round(latest.bp * 10) / 10, baseline_obs: prior.length, via }],
    explanation: `${seriesId} moved ${Math.round(latest.bp * 10) / 10} bp on ${latest.date}, beyond ${Math.round(thr * 10) / 10} bp (max of ${cfg.minAbsBp} bp and median + ${cfg.k}x robust scale of ${prior.length} prior absolute daily changes). Daily FRED observation; detection time is when this Worker first saw it.`,
    dedup_parts: ['YIELD_SHOCK', seriesId, source, latest.date],
    expires_ts: latest.ts + 3 * D,
  });
  return { status: SOURCE_STATUS.OK, event, baselineCount: prior.length };
}

// ---- Economic calendar provider interface ----
// upcoming(nowTs, horizonMs) -> { status, items: [{ key, name, scheduled_date, scheduled_ts|null, time_known, actual:null,
// consensus:null, provenance }] }. A provider that cannot supply consensus must return consensus: null.
export function fredReleaseCalendar({ http, apiKey, releases }) {
  return {
    id: 'fred_release_dates',
    supportsConsensus: false,
    async upcoming(nowTs, horizonMs) {
      if (!apiKey) return { status: SOURCE_STATUS.NOT_CONFIGURED, reason: 'FRED_API_KEY not set (fred/releases/dates requires a key)', items: [] };
      const p = await http.json(`${FRED_API}/releases/dates?api_key=${encodeURIComponent(apiKey)}&file_type=json&realtime_start=${isoDate(nowTs)}&realtime_end=${isoDate(nowTs + horizonMs)}&include_release_dates_with_no_data=true&sort_order=asc&limit=1000`, { headers: FRED_HEADERS });
      if (!p || !Array.isArray(p.release_dates)) throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'FRED releases/dates: malformed');
      const items = [];
      for (const r of p.release_dates) {
        const rel = releases.find((x) => x.pattern.test(String(r.release_name || '').trim()));
        const ts = dateToUtc(r.date);
        if (!rel || ts === null || ts + D <= nowTs || ts > nowTs + horizonMs) continue;
        items.push({ key: rel.key, name: r.release_name, release_id: r.release_id, scheduled_date: r.date, scheduled_ts: null, time_known: false, actual: null, consensus: null, provenance: 'FRED release calendar (date only)' });
      }
      return { status: SOURCE_STATUS.OK, items };
    },
  };
}

export const macroAdapter = {
  id: 'macro_fred',
  label: 'Macro: FRED yields and release calendar',
  kind: 'macro',
  freshnessLimitMs: 7 * D,
  async run({ env, http, nowTs, config, calendar }) {
    const cfg = { ...MACRO_CONFIG, ...(config || {}) };
    const apiKey = env && env.FRED_API_KEY ? String(env.FRED_API_KEY) : null;
    const observations = [], events = [], perSeries = {};
    let status = SOURCE_STATUS.OK, lastObs = null;
    const failures = [];
    for (const sid of cfg.yieldSeries) {
      // Series are isolated: one failing series is recorded and the others are still stored and evaluated.
      let fetched;
      try { fetched = await fetchYieldSeries(http, sid, { apiKey, nowTs, historyDays: cfg.historyDays }); } catch (e) {
        failures.push(e);
        perSeries[sid] = { error: String((e && e.message) || e).slice(0, 300), status: (e && e.status) || SOURCE_STATUS.PROVIDER_ERROR };
        continue;
      }
      const { rows, via } = fetched;
      // Rules evaluate on the provider's own history; only the recent overlap is stored (audit + point-in-time).
      for (const r of rows.slice(-10)) observations.push({ source: this.id, series: sid, bucket_ts: r.ts, value: r.value, unit: 'percent', coverage: 'COMPLETE', observed_ts: nowTs });
      const latest = rows[rows.length - 1];
      const stale = !latest || nowTs - latest.ts > cfg.maxStaleDays * D;
      const res = stale ? { status: SOURCE_STATUS.STALE, event: null, baselineCount: 0 } : await evaluateYieldShock(sid, rows, cfg, { nowTs, source: this.id, via });
      if (res.event) events.push(res.event);
      if (latest) lastObs = Math.max(lastObs || 0, latest.ts);
      perSeries[sid] = { observations: rows.length, latest_date: latest ? latest.date : null, rule: res.status, baseline: res.baselineCount, via };
      if (res.status !== SOURCE_STATUS.OK && status === SOURCE_STATUS.OK) status = res.status;
    }
    if (failures.length === cfg.yieldSeries.length) throw failures[0];       // nothing usable: the runner records the error
    if (failures.length) status = failures[0].status || SOURCE_STATUS.PROVIDER_ERROR;
    // Scheduled releases (date only) -- never a surprise.
    const cal = calendar || fredReleaseCalendar({ http, apiKey, releases: cfg.releases });
    let up;
    try { up = await cal.upcoming(nowTs, cfg.calendarHorizonMs); } catch (e) {
      up = { status: (e && e.status) || SOURCE_STATUS.PROVIDER_ERROR, reason: String((e && e.message) || e).slice(0, 300), items: [] };
    }
    for (const it of up.items) {
      const dayTs = dateToUtc(it.scheduled_date);
      events.push(await makeEvent({
        event_type: 'MACRO_RELEASE_SCHEDULED', asset: 'US_MACRO', market: it.name, scope: 'US', source: this.id,
        source_url: it.release_id ? `https://fred.stlouisfed.org/release?rid=${it.release_id}` : null,
        source_ts: nowTs, detected_ts: nowTs, direction: 'UNKNOWN', severity: 'LOW', confidence: 0.9,
        rule_id: 'ed-macro-calendar-v1', unit: 'scheduled release',
        evidence: [{ kind: 'calendar', release: it.name, scheduled_date: it.scheduled_date, time_known: it.time_known, consensus: 'DATA_UNAVAILABLE', actual: 'DATA_UNAVAILABLE', provenance: it.provenance }],
        explanation: `${it.name} is scheduled for ${it.scheduled_date} (date only; release time not provided). Actual-vs-consensus surprise: DATA_UNAVAILABLE (no free consensus provider).`,
        dedup_parts: ['MACRO_RELEASE_SCHEDULED', it.key, it.scheduled_date],
        expires_ts: dayTs + D,
      }));
    }
    return {
      status, observations, events, last_observation_ts: lastObs,
      coverage: 'US Treasury 2y/10y daily (FRED); release calendar dates only',
      details: {
        series: perSeries,
        calendar: { provider: cal.id, status: up.status, reason: up.reason || null, upcoming: up.items.length },
        consensus_surprise: { status: SOURCE_STATUS.DATA_UNAVAILABLE, reason: 'No free provider with consensus estimates is integrated; FRED supplies actual values only. Surprise detection is not computed.' },
        requests: { ...http.counters },
      },
    };
  },
};
