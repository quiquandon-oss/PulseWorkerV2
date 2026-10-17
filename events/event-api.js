// Event Detection Layer -- HTTP API (read-only GETs; one token-gated POST to run detection on demand).
//   GET  /api/events?since=<ms|ISO>&type=&source=&asset=&status=&limit=   recent events with evidence
//   GET  /api/event?id=ev_...                                              one event + correlated events
//   GET  /api/events/health                                                detector health, freshness, existing-source inventory
//   POST /api/events/run   (Authorization: Bearer <STAGE7_ADMIN_TOKEN>)    run all detectors once
// Responses always say these are derived events, separate from V1 composite scores.
import { RULESET_VERSION, SOURCE_STATUS } from './event-core.js';
import { eventTablesPresent, listEvents, getEvent, listHealth, EVENT_TABLES_MISSING } from './event-store.js';
import { detectorRegistry, EXISTING_SOURCES, runEventDetection } from './event-runner.js';
import { checkToken } from '../learning/learning-api.js';

export const SEPARATION_NOTE = 'Derived events from the Event Detection Layer. They are not V1 composite inputs, scores or trading signals; severity, confidence and direction are independent fields.';

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
      const auth = request.headers.get('Authorization') || '';
      const denied = checkToken(env, auth.startsWith('Bearer ') ? auth.slice(7) : '');
      if (denied) return json({ ok: false, error: denied.error }, denied.status);
      return send(await runEventDetection(env, { trigger: 'manual' }));
    }
    return json({ ok: false, error: 'method not allowed' }, 405);
  } catch (e) {
    return json({ ok: false, error: String((e && e.message) || e).slice(0, 300) }, 500);
  }
}
