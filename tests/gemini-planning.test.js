import { describe, it, expect, beforeAll } from 'vitest';
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
    const result = scope.withinGeminiRateLimit({ investigationsToday: 1, investigationsThisHour: 0 });
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
