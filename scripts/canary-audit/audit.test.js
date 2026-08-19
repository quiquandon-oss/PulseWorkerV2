import { describe, it, expect } from 'vitest';
import {
  STATUS, checkResult, redactSecrets, buildAuditId, buildTimestampFilenamePrefix,
  evaluateGithubExecution, evaluateCloudflareDeployment, evaluateExecutionOrder,
  evaluateBudget, detectDuplicates, computeExecutiveResult, safeJsonParse,
} from './lib.js';
import { buildMarkdownReport, buildJsonReport, buildStepSummary, buildIndexRow, upsertIndex } from './report.js';

describe('checkResult', () => {
  it('builds a valid check result', () => {
    expect(checkResult(STATUS.PASS, ['a', 'b'])).toEqual({ status: 'PASS', evidence: ['a', 'b'] });
  });
  it('rejects an invalid status -- never silently accepts a typo', () => {
    expect(() => checkResult('SUCCESS')).toThrow();
  });
  it('wraps a bare (non-array) evidence value into an array', () => {
    expect(checkResult(STATUS.FAIL, 'single').evidence).toEqual(['single']);
  });
});

describe('redactSecrets', () => {
  it('redacts any key that looks like a secret, regardless of nesting depth', () => {
    const input = { normal: 'x', apiToken: 'shouldnotappear', nested: { CLOUDFLARE_API_TOKEN: 'also-secret', fine: 'ok' } };
    const out = redactSecrets(input);
    expect(out.normal).toBe('x');
    expect(out.apiToken).toBe('[REDACTED]');
    expect(out.nested.CLOUDFLARE_API_TOKEN).toBe('[REDACTED]');
    expect(out.nested.fine).toBe('ok');
  });
  it('redacts inside arrays of objects too', () => {
    const out = redactSecrets([{ secret: 'x' }, { ok: 'y' }]);
    expect(out[0].secret).toBe('[REDACTED]');
    expect(out[1].ok).toBe('y');
  });
  it('leaves ordinary strings and non-secret-keyed values untouched', () => {
    expect(redactSecrets({ description: 'a normal sentence about tokens of appreciation' }).description)
      .toBe('a normal sentence about tokens of appreciation');
  });
});

describe('buildAuditId', () => {
  it('matches the required canary-YYYYMMDD-HHMMSS-shortsha format', () => {
    const date = new Date(Date.UTC(2026, 7, 19, 3, 0, 18)); // 2026-08-19T03:00:18Z
    expect(buildAuditId('0280f8fe79ac0eeb', date)).toBe('canary-20260819-030018-0280f8f');
  });
  it('always uses exactly a 7-char short SHA', () => {
    expect(buildAuditId('abc', new Date(Date.UTC(2026,0,1)))).toMatch(/-abc$/);
    expect(buildAuditId('abcdefghijklmnop', new Date(Date.UTC(2026,0,1)))).toMatch(/-abcdefg$/);
  });
});

describe('buildTimestampFilenamePrefix', () => {
  it('is sortable and collision-resistant to the second', () => {
    const a = buildTimestampFilenamePrefix(new Date(Date.UTC(2026, 7, 19, 3, 0, 18)));
    const b = buildTimestampFilenamePrefix(new Date(Date.UTC(2026, 7, 19, 3, 0, 19)));
    expect(a < b).toBe(true);
  });
});

describe('evaluateGithubExecution', () => {
  it('FAILs target_commit_exists and target_branch_exists honestly when neither exists', () => {
    const r = evaluateGithubExecution({ shaExists: false, branchExists: false, workflowRun: null, targetSha: 'deadbeef', targetBranch: 'nope' });
    expect(r.target_commit_exists.status).toBe('FAIL');
    expect(r.target_branch_exists.status).toBe('FAIL');
    expect(r.workflow_exists.status).toBe('FAIL');
  });
  it('marks correct_branch/correct_sha/workflow_succeeded UNAVAILABLE (not FAIL) when no run was found at all', () => {
    const r = evaluateGithubExecution({ shaExists: true, branchExists: true, workflowRun: null, targetSha: 'x', targetBranch: 'y' });
    expect(r.correct_branch.status).toBe('UNAVAILABLE');
    expect(r.correct_sha.status).toBe('UNAVAILABLE');
    expect(r.workflow_succeeded.status).toBe('UNAVAILABLE');
  });
  it('PASSes correct_branch/correct_sha when a matching run is found', () => {
    const run = { id: 123, status: 'completed', conclusion: 'success', head_branch: 'feature/x', head_sha: 'abc123full' };
    const r = evaluateGithubExecution({ shaExists: true, branchExists: true, workflowRun: run, targetSha: 'abc123full', targetBranch: 'feature/x' });
    expect(r.correct_branch.status).toBe('PASS');
    expect(r.correct_sha.status).toBe('PASS');
    expect(r.workflow_succeeded.status).toBe('PASS');
  });
  it('FAILs workflow_succeeded when the run concluded but not successfully', () => {
    const run = { id: 1, status: 'completed', conclusion: 'failure', head_branch: 'b', head_sha: 's' };
    const r = evaluateGithubExecution({ shaExists: true, branchExists: true, workflowRun: run, targetSha: 's', targetBranch: 'b' });
    expect(r.workflow_succeeded.status).toBe('FAIL');
  });
  it('FAILs correct_branch when the run is on the wrong branch, without also silently passing correct_sha incorrectly', () => {
    const run = { id: 1, status: 'completed', conclusion: 'success', head_branch: 'main', head_sha: 'abc' };
    const r = evaluateGithubExecution({ shaExists: true, branchExists: true, workflowRun: run, targetSha: 'abc', targetBranch: 'feature/x' });
    expect(r.correct_branch.status).toBe('FAIL');
    expect(r.correct_sha.status).toBe('PASS');
  });
});

describe('evaluateCloudflareDeployment', () => {
  it('marks everything UNAVAILABLE (not FAIL) when credentials are missing -- never fails the whole audit over this', () => {
    const r = evaluateCloudflareDeployment({ credentialsAvailable: false });
    expect(r.worker_identified.status).toBe('UNAVAILABLE');
    expect(r.live_verification.status).toBe('UNAVAILABLE');
  });
  it('PASSes deployment_matches_target only when D1 evidence actually confirms the SHA', () => {
    const r = evaluateCloudflareDeployment({
      credentialsAvailable: true, workerFound: true, deployment: { id: 'd1', created_on: '2026-08-19T00:07:53Z' },
      targetSha: '0280f8f', deployedShaFromD1: '0280f8fe79ac0eeb633dfffa91889cd77bfd030c',
    });
    expect(r.deployment_matches_target.status).toBe('PASS');
  });
  it('is UNAVAILABLE for deployment_matches_target, not a guessed PASS, when there is no D1 cross-check evidence', () => {
    const r = evaluateCloudflareDeployment({ credentialsAvailable: true, workerFound: true, deployment: { id: 'd1' }, targetSha: 'x', deployedShaFromD1: null });
    expect(r.deployment_matches_target.status).toBe('UNAVAILABLE');
  });
  it('FAILs deployment_matches_target when D1 shows a different SHA than the target', () => {
    const r = evaluateCloudflareDeployment({ credentialsAvailable: true, workerFound: true, deployment: { id: 'd1' }, targetSha: 'aaaaaaa', deployedShaFromD1: 'bbbbbbbbbbbbbbbb' });
    expect(r.deployment_matches_target.status).toBe('FAIL');
  });
});

describe('evaluateExecutionOrder — the critical gate', () => {
  it('PASSes when prediction resolved strictly before the Gemini request', () => {
    const r = evaluateExecutionOrder({ predictionTs: 1000, geminiRequestTs: 2000 });
    expect(r.status).toBe('PASS');
  });
  it('PASSes at the exact boundary (prediction_ts === gemini_request_ts)', () => {
    const r = evaluateExecutionOrder({ predictionTs: 1000, geminiRequestTs: 1000 });
    expect(r.status).toBe('PASS');
  });
  it('FAILs when Gemini fired before the prediction it supposedly followed', () => {
    const r = evaluateExecutionOrder({ predictionTs: 2000, geminiRequestTs: 1000 });
    expect(r.status).toBe('FAIL');
  });
  it('is UNAVAILABLE, never inferred from source-code order, when a timestamp is missing', () => {
    expect(evaluateExecutionOrder({ predictionTs: null, geminiRequestTs: 1000 }).status).toBe('UNAVAILABLE');
    expect(evaluateExecutionOrder({ predictionTs: 1000, geminiRequestTs: undefined }).status).toBe('UNAVAILABLE');
  });
});

describe('evaluateBudget', () => {
  it('PASSes both when observed calls are within configured limits', () => {
    const r = evaluateBudget({ configuredHourly: 1, configuredDaily: 1, observedThisHour: 1, observedThisDay: 1, observedTotal: 1 });
    expect(r.hourly.status).toBe('PASS');
    expect(r.daily.status).toBe('PASS');
    expect(r.violation).toBe('NO');
  });
  it('FAILs hourly and reports a violation when the hourly count exceeds the configured limit', () => {
    const r = evaluateBudget({ configuredHourly: 1, configuredDaily: 5, observedThisHour: 2, observedThisDay: 2, observedTotal: 2 });
    expect(r.hourly.status).toBe('FAIL');
    expect(r.violation).toBe('YES');
  });
  it('is UNAVAILABLE throughout when the configured limits cannot be found in the deployed source', () => {
    const r = evaluateBudget({ configuredHourly: null, configuredDaily: null, observedThisHour: 1, observedThisDay: 1 });
    expect(r.hourly.status).toBe('UNAVAILABLE');
    expect(r.violation).toBe('UNAVAILABLE');
  });
});

describe('detectDuplicates', () => {
  it('finds NO duplicates in a clean set', () => {
    const r = detectDuplicates([{ id: 1 }, { id: 2 }], (x) => x.id);
    expect(r.status).toBe('NO');
  });
  it('finds YES with the actual repeated key when a duplicate exists', () => {
    const r = detectDuplicates([{ id: 1 }, { id: 1 }, { id: 2 }], (x) => x.id);
    expect(r.status).toBe('YES');
    expect(r.duplicateKeys).toEqual([1]);
  });
  it('is UNAVAILABLE (not NO) when rows themselves are unavailable -- never conflates "no evidence" with "no duplicates"', () => {
    expect(detectDuplicates(null, (x) => x.id).status).toBe('UNAVAILABLE');
    expect(detectDuplicates(undefined, (x) => x.id).status).toBe('UNAVAILABLE');
  });
});

describe('computeExecutiveResult', () => {
  it('is PASS only when every check is PASS', () => {
    expect(computeExecutiveResult(['PASS', 'PASS', { status: 'PASS' }])).toBe('PASS');
  });
  it('is FAIL if even one check FAILed, regardless of how many PASSed', () => {
    expect(computeExecutiveResult(['PASS', 'PASS', 'FAIL', 'PASS'])).toBe('FAIL');
  });
  it('is UNVERIFIED (not PASS) when some checks are UNAVAILABLE and none FAILed', () => {
    expect(computeExecutiveResult(['PASS', 'UNAVAILABLE', 'PASS'])).toBe('UNVERIFIED');
  });
  it('ignores NOT_APPLICABLE checks entirely -- they neither help nor hurt the result', () => {
    expect(computeExecutiveResult(['PASS', 'PASS', 'NOT_APPLICABLE'])).toBe('PASS');
  });
  it('is UNVERIFIED for an empty/all-not-applicable check list, never a bare PASS with nothing to support it', () => {
    expect(computeExecutiveResult([])).toBe('UNVERIFIED');
    expect(computeExecutiveResult(['NOT_APPLICABLE'])).toBe('UNVERIFIED');
  });
});

describe('safeJsonParse', () => {
  it('parses valid JSON', () => {
    expect(safeJsonParse('{"a":1}')).toEqual({ ok: true, value: { a: 1 } });
  });
  it('does not throw on invalid JSON, returns ok:false instead', () => {
    const r = safeJsonParse('{not json');
    expect(r.ok).toBe(false);
    expect(r.value).toBe(null);
  });
  it('handles null/undefined input without throwing', () => {
    expect(safeJsonParse(null).ok).toBe(false);
    expect(safeJsonParse(undefined).ok).toBe(false);
  });
});

describe('buildMarkdownReport', () => {
  const minimalReport = {
    result: 'UNVERIFIED', repository: 'quiquandon-oss/PulseWorkerV2', target_branch: 'feature/x', target_sha: 'abc1234',
    environment: 'production', audit_id: 'canary-20260819-030018-abc1234', timestamp: '2026-08-19T03:00:18Z',
    github: {}, cloudflare: {}, d1: {}, execution_order: {}, budgets: {}, duplicates: {}, evidence: [],
  };
  it('contains every required top-level section heading, in order', () => {
    const md = buildMarkdownReport(minimalReport);
    const headings = ['# CryptoPulseV2 Canary Audit', '## Executive Result', '## Target', '## GitHub Execution',
      '## Cloudflare Deployment', '## D1 Execution Chain', '## Execution Ordering', '## Budget',
      '## Duplicate Detection', '## Evidence', '## Final Decision'];
    let lastIndex = -1;
    for (const h of headings) {
      const idx = md.indexOf(h);
      expect(idx).toBeGreaterThan(lastIndex);
      lastIndex = idx;
    }
  });
  it('renders UNVERIFIED as CANARY UNVERIFIED in the Final Decision section', () => {
    expect(buildMarkdownReport(minimalReport)).toContain('CANARY UNVERIFIED');
  });
  it('renders PASS as CANARY PASSED', () => {
    expect(buildMarkdownReport({ ...minimalReport, result: 'PASS' })).toContain('CANARY PASSED');
  });
  it('renders FAIL as CANARY FAILED', () => {
    expect(buildMarkdownReport({ ...minimalReport, result: 'FAIL' })).toContain('CANARY FAILED');
  });
  it('never crashes on a fully-populated report with real-shaped evidence', () => {
    const full = {
      ...minimalReport,
      github: { target_commit_exists: { status: 'PASS', evidence: ['abc1234'] } },
      d1: { investigation: { status: 'PASS', evidence: ['id=1'] }, grounding: { status: 'FAIL', evidence: ['empty groundedSources'] } },
      execution_order: { prediction_before_gemini: { status: 'PASS', evidence: ['delta_ms=4417'] } },
      budgets: { hourly: { status: 'PASS', evidence: [] }, daily: { status: 'PASS', evidence: [] }, observed_calls: 1, violation: 'NO' },
      duplicates: { investigation: 'NO', gemini: 'NO' },
      evidence: [{ source: 'D1', timestamp: '2026-08-19T03:00:18Z', id: '1', db_record_id: '1' }],
    };
    expect(() => buildMarkdownReport(full)).not.toThrow();
  });
});

describe('buildJsonReport', () => {
  it('includes every key required by the task spec, even when the input report omits some', () => {
    const j = buildJsonReport({ audit_id: 'x', timestamp: 't', repository: 'r', target_branch: 'b', target_sha: 's', environment: 'e', result: 'UNVERIFIED' });
    for (const key of ['audit_id', 'timestamp', 'repository', 'target_branch', 'target_sha', 'environment', 'result',
      'github', 'cloudflare', 'd1', 'execution_order', 'gemini', 'grounding', 'budgets', 'duplicates', 'evidence']) {
      expect(j).toHaveProperty(key);
    }
  });
  it('is JSON-serializable without throwing (no circular refs, no undefined-breaking values)', () => {
    const j = buildJsonReport({ audit_id: 'x', timestamp: 't', repository: 'r', target_branch: 'b', target_sha: 's', environment: 'e', result: 'PASS' });
    expect(() => JSON.parse(JSON.stringify(j))).not.toThrow();
  });
});

describe('buildStepSummary', () => {
  it('includes the target branch, sha, and final result', () => {
    const s = buildStepSummary({ target_branch: 'feature/x', target_sha: 'abc1234', result: 'UNVERIFIED', github: {}, cloudflare: {}, d1: {}, execution_order: {}, budgets: {}, duplicates: {} });
    expect(s).toContain('feature/x');
    expect(s).toContain('abc1234');
    expect(s).toContain('FINAL: UNVERIFIED');
  });
});

describe('buildIndexRow / upsertIndex', () => {
  it('creates a fresh index with header when none exists yet', () => {
    const row = buildIndexRow({ timestamp: 't', audit_id: 'a', target_branch: 'b', target_sha: 's', result: 'PASS' }, 'artifacts/canary-audit/x.md');
    const out = upsertIndex(null, row);
    expect(out).toContain('# CryptoPulseV2 Canary Audit Index');
    expect(out).toContain(row);
  });
  it('appends to an existing index WITHOUT removing any prior row -- never overwrites history', () => {
    const existing = '# CryptoPulseV2 Canary Audit Index\n\n| Timestamp | Audit ID | Branch | SHA | Result | Report |\n|---|---|---|---|---|---|\n| 2026-08-18T00:00:00Z | canary-old | b | s1 | PASS | [x](y) |\n';
    const newRow = '| 2026-08-19T03:00:18Z | canary-new | b | s2 | UNVERIFIED | [x](y) |';
    const out = upsertIndex(existing, newRow);
    expect(out).toContain('canary-old');
    expect(out).toContain('canary-new');
    expect(out.indexOf('canary-old')).toBeLessThan(out.indexOf('canary-new'));
  });
});
