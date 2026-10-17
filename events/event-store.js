// Event Detection Layer -- D1 persistence (tables from migration 0023 only). Idempotent writes:
// events and observations use INSERT ... ON CONFLICT DO NOTHING (uniqueness only -- a CHECK violation still raises,
// so a malformed row is never silently dropped); source health is a per-source upsert.

export const EVENT_TABLES_MISSING = { ok: false, available: false, status: 503, error: 'Event detection tables are not present (migration 0023 not applied).' };

export async function eventTablesPresent(env) {
  try { await env.DB.prepare('SELECT 1 FROM event_ledger LIMIT 1').all(); return true; } catch (_e) { return false; }
}

export async function insertEvent(env, ev, nowTs) {
  const r = await env.DB.prepare(
    `INSERT INTO event_ledger (event_id, dedup_key, event_type, asset, market, scope, source, source_url, source_ts,
       detected_ts, direction, severity, confidence, rule_id, rule_version, observed_value, baseline_value,
       threshold_value, unit, evidence_json, explanation, freshness_ms, source_health, status, expires_ts,
       correlation_key, created_ts)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
     ON CONFLICT(dedup_key) DO NOTHING`
  ).bind(
    ev.event_id, ev.dedup_key, ev.event_type, ev.asset, ev.market, ev.scope, ev.source, ev.source_url, ev.source_ts,
    ev.detected_ts, ev.direction, ev.severity, ev.confidence, ev.rule_id, ev.rule_version, ev.observed_value,
    ev.baseline_value, ev.threshold_value, ev.unit, JSON.stringify(ev.evidence || []), ev.explanation, ev.freshness_ms,
    ev.source_health, ev.status, ev.expires_ts, ev.correlation_key, nowTs
  ).run();
  return !!(r && r.meta && r.meta.changes);
}

export async function insertObservations(env, rows) {
  let written = 0;
  for (const o of rows) {
    const r = await env.DB.prepare(
      `INSERT INTO event_observations (source, series, bucket_ts, value, unit, coverage, observed_ts)
       VALUES (?,?,?,?,?,?,?) ON CONFLICT(source, series, bucket_ts) DO NOTHING`
    ).bind(o.source, o.series, o.bucket_ts, o.value, o.unit || null, o.coverage || 'COMPLETE', o.observed_ts).run();
    if (r && r.meta && r.meta.changes) written += 1;
  }
  return written;
}

// Point-in-time read: only observations this Worker had in hand at asOfTs.
export async function observationsBefore(env, source, series, { fromTs, beforeTs, asOfTs }) {
  const r = await env.DB.prepare(
    `SELECT bucket_ts, value, coverage, observed_ts FROM event_observations
     WHERE source = ? AND series = ? AND bucket_ts >= ? AND bucket_ts < ? AND observed_ts <= ?
     ORDER BY bucket_ts`
  ).bind(source, series, fromTs, beforeTs, asOfTs).all();
  return (r && r.results) || [];
}

export async function upsertHealth(env, h, nowTs) {
  await env.DB.prepare(
    `INSERT INTO event_source_health (source, status, last_attempt_ts, last_success_ts, last_observation_ts, last_error,
       coverage, details_json, updated_ts)
     VALUES (?,?,?,?,?,?,?,?,?)
     ON CONFLICT(source) DO UPDATE SET
       status = excluded.status,
       last_attempt_ts = excluded.last_attempt_ts,
       last_success_ts = COALESCE(excluded.last_success_ts, event_source_health.last_success_ts),
       last_observation_ts = COALESCE(excluded.last_observation_ts, event_source_health.last_observation_ts),
       last_error = excluded.last_error,
       coverage = excluded.coverage,
       details_json = excluded.details_json,
       updated_ts = excluded.updated_ts`
  ).bind(h.source, h.status, h.last_attempt_ts, h.last_success_ts ?? null, h.last_observation_ts ?? null,
    h.last_error ?? null, h.coverage ?? null, JSON.stringify(h.details || {}), nowTs).run();
}

export async function recordRun(env, run) {
  await env.DB.prepare(
    `INSERT INTO event_runs (run_id, started_ts, finished_ts, trigger, summary_json) VALUES (?,?,?,?,?)
     ON CONFLICT(run_id) DO UPDATE SET finished_ts = excluded.finished_ts, summary_json = excluded.summary_json`
  ).bind(run.run_id, run.started_ts, run.finished_ts ?? null, run.trigger, JSON.stringify(run.summary || {})).run();
}

// Marks events past their expiry as EXPIRED (status only; evidence is never deleted).
export async function expireEvents(env, nowTs) {
  const r = await env.DB.prepare(
    `UPDATE event_ledger SET status = 'EXPIRED' WHERE status = 'ACTIVE' AND expires_ts IS NOT NULL AND expires_ts <= ?`
  ).bind(nowTs).run();
  return (r && r.meta && r.meta.changes) || 0;
}

export function eventView(r) {
  let evidence = [];
  try { evidence = JSON.parse(r.evidence_json || '[]'); } catch (_e) { evidence = []; }
  const { evidence_json, ...rest } = r;
  return { ...rest, evidence };
}

export async function listEvents(env, { sinceTs = 0, type = null, source = null, asset = null, status = null, limit = 50 } = {}) {
  const where = ['detected_ts >= ?'];
  const args = [sinceTs];
  if (type) { where.push('event_type = ?'); args.push(type); }
  if (source) { where.push('source = ?'); args.push(source); }
  if (asset) { where.push('asset = ?'); args.push(asset); }
  if (status) { where.push('status = ?'); args.push(status); }
  const r = await env.DB.prepare(
    `SELECT * FROM event_ledger WHERE ${where.join(' AND ')} ORDER BY detected_ts DESC, event_id LIMIT ?`
  ).bind(...args, limit).all();
  return ((r && r.results) || []).map(eventView);
}

export async function getEvent(env, eventId) {
  const row = await env.DB.prepare('SELECT * FROM event_ledger WHERE event_id = ?').bind(eventId).first();
  if (!row) return null;
  const ev = eventView(row);
  let related = [];
  if (ev.correlation_key) {
    const r = await env.DB.prepare(
      'SELECT event_id, event_type, asset, severity, direction, detected_ts FROM event_ledger WHERE correlation_key = ? AND event_id != ? ORDER BY detected_ts'
    ).bind(ev.correlation_key, eventId).all();
    related = (r && r.results) || [];
  }
  return { ...ev, related };
}

export async function listHealth(env) {
  const r = await env.DB.prepare('SELECT * FROM event_source_health ORDER BY source').all();
  return ((r && r.results) || []).map((h) => {
    let details = {};
    try { details = JSON.parse(h.details_json || '{}'); } catch (_e) { details = {}; }
    const { details_json, ...rest } = h;
    return { ...rest, details };
  });
}
