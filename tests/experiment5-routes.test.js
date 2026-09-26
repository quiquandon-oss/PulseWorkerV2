import { describe, it, expect } from 'vitest';
import worker from '../worker.js';

// Route-dispatch tests for the six new Experiment 5 / Research Lab
// endpoints -- these exercise the ACTUAL `export default { fetch }`
// dispatcher (real Request objects, real url.pathname/method matching,
// real query-string parsing) rather than the extracted pure functions
// (already covered exhaustively in tests/experiment5-lab.test.js). This
// is the layer that was previously untested in this codebase (no
// existing test file imports worker.js's own fetch handler at all).
//
// A fake D1 that can either throw on every call (simulating migrations
// 0008/0015 not yet applied) or serve canned per-call responses while
// recording every call + bound args, mirroring
// tests/experiment5-lab.test.js's own makeDb() convention exactly, so
// route-level and function-level tests stay consistent.
function makeDb(responses, { alwaysThrow } = {}) {
  const calls = [];
  let callIndex = 0;
  return {
    calls,
    prepare(sql) {
      if (alwaysThrow) throw new Error('no such table: research_sentiment_archive');
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

async function call(path, { method = 'GET', db } = {}) {
  const request = new Request(`https://example.com${path}`, { method });
  const response = await worker.fetch(request, { DB: db });
  const body = await response.json();
  return { status: response.status, body };
}

const ROUTES = [
  '/api/research-lab/experiment5-overview',
  '/api/research-lab/experiment5-decisions',
  '/api/research-lab/experiment5-results',
  '/api/research-lab/experiment5-sentiment',
  '/api/research-lab/experiment5-source-intelligence',
  '/api/research-lab/timeline',
];

describe('Experiment 5 / Research Lab -- real route dispatch', () => {
  describe('every route: non-GET is rejected via the established convention', () => {
    for (const path of ROUTES) {
      for (const method of ['POST', 'PUT', 'DELETE', 'PATCH']) {
        it(`${method} ${path} -> 404 not_found (never dispatched, DB never touched)`, async () => {
          const db = makeDb([], { alwaysThrow: true }); // would throw if ever touched -- proves it wasn't
          const { status, body } = await call(path, { method, db });
          expect(status).toBe(404);
          expect(body).toEqual({ ok: false, error: 'not_found' });
        });
      }
    }
  });

  describe('every route: OPTIONS preflight still works (CORS untouched)', () => {
    it('OPTIONS to a new route returns CORS headers, no body error', async () => {
      const request = new Request('https://example.com/api/research-lab/experiment5-overview', { method: 'OPTIONS' });
      const response = await worker.fetch(request, { DB: makeDb([], { alwaysThrow: true }) });
      expect(response.status).toBe(200);
      expect(response.headers.get('Access-Control-Allow-Origin')).toBe('*');
    });
  });

  describe('GET /api/research-lab/experiment5-overview', () => {
    it('not activated: 200, honest degraded shape, narrative composed even in the degraded branch', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { status, body } = await call('/api/research-lab/experiment5-overview', { db });
      expect(status).toBe(200);
      expect(body.ok).toBe(true);
      expect(body.activated).toBe(false);
      expect(body.reason).toMatch(/not yet applied/);
      // The route handler composes narrative from BOTH
      // getResearchLabExperiment5Overview AND ...Results -- this
      // composition only exists inline in the fetch() dispatcher, so
      // this assertion is the one thing genuinely untestable at the
      // function-extraction layer.
      expect(Array.isArray(body.narrative)).toBe(true);
      expect(body.narrative[0]).toMatch(/not yet active in production/);
    });
  });

  describe('GET /api/research-lab/experiment5-decisions', () => {
    it('not activated: defaults limit=25, offset=0, empty list', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { status, body } = await call('/api/research-lab/experiment5-decisions', { db });
      expect(status).toBe(200);
      expect(body).toEqual({ ok: true, activated: false, total: 0, limit: 25, offset: 0, decisions: [] });
    });

    it('malformed limit ("abc") falls back to the default 25, never NaN', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { body } = await call('/api/research-lab/experiment5-decisions?limit=abc', { db });
      expect(body.limit).toBe(25);
    });

    it('negative limit is clamped to 1, never a negative or zero LIMIT', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { body } = await call('/api/research-lab/experiment5-decisions?limit=-5', { db });
      expect(body.limit).toBe(1);
    });

    it('excessive limit is clamped to 100, never an unbounded query', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { body } = await call('/api/research-lab/experiment5-decisions?limit=999999', { db });
      expect(body.limit).toBe(100);
    });

    it('negative offset is clamped to 0', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { body } = await call('/api/research-lab/experiment5-decisions?offset=-10', { db });
      expect(body.offset).toBe(0);
    });

    it('malformed offset ("xyz") falls back to the default 0', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { body } = await call('/api/research-lab/experiment5-decisions?offset=xyz', { db });
      expect(body.offset).toBe(0);
    });

    it('an invalid status value is rejected with 400 BEFORE any D1 call is attempted', async () => {
      const db = makeDb([], { alwaysThrow: true }); // would throw if touched -- proves the 400 short-circuits first
      const { status, body } = await call('/api/research-lab/experiment5-decisions?status=bogus', { db });
      expect(status).toBe(400);
      expect(body.ok).toBe(false);
      expect(body.error).toMatch(/status must be one of/);
      expect(body.error).toMatch(/pending/);
      expect(body.error).toMatch(/resolved/);
      expect(body.error).toMatch(/passed/);
      expect(body.error).toMatch(/failed/);
    });

    it.each(['pending', 'resolved', 'passed', 'failed'])(
      'a valid status value (%s) passes validation and reaches the handler',
      async (status) => {
        const db = makeDb([], { alwaysThrow: true }); // proves it got past validation and into the (degraded) handler
        const { status: httpStatus, body } = await call(`/api/research-lab/experiment5-decisions?status=${status}`, { db });
        expect(httpStatus).toBe(200);
        expect(body.activated).toBe(false); // reached the handler, which then degraded honestly
      }
    );

    it('an empty string status ("?status=") is treated as no filter, not rejected', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { status } = await call('/api/research-lab/experiment5-decisions?status=', { db });
      expect(status).toBe(200); // empty string is falsy -> `undefined`, matching "all", never a 400
    });

    it('populated + activated: response shape, and status filter is actually applied to the WHERE clause via the real dispatcher', async () => {
      const db = makeDb([
        { first: { n: 1 } },
        { all: { results: [{
          hypothesis_id: 7, created_ts: 1000, last_updated_ts: 2000, subject: 'experiment5:reversal:fng',
          statement: 'x', status: 'OBSERVATION', out_of_sample_status: 'PASSED_HOLDOUT',
          evidence_summary_json: JSON.stringify({
            decision: { anchor_ts: 1000, primary_source: 'fng', direction: 1, target_horizon_hours: 24, eligible_ts: 1000 + 86400000 },
            outcome: { realized_direction: 'UP' },
          }),
        }] } },
      ]);
      const { status, body } = await call('/api/research-lab/experiment5-decisions?status=passed&limit=10&offset=0', { db });
      expect(status).toBe(200);
      expect(body.activated).toBe(true);
      expect(body.decisions).toHaveLength(1);
      expect(body.decisions[0].hypothesis_id).toBe(7);
      expect(body.decisions[0].eligibility).toBe('RESOLVED');
      const rowQuery = db.calls.find((c) => /LIMIT \? OFFSET \?/.test(c.sql));
      expect(rowQuery.sql).toContain("out_of_sample_status = 'PASSED_HOLDOUT'");
      expect(rowQuery.args).toEqual([10, 0]);
    });
  });

  describe('GET /api/research-lab/experiment5-results', () => {
    it('not activated: 200, honest degraded shape', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { status, body } = await call('/api/research-lab/experiment5-results', { db });
      expect(status).toBe(200);
      expect(body).toEqual({ ok: true, activated: false, n_resolved: 0 });
    });

    it('activated, zero resolved: real empty-population shape via the real dispatcher', async () => {
      const db = makeDb([{ all: { results: [] } }]);
      const { status, body } = await call('/api/research-lab/experiment5-results', { db });
      expect(status).toBe(200);
      expect(body.activated).toBe(true);
      expect(body.n_resolved).toBe(0);
      expect(body.sufficient_sample).toBe(false);
    });
  });

  describe('GET /api/research-lab/experiment5-sentiment', () => {
    it('not activated: 200, honest degraded shape, default since_ts near "30 days ago"', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const before = Date.now();
      const { status, body } = await call('/api/research-lab/experiment5-sentiment', { db });
      expect(status).toBe(200);
      expect(body.activated).toBe(false);
      expect(body.series).toEqual([]);
      expect(body.since_ts).toBeGreaterThan(before - 31 * 24 * 3600000);
      expect(body.since_ts).toBeLessThanOrEqual(before);
    });

    it('an explicit since_ts is honored exactly', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { body } = await call('/api/research-lab/experiment5-sentiment?since_ts=12345', { db });
      expect(body.since_ts).toBe(12345);
    });

    it('a malformed since_ts ("abc") falls back to the default window, never NaN', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { body } = await call('/api/research-lab/experiment5-sentiment?since_ts=abc', { db });
      expect(Number.isFinite(body.since_ts)).toBe(true);
      expect(Number.isNaN(body.since_ts)).toBe(false);
    });

    it('a negative since_ts is treated as invalid (falls back to the default window), never crashes and never used literally', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const before = Date.now();
      const { status, body } = await call('/api/research-lab/experiment5-sentiment?since_ts=-500', { db });
      expect(status).toBe(200);
      // getResearchLabExperiment5SentimentSeries only accepts a positive
      // sinceTs; a negative one is the same as "absent" -- confirmed via
      // the real dispatcher, not just the function's own unit test.
      expect(body.since_ts).toBeGreaterThan(before - 31 * 24 * 3600000);
      expect(body.since_ts).toBeLessThanOrEqual(before);
    });
  });

  describe('GET /api/research-lab/experiment5-source-intelligence', () => {
    it('not activated: 200, honest degraded shape', async () => {
      const db = makeDb([], { alwaysThrow: true });
      const { status, body } = await call('/api/research-lab/experiment5-source-intelligence', { db });
      expect(status).toBe(200);
      expect(body).toEqual({ ok: true, activated: false, sources: [], candidate_new_sources: [] });
    });
  });

  describe('GET /api/research-lab/timeline', () => {
    it('empty datasets on both sub-queries: 200, empty items, never an error', async () => {
      const db = makeDb([{ all: { results: [] } }, { all: { results: [] } }]);
      const { status, body } = await call('/api/research-lab/timeline', { db });
      expect(status).toBe(200);
      expect(body).toEqual({ ok: true, items: [] });
    });

    it('default limit (50) is bound to both underlying queries', async () => {
      const db = makeDb([{ all: { results: [] } }, { all: { results: [] } }]);
      await call('/api/research-lab/timeline', { db });
      expect(db.calls[0].args).toEqual([50]);
      expect(db.calls[1].args).toEqual([50]);
    });

    it('malformed limit ("abc") falls back to the default 50', async () => {
      const db = makeDb([{ all: { results: [] } }, { all: { results: [] } }]);
      await call('/api/research-lab/timeline?limit=abc', { db });
      expect(db.calls[0].args).toEqual([50]);
    });

    it('negative limit is clamped to 1, never a negative LIMIT bound', async () => {
      const db = makeDb([{ all: { results: [] } }, { all: { results: [] } }]);
      await call('/api/research-lab/timeline?limit=-5', { db });
      expect(db.calls[0].args).toEqual([1]);
    });

    it('excessive limit is clamped to 200, never an unbounded query', async () => {
      const db = makeDb([{ all: { results: [] } }, { all: { results: [] } }]);
      await call('/api/research-lab/timeline?limit=999999', { db });
      expect(db.calls[0].args).toEqual([200]);
    });

    it('research_events unavailable but research_hypotheses activated: partial degradation still returns ok:true with the data that IS available', async () => {
      // First prepare() call (research_events) throws; the second table's
      // own try/catch is independent, so its real data still comes
      // through -- proves the two failure paths are genuinely isolated,
      // not a route-level catch-all masking a partial success as a total
      // failure.
      let callCount = 0;
      const db = {
        prepare(sql) {
          callCount++;
          if (callCount === 1) throw new Error('no such table: research_events');
          return {
            bind: () => ({ all: async () => ({ results: [
              { hypothesis_id: 1, created_ts: 1000, last_updated_ts: 1000, subject: 'experiment5:reversal:fng', out_of_sample_status: null },
            ] }) }),
          };
        },
      };
      const { status, body } = await call('/api/research-lab/timeline', { db });
      expect(status).toBe(200);
      expect(body.ok).toBe(true);
      expect(body.items).toHaveLength(1);
      expect(body.items[0].kind).toBe('EXPERIMENT5_DECISION_CREATED');
    });
  });
});
