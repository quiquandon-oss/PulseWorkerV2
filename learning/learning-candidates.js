// Learning loop -- candidates, V1 impact, validation, human decision and methodology versions (D1 layer).
// Every write is STAGE7_ADMIN_TOKEN-gated and limited to learning_candidates / v1_methodology_versions. Approval
// creates a methodology version with status APPROVED (ready, NOT active): nothing here changes what V1 computes,
// V1's weights, predictions or sentiment rows. Activation is a separate, explicitly authorized step.
import { checkToken, loadAllV1Observations, loadAllBtc } from './learning-api.js';
import {
  BASELINE_VERSION_ID, FORMULA_ID, baselineConfig, candidateFromFinding, normalizeAdjustment, applyAdjustment,
  buildContexts, recalculate, validateRecalculation, CANDIDATE_TYPES, CANDIDATE_STATUSES, VALIDATION_TEXT, nextVersionId,
  isSignalPrototype, isProxySignalCandidate, prototypeFromFinding, DATA_COLLECTION_REQUIRED, PROXY_INVALID_FOR_SIGNAL_VALIDATION,
} from './learning-method.js';

const EDITABLE = ['DRAFT', DATA_COLLECTION_REQUIRED, 'PENDING_REVIEW', 'NEEDS_MORE_RESEARCH'];
function parseJson(raw, fallback) { if (typeof raw !== 'string') return fallback; try { return JSON.parse(raw); } catch (_e) { return fallback; } }
function clean(v, max) { return typeof v === 'string' ? v.trim().slice(0, max) : ''; }

export const LEARNING_TABLES_MISSING = { ok: false, status: 503, error: 'The learning tables are not present on this database (migration 0021 not applied).' };

async function tablesPresent(env) {
  try { await env.DB.prepare('SELECT 1 FROM learning_candidates LIMIT 1').all(); return true; } catch (_e) { return false; }
}

export async function getVersion(env, versionId) {
  if (versionId === BASELINE_VERSION_ID) {
    const row = await env.DB.prepare('SELECT * FROM v1_methodology_versions WHERE version_id = ?').bind(versionId).first().catch(() => null);
    return row ? versionView(row) : baselineVersionView();
  }
  const row = await env.DB.prepare('SELECT * FROM v1_methodology_versions WHERE version_id = ?').bind(versionId).first();
  return row ? versionView(row) : null;
}
function baselineVersionView() {
  return { version_id: BASELINE_VERSION_ID, parent_version_id: null, formula_id: FORMULA_ID, config: baselineConfig(), status: 'BASELINE', reason: 'V1 as published (default weights), reconstructed from stored source readings.', created_ts: null, candidate_id: null, approved_by: null, approved_ts: null, effective_ts: null, validation: null };
}
function versionView(r) {
  return { version_id: r.version_id, parent_version_id: r.parent_version_id, formula_id: r.formula_id, config: parseJson(r.config_json, null), status: r.status, reason: r.reason, created_ts: r.created_ts, candidate_id: r.candidate_id, approved_by: r.approved_by, approved_ts: r.approved_ts, effective_ts: r.effective_ts, validation: parseJson(r.validation_json, null) };
}

export async function listVersions(env) {
  let rows = [];
  try { rows = ((await env.DB.prepare('SELECT * FROM v1_methodology_versions ORDER BY created_ts').all()).results) || []; } catch (_e) { return { ok: true, available: false, versions: [baselineVersionView()] }; }
  const views = rows.map(versionView);
  if (!views.some((v) => v.version_id === BASELINE_VERSION_ID)) views.unshift(baselineVersionView());
  return { ok: true, available: true, versions: views.map(({ config, ...v }) => ({ ...v, sources: config ? config.sources.length : null, signals: config ? (config.signals || []).length : null })) };
}

function candidateView(r) {
  const adjustment = parseJson(r.adjustment_json, null);
  return {
    candidate_id: r.candidate_id, created_ts: r.created_ts, updated_ts: r.updated_ts, event_id: r.event_id, request_id: r.request_id,
    response_id: r.response_id, candidate_type: r.candidate_type, title: r.title, reason: r.reason, expected_effect: r.expected_effect,
    confidence: r.confidence, evidence: parseJson(r.evidence_json, []), status: r.status, base_version_id: r.base_version_id,
    adjustment, analysis: parseJson(r.analysis_json, null), decision: r.decision,
    decided_ts: r.decided_ts, decided_by: r.decided_by, decision_note: r.decision_note, produced_version_id: r.produced_version_id,
    // Read-time label only: stored rows are never rewritten to carry it.
    signal_validity: isProxySignalCandidate(r.candidate_type, adjustment) ? PROXY_INVALID_FOR_SIGNAL_VALIDATION : null,
  };
}

export async function listCandidates(env) {
  if (!(await tablesPresent(env))) return { ok: true, available: false, candidates: [] };
  const r = await env.DB.prepare('SELECT * FROM learning_candidates ORDER BY created_ts DESC').all();
  return { ok: true, available: true, candidates: ((r && r.results) || []).map(candidateView) };
}

export async function candidatesByEvent(env, eventIds) {
  if (!eventIds.length) return {};
  try {
    const r = await env.DB.prepare(`SELECT * FROM learning_candidates WHERE event_id IN (${eventIds.map(() => '?').join(',')}) ORDER BY created_ts DESC`).bind(...eventIds).all();
    const out = {};
    for (const row of (r && r.results) || []) if (!out[row.event_id]) out[row.event_id] = candidateView(row);
    return out;
  } catch (_e) { return {}; }
}

// ---- Impact + validation (deterministic, recomputed on demand) ----
async function analyse(env, candidate, baseVersion) {
  const eventRow = await env.DB.prepare('SELECT event_ts FROM research_events WHERE event_id = ?').bind(candidate.event_id).first();
  const eventTs = eventRow ? eventRow.event_ts : null;
  if (!candidate.adjustment) return { event_ts: eventTs, recalculation: null, validation: null };
  const [obs, btc] = await Promise.all([loadAllV1Observations(env), loadAllBtc(env)]);
  const contexts = buildContexts(obs, btc);
  const recalc = recalculate(baseVersion.config, candidate.adjustment, contexts, { eventTs });
  const validation = validateRecalculation(recalc, btc, { eventTs });
  const { all_points, proposed_config, ...recalcView } = recalc;
  const proxy = isProxySignalCandidate(candidate.candidate_type, candidate.adjustment);
  return {
    event_ts: eventTs, recalculation: recalcView, proposed_config,
    validation: { ...validation, status_text: VALIDATION_TEXT[validation.status], ...(proxy ? { signal_validity: PROXY_INVALID_FOR_SIGNAL_VALIDATION } : {}) },
  };
}

function analysisSummary(a) {
  if (!a || !a.recalculation) return null;
  const r = a.recalculation;
  return {
    recalculation_possible: r.possible, availability: r.availability.status, adjustment_text: r.adjustment_text,
    event: r.event, mean_abs_delta: r.summary ? r.summary.mean_abs_delta : null, observations: r.observations,
    validation_status: a.validation.status, validation_headline: a.validation.headline,
    ...(a.validation.signal_validity ? { signal_validity: a.validation.signal_validity } : {}),
  };
}

// The stored analysis summary is a snapshot. A snapshot that a refresh would change (new adjustment, or new data) moves
// to `history` instead of being overwritten, so a result already shown (e.g. a proxy validation) stays auditable.
function withHistory(summary, prevAnalysisJson, prevAdjustmentJson, now) {
  const prev = parseJson(prevAnalysisJson, null);
  const history = prev && Array.isArray(prev.history) ? prev.history.slice(-19) : [];
  if (prev) {
    const { history: _h, ...snapshot } = prev;
    if (JSON.stringify(snapshot) !== JSON.stringify(summary || null)) history.push({ ...snapshot, adjustment: parseJson(prevAdjustmentJson, null), superseded_ts: now });
  }
  if (!summary && !history.length) return null;
  return { ...(summary || {}), ...(history.length ? { history } : {}) };
}

export async function getCandidate(env, candidateId) {
  if (!(await tablesPresent(env))) return LEARNING_TABLES_MISSING;
  const row = await env.DB.prepare('SELECT * FROM learning_candidates WHERE candidate_id = ?').bind(candidateId).first();
  if (!row) return { ok: false, status: 404, error: 'candidate_not_found' };
  const c = candidateView(row);
  const base = await getVersion(env, c.base_version_id);
  const a = await analyse(env, c, base);
  const finding = await env.DB.prepare('SELECT provider, findings_json, registered_ts FROM stage7_research_responses WHERE response_id = ?').bind(c.response_id).first();
  const produced = c.produced_version_id ? await getVersion(env, c.produced_version_id) : null;
  return {
    ok: true, candidate: c, base_version: { version_id: base.version_id, status: base.status, formula_id: base.formula_id, reason: base.reason, sources: base.config.sources },
    finding: finding ? { provider: finding.provider, registered_ts: finding.registered_ts, ...parseJson(finding.findings_json, {}) } : null,
    recalculation: a.recalculation, validation: a.validation, produced_version: produced,
    proposed_sources: a.proposed_config ? a.proposed_config.sources : null, proposed_signals: a.proposed_config ? a.proposed_config.signals : null,
    signal_validity: c.signal_validity,
    // What the finding becomes as a new-signal prototype; the human may switch a proxy candidate to it (an explicit edit).
    prototype_suggestion: c.signal_validity && finding ? prototypeFromFinding(parseJson(finding.findings_json, {}), base.config) : null,
    analysis_history: c.analysis && Array.isArray(c.analysis.history) ? c.analysis.history : [],
  };
}

// Confirmed finding -> DRAFT candidate (one per finding).
export async function createCandidate(env, { eventId, providedToken, now = Date.now() }) {
  const denied = checkToken(env, providedToken);
  if (denied) return denied;
  if (!(await tablesPresent(env))) return LEARNING_TABLES_MISSING;
  const resp = await env.DB.prepare(
    `SELECT r.request_id, resp.response_id, resp.validation_status, resp.findings_json
     FROM stage7_research_requests r JOIN stage7_research_responses resp ON resp.request_id = r.request_id
     WHERE r.event_id = ? ORDER BY resp.registered_ts DESC LIMIT 1`
  ).bind(eventId).first();
  if (!resp) return { ok: false, status: 409, error: 'Confirm a research finding for this event first.' };
  if (resp.validation_status !== 'VALIDATED') return { ok: false, status: 409, error: 'The finding has not been confirmed by a human yet.' };
  const existing = await env.DB.prepare('SELECT candidate_id FROM learning_candidates WHERE response_id = ?').bind(resp.response_id).first();
  if (existing) return { ok: true, status: 200, candidate_id: existing.candidate_id, created: false };
  const d = candidateFromFinding(parseJson(resp.findings_json, {}));
  const norm = normalizeAdjustment(d.adjustment);
  const res = await env.DB.prepare(
    `INSERT INTO learning_candidates (created_ts, updated_ts, event_id, request_id, response_id, candidate_type, title, reason,
       expected_effect, confidence, evidence_json, status, base_version_id, adjustment_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).bind(now, now, eventId, resp.request_id, resp.response_id, d.candidate_type, d.title, d.reason || '(no explanation)', d.expected_effect,
    d.confidence, JSON.stringify(d.evidence), isSignalPrototype(d.adjustment) ? DATA_COLLECTION_REQUIRED : 'DRAFT', BASELINE_VERSION_ID,
    norm.ok ? JSON.stringify(norm.adjustment) : null).run();
  const created = await env.DB.prepare('SELECT candidate_id FROM learning_candidates WHERE response_id = ?').bind(resp.response_id).first();
  await refreshAnalysis(env, created.candidate_id);
  return { ok: true, status: 200, candidate_id: created.candidate_id, created: true, inserted: res && res.meta ? res.meta.changes : null };
}

async function refreshAnalysis(env, candidateId, { prevAnalysisJson = null, prevAdjustmentJson = null, now = Date.now() } = {}) {
  const row = await env.DB.prepare('SELECT * FROM learning_candidates WHERE candidate_id = ?').bind(candidateId).first();
  const c = candidateView(row);
  const base = await getVersion(env, c.base_version_id);
  const summary = withHistory(analysisSummary(await analyse(env, c, base)), prevAnalysisJson, prevAdjustmentJson, now);
  await env.DB.prepare('UPDATE learning_candidates SET analysis_json = ? WHERE candidate_id = ?').bind(summary ? JSON.stringify(summary) : null, candidateId).run();
  return summary;
}

// Human edits the candidate / adjustment, optionally submitting it for review.
export async function updateCandidate(env, { candidateId, fields, adjustment, submit, providedToken, now = Date.now() }) {
  const denied = checkToken(env, providedToken);
  if (denied) return denied;
  if (!(await tablesPresent(env))) return LEARNING_TABLES_MISSING;
  const row = await env.DB.prepare('SELECT * FROM learning_candidates WHERE candidate_id = ?').bind(candidateId).first();
  if (!row) return { ok: false, status: 404, error: 'candidate_not_found' };
  if (!EDITABLE.includes(row.status)) return { ok: false, status: 409, error: `Candidate is ${row.status}; it can no longer be edited.` };
  const base = await getVersion(env, row.base_version_id);
  const f = fields || {};
  const type = CANDIDATE_TYPES.includes(f.candidate_type) ? f.candidate_type : row.candidate_type;
  let adjJson = row.adjustment_json;
  if (adjustment !== undefined) {
    const norm = normalizeAdjustment(adjustment, base.config);
    if (!norm.ok) return { ok: false, status: 400, error: norm.errors.join(' '), errors: norm.errors };
    adjJson = JSON.stringify(norm.adjustment);
  }
  if (submit && !adjJson) return { ok: false, status: 400, error: 'Define the V1 adjustment before submitting for review.' };
  const status = submit ? 'PENDING_REVIEW' : row.status === 'NEEDS_MORE_RESEARCH' ? 'NEEDS_MORE_RESEARCH'
    : isSignalPrototype(parseJson(adjJson, null)) ? DATA_COLLECTION_REQUIRED : 'DRAFT';
  await env.DB.prepare(
    `UPDATE learning_candidates SET updated_ts = ?, candidate_type = ?, title = ?, reason = ?, expected_effect = ?, confidence = ?,
       adjustment_json = ?, status = ? WHERE candidate_id = ?`
  ).bind(now, type, clean(f.title, 140) || row.title, clean(f.reason, 2000) || row.reason, f.expected_effect !== undefined ? clean(f.expected_effect, 500) : row.expected_effect,
    ['LOW', 'MEDIUM', 'HIGH'].includes(f.confidence) ? f.confidence : row.confidence, adjJson, status, candidateId).run();
  const summary = await refreshAnalysis(env, candidateId, { prevAnalysisJson: row.analysis_json, prevAdjustmentJson: row.adjustment_json, now });
  return { ok: true, status: 200, candidate_status: status, analysis: summary };
}

// Human decision. APPROVE creates the next methodology version (APPROVED = ready, not active).
export async function decideCandidate(env, { candidateId, decision, approver, note, acknowledgeUnsupported, providedToken, now = Date.now() }) {
  const denied = checkToken(env, providedToken);
  if (denied) return denied;
  if (!(await tablesPresent(env))) return LEARNING_TABLES_MISSING;
  if (!['APPROVE', 'REJECT', 'NEEDS_MORE_RESEARCH'].includes(decision)) return { ok: false, status: 400, error: 'decision must be APPROVE, REJECT or NEEDS_MORE_RESEARCH' };
  const who = clean(approver, 80);
  if (!who) return { ok: false, status: 400, error: 'Enter your name: every decision records who made it.' };
  const row = await env.DB.prepare('SELECT * FROM learning_candidates WHERE candidate_id = ?').bind(candidateId).first();
  if (!row) return { ok: false, status: 404, error: 'candidate_not_found' };
  if (row.status !== 'PENDING_REVIEW') return { ok: false, status: 409, error: `Only a candidate submitted for review can be decided (this one is ${row.status}).` };
  const c = candidateView(row);
  if (decision === 'APPROVE' && c.signal_validity) {
    return { ok: false, status: 409, error: 'This candidate tests a proxy (an existing V1 source), not the researched new signal. Switch it to a signal prototype, or reject it.', signal_validity: c.signal_validity };
  }
  if (decision === 'APPROVE' && isSignalPrototype(c.adjustment)) {
    // Approves the data-collection plan only: no methodology version, no source activated, V1 unchanged.
    await env.DB.prepare('UPDATE learning_candidates SET status = ?, decision = ?, decided_ts = ?, decided_by = ?, decision_note = ?, updated_ts = ? WHERE candidate_id = ?')
      .bind('DATA_COLLECTION_APPROVED', 'APPROVE_DATA_COLLECTION', now, who, clean(note, 1000), now, candidateId).run();
    return { ok: true, status: 200, candidate_status: 'DATA_COLLECTION_APPROVED', version_id: null };
  }
  if (decision !== 'APPROVE') {
    await env.DB.prepare('UPDATE learning_candidates SET status = ?, decision = ?, decided_ts = ?, decided_by = ?, decision_note = ?, updated_ts = ? WHERE candidate_id = ?')
      .bind(decision === 'REJECT' ? 'REJECTED' : 'NEEDS_MORE_RESEARCH', decision, now, who, clean(note, 1000), now, candidateId).run();
    return { ok: true, status: 200, candidate_status: decision === 'REJECT' ? 'REJECTED' : 'NEEDS_MORE_RESEARCH' };
  }
  const base = await getVersion(env, c.base_version_id);
  const a = await analyse(env, c, base);
  if (a.validation.status !== 'SUPPORTED' && acknowledgeUnsupported !== true) {
    return { ok: false, status: 409, error: `Validation is "${VALIDATION_TEXT[a.validation.status]}". Tick "approve without supporting validation" to approve anyway.`, validation_status: a.validation.status };
  }
  // Persist the baseline once, from code, so every version has a stored parent.
  await env.DB.prepare(
    `INSERT OR IGNORE INTO v1_methodology_versions (version_id, parent_version_id, formula_id, config_json, created_ts, reason, status)
     VALUES (?, NULL, ?, ?, ?, ?, 'BASELINE')`
  ).bind(BASELINE_VERSION_ID, FORMULA_ID, JSON.stringify(baselineConfig()), now, baselineVersionView().reason).run();
  const ids = ((await env.DB.prepare('SELECT version_id FROM v1_methodology_versions').all()).results || []).map((r) => r.version_id);
  const versionId = nextVersionId(ids);
  const config = applyAdjustment(base.config, c.adjustment);
  const validation = { status: a.validation.status, headline: a.validation.headline, method: a.validation.method, current_v1: a.validation.current_v1, adjusted_v1: a.validation.adjusted_v1, independent: a.validation.independent, approved_without_support: a.validation.status !== 'SUPPORTED' };
  await env.DB.prepare(
    `INSERT INTO v1_methodology_versions (version_id, parent_version_id, formula_id, config_json, created_ts, reason, candidate_id, status, approved_by, approved_ts, effective_ts, validation_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, 'APPROVED', ?, ?, NULL, ?)`
  ).bind(versionId, base.version_id, FORMULA_ID, JSON.stringify(config), now, `${c.title}: ${a.recalculation.adjustment_text}`, candidateId, who, now, JSON.stringify(validation)).run();
  await env.DB.prepare('UPDATE learning_candidates SET status = ?, decision = ?, decided_ts = ?, decided_by = ?, decision_note = ?, produced_version_id = ?, updated_ts = ?, analysis_json = ? WHERE candidate_id = ?')
    .bind('ACCEPTED', 'APPROVE', now, who, clean(note, 1000), versionId, now, JSON.stringify(withHistory(analysisSummary(a), row.analysis_json, row.adjustment_json, now)), candidateId).run();
  return { ok: true, status: 200, candidate_status: 'ACCEPTED', version_id: versionId };
}

export { CANDIDATE_STATUSES };
