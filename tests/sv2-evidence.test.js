// Sentiment V2 scope lock (2026-11-14): independent evidence collector, deterministic evidence scoring (sv2-1.1.0),
// GLM Neuron budget, and their wiring. Storage tests run on real SQLite with migrations 0015/0027/0029.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';
import { makeD1 } from './replay/d1-sqlite.mjs';
import * as C from '../sentiment-v2/collector.js';
import * as E from '../sentiment-v2/evidence-score.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const WORKER = readFileSync(join(ROOT, 'worker.js'), 'utf8');
const MIG = (f) => readFileSync(join(ROOT, '.ai/migrations', f), 'utf8');
const DDL = [MIG('0015_experiment5_sentiment_archive.sql'), MIG('0027_sentiment_v2_results.sql'), MIG('0029_sv2_evidence_and_ai_budget.sql'),
  `CREATE TABLE event_ledger (event_id TEXT, dedup_key TEXT, correlation_key TEXT, event_type TEXT, asset TEXT, source TEXT, source_ts INTEGER,
     detected_ts INTEGER, direction TEXT, severity TEXT, confidence REAL, rule_id TEXT, expires_ts INTEGER);
   CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL);
   CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL, sources_json TEXT);`];
const H = 3600000;
const NOW = Date.UTC(2026, 10, 15, 10, 0, 0);

const rss = (items) => `<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>${items.map(i =>
  `<item><title><![CDATA[${i.title}]]></title><link>${i.link}</link><pubDate>${new Date(i.ts).toUTCString()}</pubDate><description>${i.desc || ''}</description></item>`).join('')}</channel></rss>`;
const resp = (body, { status = 200, headers = {} } = {}) => new Response(body, { status, headers: { 'content-type': 'application/rss+xml', ...headers } });
const HOSTS = C.sv2RegistryHosts();
const REG = [{ id: 'a', name: 'A', family: 'market_sentiment_news', kind: 'rss', url: 'https://feeds.example.org/a.xml', interval_min: 60, freshness_h: 24 },
  { id: 'b', name: 'B', family: 'market_sentiment_news', kind: 'rss', url: 'https://news.example.net/b.xml', interval_min: 60, freshness_h: 24 }];
const noSleep = async () => {};

describe('URL safety (SSRF)', () => {
  it('accepts only https URLs on registry hosts, default port, no credentials, no IP literals or internal names', () => {
    expect(C.sv2CheckUrl('https://decrypt.co/feed', HOSTS).ok).toBe(true);
    for (const [u, why] of [['http://decrypt.co/feed', /https/], ['https://evil.example.com/x', /allowlist/], ['https://127.0.0.1/x', /IP literal/],
      ['https://[::1]/x', /IP literal/], ['https://2130706433/x', /IP literal/], ['https://0x7f000001/x', /IP literal/], ['https://localhost/x', /internal/],
      ['https://metadata.internal/x', /internal/], ['https://user:pw@decrypt.co/feed', /credentials/], ['https://decrypt.co:8443/feed', /port/], ['file:///etc/passwd', /https/], ['not a url', /invalid/]]) {
      const r = C.sv2CheckUrl(u, HOSTS);
      expect(r.ok, u).toBe(false);
      expect(r.reason, u).toMatch(why);
    }
  });
  it('every registry URL is https, unique, on its own allowlisted host, with a known family', () => {
    const fams = ['market_sentiment_news', 'derivatives_positioning', 'institutional_flows_etf', 'onchain_activity', 'macroeconomic',
      'cross_asset_equities', 'market_structure_breadth', 'regulation_geopolitics', 'corporate_treasury_catalysts'];
    const ids = new Set();
    for (const s of C.SV2_SOURCE_REGISTRY) {
      expect(C.sv2CheckUrl(s.url, HOSTS).ok, s.id).toBe(true);
      expect(fams).toContain(s.family);
      expect(ids.has(s.id)).toBe(false); ids.add(s.id);
      expect(s.interval_min).toBeGreaterThanOrEqual(60);
    }
  });
  it('excludes the feeds V1 already reads, so V2 evidence is independent of V1 readings', () => {
    const hosts = [...HOSTS].join(' ');
    for (const v1 of ['cointelegraph.com', 'coindesk.com', 'theblock.co', 'bbci.co.uk', 'investing.com']) expect(hosts).not.toContain(v1);
  });
});

describe('bounded fetch', () => {
  const hosts = new Set(['feeds.example.org', 'cdn.example.org']);
  const run = (fetchImpl, extra = {}) => C.sv2SafeFetch('https://feeds.example.org/a.xml', { allowedHosts: hosts, budget: { subrequests: 0, bytes: 0 }, fetchImpl, sleep: noSleep, ...extra });
  it('follows a redirect only to an allowlisted https host, re-validating each hop, at most twice', async () => {
    let n = 0;
    const ok = await run(async (u) => (n++ === 0 ? new Response(null, { status: 301, headers: { location: 'https://cdn.example.org/a.xml' } }) : resp(rss([]))));
    expect(ok).toMatchObject({ ok: true, status: 'OK', redirects: 1, finalUrl: 'https://cdn.example.org/a.xml' });
    const bad = await run(async () => new Response(null, { status: 302, headers: { location: 'http://169.254.169.254/latest/meta-data' } }));
    expect(bad).toMatchObject({ ok: false, status: 'BLOCKED_URL' });
    const loop = await run(async () => new Response(null, { status: 302, headers: { location: 'https://feeds.example.org/a.xml' } }));
    expect(loop.status).toBe('TOO_MANY_REDIRECTS');
  });
  it('requests with redirect: manual, a timeout signal and the collector User-Agent', async () => {
    let init;
    await run(async (u, i) => { init = i; return resp(rss([])); });
    expect(init.redirect).toBe('manual');
    expect(init.signal).toBeDefined();
    expect(init.headers['User-Agent']).toBe(C.SV2_COLLECTOR_USER_AGENT);
  });
  it('retries once on network error, timeout, 429 or 5xx, never on 4xx', async () => {
    for (const fail of [() => { throw new Error('reset'); }, () => resp('x', { status: 429 }), () => resp('x', { status: 503 })]) {
      let n = 0;
      const r = await run(async () => { n++; return fail(); });
      expect(r.ok).toBe(false);
      expect(n).toBe(2);
    }
    let n = 0;
    const r = await run(async () => { n++; return resp('x', { status: 404 }); });
    expect([r.status, n]).toEqual(['HTTP_ERROR', 1]);
  });
  it('refuses oversized responses (declared or streamed), unexpected content types, and stops at the run budget', async () => {
    const big = 'x'.repeat(C.SV2_COLLECTOR_LIMITS.maxBytesPerResponse + 1);
    expect((await run(async () => resp(big, { headers: { 'content-length': String(big.length) } }))).status).toBe('TOO_LARGE');
    expect((await run(async () => resp(big))).status).toBe('TOO_LARGE');
    expect((await run(async () => resp('{}', { headers: { 'content-type': 'application/octet-stream' } }))).status).toBe('BAD_CONTENT_TYPE');
    const budget = { subrequests: C.SV2_COLLECTOR_LIMITS.maxSubrequestsPerRun, bytes: 0 };
    let n = 0;
    expect((await run(async () => { n++; return resp(rss([])); }, { budget })).status).toBe('BUDGET_EXHAUSTED');
    expect(n).toBe(0);
  });
  it('retries once when the body read fails after the headers arrived (Codex P2)', async () => {
    let n = 0;
    const broken = () => new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode('<rss>')); c.error(new Error('connection reset')); } }),
      { status: 200, headers: { 'content-type': 'application/rss+xml' } });
    const r = await run(async () => (n++ === 0 ? broken() : resp(rss([]))));
    expect([r.ok, n]).toEqual([true, 2]);
    n = 0;
    const r2 = await run(async () => { n++; return broken(); });
    expect([r2.status, n]).toEqual(['NETWORK_ERROR', 2]);
    expect(r2.error).toMatch(/body read failed/);
  });
  it('treats 304 as success without a body', async () => {
    expect(await run(async () => new Response(null, { status: 304 }))).toMatchObject({ ok: true, notModified: true });
  });
});

describe('feed parsing, canonical URLs and hashing', () => {
  it('parses RSS with CDATA and entities, and Atom alternate links', () => {
    const r = C.sv2ParseFeed(rss([{ title: 'BTC &amp; ETH <b>rally</b>', link: 'https://x.org/a', ts: NOW, desc: '&lt;p&gt;hi&lt;/p&gt;' }]));
    expect(r.items[0]).toMatchObject({ title: 'BTC & ETH rally', link: 'https://x.org/a', published_ts: Math.floor(NOW / 1000) * 1000 });
    const atom = `<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>8-K filing</title><link rel="alternate" href="https://www.sec.gov/x"/><updated>2026-11-14T10:00:00Z</updated></entry></feed>`;
    expect(C.sv2ParseFeed(atom).items[0]).toMatchObject({ title: '8-K filing', link: 'https://www.sec.gov/x', published_ts: Date.UTC(2026, 10, 14, 10) });
    expect(C.sv2ParseFeed('<html>no feed</html>').ok).toBe(false);
    const many = rss(Array.from({ length: 60 }, (_, i) => ({ title: `t${i}`, link: `https://x.org/${i}`, ts: NOW })));
    expect(C.sv2ParseFeed(many).items.length).toBe(C.SV2_COLLECTOR_LIMITS.maxItemsPerFeed);
  });
  it('canonicalises URLs: https, no www, no fragment, no tracking parameters, sorted query, no trailing slash', () => {
    expect(C.sv2CanonicalUrl('http://www.Example.com/a/b/?utm_source=x&z=2&a=1&fbclid=q#frag')).toBe('https://example.com/a/b?a=1&z=2');
    expect(C.sv2CanonicalUrl('javascript:alert(1)')).toBeNull();
  });
  it('sha256 matches node:crypto', async () => {
    expect(await C.sv2Sha256('abc')).toBe(createHash('sha256').update('abc').digest('hex'));
  });
});

describe('scheduling and backoff', () => {
  it('fetches only due sources, most overdue first, at most the per-run limit, with exponential backoff after failures', () => {
    const reg = Array.from({ length: 6 }, (_, i) => ({ id: `s${i}`, interval_min: 60 }));
    const st = { s0: { last_attempt_ts: NOW - 30 * 60000 }, s1: { last_attempt_ts: NOW - 3 * H, consecutive_failures: 2 }, s2: { last_attempt_ts: NOW - 5 * H, consecutive_failures: 2 } };
    const due = C.sv2DueSources(reg, st, NOW, C.SV2_COLLECTOR_LIMITS, new Set(['s5'])).map(s => s.id);
    expect(due).not.toContain('s0');   // not due yet
    expect(due).not.toContain('s1');   // 2 failures → 4 h interval
    expect(due).not.toContain('s5');   // disabled
    expect(due.length).toBeLessThanOrEqual(C.SV2_COLLECTOR_LIMITS.maxSourcesPerRun);
    expect(due[0]).toBe('s3');         // never attempted = most overdue
  });
});

describe('collection run (real SQLite, migration 0029)', () => {
  const env = (d1, extra = {}) => ({ DB: d1.binding, SV2_COLLECTOR_ENABLED: '1', ...extra });
  const feeds = (map) => async (u, init) => { const f = map[new URL(u).hostname]; return typeof f === 'function' ? f(u, init) : resp(f); };
  const items = (pfx, n, ts = NOW - H) => Array.from({ length: n }, (_, i) => ({ title: `${pfx} bitcoin rally record ${i}`, link: `https://${pfx}.example/${i}?utm_source=rss`, ts }));
  it('stores new items once with provenance, records reliability state and the run with its budget use', async () => {
    const d1 = makeD1(DDL);
    const f = feeds({ 'feeds.example.org': rss(items('a', 3)), 'news.example.net': rss(items('b', 2)) });
    const r1 = await C.sv2CollectEvidence(env(d1), { nowTs: NOW, fetchImpl: f, registry: REG, sleep: noSleep });
    expect(r1).toMatchObject({ ok: true, items_new: 5, items_duplicate: 0 });
    expect(r1.budget.subrequests).toBe(2);
    const rows = d1.db.prepare('SELECT * FROM sv2_evidence ORDER BY canonical_url').all();
    expect(rows.length).toBe(5);
    expect(rows[0]).toMatchObject({ source_id: 'a', family: 'market_sentiment_news', canonical_url: 'https://a.example/0', retrieved_ts: NOW, dup_of: null, collector_version: C.SV2_COLLECTOR_VERSION });
    expect(rows[0].evidence_id).toMatch(/^ev_[0-9a-f]{32}$/);
    const st = d1.db.prepare("SELECT * FROM sv2_source_state WHERE source_id = 'a'").get();
    expect(st).toMatchObject({ last_status: 'OK', consecutive_failures: 0, ok_count: 1, fail_count: 0, last_new_items: 3, last_success_ts: NOW });
    expect(d1.db.prepare('SELECT COUNT(*) AS n FROM sv2_collection_runs').get().n).toBe(1);
    // an hour later the same items are not stored again
    const r2 = await C.sv2CollectEvidence(env(d1), { nowTs: NOW + H + 1, fetchImpl: f, registry: REG, sleep: noSleep });
    expect(r2.items_new).toBe(0);
    expect(d1.db.prepare('SELECT COUNT(*) AS n FROM sv2_evidence').get().n).toBe(5);
  });
  it('marks a story another source already published as a duplicate (kept, excluded from scoring)', async () => {
    const d1 = makeD1(DDL);
    const same = [{ title: 'SEC approves spot ETF', link: 'https://a.example/x', ts: NOW - H }];
    await C.sv2CollectEvidence(env(d1), { nowTs: NOW, fetchImpl: feeds({ 'feeds.example.org': rss(same), 'news.example.net': rss([]) }), registry: REG, sleep: noSleep });
    const r = await C.sv2CollectEvidence(env(d1), { nowTs: NOW + 2 * H, fetchImpl: feeds({ 'feeds.example.org': rss([]), 'news.example.net': rss([{ ...same[0], title: 'SEC Approves Spot ETF!', link: 'https://b.example/y' }]) }), registry: REG, sleep: noSleep });
    expect(r.items_duplicate).toBe(1);
    const dup = d1.db.prepare("SELECT dup_of FROM sv2_evidence WHERE source_id = 'b'").get();
    expect(dup.dup_of).toMatch(/^ev_/);
  });
  it('sends conditional headers, counts failures with backoff, reports NOT_CONFIGURED for SEC without a User-Agent', async () => {
    const d1 = makeD1(DDL);
    let seen;
    await C.sv2CollectEvidence(env(d1), { nowTs: NOW, fetchImpl: feeds({ 'feeds.example.org': () => resp(rss([]), { headers: { etag: '"v1"' } }), 'news.example.net': () => resp('x', { status: 500 }) }), registry: REG, sleep: noSleep });
    await C.sv2CollectEvidence(env(d1), { nowTs: NOW + 2 * H, fetchImpl: feeds({ 'feeds.example.org': (u, i) => { seen = i.headers; return new Response(null, { status: 304 }); }, 'news.example.net': () => resp('x', { status: 500 }) }), registry: REG, sleep: noSleep });
    expect(seen['If-None-Match']).toBe('"v1"');
    const b = d1.db.prepare("SELECT * FROM sv2_source_state WHERE source_id = 'b'").get();
    expect(b).toMatchObject({ consecutive_failures: 2, fail_count: 2, last_status: 'HTTP_ERROR' });
    const sec = [{ ...REG[0], id: 'sec', ua: 'sec' }];
    let calls = 0;
    const r = await C.sv2CollectEvidence(env(d1), { nowTs: NOW, fetchImpl: async () => { calls++; return resp(rss([])); }, registry: sec, sleep: noSleep });
    expect([r.sources[0].status, calls, r.ok]).toEqual(['NOT_CONFIGURED', 0, true]);
  });
  it('is off unless SV2_COLLECTOR_ENABLED = "1", and fails visibly when the tables are missing', async () => {
    const d1 = makeD1(DDL);
    let calls = 0;
    expect(await C.sv2CollectEvidence({ DB: d1.binding }, { nowTs: NOW, fetchImpl: async () => { calls++; }, registry: REG })).toMatchObject({ skipped: true });
    expect(calls).toBe(0);
    const bare = makeD1([]);
    const r = await C.sv2CollectEvidence({ DB: bare.binding, SV2_COLLECTOR_ENABLED: '1' }, { nowTs: NOW, fetchImpl: async () => resp(rss([])), registry: REG });
    expect(r.ok).toBe(false);
    expect(r.errors[0]).toMatch(/no such table/);
  });
  it('issues at most 2 + 3 × feeds D1 queries per run, however many new items arrive (Codex P1: D1 per-invocation limit)', async () => {
    const d1 = makeD1(DDL);
    let queries = 0, inBatch = false;
    const counted = { ...d1.binding,
      prepare: (sql) => { const st = d1.binding.prepare(sql); const wrap = (o) => ({ ...o, bind: (...a) => wrap(o.bind(...a)),
        first: (...x) => { if (!inBatch) queries++; return o.first(...x); }, all: (...x) => { if (!inBatch) queries++; return o.all(...x); },
        run: (...x) => { if (!inBatch) queries++; return o.run(...x); } }); return wrap(st); },
      batch: async (stmts) => { queries++; inBatch = true; try { return await d1.binding.batch(stmts); } finally { inBatch = false; } } };
    const reg3 = [...REG, { ...REG[0], id: 'c', url: 'https://feeds.example.org/c.xml' }];
    const f = async (u) => resp(rss(items(new URL(u).pathname.slice(1, 2), 25)));
    const r = await C.sv2CollectEvidence({ DB: counted, SV2_COLLECTOR_ENABLED: '1' }, { nowTs: NOW, fetchImpl: f, registry: reg3, sleep: noSleep });
    expect(r.items_new).toBe(75);
    expect(queries).toBeLessThanOrEqual(2 + 3 * C.SV2_COLLECTOR_LIMITS.maxSourcesPerRun);
    expect(d1.db.prepare('SELECT COUNT(*) AS n FROM sv2_evidence').get().n).toBe(75);
    expect(d1.db.prepare('SELECT COUNT(*) AS n FROM sv2_source_state').get().n).toBe(3);
  });
  it('spends only the D1 headroom it is given: defers feeds it could not finish, skips when none fits (Codex P2)', async () => {
    const d1 = makeD1(DDL);
    const reg3 = [...REG, { ...REG[0], id: 'c', url: 'https://feeds.example.org/c.xml' }];
    const f = async (u) => resp(rss(items(new URL(u).pathname.slice(1, 2), 3)));
    let calls = 0;
    const counted = async (u) => { calls++; return f(u); };
    const skip = await C.sv2CollectEvidence({ DB: d1.binding, SV2_COLLECTOR_ENABLED: '1' }, { nowTs: NOW, fetchImpl: counted, registry: reg3, sleep: noSleep, queryBudget: 4 });
    expect(skip).toMatchObject({ skipped: true });
    expect(calls).toBe(0);
    const part = await C.sv2CollectEvidence({ DB: d1.binding, SV2_COLLECTOR_ENABLED: '1' }, { nowTs: NOW, fetchImpl: counted, registry: reg3, sleep: noSleep, queryBudget: 8 });
    expect(part.sources.length).toBe(2);                        // (8 - 2) / 3 = 2 feeds
    expect(part.deferred_for_d1_budget.length).toBe(1);
  });
  it('a failed batch write records STORE_ERROR state for that source and the run continues', async () => {
    const d1 = makeD1(DDL);
    let calls = 0;
    const b = { ...d1.binding, batch: async (stmts) => { if (++calls === 2) throw new Error('D1_ERROR: overloaded'); return d1.binding.batch(stmts); } };
    const r = await C.sv2CollectEvidence({ DB: b, SV2_COLLECTOR_ENABLED: '1' }, { nowTs: NOW, fetchImpl: feeds({ 'feeds.example.org': rss(items('a', 2)), 'news.example.net': rss(items('b', 2)) }), registry: REG, sleep: noSleep });
    expect(r.ok).toBe(false);
    expect(r.sources.map(x => x.status)).toContain('STORE_ERROR');
    expect(r.sources.length).toBe(2);
    expect(d1.db.prepare("SELECT COUNT(*) AS n FROM sv2_source_state WHERE last_status = 'STORE_ERROR'").get().n).toBe(1);
  });
  it('writes only to sv2_* tables and reads no V1 table', () => {
    const src = readFileSync(join(ROOT, 'sentiment-v2/collector.js'), 'utf8');
    const tables = [...src.matchAll(/\b(?:INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM|FROM)\s+(\w+)/g)].map(m => m[1]).filter(t => t !== 'SET');   // DO UPDATE SET (upsert)
    expect(new Set(tables)).toEqual(new Set(['sv2_source_state', 'sv2_evidence', 'sv2_collection_runs']));
  });
});

describe('deterministic evidence scoring', () => {
  it('uses V1 lexicon verbatim (pinned) and V1 arithmetic', () => {
    // sha256 of JSON.stringify(SCOMP_SENT_LEXICON) in PulseWorker worker.js (V1, 108 terms), computed 2026-11-14.
    const digest = createHash('sha256').update(JSON.stringify(E.SV2_SENT_LEXICON)).digest('hex');
    expect(digest).toBe('989054b353254c14925435bc2eff0c2d4bc3bf5755ddbf6f1defc3a3d9903bc6');
    expect(Object.keys(E.SV2_SENT_LEXICON).length).toBe(108);
    expect(E.sv2ScoreHeadline('Bitcoin rally hits record high')).toEqual({ score: 4, hits: 3 });
    expect(E.sv2ScoreHeadline('Exchange hacked; rate hike fears')).toEqual({ score: -6, hits: 4 });   // 'rate hike' and 'hike' both count, as in V1
    expect(E.sv2HeadlineBatchTo0100(0, 0)).toBe(50);
    expect(E.sv2HeadlineBatchTo0100(4, 3)).toBe(Math.round((Math.tanh(4 / 3 / 1.5) + 1) * 50));
  });
  const reg = [{ id: 'n', family: 'market_sentiment_news', freshness_h: 24 }, { id: 'm', family: 'macroeconomic', freshness_h: 168 }, { id: 'q', family: 'macroeconomic', freshness_h: 24 }];
  const ev = (src, title, retrieved, pub = retrieved, extra = {}) => ({ evidence_id: `ev_${src}_${retrieved}_${title.length}`, source_id: src, title, retrieved_ts: retrieved, published_ts: pub, canonical_url: 'https://x', dup_of: null, ...extra });
  it('states: fresh (scored), insufficient, stale and missing — none imputed as neutral', () => {
    const rows = [ev('n', 'Bitcoin rally record', NOW - H), ev('n', 'ETF inflows surge', NOW - 2 * H), ev('m', 'Fed announces meeting dates', NOW - 3 * H),
      ev('q', 'Old bullish surge', NOW - 50 * H)];
    const r = E.sv2EvidenceReadings(rows, NOW, reg);
    expect(r['ev:n']).toMatchObject({ state: 'fresh', items: 2, hits: 4 });
    expect(r['ev:n'].value).toBeGreaterThan(50);
    expect(r['ev:m'].state).toBe('insufficient');
    expect(r['ev:q'].state).toBe('stale');
    expect(E.sv2EvidenceReadings([], NOW, reg)['ev:n'].state).toBe('missing');
  });
  it('is point-in-time: items retrieved after t, duplicates and items outside the freshness window never count', () => {
    const rows = [ev('n', 'Bitcoin crash panic', NOW + 60000), ev('n', 'Bitcoin rally surge', NOW - H, NOW - H, { dup_of: 'ev_x' }),
      ev('n', 'Bitcoin rally record', NOW - H, NOW - 30 * H)];
    expect(E.sv2EvidenceReadings(rows, NOW, reg)['ev:n'].state).toBe('stale');
    // a publication time after retrieval (bad clock) falls back to retrieval time
    const r = E.sv2EvidenceReadings([ev('n', 'rally surge record', NOW - H, NOW + 5 * H)], NOW, reg);
    expect(r['ev:n']).toMatchObject({ state: 'fresh', latest_ts: NOW - H });
  });
});

// ---- sv2-1.1.0 cycle, API and GLM ledger on real SQLite ------------------------------------------------------------
const FUNCS = ['sv2Config', 'sv2Finite', 'sv2ParseSources', 'sv2History', 'sv2AttachPrices', 'sv2IndexAtOrBefore', 'sv2PriceAt', 'sv2SourceStatus',
  'sv2Pearson', 'sv2FamilyOf', 'sv2Redundancy', 'sv2Families', 'sv2EventFamily', 'sv2EventReactions', 'sv2Events', 'sv2Regime', 'sv2Profile',
  'sv2Analogues', 'sv2Classify', 'sv2EvidenceSummary', 'sv2Compute', 'sv2EvidenceItemsForAi', 'sv2AiPackage', 'sv2ValidateAiOutput', 'sv2AiBudget',
  'sv2AiEstimateNeurons', 'sv2UngroundedNumbers', 'sv2RunAi', 'sv2Row', 'runSentimentV2Cycle', 'sv2AlignSeries', 'sv2SeriesStatus', 'sv2LatestView',
  'getSentimentV2Comparison', 'getSentimentV2Status', 'getSentimentV2Evidence', 'sv2GlmProbe'];
const CONSTS = ['SENTIMENT_V2_VERSION', 'SV2_VERSIONS_SHOWN', 'SV2_INSERT_SQL', 'SV2_PERIODS_MS', 'SV2_MAX_POINTS', 'SV2_MAX_ROWS', 'SV2_MAX_PRICE_ROWS',
  'SV2_STALE_MS', 'SV2_AI_PRICING', 'SV2_AI_MAX_OUTPUT_TOKENS', 'SV2_AI_RESERVE_SQL', 'SV2_AI_TEMPLATE_TOKENS'];
const S = evalInScope(extractConstants(...CONSTS) + '\n' + extractFunctions(...FUNCS), {
  SV2_SOURCE_REGISTRY: C.SV2_SOURCE_REGISTRY, SV2_COLLECTOR_LIMITS: C.SV2_COLLECTOR_LIMITS, SV2_COLLECTOR_VERSION: C.SV2_COLLECTOR_VERSION,
  sv2EvidenceReadings: E.sv2EvidenceReadings, sv2ScoreHeadline: E.sv2ScoreHeadline, SV2_EVIDENCE_RULES: E.SV2_EVIDENCE_RULES, SV2_LEXICON_SOURCE: E.SV2_LEXICON_SOURCE });
const KEYS = ['fng', 'cryptonews', 'funding', 'longshort', 'hloi', 'onchain', 'macrogeo', 'usd', 'oil', 'gold', 'nasdaq', 'sp500', 'regulatory', 'geopolitics'];
function seedArchive(d1, n, end = NOW - 30 * 60000) {
  for (let i = 0; i < n; i++) {
    const ts = end - (n - 1 - i) * 2 * H;
    const src = Object.fromEntries(KEYS.map((k, j) => [k, Math.round((50 + 20 * Math.sin(i / (5 + j) + j)) * 100) / 100]));
    d1.db.prepare("INSERT INTO research_sentiment_archive (observation_ts, sources_json, score, source_weights_version, schema_version, written_by, content_hash, archived_ts) VALUES (?, ?, ?, 'v1-unversioned', 'exp5-v1', 't', 'h', 1)")
      .run(ts, JSON.stringify(src), 40);
    d1.db.prepare('INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)').run(ts - 60000, 60000 + 10 * i);
  }
}
const addEvidence = (d1, src, title, retrieved, pub = retrieved) => d1.db.prepare("INSERT INTO sv2_evidence (evidence_id, source_id, family, canonical_url, title, published_ts, retrieved_ts, title_hash, content_hash, collector_version) VALUES (?, ?, ?, 'https://x', ?, ?, ?, ?, 'c', 'v')")
  .run(`ev_${src}_${retrieved}_${title.length}`, src, C.SV2_SOURCE_REGISTRY.find(s => s.id === src).family, title, pub, retrieved, `${title}${retrieved}`);

describe('sv2-1.1.0 cycle with evidence (real SQLite)', () => {
  const env = (d1, extra = {}) => ({ DB: d1.binding, SENTIMENT_V2_ENABLED: '1', SV2_COLLECTOR_ENABLED: '1', ...extra });
  it('writes sv2-1.1.0 results that include evidence retrieved before the decision, and nothing retrieved after', async () => {
    const d1 = makeD1(DDL);
    seedArchive(d1, 60);
    const t = NOW - 30 * 60000;
    addEvidence(d1, 'decrypt', 'Bitcoin rally hits record high as ETF inflows surge', t - 2 * H);
    addEvidence(d1, 'decrypt', 'Crypto market crash panic liquidation hack', t + 10 * 60000);   // after the decision
    const r = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    expect(r.written).toBe(1);
    const row = d1.db.prepare('SELECT * FROM sentiment_v2_results').get();
    expect(row.calc_version).toBe('sv2-1.1.0');
    const fam = JSON.parse(row.family_json);
    expect(fam.evidence['ev:decrypt']).toMatchObject({ state: 'fresh', items: 1 });
    expect(fam.evidence['ev:decrypt'].value).toBeGreaterThan(50);
    expect(fam.families.market_sentiment_news.sources_fresh).toContain('ev:decrypt');
    expect(fam.evidence_status).toBe('ok');
    expect(JSON.parse(row.supporting_ids_json).concat(JSON.parse(row.contradicting_ids_json)).some(x => x.startsWith('evidence:ev_decrypt'))).toBe(true);
  });
  it('without any evidence the score equals the V1-sources-only computation (no evidence is imputed)', async () => {
    const a = makeD1(DDL), b = makeD1(DDL);
    seedArchive(a, 60); seedArchive(b, 60);
    await S.runSentimentV2Cycle(env(a), { nowTs: NOW });
    const hist = S.sv2History(b.db.prepare('SELECT observation_ts, score, btc_price, sources_json FROM research_sentiment_archive ORDER BY observation_ts').all());
    S.sv2AttachPrices(hist, b.db.prepare('SELECT ts, btc_price FROM btc_data').all(), 3 * H);
    const plain = S.sv2Compute(hist, hist.length - 1, [], S.sv2Config());
    expect(a.db.prepare('SELECT sentiment_score FROM sentiment_v2_results').get().sentiment_score).toBe(plain.sentiment_score);
  });
  it('degrades with evidence_unavailable when migration 0029 is missing, and never fails the cycle', async () => {
    const d1 = makeD1(DDL.filter(x => !x.includes('sv2_evidence')));
    seedArchive(d1, 60);
    const r = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    expect(r.written).toBe(1);
    expect(d1.db.prepare('SELECT status, degraded_reason FROM sentiment_v2_results').get()).toEqual({ status: 'DEGRADED', degraded_reason: 'evidence_unavailable' });
  });
  it('the comparison API shows sv2-1.0.0 history and sv2-1.1.0 together, labelled, with the switch time', async () => {
    const d1 = makeD1(DDL);
    seedArchive(d1, 60);
    const older = NOW - 30 * 60000 - 2 * H;
    d1.db.prepare("INSERT INTO sentiment_v2_results (asset, decision_ts, calc_version, status, sentiment_score, ai_status, created_ts) VALUES ('BTC', ?, 'sv2-1.0.0', 'OK', 33.3, 'disabled', 1)").run(older);
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    const api = await S.getSentimentV2Comparison(env(d1), '24h', NOW);
    const v2pts = api.points.filter(p => p.v2 !== null);
    expect(v2pts.map(p => p.v2_version)).toEqual(['sv2-1.0.0', 'sv2-1.1.0']);
    expect(api.current_calc_version).toBe('sv2-1.1.0');
    expect(api.current_version_since).toBe(NOW - 30 * 60000);
    expect(api.v2_latest.calc_version).toBe('sv2-1.1.0');
    expect(api.v2_latest.evidence_status).toBe('ok');
  });
  it('status and evidence routes are bounded, read-only and work before migration 0029', async () => {
    const d1 = makeD1(DDL);
    addEvidence(d1, 'decrypt', 'Bitcoin rally record', NOW - H);
    const st = await S.getSentimentV2Status(env(d1, { SENTIMENT_V2_AI_MODEL: '@cf/zai-org/glm-4.7-flash', SV2_GLM_DAILY_NEURONS: '1000' }), NOW);
    expect(st.sources.length).toBe(C.SV2_SOURCE_REGISTRY.length);
    expect(st.sources.find(s => s.id === 'decrypt')).toMatchObject({ last_status: 'NEVER_RUN', fresh: true });
    expect(st.ai).toMatchObject({ enabled: true, daily_neuron_budget: 1000, remaining_neurons: 1000, account_free_allocation_neurons: 10000 });
    const ev = await S.getSentimentV2Evidence(env(d1), NOW, 24, 50);
    expect(ev.items[0]).toMatchObject({ source_id: 'decrypt', source_name: 'Decrypt', lexicon_hits: 2 });
    const bare = makeD1([]);
    expect((await S.getSentimentV2Status({ DB: bare.binding }, NOW)).errors.length).toBeGreaterThan(0);
    expect((await S.getSentimentV2Evidence({ DB: bare.binding }, NOW, 24, 50)).available).toBe(false);
    const writes = d1.log.filter(l => /^(INSERT|UPDATE|DELETE)/i.test(l.sql));
    expect(writes).toEqual([]);
  });
});

describe('GLM Neuron ledger on real SQLite', () => {
  const GLM = '@cf/zai-org/glm-4.7-flash';
  it('pricing matches the published GLM-4.7-flash Neuron rates', () => {
    expect(S.SV2_AI_PRICING[GLM]).toEqual({ neurons_per_m_input: 5500, neurons_per_m_output: 36400 });
  });
  it('the reservation SQL admits calls only while the committed total stays within the budget', async () => {
    const d1 = makeD1(DDL);
    let calls = 0;
    const env = { DB: d1.binding, SENTIMENT_V2_AI_MODEL: GLM, SV2_GLM_DAILY_NEURONS: '150',
      AI: { run: async () => { calls++; return { response: JSON.stringify({ summary: 'ok', cited_ids: ['probe:a'], contradictions: [], event_relevance: [], uncertainty: 'probe' }), usage: { prompt_tokens: 400, completion_tokens: 200 } }; } } };
    const results = [];
    for (let k = 0; k < 10; k++) results.push(await S.sv2GlmProbe(env, NOW));
    const rows = d1.db.prepare('SELECT * FROM sv2_ai_neurons').all();
    const committed = rows.reduce((s2, r) => s2 + Math.max(r.reserved_neurons, r.actual_neurons || 0), 0);
    expect(committed).toBeLessThanOrEqual(150);
    expect(rows.length).toBe(calls);
    expect(results.filter(r => r.status === 'quota_guard').length).toBe(10 - calls);
    expect(rows[0]).toMatchObject({ status: 'ok', input_tokens: 400, output_tokens: 200 });
    expect(rows[0].actual_neurons).toBeCloseTo((400 * 5500 + 200 * 36400) / 1e6, 3);
    const st = await S.getSentimentV2Status(env, NOW);
    expect(st.ai.calls).toBe(calls);
    expect(st.ai.remaining_neurons).toBeLessThanOrEqual(150 - committed + 0.01);
  });
  it('a budget stop leaves the deterministic result OK; only AI faults degrade it', async () => {
    const d1 = makeD1(DDL);
    seedArchive(d1, 60);
    let calls = 0;
    await S.runSentimentV2Cycle({ DB: d1.binding, SENTIMENT_V2_ENABLED: '1', SENTIMENT_V2_AI_MODEL: GLM, SV2_GLM_DAILY_NEURONS: '1', AI: { run: async () => { calls++; } } }, { nowTs: NOW });
    const row = d1.db.prepare('SELECT status, ai_status, ai_json FROM sentiment_v2_results').get();
    expect([row.status, row.ai_status, calls]).toEqual(['OK', 'quota_guard', 0]);
    expect(JSON.parse(row.ai_json).reason).toMatch(/daily Neuron budget 1 would be exceeded/);
  });
  it('the reservation bounds input by UTF-8 bytes, so multilingual titles cannot exceed it (Codex P2)', () => {
    const p = S.SV2_AI_PRICING[GLM];
    const text = '比特币价格暴跌，市场恐慌'.repeat(50);                       // CJK: 3 bytes per character
    const bytes = new TextEncoder().encode(text).length;
    const est = S.sv2AiEstimateNeurons(p, bytes);
    const worstTokens = bytes + S.SV2_AI_TEMPLATE_TOKENS;                // ≥ any byte-level tokenisation
    expect(est).toBeGreaterThanOrEqual((worstTokens * p.neurons_per_m_input + S.SV2_AI_MAX_OUTPUT_TOKENS * p.neurons_per_m_output) / 1e6 - 0.01);
  });
  it('grounding matches whole numbers only, never fragments of longer values (Codex P2)', () => {
    const pkg = JSON.stringify({ decision_ts: 1794655417334, score: 14.5, n: 2, price: 66989 });
    expect(S.sv2UngroundedNumbers(['Family at 14.5 from 2 sources; BTC 66,989'], pkg)).toEqual([]);
    expect(S.sv2UngroundedNumbers(['Support near 179 and 655'], pkg)).toEqual(['179', '655']);   // fragments of decision_ts
    expect(S.sv2UngroundedNumbers(['Score 14'], pkg)).toEqual(['14']);                                // not 14.5
  });
  it('the number-grounding check rejects figures absent from the package', () => {
    expect(S.sv2UngroundedNumbers(['Regulatory family at 14.5 with 2 sources'], '{"score":14.5,"n":2}')).toEqual([]);
    expect(S.sv2UngroundedNumbers(['BTC to 150,000 by Friday'], '{"score":14.5}')).toEqual(['150,000']);
  });
});

describe('wiring', () => {
  const sched = WORKER.slice(WORKER.indexOf("} else if (event.cron === '0 * * * *') {"), WORKER.indexOf('// No catch-all else'));
  it('non-tick cron order: detection → V1 archive copy → V2 → evidence collection (last, on the remaining D1 headroom), each failure logged', () => {
    const order = ['runAutomaticDetection', 'syncV1ArchiveFromHistory', 'runSentimentV2Cycle', 'sv2CollectEvidence'].map(f => sched.indexOf(f));
    expect(sched).toMatch(/queryBudget: SV2_D1_QUERIES_PER_INVOCATION - SV2_D1_QUERY_RESERVE - sv2Q\.n/);
    expect(order.every((x, i) => x > 0 && (i === 0 || x > order[i - 1]))).toBe(true);
    expect(sched).toMatch(/console\.error\('sv2 evidence \(cron\) FAILED:'/);
    expect(sched).toMatch(/\.catch\(err => console\.error\('SV2 evidence \(cron\) failed:'/);
  });
  it('the run route is owner-only, and no new Cron Trigger was added', () => {
    expect(WORKER).toMatch(/'POST:\/api\/sentiment-v2\/run',/);
    const toml = readFileSync(join(ROOT, 'wrangler.toml'), 'utf8');
    expect(toml).toMatch(/crons = \["0 \* \* \* \*", "0 7 \* \* \*"\]/);
    expect(toml).toMatch(/SV2_COLLECTOR_ENABLED = "0"/);
  });
  it('staging config targets only the staging Worker and database, with the AI binding', () => {
    const t = readFileSync(join(ROOT, 'wrangler.staging.toml'), 'utf8');
    expect(t).toMatch(/^name = "pulseworker-v2-staging"$/m);
    expect(t).toMatch(/database_id = "5458d504-2778-49ae-bd25-7751f1c49d50"/);
    expect(t).not.toMatch(/f91ca980-b886-423a-bd6f-f3baea46d181/);
    expect(t).toMatch(/\[ai\]\nbinding = "AI"/);
  });
});
