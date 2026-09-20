import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

// Research Lab (PR-7): read-only display layer over research_events /
// research_event_evidence / btc_data / history. Every helper here is
// SELECT-only against an injected fake D1 -- these tests exist
// specifically to prove that (never a .run()/.exec() call anywhere),
// and to prove the empty/populated/malformed-input behaviors the
// build's data-integrity requirement calls for (no invented sample
// data, ever -- honest "no rows" responses instead).
describe('Research Lab — read-only research API helpers', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(
      extractFunctions('computeExperiment4TimesFmLiveFields', 'getResearchLabRegistry') + '\n' +
      extractConstants('SOURCE_TOPIC_AFFINITY_DISPLAY', 'REACTION_HORIZONS_MS',
        'REACTION_GOOD_QUALITY_FRACTION', 'REACTION_APPROXIMATE_QUALITY_FRACTION', 'BTC_SERIES_WINDOW_MS',
        'REQUIRED_SAMPLE_FALLBACK', 'LIVE_METRIC_PROVIDERS') + '\n' +
      extractFunctions('resolveBtcReactionAtHorizon', 'getResearchLabDashboard', 'getResearchLabEvents',
        'getResearchLabEventDetail', 'getResearchLabSources', 'getResearchLabPipelineHealth')
    );
  });

  // A minimal fake D1 that records every SQL string + bound args issued,
  // and never implements anything but prepare/bind/first/all -- proving
  // by construction that nothing under test can call a write method
  // that doesn't exist on this fake.
  function makeDb(responses) {
    const calls = [];
    let callIndex = 0;
    return {
      calls,
      prepare(sql) {
        const thisCallIndex = callIndex++;
        calls.push({ sql, args: null });
        const respond = () => {
          const r = responses[thisCallIndex];
          if (r === undefined) throw new Error(`No fake response configured for call #${thisCallIndex}: ${sql}`);
          return r;
        };
        return {
          bind(...args) {
            calls[thisCallIndex].args = args;
            return { first: async () => respond().first, all: async () => respond().all };
          },
          first: async () => respond().first,
          all: async () => respond().all,
        };
      },
    };
  }

  describe('getResearchLabDashboard', () => {
    it('empty tables: honest zeros/nulls, never fabricated sample data', async () => {
      const db = makeDb([
        { first: null }, // btc_data latest
        { first: null }, // history latest
        { first: { n: 0 } }, // events count
        { first: { n: 0 } }, // evidence count
        { first: { latest: null } }, // latest evidence ts
        { all: { results: [] } }, // recent events
        { all: { results: [] } }, // btc price series
      ]);
      const result = await scope.getResearchLabDashboard({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.btc_latest).toBeNull();
      expect(result.btc_price_series).toEqual([]);
      expect(result.v1_composite_latest).toBeNull();
      expect(result.research_events_count).toBe(0);
      expect(result.research_event_evidence_count).toBe(0);
      expect(result.latest_evidence_collection_ts).toBeNull();
      expect(result.recent_events).toEqual([]);
      // Scheduled-run status is honestly UNKNOWN, never a fabricated
      // "success"/"running" guess.
      expect(result.latest_scheduled_run_status).toMatch(/UNKNOWN/);
    });

    it('populated tables: real values pass through unmodified', async () => {
      const seriesRows = [{ ts: 900, btc_price: 49000 }, { ts: 1000, btc_price: 50000 }];
      const db = makeDb([
        { first: { ts: 1000, btc_price: 50000 } },
        { first: { ts: 900, score: 55 } },
        { first: { n: 3 } },
        { first: { n: 0 } },
        { first: { latest: null } },
        { all: { results: [{ event_id: 1, event_ts: 1000, category: 'LARGE_MOVE', direction: 'UP', evidence_count: 0 }] } },
        { all: { results: seriesRows } },
      ]);
      const result = await scope.getResearchLabDashboard({ DB: db });
      expect(result.btc_latest).toEqual({ ts: 1000, btc_price: 50000 });
      expect(result.btc_price_series).toEqual(seriesRows);
      expect(result.v1_composite_latest).toEqual({ ts: 900, v1_composite: 55 });
      expect(result.research_events_count).toBe(3);
      expect(result.recent_events).toHaveLength(1);
    });

    it('never issues a write-shaped query (no INSERT/UPDATE/DELETE anywhere)', async () => {
      const db = makeDb([
        { first: null }, { first: null }, { first: { n: 0 } }, { first: { n: 0 } },
        { first: { latest: null } }, { all: { results: [] } }, { all: { results: [] } },
      ]);
      await scope.getResearchLabDashboard({ DB: db });
      for (const call of db.calls) {
        expect(call.sql).not.toMatch(/INSERT|UPDATE|DELETE/i);
        expect(call.sql).toMatch(/^SELECT/i);
      }
    });

    it('BTC price series query is bounded to BTC_SERIES_WINDOW_MS, never an unbounded full-table scan', async () => {
      const db = makeDb([
        { first: null }, { first: null }, { first: { n: 0 } }, { first: { n: 0 } },
        { first: { latest: null } }, { all: { results: [] } }, { all: { results: [] } },
      ]);
      await scope.getResearchLabDashboard({ DB: db });
      const seriesQuery = db.calls.find((c) => /FROM btc_data WHERE ts >=/.test(c.sql));
      expect(seriesQuery).toBeTruthy();
      expect(seriesQuery.sql).toContain(String(scope.BTC_SERIES_WINDOW_MS));
      expect(seriesQuery.sql).toMatch(/ORDER BY ts ASC/);
    });
  });

  describe('getResearchLabEvents', () => {
    it('empty research_events: total 0, empty list, not an error', async () => {
      const db = makeDb([{ first: { n: 0 } }, { all: { results: [] } }]);
      const result = await scope.getResearchLabEvents({ DB: db }, 50, 0);
      expect(result.ok).toBe(true);
      expect(result.total).toBe(0);
      expect(result.events).toEqual([]);
    });

    it('populated: returns rows with the requested columns plus evidence_count', async () => {
      const db = makeDb([
        { first: { n: 2 } },
        { all: { results: [
          { event_id: 1, event_ts: 100, detection_ts: 200, category: 'LARGE_MOVE', direction: 'UP', intensity: 5.0, trigger_metric: 'm', trigger_threshold: 4.0, trigger_version: 'v1', evidence_count: 2 },
        ] } },
      ]);
      const result = await scope.getResearchLabEvents({ DB: db }, 50, 0);
      expect(result.events[0].evidence_count).toBe(2);
      expect(result.events[0].trigger_version).toBe('v1');
    });

    it('limit/offset are bound as query parameters, not string-interpolated', async () => {
      const db = makeDb([{ first: { n: 0 } }, { all: { results: [] } }]);
      await scope.getResearchLabEvents({ DB: db }, 50, 10);
      const rowQuery = db.calls.find((c) => /ORDER BY re\.event_ts DESC LIMIT \? OFFSET \?/.test(c.sql));
      expect(rowQuery).toBeTruthy();
      expect(rowQuery.args).toEqual([50, 10]);
    });
  });

  describe('getResearchLabEventDetail', () => {
    it('event not found: explicit ok:false, not a thrown error or fabricated row', async () => {
      const db = makeDb([{ first: null }]);
      const result = await scope.getResearchLabEventDetail({ DB: db }, 999);
      expect(result.ok).toBe(false);
      expect(result.error).toBe('event_not_found');
    });

    it('populated event with evidence: relationship between event and its evidence rows holds', async () => {
      const db = makeDb([
        { first: { event_id: 1, fingerprint: 'fp', event_ts: 1000000, detection_ts: 2000000, category: 'LARGE_MOVE', direction: 'UP', intensity: 5.0, available_before_prediction: 1, is_post_event_analysis: 0, trigger_metric: 'm', trigger_threshold: 4.0, trigger_version: 'v1' } },
        { all: { results: [{ evidence_id: 1, feed_url: 'https://x', article_url: 'https://y', publisher: 'X', publication_ts: 999000, collection_ts: 1000500, headline: 'H', keyword_score: null, evidence_relation: 'PRE_EVENT', content_hash: 'abc' }] } },
        { first: { ts: 999900, btc_price: 100 } }, // anchor
        { first: { ts: 1021600000, btc_price: 105 } }, // 6h
        { first: null }, // 12h -- NO_DATA
        { first: null }, // 24h -- NO_DATA
      ]);
      const result = await scope.getResearchLabEventDetail({ DB: db }, 1);
      expect(result.ok).toBe(true);
      expect(result.event.event_id).toBe(1);
      expect(result.evidence).toHaveLength(1);
      expect(result.evidence[0].evidence_relation).toBe('PRE_EVENT');
      expect(result.btc_reaction['6h'].status).toBe('OK');
      expect(result.btc_reaction['6h'].return_pct).toBeCloseTo(5.0, 5);
      expect(result.btc_reaction['12h'].status).toBe('NO_DATA');
      expect(result.btc_reaction['24h'].status).toBe('NO_DATA');
    });

    it('event with no evidence: evidence array is empty, not omitted or null', async () => {
      const db = makeDb([
        { first: { event_id: 2, event_ts: 1000, category: 'LARGE_MOVE' } },
        { all: { results: [] } },
        { first: null }, // no anchor -- INSUFFICIENT_EVIDENCE for reaction
        { first: null }, { first: null }, { first: null },
      ]);
      const result = await scope.getResearchLabEventDetail({ DB: db }, 2);
      expect(result.evidence).toEqual([]);
      expect(result.btc_reaction['6h'].status).toBe('NO_DATA');
    });

    it('rejects a non-integer event_id upstream (handled by the route, not this helper) -- helper itself just queries with whatever id it is given', async () => {
      const db = makeDb([{ first: null }]);
      const result = await scope.getResearchLabEventDetail({ DB: db }, NaN);
      expect(result.ok).toBe(false);
    });
  });

  describe('getResearchLabSources', () => {
    it('no history rows: empty source list, not an error, not invented keys', async () => {
      const db = makeDb([{ all: { results: [] } }]);
      const result = await scope.getResearchLabSources({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.sources).toEqual([]);
    });

    it('never ranks, scores, or recommends -- only affinity_status and an honest INSUFFICIENT_EVIDENCE relationship', async () => {
      const db = makeDb([
        { all: { results: [{ sources_json: JSON.stringify({ geopolitics: 50, fng: 60 }) }] } },
      ]);
      const result = await scope.getResearchLabSources({ DB: db });
      const byKey = Object.fromEntries(result.sources.map((s) => [s.source_key, s]));
      expect(byKey.geopolitics.affinity_status).toBe('DIRECT_TOPIC_RELEVANCE');
      expect(byKey.fng.affinity_status).toBe('NO_DIRECT_TOPIC_AFFINITY');
      for (const s of result.sources) {
        expect(s.observed_event_source_relationship).toBe('INSUFFICIENT_EVIDENCE');
        expect(s).not.toHaveProperty('rank');
        expect(s).not.toHaveProperty('score');
        expect(s).not.toHaveProperty('recommended_weight');
      }
    });

    it('malformed sources_json in one row does not abort the whole listing', async () => {
      const db = makeDb([
        { all: { results: [
          { sources_json: 'not valid json' },
          { sources_json: JSON.stringify({ regulatory: 10 }) },
        ] } },
      ]);
      const result = await scope.getResearchLabSources({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.sources.map((s) => s.source_key)).toEqual(['regulatory']);
    });
  });

  describe('getResearchLabPipelineHealth', () => {
    it('empty tables: zero counts, empty lists', async () => {
      const db = makeDb([
        { first: { n: 0 } }, { first: { n: 0 } }, { first: { latest: null } },
        { all: { results: [] } }, { all: { results: [] } },
      ]);
      const result = await scope.getResearchLabPipelineHealth({ DB: db });
      expect(result.research_events_count).toBe(0);
      expect(result.events_without_evidence).toEqual([]);
      expect(result.evidence_by_publisher).toEqual([]);
      expect(result.feed_pipeline_status).toMatch(/UNKNOWN/);
    });

    it('populated: events-without-evidence and evidence-by-publisher pass through', async () => {
      const db = makeDb([
        { first: { n: 5 } }, { first: { n: 3 } }, { first: { latest: 123456 } },
        { all: { results: [{ event_id: 4, fingerprint: 'fp4', event_ts: 1000, category: 'LARGE_MOVE' }] } },
        { all: { results: [{ publisher: 'CoinDesk', n: 3 }] } },
      ]);
      const result = await scope.getResearchLabPipelineHealth({ DB: db });
      expect(result.events_without_evidence).toHaveLength(1);
      expect(result.evidence_by_publisher).toEqual([{ publisher: 'CoinDesk', n: 3 }]);
    });

    it('the events-without-evidence query never writes, and matches the NOT EXISTS shape', async () => {
      const db = makeDb([
        { first: { n: 0 } }, { first: { n: 0 } }, { first: { latest: null } },
        { all: { results: [] } }, { all: { results: [] } },
      ]);
      await scope.getResearchLabPipelineHealth({ DB: db });
      const q = db.calls.find((c) => /NOT EXISTS/.test(c.sql));
      expect(q).toBeTruthy();
      expect(q.sql).toMatch(/^SELECT/i);
      expect(q.sql).not.toMatch(/INSERT|UPDATE|DELETE/i);
    });
  });

  describe('getResearchLabRegistry — Experiment Registry (Program Foundation)', () => {
    function registryRow(overrides) {
      return {
        experiment_id: 'EXP-999', title: 't', research_question: 'q', purpose: 'p',
        experiment_type: 'TYPE_2', expected_result: 'e', success_criterion: 's',
        start_date: null, target_date: null, status: 'PROPOSED', baseline: 'b',
        required_sample: null, conclusion: null, next_action: 'n', github_refs: null,
        data_source_table: null, created_ts: 1, updated_ts: 1,
        ...overrides,
      };
    }

    it('a row with no data_source_table gets NOT_STARTED/NOT_AVAILABLE/UNKNOWN, never fabricated data', async () => {
      const db = makeDb([{ all: { results: [registryRow({})] } }]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.experiments).toHaveLength(1);
      const exp = result.experiments[0];
      expect(exp.current_sample_size).toBe('NOT_STARTED');
      expect(exp.current_measured_result).toBe('NOT_AVAILABLE');
      expect(exp.oos_result).toBe('NOT_AVAILABLE');
      expect(exp.confidence_evidence_maturity).toBe('UNKNOWN');
      expect(exp.last_updated).toBe(1); // falls back to the row's own updated_ts
    });

    it('a live-linked row (EXP-004) below its required_sample reports INSUFFICIENT_SAMPLE, not a skill claim', async () => {
      const db = makeDb([
        { all: { results: [registryRow({ experiment_id: 'EXP-004', data_source_table: 'experiment_4_timesfm', required_sample: 30, updated_ts: 1 })] } },
        // computeExperiment4TimesFmLiveFields's own single D1 call:
        { all: { results: [
          { horizon_hours: 12, total: 15, resolved: 14, correct: 7, latest_activity_ts: 1789819245336 },
          { horizon_hours: 24, total: 15, resolved: 14, correct: 5, latest_activity_ts: 1789819245336 },
        ] } },
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      const exp = result.experiments[0];
      expect(exp.current_sample_size.total_resolved).toBe(28);
      expect(exp.current_measured_result.combined_correct_of_resolved).toBe('12/28');
      // 14 resolved per horizon < required_sample 30 -> gated, exactly the
      // same "do not draw conclusions from current sample size" rule this
      // session's own audits already established for EXP-004 by hand.
      expect(exp.oos_result).toBe('INSUFFICIENT_SAMPLE');
      expect(exp.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
      expect(exp.last_updated).toBe(1789819245336);
    });

    it('a live-linked row that clears its required_sample reports a real oos_result object, not a string', async () => {
      const db = makeDb([
        { all: { results: [registryRow({ experiment_id: 'EXP-004', data_source_table: 'experiment_4_timesfm', required_sample: 5, updated_ts: 1 })] } },
        { all: { results: [
          { horizon_hours: 12, total: 10, resolved: 10, correct: 6, latest_activity_ts: 555 },
          { horizon_hours: 24, total: 10, resolved: 8, correct: 4, latest_activity_ts: 555 },
        ] } },
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      const exp = result.experiments[0];
      expect(exp.confidence_evidence_maturity).toBe('ACCUMULATING');
      expect(exp.oos_result).not.toBe('INSUFFICIENT_SAMPLE');
      expect(exp.oos_result.combined_correct_of_resolved).toBe('10/18');
    });

    it('an unknown data_source_table (no provider registered) falls back to the static NOT_STARTED shape, never throws', async () => {
      const db = makeDb([{ all: { results: [registryRow({ data_source_table: 'some_future_table' })] } }]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.experiments[0].current_sample_size).toBe('NOT_STARTED');
    });

    it('every D1 call across the registry path is SELECT-only, never a write', async () => {
      const db = makeDb([
        { all: { results: [registryRow({ experiment_id: 'EXP-004', data_source_table: 'experiment_4_timesfm', required_sample: 30 })] } },
        { all: { results: [] } },
      ]);
      await scope.getResearchLabRegistry({ DB: db });
      for (const call of db.calls) {
        expect(call.sql).toMatch(/^SELECT/i);
        // Word-boundary write-statement shapes only -- a plain substring
        // check would false-positive on the legitimate column name
        // `updated_ts` (contains "UPDATE").
        expect(call.sql).not.toMatch(/\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b/i);
      }
    });

    it('multiple registry rows are all returned, in the order the (already ORDER BY-sorted) query provides', async () => {
      const db = makeDb([
        { all: { results: [
          registryRow({ experiment_id: 'EXP-005' }),
          registryRow({ experiment_id: 'EXP-006' }),
          registryRow({ experiment_id: 'EXP-007' }),
        ] } },
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.experiments.map((e) => e.experiment_id)).toEqual(['EXP-005', 'EXP-006', 'EXP-007']);
    });

    it('empty registry table returns an empty list, never fabricated placeholder experiments', async () => {
      const db = makeDb([{ all: { results: [] } }]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result).toEqual({ ok: true, experiments: [] });
    });

    it('a throwing EXP-004 provider degrades ONLY that experiment -- the registry response still succeeds and other experiments are still returned (adversarial-audit fix)', async () => {
      // Only ONE response is supplied for TWO expected D1 calls (the
      // registry SELECT, then EXP-004's own live-metric query) --
      // makeDb's own fake throws "No fake response configured" on the
      // second, unconfigured call, exactly simulating a real D1/provider
      // failure without needing a second fake implementation.
      const db = makeDb([
        { all: { results: [
          registryRow({ experiment_id: 'EXP-004', data_source_table: 'experiment_4_timesfm', required_sample: 30, updated_ts: 42 }),
          registryRow({ experiment_id: 'EXP-005' }),
        ] } },
        // deliberately no second response -> the live provider throws
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.ok).toBe(true); // whole-endpoint success preserved
      expect(result.experiments).toHaveLength(2); // EXP-005 still present
      const exp004 = result.experiments.find((e) => e.experiment_id === 'EXP-004');
      const exp005 = result.experiments.find((e) => e.experiment_id === 'EXP-005');
      // Degraded, truthful, and NOT "NOT_STARTED" -- EXP-004 has genuinely
      // started; the honest claim is "could not be computed right now".
      expect(exp004.current_sample_size).toBe('NOT_AVAILABLE');
      expect(exp004.current_measured_result).toBe('NOT_AVAILABLE');
      expect(exp004.oos_result).toBe('NOT_AVAILABLE');
      expect(exp004.confidence_evidence_maturity).toBe('UNKNOWN');
      expect(exp004.last_updated).toBe(42); // falls back to the row's own metadata, never fabricated
      // EXP-005 (no data_source_table at all) is completely unaffected by
      // EXP-004's provider throwing.
      expect(exp005.current_sample_size).toBe('NOT_STARTED');
    });

    it('EXP-004 succeeding is unaffected by the try/catch -- normal live metrics still flow through unchanged', async () => {
      const db = makeDb([
        { all: { results: [registryRow({ experiment_id: 'EXP-004', data_source_table: 'experiment_4_timesfm', required_sample: 5, updated_ts: 1 })] } },
        { all: { results: [
          { horizon_hours: 12, total: 10, resolved: 10, correct: 6, latest_activity_ts: 555 },
          { horizon_hours: 24, total: 10, resolved: 8, correct: 4, latest_activity_ts: 555 },
        ] } },
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      const exp = result.experiments[0];
      expect(exp.confidence_evidence_maturity).toBe('ACCUMULATING');
      expect(exp.oos_result.combined_correct_of_resolved).toBe('10/18');
    });

    it('experiment_4_timesfm with zero rows is a safe, honest state, never a crash (empty-data audit finding)', async () => {
      const db = makeDb([
        { all: { results: [registryRow({ experiment_id: 'EXP-004', data_source_table: 'experiment_4_timesfm', required_sample: 30, updated_ts: 7 })] } },
        { all: { results: [] } }, // experiment_4_timesfm genuinely has zero rows
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.ok).toBe(true);
      const exp = result.experiments[0];
      expect(exp.current_sample_size).toEqual({ total_forecasts: 0, total_resolved: 0, by_horizon: [] });
      expect(exp.current_measured_result).toBe('NOT_AVAILABLE');
      expect(exp.oos_result).toBe('INSUFFICIENT_SAMPLE');
      expect(exp.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
      expect(exp.last_updated).toBe(null);
    });

    it('a malformed registry row (missing optional fields, garbage data_source_table) never crashes the endpoint, never injects into SQL, and never fabricates a live result', async () => {
      const db = makeDb([
        { all: { results: [
          // Missing required_sample entirely (undefined, not null), and a
          // data_source_table value shaped like a SQL-injection attempt --
          // this must be safe because data_source_table is only ever used
          // as a JS object-property lookup key (LIVE_METRIC_PROVIDERS[...]),
          // never concatenated into any SQL string (see worker.js).
          registryRow({
            experiment_id: 'EXP-999', required_sample: undefined,
            data_source_table: "experiment_4_timesfm; DROP TABLE research_experiment_registry; --",
          }),
        ] } },
        // No further response is configured -- if the malicious
        // data_source_table string were EVER used to look up a real
        // provider (or interpolated into SQL and executed), this test
        // would fail with "No fake response configured" or a thrown
        // error; instead the unrecognized key correctly finds no
        // provider at all and the static fallback below is used, with
        // ZERO further D1 calls issued.
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.ok).toBe(true);
      const exp = result.experiments[0];
      expect(exp.current_sample_size).toBe('NOT_STARTED');
      expect(exp.current_measured_result).toBe('NOT_AVAILABLE');
      expect(exp.oos_result).toBe('NOT_AVAILABLE');
      expect(exp.confidence_evidence_maturity).toBe('UNKNOWN');
      expect(db.calls).toHaveLength(1); // only the registry SELECT itself -- no second call was ever attempted
    });
  });

  describe('affinity classification data integrity', () => {
    it('SOURCE_TOPIC_AFFINITY_DISPLAY only contains the 4 sources PR-3 actually established', () => {
      expect(Object.keys(scope.SOURCE_TOPIC_AFFINITY_DISPLAY).sort()).toEqual(
        ['cryptonews', 'geopolitics', 'macrogeo', 'regulatory']
      );
      for (const status of Object.values(scope.SOURCE_TOPIC_AFFINITY_DISPLAY)) {
        expect(status).toBe('DIRECT_TOPIC_RELEVANCE');
      }
    });
  });
});
