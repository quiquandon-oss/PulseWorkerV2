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

// ---- Gemini investigation (section 11 + 17) ----
// allInvestigations: rows ordered newest-first. A row only counts as
// "real" per the task's explicit exclusion list (no tests/fixtures/dry
// runs/mocked/failed) if response_status === 'ok'.
export function evaluateGemini({ allInvestigations }) {
  if (!allInvestigations || allInvestigations.length === 0) {
    return { status: STATUS.NOT_VERIFIED, investigation_id: null, response_status: null, timestamp: null, evidence: ['no Gemini investigation attempts found at all -- no trigger has fired yet, not an application failure'] };
  }
  const successful = allInvestigations.find((r) => r.response_status === 'ok');
  if (!successful) {
    const latest = allInvestigations[0];
    return {
      status: STATUS.FAIL,
      investigation_id: latest.investigation_id,
      response_status: latest.response_status,
      timestamp: latest.request_ts,
      evidence: [
        `${allInvestigations.length} attempt(s) found, none with response_status='ok'`,
        `latest attempt: ${latest.investigation_id} -> ${latest.response_status}`,
      ],
    };
  }
  return {
    status: STATUS.PASS,
    investigation_id: successful.investigation_id,
    response_status: successful.response_status,
    timestamp: successful.request_ts,
    evidence: [`${successful.investigation_id} succeeded at ${successful.request_ts}`],
  };
}

// ---- Provider call (section 12) ----
export function evaluateProviderCall({ providerCallRow }) {
  if (!providerCallRow) {
    return { status: STATUS.NOT_VERIFIED, http_status: null, provider: null, model: null, timestamp: null, evidence: ['no matching gemini_provider_calls row found for this investigation'] };
  }
  const pass = providerCallRow.http_status === 200;
  return {
    status: pass ? STATUS.PASS : STATUS.FAIL,
    http_status: providerCallRow.http_status,
    provider: providerCallRow.provider,
    model: providerCallRow.model,
    timestamp: providerCallRow.request_ts,
    evidence: [`quota_decision=${providerCallRow.quota_decision}`, `response_status=${providerCallRow.response_status}`, `http_status=${providerCallRow.http_status}`],
  };
}

// ---- Grounding (section 13, hard requirement) ----
export function evaluateGrounding({ groundingMetadataJson }) {
  if (groundingMetadataJson == null) {
    return { status: STATUS.NOT_VERIFIED, search_queries: [], grounded_sources: [], source_count: 0, evidence: ['no grounding_metadata_json available'] };
  }
  let parsed;
  try { parsed = JSON.parse(groundingMetadataJson); } catch { parsed = null; }
  if (!parsed || typeof parsed !== 'object') {
    return { status: STATUS.FAIL, search_queries: [], grounded_sources: [], source_count: 0, evidence: ['grounding_metadata_json did not parse as an object'] };
  }
  const searchQueries = Array.isArray(parsed.searchQueries) ? parsed.searchQueries : [];
  const groundedSources = Array.isArray(parsed.groundedSources) ? parsed.groundedSources : [];
  // Explicitly rejects the empty-shell shapes named in the spec:
  // {"searchQueries":[],"groundedSources":[]} and {} both fail here, since
  // both produce searchQueries.length === 0 && groundedSources.length === 0.
  const pass = searchQueries.length > 0 && groundedSources.length > 0;
  return {
    status: pass ? STATUS.PASS : STATUS.FAIL,
    search_queries: searchQueries,
    grounded_sources: groundedSources,
    source_count: groundedSources.length,
    evidence: [`search_queries.length=${searchQueries.length}`, `grounded_sources.length=${groundedSources.length}`],
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

// ---- Budget (section 16) ----
// Reports the DEPLOYED configuration only -- never changes it, per
// explicit instruction. required_daily/required_hourly are the task's
// stated targets (5/24h, 1/hour), independent of what's actually deployed.
export function evaluateBudget({ configuredDaily, configuredHourly, requiredDaily = 5, requiredHourly = 1 }) {
  if (configuredDaily == null || configuredHourly == null) {
    return { status: STATUS.NOT_VERIFIED, configured_daily: configuredDaily ?? null, configured_hourly: configuredHourly ?? null, required_daily: requiredDaily, required_hourly: requiredHourly, evidence: ['could not read configured budget from deployed source'] };
  }
  const matches = configuredDaily === requiredDaily && configuredHourly === requiredHourly;
  return {
    status: matches ? STATUS.PASS : STATUS.FAIL,
    configured_daily: configuredDaily,
    configured_hourly: configuredHourly,
    required_daily: requiredDaily,
    required_hourly: requiredHourly,
    evidence: [`configured=${configuredDaily}/day + ${configuredHourly}/hour`, `required=${requiredDaily}/day + ${requiredHourly}/hour`],
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
// as a consequence of an upstream one never having run.
export function classifyFailure({ gemini, providerCall, grounding }) {
  if (gemini.status === STATUS.NOT_VERIFIED) {
    return { classification: 'NO_TRIGGER', evidence: gemini.evidence };
  }
  if (gemini.status === STATUS.FAIL) {
    const rs = gemini.response_status;
    if (rs === 'quota_deferred') return { classification: 'APPLICATION_BUDGET', evidence: gemini.evidence };
    if (rs === 'rate_limited') return { classification: 'PROVIDER_RATE_LIMIT', evidence: gemini.evidence };
    return { classification: 'UNKNOWN', evidence: gemini.evidence };
  }
  // gemini.status === PASS from here on
  if (providerCall.status !== STATUS.PASS) {
    return { classification: 'UNKNOWN', evidence: providerCall.evidence };
  }
  if (grounding.status !== STATUS.PASS) {
    return { classification: 'GROUNDING_FAILURE', evidence: grounding.evidence };
  }
  return { classification: null, evidence: [] }; // nothing to classify -- gemini+provider+grounding all PASS
}

// Per-section: does a FAIL here represent a directly PROVEN defect
// (RED-worthy), or an unproven/incomplete/externally-caused state
// (YELLOW-worthy)? NOT_VERIFIED is never RED-worthy by itself -- it means
// "nothing to check yet", not "checked and broken".
function isRedWorthyFail(key, value, { gemini, providerCall }) {
  if (value.status !== STATUS.FAIL) return false;
  switch (key) {
    case 'gemini':
      // rate_limited / quota_deferred / timeout / malformed_response are
      // all "hasn't succeeded yet" states this audit deliberately does not
      // treat as proven codebase defects (see classifyFailure).
      return false;
    case 'grounding':
      // Section 13: RED specifically when a real 200 came back ungrounded
      // despite the application requesting grounding (which it always
      // does for this call type) -- only meaningful once gemini AND the
      // provider call are both confirmed PASS; otherwise there was no real
      // 200 to have been ungrounded from.
      return gemini.status === STATUS.PASS && providerCall.status === STATUS.PASS;
    case 'provider_call':
      // gemini row claims success but the provider_call ledger doesn't
      // confirm http_status 200 -- a real internal inconsistency once
      // gemini itself is confirmed PASS.
      return gemini.status === STATUS.PASS;
    case 'catalyst_ledger':
    case 'learning_loop':
      // Only meaningful (and only a proven defect) once there was a real
      // successful investigation for these to have consumed.
      return gemini.status === STATUS.PASS;
    default:
      // frontend, worker_to_d1, prediction -- any proven FAIL here (we
      // fetched real evidence and it was wrong) is a real defect.
      return true;
  }
}

// ---- End-to-end verdict (section 19) ----
// GREEN only if every one of the 9 listed links is proven. safety is
// evaluated separately and is expected to always PASS by construction
// (this audit never writes to production) -- included in the gate anyway
// so a real safety violation would correctly block GREEN rather than be
// silently ignored.
export function computeEndToEnd({ frontend, workerToD1, prediction, gemini, providerCall, grounding, catalystLedger, learningLoop, safety }) {
  const required = {
    frontend, worker_to_d1: workerToD1, prediction, gemini,
    provider_call: providerCall, grounding, catalyst_ledger: catalystLedger, learning_loop: learningLoop, safety,
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

  const { classification, evidence } = classifyFailure({ gemini, providerCall, grounding });
  const [firstFailingKey, firstFailingValue] = failing[0];
  const reasonPrefix = classification ? `[${classification}] ` : '';
  const reasonDetail = (evidence && evidence.length) ? evidence[0] : `${firstFailingKey} is ${firstFailingValue.status}`;

  const anyRedWorthy = failing.some(([key, value]) => isRedWorthyFail(key, value, { gemini, providerCall }));

  return {
    status: anyRedWorthy ? 'RED' : 'YELLOW',
    blocking_reason: `${reasonPrefix}${reasonDetail}`,
  };
}
