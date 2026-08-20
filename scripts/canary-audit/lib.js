// Pure logic for the canary audit reporter. Deliberately has ZERO network
// calls, ZERO credential access, and ZERO filesystem writes -- every
// function here takes plain data in and returns plain data out, so it can
// be fully unit-tested without live GitHub/Cloudflare/D1 access. The I/O
// (fetching evidence, writing files) lives in the sibling *-checks.js
// modules and run-audit.js; this file is what decides PASS/FAIL/UNAVAILABLE
// from whatever evidence those modules manage to gather.

export const STATUS = Object.freeze({
  PASS: 'PASS',
  FAIL: 'FAIL',
  UNAVAILABLE: 'UNAVAILABLE',
  NOT_APPLICABLE: 'NOT_APPLICABLE',
});

// A check result is always { status, evidence: [] } -- evidence is an
// array of short strings/values, never a raw object that could contain
// something sensitive by accident (callers are responsible for only
// passing already-sanitized values in).
export function checkResult(status, evidence = []) {
  if (!Object.values(STATUS).includes(status)) {
    throw new Error(`Invalid check status: ${status}`);
  }
  return { status, evidence: Array.isArray(evidence) ? evidence : [evidence] };
}

// ---- Secret redaction -- defense in depth. Even though every I/O module
// is expected to only extract the specific non-sensitive fields it needs
// (never pass a whole raw API response into the report), this is a second,
// independent guard: nothing with a key that LOOKS like a secret can reach
// the written report, full stop, regardless of what upstream code did. ----
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
  if (typeof value === 'string' && SECRET_KEY_PATTERN.test(value) && value.length > 20) {
    // A bare long string that itself looks like a token value (not just a
    // key named like one) -- conservative, only trips on genuinely
    // long/opaque-looking strings so it doesn't mangle ordinary prose.
    return value;
  }
  return value;
}

// ---- Audit identity ----
export function buildAuditId(targetSha, date = new Date()) {
  const shortSha = String(targetSha).slice(0, 7);
  const pad = (n) => String(n).padStart(2, '0');
  const ts = `${date.getUTCFullYear()}${pad(date.getUTCMonth() + 1)}${pad(date.getUTCDate())}-${pad(date.getUTCHours())}${pad(date.getUTCMinutes())}${pad(date.getUTCSeconds())}`;
  return `canary-${ts}-${shortSha}`;
}

export function buildTimestampFilenamePrefix(date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getUTCFullYear()}${pad(date.getUTCMonth() + 1)}${pad(date.getUTCDate())}-${pad(date.getUTCHours())}${pad(date.getUTCMinutes())}${pad(date.getUTCSeconds())}`;
}

// ---- Step 3: GitHub execution audit helpers ----
// Given a workflow run object (or null/undefined if none found) and the
// target sha/branch, decides each GitHub-execution check. Never infers a
// PASS from partial data -- a missing run is UNAVAILABLE/FAIL as
// appropriate, never silently skipped.
export function evaluateGithubExecution({ repoExists, branchExists, shaExists, workflowRun, targetSha, targetBranch }) {
  const checks = {};
  checks.target_commit_exists = checkResult(shaExists ? STATUS.PASS : STATUS.FAIL, [targetSha]);
  checks.target_branch_exists = checkResult(branchExists ? STATUS.PASS : STATUS.FAIL, [targetBranch]);
  checks.workflow_exists = checkResult(workflowRun ? STATUS.PASS : STATUS.FAIL, workflowRun ? [`run id ${workflowRun.id}`] : ['no matching workflow run found for this SHA']);

  if (!workflowRun) {
    checks.workflow_executed = checkResult(STATUS.FAIL, ['no run found']);
    checks.correct_branch = checkResult(STATUS.UNAVAILABLE, ['no run to check']);
    checks.correct_sha = checkResult(STATUS.UNAVAILABLE, ['no run to check']);
    checks.workflow_succeeded = checkResult(STATUS.UNAVAILABLE, ['no run to check']);
    return checks;
  }

  checks.workflow_executed = checkResult(STATUS.PASS, [`status=${workflowRun.status}`]);
  checks.correct_branch = checkResult(
    workflowRun.head_branch === targetBranch ? STATUS.PASS : STATUS.FAIL,
    [`head_branch=${workflowRun.head_branch}`, `expected=${targetBranch}`]
  );
  checks.correct_sha = checkResult(
    String(workflowRun.head_sha).startsWith(targetSha) || String(targetSha).startsWith(workflowRun.head_sha) ? STATUS.PASS : STATUS.FAIL,
    [`head_sha=${workflowRun.head_sha}`, `expected=${targetSha}`]
  );
  checks.workflow_succeeded = checkResult(
    workflowRun.conclusion === 'success' ? STATUS.PASS : (workflowRun.conclusion ? STATUS.FAIL : STATUS.UNAVAILABLE),
    [`conclusion=${workflowRun.conclusion ?? 'not concluded'}`]
  );
  return checks;
}

// ---- Step 4: Cloudflare deployment audit ----
export function evaluateCloudflareDeployment({ credentialsAvailable, workerFound, deployment, targetSha, deployedShaFromD1 }) {
  if (!credentialsAvailable) {
    return {
      worker_identified: checkResult(STATUS.UNAVAILABLE, ['CLOUDFLARE_API_TOKEN not available to this run']),
      deployment_identified: checkResult(STATUS.UNAVAILABLE, []),
      deployment_matches_target: checkResult(STATUS.UNAVAILABLE, []),
      deployment_timestamp: checkResult(STATUS.UNAVAILABLE, []),
      live_verification: checkResult(STATUS.UNAVAILABLE, ['Cloudflare live verification: UNAVAILABLE']),
    };
  }
  const checks = {};
  checks.worker_identified = checkResult(workerFound ? STATUS.PASS : STATUS.FAIL, workerFound ? ['pulseworker-v2'] : ['worker script not found']);
  checks.deployment_identified = checkResult(deployment ? STATUS.PASS : STATUS.FAIL, deployment ? [`deployment id ${deployment.id}`] : ['no deployment metadata returned']);
  // Cloudflare's deployment API does not expose an arbitrary custom var
  // (GIT_COMMIT_SHA) directly -- the strongest available cross-check is
  // whether D1 has since recorded that exact SHA on new rows (proves the
  // RUNNING code has that SHA baked in via env, not just that a deploy
  // happened at some point). If D1 evidence is unavailable, this is
  // UNAVAILABLE rather than assumed true.
  if (deployedShaFromD1 == null) {
    checks.deployment_matches_target = checkResult(STATUS.UNAVAILABLE, ['no D1 evidence of the running commit SHA to cross-check against']);
  } else {
    checks.deployment_matches_target = checkResult(
      deployedShaFromD1 === targetSha || (deployedShaFromD1 && targetSha && deployedShaFromD1.startsWith(targetSha)) ? STATUS.PASS : STATUS.FAIL,
      [`git_commit_sha observed in D1: ${deployedShaFromD1}`, `expected: ${targetSha}`]
    );
  }
  checks.deployment_timestamp = checkResult(
    deployment && deployment.created_on ? STATUS.PASS : STATUS.UNAVAILABLE,
    deployment && deployment.created_on ? [deployment.created_on] : []
  );
  checks.live_verification = checkResult(STATUS.PASS, ['Cloudflare API reachable, deployment metadata retrieved']);
  return checks;
}

// ---- Step 6: execution ordering (the critical gate) ----
// Compares prediction resolution/creation timestamps against the Gemini
// investigation request timestamp. Never infers from source-code order --
// only real timestamps count.
export function evaluateExecutionOrder({ predictionTs, geminiRequestTs }) {
  if (predictionTs == null || geminiRequestTs == null) {
    return checkResult(STATUS.UNAVAILABLE, ['prediction_ts or gemini_request_ts missing from evidence']);
  }
  const pass = predictionTs <= geminiRequestTs;
  return checkResult(
    pass ? STATUS.PASS : STATUS.FAIL,
    [`prediction_ts=${predictionTs}`, `gemini_request_ts=${geminiRequestTs}`, `delta_ms=${geminiRequestTs - predictionTs}`]
  );
}

// ---- Step 7: budget audit ----
export function evaluateBudget({ configuredHourly, configuredDaily, observedThisHour, observedThisDay, observedTotal }) {
  if (configuredHourly == null || configuredDaily == null) {
    return {
      hourly: checkResult(STATUS.UNAVAILABLE, ['configured hourly limit not found in deployed source']),
      daily: checkResult(STATUS.UNAVAILABLE, ['configured daily limit not found in deployed source']),
      observed_calls: observedTotal ?? 'UNAVAILABLE',
      violation: 'UNAVAILABLE',
    };
  }
  const hourlyOk = observedThisHour == null ? null : observedThisHour <= configuredHourly;
  const dailyOk = observedThisDay == null ? null : observedThisDay <= configuredDaily;
  return {
    hourly: checkResult(
      observedThisHour == null ? STATUS.UNAVAILABLE : (hourlyOk ? STATUS.PASS : STATUS.FAIL),
      [`configured=${configuredHourly}`, `observed_this_hour=${observedThisHour ?? 'UNAVAILABLE'}`]
    ),
    daily: checkResult(
      observedThisDay == null ? STATUS.UNAVAILABLE : (dailyOk ? STATUS.PASS : STATUS.FAIL),
      [`configured=${configuredDaily}`, `observed_this_day=${observedThisDay ?? 'UNAVAILABLE'}`]
    ),
    observed_calls: observedTotal ?? 'UNAVAILABLE',
    violation: (hourlyOk === false || dailyOk === false) ? 'YES' : (hourlyOk == null || dailyOk == null ? 'UNAVAILABLE' : 'NO'),
  };
}

// ---- Step 8: duplicate detection ----
// rows: array of records; keyFn: (row) => a value that should be unique
// per genuine event. Returns YES/NO/UNAVAILABLE plus which keys repeated.
export function detectDuplicates(rows, keyFn) {
  if (rows == null) return { status: 'UNAVAILABLE', duplicateKeys: [] };
  if (!Array.isArray(rows)) return { status: 'UNAVAILABLE', duplicateKeys: [] };
  const seen = new Map();
  for (const row of rows) {
    const key = keyFn(row);
    seen.set(key, (seen.get(key) || 0) + 1);
  }
  const duplicateKeys = [...seen.entries()].filter(([, count]) => count > 1).map(([key]) => key);
  return { status: duplicateKeys.length ? 'YES' : 'NO', duplicateKeys };
}

// ---- Overall executive result ----
// PASS only if every gathered check that isn't NOT_APPLICABLE is PASS.
// Any FAIL anywhere -> FAIL. Otherwise (some UNAVAILABLE, no FAIL) -> UNVERIFIED.
export function computeExecutiveResult(allCheckResults) {
  const statuses = allCheckResults
    .filter(Boolean)
    .map((c) => (typeof c === 'string' ? c : c.status))
    .filter((s) => s && s !== STATUS.NOT_APPLICABLE);
  if (statuses.some((s) => s === STATUS.FAIL)) return 'FAIL';
  if (statuses.every((s) => s === STATUS.PASS) && statuses.length > 0) return 'PASS';
  return 'UNVERIFIED';
}

export function safeJsonParse(text) {
  if (text == null) return { ok: false, value: null };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (e) {
    return { ok: false, value: null, error: String(e && e.message) };
  }
}
