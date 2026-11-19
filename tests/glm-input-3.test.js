// glm-input-3 (owner data-quality instruction 2026-11-19): V1 stays read-only; the V2 adapter validates every record,
// excludes invalid ones with reasons, flags stale/degraded ones, reports V1 source failures, and summarises each input
// group so an incomplete package is never presented as complete. Production findings encoded as fixtures:
// 29/207 Foufi rows with a NULL summary (transcript error), 2 'unstructured' rows, 'global' failing 44/44 slots,
// 5 sources permanently 'unavailable', composites below floor / completeness < 0.5.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { makeD1 } from './replay/d1-sqlite.mjs';
import { CUT, H, V1_DDL, V2_DDL, seed, LEDGER, mig } from './helpers/glm-fixture.js';
import { readGlmInputs, normV1Foufi, normV1SourceObs, normV1Composite, normCollector, normV1Catalysts, v1SourceStatusSummary } from '../glm/adapters.js';
import { buildPackage, inputQuality, GLM_REQUIRED_GROUPS } from '../glm/package.js';
import { glmDailySignalRun } from '../glm/runner.js';
import { GLM_MODEL } from '../glm/contract.js';

let d1;
beforeEach(() => { d1 = makeD1([...V1_DDL, ...V2_DDL]); seed(d1.db); });
const pkgOf = async (ledger = LEDGER, db = d1) => buildPackage({ raw: await readGlmInputs(db.binding, CUT), cutoffTs: CUT, chronosLedger: ledger, baseline: null });

describe('record validation (V2 adapter only)', () => {
  const foufi = (o) => ({ video_id: 'v', published_ts: CUT - 5 * H, fetched_ts: CUT - 4 * H, url: 'https://youtube.com/watch?v=v', transcript_status: 'ok', ...o });
  it.each([
    ['NULL summary after a transcript error', { summary_json: null, transcript_status: 'error', error_message: 'no captions' }, 'invalid', 'summary_missing:error'],
    ['malformed JSON', { summary_json: '{bad' }, 'invalid', 'summary_malformed'],
    ['unstructured (no coins)', { summary_json: '{"market_summary":"x"}', transcript_status: 'unstructured' }, 'invalid', 'summary_unstructured'],
    ['BTC score out of [-2, 2]', { summary_json: '{"coins":{"BTC":{"score":7}}}' }, 'invalid', 'btc_score_out_of_range'],
    ['BTC score missing', { summary_json: '{"coins":{"ETH":{"score":1}}}' }, 'degraded', 'btc_score_missing'],
    ['valid', { summary_json: '{"coins":{"BTC":{"score":-1,"note":"x"}}}' }, 'valid', undefined],
  ])('Foufi %s', (_n, o, status, issue) => {
    const items = normV1Foufi([foufi(o)], CUT);
    const g = items.find((i) => i.provenance_class === 'gemini_interpretation');
    expect(g.quality.status).toBe(status);
    if (issue) expect(g.quality.issues).toContain(issue);
    expect(items.find((i) => i.provenance_class === 'original_source').quality.status).toBe('valid');   // the video itself exists
  });

  it('V1 source observations: out-of-range invalid, >36 h old stale, observed after fetch flagged', () => {
    const [bad] = normV1SourceObs([{ slot_ts: CUT - H, source_id: 'fng', status: 'ok', score: 140, observed_at: CUT - H, fetched_at: CUT - H }]);
    const [old] = normV1SourceObs([{ slot_ts: CUT - H, source_id: 'ninemag', status: 'ok', score: 50, observed_at: CUT - 40 * H, fetched_at: CUT - H }]);
    const [fut] = normV1SourceObs([{ slot_ts: CUT - H, source_id: 'fng', status: 'ok', score: 50, observed_at: CUT, fetched_at: CUT - H }]);
    expect(bad.quality).toEqual({ status: 'invalid', issues: ['score_out_of_range'] });
    expect(old.quality).toEqual({ status: 'stale', issues: ['observation_older_than_36h'] });
    expect(fut.quality).toEqual({ status: 'degraded', issues: ['observed_after_fetch'] });
  });

  it('V1 source failures are reported, not silently dropped', () => {
    const s = v1SourceStatusSummary([
      { slot_ts: CUT - H, source_id: 'fng', status: 'ok' }, { slot_ts: CUT - H, source_id: 'global', status: 'error' },
      { slot_ts: CUT - H, source_id: 'nasdaq', status: 'unavailable' }, { slot_ts: CUT - H, source_id: 'sp500', status: 'unavailable' },
    ]);
    expect(s.by_status).toEqual({ ok: ['fng'], error: ['global'], unavailable: ['nasdaq', 'sp500'] });
  });

  it('composites below V1 floor / low completeness / malformed provenance are degraded; out of range invalid', () => {
    const [a] = normV1Composite([{ slot_ts: CUT - H, computed_ts: CUT - H, composite: 55, completeness: 0.47, below_floor: 1, provenance_json: '{x' }]);
    expect(a.quality.status).toBe('degraded');
    expect(a.quality.issues).toEqual(['provenance_malformed', 'below_v1_floor', 'low_completeness']);
    const [b] = normV1Composite([{ slot_ts: CUT - H, computed_ts: CUT - H, composite: 101, provenance_json: '{}' }]);
    expect(b.quality.status).toBe('invalid');
  });

  it('collector evidence without title or URL is invalid; catalyst without an original is degraded', () => {
    const [e] = normCollector([{ evidence_id: 'x', source_id: 's', canonical_url: 'not a url', title: '', retrieved_ts: CUT - H }]);
    expect(e.quality).toEqual({ status: 'invalid', issues: ['missing_title', 'missing_or_invalid_url'] });
    const c = normV1Catalysts([{ id: 9, ts: CUT - H, coin: 'BTC', source_url: null, investigation_id: 'inv' }], CUT);
    expect(c[0].quality).toEqual({ status: 'degraded', issues: ['no_original_reference'] });
  });
});

describe('package: invalid never becomes evidence; quality is explicit', () => {
  it('an invalid Gemini summary is excluded with its reason; the stale Gemini daily item is kept but flagged', async () => {
    d1.db.prepare("INSERT INTO foufi_digest VALUES ('vbad', ?, 't', 'https://youtube.com/watch?v=vbad', 'error', NULL, NULL, ?, 'daily', NULL, 'no captions')").run(CUT - 2 * H, CUT - H);
    const p = await pkgOf();
    expect(p.items.some((i) => i.ref === 'gem:foufi:vbad')).toBe(false);
    expect(p.excluded).toContainEqual(expect.objectContaining({ ref: 'gem:foufi:vbad', reason: 'invalid:summary_missing:error' }));
    expect(p.quality.groups.v1.invalid).toBeGreaterThanOrEqual(1);
    expect(p.quality.degraded_groups).toContain('v1');
    const daily = JSON.parse(p.prompt.user.slice(6)).items.find((i) => i.ref === 'gem:daily:1');
    expect(daily.quality).toEqual({ status: 'stale', issues: ['older_than_24h'] });
  });

  it('header.input_quality reports package status, groups and failing V1 sources; it is part of the hash', async () => {
    d1.db.prepare("UPDATE server_source_observations SET status = 'unavailable', score = NULL WHERE source_id = 'funding'").run();
    const p = await pkgOf();
    const h = JSON.parse(p.prompt.user.slice(6)).header.input_quality;
    expect(h.v1_sources_not_ok).toEqual({ error: ['onchain'], unavailable: ['funding'] });
    expect(['complete', 'degraded']).toContain(h.package_status);
    for (const g of GLM_REQUIRED_GROUPS) expect(p.quality.groups[g]).toBeTruthy();
    const q = await pkgOf();
    expect(q.package_hash).toBe(p.package_hash);
    expect(p.prompt.bytes).toBeLessThanOrEqual(24 * 1024);
  });

  it('missing forecasts and V1 -> insufficient; partial availability -> degraded; nothing claims completeness', () => {
    const base = { raw: { errors: {} }, trimmedItems: [], excluded: [], forecasts: {}, v1Sources: null };
    const mk = (tables) => tables.map((t) => ({ table: t, quality: { status: 'valid' } }));
    expect(inputQuality({ ...base, kept: mk(['sv2_evidence', 'btc_data']) }).package_status).toBe('insufficient');
    expect(inputQuality({ ...base, kept: mk(['history', 'sv2_evidence', 'btc_data']) }).package_status).toBe('insufficient');   // no model forecast
    const deg = inputQuality({ ...base, kept: mk(['history', 'sv2_evidence', 'btc_data', 'experiment_4_timesfm']) });
    expect([deg.package_status, deg.missing_groups]).toEqual(['degraded', ['chronos']]);
    const ok = inputQuality({ ...base, kept: mk(['history', 'sv2_evidence', 'btc_data', 'experiment_4_timesfm', 'research-data/exp008-chronos']) });
    expect(ok.package_status).toBe('complete');
    const err = inputQuality({ ...base, raw: { errors: { v1_foufi: 'missing_table:foufi_digest' } }, kept: mk(['history', 'sv2_evidence', 'btc_data', 'experiment_4_timesfm', 'research-data/exp008-chronos']) });
    expect([err.package_status, err.degraded_groups, err.groups.v1.read_errors]).toEqual(['degraded', ['v1'], ['v1_foufi:missing_table']]);
  });
});

describe('runner: insufficient package -> no model call, NO_SIGNAL with reason', () => {
  it('staging-like database (no V1 tables, no TimesFM) never calls the model even with inference on', async () => {
    const stg = makeD1([...V2_DDL, mig('0031_glm_signal.sql'), 'CREATE TABLE prediction_snapshots (coin TEXT, horizon_hours INTEGER, computed_ts INTEGER, prediction_ts INTEGER, body_json TEXT, PRIMARY KEY (coin, horizon_hours))']);
    const ai = { run: vi.fn() };
    const env = { DB: stg.binding, AI: ai, GLM_SIGNAL_ENABLED: '1', GLM_INFERENCE_ENABLED: '1', SENTIMENT_V2_AI_MODEL: GLM_MODEL, SV2_GLM_DAILY_NEURONS: '300' };
    const r = await glmDailySignalRun(env, { budget: 300, pricing: {}, reserveSql: 'x', fetchImpl: async () => ({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(LEDGER) }) }, { scheduledTs: CUT, now: () => CUT + 30e3 });
    expect(r.status).toBe('insufficient_evidence');
    expect(ai.run).not.toHaveBeenCalled();
    const run = stg.db.prepare('SELECT reason, usage_json FROM glm_runs').get();
    expect(run.reason).toMatch(/^package_insufficient: missing /);
    expect(JSON.parse(run.usage_json).quality.package_status).toBe('insufficient');
    expect(stg.db.prepare("SELECT direction, reasons_json FROM glm_signals WHERE horizon_hours = 24").get()).toEqual({ direction: 'NO_SIGNAL', reasons_json: '["insufficient_evidence"]' });
  });
});
