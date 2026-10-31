// Event Detection Layer -- HTTP API (read-only GETs; one token-gated POST to run detection on demand).
//   GET  /api/events?since=<ms|ISO>&type=&source=&asset=&status=&limit=   recent events with evidence
//   GET  /api/event?id=ev_...                                              one event + correlated events
//   GET  /api/events/health                                                detector health, freshness, existing-source inventory
//   POST /api/events/run   (Authorization: Bearer <EVENT_RUN_TOKEN or STAGE7_ADMIN_TOKEN>)   run all detectors once
//        header X-Event-Trigger: schedule  -> refused (409) unless EVENT_DETECTION_ENABLED is '1'
// Each POST is its own Worker invocation with its own subrequest/D1 budget; detection never shares an invocation
// with the prediction cron.
// Responses always say these are derived events, separate from V1 composite scores.
import { RULESET_VERSION, SOURCE_STATUS } from './event-core.js';
import { eventTablesPresent, listEvents, getEvent, listHealth, EVENT_TABLES_MISSING } from './event-store.js';
import { detectorRegistry, EXISTING_SOURCES, runEventDetection } from './event-runner.js';
import { constantTimeEqual } from '../learning/learning-api.js';

export const SEPARATION_NOTE = 'Derived events from the Event Detection Layer. They are not V1 composite inputs, scores or trading signals; severity, confidence and direction are independent fields.';

export const RUN_MIN_INTERVAL_MS = 2 * 60_000;
// Automatic triggers (the Worker's hourly cron and the GitHub schedule fallback): at most one run per 30 minutes,
// whichever fires first; the other is skipped (200, skipped: true), so the two never duplicate work in one hour.
export const AUTO_RUN_MIN_INTERVAL_MS = 30 * 60_000;

async function runStartedSince(env, sinceTs) {
  return env.DB.prepare('SELECT run_id, started_ts FROM event_runs WHERE started_ts > ? ORDER BY started_ts DESC LIMIT 1').bind(sinceTs).first();
}

// One automatic detection run ('cron' from scheduled(), 'schedule' from the GitHub workflow). Refuses unless
// EVENT_DETECTION_ENABLED is '1'; skips when another run started in the last AUTO_RUN_MIN_INTERVAL_MS.
export async function runAutomaticDetection(env, { trigger, nowTs = Date.now(), run = runEventDetection } = {}) {
  if (env.EVENT_DETECTION_ENABLED !== '1') {
    return { ok: false, status: 409, source_status: SOURCE_STATUS.DISABLED, error: 'Scheduled event detection is off (EVENT_DETECTION_ENABLED is not "1").' };
  }
  if (!(await eventTablesPresent(env))) return { ...EVENT_TABLES_MISSING };
  const recent = await runStartedSince(env, nowTs - AUTO_RUN_MIN_INTERVAL_MS);
  if (recent) return { ok: true, skipped: true, trigger, reason: `run ${recent.run_id} started less than ${AUTO_RUN_MIN_INTERVAL_MS / 60_000} min ago` };
  return run(env, { trigger, nowTs });
}

// Dedicated EVENT_RUN_TOKEN (least privilege: it can only trigger a detection run) or the existing admin token.
export function runAuthorization(env, request) {
  const auth = request.headers.get('Authorization') || '';
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
  const accepted = [env.EVENT_RUN_TOKEN, env.STAGE7_ADMIN_TOKEN].filter((t) => typeof t === 'string' && t.length >= 16);
  if (!accepted.length) return { ok: false, status: 503, error: 'Event runs are disabled on this Worker: neither EVENT_RUN_TOKEN nor STAGE7_ADMIN_TOKEN is configured.' };
  if (!token || !accepted.some((t) => constantTimeEqual(token, t))) return { ok: false, status: 401, error: 'Unauthorized' };
  return { ok: true };
}

function parseSince(v, nowTs) {
  if (v === null || v === undefined || v === '') return nowTs - 7 * 24 * 3600_000;
  const n = Number(v);
  if (Number.isInteger(n) && n >= 0) return n;
  const t = Date.parse(v);
  return Number.isFinite(t) ? t : null;
}

export async function eventsResponse(env, params, nowTs = Date.now()) {
  if (!(await eventTablesPresent(env))) return { ...EVENT_TABLES_MISSING, note: SEPARATION_NOTE };
  const sinceTs = parseSince(params.get('since'), nowTs);
  if (sinceTs === null) return { ok: false, status: 400, error: 'since must be epoch ms or an ISO-8601 date' };
  const limitRaw = Number(params.get('limit') || 50);
  const limit = Number.isInteger(limitRaw) && limitRaw > 0 ? Math.min(limitRaw, 200) : 50;
  const events = await listEvents(env, { sinceTs, type: params.get('type'), source: params.get('source'), asset: params.get('asset'), status: params.get('status'), limit });
  return { ok: true, available: true, ruleset: RULESET_VERSION, since_ts: sinceTs, generated_ts: nowTs, count: events.length, events, note: SEPARATION_NOTE };
}

export async function healthResponse(env, nowTs = Date.now()) {
  const registry = detectorRegistry(env);
  const enabled = env.EVENT_DETECTION_ENABLED === '1';
  if (!(await eventTablesPresent(env))) {
    return { ...EVENT_TABLES_MISSING, enabled, detectors: registry.map((d) => ({ ...d, status: SOURCE_STATUS.NEVER_RUN })), existing_sources: EXISTING_SOURCES, note: SEPARATION_NOTE };
  }
  const rows = new Map((await listHealth(env)).map((h) => [h.source, h]));
  const detectors = registry.map((d) => {
    const h = rows.get(d.id);
    if (!h) return { ...d, status: d.enabled ? SOURCE_STATUS.NEVER_RUN : SOURCE_STATUS.DISABLED, age_ms: null, stale: null };
    const age = h.last_observation_ts ? nowTs - h.last_observation_ts : null;
    const stale = age === null ? null : age > d.freshness_limit_ms;
    return { ...d, ...h, status: stale && h.status === SOURCE_STATUS.OK ? SOURCE_STATUS.STALE : h.status, age_ms: age, stale };
  });
  let lastRun = null;
  try {
    const r = await env.DB.prepare('SELECT run_id, started_ts, finished_ts, trigger, summary_json FROM event_runs ORDER BY started_ts DESC LIMIT 1').first();
    if (r) { let s = {}; try { s = JSON.parse(r.summary_json || '{}'); } catch (_e) { s = {}; } lastRun = { run_id: r.run_id, started_ts: r.started_ts, finished_ts: r.finished_ts, trigger: r.trigger, summary: s }; }
  } catch (_e) { lastRun = null; }
  return { ok: true, available: true, enabled, ruleset: RULESET_VERSION, generated_ts: nowTs, detectors, last_run: lastRun, existing_sources: EXISTING_SOURCES, note: SEPARATION_NOTE };
}

// Returns a Response for /api/events*, /api/event, or null when the path is not ours.
export async function handleEventRoutes(request, env, url, corsHeaders) {
  if (!(url.pathname === '/api/event' || url.pathname === '/api/events' || url.pathname.startsWith('/api/events/'))) return null;
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { ...corsHeaders, 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
  const send = (r) => json(r, r.ok ? 200 : (r.status || 500));
  try {
    if (url.pathname === '/api/events' && request.method === 'GET') return send(await eventsResponse(env, url.searchParams));
    if (url.pathname === '/api/events/health' && request.method === 'GET') return send(await healthResponse(env));
    if (url.pathname === '/api/event' && request.method === 'GET') {
      const id = String(url.searchParams.get('id') || '');
      if (!/^ev_[0-9a-f]{24}$/.test(id)) return json({ ok: false, error: 'id must look like ev_<24 hex>' }, 400);
      if (!(await eventTablesPresent(env))) return send({ ...EVENT_TABLES_MISSING });
      const ev = await getEvent(env, id);
      return ev ? json({ ok: true, event: ev, note: SEPARATION_NOTE }) : json({ ok: false, error: 'not found' }, 404);
    }
    if (url.pathname === '/api/events/run' && request.method === 'POST') {
      const auth = runAuthorization(env, request);
      if (!auth.ok) return json({ ok: false, error: auth.error }, auth.status);
      const trigger = request.headers.get('X-Event-Trigger') === 'schedule' ? 'schedule' : 'manual';
      if (trigger === 'schedule') {
        const r = await runAutomaticDetection(env, { trigger });
        if (r.source_status === SOURCE_STATUS.DISABLED) return json({ ok: false, status: SOURCE_STATUS.DISABLED, error: r.error }, 409);
        return send(r);
      }
      if (!(await eventTablesPresent(env))) return send({ ...EVENT_TABLES_MISSING });
      const recent = await runStartedSince(env, Date.now() - RUN_MIN_INTERVAL_MS);
      if (recent) return json({ ok: false, error: `a run started less than ${RUN_MIN_INTERVAL_MS / 60_000} min ago (${recent.run_id})` }, 409);
      return send(await runEventDetection(env, { trigger }));
    }
    return json({ ok: false, error: 'method not allowed' }, 405);
  } catch (e) {
    return json({ ok: false, error: String((e && e.message) || e).slice(0, 300) }, 500);
  }
}
