// Focused regression tests for the Experiment 3 scope change (2026-09-02):
// ETH removed from the challenger_momentum research experiment, while
// remaining fully operational in normal production predictions,
// Challenger, SELECTION_VARIANTS, selection_decisions, and the cron.
//
// This file tests ONLY the scope boundary itself. It does not re-test
// logMomentumSelectionExperiment's own scoring/ranking logic for BTC/LINK
// (already covered exhaustively in challenger-momentum-experiment.test.js,
// which is untouched except for one extraction-list update), and does not
// re-test production selection logic (covered in challenger-cron-stall-fix
// .test.js, anomaly-gate-experiment.test.js, etc., also untouched).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

function buildScope() {
  const source = extractFunctions(
    'logMomentumSelectionExperiment', 'computeLcaScore', 'coreTableForCoin', 'nearestRow',
    'fetchEligibilityCounts', 'fetchVariantRowsByTable'
  ) + '\n\n' + extractConstants(
    'SELECTION_VARIANTS', 'MOMENTUM_EXPERIMENT_VARIANT', 'MOMENTUM_EXPERIMENT_COINS',
    'SELECTION_MIN_HISTORY', 'SELECTION_MIN_MATCHED'
  );
  return evalInScope(source);
}

describe('MOMENTUM_EXPERIMENT_COINS — the scope registry itself', () => {
  it('contains exactly BTC and LINK, not ETH', () => {
    const scope = buildScope();
    expect(scope.MOMENTUM_EXPERIMENT_COINS).toEqual(['BTC', 'LINK']);
  });
});

describe('ETH cannot enter the momentum experiment, for either horizon', () => {
  it('returns immediately with coin_not_in_experiment for ETH/24h, before touching env.DB at all', async () => {
    const scope = buildScope();
    let dbTouched = false;
    const db = { prepare() { dbTouched = true; throw new Error('env.DB.prepare should never be called for ETH'); } };
    const result = await scope.logMomentumSelectionExperiment({ DB: db }, 'ETH', 24);
    expect(result).toEqual({ ok: true, status: 'coin_not_in_experiment', logged: false });
    expect(dbTouched).toBe(false);
  });

  it('returns immediately with coin_not_in_experiment for ETH/12h, before touching env.DB at all', async () => {
    const scope = buildScope();
    let dbTouched = false;
    const db = { prepare() { dbTouched = true; throw new Error('env.DB.prepare should never be called for ETH'); } };
    const result = await scope.logMomentumSelectionExperiment({ DB: db }, 'ETH', 12);
    expect(result).toEqual({ ok: true, status: 'coin_not_in_experiment', logged: false });
    expect(dbTouched).toBe(false);
  });

  it('BTC and LINK are unaffected by the guard -- they proceed past it and do touch env.DB', async () => {
    const scope = buildScope();
    for (const coin of ['BTC', 'LINK']) {
      let dbTouched = false;
      const db = {
        prepare(sql) {
          dbTouched = true;
          return { bind: () => ({ first: async () => ({}), all: async () => ({ results: [] }), run: async () => ({}) }) };
        },
      };
      const result = await scope.logMomentumSelectionExperiment({ DB: db }, coin, 24);
      expect(dbTouched).toBe(true);
      expect(result.status).not.toBe('coin_not_in_experiment');
    }
  });
});

describe('ETH cannot write selection_decisions_momentum', () => {
  it('no INSERT statement is ever prepared for ETH, on either horizon', async () => {
    const scope = buildScope();
    const preparedStatements = [];
    const db = {
      prepare(sql) {
        preparedStatements.push(sql);
        return { bind: () => ({ first: async () => null, all: async () => ({ results: [] }), run: async () => ({}) }) };
      },
    };
    await scope.logMomentumSelectionExperiment({ DB: db }, 'ETH', 24);
    await scope.logMomentumSelectionExperiment({ DB: db }, 'ETH', 12);
    expect(preparedStatements).toHaveLength(0);
    expect(preparedStatements.some(sql => sql.includes('INSERT INTO selection_decisions_momentum'))).toBe(false);
  });
});

describe('ETH cannot be ranked by the momentum experiment', () => {
  it('the result for ETH never contains momentum_rank, momentum_lca, or total_scored -- the ranking computation is never reached', async () => {
    const scope = buildScope();
    const db = { prepare() { throw new Error('should be unreachable'); } };
    const result = await scope.logMomentumSelectionExperiment({ DB: db }, 'ETH', 24);
    expect(result.momentum_rank).toBeUndefined();
    expect(result.momentum_lca).toBeUndefined();
    expect(result.total_scored).toBeUndefined();
  });
});

describe('production isolation: ETH remains fully present everywhere Experiment 3 must not touch', () => {
  it('ETH is still a full entry in SELECTION_VARIANTS, with all 6 (not 5, not 7) production variants -- unaffected by the Experiment 3 guard', () => {
    const scope = evalInScope(extractConstants('SELECTION_VARIANTS'));
    expect(scope.SELECTION_VARIANTS.ETH).toBeDefined();
    expect(scope.SELECTION_VARIANTS.ETH).toHaveLength(6);
    const keys = scope.SELECTION_VARIANTS.ETH.map(v => v.key);
    expect(keys).toEqual(['original', 'experimental', 'calibrated', 'challenger_flat', 'challenger_tilted', 'challenger_calibrated']);
    expect(keys).not.toContain('challenger_momentum');
  });

  it('MOMENTUM_EXPERIMENT_VARIANT is not, and was never, part of SELECTION_VARIANTS.ETH or any coin\'s registry (pre-existing invariant, reconfirmed)', () => {
    const scope = evalInScope(extractConstants('SELECTION_VARIANTS', 'MOMENTUM_EXPERIMENT_VARIANT'));
    for (const coin of ['BTC', 'LINK', 'ETH']) {
      expect(scope.SELECTION_VARIANTS[coin]).not.toContainEqual(scope.MOMENTUM_EXPERIMENT_VARIANT);
    }
  });

  it('selectBestVariant, runChallengerPrediction, ethPredictAndLog, and the cron batching for ETH are byte-identical to before this change -- this diff touched nothing else', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('async function selectBestVariant(env, coin, horizonHours) {');
    expect(src).toContain('async function ethPredictAndLog(env, horizonHours = 24, { allowWrite = false } = {}) {');
    expect(src).toContain("[{ predictFn: ethPredictAndLog, coin: 'ETH', horizon: 24 }, { predictFn: ethPredictAndLog, coin: 'ETH', horizon: 12 }]");
  });
});
