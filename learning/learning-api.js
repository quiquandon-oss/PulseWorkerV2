// Learning loop -- D1 layer for /api/learning/*. Reads existing tables only (research_events, research_event_evidence,
// research_sentiment_archive, history, btc_data, stage7_research_requests, stage7_research_responses). The ONLY
// writes are (1) opening a Research Case = one stage7_research_requests row and (2) confirming a finding, which goes
// through worker.js's existing registerStage7ResearchResponse. Both require STAGE7_ADMIN_TOKEN, fail closed when it is
// not configured, and touch nothing else: no V1 weights, predictions, sentiment rows or methodology.
import {
  ASSESSMENT_RULES, V1_METHODOLOGY_V1, HOUR, nearestAtOrBefore, describeMarketEvent, assessV1Sources,
  buildResearchCase, buildResearchPack, findingToStage7Registration, learningCycleStage, VERDICT_TEXT,
} from './learning-core.js';

const BTC_MAX_GAP_MS = 4 * HOUR;
// How far before an event we must read so the "24h before" lookups can find their nearest observation.
const LOOKBACK_MS = ASSESSMENT_RULES.windowMs + Math.max(ASSESSMENT_RULES.maxLookupGapMs, BTC_MAX_GAP_MS);
const EVENT_COLUMNS = 'event_id, event_ts, detection_ts, category, direction, intensity, trigger_metric, trigger_threshold';

function parseJson(raw, fallback) {
  if (typeof raw !== 'string') return fallback;
  try { return JSON.parse(raw); } catch (_e) { return fallback; }
}

export function constantTimeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const n = Math.max(a.length, b.length);
  let diff = a.length === b.length ? 0 : 1;
  for (let i = 0; i < n; i++) diff |= (i < a.length ? a.charCodeAt(i) : 0) ^ (i < b.length ? b.charCodeAt(i) : 0);
  return diff === 0;
}

function checkToken(env, providedToken) {
  if (!env.STAGE7_ADMIN_TOKEN) return { ok: false, status: 503, error: 'Writing is disabled on this Worker: STAGE7_ADMIN_TOKEN is not configured.' };
  if (typeof providedToken !== 'string' || !providedToken || !constantTimeEqual(providedToken, env.STAGE7_ADMIN_TOKEN)) {
    return { ok: false, status: 401, error: 'Unauthorized' };
  }
  return null;
}

// V1 observations: the permanent EXP-005 archive first (no 500-row cap), then V1's own history table.
async function loadV1Observations(env, fromTs, toTs) {
  const rows = [];
  try {
    const a = await env.DB.prepare(
      'SELECT observation_ts AS ts, score, sources_json FROM research_sentiment_archive WHERE observation_ts BETWEEN ? AND ? ORDER BY observation_ts'
    ).bind(fromTs, toTs).all();
    for (const r of (a && a.results) || []) rows.push(r);
  } catch (_e) { /* archive not present on this database: history alone */ }
  const h = await env.DB.prepare(
    'SELECT ts, score, sources_json FROM history WHERE ts BETWEEN ? AND ? ORDER BY ts'
  ).bind(fromTs, toTs).all();
  for (const r of (h && h.results) || []) rows.push(r);
  rows.sort((x, y) => x.ts - y.ts);
  return rows;
}

async function loadBtc(env, fromTs, toTs) {
  const r = await env.DB.prepare('SELECT ts, btc_price FROM btc_data WHERE ts BETWEEN ? AND ? ORDER BY ts').bind(fromTs, toTs).all();
  return (r && r.results) || [];
}

function obsAt(rows, ts) {
  const row = nearestAtOrBefore(rows, ts, 'ts', ASSESSMENT_RULES.maxLookupGapMs);
  return row ? { ts: row.ts, score: row.score, sources: parseJson(row.sources_json, {}) } : null;
}

export function analyseEvent(event, v1Rows, btcRows, evidence) {
  const before = obsAt(v1Rows, event.event_ts - ASSESSMENT_RULES.windowMs);
  const at = obsAt(v1Rows, event.event_ts);
  const btcAt = nearestAtOrBefore(btcRows, event.event_ts, 'ts', BTC_MAX_GAP_MS);
  const btcBefore = nearestAtOrBefore(btcRows, event.event_ts - ASSESSMENT_RULES.windowMs, 'ts', BTC_MAX_GAP_MS);
  const described = describeMarketEvent(event, btcAt && btcBefore ? { atEvent: btcAt.btc_price, before: btcBefore.btc_price } : null);
  const assessment = assessV1Sources({ before, at, btcMovePct: described.btc_move_24h_pct });
  const researchCase = buildResearchCase(described, assessment, evidence);
  return { described, assessment, researchCase };
}

async function loadStage7ForEvents(env, eventIds) {
  if (!eventIds.length) return { available: true, byEvent: {} };
  try {
    const placeholders = eventIds.map(() => '?').join(',');
    const r = await env.DB.prepare(
      `SELECT r.request_id, r.event_id, r.status, r.created_ts, r.schema_version,
              resp.response_id, resp.validation_status, resp.registered_ts, resp.provider, resp.findings_json, resp.sources_json, resp.confidence
       FROM stage7_research_requests r
       LEFT JOIN stage7_research_responses resp ON resp.request_id = r.request_id
       WHERE r.event_id IN (${placeholders}) ORDER BY r.created_ts DESC`
    ).bind(...eventIds).all();
    const byEvent = {};
    for (const row of (r && r.results) || []) {
      if (!byEvent[row.event_id]) byEvent[row.event_id] = row; // newest case per event
    }
    return { available: true, byEvent };
  } catch (_e) {
    return { available: false, byEvent: {} };
  }
}

function caseView(row) {
  if (!row) return null;
  const findings = parseJson(row.findings_json, null);
  return {
    request_id: row.request_id, status: row.status, created_ts: row.created_ts,
    finding: row.response_id ? {
      response_id: row.response_id, validation_status: row.validation_status, registered_ts: row.registered_ts,
      provider: row.provider, confidence: row.confidence, findings, sources: parseJson(row.sources_json, []),
    } : null,
  };
}

// MARKET screen: recent events, each with its plain-language description, V1 assessment and loop stage.
export async function getLearningMarket(env, { limit = 15 } = {}) {
  const ev = await env.DB.prepare(`SELECT ${EVENT_COLUMNS} FROM research_events ORDER BY event_ts DESC LIMIT ?`).bind(limit).all();
  const events = (ev && ev.results) || [];
  const [latestBtc, latestV1] = await Promise.all([
    env.DB.prepare('SELECT ts, btc_price FROM btc_data ORDER BY ts DESC LIMIT 1').first(),
    env.DB.prepare('SELECT ts, score FROM history ORDER BY ts DESC LIMIT 1').first(),
  ]);
  if (!events.length) return { ok: true, latest: { btc: latestBtc || null, v1: latestV1 || null }, events: [], focus_event_id: null, stage7_available: false };
  const minTs = Math.min(...events.map((e) => e.event_ts)) - LOOKBACK_MS;
  const maxTs = Math.max(...events.map((e) => e.event_ts));
  const ids = events.map((e) => e.event_id);
  const [v1Rows, btcRows, evCounts, stage7] = await Promise.all([
    loadV1Observations(env, minTs, maxTs),
    loadBtc(env, minTs, maxTs),
    env.DB.prepare(`SELECT event_id, COUNT(*) AS n FROM research_event_evidence WHERE event_id IN (${ids.map(() => '?').join(',')}) GROUP BY event_id`).bind(...ids).all(),
    loadStage7ForEvents(env, ids),
  ]);
  const counts = {};
  for (const r of (evCounts && evCounts.results) || []) counts[r.event_id] = r.n;
  const out = events.map((event) => {
    const fakeEvidence = new Array(counts[event.event_id] || 0).fill({});
    const { described, assessment } = analyseEvent(event, v1Rows, btcRows, fakeEvidence);
    const cv = caseView(stage7.byEvent[event.event_id]);
    return {
      ...described,
      evidence_count: counts[event.event_id] || 0,
      verdict: assessment.verdict,
      verdict_text: VERDICT_TEXT[assessment.verdict],
      explained_share: assessment.explained_share,
      actual_direction: assessment.actual_direction,
      v1_lean_before: assessment.v1_lean_before,
      v1_called_it: assessment.v1_called_it,
      groups: assessment.groups,
      stage: learningCycleStage(cv, cv && cv.finding),
      case: cv,
    };
  });
  const needsResearch = (e) => (e.verdict === 'NOT_EXPLAINED' || e.verdict === 'PARTIALLY_EXPLAINED') && !(e.case && e.case.finding);
  const focus = out.find(needsResearch) || out[0];
  return {
    ok: true,
    latest: { btc: latestBtc || null, v1: latestV1 || null },
    methodology_version: V1_METHODOLOGY_V1.version,
    rules_version: ASSESSMENT_RULES.version,
    stage7_available: stage7.available,
    focus_event_id: focus ? focus.event_id : null,
    events: out,
  };
}

async function loadEvent(env, eventId) {
  return env.DB.prepare(`SELECT ${EVENT_COLUMNS} FROM research_events WHERE event_id = ?`).bind(eventId).first();
}

async function loadEvidence(env, eventId) {
  const r = await env.DB.prepare(
    `SELECT evidence_id, publisher, headline, article_url, publication_ts, evidence_relation
     FROM research_event_evidence WHERE event_id = ? ORDER BY publication_ts ASC`
  ).bind(eventId).all();
  return (r && r.results) || [];
}

// RESEARCH screen for one event: the Research Case, the copyable Research Pack, and any case/finding already stored.
export async function getLearningCase(env, eventId) {
  const event = await loadEvent(env, eventId);
  if (!event) return { ok: false, status: 404, error: 'event_not_found' };
  const fromTs = event.event_ts - LOOKBACK_MS;
  const [evidence, v1Rows, btcRows, stage7] = await Promise.all([
    loadEvidence(env, eventId), loadV1Observations(env, fromTs, event.event_ts), loadBtc(env, fromTs, event.event_ts),
    loadStage7ForEvents(env, [eventId]),
  ]);
  const { described, assessment, researchCase } = analyseEvent(event, v1Rows, btcRows, evidence);
  const cv = caseView(stage7.byEvent[eventId]);
  return {
    ok: true,
    event: described,
    assessment: { ...assessment, verdict_text: VERDICT_TEXT[assessment.verdict] },
    research_case: researchCase,
    research_pack: buildResearchPack(described, assessment, researchCase, evidence),
    evidence: evidence.slice(0, 50).map((e) => ({ publisher: e.publisher, headline: e.headline, url: e.article_url, relation: e.evidence_relation, publication_ts: e.publication_ts })),
    stage: learningCycleStage(cv, cv && cv.finding),
    case: cv,
    stage7_available: stage7.available,
  };
}

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// Opens (or returns the already-open) Research Case for an event: one stage7_research_requests row.
export async function openResearchCase(env, { eventId, providedToken, now = Date.now() }) {
  const denied = checkToken(env, providedToken);
  if (denied) return denied;
  let existing;
  try {
    existing = await env.DB.prepare(
      "SELECT request_id, status FROM stage7_research_requests WHERE event_id = ? AND status NOT IN ('INTEGRATED', 'REJECTED') ORDER BY created_ts DESC LIMIT 1"
    ).bind(eventId).first();
  } catch (_e) {
    return { ok: false, status: 503, error: 'Research cases need the Stage 7 tables, which are not present on this database.' };
  }
  if (existing) return { ok: true, status: 200, request_id: existing.request_id, created: false };
  const view = await getLearningCase(env, eventId);
  if (!view.ok) return view;
  const evidence = await loadEvidence(env, eventId);
  const prior = await env.DB.prepare('SELECT COUNT(*) AS n FROM stage7_research_requests WHERE event_id = ?').bind(eventId).first();
  const requestId = `stage7-req-${eventId}-${((prior && prior.n) || 0) + 1}`;
  const evidenceIds = evidence.map((e) => e.evidence_id).sort((a, b) => a - b);
  const fingerprint = await sha256Hex(JSON.stringify({ event_id: eventId, evidence_ids: evidenceIds, rules: ASSESSMENT_RULES.version, methodology: V1_METHODOLOGY_V1.version }));
  const rc = view.research_case;
  await env.DB.prepare(
    `INSERT INTO stage7_research_requests
       (request_id, event_id, created_ts, updated_ts, schema_version, status, sufficiency_status, reasons_json,
        questions_json, missing_categories_json, historical_cutoff_ts, evidence_snapshot_json, publish_attempts, input_fingerprint)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).bind(
    requestId, eventId, now, now, 'learning-case-v1', 'PENDING_RESEARCH', rc.sufficiency_status,
    JSON.stringify(rc.reasons), JSON.stringify([rc.question]), JSON.stringify(rc.weak_or_missing_areas),
    view.event.event_ts,
    JSON.stringify({ evidence_ids: evidenceIds, verdict: view.assessment.verdict, explained_share: view.assessment.explained_share, methodology: V1_METHODOLOGY_V1.version, rules: ASSESSMENT_RULES.version }),
    0, fingerprint
  ).run();
  return { ok: true, status: 200, request_id: requestId, created: true };
}

// Human confirms a (possibly edited) finding: open the case if needed, then register through Stage 7's existing,
// already-reviewed write path (passed in from worker.js) with validated=true.
export async function confirmLearningFinding(env, { eventId, provider, finding, providedToken }, { registerStage7ResearchResponse }) {
  const denied = checkToken(env, providedToken);
  if (denied) return denied;
  if (!finding || typeof finding !== 'object') return { ok: false, status: 400, error: 'finding is required' };
  const opened = await openResearchCase(env, { eventId, providedToken });
  if (!opened.ok) return opened;
  const body = findingToStage7Registration(opened.request_id, provider, finding);
  if (!body.findings.explanation) return { ok: false, status: 400, error: 'A confirmed finding needs an explanation.' };
  const result = await registerStage7ResearchResponse(env, {
    requestId: body.request_id, provider: body.provider, submittedTs: null, findings: body.findings,
    sources: body.sources, confidence: body.confidence, validated: true, providedToken,
  });
  return { ...result, request_id: opened.request_id };
}
