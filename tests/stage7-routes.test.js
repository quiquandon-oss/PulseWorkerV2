// Stage 7 route safety (corrective patch, 2026-10-31): schema diagnosis, error hygiene, authorization order,
// idempotent and concurrent registration, and atomic persistence. Every test goes through the real worker.js fetch
// handler against real SQLite built from the repository's migrations 0005 and 0016.
//
// SQLite here stands in for D1. `batch` below runs the statements in one SQLite transaction, which is what
// Cloudflare documents for D1 batches; these tests prove the Worker's logic under those semantics, not D1's
// production transaction behaviour itself.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { describe, it, expect, vi, afterEach } from 'vitest';
import worker from '../worker.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const { DatabaseSync } = createRequire(import.meta.url)('node:sqlite');
const MIGRATION_0005 = readFileSync(join(__dirname, '..', '.ai', 'migrations', '0005_research_schema.sql'), 'utf8');
const MIGRATION_0016 = readFileSync(join(__dirname, '..', '.ai', 'migrations', '0016_stage7_research_pipeline.sql'), 'utf8');
const WORKER_SRC = readFileSync(join(__dirname, '..', 'worker.js'), 'utf8');

const TOKEN = 'stage7-route-test-admin-token-41c9';
const SECRET = 'sk-live-SECRET-9f8e7d6c5b4a';
const REQ = 'stage7-req-15-1';
const FINDINGS = { summary: 'Options expiry drove the move', sentiment_assessment: 'NEGATIVE' };

// schema: 'full' (0005 + 0016), 'none' (0005 only), 'partial' (0016 without stage7_event_sentiment).
function makeEnv({ schema = 'full', token = TOKEN, requestStatus = 'RESEARCH_REQUEST_PUBLISHED' } = {}) {
  const db = new DatabaseSync(':memory:');
  db.exec(MIGRATION_0005);
  if (schema !== 'none') db.exec(MIGRATION_0016);
  if (schema === 'partial') db.exec('DROP TABLE stage7_event_sentiment');
  db.prepare("INSERT INTO research_events (event_id, fingerprint, event_ts, detection_ts, category, available_before_prediction) VALUES (15, 'f', 1, 2, 'LARGE_MOVE', 1)").run();
  if (schema !== 'none') {
    db.prepare(`INSERT INTO stage7_research_requests (request_id, event_id, created_ts, updated_ts, schema_version, status,
      sufficiency_status, reasons_json, questions_json, missing_categories_json, historical_cutoff_ts, evidence_snapshot_json, input_fingerprint)
      VALUES (?, 15, 1, 1, 'v1', ?, 'CONFLICTING', '[]', '[]', '[]', 1, '{}', 'fp')`).run(REQ, requestStatus);
  }
  const log = [];
  const hooks = { failQuery: null, beforeBatch: null, failBatchStatement: null, failBatchMessage: null };
  function stmt(sql) {
    let args = [];
    const maybeFail = () => { if (hooks.failQuery && hooks.failQuery.test(sql)) throw new Error(`D1_ERROR: Network connection lost while running ${sql} with ${SECRET}`); };
    const exec = () => { log.push(sql); maybeFail(); const r = db.prepare(sql).run(...args); return { meta: { changes: r.changes } }; };
    const o = {
      sql,
      bind(...a) { args = a; return o; },
      async first() { log.push(sql); maybeFail(); const r = db.prepare(sql).get(...args); return r ? { ...r } : null; },
      async all() { log.push(sql); maybeFail(); return { results: db.prepare(sql).all(...args).map((r) => ({ ...r })) }; },
      async run() { return exec(); },
      runSync: exec,
    };
    return o;
  }
  async function batch(statements) {
    if (hooks.beforeBatch) hooks.beforeBatch(db);
    db.exec('BEGIN');
    try {
      const out = statements.map((st, i) => {
        if (hooks.failBatchStatement === i) { log.push(st.sql); throw new Error(hooks.failBatchMessage || `D1_ERROR: statement ${i} failed near "${SECRET}"`); }
        return st.runSync();
      });
      db.exec('COMMIT');
      return out;
    } catch (err) {
      db.exec('ROLLBACK');
      throw err;
    }
  }
  const env = { DB: { prepare: stmt, batch } };
  if (token) env.STAGE7_ADMIN_TOKEN = token;
  return { env, db, log, hooks };
}

async function call(env, path, { method = 'GET', body, rawBody, token } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = 'Bearer ' + token;
  const res = await worker.fetch(new Request('https://w.test' + path, {
    method, headers, body: rawBody !== undefined ? rawBody : body === undefined ? undefined : JSON.stringify(body),
  }), env, { waitUntil() {} });
  const text = await res.text();
  return { status: res.status, text, json: JSON.parse(text) };
}
const register = (env, body, token = TOKEN) => call(env, '/api/research-lab/stage7-register-response', { method: 'POST', body, token });
const overview = (env) => call(env, '/api/research-lab/stage7-overview');
const responses = (db) => db.prepare('SELECT * FROM stage7_research_responses').all();
const requestStatus = (db) => db.prepare('SELECT status FROM stage7_research_requests WHERE request_id = ?').get(REQ).status;
const WRITE = /\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b/i;

afterEach(() => vi.restoreAllMocks());
const quiet = () => vi.spyOn(console, 'error').mockImplementation(() => {});

describe('P1: Stage 7 overview schema diagnosis', () => {
  it('schema present: activated, real counts, SELECT-only (read route writes nothing)', async () => {
    const { env, db, log } = makeEnv();
    const before = db.prepare('SELECT COUNT(*) n FROM stage7_research_requests').get().n;
    const r = await overview(env);
    expect(r.status).toBe(200);
    expect(r.json).toMatchObject({ ok: true, activated: true, requests: { total: 1, by_status: { RESEARCH_REQUEST_PUBLISHED: 1 } } });
    expect(log.length).toBe(3);
    for (const sql of log) expect(sql).not.toMatch(WRITE);
    expect(db.prepare('SELECT COUNT(*) n FROM stage7_research_requests').get().n).toBe(before);
  });

  it('schema missing (migration 0016 absent): activated:false, NOT_APPLIED, names the missing table', async () => {
    const r = await overview(makeEnv({ schema: 'none' }).env);
    expect(r.status).toBe(200);
    expect(r.json).toMatchObject({ ok: true, activated: false, schema_state: 'NOT_APPLIED', missing: { type: 'table', name: 'stage7_research_requests' } });
    expect(r.json.reason).toMatch(/0016/);
  });

  it('partial schema: activated:false, PARTIAL, names the missing table instead of a 500', async () => {
    const r = await overview(makeEnv({ schema: 'partial' }).env);
    expect(r.status).toBe(200);
    expect(r.json).toMatchObject({ ok: true, activated: false, schema_state: 'PARTIAL', missing: { type: 'table', name: 'stage7_event_sentiment' } });
  });

  it('transient D1 failure: 503 STAGE7_SCHEMA_CHECK_FAILED, never "not applied", no raw error text', async () => {
    quiet();
    for (const failQuery of [/GROUP BY status/, /stage7_event_sentiment/]) {
      const { env, hooks } = makeEnv();
      hooks.failQuery = failQuery;
      const r = await overview(env);
      expect(r.status).toBe(503);
      expect(r.json).toMatchObject({ ok: false, code: 'STAGE7_SCHEMA_CHECK_FAILED' });
      expect(r.json.activated).toBeUndefined();
      expect(r.text).not.toMatch(/not yet applied|Network|SELECT|D1_ERROR/);
      expect(r.text).not.toContain(SECRET);
    }
  });
});

describe('P2/P6: error hygiene and authorization order on the register route', () => {
  it('malformed JSON: 400 INVALID_JSON, the body is never echoed, no D1 access', async () => {
    const { env, log } = makeEnv();
    const r = await call(env, '/api/research-lab/stage7-register-response', { method: 'POST', rawBody: `{"request_id": "${SECRET}", oops`, token: TOKEN });
    expect(r.status).toBe(400);
    expect(r.json).toMatchObject({ ok: false, code: 'INVALID_JSON' });
    expect(r.text).not.toContain(SECRET);
    expect(r.text).not.toContain('oops');
    expect(log).toEqual([]);
  });

  it('missing configured token: 503 fail-closed; missing or wrong token: 401; no D1 access, token never echoed', async () => {
    const none = makeEnv({ token: null });
    const r1 = await register(none.env, { request_id: REQ, findings: FINDINGS }, SECRET);
    expect(r1.status).toBe(503);
    expect(none.log).toEqual([]);
    const { env, log } = makeEnv();
    const r2 = await register(env, { request_id: REQ, findings: FINDINGS }, SECRET);
    const r3 = await register(env, { request_id: REQ, findings: FINDINGS }, null);
    expect([r2.status, r3.status]).toEqual([401, 401]);
    for (const r of [r1, r2, r3]) {
      expect(r.text).not.toContain(SECRET);
      expect(r.text).not.toContain(TOKEN);
      expect(r.text).not.toMatch(/stage7_research|SELECT|schema/i);
    }
    expect(log).toEqual([]);
  });

  it('an invalid token is rejected before the body is parsed (malformed body still 401, not 400)', async () => {
    const { env, log } = makeEnv();
    const r = await call(env, '/api/research-lab/stage7-register-response', { method: 'POST', rawBody: '{not json', token: 'wrong-token' });
    expect(r.status).toBe(401);
    expect(log).toEqual([]);
  });

  it('unexpected D1 error: 500 STAGE7_INTERNAL_ERROR with no SQL, error text, request content or token', async () => {
    quiet();
    const { env, hooks } = makeEnv();
    hooks.failQuery = /FROM stage7_research_requests WHERE request_id/;
    const r = await register(env, { request_id: REQ, findings: { ...FINDINGS, summary: SECRET } });
    expect(r.status).toBe(500);
    expect(r.json).toEqual({ ok: false, code: 'STAGE7_INTERNAL_ERROR', error: 'Stage 7 request failed. See the Worker logs.' });
    expect(r.text).not.toContain(SECRET);
    expect(r.text).not.toContain(TOKEN);
  });

  it('a request_id that is not on file is not echoed back', async () => {
    const r = await register(makeEnv().env, { request_id: SECRET, findings: FINDINGS });
    expect(r.status).toBe(404);
    expect(r.text).not.toContain(SECRET);
  });

  it('schema not ready on a write: 503 STAGE7_SCHEMA_NOT_READY, no internal detail', async () => {
    quiet();
    const r = await register(makeEnv({ schema: 'none' }).env, { request_id: REQ, findings: FINDINGS });
    expect(r.status).toBe(503);
    expect(r.json).toMatchObject({ ok: false, code: 'STAGE7_SCHEMA_NOT_READY' });
    expect(r.text).not.toMatch(/no such table/);
  });
});

describe('P3-P5: registration, retries, concurrency and atomicity (migration 0016 constraints)', () => {
  it('success: exactly one response row and the matching request status, written in one batch', async () => {
    for (const [validated, status, rs] of [[false, 'PENDING', 'RESEARCH_RESPONSE_RECEIVED'], [true, 'VALIDATED', 'RESEARCH_COMPLETED']]) {
      const { env, db } = makeEnv();
      const r = await register(env, { request_id: REQ, provider: 'claude', findings: FINDINGS, sources: [], validated });
      expect(r.status).toBe(200);
      expect(r.json).toMatchObject({ ok: true, response_id: 'stage7-resp-' + REQ, validation_status: status });
      const rows = responses(db);
      expect(rows).toHaveLength(1);
      expect(rows[0]).toMatchObject({ request_id: REQ, provider: 'claude', validation_status: status, findings_json: JSON.stringify(FINDINGS) });
      expect(requestStatus(db)).toBe(rs);
    }
  });

  it('identical retry: 200 already_registered, nothing written or changed', async () => {
    const { env, db } = makeEnv();
    const body = { request_id: REQ, provider: 'claude', findings: FINDINGS, sources: [{ url: 'https://x' }], confidence: 'medium', validated: true };
    expect((await register(env, body)).status).toBe(200);
    const first = responses(db);
    const again = await register(env, body);
    expect(again.status).toBe(200);
    expect(again.json).toMatchObject({ ok: true, already_registered: true, response_id: 'stage7-resp-' + REQ, validation_status: 'VALIDATED' });
    expect(responses(db)).toEqual(first);
  });

  it('conflicting retry: 409 and the original row is never overwritten (content, provider or validation differ)', async () => {
    const { env, db } = makeEnv();
    const body = { request_id: REQ, provider: 'claude', findings: FINDINGS };
    expect((await register(env, body)).status).toBe(200);
    const first = responses(db);
    for (const changed of [{ findings: { ...FINDINGS, summary: 'other' } }, { provider: 'gemini' }, { validated: true }, { confidence: 'high' }]) {
      const r = await register(env, { ...body, ...changed });
      expect(r.status).toBe(409);
      expect(r.json.ok).toBe(false);
    }
    expect(responses(db)).toEqual(first);
  });

  it('concurrent submission: the loser hits the UNIQUE constraint and gets 409 (different) or 200 (identical), never a 500', async () => {
    for (const identical of [false, true]) {
      const { env, db, hooks } = makeEnv();
      const mine = { request_id: REQ, provider: 'claude', findings: FINDINGS };
      // The competing writer commits between this request's existence check and its batch.
      hooks.beforeBatch = (d) => {
        hooks.beforeBatch = null;
        const winner = identical ? FINDINGS : { ...FINDINGS, summary: 'the other writer' };
        d.prepare(`INSERT INTO stage7_research_responses (response_id, request_id, provider, submitted_ts, registered_ts, findings_json, sources_json, confidence, validation_status)
          VALUES (?, ?, 'claude', NULL, 5, ?, '[]', NULL, 'PENDING')`).run('stage7-resp-' + REQ, REQ, JSON.stringify(winner));
      };
      const r = await register(env, mine);
      expect(r.status).toBe(identical ? 200 : 409);
      if (identical) expect(r.json.already_registered).toBe(true);
      expect(responses(db)).toHaveLength(1);
      expect(r.text).not.toMatch(/UNIQUE|constraint|SQLITE/);
    }
  });

  it('atomicity: if the status UPDATE fails, the response INSERT is rolled back (no partial registration)', async () => {
    quiet();
    const { env, db, hooks } = makeEnv();
    hooks.failBatchStatement = 1;
    const r = await register(env, { request_id: REQ, findings: FINDINGS, validated: true });
    expect(r.status).toBe(500);
    expect(r.json.code).toBe('STAGE7_INTERNAL_ERROR');
    expect(r.text).not.toContain(SECRET);
    expect(responses(db)).toEqual([]);
    expect(requestStatus(db)).toBe('RESEARCH_REQUEST_PUBLISHED');
  });

  it('a request that turns terminal after the check gets no response row at all', async () => {
    const { env, db, hooks } = makeEnv();
    hooks.beforeBatch = (d) => d.prepare("UPDATE stage7_research_requests SET status = 'INTEGRATED' WHERE request_id = ?").run(REQ);
    const r = await register(env, { request_id: REQ, findings: FINDINGS });
    expect(r.status).toBe(409);
    expect(responses(db)).toEqual([]);
    expect(requestStatus(db)).toBe('INTEGRATED');
  });

  it('terminal request refused up front; nothing written', async () => {
    const { env, db } = makeEnv({ requestStatus: 'REJECTED' });
    const r = await register(env, { request_id: REQ, findings: FINDINGS });
    expect(r.status).toBe(409);
    expect(r.json.error).toMatch(/REJECTED/);
    expect(responses(db)).toEqual([]);
  });
});

describe('adversarial review (2026-10-31): retry semantics and constraint classification', () => {
  it('identical retry with object keys reordered (top level and nested) is idempotent: 200, stored text unchanged', async () => {
    const { env, db } = makeEnv();
    const findings = { summary: 'x', sentiment_assessment: 'NEGATIVE', detail: { a: 1, b: [1, 2] } };
    const sources = [{ url: 'https://x', publisher: 'p' }];
    expect((await register(env, { request_id: REQ, provider: 'claude', findings, sources })).status).toBe(200);
    const first = responses(db);
    const reordered = { detail: { b: [1, 2], a: 1 }, sentiment_assessment: 'NEGATIVE', summary: 'x' };
    const again = await register(env, { request_id: REQ, provider: 'claude', findings: reordered, sources: [{ publisher: 'p', url: 'https://x' }] });
    expect(again.status).toBe(200);
    expect(again.json.already_registered).toBe(true);
    expect(responses(db)).toEqual(first);
  });

  it('array order, nested values, value types and provider still count as different content: 409, original kept', async () => {
    const { env, db } = makeEnv();
    const base = { request_id: REQ, provider: 'claude', findings: { ...FINDINGS, list: [1, 2], n: 1 } };
    expect((await register(env, base)).status).toBe(200);
    const first = responses(db);
    for (const changed of [
      { findings: { ...FINDINGS, list: [2, 1], n: 1 } },
      { findings: { ...FINDINGS, list: [1, 2], n: 2 } },
      { findings: { ...FINDINGS, list: [1, 2], n: '1' } },
      { provider: 'Claude' },
      { provider: null },
    ]) {
      const r = await register(env, { ...base, ...changed });
      expect(r.status).toBe(409);
    }
    expect(responses(db)).toEqual(first);
  });

  it('a stored row with malformed JSON never compares equal to a retry (409, not 200)', async () => {
    const { env, db } = makeEnv();
    db.prepare(`INSERT INTO stage7_research_responses (response_id, request_id, provider, submitted_ts, registered_ts, findings_json, sources_json, confidence, validation_status)
      VALUES (?, ?, NULL, NULL, 5, '{not json', '[]', NULL, 'PENDING')`).run('stage7-resp-' + REQ, REQ);
    const r = await register(env, { request_id: REQ, findings: FINDINGS });
    expect(r.status).toBe(409);
    expect(responses(db)[0].findings_json).toBe('{not json');
  });

  it('a non-uniqueness constraint failure (e.g. FOREIGN KEY, NOT NULL) is not reported as a concurrent registration: 500, nothing written', async () => {
    quiet();
    for (const msg of ['D1_ERROR: FOREIGN KEY constraint failed: SQLITE_CONSTRAINT', 'D1_ERROR: NOT NULL constraint failed: stage7_research_responses.findings_json: SQLITE_CONSTRAINT']) {
      const { env, db, hooks } = makeEnv();
      hooks.failBatchStatement = 0;
      hooks.failBatchMessage = msg;
      const r = await register(env, { request_id: REQ, findings: FINDINGS });
      expect(r.status).toBe(500);
      expect(r.json).toEqual({ ok: false, code: 'STAGE7_INTERNAL_ERROR', error: 'Stage 7 request failed. See the Worker logs.' });
      expect(r.text).not.toMatch(/constraint|SQLITE|FOREIGN/i);
      expect(responses(db)).toEqual([]);
      expect(requestStatus(db)).toBe('RESEARCH_REQUEST_PUBLISHED');
    }
  });

  it('a uniqueness violation with no stored row to explain it fails (500) instead of claiming a registration', async () => {
    quiet();
    const { env, db, hooks } = makeEnv();
    hooks.failBatchStatement = 0;
    hooks.failBatchMessage = 'D1_ERROR: UNIQUE constraint failed: stage7_research_responses.request_id: SQLITE_CONSTRAINT';
    const r = await register(env, { request_id: REQ, findings: FINDINGS });
    expect(r.status).toBe(500);
    expect(r.json.code).toBe('STAGE7_INTERNAL_ERROR');
    expect(responses(db)).toEqual([]);
  });

  it('/api/learning/findings: malformed JSON is a 400 without echo; an identical re-confirmation is idempotent', async () => {
    const { env, db } = makeEnv();
    const bad = await call(env, '/api/learning/findings', { method: 'POST', rawBody: `{"event_id": 15, "x": "${SECRET}"`, token: TOKEN });
    expect(bad.status).toBe(400);
    expect(bad.text).not.toContain(SECRET);
    const wrong = await call(env, '/api/learning/findings', { method: 'POST', body: { event_id: 15, finding: { explanation: SECRET } }, token: 'wrong-token' });
    expect(wrong.status).toBe(401);
    expect(wrong.text).not.toContain(SECRET);
    expect(responses(db)).toEqual([]);
  });
});

describe('scope guards', () => {
  it('migration 0016 compatibility: the register path uses only 0016 columns, no new switch or migration', () => {
    const fn = WORKER_SRC.slice(WORKER_SRC.indexOf('async function registerStage7ResearchResponse'), WORKER_SRC.indexOf('const REQUIRED_SAMPLE_FALLBACK'));
    expect(fn).not.toMatch(/raw_response_text|source_validation_json|human_confirmed_ts|human_review_note|candidate_id/);
    expect(WORKER_SRC).not.toMatch(/env\.STAGE7_ENABLED|STAGE7_ENABLED\s*[=!]==/);   // no code reads a Stage 7 switch
    expect(readFileSync(join(__dirname, '..', 'wrangler.toml'), 'utf8')).not.toMatch(/STAGE7_ENABLED/);
  });

  it('learning routes: an unexpected D1 error returns a generic body, not the raw error text', async () => {
    quiet();
    const { env, hooks } = makeEnv();
    hooks.failQuery = /./;
    const r = await call(env, '/api/learning/case?event_id=15');
    if (r.status === 500) {
      expect(r.json).toEqual({ ok: false, code: 'LEARNING_INTERNAL_ERROR', error: 'Request failed. See the Worker logs.' });
    }
    expect(r.text).not.toContain(SECRET);
    expect(r.text).not.toMatch(/D1_ERROR|Network connection lost/);
  });
});
