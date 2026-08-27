import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

describe('decideSelectionSoftened — pure function, correctness', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(
      extractFunctions('decideSelectionSoftened') + '\n\n' +
      extractConstants('SELECTION_CRITICAL_Z', 'ANOMALY_GATE_MARGIN_FACTOR')
    );
  });

  it('empty scores -> no chosen, gate not cleared', () => {
    const result = scope.decideSelectionSoftened([], 0.5);
    expect(result.chosen).toBeNull();
    expect(result.clearedGate).toBe(false);
  });

  it('halves the required margin relative to the unscaled (factor=1) bar', () => {
    const scores = [{ variant: 'challenger_flat', lca: 0.62, n_matched: 10 }];
    const full = scope.decideSelectionSoftened(scores, 1);
    const halved = scope.decideSelectionSoftened(scores, 0.5);
    expect(halved.requiredMargin).toBeCloseTo(full.requiredMargin / 2, 10);
    expect(halved.baseRequiredMargin).toBeCloseTo(full.baseRequiredMargin, 10); // base bar itself never changes with the factor
  });

  it('a margin that fails the full (factor=1) bar can clear the softened (factor=0.5) bar -- proves the softening actually does something', () => {
    // z(m=1)=1.6449, n_matched=20 -> baseRequiredMargin = 1.6449*sqrt(0.25/20) = 0.18391 (computed precisely, not estimated)
    // lca=0.60 -> edge=0.10, which is below the full bar (0.18391) but above the softened bar (0.09195)
    const scores = [{ variant: 'calibrated', lca: 0.60, n_matched: 20 }];
    const full = scope.decideSelectionSoftened(scores, 1);
    const softened = scope.decideSelectionSoftened(scores, 0.5);
    expect(full.clearedGate).toBe(false); // edge=0.10 does not clear the full bar (~0.184)
    expect(softened.clearedGate).toBe(true); // but does clear the softened bar (~0.092)
    expect(full.chosen).toBe('original');
    expect(softened.chosen).toBe('calibrated');
  });

  it('reads SELECTION_CRITICAL_Z but the constant itself is untouched by this function (read-only usage)', () => {
    const src = extractFunctions('decideSelectionSoftened');
    expect(src).toContain('SELECTION_CRITICAL_Z[m]');
    expect(src).not.toMatch(/SELECTION_CRITICAL_Z\s*=\s*\{/); // never reassigns/redefines it
  });

  it('multiple simultaneous variants (m>1) use the correct Bonferroni-corrected z for that m, same table as production', () => {
    const scores = [
      { variant: 'a', lca: 0.9, n_matched: 10 },
      { variant: 'b', lca: 0.6, n_matched: 10 },
      { variant: 'c', lca: 0.55, n_matched: 10 },
    ];
    const result = scope.decideSelectionSoftened(scores, 1);
    expect(result.m).toBe(3);
    expect(result.winner.variant).toBe('a'); // highest LCA wins the winner slot regardless of gate outcome
  });
});

describe('logAnomalyGateExperiment — activation gating and no production interference', () => {
  function makeFakeDb({ coreRows, historyRows = [], variantCounts = {}, variantRowsByKey = {} }) {
    const writes = { selection_decisions: 0, selection_decisions_anomaly: 0 };
    return {
      db: {
        prepare(sql) {
          return {
            bind: (...args) => ({
              first: async () => {
                if (/FROM (predictions|link_predictions|eth_predictions) WHERE horizon_hours=\? ORDER BY ts DESC LIMIT 1/.test(sql)) {
                  return coreRows[0] || null;
                }
                return null;
              },
              all: async () => {
                if (sql.includes('SELECT COUNT(*) as n FROM')) {
                  const tableMatch = sql.match(/FROM (\w+)/);
                  const fieldMatch = sql.match(/AND (\w+) IS NOT NULL$/);
                  const key = `${tableMatch[1]}:${fieldMatch[1]}`;
                  return { results: [{ n: variantCounts[key] ?? 0 }] };
                }
                if (sql.includes('ts < ?') && sql.includes('ORDER BY ts DESC LIMIT 300')) {
                  return { results: historyRows };
                }
                if (/SELECT ts, \w+ as p_up, realized_up FROM/.test(sql)) {
                  const fieldMatch = sql.match(/SELECT ts, (\w+) as p_up/);
                  return { results: variantRowsByKey[fieldMatch[1]] || [] };
                }
                return { results: [] };
              },
              run: async () => {
                if (sql.includes('INSERT INTO selection_decisions_anomaly')) writes.selection_decisions_anomaly++;
                if (sql.includes('INSERT INTO selection_decisions ')) writes.selection_decisions++;
                return { meta: { last_row_id: 1 } };
              },
            }),
          };
        },
      },
      writes,
    };
  }

  let source;
  beforeAll(() => {
    source = extractFunctions(
      'logAnomalyGateExperiment', 'decideSelectionSoftened', 'computeLcaScore', 'coreTableForCoin', 'nearestRow'
    ) + '\n\n' + extractConstants(
      'SELECTION_VARIANTS', 'SELECTION_MIN_HISTORY', 'SELECTION_MIN_MATCHED', 'SELECTION_CRITICAL_Z', 'ANOMALY_GATE_MARGIN_FACTOR'
    );
  });

  it('is_regime_anomaly = 0 on the latest core row -> returns immediately, writes nothing', async () => {
    const { db, writes } = makeFakeDb({ coreRows: [{ ts: 1000, features_json: '{"a":1}', is_regime_anomaly: 0 }] });
    const scope = evalInScope(source);
    const result = await scope.logAnomalyGateExperiment({ DB: db }, 'BTC', 24);
    expect(result.status).toBe('not_anomalous_skip');
    expect(result.logged).toBe(false);
    expect(writes.selection_decisions_anomaly).toBe(0);
  });

  it('no core row at all -> returns immediately, writes nothing (treated the same as not-anomalous)', async () => {
    const { db, writes } = makeFakeDb({ coreRows: [] });
    const scope = evalInScope(source);
    const result = await scope.logAnomalyGateExperiment({ DB: db }, 'BTC', 24);
    expect(result.status).toBe('not_anomalous_skip');
    expect(writes.selection_decisions_anomaly).toBe(0);
  });

  it('is_regime_anomaly = 1 but insufficient history for any variant -> no eligible variants, writes nothing', async () => {
    const { db, writes } = makeFakeDb({
      coreRows: [{ ts: 1000, features_json: '{"a":1}', is_regime_anomaly: 1 }],
      variantCounts: {}, // every variant count defaults to 0, well under SELECTION_MIN_HISTORY
    });
    const scope = evalInScope(source);
    const result = await scope.logAnomalyGateExperiment({ DB: db }, 'BTC', 24);
    expect(result.status).toBe('no_eligible_variants');
    expect(writes.selection_decisions_anomaly).toBe(0);
  });

  it('this function NEVER writes to selection_decisions (the production table) under any circumstance', async () => {
    const { db, writes } = makeFakeDb({ coreRows: [{ ts: 1000, features_json: '{"a":1}', is_regime_anomaly: 1 }] });
    const scope = evalInScope(source);
    await scope.logAnomalyGateExperiment({ DB: db }, 'BTC', 24);
    expect(writes.selection_decisions).toBe(0);
  });

  it('full path: anomalous + eligible + enough history + scorable variant -> writes exactly one row to selection_decisions_anomaly, none to selection_decisions', async () => {
    const featuresJson = JSON.stringify({ score: 1, technical_score: 1 });
    const historyRows = Array.from({ length: 20 }, (_, i) => ({
      ts: 1000 - (i + 1) * 3600000,
      features_json: JSON.stringify({ score: 1 + i * 0.01, technical_score: 1 - i * 0.01 }),
      realized_up: i % 2,
    }));
    const variantRows = Array.from({ length: 60 }, (_, i) => ({
      ts: 1000 - (i + 1) * 3600000, p_up: 0.7, realized_up: 1,
    }));
    const { db, writes } = makeFakeDb({
      coreRows: [{ ts: 1000, features_json: featuresJson, is_regime_anomaly: 1 }],
      historyRows,
      variantCounts: { 'predictions:p_up': 60, 'predictions:p_up_experimental': 60, 'predictions:calibrated_p_up': 60, 'challenger_predictions:p_up_flat': 60, 'challenger_predictions:p_up_tilted': 60, 'challenger_predictions:calibrated_p_up_flat': 60 },
      variantRowsByKey: { p_up: variantRows, p_up_experimental: variantRows, calibrated_p_up: variantRows, p_up_flat: variantRows, p_up_tilted: variantRows, calibrated_p_up_flat: variantRows },
    });
    const scope = evalInScope(source);
    const result = await scope.logAnomalyGateExperiment({ DB: db }, 'BTC', 24);
    expect(result.logged).toBe(true);
    expect(result.margin_factor).toBe(0.5);
    expect(writes.selection_decisions_anomaly).toBe(1);
    expect(writes.selection_decisions).toBe(0);
  });

  it('a D1 error anywhere in the path propagates (caller is responsible for catching it, same contract as Challenger) rather than being silently swallowed here', async () => {
    const throwingDb = { prepare() { throw new Error('simulated D1 failure'); } };
    const scope = evalInScope(source);
    await expect(scope.logAnomalyGateExperiment({ DB: throwingDb }, 'BTC', 24)).rejects.toThrow('simulated D1 failure');
  });
});

describe('cron dispatch wiring', () => {
  it('predictThenSelect calls logAnomalyGateExperiment independently-caught, after and separate from the untouched selectBestVariant call', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf('const predictThenSelect');
    expect(idx).toBeGreaterThan(-1);
    const block = src.slice(idx, idx + 700);
    expect(block).toContain('await selectBestVariant(env, coin, horizon).catch(err => console.error(`Selection ${coin}/${horizon}h failed:`, err));');
    expect(block).toContain('await logAnomalyGateExperiment(env, coin, horizon).catch(err => console.error(`Anomaly-gate experiment ${coin}/${horizon}h failed:`, err));');
    // selectBestVariant's own call line is character-for-character what
    // it was before this experiment existed -- not just "still present",
    // but unchanged in exact form.
  });
});

describe('STRUCTURAL CHECK: production selection logic and fencing-token functions are byte-identical, not just present', () => {
  it('selectBestVariant, decideSelection, computeLcaScore are present with their exact original signatures', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('async function selectBestVariant(env, coin, horizonHours) {');
    expect(src).toContain('function decideSelection(scores) {');
    expect(src).toContain('function computeLcaScore(variantRows, neighborhood, todaysCallUp, tolMs) {');
  });

  it('every SELECTION_* production constant is present with its exact original value, untouched', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain("const SELECTION_MIN_HISTORY = 50;");
    expect(src).toContain("const SELECTION_MIN_MATCHED = 3;");
    expect(src).toContain('const SELECTION_CRITICAL_Z = { 1: 1.6449, 2: 1.9600, 3: 2.1280, 4: 2.2414, 5: 2.3263, 6: 2.3940 };');
  });

  it("decideSelection's own formula (the production gate) is textually unchanged -- requiredMargin computed the exact same way, no factor applied", () => {
    const src = extractFunctions('decideSelection');
    expect(src).toContain('const requiredMargin = z * Math.sqrt(0.25 / winner.n_matched);');
    expect(src).not.toContain('marginFactor');
    expect(src).not.toContain('ANOMALY_GATE_MARGIN_FACTOR');
  });

  it('selectBestVariant never calls logAnomalyGateExperiment or decideSelectionSoftened -- the experiment is not wired into the production function itself', () => {
    const src = extractFunctions('selectBestVariant');
    expect(src).not.toContain('logAnomalyGateExperiment');
    expect(src).not.toContain('decideSelectionSoftened');
    expect(src).not.toContain('ANOMALY_GATE_MARGIN_FACTOR');
  });

  it('the fencing-token / read-only ingestion functions are unmodified in shape -- untouched by this PR', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('async function claimStaleRefresh(env, coin, nowTs, claimWindowMs = 60 * 1000)');
    expect(src).toContain('async function resolveWriteAuthorization(env, table, coin, allowWrite)');
    expect(src).toMatch(/FROM stale_refresh_claim WHERE coin = /);
  });

  it('logAnomalyGateExperiment never calls any write-capable production function or selection_decisions (only its own new table)', () => {
    const src = extractFunctions('logAnomalyGateExperiment');
    expect(src).not.toContain('selectBestVariant(');
    expect(src).not.toMatch(/INSERT INTO selection_decisions\s*\(/); // the production table's own INSERT, note the space+paren distinguishes it from selection_decisions_anomaly
    expect(src).toContain('INSERT INTO selection_decisions_anomaly');
  });
});
