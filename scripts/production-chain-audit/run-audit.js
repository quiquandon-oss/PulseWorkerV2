#!/usr/bin/env node
// Orchestrates the full production-chain audit. READ ONLY against
// production: this file never imports or calls anything that deploys,
// writes to D1, or mutates GitHub state beyond committing its own output
// files (done in the workflow, not here -- this script only writes local
// files).

import { writeFileSync, existsSync, mkdirSync } from 'node:fs';
import {
  STATUS, redactSecrets, scanForLeakedSecrets,
  evaluateFrontend, evaluateWorkerToD1, evaluatePrediction, evaluateAnalystRelay,
  evaluateRelaySubmission, evaluateContextHashIntegrity, evaluateFactualContextParity,
  evaluateCatalystLedger, evaluateLearningLoop,
  evaluateRelayBudget, evaluateSafety, classifyFailure, computeEndToEnd,
} from './lib.js';
import { getMainHeadSha, getFileContentAtRef, getLatestSuccessfulDeploy } from './github-checks.js';
import { getWorkerMetadata, getLatestDeployment } from '../canary-audit/cloudflare-checks.js';
import {
  getAllAnalystRelayEntries, getAnalystRelayByRelayId, getCatalystsForInvestigation,
  getLatestResolvedPrediction, getSelectionDecisionAfter, checkD1Reachable,
} from './d1-checks.js';

function logProgress(marker) {
  try {
    mkdirSync('artifacts/production-chain-audit', { recursive: true });
    writeFileSync('artifacts/production-chain-audit/PROGRESS.log', `${new Date().toISOString()} ${marker}\n`, { flag: 'a' });
  } catch { /* best-effort */ }
}

function extractDbBindingFromSource(wranglerSource) {
  if (!wranglerSource) return { id: null, name: null };
  const idMatch = wranglerSource.match(/database_id\s*=\s*"([^"]+)"/);
  const nameMatch = wranglerSource.match(/database_name\s*=\s*"([^"]+)"/);
  return { id: idMatch ? idMatch[1] : null, name: nameMatch ? nameMatch[1] : null };
}

async function main() {
  logProgress('main() started');

  const workerOwner = 'quiquandon-oss';
  const workerRepo = 'PulseWorkerV2';
  const frontendOwner = 'quiquandon-oss';
  const frontendRepo = 'CryptoPulseV2';
  const workerUrl = 'https://pulseworker-v2.quiquandon.workers.dev';
  const expectedDatabaseId = 'f91ca980-b886-423a-bd6f-f3baea46d181'; // public identifier, not a secret

  const githubToken = process.env.GITHUB_TOKEN || null;
  const cfToken = process.env.CLOUDFLARE_API_TOKEN || null;
  const cfAccountId = process.env.CLOUDFLARE_ACCOUNT_ID || 'f58e761fbc8e62dc404d8684290af264'; // public, see wrangler.toml comment

  const now = new Date();
  const timestamp = now.toISOString();

  const report = {
    audit_version: '2.0',
    audit_scope_note: 'Approved scope update: Google Search grounding removed as a blocking criterion (the automated grounded API investigation it applied to was removed from production entirely). This audit now verifies the actual Analyst Relay response returned by the application -- provider receipt + non-empty valid response, investigation_id, D1 persistence, shared investigation-context hash integrity, Analyst Relay factual-context parity, and downstream learning/selection.',
    audit_timestamp: timestamp,
    repository: `${workerOwner}/${workerRepo}`,
    frontend_repository: `${frontendOwner}/${frontendRepo}`,
    production: {},
    frontend: {},
    worker_to_d1: {},
    prediction: {},
    analyst_relay: {},
    relay_submission: {},
    context_hash_integrity: {},
    factual_context_parity: {},
    catalyst_ledger: {},
    learning_loop: {},
    relay_budget: {},
    safety: {},
    end_to_end: {},
  };

  let productionWritesPerformed = false; // stays false by construction -- every call below is a read

  // ---- 1. Production deployment ----
  logProgress('fetching production deployment evidence');
  const mainHead = await getMainHeadSha(workerOwner, workerRepo, githubToken);
  const latestDeploy = await getLatestSuccessfulDeploy(workerOwner, workerRepo, githubToken);
  const workerMeta = cfToken ? await getWorkerMetadata(cfAccountId, 'pulseworker-v2', cfToken) : null;
  const cfDeployment = cfToken ? await getLatestDeployment(cfAccountId, 'pulseworker-v2', cfToken) : null;

  const deployedSha = latestDeploy ? latestDeploy.head_sha : null;
  report.production = {
    worker_url: workerUrl,
    worker_sha: deployedSha,
    deployment_id: cfDeployment ? cfDeployment.id : null,
    worker_version: workerMeta ? workerMeta.etag : null,
    deployment_timestamp: latestDeploy ? latestDeploy.created_at : (cfDeployment ? cfDeployment.created_on : null),
  };
  if (!cfDeployment) {
    report.production.deployment_id_note = cfToken
      ? 'Cloudflare deployments API returned no gradual-deployment record for this script (expected for a plain, non-gradual deploy) -- deployed SHA is instead evidenced via the GitHub Deploy Worker run that most recently succeeded on main.'
      : 'CLOUDFLARE_API_TOKEN not available to this run -- deployment_id could not be queried at all.';
  }

  // ---- 2. Worker source (for D1-binding extraction only -- budget
  // extraction removed, Analyst Relay is unbudgeted by design) ----
  const shaForSourceFetch = deployedSha || (mainHead && mainHead.sha);
  logProgress('fetching worker.js and wrangler.toml source at ref ' + shaForSourceFetch);
  const wranglerSource = shaForSourceFetch ? await getFileContentAtRef(workerOwner, workerRepo, 'wrangler.toml', shaForSourceFetch, githubToken) : null;

  // ---- 3. Frontend ----
  logProgress('fetching frontend evidence');
  const frontendHead = await getMainHeadSha(frontendOwner, frontendRepo, githubToken);
  const frontendSource = frontendHead ? await getFileContentAtRef(frontendOwner, frontendRepo, 'index.html', frontendHead.sha, githubToken) : null;
  report.frontend = evaluateFrontend({ sourceText: frontendSource, commitSha: frontendHead ? frontendHead.sha : null, expectedWorkerUrl: workerUrl });

  // ---- 4. Worker -> D1 binding ----
  logProgress('checking D1 reachability');
  const { id: configuredDbId, name: configuredDbName } = extractDbBindingFromSource(wranglerSource);
  const d1Reachable = await checkD1Reachable(cfAccountId, expectedDatabaseId, cfToken);
  report.worker_to_d1 = evaluateWorkerToD1({ configuredDatabaseId: configuredDbId, configuredDatabaseName: configuredDbName, liveQueryOk: d1Reachable, expectedDatabaseId });

  // ---- 5. Production prediction ----
  logProgress('fetching latest production prediction');
  const predictionResult = await getLatestResolvedPrediction(cfAccountId, expectedDatabaseId, cfToken);
  const predictionRow = predictionResult.ok ? predictionResult.row : null;
  report.prediction = evaluatePrediction({ row: predictionRow, deployedSha });

  // ---- 6. Analyst Relay response (replaces the old automated-Gemini
  // section entirely -- see evaluateAnalystRelay's own comment) ----
  logProgress('fetching Analyst Relay submissions');
  const relayEntriesResult = await getAllAnalystRelayEntries(cfAccountId, expectedDatabaseId, cfToken);
  const allRelayEntries = relayEntriesResult.ok ? relayEntriesResult.rows : [];
  report.analyst_relay = evaluateAnalystRelay({ allRelayEntries });

  // ---- 7. Relay submission receipt ("provider 200" equivalent) ----
  let successfulRelayRow = null;
  if (report.analyst_relay.status === STATUS.PASS) {
    logProgress('fetching relay row for ' + report.analyst_relay.investigation_id);
    const r = await getAnalystRelayByRelayId(cfAccountId, expectedDatabaseId, cfToken, report.analyst_relay.investigation_id);
    successfulRelayRow = r.ok ? r.row : null;
  }
  report.relay_submission = evaluateRelaySubmission({ relayRow: successfulRelayRow });

  // ---- 8. Shared investigation-context hash integrity ----
  report.context_hash_integrity = await evaluateContextHashIntegrity({
    contextJsonRaw: successfulRelayRow ? successfulRelayRow.context_json : null,
    storedContextHash: successfulRelayRow ? successfulRelayRow.context_hash : null,
  });

  // ---- 9. Analyst Relay factual-context parity ----
  let relayPrimaryAsset = null;
  if (successfulRelayRow) {
    try { relayPrimaryAsset = (JSON.parse(successfulRelayRow.assets_json || '[]'))[0] || null; } catch { /* leave null */ }
  }
  report.factual_context_parity = evaluateFactualContextParity({
    contextJsonRaw: successfulRelayRow ? successfulRelayRow.context_json : null,
    primaryAsset: relayPrimaryAsset,
  });

  // ---- 10. Catalyst ledger ----
  let catalystRows = [];
  if (report.analyst_relay.status === STATUS.PASS) {
    logProgress('fetching catalyst ledger rows for ' + report.analyst_relay.investigation_id);
    const r = await getCatalystsForInvestigation(cfAccountId, expectedDatabaseId, cfToken, report.analyst_relay.investigation_id);
    catalystRows = r.ok ? r.rows : [];
  }
  report.catalyst_ledger = evaluateCatalystLedger({
    investigationId: report.analyst_relay.status === STATUS.PASS ? report.analyst_relay.investigation_id : null,
    catalystRows,
    validationStatus: report.analyst_relay.status === STATUS.PASS ? report.analyst_relay.validation_status : null,
  });

  // ---- 11. Learning loop ----
  let selectionDecisionRow = null;
  if (report.analyst_relay.status === STATUS.PASS && relayPrimaryAsset) {
    logProgress('fetching selection_decisions after the relay submission');
    const r = await getSelectionDecisionAfter(cfAccountId, expectedDatabaseId, cfToken, relayPrimaryAsset, report.analyst_relay.timestamp);
    selectionDecisionRow = r.ok ? r.row : null;
  }
  report.learning_loop = evaluateLearningLoop({ selectionDecisionRow });

  // ---- 12. Relay budget (Analyst Relay is unbudgeted by design -- see
  // evaluateRelayBudget's own comment for why this is no longer a
  // configuration comparison) ----
  report.relay_budget = evaluateRelayBudget();

  // ---- 13. Safety ----
  report.safety = evaluateSafety({ productionWritesPerformed, secretsExposed: false }); // secretsExposed finalized after the leak scan below

  // ---- 14. End-to-end ----
  const endToEndBase = computeEndToEnd({
    frontend: report.frontend, workerToD1: report.worker_to_d1, prediction: report.prediction,
    analystRelay: report.analyst_relay, relaySubmission: report.relay_submission,
    contextHashIntegrity: report.context_hash_integrity, factualContextParity: report.factual_context_parity,
    catalystLedger: report.catalyst_ledger, learningLoop: report.learning_loop, safety: report.safety,
  });
  report.end_to_end = {
    status: endToEndBase.status,
    blocking_reason: endToEndBase.blocking_reason,
    chain: ['prediction', 'analyst_relay', 'relay_submission', 'context_hash_integrity', 'factual_context_parity', 'd1_persistence', 'learning', 'selection'],
  };

  // ---- Redact + leak-scan the fully assembled report before writing it anywhere ----
  const redacted = redactSecrets(report);
  const serialized = JSON.stringify(redacted, null, 2);
  const leakScan = scanForLeakedSecrets(serialized);
  redacted.safety.secrets_exposed = !leakScan.clean;
  if (!leakScan.clean) {
    redacted.safety.status = STATUS.FAIL;
    redacted.safety.evidence.push(`LEAK SCAN FAILED: matched patterns ${leakScan.matchedPatterns.join(', ')}`);
    redacted.end_to_end.status = 'RED';
    redacted.end_to_end.blocking_reason = 'Safety violation: secret pattern detected in report output before write';
  }

  const finalSerialized = JSON.stringify(redacted, null, 2);

  // ---- Write files ----
  const pad = (n) => String(n).padStart(2, '0');
  const historyTimestamp = `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${pad(now.getUTCDate())}T${pad(now.getUTCHours())}-${pad(now.getUTCMinutes())}-${pad(now.getUTCSeconds())}Z`;
  mkdirSync('.ai/audits/history', { recursive: true });
  const latestPath = '.ai/audits/latest-production-audit.json';
  const historyPath = `.ai/audits/history/${historyTimestamp}-production-audit.json`;
  writeFileSync(latestPath, finalSerialized);
  writeFileSync(historyPath, finalSerialized);
  logProgress(`wrote ${latestPath} and ${historyPath}`);

  console.log(finalSerialized);

  // GitHub Actions step outputs
  if (process.env.GITHUB_OUTPUT) {
    const out = [
      `latest_path=${latestPath}`,
      `history_path=${historyPath}`,
      `end_to_end=${redacted.end_to_end.status}`,
      `blocking_reason=${(redacted.end_to_end.blocking_reason || '').replace(/\n/g, ' ').slice(0, 500)}`,
    ].join('\n');
    writeFileSync(process.env.GITHUB_OUTPUT, out + '\n', { flag: 'a' });
  }

  logProgress('main() completed successfully');
}

main().catch((err) => {
  logProgress('main() FAILED: ' + (err && err.stack || err));
  console.error(err);
  process.exit(1);
});
