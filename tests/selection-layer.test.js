import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

describe('computeLcaScore — Local Class Accuracy, the actual statistical core', () => {
  let scope;
  const TOL = 6 * 3600000;

  beforeAll(() => {
    const src = extractConstants('SELECTION_MIN_MATCHED') + '\n\n' + extractFunctions('computeLcaScore', 'nearestRow');
    scope = evalInScope(src);
  });

  it('scores a variant that was correct every time it made the same-direction call', () => {
    const variantRows = [
      { ts: 1000, p_up: 0.7, realized_up: 1 }, // up call, correct
      { ts: 2000, p_up: 0.3, realized_up: 0 }, // down call — different direction, should be excluded
      { ts: 3000, p_up: 0.8, realized_up: 1 }, // up call, correct
      { ts: 4000, p_up: 0.75, realized_up: 1 }, // up call, correct
    ];
    const neighborhood = [{ ts: 1000 }, { ts: 2000 }, { ts: 3000 }, { ts: 4000 }];
    const result = scope.computeLcaScore(variantRows, neighborhood, true, TOL); // today's call is "up"
    expect(result).not.toBeNull();
    expect(result.n_matched).toBe(3); // only the 3 up-calls count, the down-call is excluded by LCA's own definition
    expect(result.lca).toBe(1); // all 3 up-calls were correct
  });

  it('correctly restricts to same-direction calls only — the actual point of LCA vs OLA', () => {
    // Deliberately mixed: variant is great at calling "down" but terrible at
    // calling "up". If today's call is "up", LCA should reflect the bad
    // up-calling record, not blend in the good down-calling record.
    const variantRows = [
      { ts: 1000, p_up: 0.6, realized_up: 0 }, // up call, WRONG
      { ts: 2000, p_up: 0.65, realized_up: 0 }, // up call, WRONG
      { ts: 3000, p_up: 0.2, realized_up: 0 }, // down call, correct
      { ts: 4000, p_up: 0.15, realized_up: 0 }, // down call, correct
      { ts: 5000, p_up: 0.55, realized_up: 0 }, // up call, WRONG
    ];
    const neighborhood = [{ ts: 1000 }, { ts: 2000 }, { ts: 3000 }, { ts: 4000 }, { ts: 5000 }];
    const result = scope.computeLcaScore(variantRows, neighborhood, true, TOL); // today's call is "up"
    expect(result.n_matched).toBe(3); // 3 up-calls in the neighborhood
    expect(result.lca).toBe(0); // all 3 were wrong — LCA correctly reflects only the up-calling record
  });

  it('returns null when there are fewer than SELECTION_MIN_MATCHED same-direction matches', () => {
    const variantRows = [
      { ts: 1000, p_up: 0.7, realized_up: 1 },
      { ts: 2000, p_up: 0.3, realized_up: 0 },
    ];
    const neighborhood = [{ ts: 1000 }, { ts: 2000 }];
    // Only 1 same-direction (up) match — below the minimum of 3
    const result = scope.computeLcaScore(variantRows, neighborhood, true, TOL);
    expect(result).toBeNull();
  });

  it('returns null (not a crash) when no variant rows fall within tolerance of any neighborhood timestamp', () => {
    const variantRows = [{ ts: 1000, p_up: 0.7, realized_up: 1 }];
    const neighborhood = [{ ts: 999999999 }]; // way outside tolerance
    expect(() => scope.computeLcaScore(variantRows, neighborhood, true, TOL)).not.toThrow();
    expect(scope.computeLcaScore(variantRows, neighborhood, true, TOL)).toBeNull();
  });
});

describe('decideSelection — significance-gated winner selection', () => {
  let scope;

  beforeAll(() => {
    const src = extractConstants('SELECTION_CRITICAL_Z') + '\n\n' + extractFunctions('decideSelection');
    scope = evalInScope(src);
  });

  it('selects a variant whose edge clearly clears the significance bar', () => {
    // n=20, lca=0.85 — a large, well-supported edge should clear even the
    // strictest (m=6) bar.
    const scores = [
      { variant: 'challenger_flat', lca: 0.85, n_matched: 20 },
      { variant: 'original', lca: 0.55, n_matched: 20 },
    ];
    const result = scope.decideSelection(scores);
    expect(result.clearedGate).toBe(true);
    expect(result.chosen).toBe('challenger_flat');
  });

  it('does NOT select a variant whose edge is real but too thin given its sample size', () => {
    // lca=0.6 on n=5 — a real edge on paper, but nowhere near enough
    // evidence to clear even the loosest (m=1) bar. This is the exact
    // "noise, not signal" case the whole significance gate exists to catch.
    const scores = [{ variant: 'challenger_tilted', lca: 0.6, n_matched: 5 }];
    const result = scope.decideSelection(scores);
    expect(result.clearedGate).toBe(false);
    expect(result.chosen).toBe('original');
  });

  it('requires a LARGER margin as more variants are compared — the actual multiple-testing correction', () => {
    // Same lca and n, but compared against a different number of other
    // variants — the required margin should grow with comparison count,
    // confirming the Bonferroni-style correction is actually doing
    // something, not just present in name.
    const scoresFew = [{ variant: 'a', lca: 0.75, n_matched: 10 }];
    const scoresMany = [
      { variant: 'a', lca: 0.75, n_matched: 10 },
      { variant: 'b', lca: 0.5, n_matched: 10 }, { variant: 'c', lca: 0.5, n_matched: 10 },
      { variant: 'd', lca: 0.5, n_matched: 10 }, { variant: 'e', lca: 0.5, n_matched: 10 },
      { variant: 'f', lca: 0.5, n_matched: 10 },
    ];
    const resultFew = scope.decideSelection(scoresFew);
    const resultMany = scope.decideSelection(scoresMany);
    expect(resultMany.requiredMargin).toBeGreaterThan(resultFew.requiredMargin);
  });

  it('handles an empty scores array without crashing', () => {
    expect(() => scope.decideSelection([])).not.toThrow();
    const result = scope.decideSelection([]);
    expect(result.chosen).toBeNull();
  });

  it('picks the single best variant by LCA when multiple are eligible', () => {
    const scores = [
      { variant: 'a', lca: 0.6, n_matched: 30 },
      { variant: 'b', lca: 0.9, n_matched: 30 },
      { variant: 'c', lca: 0.55, n_matched: 30 },
    ];
    const result = scope.decideSelection(scores);
    expect(result.winner.variant).toBe('b');
  });
});
