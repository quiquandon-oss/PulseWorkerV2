// GLM evidence intelligence: orchestration (contract §11). Three entry points, all fail-safe (never throw into the
// caller, never block V1/V2):
//   glmDailySignalRun  - the 07:00 UTC step: bounded reads -> package -> (optional) one GLM call -> validated,
//                        qualified signal -> one write batch.
//   glmTickAdjust      - per BTC 24 h tick: latest valid signal -> bounded adjustment -> idempotent ledger row.
//   handleGlmRoutes    - read-only, bounded, edge-cached research API.
// Worker internals that must exist only once (Neuron reservation SQL, pricing, grounding checker, budget) are injected
// by worker.js through `deps`.
import {
  GLM_MODEL, GLM_LIMITS, GLM_INPUT_VERSION, GLM_PROMPT_VERSION, GLM_SCHEMA_VERSION, GLM_RULE_VERSION, GLM_ADJUST, GLM_SCOPE,
  GLM_ELIGIBILITY, glmInferenceBlockers,
} from './contract.js';
import { readGlmInputs, fetchChronosLedger, headlineBaseline, GLM_READ_ROWS_BOUND } from './adapters.js';
import { buildPackage } from './package.js';
import { validateGlmOutput, qualifySignal, computeAdjustment } from './signal.js';

const H = 3600e3;

export function glmFlags(env) {
  const capNum = Number(env.GLM_INFLUENCE_CAP ?? '0');
  const cap = Number.isFinite(capNum) && capNum > 0 ? Math.min(capNum, GLM_ADJUST.hardMaxCap) : 0;
  return {
    signal: String(env.GLM_SIGNAL_ENABLED ?? '0') === '1',
    inference: String(env.GLM_INFERENCE_ENABLED ?? '0') === '1',
    tick: String(env.GLM_TICK_HOOK_ENABLED ?? '0') === '1',
    cap, mode: cap > 0 ? 'active' : 'shadow',
  };
}

// The information cutoff of a daily run: 07:00:00 UTC of the scheduled day (or of the previous day before 07:00).
export function glmCutoff(ts) {
  const d = new Date(ts);
  let c = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 7, 0, 0);
  if (c > ts) c -= 24 * H;
  return c;
}

function errText(e) { return String((e && e.message) || e).slice(0, 200); }

// One GLM call through the shared, atomic Neuron ledger (sv2_ai_neurons). Fail-closed on every unknown.
async function glmInvoke(env, deps, pkg, nowTs, flags) {
  const model = String(env.SENTIMENT_V2_AI_MODEL || '').trim();
  if (!flags.inference) return { status: 'model_disabled', reason: 'GLM_INFERENCE_ENABLED is not "1"', counted: false };
  if (model !== GLM_MODEL) return { status: 'model_disabled', reason: 'SENTIMENT_V2_AI_MODEL is not the verified GLM model', counted: false };
  const budget = Number(deps.budget) || 0;
  if (budget <= 0) return { status: 'skipped_budget', reason: 'daily Neuron budget is 0', counted: false };
  const pricing = deps.pricing && deps.pricing[model];
  if (!pricing) return { status: 'model_unavailable', reason: 'model has no Neuron pricing entry', counted: false };
  if (!env.AI || typeof env.AI.run !== 'function') return { status: 'model_unavailable', reason: 'Workers AI binding unavailable', counted: false };
  const db = env.DB;
  const day = new Date(nowTs).toISOString().slice(0, 10);
  try {
    const lim = await db.prepare("SELECT 1 AS x FROM sv2_ai_neurons WHERE day = ? AND status = 'account_limited' LIMIT 1").bind(day).first();
    if (lim) return { status: 'quota_exhausted', reason: 'account free allocation reported exhausted today', counted: false };
  } catch (e) { return { status: 'skipped_budget', reason: 'Neuron ledger unavailable: ' + errText(e), counted: false }; }
  const bytes = new TextEncoder().encode(pkg.prompt.system).length + pkg.prompt.bytes;
  const estimate = Math.ceil(((bytes + 256) * pricing.neurons_per_m_input + GLM_LIMITS.maxCompletionTokens * pricing.neurons_per_m_output) / 1e6 * 100) / 100;
  let slot = null;
  try {
    const r = await db.prepare(deps.reserveSql).bind(day, nowTs, model, estimate, day, day, estimate, budget).run();
    if (r && r.meta && Number(r.meta.changes) > 0) {
      const row = await db.prepare('SELECT slot FROM sv2_ai_neurons WHERE rowid = ?').bind(r.meta.last_row_id).first();
      slot = row ? Number(row.slot) : null;
    }
  } catch (e) { return { status: 'skipped_budget', reason: 'reservation failed: ' + errText(e), counted: false }; }
  if (slot === null) return { status: 'skipped_budget', reason: `daily Neuron budget ${budget} would be exceeded (estimate ${estimate})`, counted: false };
  const record = async (u) => {
    try {
      await db.prepare('UPDATE sv2_ai_neurons SET input_tokens = ?, output_tokens = ?, actual_neurons = ?, status = ? WHERE day = ? AND slot = ?')
        .bind(u.input_tokens ?? null, u.output_tokens ?? null, u.actual_neurons ?? null, u.status, day, slot).run();
    } catch (_e) { /* the reservation still bounds the budget */ }
  };
  let raw;
  let timer;
  try {
    const call = env.AI.run(model, {
      messages: [{ role: 'system', content: pkg.prompt.system }, { role: 'user', content: pkg.prompt.user }],
      max_completion_tokens: GLM_LIMITS.maxCompletionTokens, temperature: 0,
      chat_template_kwargs: { enable_thinking: false }, response_format: { type: 'json_object' },
    });
    const timeout = new Promise((_, rej) => { timer = setTimeout(() => rej(Object.assign(new Error('glm timeout'), { glmTimeout: true })), deps.aiTimeoutMs ?? GLM_LIMITS.aiTimeoutMs); });
    raw = await Promise.race([call, timeout]);
  } catch (e) {
    const msg = errText(e);
    if (e && e.glmTimeout) { await record({ status: 'timeout' }); return { status: 'timeout', reason: 'no answer within the time limit', counted: true, reserved_neurons: estimate }; }
    // 3036 = account daily free allocation used (recorded as account_limited so every later call today is refused).
    if (/3036|daily free allocation|neurons/i.test(msg)) { await record({ status: 'account_limited' }); return { status: 'quota_exhausted', reason: msg, counted: true, reserved_neurons: estimate }; }
    if (/3040|\b429\b|rate.?limit|capacity|5028|410/i.test(msg)) { await record({ status: 'rate_limited' }); return { status: 'model_unavailable', reason: msg, counted: true, reserved_neurons: estimate }; }
    await record({ status: 'error' });
    return { status: 'failed', reason: msg, counted: true, reserved_neurons: estimate };
  } finally { if (timer) clearTimeout(timer); }
  const usage = raw && raw.usage ? raw.usage : null;
  const inTok = usage ? Number(usage.prompt_tokens) : NaN;
  const outTok = usage ? Number(usage.completion_tokens) : NaN;
  const actual = Number.isFinite(inTok) && Number.isFinite(outTok)
    ? Math.round((inTok * pricing.neurons_per_m_input + outTok * pricing.neurons_per_m_output) / 1e6 * 1000) / 1000 : null;
  const meta = { reserved_neurons: estimate, input_tokens: Number.isFinite(inTok) ? inTok : null, output_tokens: Number.isFinite(outTok) ? outTok : null, actual_neurons: actual };
  let obj = raw && typeof raw === 'object' && 'response' in raw ? raw.response
    : raw && raw.choices && raw.choices[0] && raw.choices[0].message ? raw.choices[0].message.content : raw;
  if (typeof obj === 'string') obj = obj.trim().replace(/^```(?:json)?\s*/, '').replace(/```$/, '').trim();
  return { status: 'answered', raw: obj, counted: true, meta, record };
}

function nowFn(o) { return typeof o.now === 'function' ? o.now : () => Date.now(); }

// The 07:00 step. Returns a summary object (also logged by the caller). Never throws.
export async function glmDailySignalRun(env, deps = {}, opts = {}) {
  const flags = glmFlags(env);
  if (!flags.signal) return { status: 'disabled' };
  const now = nowFn(opts);
  const started = now();
  const cutoff = glmCutoff(opts.scheduledTs ?? started);
  const db = env.DB;
  const usage = { d1_calls: 0, d1_rows_read: 0, d1_rows_written: 0 };
  try {
    const raw = await readGlmInputs(db, cutoff);
    usage.d1_calls += raw.calls; usage.d1_rows_read += raw.rowsRead;
    // Baseline at the cutoff (latest BTC 24 h snapshot computed before it) and the previous signal (persistence).
    let baseline = null, previous = null;
    try {
      usage.d1_calls += 1;
      const [snap, prev] = await db.batch([
        db.prepare('SELECT computed_ts, body_json FROM prediction_snapshots WHERE coin = ? AND horizon_hours = ?').bind('BTC', 24),
        db.prepare('SELECT direction, eligibility, decision_cutoff_ts FROM glm_signals WHERE asset = ? AND horizon_hours = ? AND decision_cutoff_ts < ? ORDER BY decision_cutoff_ts DESC LIMIT 1').bind('BTC', 24, cutoff),
      ]);
      usage.d1_rows_read += (snap.results || []).length + (prev.results || []).length;
      const s = (snap.results || [])[0];
      if (s && Number(s.computed_ts) <= cutoff) { try { baseline = headlineBaseline(JSON.parse(s.body_json)); } catch (_e) { baseline = null; } }
      previous = (prev.results || [])[0] || null;
    } catch (e) {
      return { status: 'inputs_degraded', reason: 'glm tables or snapshots unavailable: ' + errText(e), usage };
    }
    const chron = await fetchChronosLedger(deps.fetchImpl || fetch);
    const pkg = await buildPackage({ raw, cutoffTs: cutoff, chronosLedger: chron.ledger, baseline });
    const runId = `glm_${pkg.package_hash.slice(0, 24)}_${cutoff}`;
    const degraded = Object.keys(raw.errors).length > 0;
    // Idempotency: one row per package hash. An identical package is never sent twice and never re-signals.
    usage.d1_calls += 1;
    const ins = await db.prepare(
      `INSERT OR IGNORE INTO glm_runs (run_id, asset, decision_cutoff_ts, started_ts, package_hash, input_version, prompt_version,
        schema_version, status, n_items, n_excluded, package_bytes, input_refs_json, missing_json, rule_version)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
    ).bind(runId, 'BTC', cutoff, started, pkg.package_hash, GLM_INPUT_VERSION, GLM_PROMPT_VERSION, GLM_SCHEMA_VERSION, 'started',
      pkg.items.length, pkg.excluded.length, pkg.prompt.bytes, JSON.stringify(pkg.input_refs), JSON.stringify(pkg.header.missing),
      GLM_RULE_VERSION).run();
    usage.d1_rows_read += Number(ins?.meta?.rows_read ?? 0) || 0;
    if (!(ins && ins.meta && Number(ins.meta.changes) > 0)) {
      return { status: 'duplicate_package', run_id: runId, package_hash: pkg.package_hash, usage };
    }
    usage.d1_rows_written += 1;

    let runStatus, reason = null, output = null, validationErrors = null, ai = { counted: false };
    if (pkg.items.length === 0) { runStatus = 'insufficient_evidence'; reason = 'empty package'; }
    else {
      const callCountBefore = usage.d1_calls;
      ai = await glmInvoke(env, deps, pkg, now(), flags);
      usage.d1_calls = callCountBefore + (ai.counted ? 4 : (ai.status === 'model_disabled' ? 0 : 1));
      if (ai.status !== 'answered') { runStatus = ai.status; reason = ai.reason; }
      else {
        const v = validateGlmOutput(ai.raw, pkg, { ungroundedNumbers: deps.ungroundedNumbers });
        if (!v.ok) { runStatus = 'invalid_output'; validationErrors = v.errors; await ai.record({ ...ai.meta, status: 'invalid_output' }); }
        else { output = v.value; runStatus = output.status; await ai.record({ ...ai.meta, status: 'ok' }); }
      }
    }
    if (degraded && (runStatus === 'model_disabled' || runStatus === 'completed')) reason = (reason ? reason + '; ' : '') + 'inputs_degraded: ' + Object.keys(raw.errors).join(',');
    const finished = now();
    const sig = qualifySignal({ output, pkg, previous, runStatus });
    sig.valid_from_ts = finished;
    const k = GLM_ADJUST.strengthK[sig.strength] ?? 0;
    const wouldApply = ['UP', 'DOWN'].includes(sig.direction) ? (sig.direction === 'UP' ? 1 : -1) * k * GLM_ADJUST.referenceCap : 0;
    const sigRow = (h, s, would) => db.prepare(
      `INSERT OR IGNORE INTO glm_signals (signal_id, run_id, asset, horizon_hours, decision_cutoff_ts, valid_from_ts, valid_until_ts,
        direction, strength, eligibility, proposed_direction, proposed_strength, reasons_json, supporting_families_json,
        contradicting_families_json, supporting_refs_json, contradicting_refs_json, timesfm_ref, chronos_ref, models_live,
        would_apply, reference_cap, rule_version, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
    ).bind(`${runId}:${h}`, runId, 'BTC', h, cutoff, finished, cutoff + GLM_LIMITS.signalValidityMs, s.direction, s.strength ?? null,
      s.eligibility, s.proposed_direction ?? null, s.proposed_strength ?? null, JSON.stringify(s.reasons),
      JSON.stringify(s.supporting_families || []), JSON.stringify(s.contradicting_families || []),
      JSON.stringify(s.supporting_refs || []), JSON.stringify(s.contradicting_refs || []), s.timesfm_ref ?? null, s.chronos_ref ?? null,
      s.models_live ? 1 : 0, Math.round(would * 1e6) / 1e6, GLM_ADJUST.referenceCap, GLM_RULE_VERSION, finished);
    const noSignal12 = { direction: 'NO_SIGNAL', eligibility: 'NO_SIGNAL', reasons: [GLM_SCOPE.noSignalReasons[12]] };
    // rows read by the final write batch (index lookups of the UPDATE/INSERTs) are not known until it runs; they are
    // reported by d1_usage_daily for the invocation and returned in this function's result as final_batch_rows_read.
    const runUsage = { ...usage, d1_calls: usage.d1_calls + 1, d1_rows_written: usage.d1_rows_written + 2,
      reserved_neurons: ai.reserved_neurons ?? ai.meta?.reserved_neurons ?? 0, input_tokens: ai.meta?.input_tokens ?? null,
      output_tokens: ai.meta?.output_tokens ?? null, actual_neurons: ai.meta?.actual_neurons ?? null,
      chronos_fetch: chron.error || 'ok', package_bytes: pkg.prompt.bytes, trimmed: pkg.trimmed, wall_ms: finished - started,
      read_rows_bound: GLM_READ_ROWS_BOUND };
    const fin = await db.batch([
      db.prepare(`UPDATE glm_runs SET finished_ts = ?, model = ?, status = ?, reason = ?, output_json = ?, validation_errors_json = ?, usage_json = ?
                  WHERE run_id = ?`).bind(finished, ai.counted ? GLM_MODEL : null, runStatus, reason, output ? JSON.stringify(output) : null,
        validationErrors ? JSON.stringify(validationErrors) : null, JSON.stringify(runUsage), runId),
      sigRow(24, sig, wouldApply),
      sigRow(12, noSignal12, 0),
    ]);
    const finalRows = (fin || []).reduce((s, r) => s + (Number(r?.meta?.rows_read ?? 0) || 0), 0);
    return { status: runStatus, run_id: runId, signal: { direction: sig.direction, eligibility: sig.eligibility, reasons: sig.reasons },
      usage: { ...runUsage, final_batch_rows_read: finalRows, d1_rows_read_total: runUsage.d1_rows_read + finalRows } };
  } catch (e) {
    return { status: 'failed', reason: errText(e), usage };
  }
}

// Tick hook (BTC 24 h only reads the DB; other coin/horizons return NO_SIGNAL with zero queries). Never throws.
export async function glmTickAdjust(env, { coin, horizon, predictResult, selection, now = Date.now() }) {
  const flags = glmFlags(env);
  if (!flags.tick) return null;
  try {
    if (!predictResult || predictResult.status !== 'ok' || predictResult.persisted !== true) return null;
    const base = headlineBaseline({ ...predictResult, selection });
    const predictionTs = Number(predictResult.ts);
    const common = { mode: flags.mode, rule_version: GLM_RULE_VERSION, baseline_p_up: base ? base.baseline_p_up : null,
      baseline_source: base ? base.baseline_source : null };
    if (!GLM_SCOPE.assets.includes(coin) || !GLM_SCOPE.horizons.includes(horizon)) {
      const r = computeAdjustment({ signal: null, baseline_p_up: base ? base.baseline_p_up : NaN, predictionTs, coin, horizonHours: horizon, influenceCap: flags.cap });
      return { ...common, signal_id: null, signal_status: 'NO_SIGNAL', adjustment: 0, would_apply_adjustment: 0, final_p_up: common.baseline_p_up, reason: r.reason };
    }
    if (!base || !Number.isFinite(predictionTs)) return { ...common, signal_id: null, signal_status: 'NO_SIGNAL', adjustment: 0, would_apply_adjustment: 0, final_p_up: common.baseline_p_up, reason: 'no_baseline' };
    const db = env.DB;
    const sigRes = await db.prepare(
      `SELECT signal_id, asset, horizon_hours, direction, strength, eligibility, valid_from_ts, valid_until_ts FROM glm_signals
       WHERE asset = ? AND horizon_hours = ? AND decision_cutoff_ts <= ? ORDER BY decision_cutoff_ts DESC LIMIT 1`,
    ).bind(coin, horizon, predictionTs).all();
    const sig = (sigRes && sigRes.results && sigRes.results[0]) || null;
    const r = computeAdjustment({ signal: sig, baseline_p_up: base.baseline_p_up, predictionTs, coin, horizonHours: horizon, influenceCap: flags.cap });
    await db.prepare(
      `INSERT OR IGNORE INTO glm_adjustments (coin, horizon_hours, prediction_ts, baseline_p_up, baseline_source, chosen_variant, signal_id,
        signal_status, would_apply, applied, cap, final_p_up, mode, reason, rule_version, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
    ).bind(coin, horizon, predictionTs, base.baseline_p_up, base.baseline_source, base.chosen_variant ?? null,
      r.signal_status === 'NO_SIGNAL' ? null : (sig ? sig.signal_id : null), r.signal_status, r.would_apply, r.applied, r.cap,
      r.final_p_up, flags.mode, r.reason, GLM_RULE_VERSION, now).run();
    return { ...common, signal_id: sig && r.signal_status !== 'NO_SIGNAL' ? sig.signal_id : null, signal_status: r.signal_status,
      adjustment: r.applied, would_apply_adjustment: r.would_apply, final_p_up: r.final_p_up, reason: r.reason };
  } catch (e) {
    return { mode: flags.mode, signal_status: 'NO_SIGNAL', adjustment: 0, error: errText(e) };
  }
}

// ---------- read-only research routes ----------

const JSON_HEADERS = { 'Content-Type': 'application/json', 'Cache-Control': 'public, max-age=300' };
function jparse(s, d = null) { if (s == null) return d; try { return JSON.parse(s); } catch { return d; } }

export async function glmStatus(env, nowTs = Date.now()) {
  const f = glmFlags(env);
  const model = String(env.SENTIMENT_V2_AI_MODEL || '').trim();
  const out = {
    ok: true, versions: { input: GLM_INPUT_VERSION, prompt: GLM_PROMPT_VERSION, schema: GLM_SCHEMA_VERSION, rule: GLM_RULE_VERSION, model: GLM_MODEL },
    flags: { signal_enabled: f.signal, inference_enabled: f.inference, tick_hook_enabled: f.tick, influence_cap: f.cap, mode: f.mode },
    scope: { assets: GLM_SCOPE.assets, horizons: GLM_SCOPE.horizons, run: '07:00 UTC daily', note: '12 h, ETH and LINK are NO_SIGNAL' },
    adjustment: { reference_cap: GLM_ADJUST.referenceCap, hard_max_cap: GLM_ADJUST.hardMaxCap, thresholds: 'provisional, not calibrated' },
    eligibility: GLM_ELIGIBILITY,
    blocker_codes: [...glmInferenceBlockers(), ...(f.inference ? [] : ['inference_disabled']), ...(f.tick ? [] : ['tick_hook_disabled'])],
    blockers: [
      ...(f.inference ? [] : ['inference disabled (GLM_INFERENCE_ENABLED != "1"): V1 Workers AI Neuron use and the 07:00 Worker CPU are not yet verified (model, free-plan eligibility and MIT licence are verified)']),
      ...(model === GLM_MODEL ? [] : ['SENTIMENT_V2_AI_MODEL is not the verified GLM model']),
      ...(f.tick ? [] : ['tick hook off (GLM_TICK_HOOK_ENABLED != "1"): adjustments not recorded yet']),
    ],
    last_run: null, counts: null,
  };
  try {
    const [last, cnt] = await env.DB.batch([
      env.DB.prepare('SELECT run_id, decision_cutoff_ts, started_ts, finished_ts, status, reason, model, n_items, n_excluded, package_bytes, usage_json FROM glm_runs WHERE asset = ? ORDER BY decision_cutoff_ts DESC LIMIT 1').bind('BTC'),
      env.DB.prepare("SELECT eligibility, COUNT(*) AS n FROM glm_signals WHERE asset = ? AND horizon_hours = 24 AND decision_cutoff_ts >= ? GROUP BY eligibility").bind('BTC', nowTs - 120 * 24 * H),
    ]);
    const l = (last.results || [])[0];
    out.last_run = l ? { ...l, usage: jparse(l.usage_json), usage_json: undefined } : null;
    out.counts = Object.fromEntries((cnt.results || []).map((r) => [r.eligibility, r.n]));
  } catch (e) { out.ok = false; out.error = 'glm tables unavailable: ' + errText(e); }
  return out;
}

export async function glmSignals(env, horizon, limit) {
  const r = await env.DB.prepare(
    `SELECT s.signal_id, s.run_id, s.horizon_hours, s.decision_cutoff_ts, s.valid_from_ts, s.valid_until_ts, s.direction, s.strength,
       s.eligibility, s.proposed_direction, s.proposed_strength, s.reasons_json, s.supporting_families_json, s.contradicting_families_json,
       s.supporting_refs_json, s.contradicting_refs_json, s.timesfm_ref, s.chronos_ref, s.models_live, s.would_apply, s.reference_cap,
       s.rule_version, r.status AS run_status, r.reason AS run_reason
     FROM glm_signals s LEFT JOIN glm_runs r ON r.run_id = s.run_id
     WHERE s.asset = ? AND s.horizon_hours = ? ORDER BY s.decision_cutoff_ts DESC LIMIT ?`,
  ).bind('BTC', horizon, limit).all();
  return (r.results || []).map((x) => ({
    ...x, reasons: jparse(x.reasons_json, []), supporting_families: jparse(x.supporting_families_json, []),
    contradicting_families: jparse(x.contradicting_families_json, []), supporting_refs: jparse(x.supporting_refs_json, []),
    contradicting_refs: jparse(x.contradicting_refs_json, []),
    reasons_json: undefined, supporting_families_json: undefined, contradicting_families_json: undefined, supporting_refs_json: undefined, contradicting_refs_json: undefined,
  }));
}

export async function glmRun(env, id) {
  const r = await env.DB.prepare('SELECT * FROM glm_runs WHERE run_id = ?').bind(id).first();
  if (!r) return null;
  return { ...r, input_refs: jparse(r.input_refs_json, []), missing: jparse(r.missing_json, []), output: jparse(r.output_json),
    validation_errors: jparse(r.validation_errors_json), usage: jparse(r.usage_json),
    input_refs_json: undefined, missing_json: undefined, output_json: undefined, validation_errors_json: undefined, usage_json: undefined };
}

// Baseline vs would-apply vs applied vs final, joined with the realized outcome once resolved (BTC 24 h).
export async function glmAdjustments(env, limit) {
  const r = await env.DB.prepare(
    `SELECT a.coin, a.horizon_hours, a.prediction_ts, a.baseline_p_up, a.baseline_source, a.chosen_variant, a.signal_id, a.signal_status,
       a.would_apply, a.applied, a.cap, a.final_p_up, a.mode, a.reason, a.rule_version, a.created_ts,
       p.target_ts, p.realized_up, p.resolved_ts
     FROM glm_adjustments a LEFT JOIN predictions p ON p.horizon_hours = a.horizon_hours AND p.ts = a.prediction_ts
     WHERE a.coin = ? AND a.horizon_hours = ? ORDER BY a.prediction_ts DESC LIMIT ?`,
  ).bind('BTC', 24, limit).all();
  return r.results || [];
}

export async function handleGlmRoutes(request, env, url, corsHeaders = {}) {
  if (!url.pathname.startsWith('/api/glm/')) return null;
  if (request.method !== 'GET') return new Response(JSON.stringify({ ok: false, error: 'method not allowed' }), { status: 405, headers: { ...corsHeaders, ...JSON_HEADERS } });
  const int = (k, d, lo, hi) => Math.max(lo, Math.min(hi, parseInt(url.searchParams.get(k) || String(d), 10) || d));
  let key, compute;
  if (url.pathname === '/api/glm/status') { key = '/api/glm/status'; compute = () => glmStatus(env); }
  else if (url.pathname === '/api/glm/signals') {
    const h = int('horizon', 24, 12, 24) === 12 ? 12 : 24; const lim = int('limit', 14, 1, 30);
    key = `/api/glm/signals?horizon=${h}&limit=${lim}`; compute = async () => ({ ok: true, horizon_hours: h, signals: await glmSignals(env, h, lim) });
  } else if (url.pathname === '/api/glm/run') {
    const id = String(url.searchParams.get('id') || '');
    if (!/^glm_[0-9a-f]{24}_\d{13}$/.test(id)) return new Response(JSON.stringify({ ok: false, error: 'invalid id' }), { status: 400, headers: { ...corsHeaders, ...JSON_HEADERS } });
    key = `/api/glm/run?id=${id}`; compute = async () => { const run = await glmRun(env, id); return run ? { ok: true, run } : { ok: false, error: 'not found' }; };
  } else if (url.pathname === '/api/glm/adjustments') {
    const lim = int('limit', 24, 1, 60);
    key = `/api/glm/adjustments?limit=${lim}`; compute = async () => ({ ok: true, adjustments: await glmAdjustments(env, lim) });
  } else return new Response(JSON.stringify({ ok: false, error: 'not found' }), { status: 404, headers: { ...corsHeaders, ...JSON_HEADERS } });
  try {
    const edge = typeof caches !== 'undefined' && caches.default ? caches.default : null;
    const cacheKey = new Request(`${url.origin}${key}`, { method: 'GET' });
    if (edge) { const hit = await edge.match(cacheKey); if (hit) return new Response(hit.body, { headers: { ...corsHeaders, ...JSON_HEADERS, 'X-GLM-Cache': 'edge' } }); }
    const body = JSON.stringify(await compute());
    if (edge) { try { await edge.put(cacheKey, new Response(body, { headers: JSON_HEADERS })); } catch (_e) { /* cache is best effort */ } }
    return new Response(body, { headers: { ...corsHeaders, ...JSON_HEADERS } });
  } catch (e) {
    return new Response(JSON.stringify({ ok: false, error: 'glm data unavailable', detail: errText(e) }), { status: 503, headers: { ...corsHeaders, 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
  }
}

// ---------- staging-only qualification probes (GLM_STAGING_PROBES = "1" exists only in wrangler.staging.toml) ----------

export function glmStagingProbesEnabled(env) { return String(env.GLM_STAGING_PROBES ?? '0') === '1'; }

// D1 wrapper that counts calls and the rows D1 itself reports (meta.rows_read / rows_written) for one measured step.
function countingDb(db, c) {
  const note = (r) => { c.rows_read += Number(r?.meta?.rows_read ?? 0) || 0; c.rows_written += Number(r?.meta?.rows_written ?? 0) || 0; };
  const wrap = (st) => ({
    __inner: st,
    bind: (...a) => wrap(st.bind(...a)),
    all: async (...a) => { c.calls += 1; const r = await st.all(...a); note(r); return r; },
    run: async (...a) => { c.calls += 1; const r = await st.run(...a); note(r); return r; },
    first: async (...a) => { c.calls += 1; c.first_calls += 1; return st.first(...a); },   // first() carries no meta
  });
  return { prepare: (sql) => wrap(db.prepare(sql)),
    batch: async (stmts) => { c.calls += 1; const rs = await db.batch(stmts.map((s) => (s && s.__inner ? s.__inner : s))); for (const r of rs || []) note(r); return rs; } };
}

// Tick-hook probe on a SYNTHETIC BTC 24 h prediction (marked chosen_variant 'staging_probe'), run twice to prove
// idempotency, plus an out-of-scope call (ETH) that must cost zero queries. Measures D1 calls/rows per call. Shadow only:
// the cap is forced to 0 here whatever the environment says. Never available outside staging.
export async function glmTickProbe(env, { now = Date.now(), baseline_p_up = 0.5 } = {}) {
  if (!glmStagingProbesEnabled(env)) return { ok: false, skipped: true, reason: 'GLM_STAGING_PROBES is not "1" (staging only)' };
  const predictionTs = Math.floor(now / 60000) * 60000;
  const pr = { status: 'ok', persisted: true, ts: predictionTs, p_up: 0.5, synthetic: true };
  const sel = { ok: true, chosen_variant: 'staging_probe', cycle_p_up: baseline_p_up };
  const measure = async (coin) => {
    const c = { calls: 0, first_calls: 0, rows_read: 0, rows_written: 0 };
    const penv = { ...env, DB: countingDb(env.DB, c), GLM_TICK_HOOK_ENABLED: '1', GLM_INFLUENCE_CAP: '0' };
    const t0 = Date.now();
    const glm = await glmTickAdjust(penv, { coin, horizon: 24, predictResult: pr, selection: sel, now });
    return { glm, d1: c, wall_ms: Date.now() - t0 };   // wall time includes I/O; Workers CPU time is not observable here
  };
  try {
    const first = await measure('BTC');
    const second = await measure('BTC');
    const eth = await measure('ETH');
    const row = await env.DB.prepare('SELECT COUNT(*) AS n FROM glm_adjustments WHERE coin = ? AND horizon_hours = ? AND prediction_ts = ?')
      .bind('BTC', 24, predictionTs).first();
    const ledgerRows = row ? Number(row.n) : null;
    const baselineKept = first.glm && first.glm.final_p_up === baseline_p_up && first.glm.adjustment === 0;
    const ok = !!first.glm && ledgerRows === 1 && second.d1.rows_written === 0 && eth.d1.calls === 0 && baselineKept;
    return { ok, synthetic: true, prediction_ts: predictionTs, baseline_p_up, first, second, out_of_scope_eth: eth,
      ledger_rows_for_ts: ledgerRows, idempotent: second.d1.rows_written === 0 && ledgerRows === 1, baseline_unchanged: baselineKept,
      note: 'synthetic staging prediction; shadow cap forced to 0; wall_ms includes D1 latency, not CPU' };
  } catch (e) {
    return { ok: false, error: errText(e) };
  }
}
