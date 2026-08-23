import { describe, it, expect } from 'vitest';
import {
  STATUS, section, redactSecrets, scanForLeakedSecrets,
  evaluateFrontend, evaluateWorkerToD1, evaluatePrediction, evaluateGemini,
  evaluateProviderCall, evaluateGrounding, evaluateCatalystLedger, evaluateLearningLoop,
  evaluateBudget, evaluateSafety, classifyFailure, computeEndToEnd,
} from './lib.js';

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
    const r = scanForLeakedSecrets('{"status":"PASS","evidence":["investigation_id=MI-123-BTC"]}');
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
    expect(r.live_http_verified).toBe(false); // always false -- this audit never makes a live frontend HTTP request
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

describe('evaluateGemini() — real success only, per the "no tests/fixtures/dry runs/mocked/failed" exclusion', () => {
  it('NOT_VERIFIED when there are zero investigation attempts at all (no trigger, not an app failure)', () => {
    const r = evaluateGemini({ allInvestigations: [] });
    expect(r.status).toBe('NOT_VERIFIED');
  });
  it('FAILs when attempts exist but none succeeded', () => {
    const r = evaluateGemini({ allInvestigations: [
      { investigation_id: 'MI-2-BTC', request_ts: 200, response_status: 'rate_limited' },
      { investigation_id: 'MI-1-BTC', request_ts: 100, response_status: 'rate_limited' },
    ] });
    expect(r.status).toBe('FAIL');
    expect(r.response_status).toBe('rate_limited');
    expect(r.investigation_id).toBe('MI-2-BTC'); // the latest one, for the failure evidence
  });
  it('PASSes and returns the successful row even when it is not the newest attempt', () => {
    const r = evaluateGemini({ allInvestigations: [
      { investigation_id: 'MI-2-BTC', request_ts: 200, response_status: 'rate_limited' },
      { investigation_id: 'MI-1-BTC', request_ts: 100, response_status: 'ok' },
    ] });
    expect(r.status).toBe('PASS');
    expect(r.investigation_id).toBe('MI-1-BTC');
  });
});

describe('evaluateProviderCall()', () => {
  it('NOT_VERIFIED when no matching row was found', () => {
    expect(evaluateProviderCall({ providerCallRow: null }).status).toBe('NOT_VERIFIED');
  });
  it('FAILs on any non-200 http_status, even if response_status looks benign', () => {
    const r = evaluateProviderCall({ providerCallRow: { http_status: 429, provider: 'google', model: 'gemini-3.6-flash', request_ts: 1, quota_decision: 'admitted', response_status: 'rate_limited' } });
    expect(r.status).toBe('FAIL');
  });
  it('PASSes only on exactly http_status 200', () => {
    const r = evaluateProviderCall({ providerCallRow: { http_status: 200, provider: 'google', model: 'gemini-3.6-flash', request_ts: 1, quota_decision: 'admitted', response_status: 'ok' } });
    expect(r.status).toBe('PASS');
  });
});

describe('evaluateGrounding() — the hard requirement, rejects every empty-shell shape named in the spec', () => {
  it('NOT_VERIFIED when no metadata is available at all', () => {
    expect(evaluateGrounding({ groundingMetadataJson: null }).status).toBe('NOT_VERIFIED');
  });
  it('FAILs on the empty-arrays shape {"searchQueries":[],"groundedSources":[]}', () => {
    const r = evaluateGrounding({ groundingMetadataJson: JSON.stringify({ searchQueries: [], groundedSources: [] }) });
    expect(r.status).toBe('FAIL');
  });
  it('FAILs on a bare empty object {}', () => {
    const r = evaluateGrounding({ groundingMetadataJson: '{}' });
    expect(r.status).toBe('FAIL');
  });
  it('FAILs on malformed JSON rather than throwing', () => {
    const r = evaluateGrounding({ groundingMetadataJson: 'not json' });
    expect(r.status).toBe('FAIL');
  });
  it('PASSes only with real, non-empty search queries AND grounded sources', () => {
    const r = evaluateGrounding({ groundingMetadataJson: JSON.stringify({ searchQueries: ['BTC price today'], groundedSources: [{ url: 'https://example.com', title: 't' }] }) });
    expect(r.status).toBe('PASS');
    expect(r.source_count).toBe(1);
  });
  it('FAILs when only one of the two arrays is populated', () => {
    const r = evaluateGrounding({ groundingMetadataJson: JSON.stringify({ searchQueries: ['q'], groundedSources: [] }) });
    expect(r.status).toBe('FAIL');
  });
});

describe('evaluateCatalystLedger() — table existing is not evidence', () => {
  it('NOT_VERIFIED with no investigation to trace', () => {
    expect(evaluateCatalystLedger({ investigationId: null, catalystRows: [] }).status).toBe('NOT_VERIFIED');
  });
  it('FAILs when the investigation succeeded but no catalyst rows reference it', () => {
    const r = evaluateCatalystLedger({ investigationId: 'MI-1-BTC', catalystRows: [] });
    expect(r.status).toBe('FAIL');
  });
  it('PASSes with a real matching row', () => {
    const r = evaluateCatalystLedger({ investigationId: 'MI-1-BTC', catalystRows: [{ id: 5, ts: 100, coin: 'BTC' }] });
    expect(r.status).toBe('PASS');
    expect(r.record_id).toBe(5);
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

describe('evaluateBudget() — reports only, never changes anything', () => {
  it('NOT_VERIFIED when config could not be read from source', () => {
    expect(evaluateBudget({ configuredDaily: null, configuredHourly: null }).status).toBe('NOT_VERIFIED');
  });
  it('FAILs (as documented) when production is still the canary values 1/1 against a 5/1 requirement', () => {
    const r = evaluateBudget({ configuredDaily: 1, configuredHourly: 1, requiredDaily: 5, requiredHourly: 1 });
    expect(r.status).toBe('FAIL');
    expect(r.configured_daily).toBe(1);
    expect(r.required_daily).toBe(5);
  });
  it('PASSes when configured matches required exactly', () => {
    const r = evaluateBudget({ configuredDaily: 5, configuredHourly: 1, requiredDaily: 5, requiredHourly: 1 });
    expect(r.status).toBe('PASS');
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

describe('classifyFailure()', () => {
  const passGrounding = { status: 'PASS' };
  const passProvider = { status: 'PASS' };

  it('classifies NO_TRIGGER when gemini itself is NOT_VERIFIED', () => {
    const r = classifyFailure({ gemini: { status: 'NOT_VERIFIED', evidence: ['no attempts'] }, providerCall: {}, grounding: {} });
    expect(r.classification).toBe('NO_TRIGGER');
  });
  it('classifies PROVIDER_RATE_LIMIT when the failed gemini attempt was rate_limited', () => {
    const r = classifyFailure({ gemini: { status: 'FAIL', response_status: 'rate_limited', evidence: [] }, providerCall: {}, grounding: {} });
    expect(r.classification).toBe('PROVIDER_RATE_LIMIT');
  });
  it('classifies APPLICATION_BUDGET when the failed gemini attempt was quota_deferred', () => {
    const r = classifyFailure({ gemini: { status: 'FAIL', response_status: 'quota_deferred', evidence: [] }, providerCall: {}, grounding: {} });
    expect(r.classification).toBe('APPLICATION_BUDGET');
  });
  it('classifies GROUNDING_FAILURE when gemini+provider PASS but grounding does not', () => {
    const r = classifyFailure({ gemini: { status: 'PASS' }, providerCall: passProvider, grounding: { status: 'FAIL', evidence: ['empty'] } });
    expect(r.classification).toBe('GROUNDING_FAILURE');
  });
  it('classifies nothing (null) when gemini+provider+grounding all PASS', () => {
    const r = classifyFailure({ gemini: { status: 'PASS' }, providerCall: passProvider, grounding: passGrounding });
    expect(r.classification).toBeNull();
  });
});

describe('computeEndToEnd() — the final verdict', () => {
  const allPass = () => ({
    frontend: { status: 'PASS' }, workerToD1: { status: 'PASS' }, prediction: { status: 'PASS' },
    gemini: { status: 'PASS' }, providerCall: { status: 'PASS' }, grounding: { status: 'PASS' },
    catalystLedger: { status: 'PASS' }, learningLoop: { status: 'PASS' }, safety: { status: 'PASS' },
  });

  it('GREEN only when every one of the nine links is PASS', () => {
    expect(computeEndToEnd(allPass()).status).toBe('GREEN');
  });

  it('YELLOW (not RED) when gemini has simply never fired (NOT_VERIFIED) -- absence of a trigger is not a defect', () => {
    const input = { ...allPass(), gemini: { status: 'NOT_VERIFIED', evidence: ['no attempts'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('YELLOW');
    expect(r.blocking_reason).toContain('NO_TRIGGER');
  });

  it('YELLOW (not RED) when the only failure is provider rate limiting -- not a codebase defect', () => {
    const input = { ...allPass(), gemini: { status: 'FAIL', response_status: 'rate_limited', evidence: ['429'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('YELLOW');
    expect(r.blocking_reason).toContain('PROVIDER_RATE_LIMIT');
  });

  it('YELLOW when the only failure is our own application budget deferring the call', () => {
    const input = { ...allPass(), gemini: { status: 'FAIL', response_status: 'quota_deferred', evidence: ['daily_limit_reached'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('YELLOW');
    expect(r.blocking_reason).toContain('APPLICATION_BUDGET');
  });

  it('RED when grounding metadata is empty despite a real 200 -- this application always requests grounding for this call, so an ungrounded success is a proven defect, per section 13', () => {
    const input = { ...allPass(), grounding: { status: 'FAIL', evidence: ['empty'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('RED');
    expect(r.blocking_reason).toContain('GROUNDING_FAILURE');
  });

  it('RED when the learning loop PASSED gemini/grounding but the selection/catalyst persistence step demonstrably failed (a real, observed defect)', () => {
    const input = { ...allPass(), catalystLedger: { status: 'FAIL', evidence: ['no matching row despite successful investigation'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('RED');
  });

  it('RED unconditionally on a safety violation, regardless of every other link', () => {
    const input = { ...allPass(), safety: { status: 'FAIL', evidence: ['secret leaked'] } };
    const r = computeEndToEnd(input);
    expect(r.status).toBe('RED');
    expect(r.blocking_reason).toContain('Safety violation');
  });
});
