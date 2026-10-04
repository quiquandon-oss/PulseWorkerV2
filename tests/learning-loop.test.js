// Learning loop, vertical slice 1: event -> V1 source assessment -> Research Case -> research pack -> pasted AI answer
// -> structured finding -> human confirmation. Pure functions are tested directly; the API and routes run the real
// worker.js fetch handler against an in-memory SQLite database that uses production's table definitions plus Stage 7's
// own migration 0016, so every SQL statement is executed for real.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { describe, it, expect, beforeEach } from 'vitest';
import {
  V1_METHODOLOGY_V1, v1Composite, describeMarketEvent, assessV1Sources, buildResearchCase, buildResearchPack,
  parseAiResearchResponse, findingToStage7Registration, nearestAtOrBefore,
} from '../learning/learning-core.js';
import worker from '../worker.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
// Loaded via require: Vite does not resolve the node:sqlite builtin (Node >= 22.5) as an ESM import.
const { DatabaseSync } = createRequire(import.meta.url)('node:sqlite');
const MIGRATION_0016 = readFileSync(join(__dirname, '..', '.ai', 'migrations', '0016_stage7_research_pipeline.sql'), 'utf8');
const MIGRATION_0021 = readFileSync(join(__dirname, '..', '.ai', 'migrations', '0021_learning_loop.sql'), 'utf8');
const H = 3600000;

const ALL_NEUTRAL = Object.fromEntries(V1_METHODOLOGY_V1.sources.map((s) => [s.id, 50]));

describe('V1 composite (methodology v1 snapshot)', () => {
  it('is the confidence-weighted average over resolved sources', () => {
    expect(v1Composite(ALL_NEUTRAL).score).toBe(50);
    const real = { fng: 65, funding: 50, longshort: 44, global: 75, cryptonews: 61, macrogeo: 25, geopolitics: 6, regulatory: 42, sosovalue: 50, onchain: 5, oil: 48, yield10y: 83, usd: 51, nasdaq: 77, sp500: 68, ninemag: 85, foufi: 25, etfflows: 89, hypefunding: 50, gold: 51, strc: 49 };
    expect(v1Composite(real).score).toBe(54);
    expect(v1Composite({ fng: 80 }).score).toBe(80);
    expect(v1Composite({})).toBeNull();
  });
  it('carries the 21 V1 source ids', () => {
    expect(V1_METHODOLOGY_V1.sources).toHaveLength(21);
  });
});

describe('describeMarketEvent', () => {
  const ts = Date.UTC(2026, 8, 28, 3, 0);
  it('explains each event category in plain language', () => {
    expect(describeMarketEvent({ event_id: 1, category: 'LARGE_MOVE', direction: 'UP', intensity: 6.2, event_ts: ts }, { atEvent: 106200, before: 100000 }).headline).toMatch(/^BTC rose 6\.2% in 24 hours/);
    expect(describeMarketEvent({ event_id: 2, category: 'REGIME_REVERSAL', direction: 'rally_to_chop', event_ts: ts }, null).headline).toMatch(/flipped from rally to chop/);
    expect(describeMarketEvent({ event_id: 3, category: 'VOLATILITY_EXPANSION', intensity: 1.67, event_ts: ts }, null).headline).toMatch(/1\.67x its normal level/);
    expect(describeMarketEvent({ event_id: 4, category: 'V2_FAILURE_CLUSTER', direction: 'BTC_24h', intensity: 5, event_ts: ts }, null).headline).toMatch(/got 5 BTC 24h calls wrong in a row/);
  });
  it('uses the detector\'s own move for LARGE_MOVE, prices otherwise', () => {
    const d = describeMarketEvent({ event_id: 1, category: 'LARGE_MOVE', direction: 'UP', intensity: 6.07, event_ts: ts }, { atEvent: 86204, before: 87014 });
    expect(d.btc_move_24h_pct).toBe(6.07);
    expect(d.btc_move_text).toMatch(/\+6\.1% over the 24 hours the event detector measured/);
    const v = describeMarketEvent({ event_id: 2, category: 'VOLATILITY_EXPANSION', intensity: 1.6, event_ts: ts }, { atEvent: 97000, before: 100000 });
    expect(v.btc_move_24h_pct).toBe(-3);
    expect(v.btc_move_text).toMatch(/-3\.0%.*100,000 -> 97,000/);
  });
});

describe('assessV1Sources', () => {
  const before = { score: 50, sources: { ...ALL_NEUTRAL } };
  it('EXPLAINED when most V1 weight leaned the way BTC moved', () => {
    const at = { score: 70, sources: Object.fromEntries(V1_METHODOLOGY_V1.sources.map((s) => [s.id, 75])) };
    const a = assessV1Sources({ before, at, btcMovePct: 4 });
    expect(a.verdict).toBe('EXPLAINED');
    expect(a.sources.every((s) => s.verdict === 'EXPLAINS')).toBe(true);
  });
  it('NOT_EXPLAINED, with contradicting sources named, when V1 pointed the other way', () => {
    const at = { score: 70, sources: Object.fromEntries(V1_METHODOLOGY_V1.sources.map((s) => [s.id, 75])) };
    const a = assessV1Sources({ before: { score: 62, sources: at.sources }, at, btcMovePct: -4 });
    expect(a.verdict).toBe('NOT_EXPLAINED');
    expect(a.v1_lean_before).toBe('UP');
    expect(a.v1_called_it).toBe(false);
    const rc = buildResearchCase({ event_id: 9 }, a, []);
    expect(rc.contradicted_by.length).toBe(21);
    expect(rc.sufficiency_status).toBe('CONFLICTING');
    expect(rc.reasons.join(' ')).toMatch(/V1 leaned UP \(62\)/);
  });
  it('PARTIAL when a neutral source moved toward the actual direction', () => {
    const at = { score: 52, sources: { ...ALL_NEUTRAL, etfflows: 54 } };
    const a = assessV1Sources({ before: { score: 50, sources: { ...ALL_NEUTRAL, etfflows: 40 } }, at, btcMovePct: 3 });
    expect(a.sources.find((s) => s.id === 'etfflows').verdict).toBe('PARTIAL');
    expect(a.verdict).toBe('NOT_EXPLAINED');
  });
  it('a group follows the verdict with the most weight in it, not its single best source', () => {
    const at = { score: 50, sources: { ...ALL_NEUTRAL, foufi: 80, cryptonews: 30, sosovalue: 30 } };
    const a = assessV1Sources({ before: { score: 50, sources: at.sources }, at, btcMovePct: 3 });
    expect(a.sources.find((s) => s.id === 'foufi').verdict).toBe('EXPLAINS');
    expect(a.groups.find((g) => g.group === 'NEWS').status).toBe('CONTRADICTS');
  });
  it('NO_V1_DATA when there is no V1 reading at the event', () => {
    const a = assessV1Sources({ before: null, at: null, btcMovePct: 7 });
    expect(a.verdict).toBe('NO_V1_DATA');
    expect(buildResearchCase({ event_id: 1 }, a, []).reasons[0]).toMatch(/No V1 sentiment reading/);
  });
  it('NO_DIRECTIONAL_MOVE / NO_PRICE_DATA / MISSING sources', () => {
    expect(assessV1Sources({ before, at: before, btcMovePct: 0.3 }).verdict).toBe('NO_DIRECTIONAL_MOVE');
    expect(assessV1Sources({ before, at: before, btcMovePct: null }).verdict).toBe('NO_PRICE_DATA');
    const a = assessV1Sources({ before, at: { score: 50, sources: { fng: 50 } }, btcMovePct: 2 });
    expect(a.sources.find((s) => s.id === 'etfflows').verdict).toBe('MISSING');
  });
  it('nearestAtOrBefore respects the maximum gap', () => {
    const rows = [{ ts: 0 }, { ts: 10 * H }];
    expect(nearestAtOrBefore(rows, 11 * H).ts).toBe(10 * H);
    expect(nearestAtOrBefore(rows, 20 * H)).toBeNull();
    expect(nearestAtOrBefore(rows, -1)).toBeNull();
  });
});

const GOOD_ANSWER = `Here is my research.\nBTC fell as options expiry pinned price...\n\n\`\`\`json
{
  "explanation": "A large quarterly options expiry with max pain at 82k pulled BTC down.",
  "primary_driver": "Quarterly options expiry",
  "driver_category": "options",
  "finding_type": "new source",
  "covered_by_existing_v1_source": "none",
  "proposed_new_source": { "name": "Deribit options OI", "url": "https://www.deribit.com/statistics/BTC/options-open-interest", "what_it_measures": "BTC options open interest by strike", "update_frequency": "hourly", "free_or_paid": "free" },
  "proposed_signal": "Distance of spot from max pain in the 48h before expiry",
  "trend": "Recurring around quarterly expiries",
  "evidence": [ { "claim": "$4bn notional expired", "url": "https://example.com/a", "publisher": "CoinDesk", "date": "2026-09-26" }, { "claim": "bad link", "url": "javascript:alert(1)", "publisher": "x", "date": "" } ],
  "alternative_explanations": ["ETF outflows", "Macro data"],
  "limitations": "Could not verify exact max pain level",
  "confidence": "medium",
  "sentiment_assessment": "negative",
}
\`\`\``;

describe('parseAiResearchResponse (untrusted input)', () => {
  it('extracts, normalizes and whitelists the JSON block', () => {
    const r = parseAiResearchResponse(GOOD_ANSWER);
    expect(r.ok).toBe(true);
    expect(r.finding.driver_category).toBe('OPTIONS');
    expect(r.finding.finding_type).toBe('NEW_SOURCE');
    expect(r.finding.confidence).toBe('MEDIUM');
    expect(r.finding.sentiment_assessment).toBe('NEGATIVE');
    expect(r.finding.proposed_new_source.url).toBe('https://www.deribit.com/statistics/BTC/options-open-interest');
    expect(r.finding.evidence[1].url).toBe('');
    expect(r.warnings.join(' ')).toMatch(/trailing commas/);
    expect(r.warnings.join(' ')).toMatch(/no valid http/);
  });
  it('rejects unknown enum values and non-V1 source ids instead of trusting them', () => {
    const r = parseAiResearchResponse('```json\n{"explanation":"x","driver_category":"DROP TABLE","confidence":"certain","covered_by_existing_v1_source":"secret"}\n```');
    expect(r.finding.driver_category).toBe('OTHER');
    expect(r.finding.confidence).toBe('LOW');
    expect(r.finding.covered_by_existing_v1_source).toBe('none');
  });
  it('keeps HTML/script text as inert data (the UI escapes it)', () => {
    const r = parseAiResearchResponse('```json\n{"explanation":"<img src=x onerror=alert(1)>"}\n```');
    expect(r.finding.explanation).toBe('<img src=x onerror=alert(1)>');
  });
  it('uses the last JSON fence and falls back to a manual draft when there is none', () => {
    expect(parseAiResearchResponse('```json\n{"explanation":"first"}\n```\n```json\n{"explanation":"last"}\n```').finding.explanation).toBe('last');
    const r = parseAiResearchResponse('Just prose, no JSON at all.');
    expect(r.ok).toBe(false);
    expect(r.finding.explanation).toBe('Just prose, no JSON at all.');
    expect(parseAiResearchResponse('').ok).toBe(false);
  });
  it('maps a confirmed finding onto the existing Stage 7 registration body', () => {
    const body = findingToStage7Registration('stage7-req-15-1', 'claude', parseAiResearchResponse(GOOD_ANSWER).finding);
    expect(body.validated).toBe(true);
    expect(body.findings.schema).toBe('learning-finding-v1');
    expect(body.findings.summary).toMatch(/options expiry/);
    expect(body.findings.sentiment_assessment).toBe('NEGATIVE');
    expect(body.sources[0]).toEqual({ url: 'https://example.com/a', publisher: 'CoinDesk', publication_date: '2026-09-26', claim: '$4bn notional expired' });
    expect(findingToStage7Registration('r', 'evil<script>', {}).provider).toBeNull();
  });
});

describe('buildResearchPack', () => {
  it('contains the event, every V1 source, the uncovered drivers and the answer format, and nothing internal', () => {
    const d = describeMarketEvent({ event_id: 15, category: 'LARGE_MOVE', direction: 'DOWN', intensity: 4, event_ts: Date.UTC(2026, 8, 28) }, { atEvent: 96000, before: 100000 });
    const a = assessV1Sources({ before: { score: 60, sources: ALL_NEUTRAL }, at: { score: 58, sources: ALL_NEUTRAL }, btcMovePct: -4 });
    const pack = buildResearchPack(d, a, buildResearchCase(d, a, []), [{ publisher: 'CoinDesk', headline: 'BTC slides', evidence_relation: 'PRE_EVENT' }]);
    expect(pack).toMatch(/BTC fell 4\.0%/);
    for (const s of V1_METHODOLOGY_V1.sources) expect(pack).toContain(`- ${s.id} [`);
    expect(pack).toMatch(/options positioning/i);
    expect(pack).toMatch(/"proposed_new_source"/);
    expect(pack).toMatch(/\[pre-event\] CoinDesk: BTC slides/);
    for (const section of ['Analyse this CryptoPulse research case.', 'EVENT ID: 15', 'EVENT CATEGORY: LARGE_MOVE', 'EVENT TIME:', 'MARKET PERIOD STUDIED:', 'PRICE MOVE:', 'V1 SENTIMENT:', 'V1 PREDICTION BEFORE THE MOVE: UP', 'V1 SOURCES', 'SOURCES THAT SUPPORTED THE MOVE:', 'SOURCES THAT CONTRADICTED IT:', 'UNEXPLAINED AREA:', 'EXISTING SOURCE LIMITATIONS:', 'EVIDENCE WE COLLECTED', 'RESEARCH QUESTION:', 'Identify what CryptoPulse is currently missing.', 'EVIDENCE (verifiable', 'INFERENCE', 'SPECULATION', '"inference"', '"speculation"']) expect(pack).toContain(section);
    expect(pack).not.toMatch(/workers\.dev|STAGE7|token|\bD1\b|sqlite|stage7_/i);
  });
});

// ---------------- API + routes against real SQLite ----------------
const BASE_SCHEMA = `
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL, btc_price REAL, sources_json TEXT, technical_score INTEGER, gold_regime TEXT, regime_mag REAL, bottom_score INTEGER, global_mcap REAL);
CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL NOT NULL, technical_score INTEGER);
CREATE TABLE research_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL, event_ts INTEGER NOT NULL, detection_ts INTEGER NOT NULL, category TEXT NOT NULL, direction TEXT, intensity REAL, available_before_prediction INTEGER NOT NULL, is_post_event_analysis INTEGER NOT NULL DEFAULT 0, trigger_metric TEXT, trigger_threshold REAL, trigger_version TEXT);
CREATE TABLE research_event_evidence (evidence_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES research_events(event_id), feed_url TEXT NOT NULL, article_url TEXT NOT NULL, publisher TEXT NOT NULL, publication_ts INTEGER NOT NULL, collection_ts INTEGER NOT NULL, headline TEXT NOT NULL, keyword_score REAL, evidence_relation TEXT NOT NULL, content_hash TEXT NOT NULL);
CREATE TABLE research_sentiment_archive (archive_id INTEGER PRIMARY KEY AUTOINCREMENT, observation_ts INTEGER NOT NULL, sources_json TEXT, score REAL, technical_score REAL, btc_price REAL, gold_regime TEXT, source_weights_version TEXT NOT NULL, schema_version TEXT NOT NULL, written_by TEXT NOT NULL, content_hash TEXT NOT NULL, archived_ts INTEGER NOT NULL);
`;

function makeEnv({ withStage7 = true, withLearning = true, token = 'learning-test-admin-token-7f3a' } = {}) {
  const db = new DatabaseSync(':memory:');
  db.exec(BASE_SCHEMA);
  if (withStage7) db.exec(MIGRATION_0016);
  if (withStage7 && withLearning) db.exec(MIGRATION_0021);
  const E = Date.UTC(2026, 8, 28, 3, 0);
  const ins = (sql, rows) => { const st = db.prepare(sql); for (const r of rows) st.run(...r); };
  ins('INSERT INTO research_events (event_id,fingerprint,event_ts,detection_ts,category,direction,intensity,available_before_prediction,trigger_metric,trigger_threshold,trigger_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)', [
    [14, 'LARGE_MOVE|a', E - 72 * H, E - 70 * H, 'LARGE_MOVE', 'UP', 6.2, 1, '24h_btc_return_pct', 4, 'pr3-v1'],
    [15, 'LARGE_MOVE|b', E, E + H, 'LARGE_MOVE', 'DOWN', 4.1, 1, '24h_btc_return_pct', 4, 'pr3-v1'],
  ]);
  ins('INSERT INTO research_event_evidence (event_id,feed_url,article_url,publisher,publication_ts,collection_ts,headline,keyword_score,evidence_relation,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)',
    [1, 2, 3].map((k) => [15, 'https://feed', 'https://news/' + k, 'CoinDesk', E - k * H, E + H, 'Headline ' + k, null, 'PRE_EVENT', 'h' + k]));
  const bullish = Object.fromEntries(V1_METHODOLOGY_V1.sources.map((s) => [s.id, 70]));
  // Event 14 (UP): V1 bullish -> explained. Event 15 (DOWN): V1 stayed bullish -> not explained.
  const obs = [];
  for (let t = E - 100 * H; t <= E; t += H) obs.push([t, JSON.stringify(bullish), 66, 'v1-unversioned', 'exp5-v1', 'experiment5-pipeline', 'c' + t, t]);
  ins('INSERT INTO research_sentiment_archive (observation_ts,sources_json,score,source_weights_version,schema_version,written_by,content_hash,archived_ts) VALUES (?,?,?,?,?,?,?,?)', obs);
  ins('INSERT INTO history (ts,score,sources_json) VALUES (?,?,?)', [[E + 2 * H, 51, JSON.stringify(ALL_NEUTRAL)]]);
  const btc = [];
  for (let t = E - 100 * H; t <= E - 72 * H; t += H) btc.push([t, 80000 + (t - (E - 100 * H)) / H * 200]);
  for (let t = E - 71 * H; t <= E - 24 * H; t += H) btc.push([t, 86000]);
  for (let t = E - 23 * H; t <= E; t += H) btc.push([t, 86000 - (t - (E - 24 * H)) / H * 150]);
  ins('INSERT INTO btc_data (ts,btc_price) VALUES (?,?)', btc);
  function stmt(sql) {
    let args = [];
    const o = {
      bind(...a) { args = a; return o; },
      async first() { const r = db.prepare(sql).get(...args); return r ? { ...r } : null; },
      async all() { return { results: db.prepare(sql).all(...args).map((r) => ({ ...r })) }; },
      async run() { const r = db.prepare(sql).run(...args); return { meta: { changes: r.changes } }; },
    };
    return o;
  }
  const env = { DB: { prepare: stmt } };
  if (token) env.STAGE7_ADMIN_TOKEN = token;
  return { env, db, token };
}

async function call(env, path, { method = 'GET', body, token } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = 'Bearer ' + token;
  const res = await worker.fetch(new Request('https://w.test' + path, { method, headers, body: body ? JSON.stringify(body) : undefined }), env, { waitUntil() {} });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch (_e) { /* html */ }
  return { status: res.status, json, text };
}

const counts = (db) => Object.fromEntries(['history', 'btc_data', 'research_events', 'research_event_evidence', 'research_sentiment_archive', 'stage7_research_requests', 'stage7_research_responses', 'stage7_event_sentiment', 'learning_candidates', 'v1_methodology_versions']
  .map((t) => { try { return [t, db.prepare(`SELECT COUNT(*) AS n FROM ${t}`).get().n]; } catch (_e) { return [t, null]; } }));

describe('Learning API + routes (real worker.js fetch, real SQLite)', () => {
  let ctx;
  beforeEach(() => { ctx = makeEnv(); });

  it('/research-lab serves the learning UI; the technical page moves to /research-lab/advanced', async () => {
    const main = await call(ctx.env, '/research-lab');
    expect(main.text).toContain('<title>CryptoPulse Research</title>');
    expect(main.text).toContain('/research-lab/advanced');
    const adv = await call(ctx.env, '/research-lab/advanced');
    expect(adv.text).toContain('Research Lab');
    expect(adv.text).not.toContain('<title>CryptoPulse Research</title>');
  });

  it('MARKET: explains each event, assesses V1, and focuses on the unexplained one', async () => {
    const { json } = await call(ctx.env, '/api/learning/market');
    expect(json.ok).toBe(true);
    const byId = Object.fromEntries(json.events.map((e) => [e.event_id, e]));
    expect(byId[14].verdict).toBe('EXPLAINED');
    expect(byId[15].verdict).toBe('NOT_EXPLAINED');
    expect(byId[15].v1_lean_before).toBe('UP');
    expect(byId[15].actual_direction).toBe('DOWN');
    expect(byId[15].headline).toMatch(/BTC fell 4\.1%/);
    expect(byId[15].evidence_count).toBe(3);
    expect(byId[15].journey.map((s) => s.state)).toEqual(['DONE', 'WARN', 'TODO', 'LOCKED', 'LOCKED', 'LOCKED']);
    expect(json.focus_event_id).toBe(15);
    expect(json.latest.v1.score).toBe(51);
  });

  it('RESEARCH: case + copyable pack for one event; unknown event is a 404', async () => {
    const { json } = await call(ctx.env, '/api/learning/case?event_id=15');
    expect(json.ok).toBe(true);
    expect(json.research_case.question).toMatch(/BTC fall/);
    expect(json.research_case.evidence_count).toBe(3);
    expect(json.research_pack).toMatch(/Headline 1/);
    expect(json.case).toBeNull();
    const market = (await call(ctx.env, '/api/learning/market')).json;
    expect(json.assessment.explained_share).toBe(market.events.find((e) => e.event_id === 15).explained_share);
    expect(json.assessment.v1_before_ts).toBe(market.events.length && json.assessment.v1_before_ts);
    expect((await call(ctx.env, '/api/learning/case?event_id=999')).status).toBe(404);
    expect((await call(ctx.env, '/api/learning/case?event_id=abc')).status).toBe(400);
  });

  it('/parse is read-only and returns a structured draft', async () => {
    const before = counts(ctx.db);
    const { json } = await call(ctx.env, '/api/learning/parse', { method: 'POST', body: { text: GOOD_ANSWER } });
    expect(json.ok).toBe(true);
    expect(json.finding.driver_category).toBe('OPTIONS');
    expect(counts(ctx.db)).toEqual(before);
  });

  it('confirming a finding fails closed without a configured token and rejects a wrong token, writing nothing', async () => {
    const noToken = makeEnv({ token: null });
    const finding = parseAiResearchResponse(GOOD_ANSWER).finding;
    const before = counts(noToken.db);
    expect((await call(noToken.env, '/api/learning/findings', { method: 'POST', body: { event_id: 15, finding }, token: 'x' })).status).toBe(503);
    expect(counts(noToken.db)).toEqual(before);
    const before2 = counts(ctx.db);
    expect((await call(ctx.env, '/api/learning/findings', { method: 'POST', body: { event_id: 15, finding }, token: 'wrong' })).status).toBe(401);
    expect((await call(ctx.env, '/api/learning/findings', { method: 'POST', body: { event_id: 15, finding } })).status).toBe(401);
    expect(counts(ctx.db)).toEqual(before2);
  });

  it('confirming a finding writes exactly one case + one VALIDATED response, nothing else, and shows up as LEARN', async () => {
    const before = counts(ctx.db);
    const finding = parseAiResearchResponse(GOOD_ANSWER).finding;
    const r = await call(ctx.env, '/api/learning/findings', { method: 'POST', body: { event_id: 15, provider: 'claude', finding }, token: ctx.token });
    expect(r.status).toBe(200);
    expect(r.json).toMatchObject({ ok: true, validation_status: 'VALIDATED', request_id: 'stage7-req-15-1' });
    const after = counts(ctx.db);
    expect(after.stage7_research_requests).toBe(before.stage7_research_requests + 1);
    expect(after.stage7_research_responses).toBe(before.stage7_research_responses + 1);
    for (const t of ['history', 'btc_data', 'research_events', 'research_event_evidence', 'research_sentiment_archive', 'stage7_event_sentiment']) expect(after[t]).toBe(before[t]);
    const req = ctx.db.prepare('SELECT * FROM stage7_research_requests').get();
    expect(req).toMatchObject({ event_id: 15, schema_version: 'learning-case-v1', status: 'RESEARCH_COMPLETED', sufficiency_status: 'CONFLICTING', publish_attempts: 0 });
    expect(JSON.parse(req.evidence_snapshot_json).evidence_ids).toEqual([1, 2, 3]);
    expect(req.input_fingerprint).toMatch(/^[0-9a-f]{64}$/);
    const resp = ctx.db.prepare('SELECT * FROM stage7_research_responses').get();
    expect(resp.provider).toBe('claude');
    expect(JSON.parse(resp.findings_json).proposed_new_source.name).toBe('Deribit options OI');
    const view = (await call(ctx.env, '/api/learning/case?event_id=15')).json;
    expect(view.journey.find((s) => s.key === 'RESEARCH').text).toBe('Finding confirmed');
    expect(view.journey.find((s) => s.key === 'LEARNING').state).toBe('ACTIVE');
    expect(view.case.finding.validation_status).toBe('VALIDATED');
    const market = (await call(ctx.env, '/api/learning/market')).json;
    expect(market.events.find((e) => e.event_id === 15).journey[1].state).toBe('DONE');
    // A second confirmation never overwrites the first.
    const again = await call(ctx.env, '/api/learning/findings', { method: 'POST', body: { event_id: 15, finding }, token: ctx.token });
    expect(again.status).toBe(409);
    expect(counts(ctx.db)).toEqual(after);
  });

  it('a finding without an explanation is refused before anything is registered', async () => {
    const r = await call(ctx.env, '/api/learning/findings', { method: 'POST', body: { event_id: 15, finding: { explanation: '' } }, token: ctx.token });
    expect(r.status).toBe(400);
    expect(counts(ctx.db).stage7_research_responses).toBe(0);
  });

  it('without the Stage 7 tables, reads still work and writes are refused (503)', async () => {
    const bare = makeEnv({ withStage7: false });
    const m = (await call(bare.env, '/api/learning/market')).json;
    expect(m.ok).toBe(true);
    expect(m.stage7_available).toBe(false);
    const r = await call(bare.env, '/api/learning/findings', { method: 'POST', body: { event_id: 15, finding: { explanation: 'x' } }, token: bare.token });
    expect(r.status).toBe(503);
  });
});

// ---------------- Slices 2-6: candidate -> adjustment -> recalculation -> validation -> decision -> version ----------------
import {
  baselineConfig, applyAdjustment, normalizeAdjustment, scoreWithConfig, dataAvailability, recalculate, buildContexts,
  validateRecalculation, candidateFromFinding, nextVersionId,
} from '../learning/learning-method.js';

describe('methodology: adjustments and scoring', () => {
  const base = baselineConfig();
  const readings = { ...ALL_NEUTRAL, etfflows: 90 };
  it('baseline reproduces the slice-1 V1 composite', () => {
    expect(scoreWithConfig(base, { readings: ALL_NEUTRAL }).score).toBe(50);
    expect(scoreWithConfig(base, { readings }).score).toBe(v1Composite(readings).score);
  });
  it('every adjustment type changes the configuration as described, never in place', () => {
    const mk = (a) => applyAdjustment(base, normalizeAdjustment(a, base).adjustment);
    expect(mk({ type: 'ADD_SOURCE', label: 'Options OI', group: 'DERIVATIVES', weight: 5, confidence: 0.4 }).sources.map((s) => s.id)).toContain('options_oi');
    expect(mk({ type: 'REMOVE_SOURCE', source_id: 'etfflows' }).sources.some((s) => s.id === 'etfflows')).toBe(false);
    expect(mk({ type: 'CHANGE_WEIGHT', source_id: 'etfflows', weight: 0, confidence: 0.8 }).sources.find((s) => s.id === 'etfflows').weight).toBe(0);
    expect(mk({ type: 'CHANGE_CLASSIFICATION', source_id: 'etfflows', group: 'NEWS', invert: true }).sources.find((s) => s.id === 'etfflows')).toMatchObject({ group: 'NEWS', invert: true });
    expect(mk({ type: 'ADD_SIGNAL', label: 'ETF momentum', derived_from: 'etfflows', weight: 5, confidence: 0.4 }).signals).toHaveLength(1);
    expect(mk({ type: 'ADD_REGIME_CONDITION', source_id: 'funding', op: '<=', value: -2, multiplier: 2 }).sources.find((s) => s.id === 'funding').regime).toMatchObject({ multiplier: 2 });
    expect(base.sources.find((s) => s.id === 'etfflows').weight).toBe(10);
  });
  it('rejects invalid adjustments', () => {
    expect(normalizeAdjustment({ type: 'ADD_SOURCE', label: 'etfflows', weight: 5, confidence: 0.4 }).ok).toBe(false);
    expect(normalizeAdjustment({ type: 'CHANGE_WEIGHT', source_id: 'nope', weight: 5, confidence: 0.5 }).ok).toBe(false);
    expect(normalizeAdjustment({ type: 'CHANGE_WEIGHT', source_id: 'fng', weight: 500, confidence: 0.5 }).ok).toBe(false);
    expect(normalizeAdjustment({ type: 'DROP' }).ok).toBe(false);
  });
  it('invert, regime multiplier and derived signals score deterministically', () => {
    const inv = applyAdjustment(base, { type: 'CHANGE_CLASSIFICATION', source_id: 'etfflows', group: 'FLOWS', invert: true });
    expect(scoreWithConfig(inv, { readings }).exact).toBeLessThan(50);
    const reg = applyAdjustment(base, { type: 'ADD_REGIME_CONDITION', source_id: 'etfflows', metric: 'btc_24h_change_pct', op: '<=', value: -2, multiplier: 3 });
    expect(scoreWithConfig(reg, { readings, btc24hPct: -3 }).exact).toBeGreaterThan(scoreWithConfig(reg, { readings, btc24hPct: 1 }).exact);
    const sig = applyAdjustment(base, { type: 'ADD_SIGNAL', signal_id: 'etf_mom', label: 'x', derived_from: 'etfflows', transform: 'momentum_24h', group: 'FLOWS', weight: 10, confidence: 1 });
    expect(scoreWithConfig(sig, { readings, prevReadings: { etfflows: 60 } }).exact).toBeGreaterThan(scoreWithConfig(sig, { readings, prevReadings: { etfflows: 95 } }).exact);
    expect(scoreWithConfig(sig, { readings }).used).toBe(21); // no previous reading -> signal absent, not invented
  });
  it('a new source without history is DATA COLLECTION REQUIRED; nothing is fabricated', () => {
    const obs = [{ ts: 1, stored: 50, readings: ALL_NEUTRAL }];
    const adj = normalizeAdjustment({ type: 'ADD_SOURCE', label: 'Deribit options OI', group: 'DERIVATIVES', weight: 5, confidence: 0.4 }).adjustment;
    expect(dataAvailability(adj, obs)).toMatchObject({ status: 'NO_HISTORY', observations_with_data: 0 });
    const r = recalculate(base, adj, buildContexts(obs, []));
    expect(r.possible).toBe(false);
    expect(r.availability.message).toBe('New source discovered. Historical source data is not yet available for recalculation.');
    expect(r.event).toBeNull();
    expect(validateRecalculation(r, []).status).toBe('NOT_ENOUGH_DATA');
    expect(dataAvailability({ type: 'ADD_SIGNAL', signal_id: 's', derived_from: '' }, obs).status).toBe('NO_HISTORY');
  });
  it('keeps STORED, RECONSTRUCTED and PROPOSED apart', () => {
    const obs = [{ ts: 0, stored: 58, readings }];
    const r = recalculate(base, { type: 'CHANGE_WEIGHT', source_id: 'etfflows', weight: 0, confidence: 0.8 }, buildContexts(obs, []), { eventTs: 0 });
    expect(r.event.stored).toBe(58);
    expect(r.event.reconstructed).toBe(v1Composite(readings).score);
    expect(r.event.proposed).toBe(50);
    expect(r.event.delta).toBe(r.event.proposed - r.event.reconstructed);
    expect(r.reconstruction).toMatchObject({ observations: 1, exact_matches: 0 });
  });
  it('maps findings to candidate types and adjustments', () => {
    expect(candidateFromFinding(parseAiResearchResponse(GOOD_ANSWER).finding)).toMatchObject({ candidate_type: 'NEW_SOURCE', adjustment: { type: 'ADD_SOURCE', group: 'DERIVATIVES', weight: 5, confidence: 0.4 } });
    expect(candidateFromFinding({ finding_type: 'EXISTING_SOURCE_MISREAD', covered_by_existing_v1_source: 'etfflows' })).toMatchObject({ candidate_type: 'SOURCE_WEIGHT_ADJUSTMENT', adjustment: { type: 'CHANGE_WEIGHT', source_id: 'etfflows' } });
    expect(candidateFromFinding({ finding_type: 'NEW_SIGNAL', covered_by_existing_v1_source: 'funding', proposed_signal: 'funding momentum' }).adjustment).toMatchObject({ type: 'ADD_SIGNAL', derived_from: 'funding' });
  });
  it('versions increment from the highest existing v1.N', () => {
    expect(nextVersionId(['v1.0'])).toBe('v1.1');
    expect(nextVersionId(['v1.0', 'v1.3', 'v1.1'])).toBe('v1.4');
  });
});

describe('validation (EXP-005 outcome rule)', () => {
  const D = 24 * H;
  // One observation per day for 20 days; BTC alternates up/down each day. The baseline always says UP (score 60).
  // The "proposed" side says DOWN on exactly the days BTC fell -> it fixes every day the baseline got wrong.
  function scenario(flipGood, days = 20) {
    const btc = [], pts = [];
    let price = 100000;
    for (let d = 0; d <= days; d++) { btc.push({ ts: d * D, btc_price: price }); price *= d % 2 ? 1.02 : 0.98; }
    for (let d = 0; d < days; d++) {
      const fell = d % 2 === 0;
      const proposedDown = flipGood ? fell : !fell;
      pts.push({ ts: d * D, stored: 60, reconstructed: 60, proposed: proposedDown ? 40 : 60 });
    }
    return { btc, recalc: { possible: true, all_points: pts } };
  }
  it('SUPPORTED when the adjustment fixes the days the baseline got wrong', () => {
    const { btc, recalc } = scenario(true);
    const v = validateRecalculation(recalc, btc);
    expect(v.status).toBe('SUPPORTED');
    expect(v.adjusted_v1.accuracy_pct).toBeGreaterThan(v.current_v1.accuracy_pct);
    expect(v.independent.improved).toBe(10);
  });
  it('NOT_SUPPORTED when it breaks the days the baseline got right', () => {
    const { btc, recalc } = scenario(false);
    expect(validateRecalculation(recalc, btc).status).toBe('NOT_SUPPORTED');
  });
  it('VALIDATING with too few independent disagreements, INCONCLUSIVE when calls never change', () => {
    const { btc, recalc } = scenario(true, 6);
    expect(validateRecalculation(recalc, btc).status).toBe('VALIDATING');
    const same = { possible: true, all_points: recalc.all_points.map((p) => ({ ...p, proposed: 61 })) };
    expect(validateRecalculation(same, btc).status).toBe('INCONCLUSIVE');
  });
  it('never validates on the originating event window', () => {
    const { btc, recalc } = scenario(true);
    const all = validateRecalculation(recalc, btc).resolved;
    expect(validateRecalculation(recalc, btc, { eventTs: 5 * D }).resolved).toBe(all - 3);
  });
});

describe('Full journey: finding -> candidate -> adjustment -> recalculation -> validation -> approval -> version', () => {
  async function confirmFinding(ctx) {
    const finding = parseAiResearchResponse(GOOD_ANSWER).finding;
    return call(ctx.env, '/api/learning/findings', { method: 'POST', body: { event_id: 15, provider: 'chatgpt', finding }, token: ctx.token });
  }
  it('runs end to end without touching V1 data', async () => {
    const ctx = makeEnv();
    const before = counts(ctx.db);
    expect((await confirmFinding(ctx)).status).toBe(200);
    // Slice 2: candidate from the confirmed finding (token required, idempotent)
    expect((await call(ctx.env, '/api/learning/candidates', { method: 'POST', body: { event_id: 15 } })).status).toBe(401);
    const created = await call(ctx.env, '/api/learning/candidates', { method: 'POST', body: { event_id: 15 }, token: ctx.token });
    expect(created.json).toMatchObject({ ok: true, created: true });
    const id = created.json.candidate_id;
    expect((await call(ctx.env, '/api/learning/candidates', { method: 'POST', body: { event_id: 15 }, token: ctx.token })).json).toMatchObject({ candidate_id: id, created: false });
    let view = (await call(ctx.env, `/api/learning/candidate?id=${id}`)).json;
    expect(view.candidate).toMatchObject({ status: 'DRAFT', candidate_type: 'NEW_SOURCE', base_version_id: 'v1.0', title: 'Quarterly options expiry' });
    expect(view.candidate.evidence.length).toBe(2);
    // Slice 3 (case B): brand-new source, no history -> data collection required, nothing invented
    expect(view.recalculation.possible).toBe(false);
    expect(view.recalculation.availability.message).toMatch(/Historical source data is not yet available/);
    expect(view.validation.status).toBe('NOT_ENOUGH_DATA');
    let market = (await call(ctx.env, '/api/learning/market')).json;
    expect(market.events.find((e) => e.event_id === 15).journey.find((s) => s.key === 'IMPACT')).toMatchObject({ state: 'WARN', text: 'Data collection required' });
    // Slice 3 (case A): switch to an adjustment of existing sources -> deterministic recalculation
    const upd = await call(ctx.env, '/api/learning/candidate/update', { method: 'POST', token: ctx.token, body: { candidate_id: id, submit: true, fields: { candidate_type: 'SOURCE_WEIGHT_ADJUSTMENT' }, adjustment: { type: 'CHANGE_WEIGHT', source_id: 'etfflows', weight: 0, confidence: 0.8 } } });
    expect(upd.json).toMatchObject({ ok: true, candidate_status: 'PENDING_REVIEW' });
    view = (await call(ctx.env, `/api/learning/candidate?id=${id}`)).json;
    expect(view.recalculation.possible).toBe(true);
    expect(view.recalculation.event).toMatchObject({ stored: 66, reconstructed: 70, proposed: 70, delta: 0 });
    expect(view.recalculation.observations).toBeGreaterThan(90);
    expect(view.recalculation.reconstruction.exact_matches).toBe(0);
    expect(['VALIDATING', 'INCONCLUSIVE', 'NOT_ENOUGH_DATA']).toContain(view.validation.status);
    // Slice 5: decision needs a name and, without supporting validation, an explicit acknowledgement
    expect((await call(ctx.env, '/api/learning/candidate/decide', { method: 'POST', token: ctx.token, body: { candidate_id: id, decision: 'APPROVE' } })).status).toBe(400);
    const noAck = await call(ctx.env, '/api/learning/candidate/decide', { method: 'POST', token: ctx.token, body: { candidate_id: id, decision: 'APPROVE', approver: 'Olivier' } });
    expect(noAck.status).toBe(409);
    const ok = await call(ctx.env, '/api/learning/candidate/decide', { method: 'POST', token: ctx.token, body: { candidate_id: id, decision: 'APPROVE', approver: 'Olivier', note: 'test', acknowledge_unsupported: true } });
    expect(ok.json).toMatchObject({ ok: true, candidate_status: 'ACCEPTED', version_id: 'v1.1' });
    // Slice 6: version v1.1 is APPROVED (ready), not active; baseline persisted once
    const versions = ctx.db.prepare('SELECT * FROM v1_methodology_versions ORDER BY version_id').all();
    expect(versions.map((v) => [v.version_id, v.status, v.effective_ts])).toEqual([['v1.0', 'BASELINE', null], ['v1.1', 'APPROVED', null]]);
    const v11 = versions[1];
    expect(v11).toMatchObject({ parent_version_id: 'v1.0', candidate_id: id, approved_by: 'Olivier', formula_id: 'v1-weighted-mean@1' });
    expect(JSON.parse(v11.config_json).sources.find((s) => s.id === 'etfflows').weight).toBe(0);
    expect(JSON.parse(v11.validation_json).approved_without_support).toBe(true);
    expect((await call(ctx.env, '/api/learning/versions')).json.versions.map((v) => v.version_id)).toEqual(['v1.0', 'v1.1']);
    view = (await call(ctx.env, `/api/learning/candidate?id=${id}`)).json;
    expect(view.produced_version).toMatchObject({ version_id: 'v1.1', status: 'APPROVED' });
    market = (await call(ctx.env, '/api/learning/market')).json;
    expect(market.events.find((e) => e.event_id === 15).journey.find((s) => s.key === 'APPROVAL')).toMatchObject({ state: 'DONE', text: 'Approved: V1 v1.1 ready (not active)' });
    // Terminal: no further edits or decisions
    expect((await call(ctx.env, '/api/learning/candidate/decide', { method: 'POST', token: ctx.token, body: { candidate_id: id, decision: 'REJECT', approver: 'x' } })).status).toBe(409);
    expect((await call(ctx.env, '/api/learning/candidate/update', { method: 'POST', token: ctx.token, body: { candidate_id: id, fields: { title: 'x' } } })).status).toBe(409);
    // V1 data untouched
    const after = counts(ctx.db);
    for (const t of ['history', 'btc_data', 'research_events', 'research_event_evidence', 'research_sentiment_archive', 'stage7_event_sentiment']) expect(after[t]).toBe(before[t]);
    expect(after.learning_candidates).toBe(1);
    expect(after.v1_methodology_versions).toBe(2);
  });

  it('reject and investigate-more paths; a sent-back candidate can be edited and resubmitted', async () => {
    const ctx = makeEnv();
    await confirmFinding(ctx);
    const id = (await call(ctx.env, '/api/learning/candidates', { method: 'POST', body: { event_id: 15 }, token: ctx.token })).json.candidate_id;
    expect((await call(ctx.env, '/api/learning/candidate/decide', { method: 'POST', token: ctx.token, body: { candidate_id: id, decision: 'REJECT', approver: 'O' } })).status).toBe(409); // still DRAFT
    await call(ctx.env, '/api/learning/candidate/update', { method: 'POST', token: ctx.token, body: { candidate_id: id, submit: true } });
    expect((await call(ctx.env, '/api/learning/candidate/decide', { method: 'POST', token: ctx.token, body: { candidate_id: id, decision: 'NEEDS_MORE_RESEARCH', approver: 'O' } })).json.candidate_status).toBe('NEEDS_MORE_RESEARCH');
    expect((await call(ctx.env, '/api/learning/candidate/update', { method: 'POST', token: ctx.token, body: { candidate_id: id, submit: true } })).json.candidate_status).toBe('PENDING_REVIEW');
    expect((await call(ctx.env, '/api/learning/candidate/decide', { method: 'POST', token: ctx.token, body: { candidate_id: id, decision: 'REJECT', approver: 'O' } })).json.candidate_status).toBe('REJECTED');
    expect(ctx.db.prepare('SELECT COUNT(*) AS n FROM v1_methodology_versions').get().n).toBe(0);
  });

  it('a candidate needs a human-confirmed finding; without migration 0021 learning writes are refused', async () => {
    const ctx = makeEnv();
    expect((await call(ctx.env, '/api/learning/candidates', { method: 'POST', body: { event_id: 15 }, token: ctx.token })).status).toBe(409);
    const bare = makeEnv({ withLearning: false });
    await confirmFinding(bare);
    expect((await call(bare.env, '/api/learning/candidates', { method: 'POST', body: { event_id: 15 }, token: bare.token })).status).toBe(503);
    expect((await call(bare.env, '/api/learning/market')).json.ok).toBe(true);
    expect((await call(bare.env, '/api/learning/versions')).json.versions[0].version_id).toBe('v1.0');
  });
});

describe('staging deploy job guard (.github/workflows/test.yml)', () => {
  const wf = readFileSync(join(__dirname, '..', '.github', 'workflows', 'test.yml'), 'utf8');
  const job = wf.slice(wf.indexOf('  staging-deploy-learning:'));
  it('runs only on an explicit manual dispatch and targets only the staging Worker', () => {
    expect(job).toContain("if: github.event_name == 'workflow_dispatch' && inputs.job == 'staging-deploy-learning'");
    expect(job.match(/wrangler@4 (deploy|secret put STAGE7_ADMIN_TOKEN) -c wrangler\.staging\.toml/g)).toHaveLength(2);
    expect(job).not.toMatch(/-c wrangler\.toml|--config wrangler\.toml/);
    expect(job).not.toMatch(/secrets\.CLOUDFLARE_API_TOKEN/);
    expect([...job.matchAll(/secrets\.([A-Z0-9_]+)/g)].map((m) => m[1]).every((s) => s.startsWith('STAGE7_STAGING_'))).toBe(true);
    expect(job).not.toMatch(/d1 (execute|migrations)/);
  });
  it('wrangler.staging.toml binds only the staging database and defines no cron', () => {
    const toml = readFileSync(join(__dirname, '..', 'wrangler.staging.toml'), 'utf8').split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');
    expect(toml).toMatch(/^name = "pulseworker-v2-staging"$/m);
    expect(toml).toMatch(/^database_id = "5458d504-2778-49ae-bd25-7751f1c49d50"$/m);
    expect(toml).not.toMatch(/f91ca980-b886-423a-bd6f-f3baea46d181|sentiment-history|\[triggers\]/);
  });
});
