import { describe, it, expect } from 'vitest';
import {
  STATUS, section, redactSecrets, scanForLeakedSecrets,
  evaluateFrontend, evaluateWorkerToD1, evaluatePrediction, evaluateAnalystRelay,
  evaluateRelaySubmission, evaluateContextHashIntegrity, evaluateFactualContextParity,
  evaluateCatalystLedger, evaluateLearningLoop,
  evaluateRelayBudget, evaluateSafety, classifyFailure, computeEndToEnd,
} from './lib.js';

// The exact real context shape formatContextForAnalyst/computeContextHash
// (worker.js) produce -- used across the context-hash and factual-parity
// tests below so they exercise the real canonical field set, not an
// invented shape.
function makeRealContext(overrides = {}) {
  return {
    candidateId: 'LINK',
    primaryAsset: 'LINK',
    windowMs: 12600000,
    observations: {
      BTC: { available: true, predictedDirection: 'UP', actualDirection: 'UP', confidencePct: 78.8, wasWrong: false, isRegimeAnomaly: true, recentCycles: [] },
      ETH: { available: true, predictedDirection: 'UP', actualDirection: 'UP', confidencePct: 80, wasWrong: false, recentCycles: [] },
      LINK: { available: true, predictedDirection: 'UP', actualDirection: 'DOWN', confidencePct: 72.7, wasWrong: true, recentCycles: [] },
    },
    correlatedFailureAssetCount: 1,
    correlatedFailureAssets: ['LINK'],
    ...overrides,
  };
}

async function realContextHash(context) {
  const canonical = {
    candidateId: context.candidateId, primaryAsset: context.primaryAsset, windowMs: context.windowMs,
    observations: context.observations, correlatedFailureAssetCount: context.correlatedFailureAssetCount,
    correlatedFailureAssets: context.correlatedFailureAssets,
  };
  const json = JSON.stringify(canonical, Object.keys(canonical).sort());
  const bytes = new TextEncoder().encode(json);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

describe('section() — status validation', () => {
  it('accepts PASS/FAIL/NOT_VERIFIED', () => {
    expect(section(STATUS.PASS).status).toBe('PASS');
    expect(section(STATUS.FAIL).status).toBe('FAIL');
    expect(section(STATUS.NOT_VERIFIED).status).toBe('NOT_VERIFIED');
  });
  it('rejects an invalid status', () => {
    expect(() => section('MAYBE')).toThrow();
  });
});

describe('redactSecrets()', () => {
  it('redacts values under secret-shaped keys', () => {
    const out = redactSecrets({ apiKey: 'sk-real-value-123', name: 'fine' });
    expect(out.apiKey).toBe('[REDACTED]');
    expect(out.name).toBe('fine');
  });
  it('recurses into nested objects and arrays', () => {
    const out = redactSecrets({ nested: { token: 'abc123xyz', ok: 1 }, list: [{ password: 'hunter2' }] });
    expect(out.nested.token).toBe('[REDACTED]');
    expect(out.nested.ok).toBe(1);
    expect(out.list[0].password).toBe('[REDACTED]');
  });
});

describe('scanForLeakedSecrets()', () => {
  it('flags a Bearer token pattern', () => {
    const r = scanForLeakedSecrets('Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456');
    expect(r.clean).toBe(false);
  });
  it('flags a GitHub PAT pattern', () => {
    const r = scanForLeakedSecrets('token: github_pat_11ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890');
    expect(r.clean).toBe(false);
  });
  it('does not flag ordinary report text', () => {
    const r = scanForLeakedSecrets('{"status":"PASS","evidence":["investigation_id=AR-123-LINK"]}');
    expect(r.clean).toBe(true);
  });
});

describe('evaluateFrontend()', () => {
  it('FAILs when source could not be fetched at all', () => {
    const r = evaluateFrontend({ sourceText: null, commitSha: null, expectedWorkerUrl: 'https://x' });
    expect(r.status).toBe('NOT_VERIFIED');
    expect(r.live_http_verified).toBe(false);
  });
  it('PASSes when the worker URL and both endpoint families are referenced, no mock markers', () => {
    const src = `const WORKER_URL='https://pulseworker-v2.quiquandon.workers.dev'; fetch(WORKER_URL+'/api/learning/daily'); fetch(WORKER_URL+'/predict');`;
    const r = evaluateFrontend({ sourceText: src, commitSha: 'abc123', expectedWorkerUrl: 'https://pulseworker-v2.quiquandon.workers.dev' });
    expect(r.status).toBe('PASS');
    expect(r.worker_url_configured).toBe(true);
    expect(r.live_http_verified).toBe(false);
  });
  it('FAILs when a mock marker is present, even if everything else looks right', () => {
    const src = `const WORKER_URL='https://pulseworker-v2.quiquandon.workers.dev'; /* MOCK_RESPONSE */ fetch(WORKER_URL+'/api/learning/daily'); fetch(WORKER_URL+'/predict');`;
    const r = evaluateFrontend({ sourceText: src, commitSha: 'abc123', expectedWorkerUrl: 'https://pulseworker-v2.quiquandon.workers.dev' });
    expect(r.status).toBe('FAIL');
  });
  it('FAILs when the worker URL configured in the frontend does not match production', () => {
    const src = `const WORKER_URL='https://some-other-worker.workers.dev'; fetch(WORKER_URL+'/api/learning/daily'); fetch(WORKER_URL+'/predict');`;
    const r = evaluateFrontend({ sourceText: src, commitSha: 'abc123', expectedWorkerUrl: 'https://pulseworker-v2.quiquandon.workers.dev' });
    expect(r.status).toBe('FAIL');
    expect(r.worker_url_configured).toBe(false);
  });
});

describe('evaluateWorkerToD1()', () => {
  it('NOT_VERIFIED when database_id could not be read from source', () => {
    const r = evaluateWorkerToD1({ configuredDatabaseId: null, configuredDatabaseName: null, liveQueryOk: true, expectedDatabaseId: 'x' });
    expect(r.status).toBe('NOT_VERIFIED');
  });
  it('FAILs when the configured ID does not match the expected production database', () => {
    const r = evaluateWorkerToD1({ configuredDatabaseId: 'wrong-id', configuredDatabaseName: 'db', liveQueryOk: true, expectedDatabaseId: 'right-id' });
    expect(r.status).toBe('FAIL');
  });
  it('FAILs when the ID matches but the live read query did not succeed', () => {
    const r = evaluateWorkerToD1({ configuredDatabaseId: 'right-id', configuredDatabaseName: 'db', liveQueryOk: false, expectedDatabaseId: 'right-id' });
    expect(r.status).toBe('FAIL');
  });
  it('PASSes when the ID matches and a live SELECT actually succeeded', () => {
    const r = evaluateWorkerToD1({ configuredDatabaseId: 'right-id', configuredDatabaseName: 'db', liveQueryOk: true, expectedDatabaseId: 'right-id' });
    expect(r.status).toBe('PASS');
  });
});

describe('evaluatePrediction()', () => {
  it('NOT_VERIFIED when no row was found', () => {
    expect(evaluatePrediction({ row: null }).status).toBe('NOT_VERIFIED');
  });
  it('FAILs when model_version/git_commit_sha are missing', () => {
    const r = evaluatePrediction({ row: { id: 1, ts: 100, git_commit_sha: 'unknown', model_version: null } });
    expect(r.status).toBe('FAIL');
  });
  it('PASSes with real id/timestamp/sha/version present, independent of whether it matches the currently deployed SHA', () => {
    const r = evaluatePrediction({ row: { id: 1, ts: 100, git_commit_sha: 'abc123', model_version: 'knn-v3' }, deployedSha: 'differentSha' });
    expect(r.status).toBe('PASS');
    expect(r.evidence.some((e) => e.includes('matches_currently_deployed_sha=false'))).toBe(true);
  });
});

describe('evaluateAnalystRelay() — replaces the old automated-MI evaluateGemini entirely', () => {
  it('NOT_VERIFIED when there are zero relay submissions at all (no relay yet, not an app failure)', () => {
    const r = evaluateAnalystRelay({ allRelayEntries: [] });
    expect(r.status).toBe('NOT_VERIFIED');
  });
  it('FAILs when submissions exist but none processed cleanly', () => {
    const r = evaluateAnalystRelay({ allRelayEntries: [
      { relay_id: 'AR-2-LINK', submitted_ts: 200, raw_response_text: 'garbage', validation_status: 'malformed_response' },
      { relay_id: 'AR-1-LINK', submitted_ts: 100, raw_response_text: '', validation_status: 'error' },
    ] });
    expect(r.status).toBe('FAIL');
    expect(r.investigation_id).toBe('AR-2-LINK'); // latest, for the failure evidence
  });
  it('PASSes on validation_status=ok with non-empty raw_response_text', () => {
    const r = evaluateAnalystRelay({ allRelayEntries: [
      { relay_id: 'AR-2-LINK', submitted_ts: 200, raw_response_text: '{"catalysts":[]}', validation_status: 'ok' },
    ] });
    expect(r.status).toBe('PASS');
    expect(r.investigation_id).toBe('AR-2-LINK');
  });
  it('PASSes on validation_status=no_catalyst_found -- a legitimate clean outcome, not a parsing failure', () => {
    const r = evaluateAnalystRelay({ allRelayEntries: [
      { relay_id: 'AR-1-LINK', submitted_ts: 100, raw_response_text: '{"catalysts":[]}', validation_status: 'no_catalyst_found' },
    ] });
    expect(r.status).toBe('PASS');
  });
  it('FAILs on validation_status=ok if raw_response_text is somehow empty -- non-empty is a required condition, not just clean validation', () => {
    const r = evaluateAnalystRelay({ allRelayEntries: [
      { relay_id: 'AR-1-LINK', submitted_ts: 100, raw_response_text: '', validation_status: 'ok' },
    ] });
    expect(r.status).toBe('FAIL');
  });
  it('PASSes and returns the successful row even when it is not the newest attempt', () => {
    const r = evaluateAnalystRelay({ allRelayEntries: [
      { relay_id: 'AR-2-LINK', submitted_ts: 200, raw_response_text: 'x', validation_status: 'malformed_response' },
      { relay_id: 'AR-1-LINK', submitted_ts: 100, raw_response_text: '{"catalysts":[]}', validation_status: 'ok' },
    ] });
    expect(r.status).toBe('PASS');
    expect(r.investigation_id).toBe('AR-1-LINK');
  });
});

describe('evaluateRelaySubmission() — the "provider 200" equivalent', () => {
  it('NOT_VERIFIED when no matching row was found', () => {
    expect(evaluateRelaySubmission({ relayRow: null }).status).toBe('NOT_VERIFIED');
  });
  it('FAILs when submitted_ts is missing, even if a response exists', () => {
    const r = evaluateRelaySubmission({ relayRow: { relay_id: 'AR-1', submitted_ts: null, raw_response_text: '{"x":1}' } });
    expect(r.status).toBe('FAIL');
  });
  it('FAILs when raw_response_text is empty, even if submitted_ts exists', () => {
    const r = evaluateRelaySubmission({ relayRow: { relay_id: 'AR-1', submitted_ts: 100, raw_response_text: '' } });
    expect(r.status).toBe('FAIL');
  });
  it('PASSes only with both submitted_ts AND a non-empty response present', () => {
    const r = evaluateRelaySubmission({ relayRow: { relay_id: 'AR-1', submitted_ts: 100, raw_response_text: '{"catalysts":[]}' } });
    expect(r.status).toBe('PASS');
    expect(r.response_length).toBe('{"catalysts":[]}'.length);
  });
});

describe('evaluateContextHashIntegrity() — independent recomputation, not just presence', () => {
  it('NOT_VERIFIED when context_json or context_hash is missing', async () => {
    expect((await evaluateContextHashIntegrity({ contextJsonRaw: null, storedContextHash: 'abc' })).status).toBe('NOT_VERIFIED');
    expect((await evaluateContextHashIntegrity({ contextJsonRaw: '{}', storedContextHash: null })).status).toBe('NOT_VERIFIED');
  });
  it('FAILs on unparseable context_json rather than throwing', async () => {
    const r = await evaluateContextHashIntegrity({ contextJsonRaw: 'not json', storedContextHash: 'abc' });
    expect(r.status).toBe('FAIL');
  });
  it('PASSes when the recomputed SHA-256 matches the stored hash exactly, using the real canonical field set', async () => {
    const context = makeRealContext();
    const realHash = await realContextHash(context);
    const r = await evaluateContextHashIntegrity({ contextJsonRaw: JSON.stringify(context), storedContextHash: realHash });
    expect(r.status).toBe('PASS');
    expect(r.recomputed_hash).toBe(realHash);
  });
  it('FAILs when the stored hash does not match a fresh recomputation -- catches tampered or corrupted context_json', async () => {
    const context = makeRealContext();
    const r = await evaluateContextHashIntegrity({ contextJsonRaw: JSON.stringify(context), storedContextHash: 'deadbeef'.repeat(8) });
    expect(r.status).toBe('FAIL');
    expect(r.evidence.some((e) => e.includes('MISMATCH'))).toBe(true);
  });
  it('FAILs when context_json was hand-edited after the hash was computed, even a single field', async () => {
    const context = makeRealContext();
    const realHash = await realContextHash(context);
    const tampered = { ...context, correlatedFailureAssetCount: 999 }; // changed after hashing
    const r = await evaluateContextHashIntegrity({ contextJsonRaw: JSON.stringify(tampered), storedContextHash: realHash });
    expect(r.status).toBe('FAIL');
  });
});

describe('evaluateFactualContextParity() — the context contains real evidence, not a hollow shape', () => {
  it('NOT_VERIFIED when no context_json is available at all', () => {
    expect(evaluateFactualContextParity({ contextJsonRaw: null, primaryAsset: 'LINK' }).status).toBe('NOT_VERIFIED');
  });
  it('FAILs on unparseable context_json rather than throwing', () => {
    const r = evaluateFactualContextParity({ contextJsonRaw: 'not json', primaryAsset: 'LINK' });
    expect(r.status).toBe('FAIL');
  });
  it('FAILs when every asset is unavailable -- a hollow context with no real evidence', () => {
    const context = makeRealContext({ observations: {
      BTC: { available: false }, ETH: { available: false }, LINK: { available: false },
    } });
    const r = evaluateFactualContextParity({ contextJsonRaw: JSON.stringify(context), primaryAsset: 'LINK' });
    expect(r.status).toBe('FAIL');
    expect(r.assets_with_data).toBe(0);
  });
  it('FAILs when the primary asset specifically is unavailable, even if other assets have data', () => {
    const context = makeRealContext();
    context.observations.LINK.available = false;
    const r = evaluateFactualContextParity({ contextJsonRaw: JSON.stringify(context), primaryAsset: 'LINK' });
    expect(r.status).toBe('FAIL');
    expect(r.primary_asset_available).toBe(false);
  });
  it('PASSes with real, populated observations for the primary asset', () => {
    const context = makeRealContext();
    const r = evaluateFactualContextParity({ contextJsonRaw: JSON.stringify(context), primaryAsset: 'LINK' });
    expect(r.status).toBe('PASS');
    expect(r.assets_with_data).toBe(3);
    expect(r.primary_asset_available).toBe(true);
  });
});

describe('evaluateCatalystLedger() — invariant is conditional on validation_status, not a flat "rows required" rule', () => {
  it('NOT_VERIFIED with no investigation to trace', () => {
    expect(evaluateCatalystLedger({ investigationId: null, catalystRows: [], validationStatus: null }).status).toBe('NOT_VERIFIED');
  });

  describe('validation_status=ok', () => {
    it('FAILs when the investigation found a catalyst but no matching row was persisted', () => {
      const r = evaluateCatalystLedger({ investigationId: 'AR-1-LINK', catalystRows: [], validationStatus: 'ok' });
      expect(r.status).toBe('FAIL');
    });
    it('PASSes with a real matching row', () => {
      const r = evaluateCatalystLedger({ investigationId: 'AR-1-LINK', catalystRows: [{ id: 5, ts: 100, coin: 'LINK' }], validationStatus: 'ok' });
      expect(r.status).toBe('PASS');
      expect(r.record_id).toBe(5);
    });
  });

  describe('validation_status=no_catalyst_found — zero rows is the correct, expected outcome', () => {
    it('PASSes with zero matching rows (this is the real AR-1788163649576-LINK shape: no_catalyst_found + 0 rows)', () => {
      const r = evaluateCatalystLedger({ investigationId: 'AR-1788163649576-LINK', catalystRows: [], validationStatus: 'no_catalyst_found' });
      expect(r.status).toBe('PASS');
      expect(r.record_id).toBeNull();
      expect(r.evidence.join(' ')).toContain('no_catalyst_found');
    });
    it('FAILs (inconsistent) if a catalyst row exists anyway despite validation_status=no_catalyst_found', () => {
      const r = evaluateCatalystLedger({ investigationId: 'AR-1-LINK', catalystRows: [{ id: 9, ts: 100, coin: 'LINK' }], validationStatus: 'no_catalyst_found' });
      expect(r.status).toBe('FAIL');
      expect(r.evidence.join(' ')).toMatch(/inconsistent/i);
    });
  });

  it('an unrecognized/malformed validation_status defensively requires rows (same as ok), rather than silently passing -- this path is unreachable in practice per run-audit.js\'s gating, but must not silently pass if it ever is', () => {
    const r = evaluateCatalystLedger({ investigationId: 'AR-1-LINK', catalystRows: [], validationStatus: 'malformed_response' });
    expect(r.status).toBe('FAIL');
  });
});

describe('evaluateLearningLoop() — table existing is not evidence', () => {
  it('NOT_VERIFIED with no selection_decisions row found', () => {
    expect(evaluateLearningLoop({ selectionDecisionRow: null }).status).toBe('NOT_VERIFIED');
  });
  it('FAILs when the row exists but lacks a reason or score', () => {
    const r = evaluateLearningLoop({ selectionDecisionRow: { id: 1, chosen_variant: 'x', reason: null, lca_score: null } });
    expect(r.status).toBe('FAIL');
  });
  it('PASSes with a real reason and score present', () => {
    const r = evaluateLearningLoop({ selectionDecisionRow: { id: 1, chosen_variant: 'challenger_calibrated', reason: 'locally outperformed', lca_score: 0.9, cleared_gate: 1 } });
    expect(r.status).toBe('PASS');
  });
});

describe('evaluateRelayBudget() — Analyst Relay is unbudgeted by design, always an informational PASS', () => {
  it('always PASSes and reports unbudgeted-by-design, never reads or compares against the old MAX_GEMINI_INVESTIGATIONS_PER_DAY config', () => {
    const r = evaluateRelayBudget();
    expect(r.status).toBe('PASS');
    expect(r.budgeted).toBe(false);
    expect(r.evidence.join(' ')).toContain('unbudgeted by design');
  });
});

describe('evaluateSafety()', () => {
  it('PASSes only when nothing was written and nothing leaked', () => {
    expect(evaluateSafety({ productionWritesPerformed: false, secretsExposed: false }).status).toBe('PASS');
  });
  it('FAILs if a production write occurred', () => {
    expect(evaluateSafety({ productionWritesPerformed: true, secretsExposed: false }).status).toBe('FAIL');
  });
  it('FAILs if a secret was exposed, even with no writes', () => {
    expect(evaluateSafety({ productionWritesPerformed: false, secretsExposed: true }).status).toBe('FAIL');
  });
});

describe('classifyFailure() — grounding removed entirely, new context-integrity classifications added', () => {
  const pass = { status: 'PASS' };

  it('classifies NO_TRIGGER when analystRelay itself is NOT_VERIFIED', () => {
    const r = classifyFailure({ analystRelay: { status: 'NOT_VERIFIED', evidence: ['no submissions'] }, relaySubmission: {}, contextHashIntegrity: {}, factualContextParity: {} });
    expect(r.classification).toBe('NO_TRIGGER');
  });
  it('classifies RELAY_SUBMISSION_UNCLEAN when analystRelay FAILed (not NOT_VERIFIED)', () => {
    const r = classifyFailure({ analystRelay: { status: 'FAIL', evidence: ['malformed'] }, relaySubmission: {}, contextHashIntegrity: {}, factualContextParity: {} });
    expect(r.classification).toBe('RELAY_SUBMISSION_UNCLEAN');
  });
  it('classifies CONTEXT_HASH_MISMATCH when analystRelay+relaySubmission PASS but the hash does not', () => {
    const r = classifyFailure({ analystRelay: pass, relaySubmission: pass, contextHashIntegrity: { status: 'FAIL', evidence: ['mismatch'] }, factualContextParity: {} });
    expect(r.classification).toBe('CONTEXT_HASH_MISMATCH');
  });
  it('classifies CONTEXT_FACTUAL_PARITY_FAILURE when everything upstream PASSes but the context is hollow', () => {
    const r = classifyFailure({ analystRelay: pass, relaySubmission: pass, contextHashIntegrity: pass, factualContextParity: { status: 'FAIL', evidence: ['empty'] } });
    expect(r.classification).toBe('CONTEXT_FACTUAL_PARITY_FAILURE');
  });
  it('classifies nothing (null) when all four PASS', () => {
    const r = classifyFailure({ analystRelay: pass, relaySubmission: pass, contextHashIntegrity: pass, factualContextParity: pass });
    expect(r.classification).toBeNull();
  });
});

describe('computeEndToEnd() — the final verdict, grounding removed from the gate', () => {
  const allPass = () => ({
    frontend: { status: 'PASS' }, workerToD1: { status: 'PASS' }, prediction: { status: 'PASS' },
    analystRelay: { status: 'PASS' }, relaySubmission: { status: 'PASS' },
    contextHashIntegrity: { status: 'PASS' }, factualContextParity: { status: 'PASS' },
    catalystLedger: { status: 'PASS' }, learningLoop: { status: 'PASS' }, safety: { status: 'PASS' },
  });

  it('GREEN only when every one of the ten links is PASS', () => {
    expect(computeEndToEnd(allPass()).status).toBe('GREEN');
  });

  it('YELLOW (not RED) when analystRelay has simply never fired (NOT_VERIFIED) -- absence of a submission is not a defect', () => {
    const input = { ...allPass(), analystRelay: { status: 'NOT_VERIFIED', evidence: ['no submissions'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('YELLOW');
    expect(r.blocking_reason).toContain('NO_TRIGGER');
  });

  it('YELLOW when the only failure is an unclean relay submission -- a human-input issue, not a proven codebase defect', () => {
    const input = { ...allPass(), analystRelay: { status: 'FAIL', evidence: ['malformed paste'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('YELLOW');
    expect(r.blocking_reason).toContain('RELAY_SUBMISSION_UNCLEAN');
  });

  it('RED when context_hash_integrity fails despite a real successful relay submission -- a proven internal-consistency defect', () => {
    const input = { ...allPass(), contextHashIntegrity: { status: 'FAIL', evidence: ['hash mismatch'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('RED');
    expect(r.blocking_reason).toContain('CONTEXT_HASH_MISMATCH');
  });

  it('RED when factual_context_parity fails despite everything upstream PASSing -- a hollow context is a proven defect', () => {
    const input = { ...allPass(), factualContextParity: { status: 'FAIL', evidence: ['no observations'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('RED');
    expect(r.blocking_reason).toContain('CONTEXT_FACTUAL_PARITY_FAILURE');
  });

  it('RED when the learning loop PASSED analystRelay but the catalyst persistence step demonstrably failed (a real, observed defect)', () => {
    const input = { ...allPass(), catalystLedger: { status: 'FAIL', evidence: ['no matching row despite successful relay'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('RED');
  });

  it('RED unconditionally on a safety violation, regardless of every other link', () => {
    const input = { ...allPass(), safety: { status: 'FAIL', evidence: ['secret leaked'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('RED');
    expect(r.blocking_reason).toContain('Safety violation');
  });

  it('no reference to grounding anywhere in a GREEN result -- confirms it is not silently still required', () => {
    const r = computeEndToEnd(allPass());
    expect(JSON.stringify(r)).not.toContain('grounding');
    expect(JSON.stringify(r)).not.toContain('GROUNDING');
  });
});
