// Event Detection Layer -- canonical event record, identifiers, robust baselines and source-health vocabulary.
// Pure functions only (no I/O). Deliberately separate from V1 sentiment scoring: nothing here reads or writes V1
// weights, confidence values, composite scores or predictions. A detected event is evidence, not a trading signal.

export const RULESET_VERSION = 'ed-v1';

export const DIRECTIONS = ['UP', 'DOWN', 'MIXED', 'UNKNOWN'];
export const SEVERITIES = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'];
export const EVENT_STATUSES = ['ACTIVE', 'EXPIRED'];

// Source-health states, shared by every adapter and the API/UI.
export const SOURCE_STATUS = {
  OK: 'OK',                                   // collected, fresh, baseline available
  INSUFFICIENT_BASELINE: 'INSUFFICIENT_BASELINE', // collected, but not enough history to evaluate rules yet
  STALE: 'STALE',                             // last good observation older than the source's freshness limit
  DATA_UNAVAILABLE: 'DATA_UNAVAILABLE',       // required input does not exist for free (e.g. macro consensus)
  NOT_CONFIGURED: 'NOT_CONFIGURED',           // a required setting (e.g. SEC User-Agent) is missing
  BLOCKED: 'BLOCKED_BY_PROVIDER',             // provider refused access (geo restriction, 403/451)
  RATE_LIMITED: 'RATE_LIMITED',
  PROVIDER_ERROR: 'PROVIDER_ERROR',           // HTTP error / malformed payload / timeout after bounded retries
  DISABLED: 'DISABLED',                       // adapter intentionally off in this configuration
  BUDGET_EXHAUSTED: 'BUDGET_EXHAUSTED',       // this run hit its per-invocation subrequest / D1 query budget
  NEVER_RUN: 'NEVER_RUN',
};

export class SourceError extends Error {
  constructor(status, message, detail) {
    super(message);
    this.status = status;
    this.detail = detail || null;
  }
}

// ---- hashing / identifiers ----
export async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(String(text)));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// The dedup key names WHAT happened (type, asset, source, and the observation window or document that proves it),
// never WHEN this run noticed it -- so re-detecting the same window on a later run maps to the same key.
export function dedupKey(parts) {
  return parts.map((p) => (p === null || p === undefined ? '' : String(p).trim().toLowerCase())).join('|');
}

export async function eventIdFor(key) {
  return 'ev_' + (await sha256Hex(key)).slice(0, 24);
}

// Syndicated / repeated text: normalise case, punctuation and whitespace, so the same headline or sentence from
// two outlets (or an amendment repeating the original) shares one fingerprint.
export function textFingerprint(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/<[^>]+>/g, ' ')
    .replace(/&[a-z#0-9]+;/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ');
}

// ---- robust statistics ----
export function median(values) {
  const v = values.filter(Number.isFinite).slice().sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

export function mad(values, med = median(values)) {
  if (med === null) return null;
  return median(values.filter(Number.isFinite).map((x) => Math.abs(x - med)));
}

// Robust baseline of a series. Returns null (never a guess) when there are fewer than minCount finite values.
// scale = 1.4826 * MAD (a consistent estimator of sigma for normal data); a floor keeps a perfectly flat history
// from making every tiny change look infinite.
export function robustBaseline(values, { minCount, scaleFloor = 0 } = {}) {
  const v = values.filter(Number.isFinite);
  if (v.length < (minCount || 1)) return null;
  const med = median(v);
  const m = mad(v, med);
  return { count: v.length, median: med, mad: m, scale: Math.max(1.4826 * m, scaleFloor) };
}

// Robust z-score; null when no baseline.
export function robustZ(value, baseline) {
  if (!baseline || !Number.isFinite(value)) return null;
  if (!(baseline.scale > 0)) return value === baseline.median ? 0 : (value > baseline.median ? Infinity : -Infinity);
  return (value - baseline.median) / baseline.scale;
}

// Severity is a function of how far the observation exceeds its threshold -- not of direction, and not of how
// confident we are that the event matters for price.
export function severityFor(ratioOverThreshold) {
  if (!Number.isFinite(ratioOverThreshold) || ratioOverThreshold < 1) return null;
  if (ratioOverThreshold >= 4) return 'CRITICAL';
  if (ratioOverThreshold >= 2) return 'HIGH';
  if (ratioOverThreshold >= 1.4) return 'MEDIUM';
  return 'LOW';
}

export function clamp01(x) {
  return Math.max(0, Math.min(1, x));
}

// ---- canonical event record ----
const REQUIRED = ['event_type', 'source', 'source_ts', 'detected_ts', 'rule_id', 'dedup_parts'];

// Builds and validates one canonical event. Throws on an invalid record (a rule bug must fail loudly in tests,
// not write a malformed row).
export async function makeEvent(input) {
  for (const k of REQUIRED) if (input[k] === undefined || input[k] === null) throw new Error(`event missing ${k}`);
  const direction = input.direction || 'UNKNOWN';
  if (!DIRECTIONS.includes(direction)) throw new Error(`invalid direction ${direction}`);
  if (input.severity && !SEVERITIES.includes(input.severity)) throw new Error(`invalid severity ${input.severity}`);
  for (const k of ['source_ts', 'detected_ts']) {
    if (!Number.isInteger(input[k]) || input[k] < 1_000_000_000_000) throw new Error(`${k} must be epoch ms`);
  }
  if (input.source_ts > input.detected_ts) throw new Error('source_ts after detected_ts (look-ahead)');
  const confidence = input.confidence === undefined || input.confidence === null ? null : Number(input.confidence);
  if (confidence !== null && !(confidence >= 0 && confidence <= 1)) throw new Error('confidence must be within [0,1]');
  const key = dedupKey(input.dedup_parts);
  return {
    event_id: await eventIdFor(key),
    dedup_key: key,
    event_type: input.event_type,
    asset: input.asset || null,
    market: input.market || null,
    scope: input.scope || null,
    source: input.source,
    source_url: input.source_url || null,
    source_ts: input.source_ts,
    detected_ts: input.detected_ts,
    direction,
    severity: input.severity || 'LOW',
    confidence,
    rule_id: input.rule_id,
    rule_version: input.rule_version || RULESET_VERSION,
    observed_value: numOrNull(input.observed_value),
    baseline_value: numOrNull(input.baseline_value),
    threshold_value: numOrNull(input.threshold_value),
    unit: input.unit || null,
    evidence: input.evidence || [],
    explanation: input.explanation || '',
    freshness_ms: input.detected_ts - input.source_ts,
    source_health: input.source_health || SOURCE_STATUS.OK,
    status: 'ACTIVE',
    expires_ts: Number.isInteger(input.expires_ts) ? input.expires_ts : null,
    correlation_key: input.correlation_key || null,
  };
}

function numOrNull(v) {
  const n = Number(v);
  return v === null || v === undefined || !Number.isFinite(n) ? null : n;
}

// Market-stress events on the same asset within the same 6-hour window share a correlation key. Correlation links
// events for display; it never merges them or counts one piece of evidence twice.
export function stressCorrelationKey(asset, ts, windowMs = 6 * 3600_000) {
  return `stress|${String(asset || '').toLowerCase()}|${Math.floor(ts / windowMs) * windowMs}`;
}

// ---- Per-invocation budgets (Workers Free: 50 external subrequests and 50 D1 queries per invocation) ----
// One budget object is shared by every adapter in a run; exceeding it fails the CURRENT adapter with
// BUDGET_EXHAUSTED (recorded in health) instead of letting the platform abort the whole invocation.
export const DEFAULT_BUDGET = { fetches: 40, queries: 44 };

// Per-detector share of the 40-subrequest budget, so retries on one provider cannot starve the detectors after it.
// Each share covers that detector's own maximum of logical requests (retries come out of the same share):
//   OKX 1 instruments + 8 pages = 9; Deribit 2 x 4 DVOL pages + 2 book summaries = 10; FRED 2 series + 1 calendar = 3;
//   SEC 8 submissions + 6 documents = 14; Binance 1 WebSocket upgrade. Sum = 40 = DEFAULT_BUDGET.fetches.
export const FETCH_ALLOCATION = { okx_liquidations: 10, binance_liquidation_sample: 1, deribit_options: 10, macro_fred: 5, sec_edgar: 14 };

// A view of the run budget limited to `cap` further subrequests. Spending through it spends the parent, so the
// run total can never exceed the parent limit and this detector can never exceed its cap.
export function childBudget(parent, cap) {
  const start = parent.fetches;
  const limit = Math.min(Number.isFinite(cap) ? cap : Infinity, parent.fetchLimit - start);
  return {
    get fetches() { return parent.fetches - start; },
    set fetches(v) { parent.fetches = start + v; },
    fetchLimit: Math.max(0, limit),
    get queries() { return parent.queries; },
    set queries(v) { parent.queries = v; },
    queryLimit: parent.queryLimit,
  };
}

// Removes credentials that can appear in provider URLs or error text (FRED's api_key query parameter).
export function scrubSecrets(text) {
  return String(text).replace(/(api[_-]?key|token|key)=([^&\s"']+)/gi, '$1=***');
}
export function makeBudget(limits = DEFAULT_BUDGET) {
  return { fetchLimit: limits.fetches, queryLimit: limits.queries, fetches: 0, queries: 0 };
}

// Wraps a D1 binding so every executed statement is counted against the budget.
export function countedDb(db, budget, { reserve = 0 } = {}) {
  const spend = () => {
    if (budget.queries + 1 > budget.queryLimit - reserve) throw new SourceError(SOURCE_STATUS.BUDGET_EXHAUSTED, `D1 query budget reached (${budget.queries}/${budget.queryLimit})`);
    budget.queries += 1;
  };
  return {
    prepare(sql) {
      const st = db.prepare(sql);
      let bound = st;
      const wrap = {
        bind(...args) { bound = st.bind(...args); return wrap; },
        async first(...a) { spend(); return bound.first(...a); },
        async all(...a) { spend(); return bound.all(...a); },
        async run(...a) { spend(); return bound.run(...a); },
      };
      return wrap;
    },
  };
}

// ---- HTTP with timeout, bounded retries and backoff (adapters receive this, never raw fetch) ----
// Descriptive User-Agent for provider requests that need one (no contact email: SEC's contact-bearing UA is separate).
// FRED's edge resets requests that carry no User-Agent or a browser-like one (verified 2026-10-18, see EVENT_DETECTION.md
// section 4); a Worker's fetch sends none by default, which Cloudflare reports to the Worker as HTTP 520.
export const EVENT_DETECTION_USER_AGENT = 'CryptoPulse-EventDetection/ed-v1 (+https://github.com/quiquandon-oss/PulseWorkerV2)';

// Short, secret-free description of a failed response: server header, Cloudflare ray presence, body snippet.
async function responseDiagnostics(res) {
  const h = (k) => (res.headers && res.headers.get ? res.headers.get(k) : null);
  let snippet = '';
  try { snippet = (await res.text()).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 80); } catch (_e) { snippet = ''; }
  const parts = [`server=${h('server') || '-'}`, `cf-ray=${h('cf-ray') ? 'yes' : 'no'}`];
  if (snippet) parts.push(`body="${snippet}"`);
  return scrubSecrets(parts.join(' '));
}

export function makeHttp({ fetchImpl = fetch, timeoutMs = 10_000, retries = 2, backoffMs = 500, sleep, log, budget } = {}) {
  const wait = sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const counters = { requests: 0, retries: 0, rate_limited: 0, errors: 0 };
  async function request(url, init = {}) {
    let attempt = 0;
    for (;;) {
      if (budget) {
        if (budget.fetches + 1 > budget.fetchLimit) throw new SourceError(SOURCE_STATUS.BUDGET_EXHAUSTED, `subrequest budget reached (${budget.fetches}/${budget.fetchLimit})`);
        budget.fetches += 1;
      }
      counters.requests += 1;
      let res;
      try {
        const signal = typeof AbortSignal !== 'undefined' && AbortSignal.timeout ? AbortSignal.timeout(timeoutMs) : undefined;
        res = await fetchImpl(url, { ...init, signal });
      } catch (e) {
        if (attempt < retries) { attempt += 1; counters.retries += 1; await wait(backoffMs * 2 ** (attempt - 1)); continue; }
        counters.errors += 1;
        throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, scrubSecrets(`network error: ${e && e.name === 'TimeoutError' ? 'timeout' : (e && e.message) || e}`));
      }
      if (res.status === 429 || res.status >= 500) {
        if (res.status === 429) counters.rate_limited += 1;
        if (attempt < retries) {
          attempt += 1; counters.retries += 1;
          const ra = Number(res.headers && res.headers.get && res.headers.get('retry-after'));
          await wait(Number.isFinite(ra) && ra > 0 ? Math.min(ra * 1000, 10_000) : backoffMs * 2 ** (attempt - 1));
          continue;
        }
        counters.errors += 1;
        throw new SourceError(res.status === 429 ? SOURCE_STATUS.RATE_LIMITED : SOURCE_STATUS.PROVIDER_ERROR, `HTTP ${res.status} (${await responseDiagnostics(res)})`);
      }
      if (res.status === 451 || res.status === 403) {
        counters.errors += 1;
        throw new SourceError(SOURCE_STATUS.BLOCKED, `HTTP ${res.status} (provider refused access)`);
      }
      if (!res.ok) { counters.errors += 1; throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, `HTTP ${res.status} (${await responseDiagnostics(res)})`); }
      return res;
    }
  }
  async function json(url, init) {
    const res = await request(url, init);
    try { return await res.json(); } catch (_e) { throw new SourceError(SOURCE_STATUS.PROVIDER_ERROR, 'malformed JSON'); }
  }
  async function text(url, init) {
    const res = await request(url, init);
    return res.text();
  }
  // Charges one subrequest made outside request() (e.g. a WebSocket upgrade) to the same budget.
  function spend() {
    if (budget) {
      if (budget.fetches + 1 > budget.fetchLimit) throw new SourceError(SOURCE_STATUS.BUDGET_EXHAUSTED, `subrequest budget reached (${budget.fetches}/${budget.fetchLimit})`);
      budget.fetches += 1;
    }
    counters.requests += 1;
  }
  return { request, json, text, spend, counters, log: log || (() => {}) };
}

// Structured log line (Workers Logs are enabled for this Worker).
export function logEvent(kind, fields) {
  try { console.log(JSON.stringify({ component: 'event-detection', kind, ...fields })); } catch (_e) { /* never throw from logging */ }
}
