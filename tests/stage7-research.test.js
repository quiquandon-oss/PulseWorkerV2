import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORKER_JS_SOURCE = readFileSync(join(__dirname, '..', 'worker.js'), 'utf8');

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
      extractFunctions(
        'parseStage7JsonField', 'getResearchLabStage7Overview', 'stage7ConstantTimeEqual',
        'registerStage7ResearchResponse', 'buildStage7ResearchPromptText', 'validateStage7Sources',
        'stage7IsHttpUrl', 'stage7ParseSourcePublicationTs', 'createStage7ResearchRequests',
        'triggerStage7Recalculation', 'deriveStage7RequestLifecycleStage'
      )
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
    // Query order (all synchronous `prepare()` calls inside ONE
    // Promise.all, so array position IS call order): [0] by-status
    // (awaited separately, before the Promise.all), then within the
    // Promise.all: [1] open requests, [2] latest sentiment per event,
    // [3] open candidates. A 4th, OPTIONAL [4] "previous sentiment rows"
    // query only fires when at least one sentiment row has a non-null
    // previous_sentiment_id.
    const NO_CANDIDATES = { all: { results: [] } };

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
        NO_CANDIDATES,
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.activated).toBe(true);
      expect(result.requests.total).toBe(0);
      expect(result.requests.by_status).toEqual({});
      expect(result.requests.open).toEqual([]);
      expect(result.sentiment.total_events_recalculated).toBe(0);
      expect(result.sentiment.latest_by_event).toEqual([]);
      expect(result.candidates.total_open).toBe(0);
      expect(result.candidates.open).toEqual([]);
    });

    it('every query issued is SELECT-only, never a write', async () => {
      const db = makeDb([
        { all: { results: [] } }, { all: { results: [] } }, { all: { results: [] } }, NO_CANDIDATES,
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
        NO_CANDIDATES,
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.requests.by_status).toEqual({ RESEARCH_REQUEST_PUBLISHED: 3, FAILED_RETRYABLE: 1, INTEGRATED: 5 });
      expect(result.requests.total).toBe(9);
    });

    it('open requests: JSON fields are parsed, evidence/cutoff/prompt are surfaced, and a lifecycle_stage is derived', async () => {
      const db = makeDb([
        { all: { results: [{ status: 'RESEARCH_REQUEST_PUBLISHED', n: 1 }] } },
        { all: { results: [{
          request_id: 'stage7-req-42-1', event_id: 42, event_category: 'LARGE_MOVE', event_ts: 1000,
          status: 'RESEARCH_REQUEST_PUBLISHED', sufficiency_status: 'INSUFFICIENT_EVIDENCE',
          reasons_json: '["No evidence rows exist for this event."]',
          questions_json: '["What happened?"]',
          missing_categories_json: '["primary_reporting"]',
          historical_cutoff_ts: 1000, evidence_snapshot_json: '[{"headline":"h","publisher":"p"}]',
          prompt_text: 'the full prompt text',
          created_ts: 2000, updated_ts: 2000,
          github_path: 'research/stage7_requests/stage7-req-42-1.json', github_published_ts: 2001, github_publish_error: null,
          recalculation_requested_ts: null, recalculation_requested_by: null,
          response_id: null, response_validation_status: null, response_registered_ts: null,
        }] } },
        { all: { results: [] } },
        NO_CANDIDATES,
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      const r = result.requests.open[0];
      expect(r.reasons).toEqual(['No evidence rows exist for this event.']);
      expect(r.questions).toEqual(['What happened?']);
      expect(r.missing_categories).toEqual(['primary_reporting']);
      expect(r.historical_cutoff_ts).toBe(1000);
      expect(r.evidence_snapshot).toEqual([{ headline: 'h', publisher: 'p' }]);
      expect(r.prompt_text).toBe('the full prompt text');
      expect(r.github_published).toBe(true);
      expect(r.github_publish_error).toBeNull();
      expect(r.response_received).toBe(false);
      expect(r.response_validation_status).toBeNull();
      expect(r.lifecycle_stage).toBe('AWAITING_RESEARCH');
    });

    it('a request that failed to publish reports github_published:false with the real error, never silently hidden', async () => {
      const db = makeDb([
        { all: { results: [{ status: 'FAILED_RETRYABLE', n: 1 }] } },
        { all: { results: [{
          request_id: 'stage7-req-7-1', event_id: 7, event_category: 'REGIME_REVERSAL', event_ts: 500,
          status: 'FAILED_RETRYABLE', sufficiency_status: 'INSUFFICIENT',
          reasons_json: '[]', questions_json: '[]', missing_categories_json: '[]',
          historical_cutoff_ts: 500, evidence_snapshot_json: '[]', prompt_text: null,
          created_ts: 600, updated_ts: 600,
          github_path: null, github_published_ts: null, github_publish_error: 'push rejected: network error',
          recalculation_requested_ts: null, recalculation_requested_by: null,
          response_id: null, response_validation_status: null, response_registered_ts: null,
        }] } },
        { all: { results: [] } },
        NO_CANDIDATES,
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      const r = result.requests.open[0];
      expect(r.github_published).toBe(false);
      expect(r.github_publish_error).toBe('push rejected: network error');
      expect(r.lifecycle_stage).toBe('ERROR_RETRYABLE');
    });

    it('a request with a registered but not-yet-validated response reports response_received:true distinct from validation', async () => {
      const db = makeDb([
        { all: { results: [{ status: 'RESEARCH_RESPONSE_RECEIVED', n: 1 }] } },
        { all: { results: [{
          request_id: 'stage7-req-9-1', event_id: 9, event_category: 'LARGE_MOVE', event_ts: 500,
          status: 'RESEARCH_RESPONSE_RECEIVED', sufficiency_status: 'INSUFFICIENT',
          reasons_json: '[]', questions_json: '[]', missing_categories_json: '[]',
          historical_cutoff_ts: 500, evidence_snapshot_json: '[]', prompt_text: null,
          created_ts: 600, updated_ts: 700,
          github_path: 'research/stage7_requests/stage7-req-9-1.json', github_published_ts: 601, github_publish_error: null,
          recalculation_requested_ts: null, recalculation_requested_by: null,
          response_id: 'stage7-resp-stage7-req-9-1', response_validation_status: 'PENDING', response_registered_ts: 700,
        }] } },
        { all: { results: [] } },
        NO_CANDIDATES,
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      const r = result.requests.open[0];
      expect(r.response_received).toBe(true);
      expect(r.response_validation_status).toBe('PENDING');
      expect(r.lifecycle_stage).toBe('RESPONSE_REGISTERED');
    });

    it('a VALIDATED response with recalculation_requested_ts set reports AWAITING_RECALCULATION, and without it reports VALIDATED', async () => {
      const baseRow = {
        request_id: 'stage7-req-10-1', event_id: 10, event_category: 'LARGE_MOVE', event_ts: 500,
        status: 'RESEARCH_COMPLETED', sufficiency_status: 'SUFFICIENT',
        reasons_json: '[]', questions_json: '[]', missing_categories_json: '[]',
        historical_cutoff_ts: 500, evidence_snapshot_json: '[]', prompt_text: null,
        created_ts: 600, updated_ts: 700,
        github_path: null, github_published_ts: null, github_publish_error: null,
        response_id: 'stage7-resp-stage7-req-10-1', response_validation_status: 'VALIDATED', response_registered_ts: 700,
      };
      const dbAwaiting = makeDb([
        { all: { results: [{ status: 'RESEARCH_COMPLETED', n: 1 }] } },
        { all: { results: [{ ...baseRow, recalculation_requested_ts: 800, recalculation_requested_by: 'ops' }] } },
        { all: { results: [] } }, NO_CANDIDATES,
      ]);
      const resultAwaiting = await scope.getResearchLabStage7Overview({ DB: dbAwaiting });
      expect(resultAwaiting.requests.open[0].lifecycle_stage).toBe('AWAITING_RECALCULATION');

      const dbNotYet = makeDb([
        { all: { results: [{ status: 'RESEARCH_COMPLETED', n: 1 }] } },
        { all: { results: [{ ...baseRow, recalculation_requested_ts: null, recalculation_requested_by: null }] } },
        { all: { results: [] } }, NO_CANDIDATES,
      ]);
      const resultNotYet = await scope.getResearchLabStage7Overview({ DB: dbNotYet });
      expect(resultNotYet.requests.open[0].lifecycle_stage).toBe('VALIDATED');
    });

    it('malformed reasons_json on one row degrades to an empty list, never aborts the whole listing', async () => {
      const db = makeDb([
        { all: { results: [{ status: 'RESEARCH_REQUEST_PUBLISHED', n: 1 }] } },
        { all: { results: [{
          request_id: 'stage7-req-1-1', event_id: 1, event_category: 'LARGE_MOVE', event_ts: 100,
          status: 'RESEARCH_REQUEST_PUBLISHED', sufficiency_status: 'INSUFFICIENT',
          reasons_json: 'not valid json', questions_json: '[]', missing_categories_json: '[]',
          historical_cutoff_ts: 100, evidence_snapshot_json: 'not valid json either', prompt_text: null,
          created_ts: 100, updated_ts: 100,
          github_path: null, github_published_ts: null, github_publish_error: null,
          recalculation_requested_ts: null, recalculation_requested_by: null,
          response_id: null, response_validation_status: null, response_registered_ts: null,
        }] } },
        { all: { results: [] } },
        NO_CANDIDATES,
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.ok).toBe(true);
      expect(result.requests.open[0].reasons).toEqual([]);
      expect(result.requests.open[0].evidence_snapshot).toEqual([]);
    });

    it('open candidates: JSON fields parsed, joined to their event, returned distinctly from requests', async () => {
      const db = makeDb([
        { all: { results: [] } },
        { all: { results: [] } },
        { all: { results: [] } },
        { all: { results: [{
          candidate_id: 'stage7-cand-11', event_id: 11, event_category: 'LARGE_MOVE', event_ts: 1500,
          status: 'PROPOSED', sufficiency_status: 'INSUFFICIENT_EVIDENCE',
          reasons_json: '["r1"]', questions_json: '["q1"]', missing_categories_json: '["c1"]',
          historical_cutoff_ts: 1500, evidence_snapshot_json: '[]', proposed_ts: 1600,
        }] } },
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.candidates.total_open).toBe(1);
      const c = result.candidates.open[0];
      expect(c.candidate_id).toBe('stage7-cand-11');
      expect(c.event_id).toBe(11);
      expect(c.reasons).toEqual(['r1']);
      expect(c.questions).toEqual(['q1']);
      expect(c.missing_categories).toEqual(['c1']);
      expect(c.proposed_ts).toBe(1600);
      // Never mixed into the requests list.
      expect(result.requests.open).toEqual([]);
    });

    it('latest_by_event: contributing/excluded evidence counts come from the real persisted JSON, and a null sentiment_label is never coerced', async () => {
      const db = makeDb([
        { all: { results: [] } },
        { all: { results: [] } },
        { all: { results: [{
          event_id: 5, event_category: 'LARGE_MOVE', event_ts: 900,
          evidence_sufficiency: 'SUFFICIENT', sentiment_label: null, sentiment_score: null,
          calculation_ts: 1000, formula_version: 'stage7-v1', ai_research_response_id: null, previous_sentiment_id: null,
          contributing_evidence_ids_json: '[1,2,3]', excluded_evidence_json: '[{"evidence_id":4,"reason":"dup"}]',
        }] } },
        NO_CANDIDATES,
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      expect(result.sentiment.total_events_recalculated).toBe(1);
      const ev = result.sentiment.latest_by_event[0];
      expect(ev.sentiment_label).toBeNull(); // no defensible assessment -- never fabricated
      expect(ev.contributing_evidence_count).toBe(3);
      expect(ev.excluded_evidence_count).toBe(1);
      expect(ev.previous_sentiment_id).toBeNull();
      expect(ev.previous_sentiment_label).toBeNull();
    });

    it('a validated response driving a POSITIVE recalculation passes the real label/score through unmodified, with no previous row', async () => {
      const db = makeDb([
        { all: { results: [] } }, { all: { results: [] } },
        { all: { results: [{
          event_id: 6, event_category: 'LARGE_MOVE', event_ts: 900,
          evidence_sufficiency: 'SUFFICIENT', sentiment_label: 'POSITIVE', sentiment_score: 100,
          calculation_ts: 1000, formula_version: 'stage7-v1', ai_research_response_id: 'stage7-resp-stage7-req-6-1',
          previous_sentiment_id: null,
          contributing_evidence_ids_json: '[]', excluded_evidence_json: '[]',
        }] } },
        NO_CANDIDATES,
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      const ev = result.sentiment.latest_by_event[0];
      expect(ev.sentiment_label).toBe('POSITIVE');
      expect(ev.sentiment_score).toBe(100);
      expect(ev.ai_research_response_id).toBe('stage7-resp-stage7-req-6-1');
    });

    it('"present the new result next to the previous" (Task 3.E): a non-null previous_sentiment_id fetches and attaches that row', async () => {
      const db = makeDb([
        { all: { results: [] } }, { all: { results: [] } },
        { all: { results: [{
          event_id: 6, event_category: 'LARGE_MOVE', event_ts: 900,
          evidence_sufficiency: 'SUFFICIENT', sentiment_label: 'POSITIVE', sentiment_score: 100,
          calculation_ts: 2000, formula_version: 'stage7-v1', ai_research_response_id: 'stage7-resp-stage7-req-6-2',
          previous_sentiment_id: 41,
          contributing_evidence_ids_json: '[]', excluded_evidence_json: '[]',
        }] } },
        NO_CANDIDATES,
        // The extra "previous sentiment rows" lookup, fired only because
        // previous_sentiment_id (41) is non-null above.
        { all: { results: [{ id: 41, sentiment_label: 'MIXED', sentiment_score: 50, calculation_ts: 1000 }] } },
      ]);
      const result = await scope.getResearchLabStage7Overview({ DB: db });
      const ev = result.sentiment.latest_by_event[0];
      expect(ev.previous_sentiment_id).toBe(41);
      expect(ev.previous_sentiment_label).toBe('MIXED');
      expect(ev.previous_sentiment_score).toBe(50);
      expect(ev.previous_calculation_ts).toBe(1000);
    });
  });

  // Adversarial-review remediation, finding #2: every call below now must
  // supply a correct providedToken matching env.STAGE7_ADMIN_TOKEN, or the
  // function must reject before touching the DB at all. ADMIN_TOKEN/envOf
  // are shared by every test in this describe block, including the
  // pre-existing ones (updated in place), so a regression that silently
  // widens the auth gate would fail broadly, not just in the dedicated
  // auth tests below.
  const ADMIN_TOKEN = 'test-admin-token-do-not-use-in-prod';
  function envOf(db, token = ADMIN_TOKEN) {
    return { DB: db, STAGE7_ADMIN_TOKEN: token };
  }

  describe('stage7ConstantTimeEqual', () => {
    it('equal strings are equal', () => {
      expect(scope.stage7ConstantTimeEqual('abc123', 'abc123')).toBe(true);
    });
    it('different content, same length, is not equal', () => {
      expect(scope.stage7ConstantTimeEqual('abc123', 'abc124')).toBe(false);
    });
    it('different length is not equal', () => {
      expect(scope.stage7ConstantTimeEqual('short', 'a-much-longer-string')).toBe(false);
    });
    it('empty strings compare equal to each other, never to a non-empty one', () => {
      expect(scope.stage7ConstantTimeEqual('', '')).toBe(true);
      expect(scope.stage7ConstantTimeEqual('', 'x')).toBe(false);
    });
    it('non-string inputs are never coerced into a match', () => {
      expect(scope.stage7ConstantTimeEqual(null, null)).toBe(false);
      expect(scope.stage7ConstantTimeEqual(undefined, undefined)).toBe(false);
      expect(scope.stage7ConstantTimeEqual(123, 123)).toBe(false);
    });
  });

  describe('registerStage7ResearchResponse', () => {
    const validFindings = { summary: 's', sentiment_assessment: 'POSITIVE' };
    const rawText = 'The AI\'s full pasted answer, verbatim.';

    it('STAGE7_SENTIMENT_ASSESSMENTS is exactly the 4 spec-mandated labels', () => {
      expect(scope.STAGE7_SENTIMENT_ASSESSMENTS).toEqual(['POSITIVE', 'NEGATIVE', 'MIXED', 'INDETERMINATE']);
    });

    // ---- Authorization gate: checked before ANY D1 call, in every case ----

    it('STAGE7_ADMIN_TOKEN not configured on the Worker: 503, no DB call at all, no hardcoded fallback', async () => {
      const db = makeDb([]);
      const result = await scope.registerStage7ResearchResponse(
        { DB: db }, { requestId: 'stage7-req-1-1', findings: validFindings, providedToken: 'anything' }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(503);
      expect(result.error).toMatch(/STAGE7_ADMIN_TOKEN/);
      expect(db.calls).toHaveLength(0);
    });

    it('missing providedToken: 401, no DB call, even when the Worker has a configured token', async () => {
      const db = makeDb([]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-1-1', findings: validFindings }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(401);
      expect(db.calls).toHaveLength(0);
    });

    it('wrong providedToken: 401, no DB call', async () => {
      const db = makeDb([]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-1-1', findings: validFindings, providedToken: 'totally-wrong' }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(401);
      expect(db.calls).toHaveLength(0);
    });

    it('validated:true with a missing/wrong token is still rejected 401 -- validated is NEVER itself authorization', async () => {
      const db = makeDb([]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-1-1', findings: validFindings, validated: true, providedToken: 'wrong' }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(401);
      // Nothing was ever read or written -- an attacker cannot use
      // validated:true to force a VALIDATED row without the real token.
      expect(db.calls).toHaveLength(0);
    });

    it('a correct token proceeds past the auth gate to the normal request lookup', async () => {
      const db = makeDb([{ first: null }]); // request lookup: not found
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-999-1', findings: validFindings, providedToken: ADMIN_TOKEN }
      );
      expect(result.status).toBe(404); // proves it passed auth and reached real business logic
      expect(db.calls).toHaveLength(1);
    });

    // ---- Everything below requires the correct token, per the gate above ----

    it('unknown request_id: explicit 404, never a thrown error or fabricated success', async () => {
      const db = makeDb([{ first: null }]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-999-1', findings: validFindings, providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(404);
    });

    it('a terminal (INTEGRATED) request refuses a new response -- a new research pass requires a NEW request', async () => {
      const db = makeDb([{ first: { request_id: 'stage7-req-1-1', status: 'INTEGRATED' } }]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-1-1', findings: validFindings, providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(409);
      expect(result.error).toMatch(/INTEGRATED/);
    });

    it('a REJECTED request also refuses a new response', async () => {
      const db = makeDb([{ first: { request_id: 'stage7-req-1-1', status: 'REJECTED' } }]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-1-1', findings: validFindings, providedToken: ADMIN_TOKEN }
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
        envOf(db), { requestId: 'stage7-req-1-1', findings: validFindings, providedToken: ADMIN_TOKEN }
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
        envOf(db),
        { requestId: 'stage7-req-1-1', findings: { summary: 's', sentiment_assessment: 'BULLISH' }, providedToken: ADMIN_TOKEN }
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
        envOf(db), { requestId: 'stage7-req-1-1', findings: null, providedToken: ADMIN_TOKEN }
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
        envOf(db),
        { requestId: 'stage7-req-1-1', provider: 'claude', findings: validFindings, sources: [], validated: false, rawResponseText: rawText, providedToken: ADMIN_TOKEN }
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
      // Never leak the admin token anywhere in the result.
      expect(JSON.stringify(result)).not.toContain(ADMIN_TOKEN);
    });

    it('an explicit validated:true registration WITH the correct token sets VALIDATED and moves the request to RESEARCH_COMPLETED', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-2-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
        { run: { success: true } },
        { run: { success: true } },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-2-1', findings: validFindings, validated: true, rawResponseText: rawText, providedToken: ADMIN_TOKEN }
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
        envOf(db), { requestId: 'stage7-req-3-1', findings: validFindings, rawResponseText: rawText, providedToken: ADMIN_TOKEN }
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
        envOf(db), { requestId: 'stage7-req-4-1', findings: validFindings, sources: 'not-an-array', rawResponseText: rawText, providedToken: ADMIN_TOKEN }
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
        envOf(db), { requestId: 'stage7-req-5-1', findings: sneaky, rawResponseText: rawText, providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(true);
      const insertCall = db.calls[2];
      expect(insertCall.args).toContain(JSON.stringify(sneaky));
      // Never a second statement, never string-interpolated into the SQL itself.
      expect(insertCall.sql).not.toContain('DROP TABLE');
      expect(insertCall.sql.match(/;/g) || []).toHaveLength(0);
    });

    it('raw_response_text is required -- rejected with 400 before any write when missing', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-6-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-6-1', findings: validFindings, providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(400);
      expect(result.error).toMatch(/raw_response_text/);
      expect(db.calls).toHaveLength(2); // no INSERT/UPDATE attempted
    });

    it('raw_response_text of only whitespace is also rejected', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-6-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db), { requestId: 'stage7-req-6-1', findings: validFindings, rawResponseText: '   ', providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(400);
    });

    it('"do not treat a checkbox alone as proof every source is valid": validated:true with every source failing objective validation is refused BEFORE any write', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-7-1', status: 'RESEARCH_REQUEST_PUBLISHED', historical_cutoff_ts: 1000 } },
        { first: null },
      ]);
      const badSources = [{ url: 'not a url at all' }, { url: '' }];
      const result = await scope.registerStage7ResearchResponse(
        envOf(db),
        { requestId: 'stage7-req-7-1', findings: validFindings, sources: badSources, validated: true, rawResponseText: rawText, providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(false);
      expect(result.status).toBe(422);
      expect(result.source_validation.valid_count).toBe(0);
      expect(db.calls).toHaveLength(2); // never inserted -- no dead end, same request_id can be resubmitted
    });

    it('validated:true with at least one genuinely valid source succeeds and stores the objective source_validation verdict', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-8-1', status: 'RESEARCH_REQUEST_PUBLISHED', historical_cutoff_ts: 2_000_000_000_000 } },
        { first: null },
        { run: { success: true } },
        { run: { success: true } },
      ]);
      const sources = [{ url: 'https://example.com/a', publication_date: '2020-01-01', publisher: 'X', claim: 'c' }];
      const result = await scope.registerStage7ResearchResponse(
        envOf(db),
        { requestId: 'stage7-req-8-1', findings: validFindings, sources, validated: true, rawResponseText: rawText, providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(true);
      expect(result.validation_status).toBe('VALIDATED');
      expect(result.source_validation.valid_count).toBe(1);
      const insertCall = db.calls[2];
      expect(insertCall.args).toContain(rawText);
      expect(insertCall.sql).toMatch(/raw_response_text/);
      expect(insertCall.sql).toMatch(/source_validation_json/);
    });

    it('validated:true with an EMPTY sources list (a genuine no-citations finding) is never blocked', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-9-1', status: 'RESEARCH_REQUEST_PUBLISHED', historical_cutoff_ts: 1000 } },
        { first: null },
        { run: { success: true } },
        { run: { success: true } },
      ]);
      const result = await scope.registerStage7ResearchResponse(
        envOf(db),
        { requestId: 'stage7-req-9-1', findings: validFindings, sources: [], validated: true, rawResponseText: rawText, providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(true);
      expect(result.validation_status).toBe('VALIDATED');
    });
  });

  describe('buildStage7ResearchPromptText (the ONE canonical prompt implementation)', () => {
    function candidate(overrides = {}) {
      return {
        event_category: 'LARGE_MOVE', event_ts: 1_000_000, historical_cutoff_ts: 1_000_000,
        reasons: ['no evidence rows exist'], questions: ['what happened?'],
        missing_categories: ['primary_reporting'],
        evidence_snapshot: [{ headline: 'BTC drops on ETF news', publisher: 'Reuters', article_url: 'https://example.com/a' }],
        created_ts: 1_000_000 + 3 * 86_400_000, // 3 days after the event
        ...overrides,
      };
    }

    it('includes event context, category, and timestamp', () => {
      const text = scope.buildStage7ResearchPromptText(candidate());
      expect(text).toContain('LARGE_MOVE');
      expect(text).toContain('1000000');
    });

    it('includes the event age in days, computed from created_ts - event_ts', () => {
      const text = scope.buildStage7ResearchPromptText(candidate());
      expect(text).toMatch(/approximately 3 day\(s\)/);
    });

    it('states the historical cutoff explicitly and distinguishes it from a source publication date and a retrieval date', () => {
      const text = scope.buildStage7ResearchPromptText(candidate());
      expect(text).toContain('historical cutoff');
      expect(text.toLowerCase()).toContain('publication date');
      expect(text.toLowerCase()).toContain('retrieval');
    });

    it('includes reasons, missing categories, and questions verbatim', () => {
      const text = scope.buildStage7ResearchPromptText(candidate());
      expect(text).toContain('no evidence rows exist');
      expect(text).toContain('primary_reporting');
      expect(text).toContain('what happened?');
    });

    it('includes existing evidence (Task 3.B "existing evidence") and instructs not to merely repeat it', () => {
      const text = scope.buildStage7ResearchPromptText(candidate());
      expect(text).toContain('BTC drops on ETF news');
      expect(text).toContain('Reuters');
      expect(text.toLowerCase()).toContain('do not simply repeat');
    });

    it('states plainly when no prior evidence exists at all', () => {
      const text = scope.buildStage7ResearchPromptText(candidate({ evidence_snapshot: [] }));
      expect(text.toLowerCase()).toContain('no prior evidence has been collected');
    });

    it('includes source and date requirements', () => {
      const text = scope.buildStage7ResearchPromptText(candidate());
      expect(text.toLowerCase()).toContain('verifiable url');
      expect(text.toLowerCase()).toContain('publication date');
    });

    it('instructs distinguishing sourced facts, third-party claims, inference, and uncertainty', () => {
      const text = scope.buildStage7ResearchPromptText(candidate());
      expect(text.toLowerCase()).toContain('sourced facts');
      expect(text.toLowerCase()).toContain('inference');
      expect(text.toLowerCase()).toContain('uncertainty');
    });

    it('documents a JSON response contract naming all four sentiment labels and a sources array shape', () => {
      const text = scope.buildStage7ResearchPromptText(candidate());
      expect(text).toContain('"sentiment_assessment"');
      expect(text).toContain('POSITIVE');
      expect(text).toContain('NEGATIVE');
      expect(text).toContain('MIXED');
      expect(text).toContain('INDETERMINATE');
      expect(text).toContain('"sources"');
      expect(text).toContain('"publication_date"');
    });

    it('is a pure function of its input -- the same candidate always produces the identical prompt', () => {
      const c = candidate();
      expect(scope.buildStage7ResearchPromptText(c)).toBe(scope.buildStage7ResearchPromptText(c));
    });
  });

  describe('stage7IsHttpUrl / stage7ParseSourcePublicationTs', () => {
    it('accepts http and https urls', () => {
      expect(scope.stage7IsHttpUrl('https://example.com/a')).toBe(true);
      expect(scope.stage7IsHttpUrl('http://example.com/a')).toBe(true);
    });
    it('rejects non-http(s) protocols and malformed strings', () => {
      expect(scope.stage7IsHttpUrl('ftp://example.com/a')).toBe(false);
      expect(scope.stage7IsHttpUrl('javascript:alert(1)')).toBe(false);
      expect(scope.stage7IsHttpUrl('not a url')).toBe(false);
      expect(scope.stage7IsHttpUrl('')).toBe(false);
      expect(scope.stage7IsHttpUrl(null)).toBe(false);
    });

    it('parses a numeric ms-epoch as-is', () => {
      expect(scope.stage7ParseSourcePublicationTs(1000)).toBe(1000);
    });
    it('parses a bare date as UTC midnight, never local time', () => {
      expect(scope.stage7ParseSourcePublicationTs('2020-01-01')).toBe(Date.parse('2020-01-01T00:00:00Z'));
    });
    it('parses an explicit-offset datetime string using that offset', () => {
      expect(scope.stage7ParseSourcePublicationTs('2020-01-01T00:00:00+02:00')).toBe(Date.parse('2020-01-01T00:00:00+02:00'));
    });
    it('returns null for missing/unparseable/boolean input, never a fabricated guess', () => {
      expect(scope.stage7ParseSourcePublicationTs(null)).toBeNull();
      expect(scope.stage7ParseSourcePublicationTs('')).toBeNull();
      expect(scope.stage7ParseSourcePublicationTs('garbage')).toBeNull();
      expect(scope.stage7ParseSourcePublicationTs(true)).toBeNull();
    });
  });

  describe('validateStage7Sources', () => {
    const CUTOFF = 1_700_000_000_000;

    it('a well-formed, dated, pre-cutoff source is valid', () => {
      const result = scope.validateStage7Sources([{ url: 'https://example.com/a', publication_date: '2020-01-01' }], CUTOFF);
      expect(result.valid_count).toBe(1);
      expect(result.results[0].status).toBe('valid');
    });

    it('missing/blank url is excluded', () => {
      const result = scope.validateStage7Sources([{ publication_date: '2020-01-01' }, { url: '  ' }], CUTOFF);
      expect(result.excluded_count).toBe(2);
      expect(result.results.every((r) => r.status === 'excluded')).toBe(true);
      expect(result.results[0].reason).toMatch(/verifiable url/);
    });

    it('a non-http(s) or malformed url is excluded, distinctly from "missing"', () => {
      const result = scope.validateStage7Sources([{ url: 'ftp://example.com/a', publication_date: '2020-01-01' }], CUTOFF);
      expect(result.excluded_count).toBe(1);
      expect(result.results[0].reason).toMatch(/not a valid http/);
    });

    it('missing or unparseable publication date is excluded', () => {
      const result = scope.validateStage7Sources([{ url: 'https://example.com/a' }, { url: 'https://example.com/b', publication_date: 'garbage' }], CUTOFF);
      expect(result.excluded_count).toBe(2);
      expect(result.results.map((r) => r.reason)).toEqual(
        expect.arrayContaining([expect.stringMatching(/missing or unparseable/), expect.stringMatching(/missing or unparseable/)])
      );
    });

    it('a source published after the historical cutoff is excluded, distinctly from a future-dated one', () => {
      const result = scope.validateStage7Sources([{ url: 'https://example.com/a', publication_date: CUTOFF + 1 }], CUTOFF);
      expect(result.excluded_count).toBe(1);
      expect(result.results[0].reason).toMatch(/after the historical cutoff/);
    });

    it('a source dated in the future (relative to now) is excluded with its own distinct reason', () => {
      const farFuture = Date.now() + 365 * 86_400_000;
      const result = scope.validateStage7Sources([{ url: 'https://example.com/a', publication_date: farFuture }], farFuture + 1);
      expect(result.excluded_count).toBe(1);
      expect(result.results[0].reason).toMatch(/future/);
    });

    it('a source published exactly at the cutoff is eligible (inclusive boundary)', () => {
      const result = scope.validateStage7Sources([{ url: 'https://example.com/a', publication_date: CUTOFF }], CUTOFF);
      expect(result.valid_count).toBe(1);
    });

    it('an exact duplicate url within the same submission is excluded on the second occurrence, kept on the first', () => {
      const result = scope.validateStage7Sources([
        { url: 'https://example.com/a', publication_date: '2020-01-01' },
        { url: 'https://example.com/a', publication_date: '2020-01-02' },
      ], CUTOFF);
      expect(result.results[0].status).toBe('valid');
      expect(result.results[1].status).toBe('excluded');
      expect(result.results[1].reason).toMatch(/duplicate url/);
    });

    it('the SAME claim text from two DIFFERENT urls survives as "questionable" independent corroboration, never excluded', () => {
      const result = scope.validateStage7Sources([
        { url: 'https://a.example.com/x', publication_date: '2020-01-01', claim: 'price moved because of X' },
        { url: 'https://b.example.com/y', publication_date: '2020-01-02', claim: 'price moved because of X' },
      ], CUTOFF);
      expect(result.results[0].status).toBe('valid');
      expect(result.results[1].status).toBe('questionable');
      expect(result.valid_count).toBe(1);
      expect(result.questionable_count).toBe(1);
      expect(result.excluded_count).toBe(0);
    });

    it('a malformed (non-object) entry is excluded, never throws', () => {
      const result = scope.validateStage7Sources(['not-an-object', 42, null], CUTOFF);
      expect(result.excluded_count).toBe(3);
    });

    it('empty/non-array input produces no results and never throws', () => {
      expect(scope.validateStage7Sources([], CUTOFF)).toEqual({ results: [], valid_count: 0, excluded_count: 0, questionable_count: 0 });
      expect(scope.validateStage7Sources(null, CUTOFF).results).toEqual([]);
      expect(scope.validateStage7Sources(undefined, CUTOFF).results).toEqual([]);
    });

    it('never claims a url was fetched or a claim independently verified -- only structural facts', () => {
      // Documentation-level guarantee: every possible reason string this
      // function can produce avoids words implying content was actually
      // retrieved/verified.
      const result = scope.validateStage7Sources([{ url: 'not valid' }], CUTOFF);
      for (const r of result.results) {
        expect(r.reason || '').not.toMatch(/verified|fetched|confirmed true/i);
      }
    });
  });

  describe('createStage7ResearchRequests', () => {
    it('STAGE7_ADMIN_TOKEN not configured: 503, no DB call', async () => {
      const db = makeDb([]);
      const result = await scope.createStage7ResearchRequests({ DB: db }, { candidateIds: ['stage7-cand-1'], providedToken: 'x' });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(503);
      expect(db.calls).toHaveLength(0);
    });

    it('wrong token: 401, no DB call', async () => {
      const db = makeDb([]);
      const result = await scope.createStage7ResearchRequests(envOf(db), { candidateIds: ['stage7-cand-1'], providedToken: 'wrong' });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(401);
      expect(db.calls).toHaveLength(0);
    });

    it('empty candidate_ids is rejected with 400', async () => {
      const db = makeDb([]);
      const result = await scope.createStage7ResearchRequests(envOf(db), { candidateIds: [], providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(400);
    });

    it('more than 50 candidate_ids in one call is rejected with 400', async () => {
      const db = makeDb([]);
      const ids = Array.from({ length: 51 }, (_, i) => `stage7-cand-${i}`);
      const result = await scope.createStage7ResearchRequests(envOf(db), { candidateIds: ids, providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(400);
    });

    it('a non-existent candidate is skipped with a clear reason, never crashes the batch', async () => {
      const db = makeDb([{ first: null }]);
      const result = await scope.createStage7ResearchRequests(envOf(db), { candidateIds: ['stage7-cand-999'], providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(true);
      expect(result.created).toEqual([]);
      expect(result.skipped).toEqual([{ candidate_id: 'stage7-cand-999', reason: 'no such candidate' }]);
    });

    it('an already-CONVERTED candidate is skipped, never double-converted', async () => {
      const db = makeDb([{ first: { candidate_id: 'stage7-cand-1', event_id: 1, status: 'CONVERTED' } }]);
      const result = await scope.createStage7ResearchRequests(envOf(db), { candidateIds: ['stage7-cand-1'], providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(true);
      expect(result.created).toEqual([]);
      expect(result.skipped[0].reason).toMatch(/already CONVERTED/);
    });

    it('a PROPOSED candidate is converted: request INSERTed as PENDING_RESEARCH with a generated prompt, candidate marked CONVERTED', async () => {
      const db = makeDb([
        { first: {
          candidate_id: 'stage7-cand-1', event_id: 1, status: 'PROPOSED', sufficiency_status: 'INSUFFICIENT_EVIDENCE',
          reasons_json: '["r"]', questions_json: '["q"]', missing_categories_json: '["m"]',
          historical_cutoff_ts: 1000, evidence_snapshot_json: '[]', input_fingerprint: 'fp-1',
        } },
        { first: { category: 'LARGE_MOVE', event_ts: 1000 } }, // research_events lookup
        { run: { success: true } }, // INSERT stage7_research_requests
        { run: { success: true } }, // UPDATE stage7_research_candidates
      ]);
      const result = await scope.createStage7ResearchRequests(envOf(db), { candidateIds: ['stage7-cand-1'], providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(true);
      expect(result.created).toEqual([{ candidate_id: 'stage7-cand-1', request_id: 'stage7-req-1-1', event_id: 1, prompt_text: expect.any(String) }]);
      expect(result.skipped).toEqual([]);

      const insertCall = db.calls[2];
      expect(insertCall.sql).toMatch(/INSERT INTO stage7_research_requests/);
      expect(insertCall.args).toContain('PENDING_RESEARCH');
      expect(insertCall.args).toContain('stage7-cand-1'); // candidate_id column
      expect(insertCall.args).toContain(0); // publish_attempts starts at 0 -- never attempted yet
      expect(insertCall.args).toContain('fp-1'); // reuses the candidate's own input_fingerprint

      const updateCall = db.calls[3];
      expect(updateCall.sql).toMatch(/UPDATE stage7_research_candidates SET status = 'CONVERTED'/);
      expect(updateCall.args).toContain('stage7-req-1-1');
      expect(updateCall.args).toContain('stage7-cand-1');
    });

    it('a concurrent duplicate creation (D1 unique-index violation on INSERT) is caught and reported per-item, never a 500 for the whole batch', async () => {
      const db = {
        calls: [],
        prepare(sql) {
          db.calls.push(sql);
          return {
            bind: () => ({
              first: async () => {
                if (sql.includes('FROM stage7_research_candidates')) {
                  return {
                    candidate_id: 'stage7-cand-1', event_id: 1, status: 'PROPOSED', sufficiency_status: 'INSUFFICIENT',
                    reasons_json: '[]', questions_json: '[]', missing_categories_json: '[]',
                    historical_cutoff_ts: 1000, evidence_snapshot_json: '[]', input_fingerprint: 'fp',
                  };
                }
                if (sql.includes('FROM research_events')) return { category: 'LARGE_MOVE', event_ts: 1000 };
                return null;
              },
              run: async () => {
                if (sql.includes('INSERT INTO stage7_research_requests')) {
                  throw new Error('UNIQUE constraint failed: stage7_research_requests.event_id');
                }
                return { success: true };
              },
            }),
          };
        },
      };
      const result = await scope.createStage7ResearchRequests(envOf(db), { candidateIds: ['stage7-cand-1'], providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(true);
      expect(result.created).toEqual([]);
      expect(result.skipped).toHaveLength(1);
      expect(result.skipped[0].reason).toMatch(/UNIQUE constraint/);
      // The candidate's own status update must never be attempted after a failed insert.
      expect(db.calls.some((sql) => sql.includes("SET status = 'CONVERTED'"))).toBe(false);
    });

    it('duplicate candidate_ids in one call are de-duplicated before processing', async () => {
      const db = makeDb([{ first: null }]); // only ONE lookup should happen
      const result = await scope.createStage7ResearchRequests(
        envOf(db), { candidateIds: ['stage7-cand-1', 'stage7-cand-1'], providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(true);
      expect(db.calls).toHaveLength(1);
    });
  });

  describe('triggerStage7Recalculation', () => {
    it('STAGE7_ADMIN_TOKEN not configured: 503, no DB call', async () => {
      const db = makeDb([]);
      const result = await scope.triggerStage7Recalculation({ DB: db }, { requestId: 'stage7-req-1-1', providedToken: 'x' });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(503);
      expect(db.calls).toHaveLength(0);
    });

    it('wrong token: 401, no DB call', async () => {
      const db = makeDb([]);
      const result = await scope.triggerStage7Recalculation(envOf(db), { requestId: 'stage7-req-1-1', providedToken: 'wrong' });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(401);
      expect(db.calls).toHaveLength(0);
    });

    it('unknown request_id: 404', async () => {
      const db = makeDb([{ first: null }]);
      const result = await scope.triggerStage7Recalculation(envOf(db), { requestId: 'stage7-req-1-1', providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(404);
    });

    it('a terminal request refuses recalculation', async () => {
      const db = makeDb([{ first: { request_id: 'stage7-req-1-1', status: 'INTEGRATED' } }]);
      const result = await scope.triggerStage7Recalculation(envOf(db), { requestId: 'stage7-req-1-1', providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(409);
    });

    it('a request with no registered response yet refuses -- nothing to recalculate from', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-1-1', status: 'RESEARCH_REQUEST_PUBLISHED' } },
        { first: null },
      ]);
      const result = await scope.triggerStage7Recalculation(envOf(db), { requestId: 'stage7-req-1-1', providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(409);
      expect(result.error).toMatch(/no registered response/);
    });

    it('a PENDING (not yet validated) response refuses -- a human must validate first', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-1-1', status: 'RESEARCH_RESPONSE_RECEIVED' } },
        { first: { response_id: 'stage7-resp-1', validation_status: 'PENDING' } },
      ]);
      const result = await scope.triggerStage7Recalculation(envOf(db), { requestId: 'stage7-req-1-1', providedToken: ADMIN_TOKEN });
      expect(result.ok).toBe(false);
      expect(result.status).toBe(409);
      expect(result.error).toMatch(/not VALIDATED/);
    });

    it('a VALIDATED response succeeds: sets recalculation_requested_ts, and explicitly notes recalculation has NOT happened yet', async () => {
      const db = makeDb([
        { first: { request_id: 'stage7-req-1-1', status: 'RESEARCH_COMPLETED' } },
        { first: { response_id: 'stage7-resp-1', validation_status: 'VALIDATED' } },
        { run: { success: true } },
      ]);
      const result = await scope.triggerStage7Recalculation(
        envOf(db), { requestId: 'stage7-req-1-1', requestedBy: 'olivier', providedToken: ADMIN_TOKEN }
      );
      expect(result.ok).toBe(true);
      expect(result.recalculation_requested_ts).toEqual(expect.any(Number));
      expect(result.note).toMatch(/FLAGGED, not performed/);
      const updateCall = db.calls[2];
      expect(updateCall.sql).toMatch(/UPDATE stage7_research_requests SET recalculation_requested_ts/);
      expect(updateCall.args).toContain('olivier');
      expect(updateCall.sql).toMatch(/status NOT IN \('INTEGRATED','REJECTED'\)/);
    });
  });

  describe('deriveStage7RequestLifecycleStage', () => {
    it('maps every status/response combination to exactly the documented Task F stage', () => {
      const f = scope.deriveStage7RequestLifecycleStage;
      expect(f({ status: 'FAILED_PERMANENT', response_received: false })).toBe('ERROR_PERMANENT');
      expect(f({ status: 'FAILED_RETRYABLE', response_received: false })).toBe('ERROR_RETRYABLE');
      expect(f({ status: 'INTEGRATION_REVIEW', response_received: true, response_validation_status: 'VALIDATED' })).toBe('RECALCULATED');
      expect(f({ status: 'INTEGRATED', response_received: true })).toBe('HUMAN_REVIEW_OUTCOME');
      expect(f({ status: 'APPROVED_FOR_IMPLEMENTATION', response_received: true })).toBe('HUMAN_REVIEW_OUTCOME');
      expect(f({ status: 'REJECTED', response_received: false })).toBe('HUMAN_REVIEW_OUTCOME');
      expect(f({ status: 'PENDING_RESEARCH', response_received: false })).toBe('REQUEST_CREATED');
      expect(f({ status: 'RESEARCH_REQUEST_PUBLISHED', response_received: false })).toBe('AWAITING_RESEARCH');
      expect(f({ status: 'RESEARCH_RESPONSE_RECEIVED', response_received: true, response_validation_status: 'PENDING' })).toBe('RESPONSE_REGISTERED');
      expect(f({ status: 'RESEARCH_RESPONSE_RECEIVED', response_received: true, response_validation_status: 'REJECTED' })).toBe('RESPONSE_REJECTED');
      expect(f({ status: 'RESEARCH_COMPLETED', response_received: true, response_validation_status: 'VALIDATED', recalculation_requested_ts: null })).toBe('VALIDATED');
      expect(f({ status: 'RESEARCH_COMPLETED', response_received: true, response_validation_status: 'VALIDATED', recalculation_requested_ts: 123 })).toBe('AWAITING_RECALCULATION');
    });
  });

  // Adversarial-review remediation, finding #1: the Stage 7 UI must
  // explicitly disclose that its sentiment value is a distinct, per-event
  // assessment on Stage 7's own scale -- never presented as, or confusable
  // with, V1's market-wide composite. These are lightweight content-
  // presence assertions against the raw worker.js source (the same file
  // extractFunctions/extractConstants already read), not a full DOM
  // render -- they exist so a future edit that silently deletes this
  // disclosure fails a test, not just a manual read-through.
  describe('UI text: Stage 7 sentiment scale is explicitly distinguished from V1', () => {
    // NOTE ON BACKSLASHES: worker.js's client-side UI text lives inside a
    // backtick template literal (RESEARCH_LAB_HTML) whose own contents are
    // single-quoted JS strings, so a literal apostrophe in the rendered
    // page is written in worker.js's raw source as TWO backslashes plus a
    // quote ("\\\\'" below is exactly that, in a double-quoted string so
    // the apostrophe itself needs no escaping) -- the outer template
    // literal consumes one backslash, leaving a real "\'" for the client
    // script's own single-quoted string to correctly unescape in turn.
    // Matched with plain toContain() against the raw file, not toMatch()
    // with a regex, specifically to avoid a second, independent layer of
    // backslash reinterpretation.
    it("the Stage 7 tab states plainly that its score is not V1's composite", () => {
      expect(WORKER_JS_SOURCE).toContain("This is not V1\\\\'s composite score");
    });
    it("the Stage 7 tab discloses its own 0/50/100 scale and human-validated basis", () => {
      expect(WORKER_JS_SOURCE).toContain("Stage 7\\\\'s own 0/50/100 scale");
      expect(WORKER_JS_SOURCE).toContain('human-validated AI research response');
    });
    it("the Stage 7 tab states it never automatically changes V1, weights, or predictions", () => {
      expect(WORKER_JS_SOURCE).toContain("never changes V1\\\\'s market-wide composite");
      expect(WORKER_JS_SOURCE).toContain('global source weights, or any production prediction');
    });
    it('the recalculated-sentiment row label itself names Stage 7 and disclaims V1', () => {
      expect(WORKER_JS_SOURCE).toContain('Stage 7 event sentiment (not V1)');
    });
    it("the Dashboard's V1 Composite tile cross-references the Stage 7 distinction", () => {
      expect(WORKER_JS_SOURCE).toContain('V1 market-wide sentiment score (0-100) -- distinct from any Stage 7 per-event assessment');
    });
    it('the Pipeline flow-stage description for Stage 7 also discloses the separate-scale rule', () => {
      // This one line uses the file's own curly-apostrophe convention
      // (’, U+2019), not the backslash-escaped straight quote used
      // elsewhere in this template -- matched verbatim, not via a
      // wildcard, so a future edit that reverts to a straight quote
      // would also be caught.
      expect(WORKER_JS_SOURCE).toContain('Produces a separate, per-event score on Stage 7’s own 0/50/100 scale');
    });
  });
});
