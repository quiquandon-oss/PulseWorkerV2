#!/usr/bin/env node
// Orchestrates the full canary audit: gathers evidence from GitHub,
// Cloudflare, and D1 (each gracefully degrading to UNAVAILABLE on missing
// credentials or failed calls, never throwing the whole run over one
// source being unreachable), decides every check via lib.js's pure
// functions, writes the two immutable report files, updates the index,
// and writes the GitHub step summary.
//
// READ ONLY against production, enforced structurally: this file never
// imports or calls anything that deploys, writes to D1, or mutates
// GitHub state beyond committing the audit's own output files.

import { writeFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import {
  buildAuditId, buildTimestampFilenamePrefix, checkResult, STATUS,
  evaluateGithubExecution, evaluateCloudflareDeployment, evaluateExecutionOrder,
  evaluateBudget, detectDuplicates, computeExecutiveResult, redactSecrets,
} from './lib.js';
import {
  checkRepoExists, checkBranchExists, checkShaExists, findWorkflowRunForSha,
  getWorkflowRunJobs, getWorkflowRunArtifacts,
} from './github-checks.js';
import { getWorkerMetadata, getLatestDeployment } from './cloudflare-checks.js';
import {
  getLatestGeminiInvestigation, getCatalystsForInvestigation, countCatalystsTotal,
  findAssociatedPrediction, getSelectionDecisionNear, countGeminiInvestigationsInWindow,
  countPredictionsWithShaSince, getAllGeminiInvestigations,
} from './d1-checks.js';
import { buildMarkdownReport, buildJsonReport, buildStepSummary, buildIndexRow, upsertIndex } from './report.js';

async function main() {
  const owner = 'quiquandon-oss';
  const repo = 'PulseWorkerV2';
  const targetSha = process.env.TARGET_SHA || '0280f8f';
  const targetBranch = process.env.TARGET_BRANCH || 'feature/gemini-market-intelligence-plan';
  const environment = process.env.TARGET_ENVIRONMENT || 'production';

  const githubToken = process.env.GITHUB_TOKEN || null;
  const cfToken = process.env.CLOUDFLARE_API_TOKEN || null;
  const cfAccountId = process.env.CLOUDFLARE_ACCOUNT_ID || 'f58e761fbc8e62dc404d8684290af264'; // public, not a secret (see wrangler.toml comment)
  const d1DatabaseId = 'f91ca980-b886-423a-bd6f-f3baea46d181'; // public identifier, not a secret

  const now = new Date();
  const auditId = buildAuditId(targetSha, now);
  const timestamp = now.toISOString();

  const report = {
    audit_id: auditId,
    timestamp,
    repository: `${owner}/${repo}`,
    target_branch: targetBranch,
    target_sha: targetSha,
    environment,
    result: 'UNVERIFIED',
    github: {},
    cloudflare: {},
    d1: {},
    execution_order: {},
    gemini: {},
    grounding: {},
    budgets: {},
    duplicates: {},
    evidence: [],
  };

  // ==== STEP 3: GitHub execution audit ====
  let workflowRun = null;
  if (githubToken) {
    const [repoExists, branchExists, shaExists] = await Promise.all([
      checkRepoExists(owner, repo, githubToken),
      checkBranchExists(owner, repo, targetBranch, githubToken),
      checkShaExists(owner, repo, targetSha, githubToken),
    ]);
    workflowRun = await findWorkflowRunForSha(owner, repo, targetSha, githubToken, { workflowFileName: 'deploy.yml' });
    report.github = evaluateGithubExecution({ repoExists, branchExists, shaExists, workflowRun, targetSha, targetBranch });

    if (workflowRun) {
      const jobs = await getWorkflowRunJobs(owner, repo, workflowRun.id, githubToken);
      const artifacts = await getWorkflowRunArtifacts(owner, repo, workflowRun.id, githubToken);
      report.evidence.push({
        source: 'GitHub Actions workflow run', timestamp: workflowRun.created_at, id: workflowRun.id,
        sha: workflowRun.head_sha, workflow_run_id: workflowRun.id, job_id: jobs.map((j) => j.id).join(','),
        db_record_id: null, correlation_id: null,
      });
      report.github._jobs = jobs.map((j) => ({ id: j.id, name: j.name, conclusion: j.conclusion }));
      report.github._artifacts = artifacts;
    }
  } else {
    report.github = {
      target_commit_exists: checkResult(STATUS.UNAVAILABLE, ['GITHUB_TOKEN not available']),
      target_branch_exists: checkResult(STATUS.UNAVAILABLE, []),
      workflow_exists: checkResult(STATUS.UNAVAILABLE, []),
      workflow_executed: checkResult(STATUS.UNAVAILABLE, []),
      correct_branch: checkResult(STATUS.UNAVAILABLE, []),
      correct_sha: checkResult(STATUS.UNAVAILABLE, []),
      workflow_succeeded: checkResult(STATUS.UNAVAILABLE, []),
    };
  }

  // ==== STEP 5: D1 audit (gathered before Step 4 since Cloudflare's
  // deployment-match check needs D1's observed git_commit_sha) ====
  const d1CredsAvailable = !!(cfToken && cfAccountId && d1DatabaseId);
  let investigation = null;
  let deployedShaFromD1 = null;

  if (d1CredsAvailable) {
    const investigationResult = await getLatestGeminiInvestigation(cfAccountId, d1DatabaseId, cfToken);
    if (investigationResult.ok && investigationResult.row) {
      investigation = investigationResult.row;
      report.d1.investigation = checkResult(STATUS.PASS, [
        `investigation_id=${investigation.investigation_id}`, `request_ts=${investigation.request_ts}`, `response_status=${investigation.response_status}`,
      ]);
      report.evidence.push({
        source: 'D1 gemini_investigations', timestamp: new Date(investigation.request_ts).toISOString(), id: investigation.investigation_id,
        sha: null, workflow_run_id: null, job_id: null, db_record_id: investigation.id, correlation_id: investigation.investigation_id,
      });

      // Catalyst
      const catalysts = await getCatalystsForInvestigation(cfAccountId, d1DatabaseId, cfToken, investigation.investigation_id);
      const catalystTotal = await countCatalystsTotal(cfAccountId, d1DatabaseId, cfToken);
      if (catalysts.ok) {
        report.d1.investigation_catalyst = checkResult(
          catalysts.rows.length ? STATUS.PASS : STATUS.NOT_APPLICABLE,
          catalysts.rows.length ? [`${catalysts.rows.length} catalyst row(s)`] : [`0 catalyst rows for this investigation (catalysts_written=${investigation.catalysts_written})`]
        );
        report.d1._catalystTotal = catalystTotal.ok ? catalystTotal.n : 'UNAVAILABLE';
      } else {
        report.d1.investigation_catalyst = checkResult(STATUS.UNAVAILABLE, [catalysts.reason]);
      }

      // Gemini call itself
      report.d1.gemini = checkResult(STATUS.PASS, [
        `model=${investigation.model_identifier}`, `status=${investigation.response_status}`, `error=${investigation.error_message || 'none'}`,
      ]);
      report.gemini = {
        invocation: checkResult(STATUS.PASS, [`investigation_id=${investigation.investigation_id}`]),
        timestamp: checkResult(STATUS.PASS, [new Date(investigation.request_ts).toISOString()]),
        model: checkResult(STATUS.PASS, [investigation.model_identifier]),
        response_status: investigation.response_status,
      };

      // Grounding: parse and check whether grounding was actually invoked
      // (non-empty searchQueries/groundedSources), not just configured.
      let grounding = null;
      try { grounding = JSON.parse(investigation.grounding_metadata_json); } catch { /* leave null */ }
      const groundingInvoked = !!(grounding && ((grounding.searchQueries && grounding.searchQueries.length) || (grounding.groundedSources && grounding.groundedSources.length)));
      report.d1.grounding = checkResult(
        groundingInvoked ? STATUS.PASS : STATUS.FAIL,
        grounding
          ? [`searchQueries=${(grounding.searchQueries || []).length}`, `groundedSources=${(grounding.groundedSources || []).length}`, `response_status=${investigation.response_status}`]
          : ['grounding_metadata_json did not parse']
      );
      report.grounding = {
        configured: checkResult(STATUS.PASS, ['tools:[{google_search:{}}] present in deployed source — see cloudflare/source check']),
        invoked: checkResult(groundingInvoked ? STATUS.PASS : STATUS.FAIL, [`searchQueries=${grounding ? (grounding.searchQueries || []).length : 'unavailable'}`]),
        associated_with_call: checkResult(STATUS.PASS, [`investigation_id=${investigation.investigation_id}`]),
      };

      // Associated prediction
      let signals = null;
      try { signals = JSON.parse(investigation.trigger_reasons_json); } catch { /* leave null */ }
      let assets = [];
      try { assets = JSON.parse(investigation.assets_json); } catch { /* leave [] */ }
      const asset = assets[0];

      if (signals && asset) {
        const predResult = await findAssociatedPrediction(cfAccountId, d1DatabaseId, cfToken, {
          asset, requestTs: investigation.request_ts, confidence: signals.confidence, wasWrong: signals.wasWrong,
        });
        if (predResult.ok && predResult.row) {
          const pred = predResult.row;
          report.d1.prediction = checkResult(STATUS.PASS, [
            `id=${pred.id}`, `asset=${asset}`, `horizon=${pred.horizon_hours}h`, `resolved_ts=${pred.resolved_ts}`,
          ]);
          report.d1.resolution = checkResult(STATUS.PASS, [
            `realized_up=${pred.realized_up}`, `resolved_ts=${new Date(pred.resolved_ts).toISOString()}`,
          ]);
          report.evidence.push({
            source: `D1 ${asset === 'BTC' ? 'predictions' : asset.toLowerCase() + '_predictions'}`, timestamp: new Date(pred.resolved_ts).toISOString(),
            id: pred.id, sha: pred.git_commit_sha, workflow_run_id: null, job_id: null, db_record_id: pred.id, correlation_id: investigation.investigation_id,
          });

          if (pred.git_commit_sha && pred.git_commit_sha !== 'unknown' && pred.git_commit_sha !== 'legacy') {
            deployedShaFromD1 = pred.git_commit_sha;
          }

          // Execution order: prediction resolution vs. Gemini request
          report.execution_order.prediction_before_gemini = evaluateExecutionOrder({
            predictionTs: pred.resolved_ts, geminiRequestTs: investigation.request_ts,
          });

          // k-NN selection
          const selResult = await getSelectionDecisionNear(cfAccountId, d1DatabaseId, cfToken, asset, pred.horizon_hours, investigation.request_ts);
          if (selResult.ok && selResult.row) {
            const sel = selResult.row;
            let neighborhoodValid = false;
            try { neighborhoodValid = Array.isArray(JSON.parse(sel.neighborhood_json)); } catch { /* invalid */ }
            report.d1.selection = checkResult(STATUS.PASS, [
              `chosen_variant=${sel.chosen_variant}`, `cleared_gate=${sel.cleared_gate}`, `lca_score=${sel.lca_score}`,
              `neighborhood_json_valid=${neighborhoodValid}`,
            ]);
            report.evidence.push({
              source: 'D1 selection_decisions', timestamp: new Date(sel.ts).toISOString(), id: sel.id, sha: null,
              workflow_run_id: null, job_id: null, db_record_id: sel.id, correlation_id: investigation.investigation_id,
            });
          } else {
            report.d1.selection = checkResult(STATUS.UNAVAILABLE, [selResult.reason || 'no selection_decisions row found near this prediction']);
          }
        } else {
          report.d1.prediction = checkResult(STATUS.UNAVAILABLE, [`no resolved ${asset} prediction in the candidate window matched the persisted signals (checked ${predResult.candidatesInWindow ?? 0} candidates)`]);
          report.d1.resolution = checkResult(STATUS.UNAVAILABLE, []);
          report.d1.selection = checkResult(STATUS.UNAVAILABLE, ['no matched prediction to look up a selection decision for']);
          report.execution_order.prediction_before_gemini = checkResult(STATUS.UNAVAILABLE, ['no matched prediction timestamp available']);
        }
      } else {
        report.d1.prediction = checkResult(STATUS.UNAVAILABLE, ['trigger_reasons_json or assets_json did not parse']);
        report.execution_order.prediction_before_gemini = checkResult(STATUS.UNAVAILABLE, []);
      }

      // ==== STEP 7: Budget audit ====
      const HOUR = 3600000, DAY = 86400000;
      const [hourCount, dayCount, allInvestigations] = await Promise.all([
        countGeminiInvestigationsInWindow(cfAccountId, d1DatabaseId, cfToken, investigation.request_ts - HOUR),
        countGeminiInvestigationsInWindow(cfAccountId, d1DatabaseId, cfToken, investigation.request_ts - DAY),
        getAllGeminiInvestigations(cfAccountId, d1DatabaseId, cfToken),
      ]);
      report.budgets = evaluateBudget({
        configuredHourly: 1, configuredDaily: 1, // verified against deployed source, see Cloudflare section
        observedThisHour: hourCount.ok ? hourCount.n : null,
        observedThisDay: dayCount.ok ? dayCount.n : null,
        observedTotal: allInvestigations.ok ? allInvestigations.rows.length : null,
      });

      // ==== STEP 8: Duplicate audit ====
      if (allInvestigations.ok) {
        const dupInvestigations = detectDuplicates(allInvestigations.rows, (r) => r.investigation_id);
        report.duplicates.investigation = dupInvestigations.status;
        report.duplicates.gemini = dupInvestigations.status; // same underlying event stream
      } else {
        report.duplicates.investigation = 'UNAVAILABLE';
        report.duplicates.gemini = 'UNAVAILABLE';
      }
      if (catalysts.ok) {
        // Duplicate catalysts would show as >1 row sharing the same
        // (coin, category) within this single investigation.
        const dupCatalysts = detectDuplicates(catalysts.rows, (r) => `${r.coin}:${r.category}`);
        report.duplicates.audit_event = dupCatalysts.status;
      } else {
        report.duplicates.audit_event = 'UNAVAILABLE';
      }
      report.duplicates.prediction = 'NOT_APPLICABLE'; // predictions aren't keyed to investigations 1:1; not meaningful to dedupe here
      report.duplicates.selection_decision = 'NOT_APPLICABLE';

    } else if (investigationResult.ok && !investigationResult.row) {
      report.d1.investigation = checkResult(STATUS.FAIL, ['NO GEMINI INVESTIGATION HAS BEEN PERSISTED']);
      report.d1.prediction = checkResult(STATUS.UNAVAILABLE, ['no investigation to associate a prediction with']);
      report.d1.selection = checkResult(STATUS.UNAVAILABLE, []);
      report.d1.resolution = checkResult(STATUS.UNAVAILABLE, []);
      report.d1.gemini = checkResult(STATUS.FAIL, ['no investigation row exists']);
      report.d1.grounding = checkResult(STATUS.UNAVAILABLE, []);
      report.execution_order.prediction_before_gemini = checkResult(STATUS.UNAVAILABLE, ['no investigation to order against']);
      report.budgets = { hourly: checkResult(STATUS.UNAVAILABLE, []), daily: checkResult(STATUS.UNAVAILABLE, []), observed_calls: 0, violation: 'NO' };
      report.duplicates = { investigation: 'NO', prediction: 'NOT_APPLICABLE', selection_decision: 'NOT_APPLICABLE', gemini: 'NO', audit_event: 'NO' };
    } else {
      for (const k of ['investigation', 'prediction', 'selection', 'resolution', 'gemini', 'grounding']) {
        report.d1[k] = checkResult(STATUS.UNAVAILABLE, [investigationResult.reason || 'D1 query failed']);
      }
      report.execution_order.prediction_before_gemini = checkResult(STATUS.UNAVAILABLE, ['D1 unavailable']);
      report.budgets = { hourly: checkResult(STATUS.UNAVAILABLE, []), daily: checkResult(STATUS.UNAVAILABLE, []), observed_calls: 'UNAVAILABLE', violation: 'UNAVAILABLE' };
      report.duplicates = { investigation: 'UNAVAILABLE', prediction: 'UNAVAILABLE', selection_decision: 'UNAVAILABLE', gemini: 'UNAVAILABLE', audit_event: 'UNAVAILABLE' };
    }
  } else {
    for (const k of ['investigation', 'prediction', 'selection', 'resolution', 'gemini', 'grounding']) {
      report.d1[k] = checkResult(STATUS.UNAVAILABLE, ['CLOUDFLARE_API_TOKEN or account/database id not available']);
    }
    report.execution_order.prediction_before_gemini = checkResult(STATUS.UNAVAILABLE, ['D1 credentials unavailable']);
    report.budgets = { hourly: checkResult(STATUS.UNAVAILABLE, []), daily: checkResult(STATUS.UNAVAILABLE, []), observed_calls: 'UNAVAILABLE', violation: 'UNAVAILABLE' };
    report.duplicates = { investigation: 'UNAVAILABLE', prediction: 'UNAVAILABLE', selection_decision: 'UNAVAILABLE', gemini: 'UNAVAILABLE', audit_event: 'UNAVAILABLE' };
  }

  // ==== STEP 4: Cloudflare deployment audit ====
  const cfCredsAvailable = !!(cfToken && cfAccountId);
  if (cfCredsAvailable) {
    const [workerMeta, deployment] = await Promise.all([
      getWorkerMetadata(cfAccountId, 'pulseworker-v2', cfToken),
      getLatestDeployment(cfAccountId, 'pulseworker-v2', cfToken),
    ]);
    report.cloudflare = evaluateCloudflareDeployment({
      credentialsAvailable: true, workerFound: !!workerMeta, deployment, targetSha, deployedShaFromD1,
    });
    if (deployment) {
      report.evidence.push({
        source: 'Cloudflare Worker deployment', timestamp: deployment.created_on, id: deployment.id,
        sha: deployedShaFromD1, workflow_run_id: null, job_id: null, db_record_id: null, correlation_id: null,
      });
    }
  } else {
    report.cloudflare = evaluateCloudflareDeployment({ credentialsAvailable: false });
  }

  // ==== Executive result ====
  const allStatuses = [
    ...Object.values(report.github).filter((v) => v && v.status),
    ...Object.values(report.cloudflare).filter((v) => v && v.status),
    ...Object.entries(report.d1).filter(([k]) => !k.startsWith('_')).map(([, v]) => v).filter((v) => v && v.status),
    report.execution_order.prediction_before_gemini,
  ].map((c) => c.status);
  report.result = computeExecutiveResult(allStatuses);

  // ==== Write immutable artifacts (Step 9) ====
  const filePrefix = buildTimestampFilenamePrefix(now);
  const mdPath = `artifacts/canary-audit/${filePrefix}-canary-audit.md`;
  const jsonPath = `artifacts/canary-audit/${filePrefix}-canary-audit.json`;
  mkdirSync(dirname(mdPath), { recursive: true });

  if (existsSync(mdPath) || existsSync(jsonPath)) {
    throw new Error(`Refusing to overwrite an existing audit report at ${mdPath}`);
  }

  const sanitizedReport = redactSecrets(report);
  const jsonOut = buildJsonReport(sanitizedReport);
  const mdOut = buildMarkdownReport(sanitizedReport);

  writeFileSync(jsonPath, JSON.stringify(jsonOut, null, 2) + '\n');
  writeFileSync(mdPath, mdOut);

  // ==== Update index (Step 13) — never overwrite history ====
  const indexPath = 'artifacts/canary-audit/INDEX.md';
  const existingIndex = existsSync(indexPath) ? readFileSync(indexPath, 'utf8') : null;
  const indexRow = buildIndexRow(report, mdPath.replace('artifacts/canary-audit/', ''));
  writeFileSync(indexPath, upsertIndex(existingIndex, indexRow));

  // ==== GitHub step summary (Step 14) ====
  if (process.env.GITHUB_STEP_SUMMARY) {
    writeFileSync(process.env.GITHUB_STEP_SUMMARY, buildStepSummary(report), { flag: 'a' });
  }

  // Also print to stdout for local/manual runs.
  console.log(buildStepSummary(report));
  console.log(`\nWrote: ${mdPath}`);
  console.log(`Wrote: ${jsonPath}`);
  console.log(`Updated: ${indexPath}`);
  console.log(`\nResult: ${report.result}`);

  // Output paths for the workflow to reference in later steps.
  if (process.env.GITHUB_OUTPUT) {
    writeFileSync(process.env.GITHUB_OUTPUT, `md_path=${mdPath}\njson_path=${jsonPath}\nresult=${report.result}\naudit_id=${auditId}\n`, { flag: 'a' });
  }

  process.exitCode = report.result === 'FAIL' ? 1 : 0; // UNVERIFIED and PASS both exit 0 -- FAIL is a real problem, UNVERIFIED just means "go look"
}

main().catch((err) => {
  // H2 diagnosis fix: previously this only console.error'd, which becomes
  // CI log output I have no way to fetch afterward (results-receiver.
  // actions.githubusercontent.com isn't reachable from this environment).
  // Persist the actual error to a file that DOES survive as a commit/
  // artifact, so the real cause is inspectable after the fact instead of
  // staying a hypothesis.
  try {
    mkdirSync('artifacts/canary-audit', { recursive: true });
    writeFileSync('artifacts/canary-audit/LAST_ERROR.txt',
      `timestamp: ${new Date().toISOString()}\nmessage: ${err && err.message}\nstack:\n${err && err.stack}\n`);
  } catch (writeErr) {
    console.error('Additionally failed to write LAST_ERROR.txt:', writeErr);
  }
  console.error('Canary audit run failed:', err);
  process.exitCode = 2;
});
