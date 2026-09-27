import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

// Stage 7 (AI-assisted internet research + full-source event sentiment
// recalculation): the research-lab UI layer's two new endpoints.
// getResearchLabStage7Overview is SELECT-only, read-only display over
// what stage7-research-pipeline/run_stage7.py (a separate, already
// independently-tested Python script) already decided and persisted --
// this file never re-derives a sufficiency/sentiment judgment, only
// proves the display/aggregation logic and the register-response write
// path's own guardrails (never overwrite, never accept an invalid
// sentiment_assessment, never trust a raw client-supplied validation
// status).
describe('Stage 7 — AI-assisted research & sentiment recalculation (research-lab UI layer)', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(
      extractConstants('STAGE7_SENTIMENT_ASSESSMENTS') + '\n' +
      extractFunctions('parseStage7JsonField', 'getResearchLabStage7Overview', 'registerStage7ResearchResponse')
    );
  });

  // Same fake-D1 harness convention as tests/research-lab.test.js's own
  // makeDb, extended with `.run()` since registerStage7ResearchResponse
  // is Stage 7's first write path in this file (every other endpoint in
  // research-lab.test.js is read-only).
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
            return {
              first: async () => respond().first,
              all: async () => respond().all,
              run: async () => respond().run || { success: true },
            };
          },
          first: async () => respond().first,
          all: async () => respond().all,
          run: async () => respond().run || { success: true },
        };
      },
    };
  }

  describe('parseStage7JsonField', () => {
    it('parses valid JSON', () => {
      expect(scope.parseStage7JsonField('["a","b"]', [])).toEqual(['a', 'b']);
    });
    it('malformed JSON degrades to the fallback, never throws', () => {
      expect(scope.parseStage7JsonField('{not valid', [])).toEqual([]);
    });
    it('null/undefined degrade to the fallback', () => {
      expect(scope.parseStage7JsonField(null, [])).toEqual([]);
      expect(scope.parseStage7JsonField(undefined, [])).toEqual([]);
    });
  });

  describe('getResearchLabStage7Overview', () => {
    it('migration 0016 not applied: activated:false, never a thrown error', async () => {
      const db = { prepare() { throw new Error('no such table: stage7_research_requests'); } };
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(false);
      expect(result.reason).toMatch(/0016/);
    });

    it('empty tables: honest zeros, never fabricated', async () => {
      const db = makeDb([
        { all: { results: [] } }, // by-status
        { all: { results: [] } }, // open requests
        { all: { results: [] } }, // latest sentiment per event
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(true);
      expect(result.requests.total).toBe(0);
      expect(result.requests.by_status).toEqual({});
      expect(result.requests.open).toEqual([]);
      expect(result.sentiment.total_events_recalculated).toBe(0);
      expect(result.sentiment.latest_by_event).toEqual([]);
    });

    it('every query issued is SELECT-only, never a write', async () => {
      const db = makeDb([
        { all: { results: [] } }, { all: { results: [] } }, { all: { results: [] } },
      ]);
      await scope.getResearchLabStage7Overview({ DB: db });
      for (const call of db.calls) {
        expect(call.sql).toMatch(/^SELECT/i);
        expect(call.sql).not.toMatch(/\b(INSERT|UPDATE|DELETE)\b/i);
      }
    });

    it('by_status aggregates real persisted counts, and total is their sum', async () => {
      const db = makeDb([
        { all: { results: [
          { status: 'RESEARCH_REQUEST_PUBLISHED', n: 3 },
          { status: 'FAILED_RETRYABLE', n: 1 },
          { status: 'INTEGRATED', n: 5 },
        ] } },
        { all: { results: [] } },
        { all: { results: [] } },
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.requests.by_status).toEqual({ RESEARCH_REQUEST_PUBLISHED: 3, FAILED_RETRYABLE: 1, INTEGRATED: 5 });
      expect(result.requests.total).toBe(9);
    });

    it('open requests: JSON fields are parsed, and the four distinct facts (published/response/validation) are never collapsed into one', async () => {
      const db = makeDb([
        { all: { results: [{ status: 'RESEARCH_REQUEST_PUBLISHED', n: 1 }] } },
        { all: { results: [{
          request_id: 'stage7-req-42-1', event_id: 42, event_category: 'LARGE_MOVE', event_ts: 1000,
          status: 'RESEARCH_REQUEST_PUBLISHED', sufficiency_status: 'INSUFFICIENT_EVIDENCE',
          reasons_json: '["No evidence rows exist for this event."]',
          questions_json: '["What happened?"]',
          missing_categories_json: '["primary_reporting"]',
          created_ts: 2000, updated_ts: 2000,
          github_path: 'research/stage7_requests/stage7-req-42-1.json', github_published_ts: 2001, github_publish_error: null,
          response_id: null, response_validation_status: null, response_registered_ts: null,
        }] } },
        { all: { results: [] } },
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      const r = result.requests.open[0];
      expect(r.reasons).toEqual(['No evidence rows exist for this event.']);
      expect(r.questions).toEqual(['What happened?']);
      expect(r.missing_categories).toEqual(['primary_reporting']);
      expect(r.github_published).toBe(true);
      expect(r.github_publish_error).toBeNull();
      expect(r.response_received).toBe(false);
      expect(r.response_validation_status).toBeNull();
    });

    it('a request that failed to publish reports github_published:false with the real error, never silently hidden', async () => {
      const db = makeDb([
        { all: { results: [{ status: 'FAILED_RETRYABLE', n: 1 }] } },
        { all: { results: [{
          request_id: 'stage7-req-7-1', event_id: 7, event_category: 'REGIME_REVERSAL', event_ts: 500,
          status: 'FAILED_RETRYABLE', sufficiency_status: 'INSUFFICIENT',
          reasons_json: '[]', questions_json: '[]', missing_categories_json: '[]',
          created_ts: 600, updated_ts: 600,
          github_path: null, github_published_ts: null, github_publish_error: 'push rejected: network error',
          response_id: null, response_validation_status: null, response_registered_ts: null,
        }] } },
        { all: { results: [] } },
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      const r = result.requests.open[0];
      expect(r.github_published).toBe(false);
      expect(r.github_publish_error).toBe('push rejected: network error');
    });

    it('a request with a registered but not-yet-validated response reports response_received:true distinct from validation', async () => {
      const db = makeDb([
        { all: { results: [{ status: 'RESEARCH_RESPONSE_RECEIVED', n: 1 }] } },
        { all: { results: [{
          request_id: 'stage7-req-9-1', event_id: 9, event_category: 'LARGE_MOVE', event_ts: 500,
          status: 'RESEARCH_RESPONSE_RECEIVED', sufficiency_status: 'INSUFFICIENT',
          reasons_json: '[]', questions_json: '[]', missing_categories_json: '[]',
          created_ts: 600, updated_ts: 700,
          github_path: 'research/stage7_requests/stage7-req-9-1.json', github_published_ts: 601, github_publish_error: null,
          response_id: 'stage7-resp-stage7-req-9-1', response_validation_status: 'PENDING', response_registered_ts: 700,
        }] } },
        { all: { results: [] } },
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      const r = result.requests.open[0];
      expect(r.response_received).toBe(true);
      expect(r.response_validation_status).toBe('PENDING');
    });

    it('malformed reasons_json on one row degrades to an empty list, never aborts the whole listing', async () => {
      const db = makeDb([
        { all: { results: [{ status: 'RESEARCH_REQUEST_PUBLISHED', n: 1 }] } },
        { all: { results: [{
          request_id: 'stage7-req-1-1', event_id: 1, event_category: 'LARGE_MOVE', event_ts: 100,
          status: 'RESEARCH_REQUEST_PUBLISHED', sufficiency_status: 'INSUFFICIENT',
          reasons_json: 'not valid json', questions_json: '[]', missing_categories_json: '[]',
          created_ts: 100, updated_ts: 100,
          github_path: null, github_published_ts: null, github_publish_error: null,
          response_id: null, response_validation_status: null, response_registered_ts: null,
        }] } },
        { all: { results: [] } },
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.requests.open[0].reasons).toEqual([]);
    });

    it('latest_by_event: contributing/excluded evidence counts come from the real persisted JSON, and a null sentiment_label is never coerced', async () => {
      const db = makeDb([
        { all: { results: [] } },
        { all: { results: [] } },
        { all: { results: [{
          event_id: 5, event_category: 'LARGE_MOVE', event_ts: 900,
          evidence_sufficiency: 'SUFFICIENT', sentiment_label: null, sentiment_score: null,
          calculation_ts: 1000, formula_version: 'stage7-v1', ai_research_response_id: null,
          contributing_evidence_ids_json: '[1,2,3]', excluded_evidence_json: '[{"evidence_id":4,"reason":"dup"}]',
        }] } },
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.sentiment.total_events_recalculated).toBe(1);
      const ev = result.sentiment.latest_by_event[0];
      expect(ev.sentiment_label).toBeNull(); // no defensible assessment -- never fabricated
      expect(ev.contributing_evidence_count).toBe(3);
      expect(ev.excluded_evidence_count).toBe(1);
    });

    it('a validated response driving a POSITIVE recalculation passes the real label/score through unmodified', async () => {
      const db = makeDb([
        { all: { results: [] } }, { all: { results: [] } },
        { all: { results: [{
          event_id: 6, event_category: 'LARGE_MOVE', event_ts: 900,
          evidence_sufficiency: 'SUFFICIENT', sentiment_label: 'POSITIVE', sentiment_score: 100,
          calculation_ts: 1000, formula_version: 'stage7-v1', ai_research_response_id: 'stage7-resp-stage7-req-6-1',
          contributing_evidence_ids_json: '[]', excluded_evidence_json: '[]',
        }] } },
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      const ev = result.sentiment.latest_by_event[0];
      expect(ev.sentiment_label).toBe('POSITIVE');
      expect(ev.sentiment_score).toBe(100);
      expect(ev.ai_research_response_id).toBe('stage7-resp-stage7-req-6-1');
    });
  });

  describe('registerStage7ResearchResponse', () => {
    const validFindings = { summary: 's', sentiment_assessment: 'POSITIVE' };

    it('STAGE7_SENTIMENT_ASSESSMENTS is exactly the 4 spec-mandated labels', () => {
      expect(scope.STAGE7_SENTIMENT_ASSESSMENTS).toEqual(['POSITIVE', 'NEGATIVE', 'MIXED', 'INDETERMINATE']);
    });

    it('unknown request_id: explicit 404, never a thrown error or fabricated success', async () => {
      const db = makeDb([{ first: null }]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-999-1', findings: validFindings }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(404);
    });

    it('a terminal (INTEGRATED) request refuses a new response -- a new research pass requires a NEW request', async () => {
      const db = makeDb([{ first: { request_id: 'stage7-req-1-1', status: 'INTEGRATED' } }]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-1-1', findings: validFindings }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(409);
      expect(result.error).toMatch(/INTEGRATED/);
    });

    it('a REJECTED request also refuses a new response', async () => {
      const db = makeDb([{ first: { request_id: 'stage7-req-1-1', status: 'REJECTED' } }]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-1-1', findings: validFindings }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(409);
    });

    it('a request that already has a registered response is never overwritten', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-1-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: { response_id: 'stage7-resp-stage7-req-1-1' } },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-1-1', findings: validFindings }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(409);
      expect(result.error).toMatch(/already has a registered response/);
      // No INSERT/UPDATE was ever attempted.
      expect(db.calls).toHaveLength(2);
    });

    it('a missing or invalid sentiment_assessment is rejected with 400, never silently defaulted', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-1-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-1-1', findings: { summary: 's', sentiment_assessment: 'BULLISH' } }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(400);
    });

    it('missing findings object entirely is also rejected with 400', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-1-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-1-1', findings: null }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(400);
    });

    it('a valid, unvalidated registration: PENDING status, request moves to RESEARCH_RESPONSE_RECEIVED, response_id is deterministic', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-1-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
        { run: { success: true } }, // INSERT stage7_research_responses
        { run: { success: true } }, // UPDATE stage7_research_requests
      ]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-1-1', provider: 'claude', findings: validFindings, sources: [], validated: false }
      );
      expect(result.ok).toBe(true);
      expect(result.response_id).toBe('stage7-resp-stage7-req-1-1');
      expect(result.validation_status).toBe('PENDING');
      const insertCall = db.calls[2];
      expect(insertCall.sql).toMatch(/INSERT INTO stage7_research_responses/);
      expect(insertCall.args).toContain('PENDING');
      const updateCall = db.calls[3];
      expect(updateCall.sql).toMatch(/UPDATE stage7_research_requests SET status/);
      expect(updateCall.args).toContain('RESEARCH_RESPONSE_RECEIVED');
      // Defense in depth: the UPDATE itself never touches a terminal row,
      // even though the earlier explicit check already prevents reaching
      // here for one.
      expect(updateCall.sql).toMatch(/status NOT IN \('INTEGRATED', 'REJECTED'\)/);
    });

    it('an explicit validated:true registration sets VALIDATED and moves the request to RESEARCH_COMPLETED', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-2-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
        { run: { success: true } },
        { run: { success: true } },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-2-1', findings: validFindings, validated: true }
      );
      expect(result.validation_status).toBe('VALIDATED');
      const insertCall = db.calls[2];
      expect(insertCall.args).toContain('VALIDATED');
      const updateCall = db.calls[3];
      expect(updateCall.args).toContain('RESEARCH_COMPLETED');
    });

    it('validated is never inferred as true -- omitting it defaults to PENDING, never VALIDATED', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-3-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
        { run: { success: true } },
        { run: { success: true } },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-3-1', findings: validFindings }
      );
      expect(result.validation_status).toBe('PENDING');
    });

    it('sources default to an empty array when not an array, never crashing on malformed input', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-4-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
        { run: { success: true } },
        { run: { success: true } },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-4-1', findings: validFindings, sources: 'not-an-array' }
      );
      expect(result.ok).toBe(true);
      const insertCall = db.calls[2];
      expect(insertCall.args).toContain('[]');
    });

    it('findings are stored verbatim as inert JSON text, never executed or reinterpreted', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-5-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
        { run: { success: true } },
        { run: { success: true } },
      ]);
      const sneaky = { summary: '<script>alert(1)</script>; DROP TABLE stage7_event_sentiment;', sentiment_assessment: 'NEGATIVE' };
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-5-1', findings: sneaky }
      );
      expect(result.ok).toBe(true);
      const insertCall = db.calls[2];
      expect(insertCall.args).toContain(JSON.stringify(sneaky));
      // Never a second statement, never string-interpolated into the SQL itself.
      expect(insertCall.sql).not.toContain('DROP TABLE');
      expect(insertCall.sql.match(/;/g) || []).toHaveLength(0);
    });
  });
});
