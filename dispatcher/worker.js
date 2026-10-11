// cryptopulse-research-dispatcher -- server-side launcher for allowlisted
// PulseWorkerV2 research workflows, used by CryptoPulse V3 Research Controls.
//
// Isolation: a separate Worker with its own wrangler.toml. It has no D1
// binding, no access to the prediction Worker, and no generic admin or
// database-write capability. The only outbound call it makes is to the GitHub
// REST API for the fixed repository below.
//
// Security model (see dispatcher/README.md):
//  - Owner authentication: Authorization: Bearer <owner dispatch key>. Only the
//    SHA-256 of the key is stored (secret OWNER_KEY_SHA256); comparison is
//    constant-time. Repeated failures from one client are rate-limited.
//  - GitHub credential: secret GITHUB_DISPATCH_TOKEN (fine-grained PAT limited
//    to quiquandon-oss/PulseWorkerV2, Actions read/write). Never returned,
//    logged or audited.
//  - Server-side allowlist of exact workflow IDs, fixed ref and permitted
//    inputs. The request may name only a job id; repository, workflow path,
//    branch, commit and inputs are never taken from the client.
//  - Duplicate protection: an atomic per-job lock in a Durable Object plus a
//    check of GitHub's own queued/running runs before dispatching.
//  - Append-only audit (Durable Object SQLite; insert-only code path) for every
//    accepted, rejected or failed dispatch, without credentials or bodies.
//  - Rate limits: per job, global, and on failed authentication.
//
// Without both secrets the dispatcher reports dispatch_enabled:false and every
// POST /dispatch returns 503; nothing is ever dispatched.

export const SERVICE = 'cryptopulse-research-dispatcher';
export const REPO = Object.freeze({ owner: 'quiquandon-oss', name: 'PulseWorkerV2' });
export const ALLOWED_ORIGINS = Object.freeze(['https://quiquandon-oss.github.io']);

// Exact allowlist. Adding a job requires a reviewed code change here.
export const ALLOWLIST = Object.freeze({
  'prediction-freshness-check': Object.freeze({
    workflowId: 381092876,
    workflowPath: '.github/workflows/prediction-freshness-check.yml',
    ref: 'main',
    inputs: Object.freeze({}), // the workflow takes no inputs; none are forwarded
    minIntervalMs: 2 * 60_000,
    description: 'Read-only V2 persistence check (public endpoints, contents: read, no secrets).',
  }),
});

export const LIMITS = Object.freeze({
  globalPerHour: 12,
  authFailuresPerHour: 10,
  lockTtlMs: 3 * 60_000,
  bodyMaxBytes: 1024,
  githubTimeoutMs: 10_000,
});

const ALLOWED_BODY_KEYS = new Set(['job', 'confirm', 'request_id']);
const JOB_ID_RE = /^[a-z0-9-]{1,64}$/;
const REQUEST_ID_RE = /^[A-Za-z0-9-]{8,64}$/;

// ---------------------------------------------------------------- helpers

function json(body, status, origin, extra = {}) {
  const headers = { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', ...extra };
  if (origin && ALLOWED_ORIGINS.includes(origin)) {
    headers['Access-Control-Allow-Origin'] = origin;
    headers['Vary'] = 'Origin';
    headers['Access-Control-Expose-Headers'] = 'Retry-After';
  }
  return new Response(JSON.stringify(body), { status, headers });
}

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
}

export function constantTimeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  let diff = a.length ^ b.length;
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

export function dispatchEnabled(env) {
  return !!(env && typeof env.GITHUB_DISPATCH_TOKEN === 'string' && env.GITHUB_DISPATCH_TOKEN.length >= 20
    && typeof env.OWNER_KEY_SHA256 === 'string' && /^[0-9a-f]{64}$/.test(env.OWNER_KEY_SHA256));
}

async function authenticate(request, env) {
  const h = request.headers.get('Authorization') || '';
  const m = /^Bearer ([\x21-\x7e]{24,256})$/.exec(h);
  if (!m) return { ok: false, code: h ? 'invalid_auth' : 'missing_auth' };
  const digest = await sha256Hex(m[1]);
  return constantTimeEqual(digest, env.OWNER_KEY_SHA256) ? { ok: true } : { ok: false, code: 'invalid_auth' };
}

/** Strict body validation: only {job, confirm, request_id}; job must be allowlisted; no inputs accepted. */
export function validateBody(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return { ok: false, code: 'invalid_body', message: 'Body must be a JSON object.' };
  for (const k of Object.keys(body)) {
    if (!ALLOWED_BODY_KEYS.has(k)) return { ok: false, code: 'field_not_allowed', message: `Field "${k.slice(0, 32)}" is not accepted. Repository, workflow, branch, commit and inputs are fixed server-side.` };
  }
  if (typeof body.job !== 'string' || !JOB_ID_RE.test(body.job)) return { ok: false, code: 'invalid_job', message: 'job must be an allowlisted job id.' };
  if (!Object.prototype.hasOwnProperty.call(ALLOWLIST, body.job)) return { ok: false, code: 'job_not_allowed', message: `Job "${body.job}" is not allowlisted.`, job: body.job };
  if (body.confirm !== true) return { ok: false, code: 'confirmation_required', message: 'confirm must be true (explicit confirmation).', job: body.job };
  if (body.request_id !== undefined && (typeof body.request_id !== 'string' || !REQUEST_ID_RE.test(body.request_id))) {
    return { ok: false, code: 'invalid_request_id', message: 'request_id must be 8-64 letters, digits or dashes.', job: body.job };
  }
  return { ok: true, job: body.job, requestId: body.request_id || null };
}

async function githubFetch(deps, env, path, init = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), LIMITS.githubTimeoutMs);
  try {
    return await deps.fetch(`https://api.github.com${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: { Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': SERVICE,
        Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`, ...(init.headers || {}) },
    });
  } finally {
    clearTimeout(timer);
  }
}

/** Map a non-OK GitHub response (never forwarding its body) to a dispatcher error. */
export function upstreamError(res) {
  const remaining = res.headers.get('x-ratelimit-remaining');
  if (res.status === 429 || (res.status === 403 && remaining === '0')) {
    const reset = Number(res.headers.get('x-ratelimit-reset'));
    const retry = Number.isFinite(reset) && reset > 0 ? Math.max(1, Math.ceil(reset - Date.now() / 1000)) : 60;
    return { status: 503, code: 'upstream_rate_limited', message: 'GitHub API rate limit reached.', retryAfter: retry, upstream: res.status };
  }
  if (res.status === 401 || res.status === 403) return { status: 502, code: 'upstream_auth', message: 'GitHub rejected the dispatcher credential (expired or insufficient permission).', upstream: res.status };
  if (res.status === 404) return { status: 502, code: 'upstream_not_found', message: 'GitHub did not find the allowlisted workflow.', upstream: res.status };
  if (res.status === 422) return { status: 502, code: 'upstream_rejected', message: 'GitHub rejected the dispatch request.', upstream: res.status };
  return { status: 502, code: 'upstream_error', message: `GitHub returned HTTP ${res.status}.`, upstream: res.status };
}

// ---------------------------------------------------------------- main handler

/**
 * @param {Request} request
 * @param {object} env  secrets/vars (GITHUB_DISPATCH_TOKEN, OWNER_KEY_SHA256, GIT_COMMIT_SHA)
 * @param {{fetch:Function, guard:object, now:Function, uuid:Function}} deps
 */
export async function handle(request, env, deps) {
  const url = new URL(request.url);
  const origin = request.headers.get('Origin');
  const now = deps.now();
  const enabled = dispatchEnabled(env);

  if (request.method === 'OPTIONS') {
    if (!origin || !ALLOWED_ORIGINS.includes(origin)) return new Response(null, { status: 403 });
    return new Response(null, { status: 204, headers: { 'Access-Control-Allow-Origin': origin, Vary: 'Origin',
      'Access-Control-Allow-Methods': 'GET, POST', 'Access-Control-Allow-Headers': 'Authorization, Content-Type', 'Access-Control-Max-Age': '600' } });
  }
  const ipHash = (await sha256Hex(`cprd:${request.headers.get('CF-Connecting-IP') || 'unknown'}`)).slice(0, 16);
  const audit = (entry) => deps.guard.appendAudit({ ts: now, ip_hash: ipHash, origin: origin || null, ...entry });
  const isDispatch = url.pathname === '/dispatch' && request.method === 'POST';
  const isAudit = url.pathname === '/audit' && request.method === 'GET';

  if (origin && !ALLOWED_ORIGINS.includes(origin)) {
    if (isDispatch) await audit({ outcome: 'rejected', code: 'origin_not_allowed', http_status: 403, origin: origin.slice(0, 120) });
    return json({ ok: false, code: 'origin_not_allowed' }, 403, null);
  }

  if (url.pathname === '/health' && request.method === 'GET') {
    return json({ ok: true, service: SERVICE, version: env.GIT_COMMIT_SHA || 'unknown', dispatch_enabled: enabled, jobs: Object.keys(ALLOWLIST) }, 200, origin);
  }

  if (!isDispatch && !isAudit) return json({ ok: false, code: 'not_found' }, 404, origin);

  if (!enabled) {
    if (isDispatch) await audit({ outcome: 'rejected', code: 'dispatch_disabled', http_status: 503 });
    return json({ ok: false, code: 'dispatch_disabled', message: 'Dispatch is disabled: the dispatcher secrets are not provisioned.' }, 503, origin);
  }

  // Failed-authentication rate limit, checked before authenticating.
  const af = await deps.guard.rateCheck(`authfail:${ipHash}`, LIMITS.authFailuresPerHour, 3_600_000, now, false);
  if (!af.allowed) {
    if (isDispatch) await audit({ outcome: 'rejected', code: 'auth_rate_limited', http_status: 429 });
    return json({ ok: false, code: 'rate_limited', message: 'Too many failed authentication attempts.' }, 429, origin, { 'Retry-After': String(Math.ceil(af.retryAfterMs / 1000)) });
  }
  const auth = await authenticate(request, env);
  if (!auth.ok) {
    await deps.guard.rateCheck(`authfail:${ipHash}`, LIMITS.authFailuresPerHour, 3_600_000, now, true);
    if (isDispatch) await audit({ outcome: 'rejected', code: auth.code, http_status: 401 });
    return json({ ok: false, code: 'unauthorized', message: 'Owner authentication required.' }, 401, origin, { 'WWW-Authenticate': 'Bearer' });
  }

  if (isAudit) return json({ ok: true, entries: await deps.guard.listAudit(50) }, 200, origin);

  // ---- POST /dispatch
  const ct = request.headers.get('Content-Type') || '';
  if (!/^application\/json\b/i.test(ct)) {
    await audit({ outcome: 'rejected', code: 'invalid_content_type', http_status: 415 });
    return json({ ok: false, code: 'invalid_content_type', message: 'Content-Type must be application/json.' }, 415, origin);
  }
  const raw = await request.text();
  if (raw.length > LIMITS.bodyMaxBytes) {
    await audit({ outcome: 'rejected', code: 'body_too_large', http_status: 413 });
    return json({ ok: false, code: 'body_too_large' }, 413, origin);
  }
  let body;
  try { body = JSON.parse(raw); } catch (_) { body = null; }
  const v = validateBody(body);
  if (!v.ok) {
    const job = v.job && JOB_ID_RE.test(v.job) ? v.job : null;
    await audit({ outcome: 'rejected', code: v.code, http_status: v.code === 'job_not_allowed' ? 403 : 400, job });
    return json({ ok: false, code: v.code, message: v.message }, v.code === 'job_not_allowed' ? 403 : 400, origin);
  }
  const spec = ALLOWLIST[v.job];
  const base = { job: v.job, request_id: v.requestId };

  const g = await deps.guard.rateCheck('dispatch:global', LIMITS.globalPerHour, 3_600_000, now, false);
  const j = await deps.guard.rateCheck(`dispatch:${v.job}`, 1, spec.minIntervalMs, now, false);
  if (!g.allowed || !j.allowed) {
    const retry = Math.ceil(Math.max(g.retryAfterMs || 0, j.retryAfterMs || 0) / 1000);
    await audit({ ...base, outcome: 'rejected', code: 'rate_limited', http_status: 429 });
    return json({ ok: false, code: 'rate_limited', message: !j.allowed ? 'This job was dispatched recently.' : 'Hourly dispatch limit reached.' }, 429, origin, { 'Retry-After': String(retry) });
  }

  if (!(await deps.guard.acquire(`job:${v.job}`, LIMITS.lockTtlMs, now))) {
    await audit({ ...base, outcome: 'rejected', code: 'dispatch_in_flight', http_status: 409 });
    return json({ ok: false, code: 'conflict', reason: 'dispatch_in_flight', message: 'A dispatch for this job is already in flight.' }, 409, origin);
  }

  let keepLock = false;
  try {
    // 1. the workflow must still be the allowlisted file and active
    const wf = await githubFetch(deps, env, `/repos/${REPO.owner}/${REPO.name}/actions/workflows/${spec.workflowId}`);
    if (!wf.ok) { const e = upstreamError(wf); await audit({ ...base, outcome: 'failed', code: e.code, http_status: e.status, upstream_status: e.upstream }); return json({ ok: false, code: e.code, message: e.message }, e.status, origin, e.retryAfter ? { 'Retry-After': String(e.retryAfter) } : {}); }
    const wfBody = await wf.json();
    if (wfBody.path !== spec.workflowPath || wfBody.state !== 'active') {
      await audit({ ...base, outcome: 'failed', code: 'workflow_mismatch', http_status: 502 });
      return json({ ok: false, code: 'workflow_mismatch', message: 'The allowlisted workflow is missing, renamed or disabled.' }, 502, origin);
    }
    // 2. no queued or running run of this workflow (GitHub's own state)
    const runsRes = await githubFetch(deps, env, `/repos/${REPO.owner}/${REPO.name}/actions/workflows/${spec.workflowId}/runs?per_page=10`);
    if (!runsRes.ok) { const e = upstreamError(runsRes); await audit({ ...base, outcome: 'failed', code: e.code, http_status: e.status, upstream_status: e.upstream }); return json({ ok: false, code: e.code, message: e.message }, e.status, origin, e.retryAfter ? { 'Retry-After': String(e.retryAfter) } : {}); }
    const runs = ((await runsRes.json()).workflow_runs || []);
    const active = runs.find(r => r && r.status !== 'completed');
    if (active) {
      await audit({ ...base, outcome: 'rejected', code: 'run_active', http_status: 409, run_id: active.id });
      return json({ ok: false, code: 'conflict', reason: 'run_active', run_id: active.id, message: `Run #${active.id} is ${active.status}; a second run is not started.` }, 409, origin);
    }
    // 3. dispatch: fixed ref, no client inputs
    const res = await githubFetch(deps, env, `/repos/${REPO.owner}/${REPO.name}/actions/workflows/${spec.workflowId}/dispatches`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ref: spec.ref, inputs: { ...spec.inputs } }),
    });
    if (res.status !== 204) {
      const e = upstreamError(res);
      await audit({ ...base, outcome: 'failed', code: e.code, http_status: e.status, upstream_status: e.upstream });
      return json({ ok: false, code: e.code, message: e.message }, e.status, origin, e.retryAfter ? { 'Retry-After': String(e.retryAfter) } : {});
    }
    keepLock = true; // hold the lock until GitHub has had time to list the run
    await deps.guard.rateCheck('dispatch:global', LIMITS.globalPerHour, 3_600_000, now, true);
    await deps.guard.rateCheck(`dispatch:${v.job}`, 1, spec.minIntervalMs, now, true);
    const dispatchId = deps.uuid();
    await audit({ ...base, outcome: 'accepted', code: 'dispatched', http_status: 202, upstream_status: 204, dispatch_id: dispatchId });
    return json({ ok: true, status: 'accepted', dispatch_id: dispatchId, job: v.job, workflow_id: spec.workflowId, workflow_path: spec.workflowPath, ref: spec.ref,
      accepted_at: now, note: 'GitHub accepted the dispatch. The run has not started until GitHub lists it.' }, 202, origin);
  } catch (e) {
    const timeout = e && e.name === 'AbortError';
    await audit({ ...base, outcome: 'failed', code: timeout ? 'upstream_timeout' : 'internal_error', http_status: timeout ? 504 : 500 });
    return json({ ok: false, code: timeout ? 'upstream_timeout' : 'internal_error', message: timeout ? 'GitHub did not respond in time.' : 'Dispatcher error.' }, timeout ? 504 : 500, origin);
  } finally {
    if (!keepLock) await deps.guard.release(`job:${v.job}`);
  }
}

// ---------------------------------------------------------------- guard (rate limits, locks, append-only audit)

const AUDIT_FIELDS = ['ts', 'outcome', 'code', 'job', 'http_status', 'upstream_status', 'request_id', 'dispatch_id', 'run_id', 'origin', 'ip_hash'];

/** Keeps only known, non-secret audit fields. */
export function sanitizeAudit(entry) {
  const out = {};
  for (const k of AUDIT_FIELDS) out[k] = entry[k] === undefined ? null : entry[k];
  return out;
}

/** In-memory guard with the same interface as the Durable Object (used by tests). */
export class MemoryGuard {
  constructor() { this.events = []; this.locks = new Map(); this.audit = []; }
  async rateCheck(key, limit, windowMs, now, record) {
    this.events = this.events.filter(e => e.ts > now - 3_600_000 * 2);
    const hits = this.events.filter(e => e.key === key && e.ts > now - windowMs).sort((a, b) => a.ts - b.ts);
    if (record) { this.events.push({ key, ts: now }); return { allowed: true }; }
    if (hits.length >= limit) return { allowed: false, retryAfterMs: hits[0].ts + windowMs - now };
    return { allowed: true };
  }
  async acquire(key, ttlMs, now) {
    const until = this.locks.get(key);
    if (until && until > now) return false;
    this.locks.set(key, now + ttlMs);
    return true;
  }
  async release(key) { this.locks.delete(key); }
  async appendAudit(entry) { this.audit.push(sanitizeAudit(entry)); }
  async listAudit(limit) { return this.audit.slice(-limit).reverse(); }
}

/** SQLite-backed guard used inside the DispatchGuard Durable Object (index.js). The Durable Object is a single
 *  instance and runs one call at a time, so each lock and limit check is atomic. */
export class SqlGuard {
  constructor(sql) {
    this.sql = sql;
    this.sql.exec('CREATE TABLE IF NOT EXISTS events (key TEXT NOT NULL, ts INTEGER NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS locks (key TEXT PRIMARY KEY, until INTEGER NOT NULL)');
    this.sql.exec(`CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ${AUDIT_FIELDS.map(f => `${f} ${f.endsWith('status') || f === 'ts' || f === 'run_id' ? 'INTEGER' : 'TEXT'}`).join(', ')})`);
  }
  async rateCheck(key, limit, windowMs, now, record) {
    this.sql.exec('DELETE FROM events WHERE ts < ?', now - 3_600_000 * 2);
    if (record) { this.sql.exec('INSERT INTO events (key, ts) VALUES (?, ?)', key, now); return { allowed: true }; }
    const rows = this.sql.exec('SELECT ts FROM events WHERE key = ? AND ts > ? ORDER BY ts ASC', key, now - windowMs).toArray();
    if (rows.length >= limit) return { allowed: false, retryAfterMs: rows[0].ts + windowMs - now };
    return { allowed: true };
  }
  async acquire(key, ttlMs, now) {
    const row = this.sql.exec('SELECT until FROM locks WHERE key = ?', key).toArray()[0];
    if (row && row.until > now) return false;
    this.sql.exec('INSERT OR REPLACE INTO locks (key, until) VALUES (?, ?)', key, now + ttlMs);
    return true;
  }
  async release(key) { this.sql.exec('DELETE FROM locks WHERE key = ?', key); }
  async appendAudit(entry) { // insert-only: no code path updates or deletes audit rows
    const e = sanitizeAudit(entry);
    this.sql.exec(`INSERT INTO audit (${AUDIT_FIELDS.join(', ')}) VALUES (${AUDIT_FIELDS.map(() => '?').join(', ')})`, ...AUDIT_FIELDS.map(f => e[f]));
  }
  async listAudit(limit) {
    return this.sql.exec(`SELECT ${AUDIT_FIELDS.join(', ')} FROM audit ORDER BY id DESC LIMIT ?`, Math.min(200, limit)).toArray();
  }
}
