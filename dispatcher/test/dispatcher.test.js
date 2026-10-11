import { describe, it, expect, beforeEach } from 'vitest';
import { createHash } from 'node:crypto';
import { handle, MemoryGuard, ALLOWLIST, LIMITS, validateBody, constantTimeEqual, dispatchEnabled, sanitizeAudit } from '../worker.js';

const OWNER_KEY = 'owner-key-for-tests-0123456789abcdef';
const GH_TOKEN = 'github_pat_TESTTOKEN_0123456789_abcdefghij';
const ORIGIN = 'https://quiquandon-oss.github.io';
const env = (over = {}) => ({ GITHUB_DISPATCH_TOKEN: GH_TOKEN, OWNER_KEY_SHA256: createHash('sha256').update(OWNER_KEY).digest('hex'), GIT_COMMIT_SHA: 'abc1234', ...over });

function gh({ workflow = { path: '.github/workflows/prediction-freshness-check.yml', state: 'active' }, runs = [], dispatch = 204, throwOn = null } = {}) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push({ url, init });
    if (throwOn && url.includes(throwOn)) { const e = new Error('aborted'); e.name = 'AbortError'; throw e; }
    const res = (status, body, headers = {}) => ({ ok: status >= 200 && status < 300, status, json: async () => body, headers: { get: (k) => headers[k.toLowerCase()] ?? null } });
    if (url.endsWith('/dispatches')) return typeof dispatch === 'function' ? dispatch() : res(dispatch, {});
    if (url.includes('/runs')) return res(200, { workflow_runs: runs });
    if (typeof workflow === 'number') return res(workflow, {});
    return res(200, workflow);
  };
  return { fetch, calls };
}

let t, guard;
const deps = (g) => ({ fetch: g.fetch, guard, now: () => t, uuid: () => 'uuid-1' });
const req = (body, { auth = OWNER_KEY, origin = ORIGIN, method = 'POST', path = '/dispatch', ct = 'application/json', ip = '1.2.3.4' } = {}) => new Request(`https://d.example${path}`, {
  method, headers: { ...(auth ? { Authorization: `Bearer ${auth}` } : {}), ...(origin ? { Origin: origin } : {}), ...(ct ? { 'Content-Type': ct } : {}), 'CF-Connecting-IP': ip },
  body: method === 'POST' ? (typeof body === 'string' ? body : JSON.stringify(body)) : undefined,
});
const ok = { job: 'prediction-freshness-check', confirm: true, request_id: 'req-12345678' };

beforeEach(() => { t = Date.UTC(2026, 9, 11, 18, 0); guard = new MemoryGuard(); });

describe('configuration and allowlist', () => {
  it('allowlist holds only the read-only persistence check with fixed id, path, ref and no inputs', () => {
    expect(Object.keys(ALLOWLIST)).toEqual(['prediction-freshness-check']);
    const s = ALLOWLIST['prediction-freshness-check'];
    expect([s.workflowId, s.workflowPath, s.ref, Object.keys(s.inputs).length]).toEqual([381092876, '.github/workflows/prediction-freshness-check.yml', 'main', 0]);
  });
  it('dispatch is disabled unless both secrets are provisioned', async () => {
    expect(dispatchEnabled({})).toBe(false);
    expect(dispatchEnabled(env({ OWNER_KEY_SHA256: 'nothex' }))).toBe(false);
    const g = gh();
    const r = await handle(req(ok), {}, deps(g));
    expect(r.status).toBe(503); expect((await r.json()).code).toBe('dispatch_disabled');
    expect(g.calls).toHaveLength(0);
    expect(guard.audit[0]).toMatchObject({ outcome: 'rejected', code: 'dispatch_disabled' });
  });
  it('health is public and reveals no secret', async () => {
    const r = await handle(req(null, { method: 'GET', path: '/health', auth: null, ct: null }), env(), deps(gh()));
    const b = await r.json();
    expect(b).toEqual({ ok: true, service: 'cryptopulse-research-dispatcher', version: 'abc1234', dispatch_enabled: true, jobs: ['prediction-freshness-check'] });
    expect(JSON.stringify(b)).not.toContain(GH_TOKEN);
  });
});

describe('authentication', () => {
  it('missing and invalid credentials are 401 and dispatch nothing', async () => {
    const g = gh();
    for (const auth of [null, 'wrong-key-wrong-key-wrong-key-1234', OWNER_KEY.slice(0, -1) + 'X']) {
      const r = await handle(req(ok, { auth }), env(), deps(g));
      expect(r.status).toBe(401); expect((await r.json()).code).toBe('unauthorized');
    }
    expect(g.calls).toHaveLength(0);
    expect(guard.audit.map(a => a.code)).toEqual(['missing_auth', 'invalid_auth', 'invalid_auth']);
  });
  it('repeated failures are rate-limited even if a correct key follows', async () => {
    for (let i = 0; i < LIMITS.authFailuresPerHour; i++) await handle(req(ok, { auth: 'bad-key-bad-key-bad-key-0000' + i }), env(), deps(gh()));
    const r = await handle(req(ok), env(), deps(gh()));
    expect(r.status).toBe(429); expect(r.headers.get('Retry-After')).toBeTruthy();
    const other = await handle(req(ok, { ip: '9.9.9.9' }), env(), deps(gh()));
    expect(other.status).toBe(202); // limit is per client
  });
  it('audit listing requires authentication', async () => {
    expect((await handle(req(null, { method: 'GET', path: '/audit', auth: null, ct: null }), env(), deps(gh()))).status).toBe(401);
    expect((await handle(req(null, { method: 'GET', path: '/audit', ct: null }), env(), deps(gh()))).status).toBe(200);
  });
  it('constant-time comparison', () => {
    expect(constantTimeEqual('abc', 'abc')).toBe(true);
    expect(constantTimeEqual('abc', 'abd')).toBe(false);
    expect(constantTimeEqual('abc', 'abcd')).toBe(false);
    expect(constantTimeEqual(undefined, 'a')).toBe(false);
  });
});

describe('origin and request validation', () => {
  it('only the V3 Pages origin passes CORS; other origins are refused', async () => {
    const pre = await handle(new Request('https://d.example/dispatch', { method: 'OPTIONS', headers: { Origin: ORIGIN } }), env(), deps(gh()));
    expect(pre.status).toBe(204); expect(pre.headers.get('Access-Control-Allow-Origin')).toBe(ORIGIN);
    const bad = await handle(new Request('https://d.example/dispatch', { method: 'OPTIONS', headers: { Origin: 'https://evil.example' } }), env(), deps(gh()));
    expect(bad.status).toBe(403); expect(bad.headers.get('Access-Control-Allow-Origin')).toBeNull();
    const g = gh();
    const post = await handle(req(ok, { origin: 'https://evil.example' }), env(), deps(g));
    expect(post.status).toBe(403); expect(g.calls).toHaveLength(0);
    expect(guard.audit.at(-1)).toMatchObject({ outcome: 'rejected', code: 'origin_not_allowed', origin: 'https://evil.example' });
  });
  it('repository, workflow, branch, commit and inputs cannot be supplied by the client', async () => {
    for (const extra of [{ inputs: { x: 1 } }, { ref: 'evil' }, { repo: 'x/y' }, { workflow: 'deploy.yml' }, { sha: 'deadbeef' }, { cmd: 'rm -rf /' }]) {
      const r = await handle(req({ ...ok, ...extra }), env(), deps(gh()));
      expect(r.status).toBe(400); expect((await r.json()).code).toBe('field_not_allowed');
    }
  });
  it('disallowed jobs and malformed requests are rejected', async () => {
    const g = gh();
    for (const [body, status, code] of [
      [{ job: 'deploy', confirm: true }, 403, 'job_not_allowed'], [{ job: 'export-learning-data', confirm: true }, 403, 'job_not_allowed'],
      [{ job: 'exp23-forward-evaluation', confirm: true }, 403, 'job_not_allowed'], [{ job: 'market-moves-evaluation', confirm: true }, 403, 'job_not_allowed'],
      [{ job: '../../x', confirm: true }, 400, 'invalid_job'], [{ job: 'prediction-freshness-check' }, 400, 'confirmation_required'],
      [{ ...ok, request_id: 'x' }, 400, 'invalid_request_id'], ['not json', 400, 'invalid_body'], [[1], 400, 'invalid_body']]) {
      const r = await handle(req(body), env(), deps(g));
      expect([r.status, (await r.json()).code]).toEqual([status, code]);
    }
    expect((await handle(req(ok, { ct: 'text/plain' }), env(), deps(g))).status).toBe(415);
    expect((await handle(req(JSON.stringify({ ...ok, request_id: 'a'.repeat(2000) })), env(), deps(g))).status).toBe(413);
    expect(g.calls).toHaveLength(0);
    expect(validateBody({ job: 'prediction-freshness-check', confirm: true })).toMatchObject({ ok: true, requestId: null });
  });
});

describe('dispatch', () => {
  it('accepted dispatch: fixed repository, workflow id, ref main, no inputs; token only sent to GitHub', async () => {
    const g = gh();
    const r = await handle(req(ok), env(), deps(g));
    expect(r.status).toBe(202);
    const b = await r.json();
    expect(b).toMatchObject({ ok: true, status: 'accepted', dispatch_id: 'uuid-1', job: 'prediction-freshness-check', workflow_id: 381092876, ref: 'main', accepted_at: t });
    expect(b.note).toMatch(/has not started until GitHub lists it/);
    const d = g.calls.find(c => c.url.endsWith('/dispatches'));
    expect(d.url).toBe('https://api.github.com/repos/quiquandon-oss/PulseWorkerV2/actions/workflows/381092876/dispatches');
    expect(JSON.parse(d.init.body)).toEqual({ ref: 'main', inputs: {} });
    expect(d.init.headers.Authorization).toBe(`Bearer ${GH_TOKEN}`);
    expect(g.calls.every(c => c.url.startsWith('https://api.github.com/repos/quiquandon-oss/PulseWorkerV2/'))).toBe(true);
    expect(JSON.stringify(b)).not.toContain(GH_TOKEN);
    expect(r.headers.get('Access-Control-Allow-Origin')).toBe(ORIGIN);
  });
  it('a repeat dispatch within the job interval is rate-limited', async () => {
    expect((await handle(req(ok), env(), deps(gh()))).status).toBe(202);
    t += 30_000;
    const r = await handle(req(ok), env(), deps(gh()));
    expect(r.status).toBe(429); expect(Number(r.headers.get('Retry-After'))).toBeGreaterThan(0);
  });
  it('concurrent dispatches: only one reaches GitHub, the other gets 409', async () => {
    let release;
    const slow = gh({ dispatch: () => new Promise(res => { release = () => res({ ok: true, status: 204, json: async () => ({}), headers: { get: () => null } }); }) });
    const a = handle(req(ok), env(), deps(slow));
    await new Promise(r => setTimeout(r, 10));
    const b = await handle(req(ok), env(), deps(gh()));
    expect(b.status).toBe(409); expect((await b.json()).reason).toBe('dispatch_in_flight');
    release();
    expect((await a).status).toBe(202);
    expect(slow.calls.filter(c => c.url.endsWith('/dispatches'))).toHaveLength(1);
  });
  it('a queued or running run on GitHub blocks a new dispatch and frees the lock', async () => {
    for (const status of ['queued', 'in_progress', 'waiting']) {
      guard = new MemoryGuard();
      const g = gh({ runs: [{ id: 555, status }] });
      const r = await handle(req(ok), env(), deps(g));
      expect(r.status).toBe(409); expect(await r.json()).toMatchObject({ reason: 'run_active', run_id: 555 });
      expect(g.calls.some(c => c.url.endsWith('/dispatches'))).toBe(false);
      expect(guard.locks.size).toBe(0);
    }
  });
  it('workflow renamed or disabled on GitHub is refused', async () => {
    for (const workflow of [{ path: '.github/workflows/other.yml', state: 'active' }, { path: '.github/workflows/prediction-freshness-check.yml', state: 'disabled_manually' }]) {
      guard = new MemoryGuard();
      const r = await handle(req(ok), env(), deps(gh({ workflow })));
      expect(r.status).toBe(502); expect((await r.json()).code).toBe('workflow_mismatch');
    }
  });
  it('GitHub rejection, rate limit and timeout map to explicit errors and release the lock', async () => {
    const cases = [
      [gh({ dispatch: 401 }), 502, 'upstream_auth'], [gh({ dispatch: 403 }), 502, 'upstream_auth'], [gh({ dispatch: 422 }), 502, 'upstream_rejected'],
      [gh({ workflow: 404 }), 502, 'upstream_not_found'], [gh({ dispatch: 500 }), 502, 'upstream_error'],
      [gh({ dispatch: () => ({ ok: false, status: 403, json: async () => ({}), headers: { get: (k) => ({ 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': String(Math.floor(Date.now() / 1000) + 120) })[k] ?? null } }) }), 503, 'upstream_rate_limited'],
      [gh({ throwOn: '/dispatches' }), 504, 'upstream_timeout'],
    ];
    for (const [g, status, code] of cases) {
      guard = new MemoryGuard();
      const r = await handle(req(ok), env(), deps(g));
      const b = await r.json();
      expect([r.status, b.code]).toEqual([status, code]);
      expect(JSON.stringify(b)).not.toContain(GH_TOKEN);
      expect(guard.locks.size).toBe(0);
      expect(guard.audit.at(-1)).toMatchObject({ outcome: 'failed', code });
      if (code === 'upstream_rate_limited') expect(Number(r.headers.get('Retry-After'))).toBeGreaterThan(0);
      // a failed dispatch is not counted against the job interval
      expect((await handle(req(ok), env(), deps(gh()))).status).toBe(202);
    }
  });
});

describe('audit', () => {
  it('records accepted, rejected and failed dispatches with no credential, key or request body', async () => {
    await handle(req(ok, { auth: null }), env(), deps(gh()));
    await handle(req({ job: 'deploy', confirm: true }), env(), deps(gh()));
    await handle(req(ok), env(), deps(gh({ dispatch: 422 })));
    await handle(req(ok), env(), deps(gh()));
    expect(guard.audit.map(a => a.outcome)).toEqual(['rejected', 'rejected', 'failed', 'accepted']);
    const text = JSON.stringify(guard.audit);
    expect(text).not.toContain(GH_TOKEN); expect(text).not.toContain(OWNER_KEY); expect(text).not.toContain('1.2.3.4');
    for (const a of guard.audit) expect(Object.keys(a).sort()).toEqual(['code', 'dispatch_id', 'http_status', 'ip_hash', 'job', 'origin', 'outcome', 'request_id', 'run_id', 'ts', 'upstream_status']);
    expect(guard.audit[3]).toMatchObject({ dispatch_id: 'uuid-1', request_id: 'req-12345678', job: 'prediction-freshness-check', upstream_status: 204 });
    expect(sanitizeAudit({ outcome: 'x', authorization: 'Bearer secret', token: 'y' })).not.toHaveProperty('authorization');
  });
});
