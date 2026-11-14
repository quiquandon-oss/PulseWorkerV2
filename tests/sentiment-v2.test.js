// Sentiment V2 (docs/SENTIMENT_V2_BUILD_SPEC.md §9): score and schema, families, redundancy, stale/missing sources,
// event timestamps/dedup/relevance/overlap, AI validation/quota/unavailability, zero paid fallback, idempotency,
// V1 immutability, V1/V2/BTC alignment, API periods and empty history, point-in-time correctness. Synthetic data only.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';
import { SV2_SOURCE_REGISTRY } from '../sentiment-v2/collector.js';
import { sv2EvidenceReadings, sv2ScoreHeadline, SV2_EVIDENCE_RULES } from '../sentiment-v2/evidence-score.js';

const FUNCS = ['sv2Config', 'sv2Finite', 'sv2ParseSources', 'sv2History', 'sv2AttachPrices', 'sv2IndexAtOrBefore', 'sv2PriceAt',
  'sv2SourceStatus', 'sv2Pearson', 'sv2FamilyOf', 'sv2Redundancy', 'sv2Families', 'sv2EventFamily', 'sv2EventReactions',
  'sv2Events', 'sv2Regime', 'sv2Profile', 'sv2Analogues', 'sv2Classify', 'sv2Compute', 'sv2AiPackage',
  'sv2ValidateAiOutput', 'sv2RunAi', 'sv2Row', 'runSentimentV2Cycle', 'sv2AlignSeries', 'sv2SeriesStatus',
  'getSentimentV2Comparison', 'getSentimentV2ComparisonCached', 'sv2EvidenceSummary', 'sv2EvidenceItemsForAi', 'sv2AiBudget',
  'sv2AiEstimateNeurons', 'sv2UngroundedNumbers', 'sv2LatestView'];
const CONSTS = ['SENTIMENT_V2_VERSION', 'SV2_VERSIONS_SHOWN', 'SV2_INSERT_SQL', 'SV2_PERIODS_MS', 'SV2_MAX_POINTS', 'SV2_MAX_ROWS', 'SV2_MAX_PRICE_ROWS', 'SV2_STALE_MS', 'sv2ComparisonCache',
  'SV2_AI_PRICING', 'SV2_AI_MAX_OUTPUT_TOKENS', 'SV2_AI_RESERVE_SQL'];
const GLOBALS = { SV2_SOURCE_REGISTRY, sv2EvidenceReadings, sv2ScoreHeadline, SV2_EVIDENCE_RULES };
const SRC = extractConstants(...CONSTS) + '\n' + extractFunctions(...FUNCS);
const fresh = () => evalInScope(SRC, GLOBALS);
const S = fresh();
const cfg = S.sv2Config();
const H = 3600000, D = 24 * H;
const T0 = Date.UTC(2026, 9, 1);

const KEYS = ['fng', 'cryptonews', 'foufi', 'funding', 'longshort', 'hypefunding', 'hloi', 'etfflows', 'sosovalue', 'onchain',
  'macrogeo', 'yield10y', 'usd', 'oil', 'gold', 'nasdaq', 'sp500', 'ninemag', 'regulatory', 'geopolitics', 'strc'];

function archive(n, { step = 2 * H, start = T0, src = null, price = null } = {}) {
  const rows = [];
  for (let i = 0; i < n; i++) {
    const sources = {};
    KEYS.forEach((k, j) => { sources[k] = Math.round((50 + 20 * Math.sin(i / (5 + j) + j)) * 100) / 100; });
    if (src) Object.assign(sources, src(i, sources));
    rows.push({ observation_ts: start + i * step, score: Math.round((40 + 10 * Math.sin(i / 7)) * 100) / 100,
      btc_price: price ? price(i) : 60000 + 30 * i + 500 * Math.sin(i / 11), sources_json: JSON.stringify(sources) });
  }
  return rows;
}

function fakeDb({ archive: arch = [], events = [], results = [], failEvents = false, prices = [], noTable = false, slots = [], evidence = [], noEvidenceTable = false } = {}) {
  const log = [];
  const exec = (sql, a) => {
    log.push(sql);
    if (sql.startsWith('SELECT name FROM sqlite_master')) return noTable ? null : { name: 'sentiment_v2_results' };
    // Neuron ledger (shared `slots` array plays the table; the budget condition is evaluated exactly as the SQL does)
    if (sql.startsWith('INSERT INTO sv2_ai_neurons')) {
      const [day, ts, model, est, , , est2, budget] = a;
      const committed = slots.filter(x => x.day === day).reduce((s2, x) => s2 + Math.max(x.reserved_neurons, x.actual_neurons || 0), 0);
      if (committed + est2 > budget) return { meta: { changes: 0 } };
      const slot = slots.filter(x => x.day === day).length + 1;
      slots.push({ rowid: slots.length + 1, day, slot, reserved_ts: ts, model, reserved_neurons: est, actual_neurons: null, status: 'reserved' });
      return { meta: { changes: 1, last_row_id: slots.length } };
    }
    if (sql.startsWith('SELECT slot FROM sv2_ai_neurons WHERE rowid')) { const x = slots.find(y => y.rowid === a[0]); return x ? { slot: x.slot } : null; }
    if (sql.startsWith('UPDATE sv2_ai_neurons')) {
      const x = slots.find(y => y.day === a[4] && y.slot === a[5]);
      if (x) Object.assign(x, { input_tokens: a[0], output_tokens: a[1], actual_neurons: a[2], status: a[3] });
      return { meta: { changes: x ? 1 : 0 } };
    }
    if (sql.startsWith("SELECT 1 AS x FROM sv2_ai_neurons")) return slots.some(y => y.day === a[0] && y.status === 'account_limited') ? { x: 1 } : null;
    if (sql.startsWith('SELECT evidence_id, source_id, title, canonical_url, published_ts, retrieved_ts, dup_of FROM sv2_evidence')) {
      if (noEvidenceTable) throw new Error('no such table: sv2_evidence');
      if (/WHERE source_id = \?/.test(sql)) {
        const f = evidence.filter(e => e.source_id === a[0] && e.retrieved_ts <= a[1] && !e.dup_of).sort((x, y) => y.retrieved_ts - x.retrieved_ts);
        return f[0] || null;
      }
      if (!/LIMIT \?$/.test(sql)) throw new Error('unbounded evidence read');
      return evidence.filter(e => e.retrieved_ts <= a[0] && !e.dup_of).sort((x, y) => y.retrieved_ts - x.retrieved_ts).slice(0, a[a.length - 1]);
    }
    if (sql.startsWith('SELECT ts FROM history')) throw new Error('no such table: history');
    if (sql.startsWith('SELECT MAX(decision_ts)')) {
      const ts = results.filter(r => r.asset === a[0] && r.calc_version === a[1]).map(r => r.decision_ts);
      return { t: ts.length ? Math.max(...ts) : null };
    }
    if (sql.startsWith('SELECT observation_ts FROM research_sentiment_archive')) {
      return arch.filter(r => r.observation_ts > a[0] && r.observation_ts <= a[1]).sort((x, y) => y.observation_ts - x.observation_ts)
        .slice(0, a[2]).map(r => ({ observation_ts: r.observation_ts }));
    }
    if (sql.startsWith('SELECT observation_ts, score, btc_price, sources_json FROM research_sentiment_archive')) {
      return arch.filter(r => r.observation_ts >= a[0] && r.observation_ts <= a[1]).sort((x, y) => x.observation_ts - y.observation_ts);
    }
    if (sql.startsWith('SELECT observation_ts, score, btc_price FROM research_sentiment_archive')) {
      if (!/ORDER BY observation_ts DESC LIMIT \?/.test(sql)) throw new Error('unbounded archive read');
      return arch.filter(r => r.observation_ts >= a[0] && r.observation_ts <= a[1]).sort((x, y) => y.observation_ts - x.observation_ts)
        .slice(0, a[2]).map(({ observation_ts, score, btc_price }) => ({ observation_ts, score, btc_price }));
    }
    if (sql.startsWith('SELECT ts, btc_price FROM btc_data')) {
      const f = prices.filter(r => r.ts >= a[0] && r.ts <= a[1]);
      return /DESC/.test(sql) ? f.sort((x, y) => y.ts - x.ts).slice(0, a[2]) : f.sort((x, y) => x.ts - y.ts);
    }
    if (sql.startsWith('SELECT event_id')) {
      if (failEvents) throw new Error('no such table: event_ledger');
      const f = events.filter(e => e.detected_ts >= a[0] && e.detected_ts <= a[1]);
      return /DESC/.test(sql) ? f.sort((x, y) => y.detected_ts - x.detected_ts).slice(0, a[2]) : f;
    }
    if (sql.startsWith('INSERT OR IGNORE INTO sentiment_v2_results')) {
      if (a.length !== 23) throw new Error('bind count');
      const key = `${a[0]}|${a[1]}|${a[2]}`;
      if (results.some(r => r.key === key)) return { meta: { changes: 0 } };
      results.push({ key, asset: a[0], decision_ts: a[1], calc_version: a[2], status: a[3], degraded_reason: a[4],
        sentiment_score: a[5], sentiment_class: a[6], confidence: a[7], ai_model: a[18], ai_status: a[19], created_ts: a[22] });
      return { meta: { changes: 1 } };
    }
    if (sql.startsWith('SELECT decision_ts, sentiment_score, status, calc_version FROM sentiment_v2_results')) {
      if (!/DESC LIMIT \?/.test(sql)) throw new Error('unbounded V2 read');
      const nv = (sql.match(/IN \(([?,\s]+)\)/)[1].match(/\?/g) || []).length;
      const vs = a.slice(0, nv), [from, to, lim] = a.slice(nv);
      return results.filter(r => vs.includes(r.calc_version) && r.decision_ts >= from && r.decision_ts <= to).sort((x, y) => y.decision_ts - x.decision_ts).slice(0, lim);
    }
    if (sql.startsWith('SELECT decision_ts FROM sentiment_v2_results') || sql.startsWith('SELECT decision_ts, calc_version FROM sentiment_v2_results')) {
      if (!/LIMIT 1$/.test(sql)) throw new Error('unbounded V2 metadata read');
      const vs = /IN \(/.test(sql) ? a : [a[0]];
      const rs = results.filter(r => vs.includes(r.calc_version));
      if (!rs.length) return null;
      const pick = rs.reduce((m, r) => (/DESC/.test(sql) ? r.decision_ts > m.decision_ts : r.decision_ts < m.decision_ts) ? r : m);
      return { decision_ts: pick.decision_ts, calc_version: pick.calc_version };
    }
    if (sql.startsWith('SELECT decision_ts, calc_version, status, degraded_reason')) return results.find(r => r.decision_ts === a[1] && r.calc_version === a[0]) || null;
    throw new Error(`unexpected SQL: ${sql}`);
  };
  return { log, results, slots, prepare(sql) {
    const st = { args: [], bind(...x) { st.args = x; return st; },
      first: async () => exec(sql, st.args), all: async () => ({ results: exec(sql, st.args) }), run: async () => exec(sql, st.args) };
    return st;
  } };
}

const ENABLED = { SENTIMENT_V2_ENABLED: '1' };

describe('score and output schema', () => {
  const hist = S.sv2History(archive(300));
  const r = S.sv2Compute(hist, hist.length - 1, [], cfg);
  it('produces every spec §5 output, each as a distinct field', () => {
    for (const k of ['asset', 'decision_ts', 'calc_version', 'sentiment_score', 'sentiment_class', 'confidence', 'evidence_coverage',
      'market_regime', 'event_intensity', 'predictive', 'families', 'supporting_ids', 'contradicting_ids', 'missing', 'status']) {
      expect(r).toHaveProperty(k);
    }
    expect(r.sentiment_score).toBeGreaterThanOrEqual(0);
    expect(r.sentiment_score).toBeLessThanOrEqual(100);
    expect(r.confidence).toBeGreaterThanOrEqual(0);
    expect(r.confidence).toBeLessThanOrEqual(1);
    expect(r.predictive.validation).toBe('candidate_unvalidated');
    expect(r.decision_ts).toBe(hist[hist.length - 1].ts);
    expect(r.calc_version).toBe(S.SENTIMENT_V2_VERSION);
  });
  it('classifies the score on fixed bands', () => {
    expect([10, 30, 50, 60, 90].map(S.sv2Classify)).toEqual(['strongly_bearish', 'bearish', 'neutral', 'bullish', 'strongly_bullish']);
    expect(S.sv2Classify(null)).toBe(null);
  });
  it('row has 23 bound values matching the insert statement', () => {
    expect(S.sv2Row(r, { status: 'disabled', model: null }, {}, 1).length).toBe((S.SV2_INSERT_SQL.match(/\?/g) || []).length);
  });
  it('rejects out-of-range and non-numeric source readings', () => {
    expect(S.sv2ParseSources('{"a": 120, "b": -1, "c": "x", "d": 42, "e": null}')).toEqual({ d: 42 });
    expect(S.sv2ParseSources('not json')).toEqual({});
  });
});

describe('source families and redundancy', () => {
  it('aggregates fresh sources into equal-weight families', () => {
    const rows = archive(60, { src: i => ({ fng: 80, cryptonews: 60 + (i % 3), foufi: 70 + (i % 2) }) });
    const hist = S.sv2History(rows);
    const r = S.sv2Compute(hist, hist.length - 1, [], cfg);
    const fam = r.families.market_sentiment_news;
    expect(fam.sources_stale).toContain('fng');                                   // constant for >= 10 observations
    expect(fam.sources_fresh).not.toContain('fng');
    const scored = Object.values(r.families).filter(f => f.score !== null);
    const mean = scored.reduce((s, f) => s + f.score, 0) / scored.length;
    expect(r.composite_before_events).toBeCloseTo(mean, 1);
  });
  it('counts perfectly correlated sources of one family once', () => {
    const rows = archive(80, { src: (i, s) => ({ funding: 30 + i % 17, longshort: 30 + i % 17, hypefunding: 90 - (i % 13), hloi: 50 + (i % 11) }) });
    const hist = S.sv2History(rows);
    const r = S.sv2Compute(hist, hist.length - 1, [], cfg);
    const fam = r.families.derivatives_positioning;
    expect(r.redundancy.some(p => [p.a, p.b].sort().join() === 'funding,longshort' && p.same_family)).toBe(true);
    expect(fam.independent_groups).toBe(fam.sources_fresh.length - 1);
  });
  it('reports cross-family redundancy without merging families', () => {
    const rows = archive(80, { src: i => ({ nasdaq: 20 + i % 19, sp500: 20 + i % 19, gold: 20 + i % 19 }) });
    const hist = S.sv2History(rows);
    const r = S.sv2Compute(hist, hist.length - 1, [], cfg);
    const cross = r.redundancy.find(p => [p.a, p.b].includes('gold') && !p.same_family);
    expect(cross).toBeTruthy();
    expect(r.families.macroeconomic.score).not.toBe(null);
  });
});

describe('stale and missing sources', () => {
  it('never treats a stale or missing source as a fresh neutral reading', () => {
    const rows = archive(40, { src: (i, s) => { const o = { usd: 5 }; if (i === 39) { delete s.oil; } return o; } });
    rows[39].sources_json = JSON.stringify(Object.fromEntries(Object.entries(JSON.parse(rows[39].sources_json)).filter(([k]) => k !== 'oil')));
    const hist = S.sv2History(rows);
    const r = S.sv2Compute(hist, hist.length - 1, [], cfg);
    expect(r.missing.stale).toContain('usd');
    expect(r.missing.missing).toContain('oil');
    expect(r.families.macroeconomic.sources_fresh).not.toContain('usd');
    expect(r.families.macroeconomic.sources_fresh).not.toContain('oil');
    expect(r.evidence_coverage).toBeLessThan(1);
  });
  it('reports families without any observed source instead of imputing them', () => {
    const hist = S.sv2History(archive(30));
    const r = S.sv2Compute(hist, hist.length - 1, [], cfg);
    expect(r.missing.families_without_sources).toContain('market_structure_breadth');
    expect(r.families.market_structure_breadth.score).toBe(null);
  });
  it('fails honestly when no source is fresh', () => {
    const rows = archive(30, { src: () => Object.fromEntries(KEYS.map(k => [k, 50])) });
    const hist = S.sv2History(rows);
    const r = S.sv2Compute(hist, hist.length - 1, [], cfg);
    expect(r.status).toBe('FAILED');
    expect(r.sentiment_score).toBe(null);
    expect(r.degraded_reason).toBe('no_fresh_sources');
  });
});

describe('events: timestamps, deduplication, relevance, overlap', () => {
  const hist = S.sv2History(archive(300));
  const i = hist.length - 1, t = hist[i].ts;
  const ev = (o) => ({ event_id: o.id, dedup_key: o.id, correlation_key: o.corr || null, event_type: o.type || 'etf_flow_shock',
    asset: o.asset ?? 'BTC', source: 'test', source_ts: o.detected - H, detected_ts: o.detected, direction: o.dir || 'UP',
    severity: o.sev || 'HIGH', confidence: null, rule_id: 'r1', expires_ts: o.expires ?? null });
  it('uses detection time, never future detections, and drops expired or other-asset events', () => {
    const r = S.sv2Events([ev({ id: 'a', detected: t - H }), ev({ id: 'future', detected: t + H }), ev({ id: 'old', detected: t - 4 * D }),
      ev({ id: 'exp', detected: t - H, expires: t - 1 }), ev({ id: 'eth', detected: t - H, asset: 'ETH' })], hist, i, new Set(), cfg);
    expect(r.events.map(e => e.id)).toEqual(['a']);
    expect(r.excluded).toEqual({ other_asset: 1, expired: 1 });
  });
  it('deduplicates related detections into one group with persistence', () => {
    const r = S.sv2Events([ev({ id: 'a', detected: t - 3 * H, corr: 'g', sev: 'MEDIUM' }), ev({ id: 'b', detected: t - H, corr: 'g', sev: 'HIGH' })],
      hist, i, new Set(), cfg);
    expect(r.events.length).toBe(1);
    expect(r.events[0].id).toBe('b');
    expect(r.events[0].persistence).toBe(2);
    expect(r.events[0].first_detected_ts).toBe(t - 3 * H);
  });
  it('decays novelty and halves the adjustment when the family is already covered', () => {
    const e = [ev({ id: 'a', detected: t - H, type: 'etf_flow_shock' })];
    const plain = S.sv2Events(e, hist, i, new Set(), cfg);
    const overl = S.sv2Events(e, hist, i, new Set(['institutional_flows_etf']), cfg);
    expect(plain.events[0].overlaps_fresh_family).toBe(false);
    expect(overl.events[0].overlaps_fresh_family).toBe(true);
    expect(overl.adjustment).toBeCloseTo(plain.adjustment * cfg.overlapFactor, 1);   // both rounded to 0.01
    expect(Math.abs(plain.adjustment)).toBeLessThanOrEqual(cfg.eventMaxAdjust);
    expect(plain.intensity).toBeGreaterThan(0);
    expect(plain.intensity).toBeLessThanOrEqual(1);
  });
  it('labels price-derived events as consequences, not causes', () => {
    const r = S.sv2Events([ev({ id: 'm', detected: t - H, type: 'price_move' })], hist, i, new Set(), cfg);
    expect(r.events[0].role).toBe('consequence');
  });
  it('event reaction priors use only outcomes observable at decision time', () => {
    const r = S.sv2EventReactions([ev({ id: 'x', detected: t - 2 * D }), ev({ id: 'y', detected: t - 12 * H })], hist, t, cfg);
    expect(r.etf_flow_shock.n).toBe(1);
  });
});

describe('point-in-time correctness and no fabricated history', () => {
  it('a decision is unchanged by later observations and later events', () => {
    const rows = archive(300);
    const a = S.sv2History(rows.slice(0, 250));
    const b = S.sv2History(rows);
    const i = 249, t = b[i].ts;
    const later = [{ event_id: 'late', dedup_key: 'late', event_type: 'etf_flow_shock', asset: 'BTC', source: 's', source_ts: t + H,
      detected_ts: t + 2 * H, direction: 'DOWN', severity: 'CRITICAL', confidence: null, rule_id: 'r', expires_ts: null }];
    expect(S.sv2Compute(b, i, later, cfg)).toEqual(S.sv2Compute(a, i, [], cfg));
  });
  it('the predictive signal stays apart from sentiment and reports insufficient history', () => {
    const hist = S.sv2History(archive(8));
    const r = S.sv2Compute(hist, hist.length - 1, [], cfg);
    expect(r.predictive.status).toBe('insufficient_history');
    expect(r.predictive.p_up).toBe(null);
    expect(r.sentiment_score).not.toBe(null);
  });
  it('classifies regimes from point-in-time prices', () => {
    const up = S.sv2History(archive(400, { price: i => 50000 * (1 + i * 0.0006) }));
    expect(S.sv2Regime(up, up.length - 1).regime).toBe('bullish');
    const down = S.sv2History(archive(400, { price: i => 50000 * (1 - i * 0.0006) }));
    expect(S.sv2Regime(down, down.length - 1).regime).toBe('bearish');
    const short = S.sv2History(archive(20));
    expect(S.sv2Regime(short, short.length - 1).regime).toBe('insufficient_history');
  });
  it('the first cycle evaluates only the newest V1 observation; later cycles catch up without duplicates', async () => {
    const X = fresh();
    const rows = archive(400);
    const db = fakeDb({ archive: rows.slice(0, 390) });
    const r1 = await X.runSentimentV2Cycle({ ...ENABLED, DB: db }, { nowTs: rows[389].observation_ts });
    expect(r1.written).toBe(1);
    expect(db.results.map(r => r.decision_ts)).toEqual([rows[389].observation_ts]);
    const db2 = fakeDb({ archive: rows, results: db.results });
    const r2 = await X.runSentimentV2Cycle({ ...ENABLED, DB: db2 }, { nowTs: rows[399].observation_ts });
    expect(r2.written).toBe(cfg.maxCatchUp);
    expect(db2.results.length).toBe(1 + cfg.maxCatchUp);
  });
});

describe('idempotency and V1 immutability', () => {
  it('a retried cycle writes nothing new', async () => {
    const X = fresh();
    const rows = archive(200);
    const db = fakeDb({ archive: rows });
    const env = { ...ENABLED, DB: db };
    await X.runSentimentV2Cycle(env, { nowTs: rows[199].observation_ts });
    db.results.length = 0; // simulate the MAX() lookup missing a just-written row (worst case retry)
    const first = await X.runSentimentV2Cycle(env, { nowTs: rows[199].observation_ts });
    const again = await X.runSentimentV2Cycle(env, { nowTs: rows[199].observation_ts });
    expect(first.written).toBe(1);
    expect(again.written).toBe(0);
    expect(db.results.length).toBe(1);
  });
  it('writes only to sentiment_v2_results and never to a V1 or research table', async () => {
    const X = fresh();
    const rows = archive(120);
    const db = fakeDb({ archive: rows });
    await X.runSentimentV2Cycle({ ...ENABLED, DB: db }, { nowTs: rows[119].observation_ts });
    const writes = db.log.filter(sql => /^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)/i.test(sql));
    expect(writes.length).toBeGreaterThan(0);
    expect(writes.every(sql => /^INSERT OR IGNORE INTO sentiment_v2_results |^INSERT INTO sv2_ai_neurons |^UPDATE sv2_ai_neurons /.test(sql))).toBe(true);
  });
  it('no V2 function contains SQL that modifies another table', () => {
    const body = extractFunctions(...FUNCS) + extractConstants(...CONSTS);
    const targets = [...body.matchAll(/\b(INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM|REPLACE INTO|DROP TABLE|ALTER TABLE)\s+(\w+)/gi)].map(m => m[2]);
    expect(targets.length).toBeGreaterThan(0);
    expect(new Set(targets)).toEqual(new Set(['sentiment_v2_results', 'sv2_ai_neurons']));
  });
  it('does nothing when disabled', async () => {
    const db = fakeDb({ archive: archive(50) });
    const r = await fresh().runSentimentV2Cycle({ SENTIMENT_V2_ENABLED: '0', DB: db }, {});
    expect(r.skipped).toBe(true);
    expect(db.log.length).toBe(0);
  });
  it('degrades honestly when event_ledger cannot be read', async () => {
    const rows = archive(120);
    const db = fakeDb({ archive: rows, failEvents: true });
    await fresh().runSentimentV2Cycle({ ...ENABLED, DB: db }, { nowTs: rows[119].observation_ts });
    expect(db.results[0].status).toBe('DEGRADED');
    expect(db.results[0].degraded_reason).toBe('event_ledger_unavailable');
  });
});

describe('AI: validation, quota, unavailability, zero paid fallback', () => {
  const hist = S.sv2History(archive(200));
  const r = S.sv2Compute(hist, hist.length - 1, [], cfg);
  const { pkg, allowedIds } = S.sv2AiPackage(r);
  const yes = async () => true, no = async () => false;
  const good = { summary: 'Families mostly bearish.', cited_ids: [allowedIds[0]], contradictions: [], event_relevance: [], uncertainty: 'Small sample.' };
  it('accepts schema-valid output citing package identifiers only', () => {
    expect(S.sv2ValidateAiOutput(good, allowedIds).ok).toBe(true);
  });
  it('rejects invented identifiers, numeric scores, missing fields and non-objects', () => {
    expect(S.sv2ValidateAiOutput({ ...good, cited_ids: ['v1:made_up@1'] }, allowedIds).ok).toBe(false);
    expect(S.sv2ValidateAiOutput({ ...good, score: 72 }, allowedIds).ok).toBe(false);
    expect(S.sv2ValidateAiOutput({ summary: 'x' }, allowedIds).ok).toBe(false);
    expect(S.sv2ValidateAiOutput('text', allowedIds).ok).toBe(false);
    expect(S.sv2ValidateAiOutput({ ...good, event_relevance: [{ id: 'event:zz', relevance: 'high', overlap: 'none' }] }, allowedIds).ok).toBe(false);
  });
  // A ledger double: reserve() succeeds while the budget allows (same rule as SV2_AI_RESERVE_SQL), record() stores usage.
  const ledgerFor = (budgetRows = []) => ({
    rows: budgetRows,
    async accountLimitedToday() { return this.rows.some(r => r.status === 'account_limited'); },
    async reserveNeurons(budget, est) {
      const committed = this.rows.reduce((s2, r) => s2 + Math.max(r.reserved, r.actual || 0), 0);
      if (committed + est > budget) return null;
      this.rows.push({ slot: this.rows.length + 1, reserved: est, actual: null, status: 'reserved' });
      return this.rows.length;
    },
    async recordNeurons(slot, u) { Object.assign(this.rows[slot - 1], { actual: u.actual_neurons ?? null, status: u.status }); },
  });
  const GLM = '@cf/zai-org/glm-4.7-flash';
  const okResp = (obj, usage = { prompt_tokens: 900, completion_tokens: 300 }) => ({ response: JSON.stringify(obj), usage });
  it('is disabled without a model or a Neuron budget, and never calls the binding then', async () => {
    let calls = 0;
    const AI = { run: async () => { calls++; return okResp(good); } };
    expect((await S.sv2RunAi({ AI }, pkg, allowedIds, ledgerFor())).status).toBe('disabled');
    expect((await S.sv2RunAi({ AI, SENTIMENT_V2_AI_MODEL: GLM, SV2_GLM_DAILY_NEURONS: '0' }, pkg, allowedIds, ledgerFor())).status).toBe('disabled');
    expect((await S.sv2RunAi({ AI, SENTIMENT_V2_AI_MODEL: '@cf/unknown/model', SV2_GLM_DAILY_NEURONS: '500' }, pkg, allowedIds, ledgerFor())).status).toBe('unpriced_model');
    expect(calls).toBe(0);
  });
  it('enforces the daily Neuron budget, stops after the account allocation is reported exhausted, and never retries elsewhere', async () => {
    let calls = 0;
    const est = S.sv2AiEstimateNeurons(S.SV2_AI_PRICING[GLM], 5000);
    expect(est).toBeGreaterThan(S.SV2_AI_MAX_OUTPUT_TOKENS * 36400 / 1e6);   // the output cap alone is inside the bound
    const env = { SENTIMENT_V2_AI_MODEL: GLM, SV2_GLM_DAILY_NEURONS: '100', AI: { run: async () => { calls++; throw new Error('3036: You have used up your daily free allocation of 10,000 neurons'); } } };
    const led = ledgerFor();
    const first = await S.sv2RunAi(env, pkg, allowedIds, led);
    expect(first.status).toBe('account_limited');
    expect(calls).toBe(1);
    expect((await S.sv2RunAi(env, pkg, allowedIds, led)).status).toBe('quota_guard');   // no further call today
    expect(calls).toBe(1);
    const tiny = await S.sv2RunAi({ ...env, SV2_GLM_DAILY_NEURONS: '1' }, pkg, allowedIds, ledgerFor());
    expect(tiny.status).toBe('quota_guard');                                              // reservation larger than budget
    expect(calls).toBe(1);
    expect((await S.sv2RunAi({ ...env, AI: undefined }, pkg, allowedIds, ledgerFor())).status).toBe('unavailable');
  });
  it('records actual Neurons from reported token usage, and the committed total never exceeds the budget', async () => {
    const env = { SENTIMENT_V2_AI_MODEL: GLM, SV2_GLM_DAILY_NEURONS: '200', AI: { run: async () => okResp(good) } };
    const led = ledgerFor();
    let n = 0;
    for (let k = 0; k < 20; k++) if ((await S.sv2RunAi(env, pkg, allowedIds, led)).status === 'ok') n++;
    const committed = led.rows.reduce((s2, r) => s2 + Math.max(r.reserved, r.actual || 0), 0);
    expect(n).toBeGreaterThan(0);
    expect(committed).toBeLessThanOrEqual(200);
    expect(led.rows[0].actual).toBeCloseTo((900 * 5500 + 300 * 36400) / 1e6, 3);
  });
  it('accepts valid JSON from the binding and rejects malformed output, extra fields and ungrounded numbers', async () => {
    const env = { SENTIMENT_V2_AI_MODEL: GLM, SV2_GLM_DAILY_NEURONS: '1000' };
    expect((await S.sv2RunAi({ ...env, AI: { run: async () => ({ response: '```json\n' + JSON.stringify(good) + '\n```' }) } }, pkg, allowedIds, ledgerFor())).status).toBe('ok');
    expect((await S.sv2RunAi({ ...env, AI: { run: async () => ({ choices: [{ message: { content: JSON.stringify(good) } }], usage: { prompt_tokens: 1, completion_tokens: 1 } }) } }, pkg, allowedIds, ledgerFor())).status).toBe('ok');
    expect((await S.sv2RunAi({ ...env, AI: { run: async () => ({ response: 'Sentiment is 72.' }) } }, pkg, allowedIds, ledgerFor())).status).toBe('invalid_output');
    expect((await S.sv2RunAi({ ...env, AI: { run: async () => ({ response: { ...good, score: 9 } }) } }, pkg, allowedIds, ledgerFor())).status).toBe('invalid_output');
    const invented = await S.sv2RunAi({ ...env, AI: { run: async () => okResp({ ...good, summary: 'BTC will reach 123456 next week.' }) } }, pkg, allowedIds, ledgerFor());
    expect(invented.status).toBe('invalid_output');
    expect(invented.reason).toMatch(/numbers not in the evidence package: 123456/);
  });
  it('an AI failure degrades the cycle but never changes the score', async () => {
    const rows = archive(150);
    const env = { ...ENABLED, SENTIMENT_V2_AI_MODEL: GLM, SV2_GLM_DAILY_NEURONS: '500', AI: { run: async () => { throw new Error('x'); } } };
    const db = fakeDb({ archive: rows });
    await fresh().runSentimentV2Cycle({ ...env, DB: db }, { nowTs: rows[149].observation_ts });
    const off = fakeDb({ archive: rows });
    await fresh().runSentimentV2Cycle({ ...ENABLED, DB: off }, { nowTs: rows[149].observation_ts });
    expect(db.results[0].status).toBe('DEGRADED');
    expect(db.results[0].degraded_reason).toBe('ai_error');
    expect(db.results[0].sentiment_score).toBe(off.results[0].sentiment_score);
    expect(off.results[0].ai_status).toBe('disabled');
  });
  it('V2 code reaches no network host and no other AI provider', () => {
    const body = extractFunctions(...FUNCS);
    expect(body).not.toMatch(/\bfetch\s*\(/);
    expect(body).not.toMatch(/openai|anthropic|generativelanguage|googleapis|huggingface|replicate|together|groq|api[_-]?key/i);
    expect((body.match(/env\.AI\.run\(/g) || []).length).toBe(1);
  });
  it('ships in wrangler.toml with GLM named but a zero Neuron budget until staging verifies it', () => {
    const toml = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'wrangler.toml'), 'utf8');
    expect(toml).toMatch(/SENTIMENT_V2_AI_MODEL = "@cf\/zai-org\/glm-4\.7-flash"/);
    expect(toml).toMatch(/SV2_GLM_DAILY_NEURONS = "0"/);
  });
});

describe('comparison API: alignment, periods, empty history', () => {
  it('aligns V1, V2 and BTC on identical timestamps and passes V1 values through unchanged', () => {
    const v1 = [{ observation_ts: 1, score: 41.234, btc_price: 61000.5 }, { observation_ts: 2, score: 39.9, btc_price: 60900 }];
    const out = S.sv2AlignSeries(v1, [{ decision_ts: 2, sentiment_score: 47.1, status: 'OK' }], 500);
    expect(out.points).toEqual([{ ts: 1, v1: 41.234, btc: 61000.5, v2: null, v2_status: null },
      { ts: 2, v1: 39.9, btc: 60900, v2: 47.1, v2_status: 'OK' }]);
  });
  it('downsamples by keeping actual observations, including the latest', () => {
    const v1 = Array.from({ length: 1200 }, (_, i) => ({ observation_ts: i, score: i % 100, btc_price: 100 + i }));
    const out = S.sv2AlignSeries(v1, [], 500);
    expect(out.points.length).toBeLessThanOrEqual(501);
    expect(out.points[out.points.length - 1].ts).toBe(1199);
    for (const p of out.points) expect(p.v1).toBe(p.ts % 100);
  });
  it('supports exactly the five periods', () => {
    expect(Object.keys(S.SV2_PERIODS_MS)).toEqual(['24h', '7d', '30d', '90d', 'all']);
  });
  it('reports insufficient V2 history and stale series honestly when V2 has no results', async () => {
    const rows = archive(50);
    const now = rows[49].observation_ts + 10 * H;
    const out = await fresh().getSentimentV2Comparison({ DB: fakeDb({ archive: rows }) }, '7d', now);
    expect(out.points.every(p => p.v2 === null)).toBe(true);
    expect(out.series_status.v2.available).toBe(false);
    expect(out.series_status.v2.insufficient_history).toBe(true);
    expect(out.series_status.v1.stale).toBe(true);
    expect(out.v2_latest).toBe(null);
  });
  it('returns stored V2 results on their V1 timestamps within the period', async () => {
    const X = fresh();
    const rows = archive(120);
    const db = fakeDb({ archive: rows.slice(0, 118) });
    await X.runSentimentV2Cycle({ ...ENABLED, DB: db }, { nowTs: rows[117].observation_ts });
    const db2 = fakeDb({ archive: rows, results: db.results });
    await X.runSentimentV2Cycle({ ...ENABLED, DB: db2 }, { nowTs: rows[119].observation_ts });
    const out = await X.getSentimentV2Comparison({ DB: db2 }, '24h', rows[119].observation_ts + H);
    const withV2 = out.points.filter(p => p.v2 !== null);
    expect(withV2.map(p => p.ts)).toEqual([rows[117].observation_ts, rows[118].observation_ts, rows[119].observation_ts]);
    for (const p of withV2) expect(p.v1).toBe(rows.find(r => r.observation_ts === p.ts).score);
    expect(out.series_status.v2.insufficient_history).toBe(false);
    expect(out.series_status.v2.first_ts).toBe(rows[117].observation_ts);
    expect(out.v2_latest.decision_ts).toBe(rows[119].observation_ts);
    expect(out.points.length).toBe(12);
  });
  it('caches each period for five minutes', async () => {
    const X = fresh();
    const db = fakeDb({ archive: archive(20) });
    const a = await X.getSentimentV2ComparisonCached({ DB: db }, '7d', T0 + 40 * H);
    const b = await X.getSentimentV2ComparisonCached({ DB: db }, '7d', T0 + 40 * H + 60000);
    expect([a.cache, b.cache]).toEqual(['miss', 'hit']);
  });
});


describe('BTC prices from btc_data (the archive carries none)', () => {
  const rows = archive(400).map(r => ({ ...r, btc_price: null }));
  const prices = rows.map((r, i) => ({ ts: r.observation_ts - 10 * 60000, btc_price: 50000 * (1 + i * 0.0006) }));
  it('attaches only a price at or before each observation, within the tolerance', () => {
    const hist = S.sv2History(rows);
    const later = rows.map(r => ({ ts: r.observation_ts + 60000, btc_price: 1 }));
    expect(S.sv2AttachPrices(hist, later, cfg.priceToleranceMs)).toBe(399);
    expect(hist.slice(1).every(h => h.btc === 1 && h.btc_ts < h.ts)).toBe(true);
    const h2 = S.sv2History(rows);
    S.sv2AttachPrices(h2, [{ ts: rows[0].observation_ts - 4 * H, btc_price: 7 }], cfg.priceToleranceMs);
    expect(h2[0].btc).toBe(null);
  });
  it('the cycle uses btc_data for regime and analogues', async () => {
    const db = fakeDb({ archive: rows, prices });
    await fresh().runSentimentV2Cycle({ ...ENABLED, DB: db }, { nowTs: rows[399].observation_ts });
    expect(db.log.some(sql => sql.startsWith('SELECT ts, btc_price FROM btc_data'))).toBe(true);
    const hist = S.sv2History(rows);
    S.sv2AttachPrices(hist, prices, cfg.priceToleranceMs);
    expect(S.sv2Regime(hist, hist.length - 1).regime).toBe('bullish');
    expect(S.sv2Analogues(hist, hist.length - 1, cfg).status).toBe('ok');
  });
  it('the API aligns BTC to each V1 timestamp and says which price it used', async () => {
    const out = await fresh().getSentimentV2Comparison({ DB: fakeDb({ archive: rows, prices }) }, '24h', rows[399].observation_ts);
    expect(out.points.length).toBeGreaterThan(5);
    for (const p of out.points) { expect(p.btc).not.toBe(null); expect(p.btc_ts).toBeLessThanOrEqual(p.ts); }
  });
});


describe('comparison API: bounded D1 reads', () => {
  it('reads at most SV2_MAX_ROWS rows per query, even for `all`, and says so', async () => {
    const X = fresh();
    const rows = archive(X.SV2_MAX_ROWS + 400, { step: H }).map(r => ({ ...r, btc_price: null }));
    const prices = rows.map(r => ({ ts: r.observation_ts - 60000, btc_price: 1 }));
    const out = await X.getSentimentV2Comparison({ DB: fakeDb({ archive: rows, prices }) }, 'all', rows[rows.length - 1].observation_ts);
    expect(out.truncated).toBe(true);
    expect(out.truncated_from_ts).toBe(rows[400].observation_ts);
    expect(out.points[out.points.length - 1].ts).toBe(rows[rows.length - 1].observation_ts);
    expect(out.points.length).toBeLessThanOrEqual(X.SV2_MAX_POINTS + 1);
    expect(out.points.every(p => p.btc === 1)).toBe(true);
  });
  it('every API query against the archive, V2 results and btc_data carries a LIMIT', () => {
    const body = extractFunctions('getSentimentV2Comparison');
    for (const m of body.matchAll(/'(SELECT [^']+)'|"(SELECT [^"]+)"/g)) {
      const sql = m[1] || m[2];
      if (/decision_ts = \?/.test(sql)) continue;                 // unique single-row lookup
      expect(sql).not.toMatch(/COUNT\(|MIN\(|MAX\(/);              // no history-wide aggregate on the request path
      expect(sql).toMatch(/LIMIT (\?|1)$/);
    }
  });
});


describe('deployment safety and the atomic AI ceiling', () => {
  it('skips quietly, running no V2 query, until migration 0027 is applied', async () => {
    const db = fakeDb({ archive: archive(50), noTable: true });
    const r = await fresh().runSentimentV2Cycle({ ...ENABLED, DB: db }, {});
    expect(r.skipped).toBe(true);
    expect(r.reason).toMatch(/migration 0027/);
    expect(db.log.length).toBe(1);
    expect(db.results.length).toBe(0);
  });
  it('never exceeds the daily Neuron budget across overlapping and retried cycles; a reservation counts even when the call fails', async () => {
    const rows = archive(160);
    let calls = 0;
    const AI = { run: async () => { calls++; throw new Error('upstream failure'); } };
    const GLM = '@cf/zai-org/glm-4.7-flash';
    const est = 30;   // budget for exactly two reservations of this package size (checked below)
    const env = { ...ENABLED, SENTIMENT_V2_AI_MODEL: GLM, AI };
    const slots = [];
    const now = rows[159].observation_ts;
    const probe = fakeDb({ archive: rows });
    await fresh().runSentimentV2Cycle({ ...env, SV2_GLM_DAILY_NEURONS: '100000', DB: probe }, { nowTs: now });
    const one = probe.slots[0].reserved_neurons;
    expect(one).toBeGreaterThan(est);
    const budget = String(one * 2 + one / 2);
    await Promise.all([0, 1, 2].map(() => fresh().runSentimentV2Cycle({ ...env, SV2_GLM_DAILY_NEURONS: budget, DB: fakeDb({ archive: rows, slots }) }, { nowTs: now })));
    await fresh().runSentimentV2Cycle({ ...env, SV2_GLM_DAILY_NEURONS: budget, DB: fakeDb({ archive: rows, slots }) }, { nowTs: now });
    expect(slots.length).toBe(2);
    expect(calls).toBe(3);                 // 1 probe + 2 within budget
  });
  it('the reservation is one atomic conditional insert keyed by day and slot, checked against max(reserved, actual)', () => {
    expect(S.SV2_AI_RESERVE_SQL).toMatch(/^INSERT INTO sv2_ai_neurons .* SELECT .* WHERE \(SELECT COALESCE\(SUM\(MAX\(reserved_neurons, COALESCE\(actual_neurons, 0\)\)\), 0\) FROM sv2_ai_neurons WHERE day = \?\) \+ \? <= \?$/);
    const mig = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '.ai', 'migrations', '0029_sv2_evidence_and_ai_budget.sql'), 'utf8');
    expect(mig).toMatch(/PRIMARY KEY \(day, slot\)/);
  });
});


describe('BTC price age and the request-wide read bound', () => {
  it('never uses a price older than the tolerance for regime or event context', () => {
    const rows = archive(300, { price: i => 50000 + i });
    const hist = S.sv2History(rows);
    for (const h of hist.slice(0, 250)) { h.btc = null; h.btc_ts = null; }        // a long gap in prices
    const t = hist[260].ts;
    expect(S.sv2PriceAt(hist, hist[240].ts)).toBe(null);                          // inside the gap: nearest price is days older
    expect(S.sv2PriceAt(hist, t).ts).toBeLessThanOrEqual(t);
    expect(S.sv2Regime(hist, 260).regime).toBe('insufficient_history');         // no valid price 7 days back
    const ok = S.sv2History(rows);
    expect(S.sv2PriceAt(ok, ok[260].ts).price).toBe(50260);
  });
  it('an attached price keeps its own timestamp, and a stale attachment is refused at use', () => {
    const rows = archive(10).map(r => ({ ...r, btc_price: null }));
    const hist = S.sv2History(rows);
    S.sv2AttachPrices(hist, [{ ts: rows[0].observation_ts - 2 * H, btc_price: 9 }], cfg.priceToleranceMs);
    expect(hist[0].btc_ts).toBe(rows[0].observation_ts - 2 * H);
    expect(S.sv2PriceAt(hist, hist[0].ts)).toEqual({ ts: rows[0].observation_ts - 2 * H, price: 9 });
    expect(S.sv2PriceAt(hist, hist[0].ts + 90 * 60000)).toBe(null);             // 3.5 h after the price
  });
  it('a worst-case uncached request reads at most three capped queries plus two single-row lookups', async () => {
    const X = fresh();
    const n = X.SV2_MAX_ROWS + 500;
    const rows = archive(n, { step: H }).map(r => ({ ...r, btc_price: null }));
    const prices = rows.map(r => ({ ts: r.observation_ts - 60000, btc_price: 1 }));
    const results = rows.map(r => ({ key: r.observation_ts, asset: 'BTC', decision_ts: r.observation_ts, calc_version: X.SENTIMENT_V2_VERSION, status: 'OK', sentiment_score: 50 }));
    let read = 0;
    const db = fakeDb({ archive: rows, prices, results });
    const prep = db.prepare;
    db.prepare = sql => { const st = prep(sql); const all = st.all, first = st.first;
      st.all = async () => { const r = await all(); read += r.results.length; return r; };
      st.first = async () => { const r = await first(); read += r ? 1 : 0; return r; }; return st; };
    const out = await X.getSentimentV2Comparison({ DB: db }, 'all', rows[n - 1].observation_ts);
    expect(out.truncated).toBe(true);
    expect(read).toBeLessThanOrEqual(2 * X.SV2_MAX_ROWS + X.SV2_MAX_PRICE_ROWS + 3);
  });
});


describe('dense price lookups', () => {
  it('uses btc_data prices between observations, still at-or-before and within the tolerance', () => {
    const rows = archive(200, { step: 8 * H }).map(r => ({ ...r, btc_price: null }));   // sparse observations
    const prices = [];
    for (let ts = rows[0].observation_ts - 4 * H; ts <= rows[199].observation_ts; ts += H) prices.push({ ts, btc_price: 1000 + (ts - T0) / H });
    const hist = S.sv2History(rows);
    S.sv2AttachPrices(hist, prices, cfg.priceToleranceMs);
    const q = rows[100].observation_ts - 5 * H - 1;                                      // between observations
    const p = S.sv2PriceAt(hist, q);
    expect(p.ts).toBeLessThanOrEqual(q);
    expect(q - p.ts).toBeLessThan(H);
    expect(S.sv2Regime(hist, 199).regime).not.toBe('insufficient_history');
    expect(S.sv2PriceAt(hist, rows[0].observation_ts - 8 * H)).toBe(null);             // before any price
  });
});


describe('dense btc_data under the cap', () => {
  it('covers every returned V1 point when btc_data is ~2.3x denser than V1, and flags truncation beyond the cap', async () => {
    const X = fresh();
    const rows = archive(X.SV2_MAX_ROWS, { step: 2 * H }).map(r => ({ ...r, btc_price: null }));
    const dense = [];
    for (let ts = rows[0].observation_ts - H; ts <= rows[rows.length - 1].observation_ts; ts += 52 * 60000) dense.push({ ts, btc_price: 5 });
    expect(dense.length / rows.length).toBeGreaterThan(2.2);
    const out = await X.getSentimentV2Comparison({ DB: fakeDb({ archive: rows, prices: dense }) }, 'all', rows[rows.length - 1].observation_ts);
    expect(out.btc_truncated).toBe(false);
    expect(out.points.every(p => p.btc === 5)).toBe(true);
    const tooDense = [];
    for (let ts = rows[0].observation_ts - H; ts <= rows[rows.length - 1].observation_ts; ts += 10 * 60000) tooDense.push({ ts, btc_price: 5 });
    const out2 = await X.getSentimentV2Comparison({ DB: fakeDb({ archive: rows, prices: tooDense }) }, 'all', rows[rows.length - 1].observation_ts);
    expect(out2.btc_truncated).toBe(true);
    expect(out2.points.filter(p => p.btc === null).every(p => p.ts < out2.btc_from_ts)).toBe(true);
  });
});


describe('cycle input caps', () => {
  it('reads prices and events newest-first and degrades, saying why, when a cap is reached', async () => {
    const rows = archive(120);
    const t = rows[119].observation_ts;
    const events = Array.from({ length: 2100 }, (_, k) => ({ event_id: 'e' + k, dedup_key: 'e' + k, correlation_key: null, event_type: 'etf_flow_shock',
      asset: 'BTC', source: 's', source_ts: t - 1000 - k * 60000, detected_ts: t - k * 60000, direction: 'UP', severity: 'LOW', confidence: null, rule_id: 'r', expires_ts: null }));
    const db = fakeDb({ archive: rows, events });
    await fresh().runSentimentV2Cycle({ ...ENABLED, DB: db }, { nowTs: t });
    expect(db.results[0].status).toBe('DEGRADED');
    expect(db.results[0].degraded_reason).toBe('inputs_truncated');
    const ok = fakeDb({ archive: rows, events: events.slice(0, 50) });
    await fresh().runSentimentV2Cycle({ ...ENABLED, DB: ok }, { nowTs: t });
    expect(ok.results[0].status).toBe('OK');
  });
  it('every cycle query that can return many rows is bounded and newest-first', () => {
    const body = extractFunctions('runSentimentV2Cycle');
    expect(body).toMatch(/FROM btc_data WHERE ts >= \? AND ts <= \? ORDER BY ts DESC LIMIT \?/);
    expect(body).toMatch(/FROM event_ledger WHERE detected_ts >= \? AND detected_ts <= \? ORDER BY detected_ts DESC LIMIT \?/);
  });
});


describe('comparison route wiring', () => {
  it('the route only uses identifiers its enclosing handler defines (regression: ctx is not a parameter there)', () => {
    const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'worker.js'), 'utf8');
    const start = src.indexOf("url.pathname === '/api/sentiment-v2/comparison'");
    const end = src.indexOf("url.pathname === '/prediction-freshness'", start);
    expect(start).toBeGreaterThan(0);
    const block = src.slice(start, end);
    const head = src.lastIndexOf('async fetch(', start);
    const signature = src.slice(head, src.indexOf(')', head) + 1);
    if (!/\bctx\b/.test(signature)) expect(block).not.toMatch(/\bctx\b/);
    expect(block).toMatch(/await edge\.put\(/);
  });
});
