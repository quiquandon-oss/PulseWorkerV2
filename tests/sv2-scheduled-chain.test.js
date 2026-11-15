// Scheduled-tick integration for the non-tick hourly cron, through the real Worker entry point (worker.scheduled):
//   event detection -> V1 archive copy -> V2 cycle -> independent evidence collection (RSS / Atom / official feeds)
// on real SQLite with the real migrations 0015, 0023, 0026, 0027 and 0029. Counts every fetch and every D1 call in one
// invocation against the Workers Free limits (50 subrequests, 50 D1 queries), and exercises healthy, empty,
// rate-limited, unreachable, oversized-volume and missing-User-Agent conditions. No network: fetch is a stub.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import worker from '../worker.js';
import { makeD1 } from './replay/d1-sqlite.mjs';
import { SV2_SOURCE_REGISTRY } from '../sentiment-v2/collector.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const mig = (f) => readFileSync(join(ROOT, '.ai/migrations', f), 'utf8');
const DDL = [
  `CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL, sources_json TEXT,
     technical_score INTEGER, gold_regime TEXT, btc_price REAL, regime_mag REAL, bottom_score REAL); CREATE INDEX idx_ts ON history(ts);`,
  `CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL);`,
  mig('0015_experiment5_sentiment_archive.sql'), mig('0023_event_detection.sql'), mig('0026_d1_usage_daily.sql'),
  mig('0027_sentiment_v2_results.sql'), mig('0029_sv2_evidence_and_ai_budget.sql'),
];
const H = 3600000;
const T = Date.UTC(2026, 10, 15, 5, 0, 0);                       // hour 5: a non-prediction hour, so the detection chain runs
const KEYS = ['fng', 'cryptonews', 'foufi', 'funding', 'longshort', 'hypefunding', 'hloi', 'onchain', 'macrogeo', 'usd', 'oil',
  'gold', 'nasdaq', 'sp500', 'ninemag', 'regulatory', 'geopolitics', 'strc'];
const sources = (i) => JSON.stringify(Object.fromEntries(KEYS.map((k, j) => [k, Math.round((50 + 20 * Math.sin(i / (4 + j) + j)) * 100) / 100])));
const UA = 'CryptoPulse research tester@example.invalid';
const REGISTRY_HOSTS = new Set(SV2_SOURCE_REGISTRY.map((s) => new URL(s.url).hostname));
// Every host the Worker may call in this chain: the evidence registry plus the official Event Detection sources.
const ALLOWED_HOSTS = new Set([...REGISTRY_HOSTS, 'www.okx.com', 'www.deribit.com', 'fred.stlouisfed.org', 'api.stlouisfed.org', 'data.sec.gov', 'www.sec.gov', 'fstream.binance.com']);

function build({ historyRows = 120 } = {}) {
  const d1 = makeD1(DDL);
  for (let i = 0; i < historyRows; i++) {
    const ts = T - (historyRows - i) * H;
    d1.db.prepare('INSERT INTO history (ts, score, sources_json, technical_score, btc_price) VALUES (?,?,?,?,?)').run(ts, 50 + Math.round(10 * Math.sin(i / 5)), sources(i), 50, 70000 + 100 * i);
    d1.db.prepare('INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)').run(ts, 70000 + 100 * i);
  }
  return d1;
}

// Wraps the D1 binding: counts every D1 call (a batch counts as one, as D1 does) and enforces the Free limit of 50.
function limitedDb(binding, counter) {
  const bump = () => { counter.n += 1; if (counter.n > 50) throw new Error('Too many API requests by single worker invocation.'); };
  const wrapStmt = (st) => new Proxy(st, { get(target, prop) {
    if (prop === '__orig') return target;
    if (['first', 'all', 'run', 'raw'].includes(prop)) return (...a) => { bump(); return target[prop](...a); };
    if (prop === 'bind') return (...a) => wrapStmt(target.bind(...a));
    return target[prop];
  } });
  return { prepare: (sql) => wrapStmt(binding.prepare(sql)), batch: (list) => { bump(); return binding.batch(list.map((s) => s.__orig || s)); }, exec: binding.exec && ((s) => { bump(); return binding.exec(s); }) };
}

const rssItem = (title, link, ts) => `<item><title><![CDATA[${title}]]></title><link>${link}</link><pubDate>${new Date(ts).toUTCString()}</pubDate><description>d</description></item>`;
const rssBody = (host, n, ts) => `<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>${Array.from({ length: n }, (_, i) => rssItem(`Bitcoin rally extends as ETF inflows surge ${host} ${i}`, `https://${host}/story-${i}`, ts - i * 60000)).join('')}</channel></rss>`;
const atomBody = (host, n, ts) => `<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>${Array.from({ length: n }, (_, i) => `<entry><title>8-K filing ${i}</title><link rel="alternate" href="https://${host}/filing-${i}"/><updated>${new Date(ts - i * 60000).toISOString()}</updated></entry>`).join('')}</feed>`;

// mode: ok | empty | ratelimited | unreachable | volume (applies to the evidence feeds).   Event Detection sources are unavailable in every mode
// (their payload parsing is covered by tests/event-detection.test.js); this test is about the chain, the budgets and the isolation.
function stubFetch(mode, log) {
  return vi.fn(async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    const u = new URL(url);
    log.push({ url, host: u.hostname, headers: init.headers || {} });
    const reg = SV2_SOURCE_REGISTRY.find((s) => s.url === url);
    // Event Detection hosts: unavailable (404 is not retried, so the test does not wait on real backoff sleeps)
    if (!reg) return new Response('blocked in tests', { status: 404 });
    if (mode === 'unreachable') throw new TypeError('network unreachable');
    if (mode === 'ratelimited') return new Response('slow down', { status: 429, headers: { 'retry-after': '120' } });
    const n = mode === 'empty' ? 0 : mode === 'volume' ? 400 : 3;
    const body = reg.kind === 'atom' ? atomBody(u.hostname, n, T - 30 * 60000) : rssBody(u.hostname, n, T - 30 * 60000);
    return new Response(body, { status: 200, headers: { 'content-type': reg.kind === 'atom' ? 'application/atom+xml' : 'application/rss+xml' } });
  });
}

async function tick({ mode = 'ok', d1 = build(), when = T, extraEnv = {} } = {}) {
  const fetchLog = [];
  const counter = { n: 0 };
  const ai = { run: vi.fn(async () => { throw new Error('Workers AI must not be called'); }) };
  const env = { DB: limitedDb(d1.binding, counter), AI: ai, SENTIMENT_V2_ENABLED: '1', SV2_COLLECTOR_ENABLED: '1', EVENT_DETECTION_ENABLED: '1',
    EVENT_SEC_USER_AGENT: UA, SENTIMENT_V2_AI_MODEL: '', SV2_GLM_DAILY_NEURONS: '0', V1_ARCHIVE_SYNC_ENABLED: '1', ...extraEnv };
  vi.stubGlobal('fetch', stubFetch(mode, fetchLog));
  const out = [];
  const logs = [];
  for (const k of ['log', 'warn', 'error']) vi.spyOn(console, k).mockImplementation((...a) => { logs.push(`${k}: ${a.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join(' ')}`); });
  const waits = [];
  await worker.scheduled({ cron: '0 * * * *', scheduledTime: when }, env, { waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
  void out;
  return { d1, fetchLog, d1Calls: counter.n, ai, logs, env };
}
const rows = (d1, sql, ...a) => d1.db.prepare(sql).all(...a).map((r) => ({ ...r }));
const one = (d1, sql, ...a) => d1.db.prepare(sql).get(...a);

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
vi.setConfig({ testTimeout: 60000 });   // the collector's real retry backoff runs in the rate-limited and unreachable cases

describe('hourly non-tick chain: healthy feeds', () => {
  it('detection runs, V1 readings are copied, V2 is written, and evidence is collected, persisted with provenance and bounded', async () => {
    const r = await tick({ mode: 'ok' });
    expect(one(r.d1, "SELECT COUNT(*) n FROM event_runs WHERE trigger = 'cron'").n).toBe(1);           // detection ran first
    expect(one(r.d1, 'SELECT COUNT(*) n FROM research_sentiment_archive').n).toBeGreaterThan(0);       // V1 history copied
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sentiment_v2_results').n).toBeGreaterThan(0);              // V2 output written
    const ev = rows(r.d1, 'SELECT * FROM sv2_evidence');
    expect(ev.length).toBeGreaterThan(0);                                                               // evidence persisted
    for (const e of ev) {                                                                               // with provenance
      expect(e.canonical_url).toMatch(/^https:\/\//);
      expect(e.content_hash).toMatch(/^[0-9a-f]{64}$/);
      expect(e.collector_version).toBeTruthy();
      expect(e.source_id).toBeTruthy();
      expect(e.retrieved_ts).toBe(T);
    }
    const run = one(r.d1, 'SELECT * FROM sv2_collection_runs');
    expect(run.subrequests).toBeLessThanOrEqual(8);
    expect(run.subrequests).toBeGreaterThan(0);
    // shared budget for the whole invocation
    expect(r.fetchLog.length).toBeLessThanOrEqual(50);
    expect(r.d1Calls).toBeLessThanOrEqual(50);
    expect(r.logs.filter((l) => /Too many API requests|BUDGET/.test(l) && l.startsWith('error')).length).toBe(0);
    if (process.env.MEASURE) process.stdout.write(`[measured healthy tick] fetches=${r.fetchLog.length} (limit 50) d1_calls=${r.d1Calls} (limit 50) collector_subrequests=${run.subrequests} (limit 8) evidence_rows=${ev.length} v2_rows=${one(r.d1, 'SELECT COUNT(*) n FROM sentiment_v2_results').n} archive_rows=${one(r.d1, 'SELECT COUNT(*) n FROM research_sentiment_archive').n}\n`);
  });

  it('the next hour scores with the evidence collected before the decision (sv2-1.1.0) and does not duplicate stored items', async () => {
    const first = await tick({ mode: 'ok' });
    const before = one(first.d1, 'SELECT COUNT(*) n FROM sv2_evidence').n;
    // V1 reading at T+1h so V2 has a new decision point after the evidence retrieval time
    first.d1.db.prepare('INSERT INTO history (ts, score, sources_json, technical_score, btc_price) VALUES (?,?,?,?,?)').run(T + H - 60000, 55, sources(121), 50, 82100);
    first.d1.db.prepare('INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)').run(T + H - 60000, 82100);
    const second = await tick({ mode: 'ok', d1: first.d1, when: T + H });
    expect(one(second.d1, 'SELECT COUNT(*) n FROM sv2_evidence').n).toBe(before);                      // feeds are not due again: no duplicates
    const versions = rows(second.d1, 'SELECT DISTINCT calc_version v FROM sentiment_v2_results').map((x) => x.v);
    expect(versions).toContain('sv2-1.1.0');
    expect(second.d1Calls).toBeLessThanOrEqual(50);
  });
});

describe('hourly non-tick chain: degraded sources', () => {
  it('empty feeds: nothing is invented, V2 still runs, the run is recorded', async () => {
    const r = await tick({ mode: 'empty' });
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sv2_evidence').n).toBe(0);
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sv2_collection_runs').n).toBe(1);
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sentiment_v2_results').n).toBeGreaterThan(0);
    expect(r.d1Calls).toBeLessThanOrEqual(50);
  });

  it('rate-limited (429) feeds: failures and backoff are recorded, retries stay inside the per-run budget, V2 is unaffected', async () => {
    const r = await tick({ mode: 'ratelimited' });
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sv2_evidence').n).toBe(0);
    const state = rows(r.d1, 'SELECT source_id, last_status, consecutive_failures FROM sv2_source_state');
    expect(state.length).toBeGreaterThan(0);
    for (const s of state) { expect(s.consecutive_failures).toBeGreaterThanOrEqual(1); expect(s.last_status).not.toBe('OK'); }
    expect(one(r.d1, 'SELECT subrequests FROM sv2_collection_runs').subrequests).toBeLessThanOrEqual(8);
    expect(r.fetchLog.length).toBeLessThanOrEqual(50);
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sentiment_v2_results').n).toBeGreaterThan(0);
  });

  it('unreachable network: the tick completes, every failure is recorded, nothing throws out of scheduled()', async () => {
    const r = await tick({ mode: 'unreachable' });
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sv2_evidence').n).toBe(0);
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sv2_collection_runs').n).toBe(1);
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sentiment_v2_results').n).toBeGreaterThan(0);
    expect(r.fetchLog.length).toBeLessThanOrEqual(50);
  });

  it('very large feeds: D1 queries and subrequests stay bounded however many items arrive', async () => {
    const r = await tick({ mode: 'volume' });
    expect(r.d1Calls).toBeLessThanOrEqual(50);
    expect(r.fetchLog.length).toBeLessThanOrEqual(50);
    expect(one(r.d1, 'SELECT subrequests FROM sv2_collection_runs').subrequests).toBeLessThanOrEqual(8);
    expect(r.logs.filter((l) => l.startsWith('error') && /Too many API requests/.test(l))).toEqual([]);
    if (process.env.MEASURE) process.stdout.write(`[measured volume tick, 400 items/feed] fetches=${r.fetchLog.length} d1_calls=${r.d1Calls} evidence_rows=${one(r.d1, 'SELECT COUNT(*) n FROM sv2_evidence').n}\n`);
  });

  it('collector off (SV2_COLLECTOR_ENABLED unset): no feed is fetched and V2 still runs', async () => {
    const r = await tick({ mode: 'ok', extraEnv: { SV2_COLLECTOR_ENABLED: undefined } });
    expect(r.fetchLog.filter((f) => REGISTRY_HOSTS.has(f.host) && !/okx|deribit|stlouisfed|data\.sec\.gov|fstream/.test(f.host))).toEqual([]);
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sv2_evidence').n).toBe(0);
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sentiment_v2_results').n).toBeGreaterThan(0);
  });
});

describe('hourly non-tick chain: free-only and privacy invariants', () => {
  it('calls only the allowlisted public hosts, never Workers AI, and writes no Workers AI ledger row', async () => {
    const r = await tick({ mode: 'ok' });
    for (const f of r.fetchLog) expect(ALLOWED_HOSTS.has(f.host), f.host).toBe(true);
    for (const f of r.fetchLog) expect(JSON.stringify(f.headers).toLowerCase()).not.toMatch(/authorization|api-key|x-api-key|bearer/);
    expect(r.ai.run).not.toHaveBeenCalled();
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sv2_ai_neurons').n).toBe(0);
    expect(one(r.d1, 'SELECT COUNT(*) n FROM sentiment_v2_ai_calls').n).toBe(0);
  });

  it('GLM stays off even when a model is configured but the Neuron budget is 0', async () => {
    const r = await tick({ mode: 'ok', extraEnv: { SENTIMENT_V2_AI_MODEL: '@cf/zai-org/glm-4.7-flash', SV2_GLM_DAILY_NEURONS: '0' } });
    expect(r.ai.run).not.toHaveBeenCalled();
  });

  it('SEC sources are fetched only with the configured User-Agent, which is never written to logs or the database', async () => {
    const withUa = await tick({ mode: 'ok' });
    const sec = withUa.fetchLog.filter((f) => f.host === 'www.sec.gov' || f.host === 'data.sec.gov');
    expect(sec.length).toBeGreaterThan(0);
    for (const f of sec) expect(JSON.stringify(f.headers)).toContain('tester@example.invalid');
    expect(withUa.logs.join('\n')).not.toContain('tester@example.invalid');
    const dump = JSON.stringify([rows(withUa.d1, 'SELECT * FROM sv2_evidence'), rows(withUa.d1, 'SELECT * FROM sv2_source_state'), rows(withUa.d1, 'SELECT * FROM sv2_collection_runs'), rows(withUa.d1, 'SELECT * FROM event_runs'), rows(withUa.d1, 'SELECT * FROM event_source_health')]);
    expect(dump).not.toContain('tester@example.invalid');
    vi.unstubAllGlobals(); vi.restoreAllMocks();
    const noUa = await tick({ mode: 'ok', extraEnv: { EVENT_SEC_USER_AGENT: undefined } });
    expect(noUa.fetchLog.filter((f) => f.host === 'www.sec.gov' || f.host === 'data.sec.gov')).toEqual([]);   // not configured: not fetched
    expect(one(noUa.d1, 'SELECT COUNT(*) n FROM sv2_evidence').n).toBeGreaterThan(0);                           // the other feeds proceed
  });
});
