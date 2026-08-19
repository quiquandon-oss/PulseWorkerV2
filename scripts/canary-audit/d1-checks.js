// D1 evidence gathering -- READ ONLY, enforced in code, not just by
// convention: runQuery() below refuses to execute anything that isn't a
// SELECT statement, as a hard safety net independent of what any caller
// intends to pass it.
//
// Schema used here was NOT guessed -- every column name was verified
// directly against the live production database in prior sessions before
// this script was written:
//   gemini_investigations: id, investigation_id, request_ts, trigger_reasons_json,
//     assets_json, model_identifier, response_status, source_count,
//     validation_status, error_message, catalysts_written, grounding_metadata_json
//   coin_catalyst_log: id, ts, coin, price_move_pct, headline_matched, headline_source,
//     extracted_reason, verdict, category, direction, source_url, discovery_timestamp,
//     confidence, market_classification, first_public_timestamp, investigation_id,
//     source_grounded, timestamp_source, timestamp_confidence
//   predictions (BTC core): id, ts, target_ts, p_up, calibrated_p_up, horizon_hours,
//     is_regime_anomaly, volatility_percentile, model_version, git_commit_sha,
//     realized_up, resolved_ts
//   selection_decisions: id, ts, coin, horizon_hours, chosen_variant, chosen_p_up,
//     lca_score, comparison_count, corrected_alpha, cleared_gate, k_sel,
//     neighborhood_json, reason, prediction_ts

const CF_API = 'https://api.cloudflare.com/client/v4';

export async function runQuery(accountId, databaseId, token, sql, params = []) {
  if (!accountId || !databaseId || !token) return { ok: false, unavailable: true, reason: 'credentials unavailable' };
  const trimmed = sql.trim().toUpperCase();
  if (!trimmed.startsWith('SELECT')) {
    throw new Error(`Refusing to execute a non-SELECT statement in a read-only audit: ${sql.slice(0, 80)}`);
  }
  try {
    const res = await fetch(`${CF_API}/accounts/${accountId}/d1/database/${databaseId}/query`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql, params }),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok || !body || body.success === false) {
      return { ok: false, unavailable: true, reason: `D1 query failed: ${res.status} ${JSON.stringify(body && body.errors)}` };
    }
    const results = (body.result && body.result[0] && body.result[0].results) || [];
    return { ok: true, unavailable: false, results };
  } catch (e) {
    return { ok: false, unavailable: true, reason: `D1 query threw: ${e.message}` };
  }
}

export async function getLatestGeminiInvestigation(accountId, databaseId, token) {
  const r = await runQuery(accountId, databaseId, token,
    'SELECT id, investigation_id, request_ts, trigger_reasons_json, assets_json, model_identifier, response_status, source_count, validation_status, error_message, catalysts_written, grounding_metadata_json FROM gemini_investigations ORDER BY request_ts DESC LIMIT 1');
  if (!r.ok) return r;
  return { ok: true, unavailable: false, row: r.results[0] || null };
}

export async function getAllGeminiInvestigations(accountId, databaseId, token) {
  const r = await runQuery(accountId, databaseId, token,
    'SELECT id, investigation_id, request_ts, assets_json, response_status, catalysts_written FROM gemini_investigations ORDER BY request_ts DESC');
  if (!r.ok) return r;
  return { ok: true, unavailable: false, rows: r.results };
}

export async function getCatalystsForInvestigation(accountId, databaseId, token, investigationId) {
  const r = await runQuery(accountId, databaseId, token,
    'SELECT id, ts, coin, category, direction, source_url, discovery_timestamp, confidence, market_classification, first_public_timestamp, investigation_id, source_grounded, timestamp_source, timestamp_confidence FROM coin_catalyst_log WHERE investigation_id = ?',
    [investigationId]);
  if (!r.ok) return r;
  return { ok: true, unavailable: false, rows: r.results };
}

export async function countCatalystsTotal(accountId, databaseId, token) {
  const r = await runQuery(accountId, databaseId, token, 'SELECT COUNT(*) as n FROM coin_catalyst_log');
  if (!r.ok) return r;
  return { ok: true, unavailable: false, n: r.results[0] ? r.results[0].n : null };
}

// Reconstructs which prediction most plausibly triggered a given
// investigation, using the same window/signal logic
// buildInvestigationCandidates itself uses (3.5h trailing window, latest
// resolved row per asset) -- NOT a guess, a re-derivation from the same
// rule the production code follows. Matches on (confidence, wasWrong)
// from trigger_reasons_json against calibrated_p_up/realized_up in the
// window, since gemini_investigations does not store a direct prediction
// foreign key.
export async function findAssociatedPrediction(accountId, databaseId, token, { asset, requestTs, confidence, wasWrong }) {
  const table = { BTC: 'predictions', LINK: 'link_predictions', ETH: 'eth_predictions' }[asset];
  if (!table) return { ok: false, unavailable: true, reason: `unknown asset ${asset}` };
  const WINDOW_MS = 3.5 * 3600 * 1000;
  const r = await runQuery(accountId, databaseId, token,
    `SELECT id, ts, target_ts, resolved_ts, horizon_hours, p_up, calibrated_p_up, realized_up, is_regime_anomaly, volatility_percentile, model_version, git_commit_sha
     FROM ${table}
     WHERE resolved_ts IS NOT NULL AND resolved_ts <= ? AND resolved_ts >= ?
     ORDER BY resolved_ts DESC LIMIT 10`,
    [requestTs, requestTs - WINDOW_MS]);
  if (!r.ok) return r;
  const match = r.results.find((row) => {
    const p = row.calibrated_p_up ?? row.p_up;
    const rowWasWrong = (p >= 0.5 ? 1 : 0) !== row.realized_up;
    const rowConfidence = Math.max(p, 1 - p);
    return Math.abs(rowConfidence - confidence) < 1e-9 && rowWasWrong === !!wasWrong;
  });
  return { ok: true, unavailable: false, row: match || null, candidatesInWindow: r.results.length };
}

export async function getSelectionDecisionNear(accountId, databaseId, token, coin, horizonHours, nearTs) {
  const r = await runQuery(accountId, databaseId, token,
    `SELECT id, ts, coin, horizon_hours, chosen_variant, chosen_p_up, lca_score, comparison_count, corrected_alpha, cleared_gate, k_sel, neighborhood_json, reason, prediction_ts
     FROM selection_decisions WHERE coin = ? AND horizon_hours = ? AND ts <= ? ORDER BY ts DESC LIMIT 1`,
    [coin, horizonHours, nearTs + 3600000]);
  if (!r.ok) return r;
  return { ok: true, unavailable: false, row: r.results[0] || null };
}

export async function countGeminiInvestigationsInWindow(accountId, databaseId, token, sinceTs) {
  const r = await runQuery(accountId, databaseId, token,
    'SELECT COUNT(*) as n FROM gemini_investigations WHERE request_ts >= ?', [sinceTs]);
  if (!r.ok) return r;
  return { ok: true, unavailable: false, n: r.results[0] ? r.results[0].n : null };
}

export async function countPredictionsWithShaSince(accountId, databaseId, token, table, sinceTs) {
  const r = await runQuery(accountId, databaseId, token,
    `SELECT DISTINCT git_commit_sha FROM ${table} WHERE ts >= ? AND git_commit_sha IS NOT NULL AND git_commit_sha != 'unknown'`,
    [sinceTs]);
  if (!r.ok) return r;
  return { ok: true, unavailable: false, shas: r.results.map((x) => x.git_commit_sha) };
}
