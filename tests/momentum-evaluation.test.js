import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const EVAL_FNS = [
  'getMomentumCalibration', 'getMomentumSelectionReport', 'groupMomentumEpisodes',
  'computeMomentumSelectionAggregate', 'computeFullMomentumAudit',
];
const EVAL_CONSTS = ['ANOMALY_AUDIT_MIN_SAMPLE_N', 'ANOMALY_AUDIT_MIN_EPISODES', 'ANOMALY_AUDIT_MAX_GAP_MS'];

function evalSource() {
  return extractFunctions(...EVAL_FNS) + '\n\n' + extractConstants(...EVAL_CONSTS);
}

// Fake DB: routes purely by table name in the SQL text, returns pre-seeded
// rows. Deliberately has NO .run() implementation at all -- any write
// attempt (INSERT/UPDATE/DELETE via .run()) throws immediately, which is
// itself the read-only regression test, same convention as
// anomaly-gate-report.test.js's makeFakeDb.
function makeFakeDb({ challengerRows = [], priceRows = [], momentumRows = [] } = {}) {
  const resolve = (sql) => {
    if (sql.includes('FROM challenger_predictions')) return { results: challengerRows };
    if (sql.includes('FROM selection_decisions_momentum')) return { results: momentumRows };
    return { results: priceRows };
  };
  return {
    prepare(sql) {
      return {
        // getMomentumCalibration's price-table lookup calls .all() directly
        // with no .bind() (no params to bind) -- getChallengerCalibration's
        // own price query does the same, so this fake must support both
        // call shapes, not just the bind().all() one.
        all: async () => resolve(sql),
        bind: (...args) => ({
          all: async () => resolve(sql),
        }),
      };
    },
  };
}

function challengerRow({ ts, p_up_momentum, realized_up, momentum_triggered = 0, price_at_prediction = 100 }) {
  return { ts, coin: 'BTC', horizon_hours: 24, p_up_momentum, realized_up, momentum_triggered, price_at_prediction, resolved_ts: ts + 1 };
}

function momentumRow({ prediction_ts, momentum_lca, momentum_rank, production_chosen_lca = null, momentum_n_matched = 12, total_scored = 7 }) {
  return {
    ts: prediction_ts + 100, coin: 'BTC', horizon_hours: 24, prediction_ts,
    momentum_p_up: 0.6, momentum_lca, momentum_n_matched, momentum_rank, total_scored,
    production_chosen_variant: 'original', production_chosen_p_up: 0.55, production_chosen_lca,
    k_sel: 10, scores_json: '[]',
  };
}

// =====================================================================
// A. Momentum prediction evaluation (getMomentumCalibration)
// =====================================================================
describe('getMomentumCalibration — sample size / empty handling', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(evalSource()); });

  it('zero resolved observations -> ok:true, explicit insufficient-sample note, no fabricated numbers', async () => {
    const db = makeFakeDb({ challengerRows: [] });
    const result = await scope.getMomentumCalibration({ DB: db }, 'BTC', 24);
    expect(result.ok).toBe(true);
    expect(result.n_resolved).toBe(0);
    expect(result.note).toMatch(/not enough/i);
    expect(result.accuracy_momentum).toBeUndefined();
  });

  it('fewer than 5 resolved rows -> same insufficient-sample shape, not treated as a failure', async () => {
    const rows = [0, 1, 2].map(i => challengerRow({ ts: i * 1000, p_up_momentum: 0.6, realized_up: 1 }));
    const db = makeFakeDb({ challengerRows: rows });
    const result = await scope.getMomentumCalibration({ DB: db }, 'BTC', 24);
    expect(result.ok).toBe(true);
    expect(result.n_resolved).toBe(3);
    expect(result.note).toMatch(/not enough/i);
  });
});

describe('getMomentumCalibration — accuracy / Brier / baseline correctness', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(evalSource()); });

  it('correctly computes accuracy and Brier for p_up_momentum over resolved rows', async () => {
    // 5 rows: momentum correct 4/5, actual up-rate 3/5.
    const rows = [
      challengerRow({ ts: 0, p_up_momentum: 0.8, realized_up: 1 }),   // correct
      challengerRow({ ts: 1, p_up_momentum: 0.7, realized_up: 1 }),   // correct
      challengerRow({ ts: 2, p_up_momentum: 0.3, realized_up: 0 }),   // correct
      challengerRow({ ts: 3, p_up_momentum: 0.6, realized_up: 0 }),   // wrong
      challengerRow({ ts: 4, p_up_momentum: 0.4, realized_up: 1 }),   // wrong
    ];
    const db = makeFakeDb({ challengerRows: rows, priceRows: [] });
    const result = await scope.getMomentumCalibration({ DB: db }, 'BTC', 24);
    expect(result.n_resolved).toBe(5);
    expect(result.accuracy_momentum).toBeCloseTo(3 / 5, 5);
    const expectedBrier = ((0.8 - 1) ** 2 + (0.7 - 1) ** 2 + (0.3 - 0) ** 2 + (0.6 - 0) ** 2 + (0.4 - 1) ** 2) / 5;
    expect(result.brier_momentum).toBeCloseTo(expectedBrier, 5);
    expect(result.historical_up_rate).toBeCloseTo(3 / 5, 5);
    expect(result.naive_baseline_accuracy).toBeCloseTo(3 / 5, 5);
  });

  it('ma_crossover_baseline reports not-enough-history when fewer than 5 MA-eligible points exist', async () => {
    const rows = [0, 1, 2, 3, 4].map(i => challengerRow({ ts: i * 1000, p_up_momentum: 0.6, realized_up: 1 }));
    const db = makeFakeDb({ challengerRows: rows, priceRows: [] });
    const result = await scope.getMomentumCalibration({ DB: db }, 'BTC', 24);
    expect(result.ma_crossover_baseline.n).toBe(0);
    expect(result.beats_ma_crossover_momentum).toBeNull();
  });

  it('unresolved rows are excluded by construction (query filters resolved_ts IS NOT NULL) — asserted on the SQL itself, matching getChallengerCalibration\'s own convention', () => {
    const src = extractFunctions('getMomentumCalibration');
    expect(src).toContain('resolved_ts IS NOT NULL');
    expect(src).toContain('p_up_momentum IS NOT NULL');
  });

  it('triggered_only breaks out momentum_triggered=1 rows separately, gated at n<5 same as the overall sample gate', async () => {
    const rows = [
      ...[0, 1, 2, 3, 4].map(i => challengerRow({ ts: i * 1000, p_up_momentum: 0.6, realized_up: 1, momentum_triggered: 0 })),
      challengerRow({ ts: 5000, p_up_momentum: 0.9, realized_up: 1, momentum_triggered: 1 }),
    ];
    const db = makeFakeDb({ challengerRows: rows, priceRows: [] });
    const result = await scope.getMomentumCalibration({ DB: db }, 'BTC', 24);
    expect(result.triggered_only.n).toBe(1);
    expect(result.triggered_only.note).toMatch(/fewer than 5/i);
    expect(result.triggered_only.accuracy_momentum).toBeUndefined();
  });

  it('NULL realized_up would break correctness silently if ever selected — confirms the query never selects unresolved rows in the first place (defense-in-depth check, mirrors resolve_pending-style fail-closed philosophy)', () => {
    const src = extractFunctions('getMomentumCalibration');
    expect(src).toMatch(/WHERE coin=\? AND horizon_hours=\? AND resolved_ts IS NOT NULL AND p_up_momentum IS NOT NULL/);
  });
});

// =====================================================================
// B. Momentum selection-experiment evaluation (getMomentumSelectionReport)
// =====================================================================
describe('getMomentumSelectionReport — empty table (current production state)', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(evalSource()); });

  it('zero selection_decisions_momentum rows -> ok:true, explicit no_resolved_observations aggregate, never throws, never fabricates a verdict', async () => {
    const db = makeFakeDb({ momentumRows: [] });
    const report = await scope.getMomentumSelectionReport({ DB: db }, 'BTC', 24);
    expect(report.ok).toBe(true);
    expect(report.raw_observation_count).toBe(0);
    expect(report.rows).toEqual([]);
    expect(report.episodes).toEqual([]);
    expect(report.aggregate.available).toBe(false);
    expect(report.aggregate.reason).toBe('no_resolved_observations');
    // Must not imply success or failure either way.
    expect(JSON.stringify(report).toLowerCase()).not.toMatch(/momentum (has |is )?(failed|succeeded|superior|inferior)/);
  });
});

describe('getMomentumSelectionReport — ranking, agreement-with-production, matched population', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(evalSource()); });

  it('exposes momentum_rank, total_scored, and momentum_lca vs production_chosen_lca side by side, read as persisted (no recomputation)', async () => {
    const rows = [momentumRow({ prediction_ts: 1000, momentum_lca: 0.72, momentum_rank: 1, production_chosen_lca: 0.6 })];
    const db = makeFakeDb({ momentumRows: rows });
    const report = await scope.getMomentumSelectionReport({ DB: db }, 'BTC', 24);
    expect(report.rows).toHaveLength(1);
    const r = report.rows[0];
    expect(r.momentum_rank).toBe(1);
    expect(r.total_scored).toBe(7);
    expect(r.momentum_lca).toBe(0.72);
    expect(r.production_chosen_lca).toBe(0.6);
    expect(r.momentum_outranks_production).toBe(true);
  });

  it('production_chosen_lca null (no production comparison available that cycle) -> momentum_outranks_production is null, never coerced to false', async () => {
    const rows = [momentumRow({ prediction_ts: 1000, momentum_lca: 0.72, momentum_rank: 1, production_chosen_lca: null })];
    const db = makeFakeDb({ momentumRows: rows });
    const report = await scope.getMomentumSelectionReport({ DB: db }, 'BTC', 24);
    expect(report.rows[0].momentum_outranks_production).toBeNull();
  });

  it('aggregate correctly counts outrank-rate and rank1-rate across multiple rows', async () => {
    const rows = [
      momentumRow({ prediction_ts: 1000, momentum_lca: 0.72, momentum_rank: 1, production_chosen_lca: 0.6 }),  // outranks, rank1
      momentumRow({ prediction_ts: 2000, momentum_lca: 0.50, momentum_rank: 3, production_chosen_lca: 0.6 }),  // does not outrank
      momentumRow({ prediction_ts: 3000, momentum_lca: 0.65, momentum_rank: 2, production_chosen_lca: 0.6 }),  // outranks, not rank1
    ];
    const db = makeFakeDb({ momentumRows: rows });
    const report = await scope.getMomentumSelectionReport({ DB: db }, 'BTC', 24);
    expect(report.aggregate.n_observations).toBe(3);
    expect(report.aggregate.n_scored_with_production_comparison).toBe(3);
    expect(report.aggregate.momentum_outranks_production_lca_count).toBe(2);
    // Values are rounded to 3 decimals by the implementation (toFixed(3)),
    // same convention as computeAnomalyGateAggregate's own rate fields --
    // compare against that same rounded precision, not raw division.
    expect(report.aggregate.momentum_outranks_production_lca_rate).toBeCloseTo(2 / 3, 2);
    expect(report.aggregate.momentum_rank1_count).toBe(1);
    expect(report.aggregate.momentum_rank1_rate).toBeCloseTo(1 / 3, 2);
  });

  it('outcome-based accuracy is explicitly reported unavailable, never approximated by joining another table', async () => {
    const rows = [momentumRow({ prediction_ts: 1000, momentum_lca: 0.72, momentum_rank: 1, production_chosen_lca: 0.6 })];
    const db = makeFakeDb({ momentumRows: rows });
    const report = await scope.getMomentumSelectionReport({ DB: db }, 'BTC', 24);
    expect(report.aggregate.outcome_based_accuracy_note).toMatch(/unavailable/i);
  });

  it('never calls .run() -- a write attempt on this fake DB throws, proving the report performs reads only', async () => {
    const throwingDb = {
      prepare(sql) {
        return { bind: () => ({ all: async () => ({ results: [] }), run: async () => { throw new Error('unexpected write'); } }) };
      },
    };
    await expect(scope.getMomentumSelectionReport({ DB: throwingDb }, 'BTC', 24)).resolves.toBeDefined();
  });
});

describe('groupMomentumEpisodes — adjacency grouping and insufficient-sample interaction', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(evalSource()); });

  it('rows within ANOMALY_AUDIT_MAX_GAP_MS of each other collapse into one episode', () => {
    const gap = scope.ANOMALY_AUDIT_MAX_GAP_MS;
    const rows = [
      { prediction_ts: 1000, momentum_outranks_production: true },
      { prediction_ts: 1000 + gap - 1, momentum_outranks_production: false },
    ];
    const episodes = scope.groupMomentumEpisodes(rows);
    expect(episodes).toHaveLength(1);
    expect(episodes[0].n_cycles).toBe(2);
    expect(episodes[0].n_momentum_outranks_production).toBe(1);
  });

  it('rows farther apart than ANOMALY_AUDIT_MAX_GAP_MS (the realistic once-daily-cron case) form separate episodes', () => {
    const gap = scope.ANOMALY_AUDIT_MAX_GAP_MS;
    const oneDayMs = 24 * 3600 * 1000;
    expect(oneDayMs).toBeGreaterThan(gap);
    const rows = [
      { prediction_ts: 1000, momentum_outranks_production: false },
      { prediction_ts: 1000 + oneDayMs, momentum_outranks_production: true },
    ];
    const episodes = scope.groupMomentumEpisodes(rows);
    expect(episodes).toHaveLength(2);
    expect(episodes.every(e => e.n_cycles === 1)).toBe(true);
  });

  it('insufficient_sample is set once either the row-count or episode-count minimum is not met', async () => {
    // 2 rows, both far apart (2 episodes) -- below MIN_SAMPLE_N=5 and below MIN_EPISODES=3.
    const rows = [
      momentumRow({ prediction_ts: 1000, momentum_lca: 0.6, momentum_rank: 2, production_chosen_lca: 0.6 }),
      momentumRow({ prediction_ts: 1000 + 24 * 3600 * 1000, momentum_lca: 0.6, momentum_rank: 2, production_chosen_lca: 0.6 }),
    ];
    const db = makeFakeDb({ momentumRows: rows });
    const report = await scope.getMomentumSelectionReport({ DB: db }, 'BTC', 24);
    expect(report.aggregate.insufficient_sample).toBe(true);
    expect(report.aggregate.note).toMatch(/insufficient_sample=true/);
  });
});

// =====================================================================
// C. Full audit orchestration (computeFullMomentumAudit) — endpoint's
// no-params branch
// =====================================================================
describe('computeFullMomentumAudit — coin scoping and empty-state propagation', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(evalSource()); });

  it('prediction_evaluation covers all 3 coins x 2 horizons; selection_evaluation covers BTC only', async () => {
    const db = makeFakeDb({ challengerRows: [], momentumRows: [] });
    const audit = await scope.computeFullMomentumAudit({ DB: db });
    expect(audit.ok).toBe(true);
    expect(Object.keys(audit.prediction_evaluation).sort()).toEqual(['BTC', 'ETH', 'LINK']);
    expect(Object.keys(audit.prediction_evaluation.BTC).map(Number).sort()).toEqual([12, 24]);
    expect(Object.keys(audit.selection_evaluation)).toEqual(['BTC']);
    expect(Object.keys(audit.selection_evaluation.BTC).map(Number).sort()).toEqual([12, 24]);
  });

  it('a per-coin/horizon D1 failure is caught and reported inline, not thrown, so one bad combination cannot break the whole audit', async () => {
    const throwingDb = { prepare() { throw new Error('simulated D1 failure'); } };
    const audit = await scope.computeFullMomentumAudit({ DB: throwingDb });
    expect(audit.ok).toBe(true);
    expect(audit.prediction_evaluation.BTC[24].ok).toBe(false);
    expect(audit.prediction_evaluation.BTC[24].error).toMatch(/simulated D1 failure/);
    expect(audit.selection_evaluation.BTC[24].ok).toBe(false);
  });

  it('reuses the exact same min_sample_n / min_episodes constants as Experiment 2, no new threshold invented', async () => {
    const db = makeFakeDb({});
    const audit = await scope.computeFullMomentumAudit({ DB: db });
    expect(audit.min_sample_n).toBe(scope.ANOMALY_AUDIT_MIN_SAMPLE_N);
    expect(audit.min_episodes).toBe(scope.ANOMALY_AUDIT_MIN_EPISODES);
  });
});

// =====================================================================
// D. Production selection remains untouched
// =====================================================================
describe('LR-3 evaluation layer never touches production selection', () => {
  it('none of the new evaluation functions reference selectBestVariant, decideSelection, computeLcaScore, or logMomentumSelectionExperiment', () => {
    const src = evalSource();
    expect(src).not.toContain('selectBestVariant(');
    expect(src).not.toContain('decideSelection(');
    expect(src).not.toContain('computeLcaScore(');
    expect(src).not.toContain('logMomentumSelectionExperiment(');
  });

  it('none of the new evaluation functions call .run() anywhere in their source', () => {
    const src = evalSource();
    expect(src).not.toMatch(/\.run\(\)/);
  });
});

// =====================================================================
// C. Endpoint: GET /research/momentum -- source-level checks, same
// string-matching-around-the-route-index convention as
// anomaly-gate-report.test.js's route block (no fetch()/Request
// simulation elsewhere in this repo's test suite).
// =====================================================================
describe('GET /research/momentum route', () => {
  it('the route block requires GET, is scoped by coin+horizon, and exposes both evaluations', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf("'/research/momentum'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = src.slice(idx, idx + 2000);
    expect(nearby).toContain("request.method === 'GET'");
    expect(nearby).toContain('hasValidCoin && hasValidHorizon');
    expect(nearby).toContain('getMomentumCalibration');
    expect(nearby).toContain('getMomentumSelectionReport');
    expect(nearby).toContain('computeFullMomentumAudit');
    expect(nearby).toContain('prediction_evaluation');
    expect(nearby).toContain('selection_evaluation');
  });

  it('the route block performs no writes -- no .run(), no INSERT/UPDATE/DELETE', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf("'/research/momentum'");
    const nearby = src.slice(idx, idx + 2000);
    expect(nearby).not.toContain('.run(');
    expect(nearby).not.toMatch(/INSERT INTO|UPDATE \w+ SET|DELETE FROM/);
  });

  it('a coin other than BTC explicitly marks selection_evaluation unavailable rather than fabricating a BTC-only result for it', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf("'/research/momentum'");
    const nearby = src.slice(idx, idx + 2000);
    expect(nearby).toContain("coin_not_in_experiment");
    expect(nearby).toContain("coinParam === 'BTC'");
  });

  it('the route never defaults coin or horizon for the single-combination branch -- both must be explicitly valid, same discipline as /research/anomaly-gate', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf("'/research/momentum'");
    const nearby = src.slice(idx, idx + 700);
    expect(nearby).not.toMatch(/:\s*'BTC'/);
    expect(nearby).not.toMatch(/:\s*24\b/);
  });

  it('never calls selectBestVariant, decideSelection, or logMomentumSelectionExperiment from within the route block', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf("'/research/momentum'");
    const nearby = src.slice(idx, idx + 2000);
    expect(nearby).not.toMatch(/selectBestVariant\(|decideSelection\(|logMomentumSelectionExperiment\(/);
  });
});
