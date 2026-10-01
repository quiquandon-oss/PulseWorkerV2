import { describe, it, expect, beforeAll, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import worker from '../worker.js';
import { extractConstants, evalInScope } from './helpers/extract.js';

// Route-level tests for the Stage 7 deployment gate, schema readiness and safe error mapping. These drive the
// REAL `export default { fetch }` dispatcher, so they prove what a merge to main (which auto-deploys worker.js to
// production) can and cannot do there: with STAGE7_ENABLED unset nothing is read or written; with it set, writes
// still need the admin token, then a complete schema, and errors never echo SQL or request content.
const WORKER_SRC = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
const TOKEN = 'test-admin-token-value';

let PROBE_SQL;
let REQUIRED;
beforeAll(() => {
  const scope = evalInScope(extractConstants('STAGE7_REQUIRED_SCHEMA', 'STAGE7_SCHEMA_PROBE_SQL'));
  PROBE_SQL = scope.STAGE7_SCHEMA_PROBE_SQL;
  REQUIRED = scope.STAGE7_REQUIRED_SCHEMA;
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

// Rows the schema probe returns for a database that has migrations up to and including `upTo`.
function schemaRows(upTo) {
  const rows = [];
  for (const group of REQUIRED) {
    if (group.migration > upTo) continue;
    for (const col of group.columns) rows.push({ tbl: group.table, col });
  }
  return rows;
}

// A fake D1 that answers the schema probe and records every other statement. `handler(sql, args)` answers the rest.
function fakeDb({ rows = [], probeThrows = false, handler } = {}) {
  const calls = [];
  const db = {
    calls,
    prepare(sql) {
      calls.push(sql);
      const run = (args) => ({
        all: async () => {
          if (sql === PROBE_SQL) {
            if (probeThrows) throw new Error('D1_ERROR: network connection lost');
            return { results: rows };
          }
          return handler ? handler(sql, args, 'all') : { results: [] };
        },
        first: async () => (handler ? handler(sql, args, 'first') : null),
        run: async () => (handler ? handler(sql, args, 'run') : { success: true, meta: { changes: 1 } }),
      });
      return { ...run([]), bind: (...args) => run(args) };
    },
    async batch(statements) { return Promise.all(statements.map((st) => st.run())); },
  };
  return db;
}

const nonProbe = (db) => db.calls.filter((sql) => sql !== PROBE_SQL);

async function call(path, { method = 'GET', body, token, env }) {
  const headers = { 'Content-Type': 'application/json' };
  if (token !== undefined) headers.Authorization = `Bearer ${token}`;
  const request = new Request(`https://example.com${path}`, {
    method, headers, body: body === undefined ? undefined : (typeof body === 'string' ? body : JSON.stringify(body)),
  });
  const response = await worker.fetch(request, env);
  const text = await response.text();
  let json = null;
  try { json = JSON.parse(text); } catch (_e) { /* leave null */ }
  return { status: response.status, json, text };
}

const WRITE_ROUTES = [
  ['/api/research-lab/stage7-create-requests', { candidate_ids: ['stage7-cand-1'] }],
  ['/api/research-lab/stage7-register-response', { request_id: 'stage7-req-1-1', findings: { sentiment_assessment: 'POSITIVE' }, raw_response_text: 'x' }],
  ['/api/research-lab/stage7-trigger-recalculation', { request_id: 'stage7-req-1-1' }],
  ['/api/research-lab/stage7-review-response', { request_id: 'stage7-req-1-1', decision: 'VALIDATE', human_confirmed: true }],
];

describe('Stage 7 is OFF unless the deployment enables it (a merge to main must not activate it)', () => {
  const exploding = () => ({ prepare() { throw new Error('the database must not be touched'); } });

  it('overview: reports not-enabled, touches no database', async () => {
    const r = await call('/api/research-lab/stage7-overview', { env: { DB: exploding() } });
    expect(r.status).toBe(200);
    expect(r.json).toMatchObject({ ok: true, activated: false, enabled: false });
    expect(r.json.reason).toMatch(/not enabled/);
  });

  for (const [path, body] of WRITE_ROUTES) {
    it(`${path}: 503 STAGE7_DISABLED even with a CORRECT token and a fully configured secret; no database access`, async () => {
      const r = await call(path, { method: 'POST', body, token: TOKEN, env: { DB: exploding(), STAGE7_ADMIN_TOKEN: TOKEN } });
      expect(r.status).toBe(503);
      expect(r.json.code).toBe('STAGE7_DISABLED');
    });
  }

  it('validate-sources is also off', async () => {
    const r = await call('/api/research-lab/stage7-validate-sources', { method: 'POST', body: { sources: [] }, env: { DB: exploding() } });
    expect(r.status).toBe(503);
    expect(r.json.code).toBe('STAGE7_DISABLED');
  });

  it.each(['true ', 'TRUE', '1', 'yes', true, ''])('only the exact string "true" enables it (%j does not)', async (value) => {
    const r = await call('/api/research-lab/stage7-overview', { env: { DB: exploding(), STAGE7_ENABLED: value } });
    expect(r.json.enabled).toBe(false);
  });

  it('the production wrangler.toml never enables Stage 7; only the staging config does', () => {
    const prod = readFileSync(new URL('../wrangler.toml', import.meta.url), 'utf8');
    const staging = readFileSync(new URL('../wrangler.staging.toml', import.meta.url), 'utf8');
    expect(prod).not.toMatch(/STAGE7_ENABLED/);
    expect(staging).toMatch(/^STAGE7_ENABLED = "true"$/m);
  });

  it('every Stage 7 route in worker.js passes through the gate (no route can be added un-gated unnoticed)', () => {
    const routes = [...WORKER_SRC.matchAll(/url\.pathname === '(\/api\/research-lab\/stage7-[a-z-]+)'/g)].map((m) => m[1]);
    expect(routes.sort()).toEqual([
      '/api/research-lab/stage7-create-requests', '/api/research-lab/stage7-overview',
      '/api/research-lab/stage7-register-response', '/api/research-lab/stage7-review-response',
      '/api/research-lab/stage7-trigger-recalculation', '/api/research-lab/stage7-validate-sources',
    ]);
    for (const route of routes) {
      const at = WORKER_SRC.indexOf(`url.pathname === '${route}'`);
      const block = WORKER_SRC.slice(at, at + 1400);
      expect(block, route).toMatch(/stage7WriteRoute|stage7RoutePreflight|stage7Enabled\(env\)/);
    }
  });
});

describe('writes: token first, then schema, and only then anything else', () => {
  const enabled = (db, extra = {}) => ({ DB: db, STAGE7_ENABLED: 'true', STAGE7_ADMIN_TOKEN: TOKEN, ...extra });

  it('no STAGE7_ADMIN_TOKEN configured: 503 (fail closed), database untouched', async () => {
    const db = fakeDb({ rows: schemaRows('0018') });
    const [path, body] = WRITE_ROUTES[0];
    const r = await call(path, { method: 'POST', body, token: TOKEN, env: { DB: db, STAGE7_ENABLED: 'true' } });
    expect(r.status).toBe(503);
    expect(r.json.code).toBe('STAGE7_TOKEN_NOT_CONFIGURED');
    expect(db.calls).toEqual([]);
  });

  for (const [path, body] of WRITE_ROUTES) {
    it(`${path}: missing and wrong tokens are 401 and reveal nothing about the schema`, async () => {
      for (const token of [undefined, 'wrong', '']) {
        const db = fakeDb({ rows: [] }); // an empty (unmigrated) database
        const r = await call(path, { method: 'POST', body, token, env: enabled(db) });
        expect(r.status).toBe(401);
        expect(r.json).toEqual({ ok: false, error: 'Unauthorized' });
        expect(db.calls).toEqual([]); // not even the schema probe ran
      }
    });
  }

  for (const [path, body] of WRITE_ROUTES) {
    it(`${path}: authorised but schema not applied -> 503 naming the migrations, and NO write is attempted`, async () => {
      const db = fakeDb({ rows: [] });
      const r = await call(path, { method: 'POST', body, token: TOKEN, env: enabled(db) });
      expect(r.status).toBe(503);
      expect(r.json.code).toBe('STAGE7_SCHEMA_NOT_READY');
      expect(r.json.schema.state).toBe('NOT_APPLIED');
      expect(r.json.schema.migrations_required).toEqual(['0005', '0016', '0017', '0018']);
      expect(r.json.error).toMatch(/Apply, in order/);
      expect(nonProbe(db)).toEqual([]);
    });
  }

  it('migration 0018 missing (0016+0017 applied): PARTIAL, names exactly 0018 and the columns', async () => {
    const db = fakeDb({ rows: schemaRows('0017') });
    const [path, body] = WRITE_ROUTES[2];
    const r = await call(path, { method: 'POST', body, token: TOKEN, env: enabled(db) });
    expect(r.status).toBe(503);
    expect(r.json.schema.state).toBe('PARTIAL');
    expect(r.json.schema.migrations_required).toEqual(['0018']);
    expect(r.json.schema.missing_columns.map((c) => c.column)).toContain('recalculation_status');
    expect(r.json.schema.missing_columns.every((c) => c.migration === '0018')).toBe(true);
    expect(nonProbe(db)).toEqual([]);
  });

  it('a transient database failure during the schema probe is NOT reported as a missing migration', async () => {
    const db = fakeDb({ probeThrows: true });
    const [path, body] = WRITE_ROUTES[1];
    const r = await call(path, { method: 'POST', body, token: TOKEN, env: enabled(db) });
    expect(r.status).toBe(503);
    expect(r.json.code).toBe('STAGE7_SCHEMA_CHECK_FAILED');
    expect(r.json.error).toMatch(/NOT evidence that a migration is missing/);
    expect(r.text).not.toMatch(/network connection lost/);
  });

  for (const [path] of WRITE_ROUTES) {
    it(`${path}: a body that is not JSON is 400, not a 500, and nothing is written`, async () => {
      const db = fakeDb({ rows: schemaRows('0018') });
      const r = await call(path, { method: 'POST', body: '{not json', token: TOKEN, env: enabled(db) });
      expect(r.status).toBe(400);
      expect(r.json.code).toBe('INVALID_JSON');
      expect(nonProbe(db)).toEqual([]);
    });
  }

  it('an unexpected internal error is a generic 500 that never echoes the error text, SQL or request content', async () => {
    const db = fakeDb({
      rows: schemaRows('0018'),
      handler(sql) { throw new Error('boom: SELECT secret_column FROM somewhere WHERE token = hunter2'); },
    });
    const [path] = WRITE_ROUTES[1];
    const r = await call(path, {
      method: 'POST', token: TOKEN, env: enabled(db),
      body: { request_id: 'stage7-req-1-1', findings: { sentiment_assessment: 'POSITIVE' }, raw_response_text: 'PRIVATE-PASTE' },
    });
    expect(r.status).toBe(500);
    expect(r.json.code).toBe('STAGE7_INTERNAL_ERROR');
    expect(r.text).not.toMatch(/hunter2|secret_column|SELECT|PRIVATE-PASTE|boom/);
  });

  it('a "no such column" raised after the probe (schema drift between calls) maps to 503 without leaking SQL', async () => {
    const db = fakeDb({
      rows: schemaRows('0018'),
      handler() { throw new Error('D1_ERROR: no such column: recalculation_status at offset 12'); },
    });
    const [path, body] = WRITE_ROUTES[2];
    const r = await call(path, { method: 'POST', body, token: TOKEN, env: enabled(db) });
    expect(r.status).toBe(503);
    expect(r.json.code).toBe('STAGE7_SCHEMA_NOT_READY');
    expect(r.text).not.toMatch(/offset 12|D1_ERROR/);
  });

  it('the token never appears in any response body', async () => {
    const db = fakeDb({ rows: [] });
    const [path, body] = WRITE_ROUTES[0];
    const r = await call(path, { method: 'POST', body, token: TOKEN, env: enabled(db) });
    expect(r.text).not.toContain(TOKEN);
  });
});

describe('overview: reads are gated by schema too, and report the deployed identity', () => {
  const enabled = (db, extra = {}) => ({ DB: db, STAGE7_ENABLED: 'true', ...extra });

  it('not applied: activated:false with the migrations to apply (never claims "production")', async () => {
    const r = await call('/api/research-lab/stage7-overview', { env: enabled(fakeDb({ rows: [] })) });
    expect(r.status).toBe(200);
    expect(r.json).toMatchObject({ ok: true, activated: false, enabled: true });
    expect(r.json.schema.state).toBe('NOT_APPLIED');
    expect(r.json.reason).not.toMatch(/production/i);
  });

  it('partial schema: activated:false, not a 500', async () => {
    const r = await call('/api/research-lab/stage7-overview', { env: enabled(fakeDb({ rows: schemaRows('0016') })) });
    expect(r.status).toBe(200);
    expect(r.json.activated).toBe(false);
    expect(r.json.schema.migrations_required).toEqual(['0017', '0018']);
  });

  it('probe failure: 503 check-failed, not "migration missing"', async () => {
    const r = await call('/api/research-lab/stage7-overview', { env: enabled(fakeDb({ probeThrows: true })) });
    expect(r.status).toBe(503);
    expect(r.json.code).toBe('STAGE7_SCHEMA_CHECK_FAILED');
  });

  it('a transient failure of the overview query itself is a generic 500, never "not applied"', async () => {
    const db = fakeDb({ rows: schemaRows('0018'), handler() { throw new Error('D1_ERROR: Network connection lost'); } });
    const r = await call('/api/research-lab/stage7-overview', { env: enabled(db) });
    expect(r.status).toBe(500);
    expect(r.json.code).toBe('STAGE7_INTERNAL_ERROR');
    expect(r.text).not.toMatch(/not applied|Network connection lost/);
  });

  it('ready schema: serves the overview and includes the deployed commit and enabled flag', async () => {
    const db = fakeDb({ rows: schemaRows('0018'), handler: () => ({ results: [] }) });
    const r = await call('/api/research-lab/stage7-overview', { env: enabled(db, { GIT_COMMIT_SHA: 'abc123' }) });
    expect(r.status).toBe(200);
    expect(r.json.activated).toBe(true);
    expect(r.json.deployment).toEqual({ git_commit_sha: 'abc123', stage7_enabled: true });
  });

  it('every statement the overview route issues is a SELECT', async () => {
    const db = fakeDb({ rows: schemaRows('0018'), handler: () => ({ results: [] }) });
    await call('/api/research-lab/stage7-overview', { env: enabled(db) });
    for (const sql of db.calls) expect(sql.trim()).toMatch(/^SELECT /i);
  });
});
