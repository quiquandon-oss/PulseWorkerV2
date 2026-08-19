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

function makeFakeDb({ existingCatalysts = [] } = {}) {
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
              return { results: [] };
            },
            async first() {
              return null;
            },
            async run() {
              const table = /INSERT INTO (\w+)/i.exec(sql)?.[1] || 'unknown';
              const row = { table, sql, args };
              inserts.push(row);
              return { meta: { last_row_id: inserts.length } };
            },
          };
        },
      };
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

describe('Gemini live — investigateMarketEvent (mocked fetch + D1)', () => {
  let scope;
  let restoreFetch;

  beforeAll(() => {
    const src = extractFunctions(
      'investigateMarketEvent', 'callGeminiForMarketInvestigation', 'buildGeminiInvestigationPrompt',
      'parseGeminiInvestigationResponse', 'validateGeminiInvestigationResponse', 'validateCatalystSources',
      'validateCatalystPayload', 'isDuplicateCatalyst', 'fetchCatalystsForPeriod', 'recordCatalyst', 'recordGeminiInvestigation',
      'extractGroundingMetadata', 'isSourceGrounded', 'deriveTimestampProvenance'
    ) + '\n\n' + extractConstants(
      'ALLOWED_MARKET_CLASSIFICATIONS', 'ALLOWED_CATALYST_CATEGORIES',
      'GEMINI_INVESTIGATION_TIMEOUT_MS', 'GEMINI_INVESTIGATION_MODEL'
    );
    scope = evalInScope(src);
  });

  afterEach(() => { if (restoreFetch) { restoreFetch(); restoreFetch = null; } });

  const candidate = { id: 'BTC', assets: ['BTC'], signals: { priceMovePct: 8, wasWrong: true, confidence: 0.65, correlatedFailureAssetCount: 0 } };

  it('happy path: writes exactly one catalyst and one audit row for a single-asset, single-source catalyst', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson() }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);

    const catalystInserts = inserts.filter(i => i.table === 'coin_catalyst_log');
    const auditInserts = inserts.filter(i => i.table === 'gemini_investigations');
    expect(catalystInserts.length).toBe(1);
    expect(auditInserts.length).toBe(1);
  });

  it('multiple assets, one catalyst: writes one row PER asset, not one shared row', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson({
        catalysts: [{
          category: 'MACRO', event_timestamp: '2026-08-18T10:00:00Z', first_public_timestamp: '2026-08-18T10:05:00Z',
          direction: 'DOWN', confidence: 'HIGH', description: 'market-wide', assets: ['BTC', 'ETH', 'LINK'],
          sources: [{ url: 'https://example.com/market-wide' }],
        }],
      }) }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, { ...candidate, assets: ['BTC', 'ETH', 'LINK'] });

    const catalystInserts = inserts.filter(i => i.table === 'coin_catalyst_log');
    expect(catalystInserts.length).toBe(3); // one per affected asset
  });

  it('duplicate catalyst: does not write a catalyst that already exists in the recent window', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson() }] } }] }),
    }));
    const eventTs = Date.parse('2026-08-18T10:00:00Z');
    const { db, inserts } = makeFakeDb({ existingCatalysts: [{ coin: 'BTC', category: 'REGULATION', ts: eventTs }] });
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);

    const catalystInserts = inserts.filter(i => i.table === 'coin_catalyst_log');
    const auditInserts = inserts.filter(i => i.table === 'gemini_investigations');
    expect(catalystInserts.length).toBe(0); // deduped
    expect(auditInserts.length).toBe(1); // investigation is still audited even though nothing new was written
  });

  it('invalid timestamp ordering on one catalyst: skips that catalyst, still audits the investigation as ok', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson({
        catalysts: [{
          category: 'MACRO',
          event_timestamp: '2026-08-18T10:00:00Z',
          first_public_timestamp: '2026-08-10T00:00:00Z', // impossibly early -- "event timestamp after publication timestamp" case
          direction: 'DOWN', confidence: 'HIGH', description: 'bad timestamps', assets: ['BTC'],
          sources: [{ url: 'https://example.com/a' }],
        }],
      }) }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);

    const catalystInserts = inserts.filter(i => i.table === 'coin_catalyst_log');
    const auditRow = inserts.find(i => i.table === 'gemini_investigations');
    expect(catalystInserts.length).toBe(0); // skipped, not written
    expect(auditRow).toBeTruthy(); // but the investigation itself is still recorded
  });

  it('unknown first_public_timestamp: writes the catalyst with a null first_public_timestamp, never a guess', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson({
        catalysts: [{
          category: 'MACRO', event_timestamp: '2026-08-18T10:00:00Z', first_public_timestamp: null,
          direction: 'DOWN', confidence: 'MEDIUM', description: 'unverified timing', assets: ['BTC'],
          sources: [{ url: 'https://example.com/a' }],
        }],
      }) }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);

    const catalystInsert = inserts.find(i => i.table === 'coin_catalyst_log');
    expect(catalystInsert).toBeTruthy(); // still written -- missing first_public_timestamp isn't a validation failure by itself
  });

  it('Gemini cannot find a credible catalyst: valid response, empty catalysts, no D1 catalyst write, audited as no_catalyst_found', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson({ catalysts: [] }) }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);

    expect(inserts.filter(i => i.table === 'coin_catalyst_log').length).toBe(0);
    const auditInsert = inserts.find(i => i.table === 'gemini_investigations');
    expect(auditInsert.args).toContain('no_catalyst_found');
  });

  it('malformed Gemini response (not valid JSON): does not throw, audits as malformed_response, no catalyst written', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: 'this is not json' }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await expect(scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate)).resolves.not.toThrow();
    expect(inserts.filter(i => i.table === 'coin_catalyst_log').length).toBe(0);
    const auditInsert = inserts.find(i => i.table === 'gemini_investigations');
    expect(auditInsert.args).toContain('malformed_response');
  });

  it('missing source (structurally invalid response): audits as invalid_response, no catalyst written', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson({
        catalysts: [{ category: 'MACRO', direction: 'DOWN', confidence: 'HIGH', description: 'no sources', assets: ['BTC'], sources: [] }],
      }) }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);

    expect(inserts.filter(i => i.table === 'coin_catalyst_log').length).toBe(0);
    const auditInsert = inserts.find(i => i.table === 'gemini_investigations');
    expect(auditInsert.args).toContain('invalid_response');
  });

  it('Gemini API failure (non-2xx, non-429): does not throw, audits as error', async () => {
    restoreFetch = mockFetchOnce(async () => ({ ok: false, status: 500, text: async () => 'internal server error' }));
    const { db, inserts } = makeFakeDb();
    await expect(scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate)).resolves.not.toThrow();
    const auditInsert = inserts.find(i => i.table === 'gemini_investigations');
    expect(auditInsert.args).toContain('error');
  });

  it('Gemini rate limit (429): does not throw, audits as rate_limited specifically (not generic error)', async () => {
    restoreFetch = mockFetchOnce(async () => ({ ok: false, status: 429, text: async () => 'rate limited' }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);
    const auditInsert = inserts.find(i => i.table === 'gemini_investigations');
    expect(auditInsert.args).toContain('rate_limited');
  });

  it('Gemini timeout (fetch rejects with an AbortError): does not throw, audits as timeout', async () => {
    restoreFetch = mockFetchOnce(async () => {
      const err = new Error('The operation was aborted');
      err.name = 'AbortError';
      throw err;
    });
    const { db, inserts } = makeFakeDb();
    await expect(scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate)).resolves.not.toThrow();
    const auditInsert = inserts.find(i => i.table === 'gemini_investigations');
    expect(auditInsert.args).toContain('timeout');
  });

  it('a network-level throw (e.g. DNS failure) is caught, not propagated, and still audited', async () => {
    restoreFetch = mockFetchOnce(async () => { throw new TypeError('fetch failed'); });
    const { db, inserts } = makeFakeDb();
    await expect(scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate)).resolves.not.toThrow();
    expect(inserts.find(i => i.table === 'gemini_investigations')).toBeTruthy();
  });

  it('every investigation is audited even on total failure -- auditability holds regardless of outcome', async () => {
    restoreFetch = mockFetchOnce(async () => ({ ok: false, status: 503, text: async () => 'unavailable' }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);
    expect(inserts.filter(i => i.table === 'gemini_investigations').length).toBe(1);
  });

  it('the Gemini request includes google_search grounding tool', async () => {
    let capturedBody = null;
    restoreFetch = mockFetchOnce(async (url, opts) => {
      capturedBody = JSON.parse(opts.body);
      return { ok: true, status: 200, json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson({ catalysts: [] }) }] } }] }) };
    });
    const { db } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);
    expect(capturedBody.tools).toEqual([{ google_search: {} }]);
  });

  it('throws immediately (before any fetch) if GEMINI_API_KEY is not configured -- caught and audited, never crashes the caller', async () => {
    const { db, inserts } = makeFakeDb();
    await expect(scope.investigateMarketEvent({ DB: db }, candidate)).resolves.not.toThrow();
    const auditInsert = inserts.find(i => i.table === 'gemini_investigations');
    expect(auditInsert).toBeTruthy();
    expect(auditInsert.args.some(a => typeof a === 'string' && a.includes('GEMINI_API_KEY'))).toBe(true);
  });
});

describe('Gemini live — extractGroundingMetadata', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(extractFunctions('extractGroundingMetadata')); });

  it('extracts search queries and grounded source URLs when groundingMetadata is present', () => {
    const raw = {
      candidates: [{
        groundingMetadata: {
          webSearchQueries: ['btc regulation news august 2026'],
          groundingChunks: [
            { web: { uri: 'https://example.com/a', title: 'Article A' } },
            { web: { uri: 'https://example.com/b', title: 'Article B' } },
          ],
        },
      }],
    };
    const result = scope.extractGroundingMetadata(raw);
    expect(result.searchQueries).toEqual(['btc regulation news august 2026']);
    expect(result.groundedSources).toEqual([
      { url: 'https://example.com/a', title: 'Article A' },
      { url: 'https://example.com/b', title: 'Article B' },
    ]);
  });

  it('missing groundingMetadata entirely is handled safely -- empty arrays, not a throw', () => {
    expect(scope.extractGroundingMetadata({ candidates: [{ content: {} }] })).toEqual({ searchQueries: [], groundedSources: [] });
  });

  it('a completely empty/malformed response is handled safely', () => {
    expect(scope.extractGroundingMetadata({})).toEqual({ searchQueries: [], groundedSources: [] });
    expect(scope.extractGroundingMetadata(null)).toEqual({ searchQueries: [], groundedSources: [] });
  });

  it('a grounding chunk with no web.uri is filtered out rather than producing a null-url entry', () => {
    const raw = { candidates: [{ groundingMetadata: { groundingChunks: [{ web: { title: 'no url here' } }] } }] };
    expect(scope.extractGroundingMetadata(raw).groundedSources).toEqual([]);
  });
});

describe('Gemini live — isSourceGrounded (source provenance)', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(extractFunctions('isSourceGrounded')); });

  it('true when the source URL exactly matches a grounded source', () => {
    const gm = { groundedSources: [{ url: 'https://example.com/a', title: 'A' }] };
    expect(scope.isSourceGrounded('https://example.com/a', gm)).toBe(true);
  });

  it('false when the source URL does not appear in grounding metadata -- Gemini wrote it from its own knowledge, not a grounded result', () => {
    const gm = { groundedSources: [{ url: 'https://example.com/a', title: 'A' }] };
    expect(scope.isSourceGrounded('https://example.com/different-article', gm)).toBe(false);
  });

  it('false (not a throw) when groundingMetadata is empty or missing', () => {
    expect(scope.isSourceGrounded('https://example.com/a', { groundedSources: [] })).toBe(false);
    expect(scope.isSourceGrounded('https://example.com/a', undefined)).toBe(false);
  });

  it('false when there is no source URL to check', () => {
    expect(scope.isSourceGrounded(null, { groundedSources: [{ url: 'https://example.com/a' }] })).toBe(false);
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

describe('Gemini live — investigateMarketEvent (mocked fetch + D1) — grounding & timestamp provenance', () => {
  let scope;
  let restoreFetch;

  beforeAll(() => {
    const src = extractFunctions(
      'investigateMarketEvent', 'callGeminiForMarketInvestigation', 'buildGeminiInvestigationPrompt',
      'parseGeminiInvestigationResponse', 'validateGeminiInvestigationResponse', 'validateCatalystSources',
      'validateCatalystPayload', 'isDuplicateCatalyst', 'fetchCatalystsForPeriod', 'recordCatalyst', 'recordGeminiInvestigation',
      'extractGroundingMetadata', 'isSourceGrounded', 'deriveTimestampProvenance'
    ) + '\n\n' + extractConstants(
      'ALLOWED_MARKET_CLASSIFICATIONS', 'ALLOWED_CATALYST_CATEGORIES',
      'GEMINI_INVESTIGATION_TIMEOUT_MS', 'GEMINI_INVESTIGATION_MODEL'
    );
    scope = evalInScope(src);
  });

  afterEach(() => { if (restoreFetch) { restoreFetch(); restoreFetch = null; } });

  const candidate = { id: 'BTC', assets: ['BTC'], signals: { priceMovePct: 8, wasWrong: true, confidence: 0.65, correlatedFailureAssetCount: 0 } };

  it('grounding metadata is captured and normalized into the audit row when Google returns it', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({
        candidates: [{
          content: { parts: [{ text: validGeminiJson() }] },
          groundingMetadata: {
            webSearchQueries: ['btc regulation august 2026'],
            groundingChunks: [{ web: { uri: 'https://example.com/article', title: 'Test Source' } }],
          },
        }],
      }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);

    const auditInsert = inserts.find(i => i.table === 'gemini_investigations');
    const groundingJson = auditInsert.args.find(a => typeof a === 'string' && a.includes('webSearchQueries') === false && a.includes('searchQueries'));
    expect(groundingJson).toBeTruthy();
    const parsed = JSON.parse(groundingJson);
    expect(parsed.searchQueries).toEqual(['btc regulation august 2026']);
    expect(parsed.groundedSources).toEqual([{ url: 'https://example.com/article', title: 'Test Source' }]);
  });

  it('missing groundingMetadata on the response is handled safely -- audit row still written, empty grounding structure, no crash', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson() }] } }] }), // no groundingMetadata key at all
    }));
    const { db, inserts } = makeFakeDb();
    await expect(scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate)).resolves.not.toThrow();
    const auditInsert = inserts.find(i => i.table === 'gemini_investigations');
    expect(auditInsert).toBeTruthy();
    const groundingJson = auditInsert.args.find(a => typeof a === 'string' && a.includes('searchQueries'));
    expect(JSON.parse(groundingJson)).toEqual({ searchQueries: [], groundedSources: [] });
  });

  it('a catalyst source that matches grounding metadata is recorded as grounded (source_grounded=1)', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({
        candidates: [{
          content: { parts: [{ text: validGeminiJson() }] }, // source url: https://example.com/article
          groundingMetadata: { groundingChunks: [{ web: { uri: 'https://example.com/article', title: 'Test Source' } }] },
        }],
      }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);
    const catalystInsert = inserts.find(i => i.table === 'coin_catalyst_log');
    expect(catalystInsert.args).toContain(1); // source_grounded bound as 1
  });

  it('a catalyst source that does NOT match any grounded URL is recorded as not grounded (source_grounded=0), not silently dropped', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({
        candidates: [{
          content: { parts: [{ text: validGeminiJson() }] }, // source url: https://example.com/article
          groundingMetadata: { groundingChunks: [{ web: { uri: 'https://example.com/totally-different-page', title: 'Other' } }] },
        }],
      }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);
    const catalystInsert = inserts.find(i => i.table === 'coin_catalyst_log');
    expect(catalystInsert).toBeTruthy(); // still written -- an ungrounded source is flagged, not rejected
    expect(catalystInsert.args).toContain(0); // source_grounded bound as 0
  });

  it('timestamp provenance (timestamp_source, timestamp_confidence) is written alongside the catalyst', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson({
        catalysts: [{
          category: 'MACRO', event_timestamp: '2026-08-18T10:00:00Z', first_public_timestamp: '2026-08-18T10:05:00Z',
          first_public_timestamp_confidence: 'MEDIUM', direction: 'DOWN', confidence: 'HIGH', description: 'x', assets: ['BTC'],
          sources: [{ url: 'https://example.com/a' }],
        }],
      }) }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);
    const catalystInsert = inserts.find(i => i.table === 'coin_catalyst_log');
    expect(catalystInsert.args).toContain('gemini_reported');
    expect(catalystInsert.args).toContain('MEDIUM');
  });

  it('unknown first_public_timestamp confidence: writes timestamp_source=unknown, timestamp_confidence=UNKNOWN', async () => {
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson({
        catalysts: [{
          category: 'MACRO', event_timestamp: '2026-08-18T10:00:00Z', first_public_timestamp: null,
          direction: 'DOWN', confidence: 'MEDIUM', description: 'unverified timing', assets: ['BTC'],
          sources: [{ url: 'https://example.com/a' }],
        }],
      }) }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);
    const catalystInsert = inserts.find(i => i.table === 'coin_catalyst_log');
    expect(catalystInsert.args).toContain('unknown');
    expect(catalystInsert.args).toContain('UNKNOWN');
  });

  it('never substitutes discovery_timestamp for a missing first_public_timestamp -- the D1 row keeps them as two distinct values', async () => {
    const discoveryWindowStart = Date.now();
    restoreFetch = mockFetchOnce(async () => ({
      ok: true, status: 200,
      json: async () => ({ candidates: [{ content: { parts: [{ text: validGeminiJson({
        catalysts: [{
          category: 'MACRO', event_timestamp: '2026-08-18T10:00:00Z', first_public_timestamp: null,
          direction: 'DOWN', confidence: 'MEDIUM', description: 'x', assets: ['BTC'],
          sources: [{ url: 'https://example.com/a' }],
        }],
      }) }] } }] }),
    }));
    const { db, inserts } = makeFakeDb();
    await scope.investigateMarketEvent({ DB: db, GEMINI_API_KEY: 'fake-key' }, candidate);
    const catalystInsert = inserts.find(i => i.table === 'coin_catalyst_log');

    // recordCatalyst's bind order: ts, coin, price_move_pct, headline_source,
    // extracted_reason, category, direction, source_url, discovery_timestamp,
    // confidence, market_classification, first_public_timestamp, ...
    const discoveryTimestampArg = catalystInsert.args[8];
    const firstPublicTimestampArg = catalystInsert.args[11];
    expect(firstPublicTimestampArg).toBeNull(); // stayed null
    expect(discoveryTimestampArg).toBeGreaterThanOrEqual(discoveryWindowStart); // discovery_timestamp is real, recent, and NOT copied into first_public_timestamp's slot
    expect(firstPublicTimestampArg).not.toBe(discoveryTimestampArg);
  });
});

describe('Gemini live — scheduled() ordering (BLOCKER 1 regression tests)', () => {
  let scope;

  beforeAll(() => {
    scope = evalInScope(extractFunctions('runPredictionCycleThenGemini'));
  });

  it('Gemini evaluation does not start until every prediction/resolution task has settled, regardless of individual task speed', async () => {
    const order = [];
    const slowTask = new Promise(resolve => setTimeout(() => { order.push('slow-task-done'); resolve(); }, 15));
    const fastTask = Promise.resolve().then(() => { order.push('fast-task-done'); });
    const mediumTask = new Promise(resolve => setTimeout(() => { order.push('medium-task-done'); resolve(); }, 5));
    const geminiFn = async () => { order.push('gemini-started'); };

    await scope.runPredictionCycleThenGemini([slowTask, fastTask, mediumTask], geminiFn);

    expect(order.indexOf('gemini-started')).toBe(order.length - 1); // gemini is always last
    expect(order).toContain('slow-task-done');
    expect(order).toContain('fast-task-done');
    expect(order).toContain('medium-task-done');
  });

  it('a pre-caught (resolved-after-catch) prediction task failure does not prevent Gemini evaluation from running', async () => {
    const order = [];
    const failingTask = Promise.reject(new Error('BTC prediction failed')).catch(() => { order.push('task-failed-caught'); });
    const okTask = Promise.resolve().then(() => { order.push('task-ok'); });
    const geminiFn = async () => { order.push('gemini-ran'); };

    await scope.runPredictionCycleThenGemini([failingTask, okTask], geminiFn);

    expect(order).toContain('gemini-ran');
    expect(order).toContain('task-failed-caught');
  });

  it('even a genuinely uncaught rejection among the tasks does not stop Gemini evaluation -- Promise.allSettled, not Promise.all', async () => {
    const order = [];
    const uncaughtRejection = Promise.reject(new Error('never caught upstream'));
    const geminiFn = async () => { order.push('gemini-ran'); };

    await expect(scope.runPredictionCycleThenGemini([uncaughtRejection], geminiFn)).resolves.not.toThrow();
    expect(order).toContain('gemini-ran');
  });

  it('runs with zero prediction tasks (e.g. all six somehow failed to even start) without erroring', async () => {
    const order = [];
    const geminiFn = async () => { order.push('gemini-ran'); };
    await scope.runPredictionCycleThenGemini([], geminiFn);
    expect(order).toEqual(['gemini-ran']);
  });

  it("propagates a genuine failure IN the Gemini evaluation function itself (not swallowed by this helper -- that is evaluateGeminiTriggers's job)", async () => {
    const geminiFn = async () => { throw new Error('gemini eval blew up'); };
    await expect(scope.runPredictionCycleThenGemini([Promise.resolve()], geminiFn)).rejects.toThrow('gemini eval blew up');
  });
});

describe('Gemini live — evaluateGeminiTriggers respects the budget (excessive-call-volume guard)', () => {
  let scope;

  beforeAll(() => {
    // Mocks buildInvestigationCandidates/getGeminiInvestigationCounts/
    // investigateMarketEvent (D1/network-dependent) so this test isolates
    // exactly the concern ChatGPT flagged: can the scheduled job call
    // Gemini more times than the budget allows? Real ranking/budget logic
    // (rankInvestigationCandidates, remainingGeminiBudget, selectWithinBudget)
    // is NOT mocked -- it's the real, already-tested implementation.
    const mocks = `
      async function buildInvestigationCandidates(env) { return env.__mockCandidates; }
      async function getGeminiInvestigationCounts(env) { return env.__mockCounts; }
      async function investigateMarketEvent(env, candidate) { env.__investigated.push(candidate.id); }
    `;
    const src = mocks + '\n\n' + extractFunctions(
      'rankInvestigationCandidates', 'computeInvestigationPriority', 'remainingGeminiBudget', 'selectWithinBudget', 'evaluateGeminiTriggers'
    ) + '\n\n' + extractConstants('INVESTIGATION_PRIORITY_WEIGHTS', 'INVESTIGATION_PRIORITY_THRESHOLD', 'GEMINI_TRIGGER_CONFIG');
    scope = evalInScope(src);
  });

  it('never investigates more candidates than the remaining budget, even with many high-priority candidates ranked', async () => {
    const manyHighPriorityCandidates = ['BTC', 'ETH', 'LINK'].map(id => ({
      id, assets: [id],
      signals: { priceMovePct: 12, wasWrong: true, confidence: 0.9, correlatedFailureAssetCount: 3, isVolatilityAnomaly: true, isRegimeChange: true },
    }));
    const env = {
      __mockCandidates: manyHighPriorityCandidates,
      // 1 remaining today regardless of the current configured daily limit
      // (references the real GEMINI_TRIGGER_CONFIG rather than hardcoding a
      // number that would silently go stale if the budget is ever
      // reconfigured -- e.g. the canary config currently in effect).
      __mockCounts: { investigationsToday: scope.GEMINI_TRIGGER_CONFIG.MAX_GEMINI_INVESTIGATIONS_PER_DAY - 1, investigationsThisHour: 0 },
      __investigated: [],
    };
    const result = await scope.evaluateGeminiTriggers(env);
    expect(env.__investigated.length).toBe(1); // budget of 1, not 3
    expect(result.investigationsRun).toBe(1);
    expect(result.candidatesEvaluated).toBe(3);
  });

  it('investigates zero candidates when the budget is exhausted, regardless of priority', async () => {
    const env = {
      __mockCandidates: [{ id: 'BTC', assets: ['BTC'], signals: { priceMovePct: 20, wasWrong: true, confidence: 0.99, correlatedFailureAssetCount: 3 } }],
      __mockCounts: { investigationsToday: scope.GEMINI_TRIGGER_CONFIG.MAX_GEMINI_INVESTIGATIONS_PER_DAY, investigationsThisHour: 0 }, // daily limit already reached, whatever it currently is
      __investigated: [],
    };
    const result = await scope.evaluateGeminiTriggers(env);
    expect(env.__investigated.length).toBe(0);
    expect(result.investigationsRun).toBe(0);
  });

  it('investigates zero candidates when there is nothing to evaluate this cycle -- no D1 write, no fetch attempted', async () => {
    const env = { __mockCandidates: [], __mockCounts: { investigationsToday: 0, investigationsThisHour: 0 }, __investigated: [] };
    const result = await scope.evaluateGeminiTriggers(env);
    expect(result).toEqual({ candidatesEvaluated: 0, investigationsRun: 0 });
    expect(env.__investigated.length).toBe(0);
  });

  it('a LOW-priority candidate is never investigated even with full budget available', async () => {
    const env = {
      __mockCandidates: [{ id: 'BTC', assets: ['BTC'], signals: { priceMovePct: 0.2, wasWrong: false, confidence: 0.9 } }], // Example-A-shaped: LOW
      __mockCounts: { investigationsToday: 0, investigationsThisHour: 0 },
      __investigated: [],
    };
    const result = await scope.evaluateGeminiTriggers(env);
    expect(env.__investigated.length).toBe(0);
    expect(result.investigationsRun).toBe(0);
  });
});
