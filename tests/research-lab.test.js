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
      extractConstants('SOURCE_TOPIC_AFFINITY_DISPLAY', 'REACTION_HORIZONS_MS',
        'REACTION_GOOD_QUALITY_FRACTION', 'REACTION_APPROXIMATE_QUALITY_FRACTION') + '\n' +
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
      ]);
      const result = await scope.getResearchLabDashboard({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.btc_latest).toBeNull();
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
      const db = makeDb([
        { first: { ts: 1000, btc_price: 50000 } },
        { first: { ts: 900, score: 55 } },
        { first: { n: 3 } },
        { first: { n: 0 } },
        { first: { latest: null } },
        { all: { results: [{ event_id: 1, event_ts: 1000, category: 'LARGE_MOVE', direction: 'UP', evidence_count: 0 }] } },
      ]);
      const result = await scope.getResearchLabDashboard({ DB: db });
      expect(result.btc_latest).toEqual({ ts: 1000, btc_price: 50000 });
      expect(result.v1_composite_latest).toEqual({ ts: 900, v1_composite: 55 });
      expect(result.research_events_count).toBe(3);
      expect(result.recent_events).toHaveLength(1);
    });

    it('never issues a write-shaped query (no INSERT/UPDATE/DELETE anywhere)', async () => {
      const db = makeDb([
        { first: null }, { first: null }, { first: { n: 0 } }, { first: { n: 0 } },
        { first: { latest: null } }, { all: { results: [] } },
      ]);
      await scope.getResearchLabDashboard({ DB: db });
      for (const call of db.calls) {
        expect(call.sql).not.toMatch(/INSERT|UPDATE|DELETE/i);
        expect(call.sql).toMatch(/^SELECT/i);
      }
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
