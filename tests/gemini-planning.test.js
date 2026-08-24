import { describe, it, expect, beforeAll, afterEach } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

// These tests cover ONLY the deterministic, side-effect-free pieces of the
// planned Gemini Market Intelligence integration -- see
// .ai/GEMINI_MARKET_INTELLIGENCE.md and learning/GEMINI_IMPLEMENTATION_PLAN.md.
// None of this code is wired into any route or cron; these tests protect
// the logic that WILL be wired in, once a separate PR does that.

describe('Gemini planning — shouldTriggerInvestigation', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('shouldTriggerInvestigation') + '\n\n' + extractConstants('GEMINI_TRIGGER_CONFIG');
    scope = evalInScope(src);
  });

  it('does not trigger when no signal crosses its threshold', () => {
    const result = scope.shouldTriggerInvestigation({ priceMovePct: 1, highConfidenceFailureConfidence: 0.6, correlatedFailureAssetCount: 1 });
    expect(result.trigger).toBe(false);
    expect(result.reasons).toEqual([]);
  });

  it('triggers on a large price move in either direction', () => {
    expect(scope.shouldTriggerInvestigation({ priceMovePct: 4 }).trigger).toBe(true);
    expect(scope.shouldTriggerInvestigation({ priceMovePct: -4 }).trigger).toBe(true);
    expect(scope.shouldTriggerInvestigation({ priceMovePct: 2.9 }).trigger).toBe(false);
  });

  it('triggers on a high-confidence failure at or above the configured threshold', () => {
    const config = { ...scope.GEMINI_TRIGGER_CONFIG, HIGH_CONFIDENCE_TRIGGER: 0.85 };
    expect(scope.shouldTriggerInvestigation({ highConfidenceFailureConfidence: 0.85 }, config).trigger).toBe(true);
    expect(scope.shouldTriggerInvestigation({ highConfidenceFailureConfidence: 0.84 }, config).trigger).toBe(false);
  });

  it('triggers on correlated multi-asset failures', () => {
    const config = { ...scope.GEMINI_TRIGGER_CONFIG, MULTI_ASSET_TRIGGER_COUNT: 3 };
    expect(scope.shouldTriggerInvestigation({ correlatedFailureAssetCount: 3 }, config).trigger).toBe(true);
    expect(scope.shouldTriggerInvestigation({ correlatedFailureAssetCount: 2 }, config).trigger).toBe(false);
  });

  it('reports every reason that fired, not just the first', () => {
    const result = scope.shouldTriggerInvestigation({ priceMovePct: 5, highConfidenceFailureConfidence: 0.9 }, scope.GEMINI_TRIGGER_CONFIG);
    expect(result.reasons.length).toBe(2);
  });

  it('a config with a custom (e.g. tightened) threshold is respected instead of the default', () => {
    const strictConfig = { ...scope.GEMINI_TRIGGER_CONFIG, HIGH_CONFIDENCE_TRIGGER: 0.95 };
    expect(scope.shouldTriggerInvestigation({ highConfidenceFailureConfidence: 0.9 }, strictConfig).trigger).toBe(false);
  });
});

describe('Gemini planning — withinGeminiRateLimit', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('withinGeminiRateLimit') + '\n\n' + extractConstants('GEMINI_TRIGGER_CONFIG');
    scope = evalInScope(src);
  });

  it('allows when under both daily and hourly limits', () => {
    // Explicit config (not the bare default) so this test's meaning doesn't
    // silently change if GEMINI_TRIGGER_CONFIG's live values change (e.g.
    // the canary's temporarily-tightened 1/1 budget).
    const config = { ...scope.GEMINI_TRIGGER_CONFIG, MAX_GEMINI_INVESTIGATIONS_PER_DAY: 8, MAX_GEMINI_INVESTIGATIONS_PER_HOUR: 2 };
    const result = scope.withinGeminiRateLimit({ investigationsToday: 1, investigationsThisHour: 0 }, config);
    expect(result.allowed).toBe(true);
  });

  it('blocks at the daily limit even if the hourly count is fine', () => {
    const config = { ...scope.GEMINI_TRIGGER_CONFIG, MAX_GEMINI_INVESTIGATIONS_PER_DAY: 8 };
    const result = scope.withinGeminiRateLimit({ investigationsToday: 8, investigationsThisHour: 0 }, config);
    expect(result.allowed).toBe(false);
    expect(result.reason).toBe('daily_limit_reached');
  });

  it('blocks at the hourly limit even if the daily count is fine', () => {
    const config = { ...scope.GEMINI_TRIGGER_CONFIG, MAX_GEMINI_INVESTIGATIONS_PER_HOUR: 2 };
    const result = scope.withinGeminiRateLimit({ investigationsToday: 0, investigationsThisHour: 2 }, config);
    expect(result.allowed).toBe(false);
    expect(result.reason).toBe('hourly_limit_reached');
  });
});

describe('Gemini planning — computeInvestigationPriority (worked examples from PR #2 review)', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('computeInvestigationPriority') + '\n\n' + extractConstants('INVESTIGATION_PRIORITY_WEIGHTS');
    scope = evalInScope(src);
  });

  // These three mirror learning/GEMINI_IMPLEMENTATION_PLAN.md's worked
  // examples A/B/C exactly -- the doc and this test must be kept in sync.

  it('Example A — high confidence (0.90), correct call, small market move: LOW priority', () => {
    const score = scope.computeInvestigationPriority({
      priceMovePct: 0.4, wasWrong: false, confidence: 0.90,
      correlatedFailureAssetCount: 0, isVolatilityAnomaly: false, recentFailureCount: 0, isRegimeChange: false,
    });
    expect(score).toBeLessThan(4); // INVESTIGATION_PRIORITY_THRESHOLD
  });

  it('Example B — moderate confidence (0.65), wrong, extreme single-asset move (8%): HIGH priority', () => {
    const score = scope.computeInvestigationPriority({
      priceMovePct: 8, wasWrong: true, confidence: 0.65,
      correlatedFailureAssetCount: 0, isVolatilityAnomaly: true, recentFailureCount: 0, isRegimeChange: false,
    });
    expect(score).toBeGreaterThanOrEqual(4);
  });

  it('Example C — correlated multi-asset failure (BTC 8%, ETH 9%, LINK 12%): HIGHEST priority, exceeds B', () => {
    const scoreB = scope.computeInvestigationPriority({
      priceMovePct: 8, wasWrong: true, confidence: 0.65,
      correlatedFailureAssetCount: 0, isVolatilityAnomaly: true, recentFailureCount: 0, isRegimeChange: false,
    });
    const scoreC = scope.computeInvestigationPriority({
      priceMovePct: 12, wasWrong: true, confidence: 0.65,
      correlatedFailureAssetCount: 3, isVolatilityAnomaly: true, recentFailureCount: 0, isRegimeChange: true,
    });
    expect(scoreC).toBeGreaterThanOrEqual(4);
    expect(scoreC).toBeGreaterThan(scoreB); // correlation across assets outweighs a single large move
  });

  it('a correct call contributes zero confidence-adjusted error regardless of how confident it was', () => {
    const veryConfidentCorrect = scope.computeInvestigationPriority({ priceMovePct: 0, wasWrong: false, confidence: 0.99 });
    expect(veryConfidentCorrect).toBe(0);
  });

  it('prediction confidence alone (without an error) never drives priority -- the core PR #2 review distinction', () => {
    const highConfidenceCorrect = scope.computeInvestigationPriority({ priceMovePct: 1, wasWrong: false, confidence: 0.95 });
    const lowConfidenceCorrect = scope.computeInvestigationPriority({ priceMovePct: 1, wasWrong: false, confidence: 0.55 });
    expect(highConfidenceCorrect).toBe(lowConfidenceCorrect); // confidence is irrelevant when there's no error
  });

  it('a wrong higher-confidence call scores higher than a wrong lower-confidence call, all else equal', () => {
    const wrongHighConf = scope.computeInvestigationPriority({ priceMovePct: 1, wasWrong: true, confidence: 0.9 });
    const wrongLowConf = scope.computeInvestigationPriority({ priceMovePct: 1, wasWrong: true, confidence: 0.55 });
    expect(wrongHighConf).toBeGreaterThan(wrongLowConf);
  });
});

describe('Gemini planning — isHighInvestigationPriority', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('isHighInvestigationPriority') + '\n\n' + extractConstants('INVESTIGATION_PRIORITY_THRESHOLD');
    scope = evalInScope(src);
  });

  it('classifies below-threshold scores as not high priority', () => {
    expect(scope.isHighInvestigationPriority(3.9)).toBe(false);
  });

  it('classifies at-or-above-threshold scores as high priority', () => {
    expect(scope.isHighInvestigationPriority(4)).toBe(true);
    expect(scope.isHighInvestigationPriority(10)).toBe(true);
  });

  it('respects a custom threshold', () => {
    expect(scope.isHighInvestigationPriority(5, 10)).toBe(false);
  });
});

describe('Gemini planning — rankInvestigationCandidates + selectWithinBudget', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('rankInvestigationCandidates', 'computeInvestigationPriority', 'selectWithinBudget')
      + '\n\n' + extractConstants('INVESTIGATION_PRIORITY_WEIGHTS', 'INVESTIGATION_PRIORITY_THRESHOLD');
    scope = evalInScope(src);
  });

  const candidateA = { id: 'A', signals: { priceMovePct: 0.4, wasWrong: false, confidence: 0.90 } };
  const candidateB = { id: 'B', signals: { priceMovePct: 8, wasWrong: true, confidence: 0.65, isVolatilityAnomaly: true } };
  const candidateC = { id: 'C', signals: { priceMovePct: 12, wasWrong: true, confidence: 0.65, correlatedFailureAssetCount: 3, isVolatilityAnomaly: true, isRegimeChange: true } };

  it('ranks candidates highest-priority first: C > B > A', () => {
    const ranked = scope.rankInvestigationCandidates([candidateA, candidateB, candidateC]);
    expect(ranked.map(c => c.id)).toEqual(['C', 'B', 'A']);
  });

  it('selects only what fits the budget, deferring the rest', () => {
    const ranked = scope.rankInvestigationCandidates([candidateA, candidateB, candidateC]);
    const { selected, deferred } = scope.selectWithinBudget(ranked, 1);
    expect(selected.map(c => c.id)).toEqual(['C']); // highest priority takes the one slot
    expect(deferred.map(c => c.id)).toContain('B');
  });

  it('never selects a below-threshold candidate even with unlimited budget', () => {
    const ranked = scope.rankInvestigationCandidates([candidateA, candidateB, candidateC]);
    const { selected, deferred } = scope.selectWithinBudget(ranked, 99);
    expect(selected.map(c => c.id)).not.toContain('A'); // A is below INVESTIGATION_PRIORITY_THRESHOLD
    expect(deferred.map(c => c.id)).toContain('A');
  });

  it('selects nothing when budget is zero, regardless of priority', () => {
    const ranked = scope.rankInvestigationCandidates([candidateB, candidateC]);
    const { selected } = scope.selectWithinBudget(ranked, 0);
    expect(selected).toEqual([]);
  });
});

describe('Gemini planning — remainingGeminiBudget', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('remainingGeminiBudget') + '\n\n' + extractConstants('GEMINI_TRIGGER_CONFIG');
    scope = evalInScope(src);
  });

  it('is the SMALLER of daily-remaining and hourly-remaining, not their sum', () => {
    const config = { MAX_GEMINI_INVESTIGATIONS_PER_DAY: 8, MAX_GEMINI_INVESTIGATIONS_PER_HOUR: 2 };
    // 7 remaining today, but only 1 remaining this hour -- budget is 1
    const result = scope.remainingGeminiBudget({ investigationsToday: 1, investigationsThisHour: 1 }, config);
    expect(result).toBe(1);
  });

  it('never goes negative once a limit is exceeded', () => {
    const config = { MAX_GEMINI_INVESTIGATIONS_PER_DAY: 8, MAX_GEMINI_INVESTIGATIONS_PER_HOUR: 2 };
    const result = scope.remainingGeminiBudget({ investigationsToday: 20, investigationsThisHour: 20 }, config);
    expect(result).toBe(0);
  });
});

describe('Gemini planning — computeAvailableBeforePrediction (three-state contract)', () => {
  let scope;

  beforeAll(() => {
    scope = evalInScope(extractFunctions('computeAvailableBeforePrediction'));
  });

  it('true when first_public_timestamp is at or before prediction_timestamp', () => {
    expect(scope.computeAvailableBeforePrediction(1000, 2000)).toBe(true);
    expect(scope.computeAvailableBeforePrediction(2000, 2000)).toBe(true); // T1 <= T0 boundary
  });

  it('false when first_public_timestamp is after prediction_timestamp -- never lets hindsight in', () => {
    expect(scope.computeAvailableBeforePrediction(3000, 2000)).toBe(false);
  });

  it('the string "unknown", not null/undefined, when first_public_timestamp is not established', () => {
    expect(scope.computeAvailableBeforePrediction(null, 2000)).toBe('unknown');
    expect(scope.computeAvailableBeforePrediction(undefined, 2000)).toBe('unknown');
  });

  it('never uses event_timestamp -- only takes two arguments, so it structurally cannot', () => {
    expect(scope.computeAvailableBeforePrediction.length).toBe(2);
  });
});

describe('Gemini planning — validateCatalystPayload', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('validateCatalystPayload') + '\n\n'
      + extractConstants('ALLOWED_CATALYST_CATEGORIES', 'ALLOWED_MARKET_CLASSIFICATIONS');
    scope = evalInScope(src);
  });

  const base = { coin: 'BTC', category: 'MACRO' };

  it('accepts a minimal valid payload', () => {
    expect(scope.validateCatalystPayload(base).valid).toBe(true);
  });

  it('rejects a missing coin', () => {
    const result = scope.validateCatalystPayload({ category: 'MACRO' });
    expect(result.valid).toBe(false);
    expect(result.errors).toContain('missing coin');
  });

  it('rejects a category outside the allowed list -- catches a Gemini hallucinated category', () => {
    const result = scope.validateCatalystPayload({ coin: 'BTC', category: 'MOON_LANDING' });
    expect(result.valid).toBe(false);
    expect(result.errors.some(e => e.includes('invalid category'))).toBe(true);
  });

  it('rejects an invalid market_classification', () => {
    const result = scope.validateCatalystPayload({ ...base, marketClassification: 'VERY_WIDE' });
    expect(result.valid).toBe(false);
  });

  it('accepts every allowed category and market classification', () => {
    for (const category of scope.ALLOWED_CATALYST_CATEGORIES) {
      expect(scope.validateCatalystPayload({ coin: 'BTC', category }).valid).toBe(true);
    }
    for (const marketClassification of scope.ALLOWED_MARKET_CLASSIFICATIONS) {
      expect(scope.validateCatalystPayload({ ...base, marketClassification }).valid).toBe(true);
    }
  });

  it('rejects a malformed source URL', () => {
    const result = scope.validateCatalystPayload({ ...base, sourceUrl: 'not-a-url' });
    expect(result.valid).toBe(false);
  });

  it('accepts a well-formed https source URL', () => {
    expect(scope.validateCatalystPayload({ ...base, sourceUrl: 'https://example.com/article' }).valid).toBe(true);
  });

  it('rejects first_public_timestamp implausibly before event_timestamp (impossible ordering)', () => {
    const result = scope.validateCatalystPayload({ ...base, eventTimestamp: 100_000_000, firstPublicTimestamp: 1000 });
    expect(result.valid).toBe(false);
    expect(result.errors.some(e => e.includes('first_public_timestamp'))).toBe(true);
  });

  it('allows first_public_timestamp slightly before event_timestamp within clock-skew tolerance', () => {
    const eventTs = 100_000_000;
    const result = scope.validateCatalystPayload({ ...base, eventTimestamp: eventTs, firstPublicTimestamp: eventTs - 1000 });
    expect(result.valid).toBe(true);
  });

  it('rejects discovery_timestamp implausibly before first_public_timestamp', () => {
    const result = scope.validateCatalystPayload({ ...base, firstPublicTimestamp: 100_000_000, discoveryTimestamp: 1000 });
    expect(result.valid).toBe(false);
    expect(result.errors.some(e => e.includes('discovery_timestamp'))).toBe(true);
  });

  it('does not require optional timestamp fields to be present', () => {
    expect(scope.validateCatalystPayload(base).valid).toBe(true);
  });
});

describe('Gemini planning — isDuplicateCatalyst', () => {
  let scope;

  beforeAll(() => {
    scope = evalInScope(extractFunctions('isDuplicateCatalyst'));
  });

  it('flags a same-coin, same-category catalyst within the tolerance window as a duplicate', () => {
    const existing = [{ coin: 'BTC', category: 'MACRO', ts: 1_000_000 }];
    const candidate = { coin: 'BTC', category: 'MACRO', ts: 1_000_000 + 60000 };
    expect(scope.isDuplicateCatalyst(candidate, existing, 6 * 3600000)).toBe(true);
  });

  it('does not flag a different category as a duplicate, even at the same time', () => {
    const existing = [{ coin: 'BTC', category: 'MACRO', ts: 1_000_000 }];
    const candidate = { coin: 'BTC', category: 'REGULATION', ts: 1_000_000 };
    expect(scope.isDuplicateCatalyst(candidate, existing, 6 * 3600000)).toBe(false);
  });

  it('does not flag a different asset as a duplicate -- one market event can affect several assets as separate rows only when genuinely asset-specific', () => {
    const existing = [{ coin: 'BTC', category: 'MACRO', ts: 1_000_000 }];
    const candidate = { coin: 'ETH', category: 'MACRO', ts: 1_000_000 };
    expect(scope.isDuplicateCatalyst(candidate, existing, 6 * 3600000)).toBe(false);
  });

  it('does not flag an event outside the tolerance window', () => {
    const existing = [{ coin: 'BTC', category: 'MACRO', ts: 1_000_000 }];
    const candidate = { coin: 'BTC', category: 'MACRO', ts: 1_000_000 + 7 * 3600000 };
    expect(scope.isDuplicateCatalyst(candidate, existing, 6 * 3600000)).toBe(false);
  });

  it('returns false against an empty existing-catalyst list', () => {
    expect(scope.isDuplicateCatalyst({ coin: 'BTC', category: 'MACRO', ts: 1000 }, [], 3600000)).toBe(false);
  });
});

// =====================================================================
// LIVE implementation tests -- investigateMarketEvent and its direct
// dependencies. These mock fetch() and D1 (env.DB) since the functions
// under test are no longer pure. Per ChatGPT's specific audit focus: the
// Gemini prompt/grounding request shape, source handling, D1 writes,
// timestamp enforcement, and whether the scheduled job can call Gemini
// excessively.
// =====================================================================

// quotaAdmitted controls whether the shared-ledger reservation
// (reserveGeminiQuotaSlot's UPDATE ... RETURNING statements) succeeds --
// defaults to true so pre-existing tests exercising the happy Gemini-call
// path don't need to know the quota mechanism exists at all. Set
// quotaAdmitted: false to simulate the reservation being rejected (see the
// dedicated deferred-quota tests below).
function makeFakeDb({ existingCatalysts = [], quotaAdmitted = true } = {}) {
  const inserts = [];
  const selects = [];
  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async all() {
              selects.push({ sql, args });
              if (/FROM coin_catalyst_log/i.test(sql)) return { results: existingCatalysts };
              // The atomic reservation statement in reserveGeminiQuotaSlot --
              // returns a row (admission granted) or an empty array
              // (rejected) depending on quotaAdmitted.
              if (/UPDATE gemini_quota_ledger SET reserved = reserved \+ 1/i.test(sql)) {
                return quotaAdmitted ? { results: [{ reserved: 1 }] } : { results: [] };
              }
              return { results: [] };
            },
            async first() {
              return null;
            },
            async run() {
              const table = /INSERT INTO (\w+)/i.exec(sql)?.[1] || /UPDATE (\w+)/i.exec(sql)?.[1] || 'unknown';
              const row = { table, sql, args };
              inserts.push(row);
              return { meta: { last_row_id: inserts.length } };
            },
          };
        },
      };
    },
    async batch(stmts) {
      // Real D1 runs these as a single implicit transaction; this fake just
      // runs each in order, which is faithful enough for the idempotent
      // "INSERT ... ON CONFLICT DO NOTHING" bucket-creation statements this
      // is used for (see reserveGeminiQuotaSlot).
      return Promise.all(stmts.map(s => s.run()));
    },
  };
  return { db, inserts, selects };
}

function validGeminiJson(overrides = {}) {
  return JSON.stringify({
    investigation_id: 'MI-test',
    assets: ['BTC'],
    market_classification: 'ASSET_SPECIFIC',
    catalysts: [{
      category: 'REGULATION',
      event_timestamp: '2026-08-18T10:00:00Z',
      first_public_timestamp: '2026-08-18T10:05:00Z',
      direction: 'DOWN',
      confidence: 'HIGH',
      description: 'test catalyst',
      assets: ['BTC'],
      sources: [{ title: 'Test Source', publisher: 'Test Publisher', url: 'https://example.com/article', published_at: '2026-08-18T10:05:00Z' }],
    }],
    ...overrides,
  });
}

function mockFetchOnce(implementation) {
  const original = global.fetch;
  global.fetch = implementation;
  return () => { global.fetch = original; };
}

describe('Gemini live — parseGeminiInvestigationResponse', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(extractFunctions('parseGeminiInvestigationResponse')); });

  it('parses clean JSON', () => {
    expect(scope.parseGeminiInvestigationResponse('{"a":1}')).toEqual({ a: 1 });
  });

  it('strips a markdown code fence Gemini adds despite instructions not to', () => {
    expect(scope.parseGeminiInvestigationResponse('```json\n{"a":1}\n```')).toEqual({ a: 1 });
  });

  it('throws (does not silently return a guess) on genuinely malformed JSON', () => {
    expect(() => scope.parseGeminiInvestigationResponse('not json at all')).toThrow();
  });
});

describe('Gemini live — validateCatalystSources', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(extractFunctions('validateCatalystSources')); });

  it('rejects a missing sources field entirely', () => {
    expect(scope.validateCatalystSources(undefined).valid).toBe(false);
  });

  it('rejects an empty sources array -- a catalyst with zero sources is not evidence', () => {
    expect(scope.validateCatalystSources([]).valid).toBe(false);
  });

  it('rejects a source with no url', () => {
    expect(scope.validateCatalystSources([{ title: 'x' }]).valid).toBe(false);
  });

  it('rejects a malformed url', () => {
    expect(scope.validateCatalystSources([{ url: 'not-a-url' }]).valid).toBe(false);
  });

  it('accepts a well-formed source', () => {
    expect(scope.validateCatalystSources([{ url: 'https://example.com/a' }]).valid).toBe(true);
  });
});

describe('Gemini live — validateGeminiInvestigationResponse', () => {
  let scope;
  beforeAll(() => {
    const src = extractFunctions('validateGeminiInvestigationResponse', 'validateCatalystSources')
      + '\n\n' + extractConstants('ALLOWED_MARKET_CLASSIFICATIONS');
    scope = evalInScope(src);
  });

  it('accepts a fully valid response', () => {
    const parsed = JSON.parse(validGeminiJson());
    expect(scope.validateGeminiInvestigationResponse(parsed).valid).toBe(true);
  });

  it('rejects a non-object response', () => {
    expect(scope.validateGeminiInvestigationResponse(null).valid).toBe(false);
    expect(scope.validateGeminiInvestigationResponse('a string').valid).toBe(false);
  });

  it('rejects a missing assets array', () => {
    const parsed = { catalysts: [] };
    expect(scope.validateGeminiInvestigationResponse(parsed).valid).toBe(false);
  });

  it('rejects an invalid market_classification', () => {
    const parsed = JSON.parse(validGeminiJson({ market_classification: 'EXTREMELY_WIDE' }));
    expect(scope.validateGeminiInvestigationResponse(parsed).valid).toBe(false);
  });

  it('rejects a catalyst with a missing source -- the "missing source" case', () => {
    const parsed = JSON.parse(validGeminiJson());
    parsed.catalysts[0].sources = [];
    expect(scope.validateGeminiInvestigationResponse(parsed).valid).toBe(false);
  });

  it('accepts an empty catalysts array -- "Gemini cannot find a credible catalyst" is a valid response shape', () => {
    const parsed = JSON.parse(validGeminiJson({ catalysts: [] }));
    expect(scope.validateGeminiInvestigationResponse(parsed).valid).toBe(true);
  });
});

describe('Gemini live — deriveTimestampProvenance (BLOCKER 3)', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(extractFunctions('deriveTimestampProvenance')); });

  it('gemini_reported + the reported confidence when first_public_timestamp is present and confidence is valid', () => {
    expect(scope.deriveTimestampProvenance(1700000000000, 'HIGH')).toEqual({ timestampSource: 'gemini_reported', timestampConfidence: 'HIGH' });
    expect(scope.deriveTimestampProvenance(1700000000000, 'MEDIUM')).toEqual({ timestampSource: 'gemini_reported', timestampConfidence: 'MEDIUM' });
    expect(scope.deriveTimestampProvenance(1700000000000, 'LOW')).toEqual({ timestampSource: 'gemini_reported', timestampConfidence: 'LOW' });
  });

  it('unknown source + UNKNOWN confidence when first_public_timestamp is null -- the "cannot establish it reliably" case', () => {
    expect(scope.deriveTimestampProvenance(null, 'HIGH')).toEqual({ timestampSource: 'unknown', timestampConfidence: 'UNKNOWN' });
  });

  it('never invents a confidence value -- an out-of-range/hallucinated confidence is downgraded to UNKNOWN, not passed through', () => {
    expect(scope.deriveTimestampProvenance(1700000000000, 'VERY_SURE')).toEqual({ timestampSource: 'gemini_reported', timestampConfidence: 'UNKNOWN' });
    expect(scope.deriveTimestampProvenance(1700000000000, undefined)).toEqual({ timestampSource: 'gemini_reported', timestampConfidence: 'UNKNOWN' });
  });
});

describe('Analyst Relay — getAnalystRelayCandidate (mocked candidate-building)', () => {
  let scope;

  beforeAll(() => {
    // Mocks buildInvestigationCandidates the same way the
    // evaluateGeminiTriggers tests above do -- isolates this function's own
    // logic (ranking + threshold + context-building) from D1/network.
    // buildInvestigationContext's own D1 reads are NOT mocked here -- a
    // fake DB (makeFakeDb, defined above) is passed in per test instead.
    const mocks = `async function buildInvestigationCandidates(env) { return env.__mockCandidates; }`;
    const src = mocks + '\n\n' + extractFunctions(
      'rankInvestigationCandidates', 'computeInvestigationPriority', 'selectWithinBudget',
      'buildInvestigationContext', 'computeContextHash', 'formatContextForGemini', 'formatContextForAnalyst',
      'getAnalystRelayCandidate'
    ) + '\n\n' + extractConstants('INVESTIGATION_PRIORITY_WEIGHTS', 'INVESTIGATION_PRIORITY_THRESHOLD', 'INVESTIGATION_ASSETS', 'INVESTIGATION_WINDOW_MS');
    scope = evalInScope(src);
  });

  it('hasCandidate:false when there is nothing to evaluate', async () => {
    const { db } = makeFakeDb();
    const result = await scope.getAnalystRelayCandidate({ __mockCandidates: [], DB: db });
    expect(result).toEqual({ ok: true, hasCandidate: false });
  });

  it('hasCandidate:false when candidates exist but none clear the priority threshold', async () => {
    const { db } = makeFakeDb();
    const result = await scope.getAnalystRelayCandidate({
      __mockCandidates: [{ id: 'BTC', assets: ['BTC'], signals: { priceMovePct: 0.2, wasWrong: false, confidence: 0.9 } }],
      DB: db,
    });
    expect(result.hasCandidate).toBe(false);
  });

  it('returns the top-ranked candidate, a real prompt, factual summary, and a context hash when one clears the bar', async () => {
    const { db } = makeFakeDb();
    const env = {
      __mockCandidates: [{
        id: 'BTC', assets: ['BTC'],
        signals: { priceMovePct: 12, wasWrong: true, confidence: 0.9, correlatedFailureAssetCount: 3, isVolatilityAnomaly: true, isRegimeChange: true },
      }],
      DB: db,
    };
    const result = await scope.getAnalystRelayCandidate(env);
    expect(result.hasCandidate).toBe(true);
    expect(result.candidateId).toBe('BTC');
    expect(result.assets).toEqual(['BTC']);
    expect(typeof result.prompt).toBe('string');
    expect(result.prompt).toContain('BTC');
    expect(typeof result.factualSummary).toBe('string');
    expect(typeof result.contextHash).toBe('string');
    expect(result.contextHash.length).toBe(64); // SHA-256 hex
    expect(typeof result.promptRequestedTs).toBe('number');
    expect(result.context).toBeTruthy(); // full context round-tripped for recordAnalystRelay to persist verbatim
  });

  it('picks only ONE candidate even when multiple clear the bar -- one prompt slot at a time', async () => {
    const { db } = makeFakeDb();
    const strong = { signals: { priceMovePct: 12, wasWrong: true, confidence: 0.9, correlatedFailureAssetCount: 3, isVolatilityAnomaly: true, isRegimeChange: true } };
    const env = { __mockCandidates: [{ id: 'BTC', assets: ['BTC'], ...strong }, { id: 'LINK', assets: ['LINK'], ...strong }], DB: db };
    const result = await scope.getAnalystRelayCandidate(env);
    expect(result.hasCandidate).toBe(true);
    expect(['BTC', 'LINK']).toContain(result.candidateId); // exactly one, whichever ranks higher
  });
});

describe('Analyst Relay — recordAnalystRelay (mocked D1) — reuses the real parse/validate/catalyst pipeline', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions(
      'recordAnalystRelay', 'parseGeminiInvestigationResponse', 'validateGeminiInvestigationResponse',
      'validateCatalystSources', 'validateCatalystPayload', 'isDuplicateCatalyst', 'fetchCatalystsForPeriod',
      'recordCatalyst', 'deriveTimestampProvenance'
    ) + '\n\n' + extractConstants('ALLOWED_MARKET_CLASSIFICATIONS', 'ALLOWED_CATALYST_CATEGORIES');
    scope = evalInScope(src);
  });

  it('happy path: writes exactly one catalyst with an AR- prefixed investigation_id, and one analyst_relay_log row', async () => {
    const { db, inserts } = makeFakeDb();
    const result = await scope.recordAnalystRelay({ DB: db }, {
      candidateId: 'BTC', assets: ['BTC'], promptRequestedTs: Date.now() - 60000,
      rawResponseText: validGeminiJson(),
    });
    expect(result.ok).toBe(true);
    expect(result.validationStatus).toBe('ok');
    expect(result.catalystsWritten).toBe(1);
    expect(result.relayId).toMatch(/^AR-\d+-BTC$/);

    const catalystInsert = inserts.find(i => i.table === 'coin_catalyst_log');
    expect(catalystInsert.args).toContain(result.relayId); // investigation_id bound to the relay id, not an MI- id
    expect(catalystInsert.args).toContain(0); // source_grounded bound as 0 -- always, for a human-relayed response

    const relayInsert = inserts.find(i => i.table === 'analyst_relay_log');
    expect(relayInsert).toBeTruthy();
  });

  it('regression: NEVER writes to gemini_investigations or gemini_provider_calls -- structurally cannot be mistaken for a real API call', async () => {
    const { db, inserts } = makeFakeDb();
    await scope.recordAnalystRelay({ DB: db }, { candidateId: 'BTC', assets: ['BTC'], rawResponseText: validGeminiJson() });
    expect(inserts.some(i => i.table === 'gemini_investigations')).toBe(false);
    expect(inserts.some(i => i.table === 'gemini_provider_calls')).toBe(false);
  });

  it('malformed pasted text (not JSON at all): validation_status=malformed_response, zero catalysts, still logs the attempt', async () => {
    const { db, inserts } = makeFakeDb();
    const result = await scope.recordAnalystRelay({ DB: db }, {
      candidateId: 'BTC', assets: ['BTC'], rawResponseText: 'I could not find anything relevant, sorry!',
    });
    expect(result.validationStatus).toBe('malformed_response');
    expect(result.catalystsWritten).toBe(0);
    expect(inserts.some(i => i.table === 'coin_catalyst_log')).toBe(false);
    expect(inserts.find(i => i.table === 'analyst_relay_log')).toBeTruthy(); // the attempt is still audited
  });

  it('valid JSON, empty catalysts array: validation_status=no_catalyst_found, not an error', async () => {
    const { db } = makeFakeDb();
    const result = await scope.recordAnalystRelay({ DB: db }, {
      candidateId: 'BTC', assets: ['BTC'], rawResponseText: validGeminiJson({ catalysts: [] }),
    });
    expect(result.validationStatus).toBe('no_catalyst_found');
    expect(result.catalystsWritten).toBe(0);
  });

  it('structurally invalid response (fails validateGeminiInvestigationResponse): validation_status=invalid_response', async () => {
    const { db } = makeFakeDb();
    const result = await scope.recordAnalystRelay({ DB: db }, {
      candidateId: 'BTC', assets: ['BTC'], rawResponseText: JSON.stringify({ not_the_right_shape: true }),
    });
    expect(result.validationStatus).toBe('invalid_response');
    expect(result.catalystsWritten).toBe(0);
  });

  it('duplicate catalyst (already exists in the recent window): skipped, still audited', async () => {
    const eventTs = Date.parse('2026-08-18T10:00:00Z');
    const { db, inserts } = makeFakeDb({ existingCatalysts: [{ coin: 'BTC', category: 'REGULATION', ts: eventTs }] });
    const result = await scope.recordAnalystRelay({ DB: db }, {
      candidateId: 'BTC', assets: ['BTC'], rawResponseText: validGeminiJson(),
    });
    expect(result.catalystsWritten).toBe(0);
    expect(inserts.some(i => i.table === 'coin_catalyst_log')).toBe(false);
    expect(inserts.find(i => i.table === 'analyst_relay_log')).toBeTruthy();
  });

  it('the pasted raw text is preserved in the audit row for later review, truncated to a sane cap', async () => {
    const { db, inserts } = makeFakeDb();
    const longText = validGeminiJson() + ' '.repeat(30000);
    await scope.recordAnalystRelay({ DB: db }, { candidateId: 'BTC', assets: ['BTC'], rawResponseText: longText });
    const relayInsert = inserts.find(i => i.table === 'analyst_relay_log');
    const storedText = relayInsert.args.find(a => typeof a === 'string' && a.length > 100);
    expect(storedText.length).toBeLessThanOrEqual(20000);
  });
});

// ---------------------------------------------------------------------
// A dedicated fake DB for buildInvestigationContext -- returns REAL
// multi-row, multi-asset data (unlike makeFakeDb above, which defaults to
// empty results) so these tests can prove cross-asset observations
// actually flow through, not just that the code doesn't crash on empty
// data. Routes by table/data-table name appearing in the SQL text.
function makeContextFakeDb({ btc = [], eth = [], link = [], prices = {} } = {}) {
  const tableToRows = { predictions: btc, eth_predictions: eth, link_predictions: link };
  const dataTableToPrices = { btc_data: prices.BTC, eth_data: prices.ETH, link_data: prices.LINK };
  function statementFor(sql) {
    return {
      async all() {
        for (const [table, rows] of Object.entries(tableToRows)) {
          if (sql.includes(`FROM ${table} `)) return { results: rows };
        }
        return { results: [] };
      },
      async first() {
        for (const [dataTable, p] of Object.entries(dataTableToPrices)) {
          if (sql.includes(`FROM ${dataTable} `)) {
            if (!p) return null;
            return sql.includes('ASC') ? { price: p.oldest } : { price: p.newest };
          }
        }
        return null;
      },
    };
  }
  return {
    prepare(sql) {
      // Real D1 prepared statements support .first()/.all() directly (no
      // params) as well as after .bind(...) -- the "newest price" query
      // here has no parameters at all, so both paths are exercised.
      return { ...statementFor(sql), bind: () => statementFor(sql) };
    },
  };
}

describe('Shared investigation context — buildInvestigationContext / formatContextForGemini / formatContextForAnalyst', () => {
  let scope;

  beforeAll(() => {
    scope = evalInScope(extractFunctions(
      'buildInvestigationContext', 'computeContextHash', 'formatContextForGemini', 'formatContextForAnalyst'
    ) + '\n\n' + extractConstants('INVESTIGATION_ASSETS', 'INVESTIGATION_WINDOW_MS'));
  });

  // The exact BTC/ETH/LINK numbers from the build spec's own worked
  // example (100%/66.7%/73.3% confidence, all predicted UP, all actually
  // went DOWN, -1.70%/-3.83%/-5.48%).
  const nowTs = Date.parse('2026-08-24T00:00:00Z').valueOf();
  const resolvedTs = nowTs - 60000;
  const btcRow = { ts: nowTs - 3600000, resolved_ts: resolvedTs, p_up: 1.0, calibrated_p_up: null, realized_up: 0, is_regime_anomaly: 1 };
  const ethRow = { ts: nowTs - 3600000, resolved_ts: resolvedTs, p_up: 0.667, calibrated_p_up: null, realized_up: 0, is_regime_anomaly: 0 };
  const linkRow = { ts: nowTs - 3600000, resolved_ts: resolvedTs, p_up: 0.733, calibrated_p_up: null, realized_up: 0, is_regime_anomaly: 0 };
  const fullPrices = {
    BTC: { oldest: 100000, newest: 98300 },   // -1.70%
    ETH: { oldest: 3000, newest: 2885.1 },    // -3.83%
    LINK: { oldest: 11, newest: 10.3972 },    // -5.48%
  };

  it('A. normal single-asset candidate: primary asset correctly identified, others reported per real (here: absent) data -- never fabricated', async () => {
    const db = makeContextFakeDb({ btc: [btcRow] }); // only BTC has data
    const context = await scope.buildInvestigationContext({ DB: db }, { id: 'BTC', assets: ['BTC'] }, nowTs);
    expect(context.primaryAsset).toBe('BTC');
    expect(context.observations.BTC.available).toBe(true);
    expect(context.observations.ETH).toEqual({ available: false });
    expect(context.observations.LINK).toEqual({ available: false });
    expect(context.correlatedFailureAssetCount).toBe(1); // only BTC has data to be wrong about
  });

  it('B. cross-asset anomaly: actual BTC/ETH/LINK observations are present, not a bare correlatedFailureAssetCount integer', async () => {
    const db = makeContextFakeDb({ btc: [btcRow], eth: [ethRow], link: [linkRow], prices: fullPrices });
    const context = await scope.buildInvestigationContext({ DB: db }, { id: 'BTC', assets: ['BTC'] }, nowTs);

    expect(context.observations.BTC).toMatchObject({ available: true, predictedDirection: 'UP', confidencePct: 100, actualDirection: 'DOWN', wasWrong: true, isRegimeAnomaly: true });
    expect(context.observations.BTC.actualMovePct).toBeCloseTo(-1.70, 1);
    expect(context.observations.ETH).toMatchObject({ available: true, predictedDirection: 'UP', confidencePct: 66.7, actualDirection: 'DOWN', wasWrong: true });
    expect(context.observations.ETH.actualMovePct).toBeCloseTo(-3.83, 1);
    expect(context.observations.LINK).toMatchObject({ available: true, predictedDirection: 'UP', confidencePct: 73.3, actualDirection: 'DOWN', wasWrong: true });
    expect(context.observations.LINK.actualMovePct).toBeCloseTo(-5.48, 1);

    expect(context.correlatedFailureAssetCount).toBe(3);
    expect(context.correlatedFailureAssets.sort()).toEqual(['BTC', 'ETH', 'LINK']);
  });

  it('C. historical context: real recent cycles only, bounded to the existing 5-row/3.5h window -- not a new/arbitrary window', async () => {
    const olderRow = { ts: nowTs - 7200000, resolved_ts: resolvedTs - 3600000, p_up: 0.6, calibrated_p_up: null, realized_up: 1, is_regime_anomaly: 0 };
    const db = makeContextFakeDb({ btc: [btcRow, olderRow], prices: fullPrices });
    const context = await scope.buildInvestigationContext({ DB: db }, { id: 'BTC', assets: ['BTC'] }, nowTs);
    expect(context.windowMs).toBe(scope.INVESTIGATION_WINDOW_MS); // literally the same constant, not a new value
    expect(context.observations.BTC.recentCycles).toHaveLength(2);
    expect(context.observations.BTC.recentCycles[0].wasWrong).toBe(true);  // btcRow
    expect(context.observations.BTC.recentCycles[1].wasWrong).toBe(false); // olderRow -- predicted UP(0.6), actual UP -- correct
  });

  it('D. missing observation: explicitly available:false, never a fabricated/guessed value', async () => {
    const db = makeContextFakeDb({ btc: [btcRow] }); // ETH/LINK have zero rows
    const context = await scope.buildInvestigationContext({ DB: db }, { id: 'BTC', assets: ['BTC'] }, nowTs);
    expect(context.observations.ETH).toEqual({ available: false });
    expect(context.observations.LINK).toEqual({ available: false });
    // Not present in correlatedFailureAssets either -- absence of data is
    // not silently treated as "wrong".
    expect(context.correlatedFailureAssets).not.toContain('ETH');
    expect(context.correlatedFailureAssets).not.toContain('LINK');
  });

  it('E. Gemini and Analyst Relay receive identical factual context -- same context object feeds both formatters, same facts appear in both outputs', async () => {
    const db = makeContextFakeDb({ btc: [btcRow], eth: [ethRow], link: [linkRow], prices: fullPrices });
    const context = await scope.buildInvestigationContext({ DB: db }, { id: 'BTC', assets: ['BTC'] }, nowTs);

    const geminiText = scope.formatContextForGemini(context);
    const analystOutput = scope.formatContextForAnalyst(context);

    // Every real number appears in BOTH outputs -- the facts are identical.
    for (const needle of ['ETH', 'LINK', '-1.7', '-3.83', '-5.48', '100%', '66.7%', '73.3%']) {
      expect(geminiText).toContain(needle);
      expect(analystOutput.factualSummary).toContain(needle);
      expect(analystOutput.prompt).toContain(needle);
    }
  });

  it('F. different presentation is allowed: the human factual summary is NOT byte-identical to the Gemini prompt, despite sharing all facts', async () => {
    const db = makeContextFakeDb({ btc: [btcRow], eth: [ethRow], link: [linkRow], prices: fullPrices });
    const context = await scope.buildInvestigationContext({ DB: db }, { id: 'BTC', assets: ['BTC'] }, nowTs);
    const geminiText = scope.formatContextForGemini(context);
    const factualSummary = scope.formatContextForAnalyst(context).factualSummary;
    expect(factualSummary).not.toBe(geminiText); // genuinely different presentation
    expect(factualSummary.length).toBeLessThan(geminiText.length); // the human summary is the short version, not the full instruction+schema text
  });

  it('same underlying context produces the same hash regardless of which formatter reads it -- proves the hash is about the facts, not the presentation', async () => {
    const db = makeContextFakeDb({ btc: [btcRow], eth: [ethRow], link: [linkRow], prices: fullPrices });
    const context = await scope.buildInvestigationContext({ DB: db }, { id: 'BTC', assets: ['BTC'] }, nowTs);
    // Formatting doesn't mutate the context or its hash.
    scope.formatContextForGemini(context);
    scope.formatContextForAnalyst(context);
    const recomputed = await scope.computeContextHash(context);
    expect(context.contextHash).toBe(recomputed);
    expect(context.contextHash).toMatch(/^[0-9a-f]{64}$/);
  });

  it('two builds of byte-identical underlying data hash the same, even seconds apart -- generatedTs is deliberately excluded from the hash', async () => {
    const db1 = makeContextFakeDb({ btc: [btcRow], eth: [ethRow], link: [linkRow], prices: fullPrices });
    const db2 = makeContextFakeDb({ btc: [btcRow], eth: [ethRow], link: [linkRow], prices: fullPrices });
    const contextA = await scope.buildInvestigationContext({ DB: db1 }, { id: 'BTC', assets: ['BTC'] }, nowTs);
    const contextB = await scope.buildInvestigationContext({ DB: db2 }, { id: 'BTC', assets: ['BTC'] }, nowTs + 5000); // different generatedTs
    expect(contextA.contextHash).toBe(contextB.contextHash);
  });

  it('different underlying data produces a different hash', async () => {
    const dbWrong = makeContextFakeDb({ btc: [btcRow], prices: fullPrices });
    const dbFull = makeContextFakeDb({ btc: [btcRow], eth: [ethRow], link: [linkRow], prices: fullPrices });
    const contextWrong = await scope.buildInvestigationContext({ DB: dbWrong }, { id: 'BTC', assets: ['BTC'] }, nowTs);
    const contextFull = await scope.buildInvestigationContext({ DB: dbFull }, { id: 'BTC', assets: ['BTC'] }, nowTs);
    expect(contextWrong.contextHash).not.toBe(contextFull.contextHash);
  });
});

describe('Shared investigation context — G/H/I/J regression, updated for the grounding removal: what changed vs what stayed', () => {
  it('G. narrative\'s own budget (the only live Gemini budget left) is unchanged: 1/day+1/hour cron lanes, 3/day+1/hour manual lanes', () => {
    const scope = evalInScope(extractConstants('GEMINI_SHARED_QUOTA_CONFIG'));
    expect(scope.GEMINI_SHARED_QUOTA_CONFIG.btc_narrative_cron).toEqual({ day: 1, hour: 1 });
    expect(scope.GEMINI_SHARED_QUOTA_CONFIG.btc_narrative_manual).toEqual({ day: 3, hour: 1 });
    expect(scope.GEMINI_SHARED_QUOTA_CONFIG.link_narrative_cron).toEqual({ day: 1, hour: 1 });
    expect(scope.GEMINI_SHARED_QUOTA_CONFIG.link_narrative_manual).toEqual({ day: 3, hour: 1 });
    // The investigation lane itself is gone -- there's nothing left to reserve it.
    expect(scope.GEMINI_SHARED_QUOTA_CONFIG.investigation).toBeUndefined();
  });

  it('H. priority threshold is still exactly 4, unchanged -- Analyst Relay uses the same bar the automated investigation used to', () => {
    const scope = evalInScope(extractConstants('INVESTIGATION_PRIORITY_THRESHOLD'));
    expect(scope.INVESTIGATION_PRIORITY_THRESHOLD).toBe(4);
  });

  it('I. the automated MI- correlation ID generator is gone; the AR- one (Analyst Relay) is not', () => {
    expect(() => extractFunctions('investigateMarketEvent')).toThrow(); // confirms it's actually gone, not just untested
    const relaySrc = extractFunctions('recordAnalystRelay');
    expect(relaySrc).toMatch(/`AR-\$\{Date\.now\(\)\}-\$\{candidateId\}`/);
  });

  it('J. Gemini grounding is fully removed, not just unused: no useGrounding param, no google_search tool, anywhere in the shared caller', () => {
    const src = extractFunctions('callGeminiGenerateContent');
    expect(src).not.toMatch(/useGrounding/);
    expect(src).not.toMatch(/google_search/);
    expect(src).not.toMatch(/tools\s*:/);
  });
});
