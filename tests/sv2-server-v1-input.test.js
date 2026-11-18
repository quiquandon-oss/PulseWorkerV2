// V1 hourly server readings (sentiment-ff75 scomp-v1, server_composite_readings) as a second V2 input
// (SV2_V1_SERVER_INPUT, owner decision 2026-11-16). Real SQLite (node:sqlite) with the production DDL of every table.
// Checks: flag off = archive only; flag on = server readings scored at their computed_ts and labelled; app readings
// scored exactly as without the flag (context = archive only); a server reading's context = archive + itself (so the
// observation-count rules keep the archive density); below-floor / no-composite skipped; archive wins an exact tie;
// nothing written to V1 or research tables; missing server table never fails the cycle; comparison points labelled.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';
import { makeD1 } from './replay/d1-sqlite.mjs';
import { SV2_SOURCE_REGISTRY } from '../sentiment-v2/collector.js';
import { sv2EvidenceReadings, sv2ScoreHeadline, SV2_EVIDENCE_RULES } from '../sentiment-v2/evidence-score.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const WORKER = readFileSync(join(ROOT, 'worker.js'), 'utf8');
const ARCHIVE_DDL = readFileSync(join(ROOT, '.ai/migrations/0015_experiment5_sentiment_archive.sql'), 'utf8');
const V2_DDL = readFileSync(join(ROOT, '.ai/migrations/0027_sentiment_v2_results.sql'), 'utf8') + '\n' +
  readFileSync(join(ROOT, '.ai/migrations/0029_sv2_evidence_and_ai_budget.sql'), 'utf8');
const OTHER_DDL = `CREATE TABLE event_ledger (event_id TEXT, dedup_key TEXT, correlation_key TEXT, event_type TEXT, asset TEXT,
  source TEXT, source_ts INTEGER, detected_ts INTEGER, direction TEXT, severity TEXT, confidence REAL, rule_id TEXT, expires_ts INTEGER);
  CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL);
  CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL, sources_json TEXT);`;
// PulseWorker db/migrations/0002_server_composite.sql (V1 repository), the table V2 reads; append-only triggers included.
const SERVER_DDL = `CREATE TABLE server_composite_readings (slot_ts INTEGER PRIMARY KEY, computed_ts INTEGER NOT NULL, version TEXT NOT NULL,
  weights_version TEXT NOT NULL, composite INTEGER, composite_exact REAL, completeness REAL NOT NULL,
  below_floor INTEGER NOT NULL CHECK (below_floor IN (0, 1)), n_ok INTEGER NOT NULL, n_missing INTEGER NOT NULL, provenance_json TEXT NOT NULL);
  CREATE TRIGGER server_composite_readings_no_update BEFORE UPDATE ON server_composite_readings
  BEGIN SELECT RAISE(ABORT, 'server_composite_readings is append-only'); END;`;

const V2 = ['sv2Config', 'sv2Finite', 'sv2ParseSources', 'sv2History', 'sv2ServerInputEnabled', 'sv2ServerV1Rows', 'sv2MergeV1Inputs', 'sv2ServerDecisionHist',
  'sv2AttachPrices', 'sv2IndexAtOrBefore', 'sv2PriceAt', 'sv2SourceStatus', 'sv2Pearson', 'sv2FamilyOf', 'sv2Redundancy', 'sv2Families',
  'sv2EventFamily', 'sv2EventReactions', 'sv2Events', 'sv2Regime', 'sv2Profile', 'sv2Analogues', 'sv2Classify', 'sv2Compute', 'sv2AiPackage',
  'sv2ValidateAiOutput', 'sv2RunAi', 'sv2HexSha256', 'sv2Row', 'runSentimentV2Cycle', 'sv2CycleQueriesPerReading', 'sv2SelectReadings', 'sv2AlignSeries', 'sv2SeriesStatus',
  'getSentimentV2Comparison', 'sv2EvidenceSummary', 'sv2EvidenceItemsForAi', 'sv2AiBudget', 'sv2AiEstimateNeurons', 'sv2UngroundedNumbers',
  'sv2StructuredAnnotation', 'sv2LatestView'];
const CONSTS = ['SENTIMENT_V2_VERSION', 'SV2_VERSIONS_SHOWN', 'SV2_INSERT_SQL', 'SV2_PERIODS_MS', 'SV2_MAX_POINTS', 'SV2_MAX_ROWS',
  'SV2_MAX_PRICE_ROWS', 'SV2_STALE_MS', 'SV2_SERVER_V1_COLLECTOR', 'SV2_SERVER_V1_MAX_ROWS', 'SV2_SERVER_V1_PER_CYCLE', 'SV2_SERVER_V1_SOURCES_SQL', 'SV2_AI_PRICING',
  'SV2_AI_MAX_OUTPUT_TOKENS', 'SV2_AI_RESERVE_SQL', 'SV2_AI_TEMPLATE_TOKENS', 'SV2_AI_PROMPT_VERSION', 'SV2_CYCLE_QUERIES_FIXED',
  'SV2_CYCLE_QUERIES_PER_READING', 'SV2_CYCLE_QUERIES_PER_READING_AI_OFF', 'SV2_CYCLE_MAX_READINGS_WITH_SERVER', 'SV2_SKIP_AUDIT_ROWS'];
const S = evalInScope(extractConstants(...CONSTS) + '\n' + extractFunctions(...V2),
  { SV2_SOURCE_REGISTRY, sv2EvidenceReadings, sv2ScoreHeadline, SV2_EVIDENCE_RULES });

const H = 3600000;
const C = 20000;                       // computed_ts = slot_ts + 20 s in these fixtures (the decision time)
const NOW = Date.UTC(2026, 10, 16, 20, 0, 0);
const KEYS = ['fng', 'cryptonews', 'foufi', 'funding', 'longshort', 'hypefunding', 'hloi', 'onchain', 'macrogeo', 'usd', 'oil',
  'gold', 'ninemag', 'regulatory', 'geopolitics', 'strc'];
const srcObj = (i) => Object.fromEntries(KEYS.map((k, j) => [k, Math.round((50 + 20 * Math.sin(i / (4 + j) + j)) * 100) / 100]));

function setup({ archive = [], server = [], withServerTable = true } = {}) {
  const d1 = makeD1([ARCHIVE_DDL, V2_DDL, OTHER_DDL, ...(withServerTable ? [SERVER_DDL] : [])]);
  for (const r of archive) {
    d1.db.prepare('INSERT INTO research_sentiment_archive (observation_ts, sources_json, score, source_weights_version, schema_version, written_by, content_hash, archived_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)')
      .run(r.ts, JSON.stringify(srcObj(r.i)), r.score ?? 40, 'v1-unversioned', 'exp5-v1', 'experiment5-pipeline', 'x', 1);
  }
  for (const r of server) {
    const sources = Object.entries(srcObj(r.i)).map(([id, score]) => ({ id, score, base_weight: 5, confidence: 1, boost: 1, observed_at: r.ts }));
    const prov = { formula_version: 'v1-composite-1', sources, missing: [{ id: 'nasdaq', base_weight: 5, reason: 'unavailable' }], collector: 'scomp-v1' };
    d1.db.prepare('INSERT INTO server_composite_readings (slot_ts, computed_ts, version, weights_version, composite, composite_exact, completeness, below_floor, n_ok, n_missing, provenance_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
      .run(r.ts, r.ts + 20000, 'scomp-v1', 'w1', r.composite === undefined ? 38 : r.composite, r.composite === undefined ? 38.2 : r.composite,
        r.below ? 0.4 : 0.75, r.below ? 1 : 0, sources.length, 1, JSON.stringify(prov));
  }
  for (let k = 0; k < 900; k++) d1.db.prepare('INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)').run(NOW - 900 * H + k * H, 70000 + 10 * k);
  return d1;
}
const env = (d1, on = true) => ({ DB: d1.binding, SENTIMENT_V2_ENABLED: '1', ...(on ? { SV2_V1_SERVER_INPUT: '1' } : {}) });
const results = (d1) => d1.db.prepare('SELECT decision_ts, family_json FROM sentiment_v2_results ORDER BY decision_ts').all()
  .map(r => ({ ts: r.decision_ts, input: JSON.parse(r.family_json).v1_input }));
// Browser readings every 6 h for 20 days (context), server readings every hour for the last 3 days.
const ctxArchive = () => Array.from({ length: 80 }, (_, i) => ({ ts: NOW - 20 * 24 * H + i * 6 * H + 17 * 60000, i }));
const ctxServer = () => Array.from({ length: 72 }, (_, i) => ({ ts: NOW - 72 * H + i * H, i: 100 + i }));

describe('V2 cycle with V1 server readings', () => {
  it('flag off: only archive observations are candidates (unchanged behaviour)', async () => {
    const d1 = setup({ archive: ctxArchive(), server: ctxServer() });
    const r = await S.runSentimentV2Cycle(env(d1, false), { nowTs: NOW });
    expect(r.written).toBe(1);
    expect(results(d1)[0]).toEqual({ ts: ctxArchive().at(-1).ts, input: { origin: 'browser', table: 'research_sentiment_archive' } });
  });

  it('flag on: the newest hourly server slot is scored and labelled server; one server reading per run (CPU bound)', async () => {
    const d1 = setup({ archive: ctxArchive(), server: ctxServer() });
    const first = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 3 * H });
    expect(first.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[NOW - 4 * H + C, 'server']]);   // NOW-3h computed after nowTs
    const next = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });     // NOW-3h, NOW-2h left unscored, never back-filled
    expect(next.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[NOW - H + C, 'server']]);
    expect(S.SV2_SERVER_V1_PER_CYCLE).toBe(1);
    const rows = results(d1);
    expect(rows.every(x => x.input.origin === 'server' && x.input.collector === 'scomp-v1' && x.input.table === 'server_composite_readings')).toBe(true);
    expect(rows.map(x => x.input.slot_ts)).toEqual([NOW - 4 * H, NOW - H]);
  });

  it('browser and server candidates are merged, oldest first, each with its own origin; 2 per run, the newer one follows', async () => {
    const t1 = NOW - 150 * 60000, t2 = NOW - 30 * 60000;          // browser readings around the newest slot
    const d1 = setup({ archive: [...ctxArchive(), { ts: t1, i: 998 }, { ts: t2, i: 999 }], server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 4 * H });
    const r = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    expect(S.SV2_CYCLE_MAX_READINGS_WITH_SERVER).toBe(2);
    expect(r.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[t1, 'browser'], [NOW - H + C, 'server']]);
    expect(r.deferred_readings).toEqual([t2]);                    // newer than the last decision: still pending, not lost
    // Older hourly slots passed by the cursor: one server reading per cycle (unchanged rule), now recorded, not silent.
    expect(r.skipped_readings).toEqual([NOW - 4 * H + C, NOW - 3 * H + C, NOW - 2 * H + C]
      .map(ts => ({ ts, origin: 'server', reason: 'one_server_per_cycle' })));
    const next = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW + 60000 });
    expect(next.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[t2, 'browser']]);
  });

  it('skips below-floor and composite-less slots; an archive row at the same timestamp wins', async () => {
    const server = [...ctxServer().slice(0, 69), { ts: NOW - 3 * H, i: 1, below: true }, { ts: NOW - 2 * H, i: 2, composite: null },
      { ts: NOW - H, i: 3 }];
    const d1 = setup({ archive: [...ctxArchive(), { ts: NOW - H + C, i: 4, score: 55 }], server });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 4 * H });
    const r = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    expect(r.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[NOW - H + C, 'browser']]);
  });

  it('an app reading is scored exactly as without the flag (server readings never enter its context)', async () => {
    const t = NOW - 30 * 60000;
    const cols = 'decision_ts, status, degraded_reason, sentiment_score, sentiment_class, confidence, evidence_coverage, market_regime, regime_json, event_json, predictive_json, missing_json';
    const run = async (on) => {
      const d1 = setup({ archive: [...ctxArchive(), { ts: t, i: 999 }], server: on ? ctxServer() : [] });
      await S.runSentimentV2Cycle(env(d1, on), { nowTs: NOW });
      return d1.db.prepare(`SELECT ${cols}, family_json FROM sentiment_v2_results WHERE decision_ts = ?`).get(t);
    };
    const off = await run(false), on = await run(true);
    const fam = (r) => { const f = JSON.parse(r.family_json); delete f.v1_input; return f; };
    expect({ ...on, family_json: fam(on) }).toEqual({ ...off, family_json: fam(off) });
    expect(JSON.parse(on.family_json).v1_input.origin).toBe('browser');
  });

  it('a server reading is scored on the archive density: a daily source unchanged across hourly readings is not stale', async () => {
    // fng is constant across all 72 hourly server readings but changes in every archive observation.
    const server = ctxServer().map((r) => ({ ...r }));
    const d1 = setup({ archive: ctxArchive(), server });
    d1.db.exec("DROP TRIGGER server_composite_readings_no_update");
    const rows = d1.db.prepare('SELECT slot_ts, provenance_json FROM server_composite_readings').all();
    for (const r of rows) {
      const p = JSON.parse(r.provenance_json);
      p.sources = p.sources.map((x) => (x.id === 'fng' ? { ...x, score: 12 } : x));
      d1.db.prepare('UPDATE server_composite_readings SET provenance_json = ? WHERE slot_ts = ?').run(JSON.stringify(p), r.slot_ts);
    }
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    const res = d1.db.prepare('SELECT missing_json, family_json FROM sentiment_v2_results').get();
    expect(JSON.parse(res.missing_json).stale || []).not.toContain('fng');
    expect(JSON.parse(res.family_json).families.market_sentiment_news.sources_fresh).toContain('fng');
    const dh = S.sv2ServerDecisionHist(ctxArchive().map(a => ({ observation_ts: a.ts, score: 40, sources_json: '{}' })),
      { observation_ts: NOW, score: 38, sources_json: '{"fng":12}', v1_origin: 'server' }, [], 0);
    expect(dh.length).toBe(81);
    expect(dh.at(-1)).toMatchObject({ ts: NOW, origin: 'server' });
  });

  it('writes only sentiment_v2_results: archive, history and server tables are unchanged', async () => {
    const d1 = setup({ archive: ctxArchive(), server: ctxServer() });
    const count = (t) => d1.db.prepare(`SELECT COUNT(*) AS n FROM ${t}`).get().n;
    const before = ['research_sentiment_archive', 'history', 'server_composite_readings'].map(count);
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 5 * H });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    expect(['research_sentiment_archive', 'history', 'server_composite_readings'].map(count)).toEqual(before);
    expect(count('sentiment_v2_results')).toBeGreaterThan(1);
  });

  it('server table missing (staging, V1 migration not applied): archive-only cycle, error reported, nothing fails', async () => {
    const d1 = setup({ archive: ctxArchive(), withServerTable: false });
    const r = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    expect(r.ok).toBe(true);
    expect(r.written).toBe(1);
    expect(r.v1_server_error).toMatch(/no such table/);
    expect(results(d1)[0].input.origin).toBe('browser');
  });

  it('server per-source readings reach V2 in the archive shape (SQL json_group_object over provenance sources)', async () => {
    const d1 = setup({ server: ctxServer() });
    const { rows, error } = await S.sv2ServerV1Rows({ DB: d1.binding }, NOW - 2 * H, NOW);
    expect(error).toBeNull();
    expect(rows.map(r => [r.observation_ts, r.slot_ts])).toEqual([[NOW - 2 * H + C, NOW - 2 * H], [NOW - H + C, NOW - H]]);
    expect(JSON.parse(rows[1].sources_json)).toEqual(srcObj(100 + 71));
    expect(rows[1]).toMatchObject({ score: 38, btc_price: null, v1_origin: 'server' });
    expect(S.sv2History(rows)[1]).toMatchObject({ origin: 'server', v1: 38, src: srcObj(171) });
    expect((await S.sv2ServerV1Rows({ DB: d1.binding }, NOW - H + C, NOW)).rows).toEqual([]);   // (lo, hi] on computed_ts
  });

  it('stays within the declared D1 query budget with the server reads included', async () => {
    const d1 = setup({ archive: [...ctxArchive(), { ts: NOW - 170 * 60000, i: 997 }, { ts: NOW - 20 * 60000, i: 996 }], server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 4 * H });
    let q = 0;
    const b = d1.binding;
    const counted = { ...b, prepare: (sql) => { const st = b.prepare(sql); const w = (o) => ({ ...o, bind: (...a) => w(o.bind(...a)),
      all: (...x) => { q++; return o.all(...x); }, first: (...x) => { q++; return o.first(...x); }, run: (...x) => { q++; return o.run(...x); } }); return w(st); } };
    const budget = S.SV2_CYCLE_QUERIES_FIXED + 3 * S.SV2_CYCLE_QUERIES_PER_READING;
    const r = await S.runSentimentV2Cycle({ DB: counted, SENTIMENT_V2_ENABLED: '1', SV2_V1_SERVER_INPUT: '1' }, { nowTs: NOW, queryBudget: budget });
    expect(r.written).toBe(2);                                     // capped at 2 while the server input is on
    expect(r.decisions.map(x => x.v1_origin)).toContain('server');
    expect(q).toBeLessThanOrEqual(budget);
  });
});

describe('comparison API with V1 server readings', () => {
  it('flag on: server readings appear on the V1 line with v1_origin server, V2 joined on them', async () => {
    const d1 = setup({ archive: ctxArchive(), server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - H });
    const c = await S.getSentimentV2Comparison(env(d1), '24h', NOW);
    const server = c.points.filter(p => p.v1_origin === 'server');
    expect(server.length).toBe(24);
    expect(c.points.find(p => p.ts === NOW - 2 * H + C)).toMatchObject({ v1: 38, v1_origin: 'server', v2_status: expect.any(String) });
    expect(c.points.find(p => p.ts === NOW - 2 * H + C).btc).toBeGreaterThan(0);          // price attached from btc_data
    expect(c.v1_server_input).toEqual({ enabled: true, collector: 'scomp-v1', readings_in_period: 24, error: null });
    expect(c.series_status.v1.last_ts).toBe(NOW - H + C);
  });

  it('flag off: no server points, same payload as before plus the disabled marker', async () => {
    const d1 = setup({ archive: ctxArchive(), server: ctxServer() });
    const c = await S.getSentimentV2Comparison(env(d1, false), '7d', NOW);
    expect(c.points.some(p => p.v1_origin === 'server')).toBe(false);
    expect(c.v1_server_input.enabled).toBe(false);
  });

  it('production enables the input; staging leaves it unset (no server table there)', () => {
    const prod = readFileSync(join(ROOT, 'wrangler.toml'), 'utf8');
    const stag = readFileSync(join(ROOT, 'wrangler.staging.toml'), 'utf8');
    expect(prod).toMatch(/^SV2_V1_SERVER_INPUT = "1"$/m);
    expect(stag).not.toMatch(/^SV2_V1_SERVER_INPUT/m);
    expect(WORKER).not.toMatch(/INSERT[^;]*research_sentiment_archive[^;]*server/i);
  });
});

// Server-reading crowd-out fix (owner decision 2026-11-18). Production 2026-11-16 and 2026-11-18: with GLM off a reading
// really costs 1 D1 call (its insert), but the cycle budgeted 5, so the ~21-25 calls left after detection and the archive
// sync fitted one reading: an app reading waiting first meant the hourly server reading was not scored in that run.
describe('V2 cycle budget: real per-reading cost, server reading keeps a slot', () => {
  const countingDb = (d1) => {
    const c = { q: 0 };
    const b = d1.binding;
    c.DB = { ...b, prepare: (sql) => { const st = b.prepare(sql); const w = (o) => ({ ...o, bind: (...a) => w(o.bind(...a)),
      all: (...x) => { c.q++; return o.all(...x); }, first: (...x) => { c.q++; return o.first(...x); }, run: (...x) => { c.q++; return o.run(...x); } }); return w(st); } };
    return c;
  };
  const PROD_LEFT = 22;                                    // measured headroom left for V2 in a non-tick run (21-25)

  it('per-reading cost mirrors sv2RunAi: 1 call with no model or a "0" budget, the full 5 otherwise', async () => {
    expect(S.sv2CycleQueriesPerReading({})).toBe(1);
    expect(S.sv2CycleQueriesPerReading({ SENTIMENT_V2_AI_MODEL: '', SV2_GLM_DAILY_NEURONS: '0' })).toBe(1);        // production
    expect(S.sv2CycleQueriesPerReading({ SENTIMENT_V2_AI_MODEL: '@cf/zai-org/glm-4.7-flash', SV2_GLM_DAILY_NEURONS: '0' })).toBe(1);
    expect(S.sv2CycleQueriesPerReading({ SENTIMENT_V2_AI_MODEL: '', SV2_GLM_DAILY_NEURONS: '1000' })).toBe(1);
    expect(S.sv2CycleQueriesPerReading({ SENTIMENT_V2_AI_MODEL: '@cf/zai-org/glm-4.7-flash', SV2_GLM_DAILY_NEURONS: '1000' })).toBe(5);
    // Where the cost is 1, sv2RunAi must touch no ledger at all (any ledger call throws here).
    const boom = () => { throw new Error('ledger touched'); };
    const ledger = { reserveNeurons: boom, recordNeurons: boom, accountLimitedToday: boom };
    for (const e of [{}, { SENTIMENT_V2_AI_MODEL: '@cf/zai-org/glm-4.7-flash', SV2_GLM_DAILY_NEURONS: '0' }, { SV2_GLM_DAILY_NEURONS: '1000' }]) {
      expect(S.sv2CycleQueriesPerReading(e)).toBe(1);
      expect((await S.sv2RunAi(e, {}, [], ledger)).status).toBe('disabled');
    }
  });

  it('production case: app reading waiting + hourly server reading, production headroom: both scored in one run', async () => {
    const tApp = NOW - 150 * 60000;                           // app reading before the newest server slot
    const d1 = setup({ archive: [...ctxArchive(), { ts: tApp, i: 990 }], server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 4 * H });
    const c = countingDb(d1);
    const r = await S.runSentimentV2Cycle({ DB: c.DB, SENTIMENT_V2_ENABLED: '1', SV2_V1_SERVER_INPUT: '1' }, { nowTs: NOW, queryBudget: PROD_LEFT });
    expect(r.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[tApp, 'browser'], [NOW - H + C, 'server']]);
    expect(c.q).toBeLessThanOrEqual(PROD_LEFT);
    // Before the fix this budget fitted floor((22 - 16) / 5) = 1 reading: only the app reading.
    expect(Math.floor((PROD_LEFT - S.SV2_CYCLE_QUERIES_FIXED) / S.SV2_CYCLE_QUERIES_PER_READING)).toBe(1);
  });

  // Production timing: the cron runs exactly on the hour (nowTs = scheduledTime), so the current hour's server reading
  // (computed at hh:00:20 here) is not yet eligible; tick hours (every 3rd) run no V2 cycle.
  const runDay = async (d1, day0, { budget = PROD_LEFT, on = true } = {}) => {
    const out = [];
    for (let h = 1; h <= 24; h++) {
      if (h % 3 === 0) continue;
      const c = countingDb(d1);
      const r = await S.runSentimentV2Cycle({ DB: c.DB, SENTIMENT_V2_ENABLED: '1', ...(on ? { SV2_V1_SERVER_INPUT: '1' } : {}) }, { nowTs: day0 + h * H, queryBudget: budget });
      out.push({ r, q: c.q });
    }
    return out;
  };
  // Lifecycle invariant: every reading eligible by the end is scored, still pending (newer than the last decision) or
  // recorded as skipped with a reason in a written row's usage_json. Nothing disappears; nothing is scored twice.
  const audit = (d1, from, to) => {
    const rows = d1.db.prepare('SELECT decision_ts, usage_json FROM sentiment_v2_results WHERE decision_ts > ? ORDER BY decision_ts').all(from);
    const scored = rows.map(r => r.decision_ts);
    const recorded = new Map();
    for (const r of rows) for (const x of JSON.parse(r.usage_json).skipped_readings || []) {
      expect(recorded.has(x.ts)).toBe(false);                     // each skip recorded once
      recorded.set(x.ts, x);
    }
    const cursor = scored.length ? scored[scored.length - 1] : from;
    const app = d1.db.prepare('SELECT observation_ts AS ts FROM research_sentiment_archive WHERE observation_ts > ? AND observation_ts <= ?').all(from, to).map(r => r.ts);
    const archived = new Set(app);
    const srv = d1.db.prepare('SELECT computed_ts AS ts FROM server_composite_readings WHERE computed_ts > ? AND computed_ts <= ? AND composite IS NOT NULL AND below_floor = 0').all(from, to)
      .map(r => r.ts).filter(t => !archived.has(t));
    const scoredSet = new Set(scored);
    const tally = (list) => {
      const t = { eligible: list.length, scored: 0, pending: 0, skipped: {}, lost: 0 };
      for (const ts of list) {
        if (scoredSet.has(ts)) t.scored++;
        else if (ts > cursor) t.pending++;
        else if (recorded.has(ts)) t.skipped[recorded.get(ts).reason] = (t.skipped[recorded.get(ts).reason] || 0) + 1;
        else t.lost++;
      }
      return t;
    };
    expect(new Set(scored).size).toBe(scored.length);              // no duplicate decision
    for (const ts of recorded.keys()) expect(scoredSet.has(ts)).toBe(false);
    return { app: tally(app), server: tally(srv), complete: rows.every(r => JSON.parse(r.usage_json).skip_list_complete !== false) };
  };

  for (const cad of [30, 15]) it(`a day with app readings every ${cad} min: the server reading is scored in every run, nothing is lost silently`, async () => {
    const day0 = NOW - 24 * H;
    const apps = Array.from({ length: 24 * 60 / cad }, (_, k) => ({ ts: day0 + k * cad * 60000 + 7 * 60000, i: 500 + k }));
    const d1 = setup({ archive: [...ctxArchive(), ...apps], server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: day0 });
    const from = d1.db.prepare('SELECT MAX(decision_ts) AS t FROM sentiment_v2_results').get().t;
    const runs = await runDay(d1, day0);
    expect(runs.length).toBe(16);
    for (const { r, q } of runs) {
      expect(q).toBeLessThanOrEqual(PROD_LEFT);
      expect(r.written).toBeLessThanOrEqual(S.SV2_CYCLE_MAX_READINGS_WITH_SERVER);
      expect(r.decisions.some(x => x.v1_origin === 'server')).toBe(true);
    }
    const a = audit(d1, from, day0 + 24 * H);
    expect(a.complete).toBe(true);
    expect(a.server.lost).toBe(0);
    expect(a.app.lost).toBe(0);
    expect(a.server.scored).toBe(16);
    // Hourly slots computed in a tick hour are passed after it: one server reading per cycle (unchanged rule), recorded.
    expect(a.server.skipped).toEqual({ one_server_per_cycle: a.server.eligible - 16 - a.server.pending });
  });

  it('backlog larger than maxCatchUp, mixed origins, over repeated runs: the cursor always moves forward, bounded work per run', async () => {
    const day0 = NOW - 24 * H;
    const apps = Array.from({ length: 96 }, (_, k) => ({ ts: day0 + k * 15 * 60000 + 3 * 60000, i: 700 + k }));
    const d1 = setup({ archive: [...ctxArchive(), ...apps], server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: day0 });
    const from = d1.db.prepare('SELECT MAX(decision_ts) AS t FROM sentiment_v2_results').get().t;
    let prev = from;
    for (const { r } of await runDay(d1, day0)) {
      const last = Math.max(...r.decisions.filter(x => x.inserted).map(x => x.decision_ts));
      expect(last).toBeGreaterThan(prev);                          // progress every run
      prev = last;
      expect(r.decisions.length + (r.deferred_readings || []).length).toBeLessThanOrEqual(S.SV2_CYCLE_MAX_READINGS_WITH_SERVER + 2);
    }
    const a = audit(d1, from, day0 + 24 * H);
    expect(a.app.eligible).toBeGreaterThan(16 * 3);                  // the backlog exceeds what 16 runs could ever score
    expect(a.app.lost + a.server.lost).toBe(0);
    expect(a.app.skipped.catch_up_window).toBeGreaterThan(0);
  });

  it('server reading delayed by an exhausted budget: scored by a retry before the next slot exists', async () => {
    const d1 = setup({ archive: ctxArchive(), server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 2 * H });
    let q = 0;
    const short = await S.runSentimentV2Cycle({ ...env(d1), DB: { prepare: () => { q++; throw new Error('no query expected'); } } },
      { nowTs: NOW - 2 * H + 30 * 60000, queryBudget: S.SV2_CYCLE_QUERIES_FIXED });
    expect(short).toMatchObject({ skipped: true, deferred_d1_budget: true });
    expect(q).toBe(0);                                               // early exit spends nothing
    const next = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 2 * H + 40 * 60000, queryBudget: PROD_LEFT });
    expect(next.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[NOW - 2 * H + C, 'server']]);
  });

  it('server reading delayed past the next hourly slot (production timing): superseded and recorded, not silent', async () => {
    const d1 = setup({ archive: ctxArchive(), server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 2 * H });
    const short = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - H, queryBudget: S.SV2_CYCLE_QUERIES_FIXED });
    expect(short).toMatchObject({ skipped: true, deferred_d1_budget: true });
    const next = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW, queryBudget: PROD_LEFT });
    expect(next.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[NOW - H + C, 'server']]);
    expect(next.skipped_readings).toEqual([{ ts: NOW - 2 * H + C, origin: 'server', reason: 'one_server_per_cycle' }]);
  });

  it('app reading not chosen because of the cap is scored next run when newer than the scored ones', async () => {
    const t = [NOW - 50 * 60000, NOW - 40 * 60000, NOW - 30 * 60000];   // all newer than the pending server reading
    const d1 = setup({ archive: [...ctxArchive(), ...t.map((ts, k) => ({ ts, i: 880 + k }))], server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 90 * 60000 });
    const r1 = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW, queryBudget: PROD_LEFT });
    expect(r1.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[NOW - H + C, 'server'], [t[0], 'browser']]);
    expect(r1.deferred_readings).toEqual([t[1], t[2]]);
    const r2 = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW + 60000, queryBudget: PROD_LEFT });
    expect(r2.decisions.map(x => x.decision_ts)).toEqual([t[1], t[2]]);
    expect(results(d1).filter(x => t.includes(x.ts)).length).toBe(3);   // each once
  });

  it('a D1 failure mid-run loses nothing: written readings stay, the rest is retried next run', async () => {
    const t = [NOW - 50 * 60000, NOW - 30 * 60000];
    const d1 = setup({ archive: [...ctxArchive(), ...t.map((ts, k) => ({ ts, i: 870 + k }))], server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 90 * 60000 });
    let inserts = 0;
    const b = d1.binding;
    const flaky = { ...b, prepare: (sql) => { const st = b.prepare(sql); if (!/^INSERT OR IGNORE INTO sentiment_v2_results/.test(sql)) return st;
      const w = (o) => ({ ...o, bind: (...a) => w(o.bind(...a)), run: (...x) => { if (++inserts === 2) throw new Error('D1_ERROR: simulated'); return o.run(...x); } }); return w(st); } };
    await expect(S.runSentimentV2Cycle({ DB: flaky, SENTIMENT_V2_ENABLED: '1', SV2_V1_SERVER_INPUT: '1' }, { nowTs: NOW, queryBudget: PROD_LEFT })).rejects.toThrow('simulated');
    expect(results(d1).map(x => x.ts).filter(ts => ts > NOW - 90 * 60000)).toEqual([NOW - H + C]);
    const r = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW + 60000, queryBudget: PROD_LEFT });
    expect(r.decisions.map(x => x.decision_ts)).toEqual(t);
    expect(r.skipped_readings).toBeUndefined();
  });

  it('server-reading scoring is unchanged by the cap: identical to scoring that reading alone', async () => {
    const apps = [{ ts: NOW - 170 * 60000, i: 981 }, { ts: NOW - 100 * 60000, i: 982 }, { ts: NOW - 20 * 60000, i: 983 }];
    const cur = setup({ archive: [...ctxArchive(), ...apps], server: ctxServer() });
    await S.runSentimentV2Cycle(env(cur), { nowTs: NOW - 4 * H });
    await S.runSentimentV2Cycle(env(cur), { nowTs: NOW, queryBudget: PROD_LEFT });
    const ref = setup({ archive: [...ctxArchive(), ...apps], server: ctxServer() });
    await S.runSentimentV2Cycle(env(ref), { nowTs: NOW - 4 * H });
    ref.db.prepare("UPDATE sentiment_v2_results SET decision_ts = ? WHERE decision_ts = (SELECT MAX(decision_ts) FROM sentiment_v2_results)").run(NOW - H + C - 1);
    const alone = await S.runSentimentV2Cycle(env(ref), { nowTs: NOW - H + C });
    expect(alone.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[NOW - H + C, 'server']]);
    const cols = 'decision_ts, calc_version, status, degraded_reason, sentiment_score, sentiment_class, confidence, evidence_coverage, market_regime, regime_json, event_intensity, event_json, predictive_json, family_json, supporting_ids_json, contradicting_ids_json, missing_json, ai_model, ai_status';
    const row = (d) => d.db.prepare(`SELECT ${cols} FROM sentiment_v2_results WHERE decision_ts = ?`).get(NOW - H + C);
    expect(row(cur)).toEqual(row(ref));
  });

  it('declared fixed cost is exact: worst case (collector on, every source missing, server read) = 16 + 1 per reading', async () => {
    const d1 = setup({ archive: ctxArchive(), server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 2 * H });
    const c = countingDb(d1);
    const r = await S.runSentimentV2Cycle({ DB: c.DB, SENTIMENT_V2_ENABLED: '1', SV2_V1_SERVER_INPUT: '1', SV2_COLLECTOR_ENABLED: '1' }, { nowTs: NOW - H + 60000 });
    expect(r.written).toBe(1);
    expect(SV2_SOURCE_REGISTRY.length).toBe(8);                    // 8 per-source lookups are inside the fixed 16
    expect(c.q).toBe(S.SV2_CYCLE_QUERIES_FIXED + S.SV2_CYCLE_QUERIES_PER_READING_AI_OFF);
  });

  it('never exceeds the budget it is given, AI off or on, server input on or off', async () => {
    const GLM = '@cf/zai-org/glm-4.7-flash';
    const AI = { run: async () => ({ response: '{}' }) };   // invalid output: the ledger path still runs in full
    for (const ai of [false, true]) for (const on of [false, true]) for (let budget = 0; budget <= 40; budget += (budget < 26 ? 1 : 2)) {
      const d1 = setup({ archive: [...ctxArchive(), { ts: NOW - 170 * 60000, i: 997 }, { ts: NOW - 100 * 60000, i: 995 }, { ts: NOW - 20 * 60000, i: 996 }], server: ctxServer() });
      await S.runSentimentV2Cycle(env(d1, on), { nowTs: NOW - 4 * H });
      const c = countingDb(d1);
      const e = { DB: c.DB, SENTIMENT_V2_ENABLED: '1', SV2_COLLECTOR_ENABLED: '1', ...(on ? { SV2_V1_SERVER_INPUT: '1' } : {}),
        ...(ai ? { SENTIMENT_V2_AI_MODEL: GLM, SV2_GLM_DAILY_NEURONS: '100000', AI } : {}) };
      const r = await S.runSentimentV2Cycle(e, { nowTs: NOW, queryBudget: budget });
      expect(c.q).toBeLessThanOrEqual(budget);
      if (on) expect(r.written || 0).toBeLessThanOrEqual(2);
    }
  });

  it('app readings are scored exactly as before: identical rows to the uncapped archive-only path', async () => {
    const apps = [{ ts: NOW - 170 * 60000, i: 981 }, { ts: NOW - 100 * 60000, i: 982 }, { ts: NOW - 20 * 60000, i: 983 }];
    const ref = setup({ archive: [...ctxArchive(), ...apps], server: ctxServer() });
    const cur = setup({ archive: [...ctxArchive(), ...apps], server: ctxServer() });
    await S.runSentimentV2Cycle(env(ref, false), { nowTs: NOW - 4 * H });
    await S.runSentimentV2Cycle(env(cur, true), { nowTs: NOW - 4 * H });
    for (let k = 0; k < 4; k++) {
      await S.runSentimentV2Cycle(env(ref, false), { nowTs: NOW + k * 60000 });
      await S.runSentimentV2Cycle(env(cur, true), { nowTs: NOW + k * 60000, queryBudget: PROD_LEFT });
    }
    const cols = 'decision_ts, calc_version, status, degraded_reason, sentiment_score, sentiment_class, confidence, evidence_coverage, market_regime, regime_json, event_intensity, event_json, predictive_json, family_json, supporting_ids_json, contradicting_ids_json, missing_json, ai_model, ai_status';
    const rows = (d) => d.db.prepare(`SELECT ${cols} FROM sentiment_v2_results WHERE decision_ts IN (${apps.map(a => a.ts).join(',')}) ORDER BY decision_ts`).all();
    // Run 1 scores the oldest app reading and the server reading (slot kept); the app reading between them is passed by the
    // cursor (server_slot_priority, recorded); the newest follows in run 2. Every scored app reading is byte-identical to
    // the archive-only path.
    const refBy = new Map(rows(ref).map(r => [r.decision_ts, r]));
    expect(rows(ref).length).toBe(3);
    expect(rows(cur).map(r => r.decision_ts)).toEqual([apps[0].ts, apps[2].ts]);
    for (const r of rows(cur)) expect(r).toEqual(refBy.get(r.decision_ts));
    const usage = JSON.parse(cur.db.prepare('SELECT usage_json FROM sentiment_v2_results WHERE decision_ts = ?').get(NOW - H + C).usage_json);
    expect(usage.skipped_readings).toContainEqual({ ts: apps[1].ts, origin: 'browser', reason: 'server_slot_priority' });
  });

  it('only when the budget fits a single reading does the server reading take it; the older app reading is reported as skipped', async () => {
    const tApp = NOW - 150 * 60000;
    const d1 = setup({ archive: [...ctxArchive(), { ts: tApp, i: 990 }], server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 4 * H });
    const r = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW, queryBudget: S.SV2_CYCLE_QUERIES_FIXED + 1 });
    expect(r.decisions.map(x => [x.decision_ts, x.v1_origin])).toEqual([[NOW - H + C, 'server']]);
    expect(r.skipped_readings).toEqual([
      { ts: NOW - 4 * H + C, origin: 'server', reason: 'one_server_per_cycle' },
      { ts: NOW - 3 * H + C, origin: 'server', reason: 'one_server_per_cycle' },
      { ts: tApp, origin: 'browser', reason: 'server_slot_priority' },
      { ts: NOW - 2 * H + C, origin: 'server', reason: 'one_server_per_cycle' }]);
    const again = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW + 60000 });
    expect(again.written || 0).toBe(0);                          // decisions only move forward, as before
  });

  it('server input off: no cap, all pending readings fit at the real cost (previous ordering)', async () => {
    const apps = [{ ts: NOW - 170 * 60000, i: 981 }, { ts: NOW - 100 * 60000, i: 982 }, { ts: NOW - 20 * 60000, i: 983 }];
    const d1 = setup({ archive: [...ctxArchive(), ...apps], server: ctxServer() });
    await S.runSentimentV2Cycle(env(d1, false), { nowTs: NOW - 4 * H });
    const r = await S.runSentimentV2Cycle(env(d1, false), { nowTs: NOW, queryBudget: PROD_LEFT });
    expect(r.decisions.map(x => x.decision_ts)).toEqual(apps.map(a => a.ts));
  });
});
