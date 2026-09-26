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
      extractFunctions('computeExperiment4TimesFmLiveFields', 'computeExp005LiveFields', 'computeExp009LiveFields', 'computeExp010LiveFields', 'getResearchLabRegistry') + '\n' +
      extractConstants('SOURCE_TOPIC_AFFINITY_DISPLAY', 'REACTION_HORIZONS_MS',
        'REACTION_GOOD_QUALITY_FRACTION', 'REACTION_APPROXIMATE_QUALITY_FRACTION', 'BTC_SERIES_WINDOW_MS',
        'REQUIRED_SAMPLE_FALLBACK', 'EXP005_SUBJECT', 'EXP009_SUBJECT', 'EXP010_SUBJECT', 'LIVE_METRIC_PROVIDERS',
        'EXPERIMENT5_KNOWN_V1_SOURCE_IDS', 'SOURCE_EFFECTIVENESS_SUBJECT', 'SOURCE_EFFECTIVENESS_HORIZONS',
        'SOURCE_EFFECTIVENESS_MIN_SAMPLE_LEVEL2', 'SOURCE_EFFECTIVENESS_MIN_SAMPLE_LEVEL3',
        'SOURCE_EFFECTIVENESS_CONFIGURED_WINDOW_DAYS') + '\n' +
      extractFunctions('resolveBtcReactionAtHorizon', 'getResearchLabDashboard', 'getResearchLabEvents',
        'getResearchLabEventDetail', 'getResearchLabSources', 'getResearchLabPipelineHealth',
        '_seLevel2Key', '_seLevel3Key', 'getResearchLabSourceEffectiveness')
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

    it('never ranks, scores, or recommends -- only affinity_status and an honest NOT_COMPUTED relationship (never the real INSUFFICIENT_EVIDENCE verdict label)', async () => {
      const db = makeDb([
        { all: { results: [{ sources_json: JSON.stringify({ geopolitics: 50, fng: 60 }) }] } },
      ]);
      const result = await scope.getResearchLabSources({ DB: db });
      const byKey = Object.fromEntries(result.sources.map((s) => [s.source_key, s]));
      expect(byKey.geopolitics.affinity_status).toBe('DIRECT_TOPIC_RELEVANCE');
      expect(byKey.fng.affinity_status).toBe('NO_DIRECT_TOPIC_AFFINITY');
      for (const s of result.sources) {
        expect(s.observed_event_source_relationship).toBe('NOT_COMPUTED');
        // Must never emit any of event_source_relevance.py's own real,
        // computed verdict labels here -- this placeholder is not one of them.
        expect(['RELEVANT', 'POSSIBLY_RELEVANT', 'NOT_ESTABLISHED', 'INSUFFICIENT_EVIDENCE'])
          .not.toContain(s.observed_event_source_relationship);
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

  describe('getResearchLabSourceEffectiveness', () => {
    // Mirrors research/source_intelligence.py's KNOWN_V1_SOURCE_IDS exactly
    // (same 21 ids as EXPERIMENT5_KNOWN_V1_SOURCE_IDS above) -- a local
    // copy here only so this test file states its own expectation
    // explicitly, never relying on the production constant to prove
    // itself correct.
    const ALL_21 = [
      'fng', 'funding', 'longshort', 'global', 'cryptonews', 'macrogeo',
      'geopolitics', 'regulatory', 'sosovalue', 'onchain', 'oil', 'yield10y',
      'usd', 'nasdaq', 'sp500', 'ninemag', 'foufi', 'etfflows', 'hypefunding',
      'gold', 'strc',
    ];
    const HORIZONS = [1, 3, 6, 12, 24];
    const level2Key = (key, h) => `${key}|${h}`;
    const level3Key = (key, h) => `${key}|${h}h`;

    function defaultLevel1(overrides = {}) {
      return {
        present: 500, missing: 0, coverage_pct: 100, distinct_values: 20,
        min: 1, max: 99, mean: 50, stddev: 10,
        first_ts_present: 1000000000000, last_ts_present: 1000000000000 + 30 * 86400000,
        non_numeric_value_count: 0, level1_status: 'OK',
        ...overrides,
      };
    }
    function defaultLevel2(overrides = {}) {
      return {
        n: 200, status: 'OK', sample_size_status: 'OK',
        effect_size_r: 0.1, p_raw: 0.5, p_corrected: 0.6, significant: false,
        ...overrides,
      };
    }
    function defaultLevel3(overrides = {}) {
      return {
        status: 'OK', n: 150, partial_correlation: 0.05,
        oos: {
          baseline_rmse_composite_only: 1, full_model_rmse_composite_plus_source: 0.99,
          rmse_reduction_pct: 1, status: 'IMPROVED',
        },
        ...overrides,
      };
    }

    // Builds a full 21-source fixture shaped like a real, persisted
    // build_source_effectiveness_report() output. Every key gets a
    // uniform, unremarkable "OK, not significant" shape by default;
    // perSourceOverrides lets one test change exactly the ONE key/field
    // it is actually exercising, so each assertion is unambiguous about
    // what triggered it.
    function buildReport(perSourceOverrides = {}) {
      const sourcesDiscovered = [];
      const sourcesEligible = [];
      const level1 = {};
      const level2Tests = {};
      const level3 = {};
      const evidenceLabels = {};
      const redundancyVsComposite = {};

      for (const key of ALL_21) {
        const ov = perSourceOverrides[key] || {};
        if (ov.notDiscovered) continue; // absent everywhere -- a real "not in this window" shape

        sourcesDiscovered.push(key);
        level1[key] = defaultLevel1(ov.level1);
        if (level1[key].level1_status !== 'OK') continue; // mirrors the real Python eligibility gate exactly

        sourcesEligible.push(key);
        redundancyVsComposite[key] = ov.redundancy || { n: 480, r: 0.1, strong_redundancy: false };

        for (const h of HORIZONS) {
          level2Tests[level2Key(key, h)] = defaultLevel2((ov.level2 && ov.level2[h]) || {});
          level3[level3Key(key, h)] = defaultLevel3((ov.level3 && ov.level3[h]) || {});
          evidenceLabels[level3Key(key, h)] = (ov.evidenceLabel && ov.evidenceLabel[h]) || 'INCONCLUSIVE';
        }
      }

      return {
        sources_discovered: sourcesDiscovered,
        sources_eligible_for_level2plus: sourcesEligible,
        level1,
        level2: {
          tests: level2Tests,
          multiple_testing_correction: {
            method: 'benjamini_hochberg', alpha: 0.05, n_tests: sourcesEligible.length * HORIZONS.length,
          },
        },
        level3,
        evidence_labels: evidenceLabels,
        redundancy: { vs_composite: redundancyVsComposite },
      };
    }

    function makeAnalysisRow(report, overrides = {}) {
      return {
        analysis_id: 2,
        analysis_ts: Date.now() - 3600000,
        window_start_ts: Date.now() - 90 * 86400000,
        window_end_ts: Date.now(),
        sample_size: 500,
        metric_json: JSON.stringify(report),
        ...overrides,
      };
    }

    it('no persisted EXP-005 analysis yet: activated:false, never fabricated', async () => {
      const db = makeDb([{ first: null }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(false);
    });

    it('all 21 known source keys appear exactly once, regardless of report contents', async () => {
      const report = buildReport();
      const db = makeDb([{ first: makeAnalysisRow(report) }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      expect(result.activated).toBe(true);
      const keys = result.sources.map((s) => s.source_key);
      expect(keys.length).toBe(21);
      expect(new Set(keys).size).toBe(21);
      for (const k of ALL_21) expect(keys).toContain(k);
    });

    it('a source missing from this window entirely: NOT_DISCOVERED_IN_WINDOW, still listed, never fabricated', async () => {
      const report = buildReport({ sosovalue: { notDiscovered: true } });
      const db = makeDb([{ first: makeAnalysisRow(report) }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      const keys = result.sources.map((s) => s.source_key);
      expect(keys).toContain('sosovalue'); // never silently dropped from the 21-key list
      const s = result.sources.find((x) => x.source_key === 'sosovalue');
      expect(s.level1_discovered).toBe(false);
      expect(s.level1_observed).toBe(false);
      expect(s.coverage).toBeNull();
      expect(s.not_advanced_reason).toBe('NOT_DISCOVERED_IN_WINDOW');
    });

    it('NO_VARIATION at Level 1: observed but not eligible, reason discloses the real Level-1 status verbatim', async () => {
      const report = buildReport({
        foufi: { level1: { present: 350, missing: 150, coverage_pct: 70, distinct_values: 1, level1_status: 'NO_VARIATION' } },
      });
      const db = makeDb([{ first: makeAnalysisRow(report) }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      const s = result.sources.find((x) => x.source_key === 'foufi');
      expect(s.level1_discovered).toBe(true);
      expect(s.level1_observed).toBe(true);
      expect(s.level1_status).toBe('NO_VARIATION');
      expect(s.level4_eligible_for_level2plus).toBe(false);
      expect(s.not_advanced_reason).toBe('NO_VARIATION');
      // Never invents Level 2/3 results for an ineligible source.
      expect(s.horizons.every((h) => h.level2 === null && h.level3 === null)).toBe(true);
    });

    it('insufficient sample at a horizon: level2 SMALL_SAMPLE_CAUTION and level3 INSUFFICIENT_DATA both preserved per-horizon, never hidden', async () => {
      const report = buildReport({
        oil: {
          level2: { 1: { n: 12, sample_size_status: 'SMALL_SAMPLE_CAUTION', p_raw: 0.3, p_corrected: 0.4, significant: false } },
          level3: { 1: { status: 'INSUFFICIENT_DATA', n: 12, partial_correlation: null, oos: null } },
        },
      });
      const db = makeDb([{ first: makeAnalysisRow(report) }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      const s = result.sources.find((x) => x.source_key === 'oil');
      const h1 = s.horizons.find((h) => h.horizon_hours === 1);
      const h3 = s.horizons.find((h) => h.horizon_hours === 3);
      expect(h1.level2.sample_size_status).toBe('SMALL_SAMPLE_CAUTION');
      expect(h1.level3.status).toBe('INSUFFICIENT_DATA');
      expect(h3.level2.sample_size_status).toBe('OK'); // untouched horizons keep their own real result
      expect(result.sample_sufficiency_thresholds.min_sample_level2).toBe(30);
      expect(result.sample_sufficiency_thresholds.min_sample_level3).toBe(40);
    });

    it('no significant horizon anywhere: NO_SIGNIFICANT_HORIZON, distinct from ineligibility', async () => {
      const report = buildReport(); // default fixture: every horizon INCONCLUSIVE
      const db = makeDb([{ first: makeAnalysisRow(report) }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      const s = result.sources.find((x) => x.source_key === 'global');
      expect(s.level4_eligible_for_level2plus).toBe(true);
      expect(s.any_significant_horizon).toBe(false);
      expect(s.not_advanced_reason).toBe('NO_SIGNIFICANT_HORIZON');
    });

    it('a significant horizon is reported, but is explicitly distinguished from passing the final hypothesis gate', async () => {
      const report = buildReport({
        fng: {
          evidenceLabel: { 6: 'STATISTICALLY_SIGNIFICANT' },
          level2: { 6: { significant: true, p_corrected: 0.01 } },
        },
      });
      const db = makeDb([{ first: makeAnalysisRow(report) }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      const s = result.sources.find((x) => x.source_key === 'fng');
      expect(s.any_significant_horizon).toBe(true);
      expect(s.not_advanced_reason).toBeNull();
      // The stricter, separate gate is explicitly reported as uncomputed --
      // NEVER inferred or approximated from level2/level3 significance.
      expect(s.level5_hypothesis_gate.computed).toBe(false);
      expect(s.level5_hypothesis_gate.reason).toMatch(/BUILD_REQUEST|hypothesis_gate/);
    });

    it('strong redundancy vs. the V1 composite is surfaced per source, never used to silently drop it', async () => {
      const report = buildReport({
        etfflows: { redundancy: { n: 497, r: 0.75, strong_redundancy: true } },
      });
      const db = makeDb([{ first: makeAnalysisRow(report) }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      const s = result.sources.find((x) => x.source_key === 'etfflows');
      expect(s.redundancy_vs_composite.strong_redundancy).toBe(true);
      expect(s.redundancy_vs_composite.r).toBe(0.75);
      expect(s.horizons.length).toBe(5); // disclosed, not silently excluded
    });

    it('window/freshness: the configured 90-day ceiling is never presented as the observed data range', async () => {
      const report = buildReport();
      // Mirror history's own 500-row retention cap: real per-source data
      // spans far less than the 90-day window this analysis requested.
      for (const key of Object.keys(report.level1)) {
        report.level1[key].first_ts_present = 1000000000000;
        report.level1[key].last_ts_present = 1000000000000 + 20 * 86400000; // 20 real days
      }
      const row = makeAnalysisRow(report, {
        window_start_ts: 900000000000, // the CONFIGURED request, always 90 days wide
        window_end_ts: 900000000000 + 90 * 86400000,
      });
      const db = makeDb([{ first: row }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      expect(result.data_window.configured_max_window_days).toBe(90);
      expect(result.data_window.observed_data_range.span_days).toBeCloseTo(20, 1);
      expect(result.data_window.observed_data_range.span_days).not.toBe(90);
      expect(result.data_window.note).toMatch(/never described as 90 days/);
    });

    it('multiple-testing correction metadata and the horizon-independence disclosure are always present', async () => {
      const report = buildReport();
      const db = makeDb([{ first: makeAnalysisRow(report) }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      expect(result.multiple_testing_correction.method).toBe('benjamini_hochberg');
      expect(result.horizon_independence_note).toMatch(/not independent/i);
    });

    it('malformed metric_json: activated:false, never a crash or fabricated data', async () => {
      const db = makeDb([{ first: makeAnalysisRow({}, { metric_json: '{not valid json' }) }]);
      const result = await scope.getResearchLabSourceEffectiveness({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(false);
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

  describe('computeExp005LiveFields — EXP-005 V1 Source Effectiveness', () => {
    function makeReport(overrides) {
      return {
        sources_discovered: ['fng', 'global'],
        evidence_labels: { 'fng|12h': 'STATISTICALLY_SIGNIFICANT', 'global|12h': 'INCONCLUSIVE' },
        level3: { 'fng|12h': { oos: { status: 'IMPROVED' } }, 'global|12h': { oos: { status: 'NOT_IMPROVED' } } },
        ...overrides,
      };
    }

    it('zero accumulated runs -> honest zero, never fabricated data (0 of N runs)', async () => {
      const db = makeDb([
        { first: { n: 0 } },
        { all: { results: [] } },
      ]);
      const result = await scope.computeExp005LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(0);
      expect(result.current_measured_result).toBe('NOT_AVAILABLE');
      expect(result.oos_result).toBe('NOT_AVAILABLE');
      expect(result.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
      expect(result.last_updated).toBe(null);
      expect(result.evidence_quality).toBe(null);
    });

    it('below required_sample: reports real descriptive counts from the latest run, but oos_result is gated INSUFFICIENT_SAMPLE -- never a conclusion from one early run', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify(makeReport({})), validation_status: 'candidate_signal_observed' }] } },
      ]);
      const result = await scope.computeExp005LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(1);
      expect(result.current_measured_result.sources_discovered).toBe(2);
      expect(result.current_measured_result.statistically_significant_pairs).toBe(1);
      expect(result.current_measured_result.incremental_beyond_composite_pairs).toBe(1);
      // The gate: even though this one run found a significant+improved
      // pair, oos_result must NOT report it as replicated -- exactly
      // "do not call it successful from an early positive result".
      expect(result.oos_result).toBe('INSUFFICIENT_SAMPLE');
      expect(result.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
      expect(result.last_updated).toBe(1000);
      // This run's stored report predates the Evidence Quality Layer
      // (no evidence_quality key at all) -- must degrade to null, never
      // crash, and must never change any of the assertions above.
      expect(result.evidence_quality).toBe(null);
    });

    it('exposes a persisted evidence_quality object from the latest run exactly unchanged -- no recomputation, renaming, or scoring', async () => {
      const evidenceQuality = {
        n_observations_supplied: 42,
        AS_OF_SAFETY: { status: 'PASS', reason: 'all observations as-of eligible', n_total: 42, n_eligible: 42, n_ineligible: 0 },
        TIMESTAMP_VALIDITY: { status: 'PASS', reason: 'all observations have valid numeric timestamps', n_total: 42, n_valid: 42, n_invalid: 0 },
        HISTORICAL_TIMESTAMP_VALIDITY: { status: 'PASS', reason: 'all observations within the historical population have valid timestamps', n_historical: 42, n_valid: 42, n_invalid: 0 },
        SAMPLE_DEPTH: { status: 'PASS', reason: 'sample depth threshold met', n_eligible: 42, min_observations: 30 },
        CADENCE: { status: 'PASS', reason: 'no gap exceeds gap_threshold_ms', observation_count: 42, n_unique_observation_times: 42, duplicate_timestamps_excluded: 0 },
        DUPLICATE_QUALITY: { status: 'PASS', reason: 'no duplicate observation_time values', n_total: 42, n_duplicate_timestamps: 0, duplicate_timestamps: [] },
        PROVENANCE: { status: 'VERIFIED', fields_present: ['source_key', 'provider'], fields_missing: [] },
        WINDOW_CONFORMANCE: { status: 'PASS', n_within: 42, n_outside: 0 },
        OVERALL_STATUS: 'SUFFICIENT',
      };
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify(makeReport({ evidence_quality: evidenceQuality })), validation_status: 'candidate_signal_observed' }] } },
      ]);
      const result = await scope.computeExp005LiveFields({ DB: db }, 4);
      // Byte-for-byte pass-through: no field renamed, dropped, added, or
      // reshaped -- and definitely no numeric score derived from it.
      expect(result.evidence_quality).toEqual(evidenceQuality);
      expect(Object.keys(result.evidence_quality).sort()).toEqual(Object.keys(evidenceQuality).sort());
      // Adding evidence_quality to the stored report must not perturb
      // any of the fields this provider already computed.
      expect(result.current_measured_result.sources_discovered).toBe(2);
      expect(result.oos_result).toBe('INSUFFICIENT_SAMPLE');
      expect(result.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
      expect(result.last_updated).toBe(1000);
    });

    it('at/above required_sample: oos_result reports ONLY pairs that replicated in EVERY accumulated run, not just the latest', async () => {
      const runs = [
        { analysis_ts: 1000, metric_json: JSON.stringify(makeReport({})) }, // fng|12h significant+improved
        { analysis_ts: 2000, metric_json: JSON.stringify(makeReport({})) }, // fng|12h significant+improved again
        { analysis_ts: 3000, metric_json: JSON.stringify(makeReport({
          evidence_labels: { 'fng|12h': 'INCONCLUSIVE', 'global|12h': 'INCONCLUSIVE' }, // fng NOT significant this run
          level3: { 'fng|12h': { oos: { status: 'NOT_IMPROVED' } }, 'global|12h': { oos: { status: 'NOT_IMPROVED' } } },
        })) },
        { analysis_ts: 4000, metric_json: JSON.stringify(makeReport({})) }, // fng|12h significant+improved again
      ];
      const db = makeDb([
        { first: { n: 4 } },
        { all: { results: runs } },
      ]);
      const result = await scope.computeExp005LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(4);
      expect(result.confidence_evidence_maturity).toBe('ACCUMULATING');
      // fng|12h was NOT significant+improved in run 3 -> must NOT be
      // reported as replicated across "every accumulated run".
      expect(result.oos_result.replicated_significant_and_incremental_pairs).toEqual([]);
      expect(result.oos_result.runs_considered).toBe(4);
    });

    it('a pair that replicates in ALL accumulated runs IS reported once required_sample is reached', async () => {
      const consistentReport = () => JSON.stringify(makeReport({}));
      const db = makeDb([
        { first: { n: 4 } },
        { all: { results: [1000, 2000, 3000, 4000].map((ts) => ({ analysis_ts: ts, metric_json: consistentReport() })) } },
      ]);
      const result = await scope.computeExp005LiveFields({ DB: db }, 4);
      expect(result.oos_result.replicated_significant_and_incremental_pairs).toEqual(['fng|12h']);
    });

    it('malformed metric_json (unparseable) degrades gracefully, never crashes, never fabricates', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 999, metric_json: 'not valid json{{{' }] } },
      ]);
      const result = await scope.computeExp005LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(1);
      expect(result.current_measured_result).toBe('NOT_AVAILABLE');
      expect(result.confidence_evidence_maturity).toBe('UNKNOWN');
      expect(result.last_updated).toBe(999);
      expect(result.evidence_quality).toBe(null);
    });

    it('every D1 call is SELECT-only, filtered by the exact EXP-005 subject, never a write', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1, metric_json: JSON.stringify(makeReport({})) }] } },
      ]);
      await scope.computeExp005LiveFields({ DB: db }, 4);
      for (const call of db.calls) {
        expect(call.sql).toMatch(/^SELECT/i);
        expect(call.sql).not.toMatch(/\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b/i);
        expect(call.args).toEqual([scope.EXP005_SUBJECT]);
      }
    });

    it('is wired into LIVE_METRIC_PROVIDERS under a dedicated lookup key, not the literal shared table name', () => {
      expect(scope.LIVE_METRIC_PROVIDERS.research_analyses_exp005_source_effectiveness).toBe(scope.computeExp005LiveFields);
    });

    it('end-to-end via getResearchLabRegistry: EXP-005 wired correctly alongside EXP-004, each using its own provider', async () => {
      const db = makeDb([
        { all: { results: [
          {
            experiment_id: 'EXP-004', title: 't', research_question: 'q', purpose: 'p', experiment_type: 'TYPE_2',
            expected_result: 'e', success_criterion: 's', start_date: null, target_date: null, status: 'ACCUMULATING',
            baseline: 'b', required_sample: 30, conclusion: null, next_action: 'n', github_refs: null,
            data_source_table: 'experiment_4_timesfm', created_ts: 1, updated_ts: 1,
          },
          {
            experiment_id: 'EXP-005', title: 't2', research_question: 'q2', purpose: 'p2', experiment_type: 'TYPE_1',
            expected_result: 'e2', success_criterion: 's2', start_date: null, target_date: null, status: 'ACCUMULATING',
            baseline: 'b2', required_sample: 4, conclusion: null, next_action: 'n2', github_refs: null,
            data_source_table: 'research_analyses_exp005_source_effectiveness', created_ts: 2, updated_ts: 2,
          },
        ] } },
        { all: { results: [] } }, // EXP-004's own live query (empty)
        { first: { n: 0 } },      // EXP-005's COUNT query
        { all: { results: [] } }, // EXP-005's rows query
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.ok).toBe(true);
      const exp004 = result.experiments.find((e) => e.experiment_id === 'EXP-004');
      const exp005 = result.experiments.find((e) => e.experiment_id === 'EXP-005');
      expect(exp004.current_measured_result).toBe('NOT_AVAILABLE');
      expect(exp005.current_sample_size).toBe(0);
      expect(exp005.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
    });
  });

  describe('computeExp009LiveFields — EXP-009 Event x Source x Real-World Evidence', () => {
    function makeReport(overrides) {
      return {
        window: { start_ts: 1, end_ts: 2 },
        events: [{ event_id: 1, event_ts: 100, is_internal_model_event: false }],
        n_internal_model_events_excluded_from_results: 3,
        source_keys: ['fng', 'geopolitics'],
        results: [
          { event_id: 1, source_key: 'fng', event_source_interpretation: 'INSUFFICIENT_EVIDENCE', is_internal_model_event: false },
          { event_id: 1, source_key: 'geopolitics', event_source_interpretation: 'EVENT_SOURCE_INFORMATIONAL_ONLY', is_internal_model_event: false },
        ],
        evidence_coverage: {
          n_events: 4, n_real_world_events: 1,
          n_real_world_events_with_any_evidence: 1, n_real_world_events_insufficient_evidence: 0,
        },
        by_source_interpretation: {
          fng: { EVENT_SOURCE_ALIGNED: 0, EVENT_SOURCE_NOT_ALIGNED: 0, EVENT_SOURCE_REDUNDANT_POSSIBLE: 0, EVENT_SOURCE_INFORMATIONAL_ONLY: 0, EVENT_SOURCE_MISLEADING_POSSIBLE: 0, INSUFFICIENT_EVIDENCE: 1 },
          geopolitics: { EVENT_SOURCE_ALIGNED: 0, EVENT_SOURCE_NOT_ALIGNED: 0, EVENT_SOURCE_REDUNDANT_POSSIBLE: 0, EVENT_SOURCE_INFORMATIONAL_ONLY: 1, EVENT_SOURCE_MISLEADING_POSSIBLE: 0, INSUFFICIENT_EVIDENCE: 0 },
        },
        btc_outcome_coverage: {
          '6h': { n_events: 1, n_resolved: 1 }, '12h': { n_events: 1, n_resolved: 1 }, '24h': { n_events: 1, n_resolved: 0 },
        },
        ...overrides,
      };
    }

    it('zero accumulated runs -> honest zero, never fabricated data', async () => {
      const db = makeDb([
        { first: { n: 0 } },
        { all: { results: [] } },
      ]);
      const result = await scope.computeExp009LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(0);
      expect(result.current_measured_result).toBe('NOT_AVAILABLE');
      expect(result.oos_result).toBe('NOT_AVAILABLE');
      expect(result.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
      expect(result.last_updated).toBe(null);
      expect(result.evidence_quality).toBe(null);
    });

    it('below required_sample: reports real descriptive counts from the latest run, but oos_result is gated INSUFFICIENT_SAMPLE', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify(makeReport({})), validation_status: 'evidence_observed' }] } },
      ]);
      const result = await scope.computeExp009LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(1);
      expect(result.current_measured_result.n_real_world_events).toBe(1);
      expect(result.current_measured_result.n_real_world_events_with_any_evidence).toBe(1);
      expect(result.current_measured_result.n_event_source_rows).toBe(2);
      expect(result.current_measured_result.interpretation_totals).toEqual({
        EVENT_SOURCE_ALIGNED: 0, EVENT_SOURCE_NOT_ALIGNED: 0, EVENT_SOURCE_REDUNDANT_POSSIBLE: 0,
        EVENT_SOURCE_INFORMATIONAL_ONLY: 1, EVENT_SOURCE_MISLEADING_POSSIBLE: 0, INSUFFICIENT_EVIDENCE: 1,
      });
      expect(result.current_measured_result.btc_outcome_coverage).toEqual(makeReport({}).btc_outcome_coverage);
      // The gate: this is an evidence-accumulation milestone, never a
      // statistical conclusion drawn from one early run.
      expect(result.oos_result).toBe('INSUFFICIENT_SAMPLE');
      expect(result.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
      expect(result.last_updated).toBe(1000);
      // This run's stored report predates the Evidence Quality Layer --
      // must degrade to null, never crash.
      expect(result.evidence_quality).toBe(null);
    });

    it('exposes a persisted evidence_quality object from the latest run exactly unchanged -- no recomputation, renaming, or scoring', async () => {
      const evidenceQuality = {
        n_observations_supplied: 10,
        AS_OF_SAFETY: { status: 'PASS', reason: 'all observations as-of eligible', n_total: 10, n_eligible: 10, n_ineligible: 0 },
        TIMESTAMP_VALIDITY: { status: 'PASS', reason: 'all observations have valid numeric timestamps', n_total: 10, n_valid: 10, n_invalid: 0 },
        HISTORICAL_TIMESTAMP_VALIDITY: { status: 'PASS', reason: 'all observations within the historical population have valid timestamps', n_historical: 10, n_valid: 10, n_invalid: 0 },
        SAMPLE_DEPTH: { status: 'INSUFFICIENT_EVIDENCE', reason: 'below min_observations', n_eligible: 10, min_observations: 30 },
        CADENCE: { status: 'WARNING', reason: '1 of 9 gaps exceed gap_threshold_ms', observation_count: 10, n_unique_observation_times: 10, duplicate_timestamps_excluded: 0 },
        DUPLICATE_QUALITY: { status: 'PASS', reason: 'no duplicate observation_time values', n_total: 10, n_duplicate_timestamps: 0, duplicate_timestamps: [] },
        PROVENANCE: { status: 'UNKNOWN', fields_present: [], fields_missing: ['source_key'] },
        WINDOW_CONFORMANCE: { status: 'PASS', n_within: 10, n_outside: 0 },
        OVERALL_STATUS: 'INSUFFICIENT',
      };
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify(makeReport({ evidence_quality: evidenceQuality })) }] } },
      ]);
      const result = await scope.computeExp009LiveFields({ DB: db }, 4);
      expect(result.evidence_quality).toEqual(evidenceQuality);
      expect(Object.keys(result.evidence_quality).sort()).toEqual(Object.keys(evidenceQuality).sort());
      expect(result.current_measured_result.n_real_world_events).toBe(1);
      expect(result.oos_result).toBe('INSUFFICIENT_SAMPLE');
    });

    it('at required_sample: oos_result reports milestone progress, never a coefficient/significance verdict', async () => {
      const runs = [1000, 2000, 3000, 4000].map((ts) => ({ analysis_ts: ts, metric_json: JSON.stringify(makeReport({})) }));
      const db = makeDb([
        { first: { n: 4 } },
        { all: { results: runs } },
      ]);
      const result = await scope.computeExp009LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(4);
      expect(result.confidence_evidence_maturity).toBe('ACCUMULATING');
      expect(result.oos_result.runs_considered).toBe(4);
      expect(result.oos_result.milestone).toMatch(/4 of 4/);
      // Never a hypothesis-test-style field on this experiment's oos_result.
      expect(result.oos_result.replicated_significant_and_incremental_pairs).toBeUndefined();
    });

    it('malformed metric_json (unparseable) degrades gracefully, never crashes, never fabricates', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 999, metric_json: 'not valid json{{{' }] } },
      ]);
      const result = await scope.computeExp009LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(1);
      expect(result.current_measured_result).toBe('NOT_AVAILABLE');
      expect(result.confidence_evidence_maturity).toBe('UNKNOWN');
      expect(result.last_updated).toBe(999);
      expect(result.evidence_quality).toBe(null);
    });

    it('every D1 call is SELECT-only, filtered by the exact EXP-009 subject, never a write', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1, metric_json: JSON.stringify(makeReport({})) }] } },
      ]);
      await scope.computeExp009LiveFields({ DB: db }, 4);
      for (const call of db.calls) {
        expect(call.sql).toMatch(/^SELECT/i);
        expect(call.sql).not.toMatch(/\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b/i);
        expect(call.args).toEqual([scope.EXP009_SUBJECT]);
      }
    });

    it('is wired into LIVE_METRIC_PROVIDERS under a dedicated lookup key, not the literal shared table name', () => {
      expect(scope.LIVE_METRIC_PROVIDERS.research_analyses_exp009_event_source_evidence).toBe(scope.computeExp009LiveFields);
    });

    it('end-to-end via getResearchLabRegistry: EXP-009 wired correctly alongside EXP-005, each using its own provider', async () => {
      const db = makeDb([
        { all: { results: [
          {
            experiment_id: 'EXP-005', title: 't', research_question: 'q', purpose: 'p', experiment_type: 'TYPE_1',
            expected_result: 'e', success_criterion: 's', start_date: null, target_date: null, status: 'PROPOSED',
            baseline: 'b', required_sample: 4, conclusion: null, next_action: 'n', github_refs: null,
            data_source_table: 'research_analyses_exp005_source_effectiveness', created_ts: 1, updated_ts: 1,
          },
          {
            experiment_id: 'EXP-009', title: 't2', research_question: 'q2', purpose: 'p2', experiment_type: 'TYPE_1',
            expected_result: 'e2', success_criterion: 's2', start_date: null, target_date: null, status: 'PROPOSED',
            baseline: 'b2', required_sample: 4, conclusion: null, next_action: 'n2', github_refs: null,
            data_source_table: 'research_analyses_exp009_event_source_evidence', created_ts: 2, updated_ts: 2,
          },
        ] } },
        { first: { n: 0 } },      // EXP-005's COUNT query
        { all: { results: [] } }, // EXP-005's rows query
        { first: { n: 0 } },      // EXP-009's COUNT query
        { all: { results: [] } }, // EXP-009's rows query
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.ok).toBe(true);
      const exp005 = result.experiments.find((e) => e.experiment_id === 'EXP-005');
      const exp009 = result.experiments.find((e) => e.experiment_id === 'EXP-009');
      expect(exp005.current_sample_size).toBe(0);
      expect(exp009.current_sample_size).toBe(0);
      expect(exp009.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
    });
  });

  describe('computeExp010LiveFields — EXP-010 Source Dialogue Validation', () => {
    function makeReport(overrides) {
      return {
        window: { start_ts: 1, end_ts: 2 },
        source_keys: ['alpha', 'beta'],
        relationship_summary: {
          n_events_used: 2, n_internal_model_events_excluded: 0, n_source_pair_observations: 4,
          by_relationship: { INSUFFICIENT_EVIDENCE: 1, DIFFERENT_TIMING: 0, SUPPORTING: 2, CONTRADICTING: 1 },
        },
        redundancy_summary: {
          n_source_pairs: 1,
          by_redundancy: { REDUNDANCY_UNRESOLVED: 0, NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED: 1, INSUFFICIENT_EVIDENCE: 0 },
        },
        ...overrides,
      };
    }

    it('zero accumulated runs -> honest zero, never fabricated data', async () => {
      const db = makeDb([
        { first: { n: 0 } },
        { all: { results: [] } },
      ]);
      const result = await scope.computeExp010LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(0);
      expect(result.current_measured_result).toBe('NOT_AVAILABLE');
      expect(result.oos_result).toBe('NOT_AVAILABLE');
      expect(result.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
      expect(result.last_updated).toBe(null);
      expect(result.evidence_quality).toBe(null);
    });

    it('below required_sample: reports real descriptive summaries from the latest run, but oos_result is gated INSUFFICIENT_SAMPLE', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify(makeReport({})), validation_status: 'comparable_pairs_observed' }] } },
      ]);
      const result = await scope.computeExp010LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(1);
      expect(result.current_measured_result.relationship_summary).toEqual(makeReport({}).relationship_summary);
      expect(result.current_measured_result.redundancy_summary).toEqual(makeReport({}).redundancy_summary);
      expect(result.oos_result).toBe('INSUFFICIENT_SAMPLE');
      expect(result.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
      expect(result.last_updated).toBe(1000);
      expect(result.evidence_quality).toBe(null);
    });

    it('exposes a persisted evidence_quality object from the latest run exactly unchanged -- no recomputation, renaming, or scoring', async () => {
      const evidenceQuality = {
        n_observations_supplied: 8,
        AS_OF_SAFETY: { status: 'PASS', reason: 'all observations as-of eligible', n_total: 8, n_eligible: 8, n_ineligible: 0 },
        TIMESTAMP_VALIDITY: { status: 'PASS', reason: 'all observations have valid numeric timestamps', n_total: 8, n_valid: 8, n_invalid: 0 },
        HISTORICAL_TIMESTAMP_VALIDITY: { status: 'FAIL', reason: '1 of 8 observations plausibly within the historical population have a missing/malformed timestamp', n_historical: 8, n_valid: 7, n_invalid: 1 },
        SAMPLE_DEPTH: { status: 'PASS', reason: 'sample depth threshold met', n_eligible: 8, min_observations: 4 },
        CADENCE: { status: 'PASS', reason: 'no gap exceeds gap_threshold_ms', observation_count: 8, n_unique_observation_times: 8, duplicate_timestamps_excluded: 0 },
        DUPLICATE_QUALITY: { status: 'PASS', reason: 'no duplicate observation_time values', n_total: 8, n_duplicate_timestamps: 0, duplicate_timestamps: [] },
        PROVENANCE: { status: 'PARTIAL', fields_present: ['source_key'], fields_missing: ['provider'] },
        WINDOW_CONFORMANCE: { status: 'PASS', n_within: 8, n_outside: 0 },
        OVERALL_STATUS: 'INVALID',
      };
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify(makeReport({ evidence_quality: evidenceQuality })) }] } },
      ]);
      const result = await scope.computeExp010LiveFields({ DB: db }, 4);
      expect(result.evidence_quality).toEqual(evidenceQuality);
      expect(Object.keys(result.evidence_quality).sort()).toEqual(Object.keys(evidenceQuality).sort());
      // An OVERALL_STATUS of INVALID is not itself reinterpreted as a
      // measured-result/oos_result verdict -- those stay exactly as
      // this provider already computed them from relationship_summary/
      // redundancy_summary, untouched by evidence_quality's contents.
      expect(result.current_measured_result.relationship_summary).toEqual(makeReport({}).relationship_summary);
      expect(result.oos_result).toBe('INSUFFICIENT_SAMPLE');
    });

    it('at required_sample: oos_result reports milestone progress, never a coefficient/significance verdict', async () => {
      const runs = [1000, 2000, 3000, 4000].map((ts) => ({ analysis_ts: ts, metric_json: JSON.stringify(makeReport({})) }));
      const db = makeDb([
        { first: { n: 4 } },
        { all: { results: runs } },
      ]);
      const result = await scope.computeExp010LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(4);
      expect(result.confidence_evidence_maturity).toBe('ACCUMULATING');
      expect(result.oos_result.runs_considered).toBe(4);
      expect(result.oos_result.milestone).toMatch(/4 of 4/);
      expect(result.oos_result.replicated_significant_and_incremental_pairs).toBeUndefined();
    });

    it('malformed metric_json (unparseable) degrades gracefully, never crashes, never fabricates', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 999, metric_json: 'not valid json{{{' }] } },
      ]);
      const result = await scope.computeExp010LiveFields({ DB: db }, 4);
      expect(result.current_sample_size).toBe(1);
      expect(result.current_measured_result).toBe('NOT_AVAILABLE');
      expect(result.confidence_evidence_maturity).toBe('UNKNOWN');
      expect(result.last_updated).toBe(999);
      expect(result.evidence_quality).toBe(null);
    });

    it('every D1 call is SELECT-only, filtered by the exact EXP-010 subject, never a write', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1, metric_json: JSON.stringify(makeReport({})) }] } },
      ]);
      await scope.computeExp010LiveFields({ DB: db }, 4);
      for (const call of db.calls) {
        expect(call.sql).toMatch(/^SELECT/i);
        expect(call.sql).not.toMatch(/\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b/i);
        expect(call.args).toEqual([scope.EXP010_SUBJECT]);
      }
    });

    it('is wired into LIVE_METRIC_PROVIDERS under a dedicated lookup key, not the literal shared table name', () => {
      expect(scope.LIVE_METRIC_PROVIDERS.research_analyses_exp010_source_dialogue_validation).toBe(scope.computeExp010LiveFields);
    });

    it('end-to-end via getResearchLabRegistry: EXP-010 wired correctly alongside EXP-009, each using its own provider', async () => {
      const db = makeDb([
        { all: { results: [
          {
            experiment_id: 'EXP-009', title: 't', research_question: 'q', purpose: 'p', experiment_type: 'TYPE_1',
            expected_result: 'e', success_criterion: 's', start_date: null, target_date: null, status: 'PROPOSED',
            baseline: 'b', required_sample: 4, conclusion: null, next_action: 'n', github_refs: null,
            data_source_table: 'research_analyses_exp009_event_source_evidence', created_ts: 1, updated_ts: 1,
          },
          {
            experiment_id: 'EXP-010', title: 't2', research_question: 'q2', purpose: 'p2', experiment_type: 'TYPE_1',
            expected_result: 'e2', success_criterion: 's2', start_date: null, target_date: null, status: 'PROPOSED',
            baseline: 'b2', required_sample: 4, conclusion: null, next_action: 'n2', github_refs: null,
            data_source_table: 'research_analyses_exp010_source_dialogue_validation', created_ts: 2, updated_ts: 2,
          },
        ] } },
        { first: { n: 0 } },      // EXP-009's COUNT query
        { all: { results: [] } }, // EXP-009's rows query
        { first: { n: 0 } },      // EXP-010's COUNT query
        { all: { results: [] } }, // EXP-010's rows query
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.ok).toBe(true);
      const exp009 = result.experiments.find((e) => e.experiment_id === 'EXP-009');
      const exp010 = result.experiments.find((e) => e.experiment_id === 'EXP-010');
      expect(exp009.current_sample_size).toBe(0);
      expect(exp010.current_sample_size).toBe(0);
      expect(exp010.confidence_evidence_maturity).toBe('INSUFFICIENT_SAMPLE');
    });
  });

  // Additive API-exposure change (follow-up to PR #70): the Evidence
  // Quality Layer's output already lives, unmodified, in each of
  // EXP-005/EXP-009/EXP-010's own persisted metric_json -- these prove
  // getResearchLabRegistry's full end-to-end response now carries it
  // through unchanged, that its absence never breaks the endpoint, and
  // that every field the registry already returned is untouched by
  // this change.
  describe('getResearchLabRegistry — evidence_quality pass-through (additive, PR #70 follow-up)', () => {
    function registryRowFor(id, dataSourceTable, requiredSample) {
      return {
        experiment_id: id, title: `title-${id}`, research_question: `q-${id}`, purpose: `p-${id}`,
        experiment_type: 'TYPE_1', expected_result: `e-${id}`, success_criterion: `s-${id}`,
        start_date: null, target_date: null, status: 'ACCUMULATING', baseline: `b-${id}`,
        required_sample: requiredSample, conclusion: null, next_action: `n-${id}`, github_refs: null,
        data_source_table: dataSourceTable, created_ts: 1, updated_ts: 1,
      };
    }

    it('EXP-005 registry entry exposes the persisted evidence_quality object unchanged', async () => {
      const evidenceQuality = { OVERALL_STATUS: 'SUFFICIENT', AS_OF_SAFETY: { status: 'PASS' } };
      const db = makeDb([
        { all: { results: [registryRowFor('EXP-005', 'research_analyses_exp005_source_effectiveness', 1)] } },
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify({
          sources_discovered: [], evidence_labels: {}, level3: {}, evidence_quality: evidenceQuality,
        }) }] } },
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      const exp005 = result.experiments.find((e) => e.experiment_id === 'EXP-005');
      expect(exp005.evidence_quality).toEqual(evidenceQuality);
    });

    it('EXP-009 registry entry exposes the persisted evidence_quality object unchanged', async () => {
      const evidenceQuality = { OVERALL_STATUS: 'LIMITED', CADENCE: { status: 'WARNING' } };
      const db = makeDb([
        { all: { results: [registryRowFor('EXP-009', 'research_analyses_exp009_event_source_evidence', 1)] } },
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify({
          events: [], results: [], by_source_interpretation: {}, evidence_quality: evidenceQuality,
        }) }] } },
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      const exp009 = result.experiments.find((e) => e.experiment_id === 'EXP-009');
      expect(exp009.evidence_quality).toEqual(evidenceQuality);
    });

    it('EXP-010 registry entry exposes the persisted evidence_quality object unchanged', async () => {
      const evidenceQuality = { OVERALL_STATUS: 'INVALID', DUPLICATE_QUALITY: { status: 'FAIL' } };
      const db = makeDb([
        { all: { results: [registryRowFor('EXP-010', 'research_analyses_exp010_source_dialogue_validation', 1)] } },
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify({
          relationship_summary: null, redundancy_summary: null, evidence_quality: evidenceQuality,
        }) }] } },
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      const exp010 = result.experiments.find((e) => e.experiment_id === 'EXP-010');
      expect(exp010.evidence_quality).toEqual(evidenceQuality);
    });

    it('a report predating the Evidence Quality Layer (no evidence_quality key at all) exposes null, never fabricated, never a crash', async () => {
      const db = makeDb([
        { all: { results: [registryRowFor('EXP-005', 'research_analyses_exp005_source_effectiveness', 1)] } },
        { first: { n: 1 } },
        { all: { results: [{ analysis_ts: 1000, metric_json: JSON.stringify({ sources_discovered: [], evidence_labels: {}, level3: {} }) }] } },
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      const exp005 = result.experiments.find((e) => e.experiment_id === 'EXP-005');
      expect(exp005.evidence_quality).toBe(null);
    });

    it('EXP-004 (predates PR #70 entirely, different report shape) is completely unaffected -- endpoint still succeeds, all its existing fields unchanged', async () => {
      const db = makeDb([
        { all: { results: [registryRowFor('EXP-004', 'experiment_4_timesfm', 30)] } },
        // computeExperiment4TimesFmLiveFields's own single D1 call:
        { all: { results: [{ horizon_hours: 12, total: 40, resolved: 35, correct: 20, latest_activity_ts: 500 }] } },
      ]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      expect(result.ok).toBe(true);
      const exp004 = result.experiments.find((e) => e.experiment_id === 'EXP-004');
      // EXP-004's own provider was never touched by this change -- its
      // report shape has no evidence_quality concept at all, so the key
      // is simply absent (not fabricated as null), and every field it
      // already computed is untouched.
      expect(exp004.evidence_quality).toBeUndefined();
      expect(exp004.current_sample_size.total_resolved).toBe(35);
      expect(exp004.confidence_evidence_maturity).toBe('ACCUMULATING');
    });

    it('a registry row with no data_source_table (NOT_STARTED) is unaffected by this change', async () => {
      const db = makeDb([{ all: { results: [registryRowFor('EXP-999', null, null)] } }]);
      const result = await scope.getResearchLabRegistry({ DB: db });
      const exp999 = result.experiments.find((e) => e.experiment_id === 'EXP-999');
      expect(exp999.current_sample_size).toBe('NOT_STARTED');
      expect(exp999.evidence_quality).toBeUndefined();
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
