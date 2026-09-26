import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

// Experiment 5 research-lab UI layer: read-only display over
// research_sentiment_archive (migration 0015) and research_hypotheses
// (migration 0008, subject LIKE 'experiment5:%') -- NEITHER migration
// is applied to production D1 as of this change, so every function
// here must degrade to activated:false (never throw) when the
// underlying table doesn't exist. These tests exist specifically to
// prove that degradation, prove every D1 call is SELECT-only, and
// prove the deterministic narrative summary never fabricates a claim.
describe('Experiment 5 research-lab API helpers', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(
      extractConstants('EXPERIMENT5_MIN_SAMPLE_FOR_CONCLUSION', 'EXPERIMENT5_KNOWN_V1_SOURCE_IDS', 'EXPERIMENT5_STALE_AFTER_N_OBSERVATIONS') + '\n' +
      extractFunctions(
        'getResearchLabExperiment5Overview', 'getResearchLabExperiment5Decisions',
        'getResearchLabExperiment5Results', 'getResearchLabExperiment5SentimentSeries',
        'getResearchLabExperiment5SourceIntelligence', 'getResearchLabTimeline',
        'buildExperiment5NarrativeSummary'
      )
    );
  });

  // A fake D1 whose prepare() can be told to THROW for specific SQL
  // substrings (simulating "no such table" for a migration that isn't
  // applied yet) -- distinct from research-lab.test.js's own makeDb
  // because these tests specifically need per-table failure, not just
  // per-call-index responses.
  function makeDb(responses, throwOnSqlContaining) {
    const calls = [];
    let callIndex = 0;
    return {
      calls,
      prepare(sql) {
        if (throwOnSqlContaining && sql.includes(throwOnSqlContaining)) {
          throw new Error(`no such table: ${throwOnSqlContaining}`);
        }
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

  describe('getResearchLabExperiment5Overview', () => {
    it('research_sentiment_archive missing entirely: honest activated:false, never a thrown error', async () => {
      const db = makeDb([], 'research_sentiment_archive');
      const result = await scope.getResearchLabExperiment5Overview({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(false);
      expect(result.reason).toMatch(/not yet applied/);
    });

    it('archive present but research_hypotheses missing: partial activation disclosed honestly, no crash', async () => {
      const db = makeDb([
        { first: { n: 5, earliest_ts: 100, latest_ts: 900, latest_archived_ts: 950 } },
      ], 'research_hypotheses');
      const result = await scope.getResearchLabExperiment5Overview({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(true);
      expect(result.archive.n_observations).toBe(5);
      expect(result.decisions).toEqual({ proposed: 0, resolved: 0, passed: 0, failed: 0, inconclusive: 0 });
    });

    it('empty archive: honest zeros, never fabricated', async () => {
      const db = makeDb([
        { first: { n: 0, earliest_ts: null, latest_ts: null, latest_archived_ts: null } },
        { first: { n: 0 } }, { first: { n: 0 } }, { first: { n: 0 } }, { first: { n: 0 } }, { first: { n: 0 } },
        { all: { results: [] } },
      ]);
      const result = await scope.getResearchLabExperiment5Overview({ DB: db });
      expect(result.activated).toBe(true);
      expect(result.archive.n_observations).toBe(0);
      expect(result.decisions.proposed).toBe(0);
      expect(result.pending_breakdown).toEqual({ not_yet_eligible: 0, eligible_awaiting_resolution: 0 });
    });

    it('splits pending decisions into not-yet-eligible vs eligible-awaiting-resolution using each decision\'s own persisted eligible_ts', async () => {
      const now = Date.now();
      const db = makeDb([
        { first: { n: 3, earliest_ts: 100, latest_ts: 900, latest_archived_ts: 950 } },
        { first: { n: 3 } }, { first: { n: 0 } }, { first: { n: 0 } }, { first: { n: 0 } }, { first: { n: 0 } },
        { all: { results: [
          { evidence_summary_json: JSON.stringify({ decision: { eligible_ts: now + 3600000 } }) }, // future -- not yet eligible
          { evidence_summary_json: JSON.stringify({ decision: { eligible_ts: now - 3600000 } }) }, // past -- eligible now
          { evidence_summary_json: 'not valid json' }, // malformed -- must not crash, counted as eligible (honest "can't tell precisely")
        ] } },
      ]);
      const result = await scope.getResearchLabExperiment5Overview({ DB: db });
      expect(result.pending_breakdown).toEqual({ not_yet_eligible: 1, eligible_awaiting_resolution: 2 });
    });

    it('never issues a write-shaped query', async () => {
      const db = makeDb([
        { first: { n: 0, earliest_ts: null, latest_ts: null, latest_archived_ts: null } },
        { first: { n: 0 } }, { first: { n: 0 } }, { first: { n: 0 } }, { first: { n: 0 } }, { first: { n: 0 } },
        { all: { results: [] } },
      ]);
      await scope.getResearchLabExperiment5Overview({ DB: db });
      for (const call of db.calls) {
        expect(call.sql).toMatch(/^SELECT/i);
        expect(call.sql).not.toMatch(/\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b/i);
      }
    });
  });

  describe('getResearchLabExperiment5Decisions', () => {
    it('table missing: honest empty list, never a thrown error', async () => {
      const db = makeDb([], 'research_hypotheses');
      const result = await scope.getResearchLabExperiment5Decisions({ DB: db }, {});
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(false);
      expect(result.decisions).toEqual([]);
    });

    it('empty table: total 0, empty list, not an error', async () => {
      const db = makeDb([{ first: { n: 0 } }, { all: { results: [] } }]);
      const result = await scope.getResearchLabExperiment5Decisions({ DB: db }, {});
      expect(result.ok).toBe(true);
      expect(result.total).toBe(0);
      expect(result.decisions).toEqual([]);
    });

    it('populated row: decision/outcome fields correctly unpacked from evidence_summary_json', async () => {
      const decisionPayload = {
        decision: {
          anchor_ts: 1000, primary_source: 'fng', direction: 1,
          target_horizon_hours: 24, eligible_ts: 1000 + 86400000,
          confirmation: { classification: 'NO_CONFIRMATION', confirming_sources: [] },
          classifications: { fng: { sequence: { classification: 'BULLISH_TREND' } } },
        },
        outcome: { realized_direction: 'UP', agent_correct: true },
      };
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{
          hypothesis_id: 5, created_ts: 1000, last_updated_ts: 2000, subject: 'experiment5:reversal:fng',
          statement: 'x', status: 'OBSERVATION', evidence_summary_json: JSON.stringify(decisionPayload),
          out_of_sample_status: 'PASSED_HOLDOUT',
        }] } },
      ]);
      const result = await scope.getResearchLabExperiment5Decisions({ DB: db }, {});
      const d = result.decisions[0];
      expect(d.hypothesis_id).toBe(5);
      expect(d.primary_source).toBe('fng');
      expect(d.direction).toBe(1);
      expect(d.target_horizon_hours).toBe(24);
      expect(d.eligibility).toBe('RESOLVED');
      expect(d.outcome.realized_direction).toBe('UP');
      expect(d.classifications.fng.sequence.classification).toBe('BULLISH_TREND');
    });

    it('a pending decision whose eligible_ts is in the future is NOT_YET_ELIGIBLE, not RESOLVED or a fabricated ELIGIBLE state', async () => {
      const future = Date.now() + 5 * 3600000;
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{
          hypothesis_id: 1, created_ts: 1000, last_updated_ts: 1000, subject: 'experiment5:reversal:fng',
          statement: 'x', status: 'OBSERVATION',
          evidence_summary_json: JSON.stringify({ decision: { anchor_ts: 1000, eligible_ts: future } }),
          out_of_sample_status: null,
        }] } },
      ]);
      const result = await scope.getResearchLabExperiment5Decisions({ DB: db }, {});
      expect(result.decisions[0].eligibility).toBe('NOT_YET_ELIGIBLE');
    });

    it('a pending decision past its own eligible_ts is ELIGIBLE_AWAITING_RESOLUTION', async () => {
      const past = Date.now() - 5 * 3600000;
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{
          hypothesis_id: 1, created_ts: 1000, last_updated_ts: 1000, subject: 'experiment5:reversal:fng',
          statement: 'x', status: 'OBSERVATION',
          evidence_summary_json: JSON.stringify({ decision: { anchor_ts: 1000, eligible_ts: past } }),
          out_of_sample_status: null,
        }] } },
      ]);
      const result = await scope.getResearchLabExperiment5Decisions({ DB: db }, {});
      expect(result.decisions[0].eligibility).toBe('ELIGIBLE_AWAITING_RESOLUTION');
    });

    it('status filter builds the correct WHERE clause and stays parameterized for limit/offset', async () => {
      const db = makeDb([{ first: { n: 0 } }, { all: { results: [] } }]);
      await scope.getResearchLabExperiment5Decisions({ DB: db }, { status: 'passed', limit: 10, offset: 5 });
      const rowQuery = db.calls.find((c) => /ORDER BY created_ts DESC LIMIT \? OFFSET \?/.test(c.sql));
      expect(rowQuery.sql).toContain("out_of_sample_status = 'PASSED_HOLDOUT'");
      expect(rowQuery.args).toEqual([10, 5]);
    });

    it('limit is clamped to a bounded maximum -- never an unbounded query', async () => {
      const db = makeDb([{ first: { n: 0 } }, { all: { results: [] } }]);
      await scope.getResearchLabExperiment5Decisions({ DB: db }, { limit: 999999 });
      const rowQuery = db.calls.find((c) => /LIMIT \? OFFSET \?/.test(c.sql));
      expect(rowQuery.args[0]).toBeLessThanOrEqual(100);
    });

    it('malformed evidence_summary_json degrades to nulls, never crashes the whole list', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{
          hypothesis_id: 1, created_ts: 1000, last_updated_ts: 1000, subject: 'experiment5:x',
          statement: 'x', status: 'OBSERVATION', evidence_summary_json: 'not valid json{{',
          out_of_sample_status: null,
        }] } },
      ]);
      const result = await scope.getResearchLabExperiment5Decisions({ DB: db }, {});
      expect(result.ok).toBe(true);
      expect(result.decisions[0].primary_source).toBeNull();
    });

    it('never issues a write-shaped query', async () => {
      const db = makeDb([{ first: { n: 0 } }, { all: { results: [] } }]);
      await scope.getResearchLabExperiment5Decisions({ DB: db }, {});
      for (const call of db.calls) {
        expect(call.sql).toMatch(/^SELECT/i);
        expect(call.sql).not.toMatch(/\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b/i);
      }
    });
  });

  describe('getResearchLabExperiment5Results', () => {
    it('table missing: honest activated:false', async () => {
      const db = makeDb([], 'research_hypotheses');
      const result = await scope.getResearchLabExperiment5Results({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(false);
      expect(result.n_resolved).toBe(0);
    });

    it('zero resolved decisions: honest zero, insufficient-sample note, never a fabricated accuracy', async () => {
      const db = makeDb([{ all: { results: [] } }]);
      const result = await scope.getResearchLabExperiment5Results({ DB: db });
      expect(result.n_resolved).toBe(0);
      expect(result.agent_accuracy).toBeNull();
      expect(result.sufficient_sample).toBe(false);
      expect(result.note).toMatch(/Not enough validated observations/);
    });

    it('below EXPERIMENT5_MIN_SAMPLE_FOR_CONCLUSION: real counts shown, but gated as insufficient, never a premature "wins" claim', async () => {
      const rows = Array.from({ length: 5 }, (_, i) => ({
        evidence_summary_json: JSON.stringify({ outcome: { agent_correct: i < 4, v1_baseline_correct: i < 2 } }),
      }));
      const db = makeDb([{ all: { results: rows } }]);
      const result = await scope.getResearchLabExperiment5Results({ DB: db });
      expect(result.n_resolved).toBe(5);
      expect(result.agent_n).toBe(5);
      expect(result.agent_accuracy).toBeCloseTo(0.8, 5);
      expect(result.sufficient_sample).toBe(false);
      expect(result.note).toMatch(/Not enough validated observations/);
    });

    it('at/above EXPERIMENT5_MIN_SAMPLE_FOR_CONCLUSION: reports a real descriptive comparison, explicitly not a significance claim', async () => {
      const n = scope.EXPERIMENT5_MIN_SAMPLE_FOR_CONCLUSION;
      const rows = Array.from({ length: n }, (_, i) => ({
        evidence_summary_json: JSON.stringify({ outcome: { agent_correct: i % 2 === 0, v1_baseline_correct: i % 3 === 0 } }),
      }));
      const db = makeDb([{ all: { results: rows } }]);
      const result = await scope.getResearchLabExperiment5Results({ DB: db });
      expect(result.sufficient_sample).toBe(true);
      expect(result.agent_n).toBe(n);
      expect(result.note).toMatch(/not a significance test, not a claim of improvement/);
    });

    it('a null agent_correct (INSUFFICIENT_DATA_FOR_HOLDOUT) is excluded from the evaluable denominator, never counted as wrong', async () => {
      const rows = [
        { evidence_summary_json: JSON.stringify({ outcome: { agent_correct: true, v1_baseline_correct: null } }) },
        { evidence_summary_json: JSON.stringify({ outcome: { agent_correct: null, v1_baseline_correct: null } }) },
      ];
      const db = makeDb([{ all: { results: rows } }]);
      const result = await scope.getResearchLabExperiment5Results({ DB: db });
      expect(result.n_resolved).toBe(2);
      expect(result.agent_n).toBe(1); // only the one non-null agent_correct row counts
      expect(result.agent_accuracy).toBe(1);
    });

    it('never issues a write-shaped query', async () => {
      const db = makeDb([{ all: { results: [] } }]);
      await scope.getResearchLabExperiment5Results({ DB: db });
      for (const call of db.calls) expect(call.sql).toMatch(/^SELECT/i);
    });
  });

  describe('getResearchLabExperiment5SentimentSeries', () => {
    it('table missing: honest activated:false, empty series', async () => {
      const db = makeDb([], 'research_sentiment_archive');
      const result = await scope.getResearchLabExperiment5SentimentSeries({ DB: db }, undefined);
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(false);
      expect(result.series).toEqual([]);
    });

    it('populated: rows pass through unmodified, chronological', async () => {
      const rows = [{ observation_ts: 100, score: 55, technical_score: 10, btc_price: 50000, sources_json: '{}' }];
      const db = makeDb([{ all: { results: rows } }]);
      const result = await scope.getResearchLabExperiment5SentimentSeries({ DB: db }, 0);
      expect(result.series).toEqual(rows);
    });

    it('defaults the since_ts bound to 30 days ago when omitted -- never an unbounded full-table scan', async () => {
      const db = makeDb([{ all: { results: [] } }]);
      const before = Date.now();
      await scope.getResearchLabExperiment5SentimentSeries({ DB: db }, undefined);
      const call = db.calls[0];
      expect(call.args[0]).toBeGreaterThan(before - 31 * 24 * 3600000);
      expect(call.args[0]).toBeLessThanOrEqual(before);
    });

    it('an explicit since_ts is bound as a query parameter, not string-interpolated', async () => {
      const db = makeDb([{ all: { results: [] } }]);
      await scope.getResearchLabExperiment5SentimentSeries({ DB: db }, 12345);
      expect(db.calls[0].args).toEqual([12345]);
      expect(db.calls[0].sql).toMatch(/^SELECT/i);
    });
  });

  describe('getResearchLabExperiment5SourceIntelligence', () => {
    it('table missing: honest activated:false, empty lists', async () => {
      const db = makeDb([], 'research_sentiment_archive');
      const result = await scope.getResearchLabExperiment5SourceIntelligence({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(false);
      expect(result.sources).toEqual([]);
    });

    it('a key outside the 21 known V1 ids is flagged as a candidate, never auto-classified as known', async () => {
      const db = makeDb([{ all: { results: [
        { sources_json: JSON.stringify({ fng: 50, totally_new_source: 1 }) },
      ] } }]);
      const result = await scope.getResearchLabExperiment5SourceIntelligence({ DB: db });
      expect(result.candidate_new_sources).toEqual(['totally_new_source']);
      const byKey = Object.fromEntries(result.sources.map((s) => [s.source_key, s]));
      expect(byKey.fng.is_known_v1_source).toBe(true);
      expect(byKey.totally_new_source.is_known_v1_source).toBe(false);
    });

    it('coverage_pct reflects how many of the fetched rows actually carry a key -- never invented for a missing source', async () => {
      const db = makeDb([{ all: { results: [
        { sources_json: JSON.stringify({ fng: 50 }) },
        { sources_json: JSON.stringify({ fng: 51, onchain: 10 }) },
      ] } }]);
      const result = await scope.getResearchLabExperiment5SourceIntelligence({ DB: db });
      const byKey = Object.fromEntries(result.sources.map((s) => [s.source_key, s]));
      expect(byKey.fng.coverage_pct).toBe(100);
      expect(byKey.onchain.coverage_pct).toBe(50);
    });

    it('a source whose trailing EXPERIMENT5_STALE_AFTER_N_OBSERVATIONS values are all identical is flagged stale', async () => {
      const rows = Array.from({ length: scope.EXPERIMENT5_STALE_AFTER_N_OBSERVATIONS }, () => (
        { sources_json: JSON.stringify({ flat_source: 42 }) }
      ));
      const db = makeDb([{ all: { results: rows } }]);
      const result = await scope.getResearchLabExperiment5SourceIntelligence({ DB: db });
      expect(result.sources.find((s) => s.source_key === 'flat_source').is_stale).toBe(true);
    });

    it('fewer than EXPERIMENT5_STALE_AFTER_N_OBSERVATIONS appearances is never guessed as stale', async () => {
      const db = makeDb([{ all: { results: [{ sources_json: JSON.stringify({ rare_source: 1 }) }] } }]);
      const result = await scope.getResearchLabExperiment5SourceIntelligence({ DB: db });
      expect(result.sources.find((s) => s.source_key === 'rare_source').is_stale).toBe(false);
    });

    it('malformed sources_json in one row does not abort the whole listing', async () => {
      const db = makeDb([{ all: { results: [
        { sources_json: 'not valid json' },
        { sources_json: JSON.stringify({ regulatory: 10 }) },
      ] } }]);
      const result = await scope.getResearchLabExperiment5SourceIntelligence({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.sources.map((s) => s.source_key)).toEqual(['regulatory']);
    });
  });

  describe('getResearchLabTimeline', () => {
    it('both tables missing: still returns ok:true with an empty list, never throws', async () => {
      const db = makeDb([], 'research_events');
      const result = await scope.getResearchLabTimeline({ DB: db }, 50);
      expect(result.ok).toBe(true);
      expect(result.items).toEqual([]);
    });

    it('merges research_events and Experiment 5 decisions into one chronologically-sorted feed', async () => {
      const db = makeDb([
        { all: { results: [{ event_id: 1, event_ts: 500, category: 'LARGE_MOVE', direction: 'UP' }] } },
        { all: { results: [
          { hypothesis_id: 1, created_ts: 1000, last_updated_ts: 2000, subject: 'experiment5:reversal:fng', out_of_sample_status: 'PASSED_HOLDOUT' },
        ] } },
      ]);
      const result = await scope.getResearchLabTimeline({ DB: db }, 50);
      const kinds = result.items.map((i) => i.kind);
      expect(kinds).toContain('MARKET_EVENT');
      expect(kinds).toContain('EXPERIMENT5_DECISION_CREATED');
      expect(kinds).toContain('EXPERIMENT5_OUTCOME_RESOLVED');
      // chronological, most recent first
      for (let i = 1; i < result.items.length; i++) {
        expect(result.items[i - 1].ts).toBeGreaterThanOrEqual(result.items[i].ts);
      }
    });

    it('a still-pending decision produces exactly one timeline item (created), not a fabricated resolution item', async () => {
      const db = makeDb([
        { all: { results: [] } },
        { all: { results: [
          { hypothesis_id: 1, created_ts: 1000, last_updated_ts: 1000, subject: 'experiment5:reversal:fng', out_of_sample_status: null },
        ] } },
      ]);
      const result = await scope.getResearchLabTimeline({ DB: db }, 50);
      expect(result.items).toHaveLength(1);
      expect(result.items[0].kind).toBe('EXPERIMENT5_DECISION_CREATED');
    });

    it('limit is clamped to a bounded maximum', async () => {
      const db = makeDb([{ all: { results: [] } }, { all: { results: [] } }]);
      await scope.getResearchLabTimeline({ DB: db }, 999999);
      for (const call of db.calls) {
        if (/LIMIT \?/.test(call.sql)) expect(call.args[0]).toBeLessThanOrEqual(200);
      }
    });

    it('re-derives the feed from persisted rows every call -- a repeated call with the SAME rows produces an identical, non-duplicated result (refresh safety)', async () => {
      const rows = [{ hypothesis_id: 1, created_ts: 1000, last_updated_ts: 1000, subject: 'experiment5:x', out_of_sample_status: null }];
      const db1 = makeDb([{ all: { results: [] } }, { all: { results: rows } }]);
      const db2 = makeDb([{ all: { results: [] } }, { all: { results: rows } }]);
      const r1 = await scope.getResearchLabTimeline({ DB: db1 }, 50);
      const r2 = await scope.getResearchLabTimeline({ DB: db2 }, 50);
      expect(r1.items).toEqual(r2.items);
    });
  });

  describe('buildExperiment5NarrativeSummary — deterministic explanation layer', () => {
    it('not activated: a single plain-language sentence, no figures', () => {
      const lines = scope.buildExperiment5NarrativeSummary({ activated: false }, null);
      expect(lines).toHaveLength(1);
      expect(lines[0]).toMatch(/not yet active in production/);
    });

    it('activated but zero observations: honest "no observations yet", nothing else fabricated', () => {
      const lines = scope.buildExperiment5NarrativeSummary({ activated: true, archive: { n_observations: 0 } }, null);
      expect(lines).toEqual(['No sentiment observations have been archived yet.']);
    });

    it('observations present, zero decisions: reports the archive fact and an honest "no decisions yet"', () => {
      const overview = {
        activated: true,
        archive: { n_observations: 10, latest_observation_ts: 1700000000000 },
        decisions: { proposed: 0, resolved: 0, passed: 0, failed: 0, inconclusive: 0 },
        pending_breakdown: { not_yet_eligible: 0, eligible_awaiting_resolution: 0 },
      };
      const lines = scope.buildExperiment5NarrativeSummary(overview, null);
      expect(lines.some((l) => l.includes('10 sentiment observation'))).toBe(true);
      expect(lines.some((l) => l === 'No decisions have been proposed yet.')).toBe(true);
    });

    it('insufficient resolved sample: states the limitation plainly, never claims a winner', () => {
      const overview = {
        activated: true,
        archive: { n_observations: 10, latest_observation_ts: 1700000000000 },
        decisions: { proposed: 3, resolved: 1, passed: 1, failed: 0, inconclusive: 0 },
        pending_breakdown: { not_yet_eligible: 1, eligible_awaiting_resolution: 1 },
      };
      const results = { activated: true, n_resolved: 1, sufficient_sample: false, agent_n: 1, min_sample_for_conclusion: 20 };
      const lines = scope.buildExperiment5NarrativeSummary(overview, results);
      expect(lines.some((l) => l.includes('Not enough validated observations'))).toBe(true);
      expect(lines.join(' ')).not.toMatch(/beats|wins|superior|outperforms/i);
    });

    it('sufficient resolved sample: reports a real descriptive comparison with the explicit non-significance caveat', () => {
      const overview = {
        activated: true,
        archive: { n_observations: 100, latest_observation_ts: 1700000000000 },
        decisions: { proposed: 25, resolved: 25, passed: 15, failed: 10, inconclusive: 0 },
        pending_breakdown: { not_yet_eligible: 0, eligible_awaiting_resolution: 0 },
      };
      const results = {
        activated: true, n_resolved: 25, sufficient_sample: true,
        agent_accuracy: 0.6, agent_n: 25, v1_baseline_accuracy: 0.52, v1_baseline_n: 25,
      };
      const lines = scope.buildExperiment5NarrativeSummary(overview, results);
      const joined = lines.join(' ');
      expect(joined).toMatch(/60% over 25 resolved decisions/);
      expect(joined).toMatch(/V1 baseline 52% over 25/);
      expect(joined).toMatch(/not a significance test/);
    });

    it('mentions pending decisions awaiting their horizon distinctly from those awaiting resolution', () => {
      const overview = {
        activated: true,
        archive: { n_observations: 5, latest_observation_ts: 1700000000000 },
        decisions: { proposed: 2, resolved: 0, passed: 0, failed: 0, inconclusive: 0 },
        pending_breakdown: { not_yet_eligible: 2, eligible_awaiting_resolution: 3 },
      };
      const lines = scope.buildExperiment5NarrativeSummary(overview, null);
      expect(lines.some((l) => l.includes('2 decision(s) awaiting their target evaluation horizon'))).toBe(true);
      expect(lines.some((l) => l.includes('3 decision(s) eligible for evaluation'))).toBe(true);
    });
  });
});
