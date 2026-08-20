// Pure report formatting. Takes a fully-assembled report object (built by
// run-audit.js from the *-checks.js modules + lib.js decisions) and
// produces the exact Markdown/JSON shapes required. No I/O, no
// credentials -- fully unit-testable with a hand-built report object.

function fmtCheck(c) {
  if (!c) return { status: 'UNAVAILABLE', evidence: [] };
  return { status: c.status, evidence: c.evidence || [] };
}
function row(label, c) {
  const f = fmtCheck(c);
  return `| ${label} | ${f.status} | ${f.evidence.join('; ') || '—'} |`;
}

export function buildMarkdownReport(report) {
  const g = report.github || {};
  const cf = report.cloudflare || {};
  const d1 = report.d1 || {};
  const order = report.execution_order || {};
  const budgets = report.budgets || {};
  const dup = report.duplicates || {};

  const lines = [];
  lines.push('# CryptoPulseV2 Canary Audit');
  lines.push('');
  lines.push('## Executive Result');
  lines.push('');
  lines.push(report.result);
  lines.push('');
  lines.push('## Target');
  lines.push('');
  lines.push(`- Repository: ${report.repository}`);
  lines.push(`- Branch: ${report.target_branch}`);
  lines.push(`- Commit: ${report.target_sha}`);
  lines.push(`- Environment: ${report.environment}`);
  lines.push(`- Audit ID: ${report.audit_id}`);
  lines.push(`- Audit timestamp: ${report.timestamp}`);
  lines.push('');
  lines.push('## GitHub Execution');
  lines.push('');
  lines.push('| Check | Result | Evidence |');
  lines.push('|---|---|---|');
  lines.push(row('Target commit exists', g.target_commit_exists));
  lines.push(row('Target branch exists', g.target_branch_exists));
  lines.push(row('Workflow exists', g.workflow_exists));
  lines.push(row('Workflow executed', g.workflow_executed));
  lines.push(row('Correct branch', g.correct_branch));
  lines.push(row('Correct SHA', g.correct_sha));
  lines.push(row('Workflow succeeded', g.workflow_succeeded));
  lines.push('');
  lines.push('## Cloudflare Deployment');
  lines.push('');
  lines.push('| Check | Result | Evidence |');
  lines.push('|---|---|---|');
  lines.push(row('Worker identified', cf.worker_identified));
  lines.push(row('Deployment identified', cf.deployment_identified));
  lines.push(row('Deployment matches target', cf.deployment_matches_target));
  lines.push(row('Deployment timestamp', cf.deployment_timestamp));
  lines.push(row('Live verification', cf.live_verification));
  lines.push('');
  lines.push('## D1 Execution Chain');
  lines.push('');
  lines.push('| Stage | Result | Evidence |');
  lines.push('|---|---|---|');
  lines.push(row('Investigation/catalyst', d1.investigation));
  lines.push(row('Prediction', d1.prediction));
  lines.push(row('k-NN selection', d1.selection));
  lines.push(row('Resolution/outcome', d1.resolution));
  lines.push(row('Gemini', d1.gemini));
  lines.push(row('Google Search grounding', d1.grounding));
  lines.push('');
  lines.push('## Execution Ordering');
  lines.push('');
  lines.push('Prediction before Gemini:');
  lines.push('');
  lines.push(fmtCheck(order.prediction_before_gemini).status);
  lines.push('');
  lines.push('Evidence:');
  lines.push('');
  lines.push(fmtCheck(order.prediction_before_gemini).evidence.join('; ') || 'none');
  lines.push('');
  lines.push('## Budget');
  lines.push('');
  lines.push(`- Hourly budget: ${fmtCheck(budgets.hourly).status}`);
  lines.push(`- Daily budget: ${fmtCheck(budgets.daily).status}`);
  lines.push(`- Observed calls: ${budgets.observed_calls ?? 'UNAVAILABLE'}`);
  lines.push(`- Budget violation: ${budgets.violation ?? 'UNAVAILABLE'}`);
  lines.push('');
  lines.push('## Duplicate Detection');
  lines.push('');
  lines.push(`- Duplicate investigation: ${dup.investigation ?? 'UNAVAILABLE'}`);
  lines.push(`- Duplicate prediction: ${dup.prediction ?? 'UNAVAILABLE'}`);
  lines.push(`- Duplicate k-NN decision: ${dup.selection_decision ?? 'UNAVAILABLE'}`);
  lines.push(`- Duplicate Gemini: ${dup.gemini ?? 'UNAVAILABLE'}`);
  lines.push(`- Duplicate audit event: ${dup.audit_event ?? 'UNAVAILABLE'}`);
  lines.push('');
  lines.push('## Evidence');
  lines.push('');
  for (const e of report.evidence || []) {
    lines.push(`- **${e.source}** — timestamp: ${e.timestamp ?? '—'}, id: ${e.id ?? '—'}, sha: ${e.sha ?? '—'}, workflow_run_id: ${e.workflow_run_id ?? '—'}, job_id: ${e.job_id ?? '—'}, db_record_id: ${e.db_record_id ?? '—'}, correlation_id: ${e.correlation_id ?? '—'}`);
  }
  lines.push('');
  lines.push('## Final Decision');
  lines.push('');
  lines.push(report.result === 'PASS' ? 'CANARY PASSED' : report.result === 'FAIL' ? 'CANARY FAILED' : 'CANARY UNVERIFIED');
  lines.push('');
  return lines.join('\n');
}

export function buildJsonReport(report) {
  // Shape matches the spec's minimum required structure exactly, plus the
  // extra fields (audit_id, timestamp, target_sha, etc.) already required
  // elsewhere in the task.
  return {
    audit_id: report.audit_id,
    timestamp: report.timestamp,
    repository: report.repository,
    target_branch: report.target_branch,
    target_sha: report.target_sha,
    environment: report.environment,
    result: report.result,
    github: report.github || {},
    cloudflare: report.cloudflare || {},
    d1: report.d1 || {},
    execution_order: report.execution_order || {},
    gemini: report.gemini || {},
    grounding: report.grounding || {},
    budgets: report.budgets || {},
    duplicates: report.duplicates || {},
    evidence: report.evidence || [],
  };
}

export function buildStepSummary(report) {
  const lines = [];
  lines.push('# CryptoPulseV2 Canary Audit');
  lines.push('');
  lines.push('**Target:**');
  lines.push(`${report.target_branch}`);
  lines.push(`${report.target_sha}`);
  lines.push('');
  lines.push(`GitHub execution: ${fmtCheck(report.github && report.github.workflow_succeeded).status}`);
  lines.push(`Cloudflare deployment: ${fmtCheck(report.cloudflare && report.cloudflare.live_verification).status}`);
  lines.push(`D1 chain: ${fmtCheck(report.d1 && report.d1.gemini).status}`);
  lines.push(`Prediction before Gemini: ${fmtCheck(report.execution_order && report.execution_order.prediction_before_gemini).status}`);
  lines.push(`Google Search grounding: ${fmtCheck(report.d1 && report.d1.grounding).status}`);
  lines.push(`Budget: ${(report.budgets && report.budgets.violation === 'NO') ? 'PASS' : (report.budgets && report.budgets.violation === 'YES') ? 'FAIL' : 'UNAVAILABLE'}`);
  lines.push(`Duplicates: ${(report.duplicates && report.duplicates.gemini === 'NO') ? 'PASS' : (report.duplicates && report.duplicates.gemini === 'YES') ? 'FAIL' : 'UNAVAILABLE'}`);
  lines.push('');
  lines.push(`**FINAL: ${report.result}**`);
  lines.push('');
  return lines.join('\n');
}

export function buildIndexRow(report, reportPath) {
  return `| ${report.timestamp} | ${report.audit_id} | ${report.target_branch} | \`${report.target_sha}\` | ${report.result} | [${report.audit_id}](${reportPath}) |`;
}

export function upsertIndex(existingContent, newRow) {
  const header = '# CryptoPulseV2 Canary Audit Index\n\n| Timestamp | Audit ID | Branch | SHA | Result | Report |\n|---|---|---|---|---|---|';
  if (!existingContent || !existingContent.trim()) {
    return `${header}\n${newRow}\n`;
  }
  // Never remove existing rows -- append the new one at the end, preserve
  // everything else byte-for-byte.
  const trimmed = existingContent.replace(/\n+$/, '');
  return `${trimmed}\n${newRow}\n`;
}
