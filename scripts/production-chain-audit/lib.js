// Pure logic for the production-chain-audit. Deliberately has ZERO network
// calls, ZERO credential access, and ZERO filesystem writes -- every
// function here takes plain data in and returns plain data out, so it can
// be fully unit-tested without live GitHub/Cloudflare/D1 access. The I/O
// lives in the sibling *-checks.js modules and run-audit.js; this file is
// what decides PASS/FAIL/NOT_VERIFIED from whatever evidence those modules
// gather, and computes the final GREEN/YELLOW/RED verdict.
//
// Status vocabulary matches the JSON contract this audit was commissioned
// to produce: PASS | FAIL | NOT_VERIFIED (not canary-audit's
// PASS/FAIL/UNAVAILABLE/NOT_APPLICABLE -- different contract, kept
// separate rather than forcing one vocabulary onto both).

export const STATUS = Object.freeze({ PASS: 'PASS', FAIL: 'FAIL', NOT_VERIFIED: 'NOT_VERIFIED' });

export function section(status, evidence = []) {
  if (!Object.values(STATUS).includes(status)) throw new Error(`Invalid section status: ${status}`);
  return { status, evidence: Array.isArray(evidence) ? evidence : [evidence] };
}

// ---- Secret redaction -- defense in depth, same approach as
// canary-audit/lib.js's redactSecrets, kept as an independent copy rather
// than a cross-module import so this audit's safety net doesn't silently
// depend on canary-audit's file staying unchanged. ----
const SECRET_KEY_PATTERN = /token|api[_-]?key|secret|password|authorization|bearer/i;
export function redactSecrets(value) {
  if (Array.isArray(value)) return value.map(redactSecrets);
  if (value && typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = SECRET_KEY_PATTERN.test(k) ? '[REDACTED]' : redactSecrets(v);
    }
    return out;
  }
  return value;
}

// Scans a string for patterns that look like an actual leaked credential
// value (not just a field named like one -- redactSecrets above already
// handles object keys; this is the last-line check against the fully
// rendered JSON/text output, matching the pattern canary-audit's workflow
// already runs as its own separate CI step).
const LEAK_PATTERNS = [
  /Bearer [A-Za-z0-9_\-.]{20,}/,
  /CLOUDFLARE_API_TOKEN\s*[:=]\s*[A-Za-z0-9_\-.]{10,}/,
  /gemini_api_key\s*[:=]\s*[A-Za-z0-9_\-]{10,}/i,
  /ghp_[A-Za-z0-9]{30,}/,
  /github_pat_[A-Za-z0-9_]{20,}/,
];
export function scanForLeakedSecrets(text) {
  const hits = LEAK_PATTERNS.filter((p) => p.test(text));
  return { clean: hits.length === 0, matchedPatterns: hits.map((p) => p.source) };
}

// ---- Frontend audit (section 8) ----
// sourceText: raw index.html content from CryptoPulseV2's main branch.
export function evaluateFrontend({ sourceText, commitSha, expectedWorkerUrl }) {
  if (sourceText == null) {
    return { status: STATUS.NOT_VERIFIED, commit_sha: commitSha ?? null, worker_url_configured: false, live_http_verified: false, evidence: ['could not fetch frontend source'] };
  }
  const workerUrlConfigured = sourceText.includes(expectedWorkerUrl);
  const hasLearningEndpoints = sourceText.includes('/api/learning/daily') || sourceText.includes('/api/learning/chatgpt');
  const hasPredictionEndpoints = sourceText.includes('/predict') || sourceText.includes('/select-variant');
  // "no audited production data is mocked" -- SOURCE_VERIFIED only, per
  // section 8's explicit instruction to separate this from
  // LIVE_HTTP_VERIFIED. This greps for the kind of literal placeholder
  // patterns a mock would leave behind; absence of these is evidence, not
  // proof -- worded as such in the evidence array rather than claimed as
  // a stronger guarantee than a source grep can actually give.
  const suspiciousMockMarkers = ['FAKE_DATA', 'MOCK_RESPONSE', 'hardcoded_for_demo', 'TODO: replace with real'];
  const mockMarkersFound = suspiciousMockMarkers.filter((m) => sourceText.includes(m));

  const evidence = [
    `worker_url_configured=${workerUrlConfigured}`,
    `learning_endpoints_referenced=${hasLearningEndpoints}`,
    `prediction_endpoints_referenced=${hasPredictionEndpoints}`,
    `mock_markers_found=${mockMarkersFound.length ? mockMarkersFound.join(',') : 'none'}`,
    'SOURCE_VERIFIED only -- no live HTTP request was made to the frontend by this audit',
  ];
  const pass = workerUrlConfigured && hasLearningEndpoints && hasPredictionEndpoints && mockMarkersFound.length === 0;
  return {
    status: pass ? STATUS.PASS : STATUS.FAIL,
    commit_sha: commitSha ?? null,
    worker_url_configured: workerUrlConfigured,
    live_http_verified: false,
    evidence,
  };
}

// ---- Worker -> D1 binding (section 9) ----
export function evaluateWorkerToD1({ configuredDatabaseId, configuredDatabaseName, liveQueryOk, expectedDatabaseId }) {
  if (!configuredDatabaseId) {
    return { status: STATUS.NOT_VERIFIED, database_id: null, database_name: configuredDatabaseName ?? null, evidence: ['database_id not found in deployed wrangler.toml source'] };
  }
  const idMatches = configuredDatabaseId === expectedDatabaseId;
  const evidence = [`configured_database_id=${configuredDatabaseId}`, `live_read_query_succeeded=${liveQueryOk}`];
  const pass = idMatches && liveQueryOk === true;
  return { status: pass ? STATUS.PASS : STATUS.FAIL, database_id: configuredDatabaseId, database_name: configuredDatabaseName ?? null, evidence };
}

// ---- Production prediction (section 10) ----
export function evaluatePrediction({ row, deployedSha }) {
  if (!row) return { status: STATUS.NOT_VERIFIED, id: null, timestamp: null, git_commit_sha: null, model_version: null, evidence: ['no resolved production prediction row found'] };
  const hasShaInfo = row.git_commit_sha != null && row.git_commit_sha !== 'unknown';
  const hasModelInfo = row.model_version != null;
  const evidence = [
    `prediction_id=${row.id}`, `ts=${row.ts}`,
    `git_commit_sha=${row.git_commit_sha ?? 'missing'}`, `model_version=${row.model_version ?? 'missing'}`,
  ];
  // Deployed SHA matching is informational, not required for PASS on its
  // own -- a prediction can be genuinely real even if it predates the
  // audited SHA (rolling 3h cron, not tied 1:1 to every deploy). Recorded
  // as evidence either way.
  if (deployedSha) evidence.push(`matches_currently_deployed_sha=${row.git_commit_sha === deployedSha}`);
  const pass = hasShaInfo && hasModelInfo;
  return { status: pass ? STATUS.PASS : STATUS.FAIL, id: row.id, timestamp: row.ts, git_commit_sha: row.git_commit_sha ?? null, model_version: row.model_version ?? null, evidence };
}

// ---- Analyst Relay response (section 11 + 17, redesigned) ----
// Replaces the old evaluateGemini, which looked for a successful automated
// MI- investigation via the paid grounded API -- that path was removed
// entirely (see PulseWorkerV2 commit "remove automated grounded API
// investigation"; Analyst Relay, human-relayed, is now the sole
// investigation mechanism). This looks for a real AR- entry instead.
//
// "Provider HTTP 200" and "non-empty valid Gemini response" (the two
// criteria explicitly kept as required evidence) don't map onto Analyst
// Relay literally -- there is no HTTP call this application makes to
// Gemini to have returned 200, since a human relays the response via
// Gemini's own consumer app. The faithful equivalents used here:
//   - "provider 200" -> a real analyst_relay_log row exists with
//     submitted_ts populated. recordAnalystRelay ALWAYS writes this row,
//     success or failure (confirmed directly in its source) -- so a
//     missing row means the submission endpoint itself never completed,
//     the closest analog to a failed provider call.
//   - "non-empty valid Gemini response" -> raw_response_text is non-empty
//     AND validation_status reflects the pasted text actually being
//     Gemini's real structured output, not garbage. validation_status
//     'ok' or 'no_catalyst_found' both mean the JSON parsed and validated
//     successfully (no_catalyst_found is a legitimate clean outcome --
//     Gemini genuinely found nothing, not a parsing failure).
//     'malformed_response' / 'invalid_response' / 'error' all mean the
//     submission itself failed one way or another.
export function evaluateAnalystRelay({ allRelayEntries }) {
  if (!allRelayEntries || allRelayEntries.length === 0) {
    return { status: STATUS.NOT_VERIFIED, investigation_id: null, validation_status: null, timestamp: null, evidence: ['no Analyst Relay submissions found at all -- no relay has been submitted yet, not an application failure'] };
  }
  const clean = (r) => r.submitted_ts != null && !!r.raw_response_text && (r.validation_status === 'ok' || r.validation_status === 'no_catalyst_found');
  const successful = allRelayEntries.find(clean);
  if (!successful) {
    const latest = allRelayEntries[0];
    return {
      status: STATUS.FAIL,
      investigation_id: latest.relay_id,
      validation_status: latest.validation_status,
      timestamp: latest.submitted_ts,
      evidence: [
        `${allRelayEntries.length} submission(s) found, none cleanly processed`,
        `latest: ${latest.relay_id} -> validation_status=${latest.validation_status}${latest.raw_response_text ? '' : ' (raw_response_text empty)'}`,
      ],
    };
  }
  return {
    status: STATUS.PASS,
    investigation_id: successful.relay_id,
    validation_status: successful.validation_status,
    timestamp: successful.submitted_ts,
    evidence: [
      `${successful.relay_id} processed cleanly at ${successful.submitted_ts}`,
      `validation_status=${successful.validation_status}`,
      `raw_response_text_length=${(successful.raw_response_text || '').length}`,
    ],
  };
}

// ---- Relay submission receipt (section 12, redesigned) ----
// The "provider HTTP 200" half of the requirement, evaluated as its own
// section for parity with the original structure. Genuinely redundant
// with part of evaluateAnalystRelay above by necessity (there's no
// separate provider-level table for a human-relayed response the way
// gemini_provider_calls existed for the API path) -- kept as a distinct
// section anyway per the explicit instruction to keep this as required
// evidence in its own right, not silently folded away.
export function evaluateRelaySubmission({ relayRow }) {
  if (!relayRow) {
    return { status: STATUS.NOT_VERIFIED, relay_id: null, submitted_ts: null, response_length: null, evidence: ['no matching analyst_relay_log row found for this submission'] };
  }
  const hasSubmission = relayRow.submitted_ts != null;
  const hasResponse = !!relayRow.raw_response_text && relayRow.raw_response_text.length > 0;
  const pass = hasSubmission && hasResponse;
  return {
    status: pass ? STATUS.PASS : STATUS.FAIL,
    relay_id: relayRow.relay_id,
    submitted_ts: relayRow.submitted_ts,
    response_length: relayRow.raw_response_text ? relayRow.raw_response_text.length : 0,
    evidence: [`submitted_ts=${relayRow.submitted_ts ?? 'missing'}`, `raw_response_text_length=${relayRow.raw_response_text ? relayRow.raw_response_text.length : 0}`],
  };
}

// ---- Shared investigation-context hash integrity (new) ----
// Independently recomputes computeContextHash's exact algorithm (copied
// deliberately, same reasoning as redactSecrets above -- this audit's
// verification shouldn't silently depend on worker.js's implementation
// staying unchanged) over the STORED context_json, and checks it matches
// the STORED context_hash. This is a genuine integrity check, not just a
// presence check -- it would catch a corrupted or hand-edited context_json
// that a naive "does context_hash exist" check would miss entirely.
async function computeContextHashIndependently(context) {
  const canonical = {
    candidateId: context.candidateId,
    primaryAsset: context.primaryAsset,
    windowMs: context.windowMs,
    observations: context.observations,
    correlatedFailureAssetCount: context.correlatedFailureAssetCount,
    correlatedFailureAssets: context.correlatedFailureAssets,
  };
  const json = JSON.stringify(canonical, Object.keys(canonical).sort());
  const bytes = new TextEncoder().encode(json);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function evaluateContextHashIntegrity({ contextJsonRaw, storedContextHash }) {
  if (!contextJsonRaw || !storedContextHash) {
    return { status: STATUS.NOT_VERIFIED, stored_hash: storedContextHash ?? null, recomputed_hash: null, evidence: ['context_json or context_hash missing from the relay row'] };
  }
  let context;
  try { context = JSON.parse(contextJsonRaw); } catch {
    return { status: STATUS.FAIL, stored_hash: storedContextHash, recomputed_hash: null, evidence: ['context_json did not parse as JSON'] };
  }
  const recomputed = await computeContextHashIndependently(context);
  const pass = recomputed === storedContextHash;
  return {
    status: pass ? STATUS.PASS : STATUS.FAIL,
    stored_hash: storedContextHash,
    recomputed_hash: recomputed,
    evidence: [`stored_hash=${storedContextHash}`, `recomputed_hash=${recomputed}`, pass ? 'hashes match -- context_json is genuine and unaltered' : 'MISMATCH -- context_json does not hash to the stored context_hash'],
  };
}

// ---- Analyst Relay factual-context parity (new) ----
// Separate from hash integrity above: even a context_json that hashes
// correctly could in principle be well-formed but factually empty (all
// assets unavailable, no real observations). This checks the CONTENTS
// look like genuine evidence, not a hollow/placeholder shape -- same
// "never invent data" standard applied to the frontend's mock-marker
// check, applied here to the shared investigation context specifically.
export function evaluateFactualContextParity({ contextJsonRaw, primaryAsset }) {
  if (!contextJsonRaw) {
    return { status: STATUS.NOT_VERIFIED, primary_asset_available: null, assets_with_data: 0, evidence: ['no context_json available to check'] };
  }
  let context;
  try { context = JSON.parse(contextJsonRaw); } catch {
    return { status: STATUS.FAIL, primary_asset_available: null, assets_with_data: 0, evidence: ['context_json did not parse as JSON'] };
  }
  const observations = context.observations || {};
  const assetsWithData = Object.entries(observations).filter(([, o]) => o && o.available === true).length;
  const primaryAssetAvailable = primaryAsset ? !!(observations[primaryAsset] && observations[primaryAsset].available === true) : null;
  const pass = assetsWithData > 0 && primaryAssetAvailable !== false;
  return {
    status: pass ? STATUS.PASS : STATUS.FAIL,
    primary_asset_available: primaryAssetAvailable,
    assets_with_data: assetsWithData,
    evidence: [`assets_with_data=${assetsWithData}`, `primary_asset_available=${primaryAssetAvailable}`, assetsWithData > 0 ? 'at least one real asset observation present, not a hollow shape' : 'no asset observations available at all -- context is empty'],
  };
}

// ---- Catalyst / investigation ledger (section 14) ----
export function evaluateCatalystLedger({ investigationId, catalystRows }) {
  if (!investigationId) return { status: STATUS.NOT_VERIFIED, record_id: null, investigation_id: null, evidence: ['no successful investigation to trace into the catalyst ledger'] };
  if (!catalystRows || catalystRows.length === 0) {
    return { status: STATUS.FAIL, record_id: null, investigation_id: investigationId, evidence: [`no coin_catalyst_log rows found with investigation_id=${investigationId}`, 'table existing is not evidence of persistence -- a real matching row is required'] };
  }
  const row = catalystRows[0];
  return { status: STATUS.PASS, record_id: row.id, investigation_id: investigationId, evidence: [`record_id=${row.id}`, `ts=${row.ts}`, `coin=${row.coin}`] };
}

// ---- Learning loop (section 15) ----
export function evaluateLearningLoop({ selectionDecisionRow }) {
  if (!selectionDecisionRow) {
    return { status: STATUS.NOT_VERIFIED, selection_decision_id: null, model_version: null, reason: null, confidence: null, evidence: ['no selection_decisions row found near the successful investigation -- a table existing is not evidence the loop ran'] };
  }
  const hasReason = !!selectionDecisionRow.reason;
  const hasScore = selectionDecisionRow.lca_score != null;
  const pass = hasReason && hasScore;
  return {
    status: pass ? STATUS.PASS : STATUS.FAIL,
    selection_decision_id: selectionDecisionRow.id,
    model_version: selectionDecisionRow.chosen_variant ?? null,
    reason: selectionDecisionRow.reason ?? null,
    confidence: selectionDecisionRow.lca_score ?? null,
    evidence: [`selection_decision_id=${selectionDecisionRow.id}`, `chosen_variant=${selectionDecisionRow.chosen_variant}`, `cleared_gate=${selectionDecisionRow.cleared_gate}`],
  };
}

// ---- Relay budget (section 16, redesigned) ----
// The old version compared MAX_GEMINI_INVESTIGATIONS_PER_DAY/HOUR against
// a required 5/1 -- that config governed the automated grounded API path,
// which no longer exists. Analyst Relay was deliberately built unbudgeted
// from day one (never touches reserveGeminiQuotaSlot /
// GEMINI_SHARED_QUOTA_CONFIG, confirmed directly in its own source
// comment) since there's no metered API call to budget. Continuing to
// check a now-irrelevant legacy number against a no-longer-applicable
// requirement would be actively misleading, not merely stale -- this
// reports the actual current design honestly instead. Per explicit
// instruction, does not read or report on GEMINI_TRIGGER_CONFIG (the old
// automated-path budget) at all; that value still exists in worker.js as
// dead reference code and is out of scope here, not silently re-purposed.
export function evaluateRelayBudget() {
  return {
    status: STATUS.PASS,
    budgeted: false,
    evidence: ['Analyst Relay is deliberately unbudgeted by design -- no metered API call exists for it to budget. This is the intended architecture, not a gap.'],
  };
}

// ---- Safety (section 3 / 19.9) ----
export function evaluateSafety({ productionWritesPerformed, secretsExposed }) {
  const pass = productionWritesPerformed === false && secretsExposed === false;
  return {
    status: pass ? STATUS.PASS : STATUS.FAIL,
    production_writes_performed: !!productionWritesPerformed,
    secrets_exposed: !!secretsExposed,
    evidence: [`production_writes_performed=${!!productionWritesPerformed}`, `secrets_exposed=${!!secretsExposed}`],
  };
}

// ---- Failure classification (section 18) ----
// Called only when the chain is NOT fully GREEN, to explain the single
// most upstream reason why. Order matters -- checks the earliest failing
// link first, since a downstream section can look FAIL/NOT_VERIFIED purely
// as a consequence of an upstream one never having run. grounding removed
// entirely -- it can never exist again post-removal, so classifying
// against it would be classifying against a permanently-absent thing.
export function classifyFailure({ analystRelay, relaySubmission, contextHashIntegrity, factualContextParity }) {
  if (analystRelay.status === STATUS.NOT_VERIFIED) {
    return { classification: 'NO_TRIGGER', evidence: analystRelay.evidence };
  }
  if (analystRelay.status === STATUS.FAIL) {
    return { classification: 'RELAY_SUBMISSION_UNCLEAN', evidence: analystRelay.evidence };
  }
  // analystRelay.status === PASS from here on
  if (relaySubmission.status !== STATUS.PASS) {
    return { classification: 'UNKNOWN', evidence: relaySubmission.evidence };
  }
  if (contextHashIntegrity.status !== STATUS.PASS) {
    return { classification: 'CONTEXT_HASH_MISMATCH', evidence: contextHashIntegrity.evidence };
  }
  if (factualContextParity.status !== STATUS.PASS) {
    return { classification: 'CONTEXT_FACTUAL_PARITY_FAILURE', evidence: factualContextParity.evidence };
  }
  return { classification: null, evidence: [] };
}

// Per-section: does a FAIL here represent a directly PROVEN defect
// (RED-worthy), or an unproven/incomplete/externally-caused state
// (YELLOW-worthy)? NOT_VERIFIED is never RED-worthy by itself -- it means
// "nothing to check yet", not "checked and broken".
function isRedWorthyFail(key, value, { analystRelay, relaySubmission }) {
  if (value.status !== STATUS.FAIL) return false;
  switch (key) {
    case 'analyst_relay':
      // A relay submission that didn't process cleanly (malformed paste,
      // etc.) is a human-input issue this audit deliberately does not
      // treat as a proven codebase defect.
      return false;
    case 'relay_submission':
      // analyst_relay claims success but the submission-level check
      // doesn't confirm it -- a real internal inconsistency once
      // analyst_relay itself is confirmed PASS.
      return analystRelay.status === STATUS.PASS;
    case 'context_hash_integrity':
    case 'factual_context_parity':
      // Only meaningful (and only a proven defect) once there was a real
      // successful relay submission for these to be checking.
      return analystRelay.status === STATUS.PASS && relaySubmission.status === STATUS.PASS;
    case 'catalyst_ledger':
    case 'learning_loop':
      return analystRelay.status === STATUS.PASS;
    default:
      // frontend, worker_to_d1, prediction -- any proven FAIL here (we
      // fetched real evidence and it was wrong) is a real defect.
      return true;
  }
}

// ---- End-to-end verdict (section 19, redesigned) ----
// GREEN only if every one of the 9 listed links is proven. grounding
// removed from this gate entirely (it can never exist again); replaced
// with contextHashIntegrity and factualContextParity, which are the new
// integrity checks that actually apply to the current Analyst Relay
// mechanism. safety is evaluated separately and is expected to always
// PASS by construction (this audit never writes to production) --
// included in the gate anyway so a real safety violation would correctly
// block GREEN rather than be silently ignored.
export function computeEndToEnd({ frontend, workerToD1, prediction, analystRelay, relaySubmission, contextHashIntegrity, factualContextParity, catalystLedger, learningLoop, safety }) {
  const required = {
    frontend, worker_to_d1: workerToD1, prediction, analyst_relay: analystRelay,
    relay_submission: relaySubmission, context_hash_integrity: contextHashIntegrity,
    factual_context_parity: factualContextParity, catalyst_ledger: catalystLedger,
    learning_loop: learningLoop, safety,
  };
  const failing = Object.entries(required).filter(([, v]) => v.status !== STATUS.PASS);

  if (failing.length === 0) {
    return { status: 'GREEN', blocking_reason: null };
  }

  // A safety violation is always RED, regardless of anything else -- a
  // hard line, not a judgment call.
  if (safety.status !== STATUS.PASS) {
    return { status: 'RED', blocking_reason: 'Safety violation: ' + safety.evidence.join('; ') };
  }

  const { classification, evidence } = classifyFailure({ analystRelay, relaySubmission, contextHashIntegrity, factualContextParity });
  const [firstFailingKey, firstFailingValue] = failing[0];
  const reasonPrefix = classification ? `[${classification}] ` : '';
  const reasonDetail = (evidence && evidence.length) ? evidence[0] : `${firstFailingKey} is ${firstFailingValue.status}`;

  const anyRedWorthy = failing.some(([key, value]) => isRedWorthyFail(key, value, { analystRelay, relaySubmission }));

  return {
    status: anyRedWorthy ? 'RED' : 'YELLOW',
    blocking_reason: `${reasonPrefix}${reasonDetail}`,
  };
}
