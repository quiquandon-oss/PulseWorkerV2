// Regression tests for the 3h cron CPU fix (2026-10-11).
//
// Evidence (data-exports, 2026-10-10 export): cron-written BTC rows (stamped
// HH:00 on the 3h grid) fell from 8-11/day in late September to 2-3/day in
// October and to none after 2026-10-09 09:00 UTC; LINK and ETH -- the later
// batches in the same invocation -- had essentially no cron rows since
// 2026-09-06. Live BTC calls on 2026-10-10 still reached selection without
// persisting, i.e. btc_data stayed fresh (the cron's logBtcData ran) while the
// cron's own prediction insert never happened: the invocation was terminated
// between the two, consistent with the exceededCpu termination already
// recorded for this shared invocation on 2026-09-05.
//
// Profiling runPrediction locally showed the regime-anomaly reference loop
// (closest earlier candidate for every candidate, O(n^2) in table rows,
// recomputing every z-score per pair) was ~2/3 of its CPU and growing
// quadratically with btc_data. historicalClosestDistances computes each
// z-score once; the values must stay bit-identical to the inline loop.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const { historicalClosestDistances, FEATURE_KEYS, ETH_FEATURE_KEYS, LINK_FEATURE_KEYS } = evalInScope(
  extractFunctions('historicalClosestDistances') + '\n' +
  extractConstants('FEATURE_KEYS', 'ETH_FEATURE_KEYS', 'LINK_FEATURE_KEYS')
);

// Verbatim copy of the inline loop that runPrediction, runEthPrediction and
// runLinkPrediction each used before this fix -- the reference semantics.
function inlineReference(candidates, keys, stats) {
  return candidates.map((_, i) => {
    if (i === 0) return null;
    let best = Infinity;
    for (let j = 0; j < i; j++) {
      let d = 0;
      for (const k of keys) {
        const z1 = (candidates[i][k] - stats[k].mean) / stats[k].std;
        const z2 = (candidates[j][k] - stats[k].mean) / stats[k].std;
        d += (z1 - z2) ** 2;
      }
      d = Math.sqrt(d);
      if (d < best) best = d;
    }
    return Number.isFinite(best) ? best : null;
  });
}

function rows(n, keys, seed) {
  let s = seed;
  const r = () => (s = (s * 16807) % 2147483647) / 2147483647;
  return Array.from({ length: n }, () => Object.fromEntries(keys.map(k => [k, r() * 100 - 20])));
}

function statsOf(candidates, keys) {
  const stats = {};
  for (const k of keys) {
    const v = candidates.map(c => c[k]);
    const mean = v.reduce((a, b) => a + b, 0) / v.length;
    stats[k] = { mean, std: Math.sqrt(v.reduce((a, b) => a + (b - mean) ** 2, 0) / v.length) };
  }
  return stats;
}

describe('historicalClosestDistances -- bit-identical to the inline loop it replaces', () => {
  for (const [name, keys] of [['BTC', FEATURE_KEYS], ['ETH', ETH_FEATURE_KEYS], ['LINK', LINK_FEATURE_KEYS]]) {
    it(`${name} feature keys, random rows`, () => {
      const c = rows(400, keys, 11 + keys.length);
      const st = statsOf(c, keys);
      expect(historicalClosestDistances(c, keys, st)).toEqual(inlineReference(c, keys, st));
      expect(Object.is(historicalClosestDistances(c, keys, st)[123], inlineReference(c, keys, st)[123])).toBe(true);
    });
  }

  it('edge cases: empty, single row, duplicate rows, zero std (NaN/Infinity) and missing features', () => {
    const keys = FEATURE_KEYS;
    expect(historicalClosestDistances([], keys, statsOf([{ score: 1, technical_score: 1, regime_mag: 1, bottom_score: 1 }], keys))).toEqual([]);
    const one = rows(1, keys, 3);
    expect(historicalClosestDistances(one, keys, statsOf(one, keys))).toEqual([null]);
    const dup = [...rows(5, keys, 5), ...rows(5, keys, 5)];
    expect(historicalClosestDistances(dup, keys, statsOf(dup, keys))).toEqual(inlineReference(dup, keys, statsOf(dup, keys)));
    const flat = rows(20, keys, 9).map(r => ({ ...r, regime_mag: 0 }));                // std 0 -> NaN z-scores
    expect(historicalClosestDistances(flat, keys, statsOf(flat, keys))).toEqual(inlineReference(flat, keys, statsOf(flat, keys)));
    const zeroStd = { ...statsOf(rows(20, keys, 2), keys), score: { mean: 1, std: 0 } }; // +/-Infinity z-scores
    const c = rows(20, keys, 2);
    expect(historicalClosestDistances(c, keys, zeroStd)).toEqual(inlineReference(c, keys, zeroStd));
    const missing = rows(20, keys, 4).map((r, i) => (i % 3 ? r : { ...r, bottom_score: undefined }));
    expect(historicalClosestDistances(missing, keys, statsOf(rows(20, keys, 4), keys)))
      .toEqual(inlineReference(missing, keys, statsOf(rows(20, keys, 4), keys)));
  });
});

describe('all three core prediction paths use the shared helper; no per-pair z-score loop remains', () => {
  const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');

  it('runPrediction / runEthPrediction / runLinkPrediction each call it with their own feature keys', () => {
    expect(src).toContain('historicalClosestDistances(candidates, FEATURE_KEYS, stats)');
    expect(src).toContain('historicalClosestDistances(candidates, ETH_FEATURE_KEYS, stats)');
    expect(src).toContain('historicalClosestDistances(candidates, LINK_FEATURE_KEYS, stats)');
    expect(src.match(/= historicalClosestDistances\(candidates,/g)).toHaveLength(3);
  });

  it('the O(n^2) loop no longer recomputes z-scores per pair anywhere in worker.js', () => {
    expect(src).not.toMatch(/const z2 = \(candidates\[j\]\[k\] - stats\[k\]\.mean\) \/ stats\[k\]\.std;/);
  });
});
