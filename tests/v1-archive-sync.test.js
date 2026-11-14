// V1 archive copy in the Worker (syncV1ArchiveFromHistory) and its effect on Sentiment V2.
// Regression for 2026-11-14: V1 wrote a reading to `history` at 18:30 UTC, but V2 still saw 11:23 as the newest V1
// reading, because research_sentiment_archive was only filled by a GitHub Actions job whose scheduled runs were late
// or dropped. Runs against real SQLite (node:sqlite) with the production schema of both tables.
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
const V2_DDL = readFileSync(join(ROOT, '.ai/migrations/0027_sentiment_v2_results.sql'), 'utf8') + '\n' + readFileSync(join(ROOT, '.ai/migrations/0029_sv2_evidence_and_ai_budget.sql'), 'utf8');
// V1's `history` table as PulseWorker creates it (columns this code reads, plus btc_price which it must not copy).
const HISTORY_DDL = `CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL,
  sources_json TEXT, technical_score INTEGER, gold_regime TEXT, btc_price REAL, regime_mag REAL, bottom_score REAL);
  CREATE INDEX idx_ts ON history(ts);`;
const EVENTS_DDL = `CREATE TABLE event_ledger (event_id TEXT, dedup_key TEXT, correlation_key TEXT, event_type TEXT, asset TEXT,
  source TEXT, source_ts INTEGER, detected_ts INTEGER, direction TEXT, severity TEXT, confidence REAL, rule_id TEXT, expires_ts INTEGER);
  CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL);`;

const SYNC = ['v1ArchivePyJson', 'v1ArchiveContentHash', 'v1ArchiveInvalidReason', 'syncV1ArchiveFromHistory'];
const SYNC_CONSTS = ['V1_ARCHIVE_SYNC_WINDOW_MS', 'V1_ARCHIVE_SYNC_MAX_ROWS', 'V1_ARCHIVE_WRITTEN_BY', 'V1_ARCHIVE_SOURCE_WEIGHTS_VERSION',
  'V1_ARCHIVE_SCHEMA_VERSION', 'V1_ARCHIVE_FUTURE_SKEW_MS', 'V1_ARCHIVE_INSERT_SQL'];
const V2 = ['sv2Config', 'sv2Finite', 'sv2ParseSources', 'sv2History', 'sv2AttachPrices', 'sv2IndexAtOrBefore', 'sv2PriceAt',
  'sv2SourceStatus', 'sv2Pearson', 'sv2FamilyOf', 'sv2Redundancy', 'sv2Families', 'sv2EventFamily', 'sv2EventReactions',
  'sv2Events', 'sv2Regime', 'sv2Profile', 'sv2Analogues', 'sv2Classify', 'sv2Compute', 'sv2AiPackage',
  'sv2ValidateAiOutput', 'sv2RunAi', 'sv2Row', 'runSentimentV2Cycle', 'sv2AlignSeries', 'sv2SeriesStatus', 'getSentimentV2Comparison',
  'sv2EvidenceSummary', 'sv2EvidenceItemsForAi', 'sv2AiBudget', 'sv2AiEstimateNeurons', 'sv2UngroundedNumbers', 'sv2LatestView'];
const V2_CONSTS = ['SENTIMENT_V2_VERSION', 'SV2_VERSIONS_SHOWN', 'SV2_INSERT_SQL', 'SV2_PERIODS_MS', 'SV2_MAX_POINTS', 'SV2_MAX_ROWS', 'SV2_MAX_PRICE_ROWS', 'SV2_STALE_MS',
  'SV2_AI_PRICING', 'SV2_AI_MAX_OUTPUT_TOKENS', 'SV2_AI_RESERVE_SQL'];
const S = evalInScope(extractConstants(...SYNC_CONSTS, ...V2_CONSTS) + '\n' + extractFunctions(...SYNC, ...V2),
  { SV2_SOURCE_REGISTRY, sv2EvidenceReadings, sv2ScoreHeadline, SV2_EVIDENCE_RULES });

const H = 3600000;
const NOW = Date.UTC(2026, 10, 14, 20, 0, 0);
const KEYS = ['fng', 'cryptonews', 'foufi', 'funding', 'longshort', 'hypefunding', 'hloi', 'onchain', 'macrogeo', 'usd', 'oil',
  'gold', 'nasdaq', 'sp500', 'ninemag', 'regulatory', 'geopolitics', 'strc'];
const sources = (i) => JSON.stringify(Object.fromEntries(KEYS.map((k, j) => [k, Math.round((50 + 20 * Math.sin(i / (4 + j) + j)) * 100) / 100])));

function setup({ history = [], archive = [] } = {}) {
  const d1 = makeD1([HISTORY_DDL, ARCHIVE_DDL, V2_DDL, EVENTS_DDL]);
  for (const r of history) {
    d1.db.prepare('INSERT INTO history (ts, score, sources_json, technical_score, gold_regime, btc_price) VALUES (?, ?, ?, ?, ?, ?)')
      .run(r.ts, r.score, r.sources_json ?? null, r.technical_score ?? null, r.gold_regime ?? null, r.btc_price ?? 70000);
  }
  for (const r of archive) {
    d1.db.prepare('INSERT INTO research_sentiment_archive (observation_ts, sources_json, score, technical_score, btc_price, gold_regime, source_weights_version, schema_version, written_by, content_hash, archived_ts) VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?)')
      .run(r.observation_ts, r.sources_json, r.score, 'v1-unversioned', 'exp5-v1', 'experiment5-pipeline', r.content_hash || 'x', r.archived_ts || 1);
  }
  return d1;
}
const archiveRows = (d1) => d1.db.prepare('SELECT * FROM research_sentiment_archive ORDER BY observation_ts').all().map(r => ({ ...r }));
const env = (d1, extra = {}) => ({ DB: d1.binding, SENTIMENT_V2_ENABLED: '1', ...extra });

describe('row format: identical to the Python Experiment 5 job', () => {
  // Golden hashes produced by research/sentiment_archive.py (archive_observation / compute_content_hash), not re-derived.
  const GOLDEN = [
    [1794655417334, '{"fng":24,"funding":61.5}', 30, null, null, 'e84b7cc25d05d5061e1bb73940dbca12e63e82abfb3113308395d665254b2ddd'],
    [1794681043592, '{}', 35, 41, 'risk-off', '3f2e2f362fc4f487bb594dc0d26551a8aee8080f71031dcc63c1639c672ed322'],
    [1700000000000, '{"x":1}', 47.25, 12.5, 'safe-haven é', '743707e52aea768ff870b91b304246d49e5e6de09243f3f7309c45d097e3497a'],
    [1700000003600, '{"a":[1,2]}', 0, 0.00001, null, 'a05191bc41a94dd266c559e0c2607efd85a4d438dfc39f96e617bd7009edd8e2'],
    [1700000007200, '{"b":"\\u00e9"}', 100, 99.5, '\u007f', '897b3577c85c4215b858b5d5683be35f35a79695eb90f44a5b4664ba08c3a7c7'],
  ];
  it('content_hash matches the Python archive module byte for byte', async () => {
    for (const [ts, src, score, tech, gold, hash] of GOLDEN) {
      expect(await S.v1ArchiveContentHash(ts, src, score, tech, null, gold)).toBe(hash);
    }
  });
  it('formats scalars like Python json.dumps', () => {
    expect([S.v1ArchivePyJson(null), S.v1ArchivePyJson(35), S.v1ArchivePyJson(35.5), S.v1ArchivePyJson(0.00001),
      S.v1ArchivePyJson(1.5e-7), S.v1ArchivePyJson('é"'), S.v1ArchivePyJson(true)])
      .toEqual(['null', '35', '35.5', '1e-05', '1.5e-07', '"\\u00e9\\""', 'true']);
  });
  it('copies V1 fields verbatim, never btc_price, with provenance; empty sources become "{}" like the Python job', async () => {
    const d1 = setup({ history: [{ ts: NOW - H, score: 30, sources_json: '{"fng":24}', technical_score: 41, gold_regime: 'risk-off', btc_price: 66989 },
      { ts: NOW - 2 * H, score: 33, sources_json: null }] });
    const r = await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW });
    expect(r).toMatchObject({ ok: true, copied: 2, already_present: 0, history_rows: 2, history_last_ts: NOW - H });
    const [older, newer] = archiveRows(d1);
    expect(newer).toMatchObject({ observation_ts: NOW - H, sources_json: '{"fng":24}', score: 30, technical_score: 41, btc_price: null,
      gold_regime: 'risk-off', source_weights_version: 'v1-unversioned', schema_version: 'exp5-v1',
      written_by: 'pulseworker-v2-archive-sync', archived_ts: NOW });
    expect(newer.content_hash).toBe(await S.v1ArchiveContentHash(NOW - H, '{"fng":24}', 30, 41, null, 'risk-off'));
    expect(older.sources_json).toBe('{}');
  });
});

describe('successful collection, idempotency and the regression itself', () => {
  it('a reading in history but not in the archive reaches V2 in the same cron chain (the 2026-11-14 gap)', async () => {
    const t1123 = NOW - 9 * H + 23 * 60000, t1830 = NOW - 2 * H + 30 * 60000;
    const history = []; const archive = [];
    for (let i = 0; i < 40; i++) {             // context V2 needs, already archived by the GitHub job
      const ts = t1123 - (40 - i) * 2 * H;
      history.push({ ts, score: 40, sources_json: sources(i) });
      archive.push({ observation_ts: ts, score: 40, sources_json: sources(i) });
    }
    history.push({ ts: t1123, score: 30, sources_json: sources(40) });
    archive.push({ observation_ts: t1123, score: 30, sources_json: sources(40) });
    history.push({ ts: t1830, score: 35, sources_json: sources(41) });       // written by V1, not archived yet
    const d1 = setup({ history, archive });
    await S.runSentimentV2Cycle(env(d1), { nowTs: NOW - 3 * H });          // V2 has already evaluated 11:23
    const before = await S.getSentimentV2Comparison(env(d1), '24h', NOW);
    expect(before.series_status.v2.last_ts).toBe(t1123);
    expect(before.v1_history_last_ts).toBe(t1830);                          // visible: V1 produced, archive lags

    const sync = await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW });
    expect(sync).toMatchObject({ ok: true, copied: 1 });
    const v2 = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    expect(v2.written).toBe(1);
    expect(v2.decisions[0]).toMatchObject({ decision_ts: t1830, inserted: true });
    const after = await S.getSentimentV2Comparison(env(d1), '24h', NOW);
    const last = after.points[after.points.length - 1];
    expect(last).toMatchObject({ ts: t1830, v1: 35 });
    expect(last.v2).not.toBeNull();
    expect(after.series_status.v2.last_ts).toBe(t1830);
  });
  it('a re-run writes nothing; rows the GitHub job already archived are not touched', async () => {
    const d1 = setup({ history: [{ ts: NOW - H, score: 30, sources_json: '{}' }, { ts: NOW - 3 * H, score: 31, sources_json: '{}' }],
      archive: [{ observation_ts: NOW - 3 * H, score: 31, sources_json: '{}', content_hash: 'from-python', archived_ts: 7 }] });
    expect((await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW })).copied).toBe(1);
    const again = await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW + H });
    expect(again).toMatchObject({ ok: true, copied: 0, already_present: 0 });
    const rows = archiveRows(d1);
    expect(rows.length).toBe(2);
    expect(rows[0]).toMatchObject({ content_hash: 'from-python', archived_ts: 7, written_by: 'experiment5-pipeline' });
  });
  it('a row archived concurrently by the GitHub job is counted as already present, never duplicated', async () => {
    const d1 = setup({ history: [{ ts: NOW - H, score: 30, sources_json: '{}' }] });
    const raceEnv = env(d1);
    const prepare = raceEnv.DB.prepare;
    raceEnv.DB = { ...raceEnv.DB, prepare: (sql) => {
      if (sql.startsWith('INSERT OR IGNORE INTO research_sentiment_archive')) {   // the job's INSERT lands first
        d1.db.prepare("INSERT INTO research_sentiment_archive (observation_ts, sources_json, score, source_weights_version, schema_version, written_by, content_hash, archived_ts) VALUES (?, '{}', 30, 'v1-unversioned', 'exp5-v1', 'experiment5-pipeline', 'h', 1)").run(NOW - H);
      }
      return prepare(sql);
    } };
    const r = await S.syncV1ArchiveFromHistory(raceEnv, { nowTs: NOW });
    expect(r).toMatchObject({ ok: true, copied: 0, already_present: 1 });
    expect(archiveRows(d1).length).toBe(1);
  });
  it('reads a bounded, indexed window and copies oldest first', async () => {
    const history = Array.from({ length: 5 }, (_, i) => ({ ts: NOW - (i + 1) * H, score: 30 + i, sources_json: '{}' }));
    history.push({ ts: NOW - 72 * H, score: 50, sources_json: '{}' });     // outside the 48 h window
    const d1 = setup({ history });
    const r = await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW });
    expect(r.copied).toBe(5);
    expect(archiveRows(d1).map(x => x.observation_ts)).toEqual([5, 4, 3, 2, 1].map(i => NOW - i * H));
    const reads = d1.log.filter(l => /^SELECT/.test(l.sql));
    expect(reads.length).toBe(2);
    for (const l of reads) { expect(l.sql).toMatch(/LIMIT \?/); expect(l.plan.join(' ')).toMatch(/SEARCH|USING INDEX/); }
  });
});

describe('invalid input, failures and error visibility', () => {
  it('never copies a future-dated reading or a score outside 0..100; reports the invalid score', async () => {
    const d1 = setup({ history: [{ ts: NOW + 10 * 60000, score: 30 }, { ts: NOW - H, score: 130 }, { ts: NOW - 2 * H, score: 40, sources_json: '{}' }] });
    const r = await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW });
    expect(r.copied).toBe(1);
    expect(r.invalid).toEqual([{ ts: NOW - H, reason: 'score missing or outside 0..100' }]);
    expect(archiveRows(d1).map(x => x.observation_ts)).toEqual([NOW - 2 * H]);   // the future row is not even read yet
    expect(S.v1ArchiveInvalidReason({ ts: NOW + 10 * 60000, score: 30 }, NOW)).toBe('ts in the future');
    const later = await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW + H });   // once its time has passed it is a reading
    expect(later.copied).toBe(1);
    expect(archiveRows(d1).map(x => x.observation_ts)).toEqual([NOW - 2 * H, NOW + 10 * 60000]);
  });
  it('a database write failure is reported as ok:false with the error, and the other rows are still copied', async () => {
    const d1 = setup({ history: [{ ts: NOW - H, score: 30, sources_json: '{}' }, { ts: NOW - 2 * H, score: 31, sources_json: '{}' }] });
    const e = env(d1); const prepare = e.DB.prepare;
    e.DB = { ...e.DB, prepare: (sql) => { const st = prepare(sql);
      return { ...st, bind: (...a) => { const b = st.bind(...a);
        if (sql.startsWith('INSERT') && a[0] === NOW - 2 * H) return { ...b, run: async () => { throw new Error('D1_ERROR: database is locked'); } };
        return b; } }; } };
    const r = await S.syncV1ArchiveFromHistory(e, { nowTs: NOW });
    expect(r.ok).toBe(false);
    expect(r.copied).toBe(1);
    expect(r.errors[0]).toMatch(/insert .* failed: D1_ERROR: database is locked/);
  });
  it('a read failure (e.g. the archive table is missing) is reported, not thrown, and nothing is written', async () => {
    const d1 = makeD1([HISTORY_DDL]);
    d1.db.prepare('INSERT INTO history (ts, score) VALUES (?, ?)').run(NOW - H, 30);
    const r = await S.syncV1ArchiveFromHistory({ DB: d1.binding }, { nowTs: NOW });
    expect(r.ok).toBe(false);
    expect(r.errors[0]).toMatch(/read failed: .*no such table/);
  });
  it('no V1 reading in the window is ok with nothing copied (V2 then has nothing new to compute)', async () => {
    const d1 = setup();
    expect(await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW })).toMatchObject({ ok: true, copied: 0, history_rows: 0, history_last_ts: null });
    const v2 = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    expect(v2).toMatchObject({ ok: true, written: 0 });
  });
  it('can be switched off with V1_ARCHIVE_SYNC_ENABLED = "0" without reading anything', async () => {
    const d1 = setup({ history: [{ ts: NOW - H, score: 30 }] });
    expect(await S.syncV1ArchiveFromHistory(env(d1, { V1_ARCHIVE_SYNC_ENABLED: '0' }), { nowTs: NOW })).toMatchObject({ skipped: true });
    expect(d1.log.length).toBe(0);
  });
});

describe('V2 only computes from new valid V1 readings', () => {
  it('without a new archived reading V2 writes nothing; with one it writes exactly one result', async () => {
    const history = Array.from({ length: 30 }, (_, i) => ({ ts: NOW - (60 - 2 * i) * H, score: 40 + (i % 5), sources_json: sources(i) }));
    const d1 = setup({ history });
    await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW });                 // only the last 48 h are copied
    const first = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW });
    expect(first.written).toBe(1);
    expect((await S.runSentimentV2Cycle(env(d1), { nowTs: NOW + H })).written).toBe(0);
    d1.db.prepare('INSERT INTO history (ts, score, sources_json) VALUES (?, ?, ?)').run(NOW + 30 * 60000, 120, sources(99));   // invalid
    await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW + H });
    expect((await S.runSentimentV2Cycle(env(d1), { nowTs: NOW + H })).written).toBe(0);
    d1.db.prepare('INSERT INTO history (ts, score, sources_json) VALUES (?, ?, ?)').run(NOW + 90 * 60000, 37, sources(100));
    await S.syncV1ArchiveFromHistory(env(d1), { nowTs: NOW + 2 * H });
    const next = await S.runSentimentV2Cycle(env(d1), { nowTs: NOW + 2 * H });
    expect(next.written).toBe(1);
    expect(next.decisions[0].decision_ts).toBe(NOW + 90 * 60000);
  });
  it('the API reports the newest history reading next to stale archived V1, and null when history cannot be read', async () => {
    const d1 = setup({ history: [{ ts: NOW - H, score: 30 }], archive: [{ observation_ts: NOW - 9 * H, score: 31, sources_json: '{}' }] });
    const r = await S.getSentimentV2Comparison(env(d1), '24h', NOW);
    expect(r.series_status.v1).toMatchObject({ last_ts: NOW - 9 * H, stale: true });
    expect(r.v1_history_last_ts).toBe(NOW - H);
    const noHist = makeD1([ARCHIVE_DDL, V2_DDL, EVENTS_DDL]);
    expect((await S.getSentimentV2Comparison({ DB: noHist.binding }, '24h', NOW)).v1_history_last_ts).toBeNull();
  });
});

describe('wiring and safety', () => {
  const sched = WORKER.slice(WORKER.indexOf("} else if (event.cron === '0 * * * *') {"), WORKER.indexOf('// No catch-all else'));
  it('runs after Event Detection and before V2, only in non-tick hours, and never blocks V2 on failure', () => {
    const iDet = sched.indexOf('runAutomaticDetection'), iSync = sched.indexOf('syncV1ArchiveFromHistory'), iV2 = sched.indexOf('runSentimentV2Cycle');
    expect(iDet).toBeGreaterThan(0); expect(iSync).toBeGreaterThan(iDet); expect(iV2).toBeGreaterThan(iSync);
    expect(sched.indexOf('syncV1ArchiveFromHistory')).toBeGreaterThan(sched.indexOf('} else {'));
    expect(sched.slice(iSync, iV2)).toMatch(/\.catch\(err => console\.error\('V1 archive sync \(cron\) failed:'/);
    expect(sched.slice(iSync, iV2)).toMatch(/console\.error\('v1 archive sync \(cron\) FAILED:'/);
  });
  it('reads history only, writes only INSERT OR IGNORE into research_sentiment_archive, and reaches no network', () => {
    const body = extractFunctions(...SYNC) + extractConstants(...SYNC_CONSTS);
    const writes = body.match(/\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE)\b[^'`]*/g) || [];
    expect(writes.every(w => /^INSERT OR IGNORE INTO research_sentiment_archive/.test(w))).toBe(true);
    expect(body).not.toMatch(/\bfetch\(/);
  });
  it('ships enabled in wrangler.toml, with a documented off switch', () => {
    expect(readFileSync(join(ROOT, 'wrangler.toml'), 'utf8')).toMatch(/\nV1_ARCHIVE_SYNC_ENABLED = "1"\n/);
  });
});
