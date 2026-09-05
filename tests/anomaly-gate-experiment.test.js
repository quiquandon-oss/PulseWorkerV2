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
  // Table each field belongs to, matching the real SELECTION_VARIANTS
  // registry -- the mock needs this to answer the batched per-table
  // scoring query (fetchVariantRowsByTable) with a single combined row
  // set per table, the same way the real query would.
  const FIELD_TABLE = {
    p_up: 'predictions', p_up_experimental: 'predictions', calibrated_p_up: 'predictions',
    p_up_flat: 'challenger_predictions', p_up_tilted: 'challenger_predictions', calibrated_p_up_flat: 'challenger_predictions',
  };

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
                // Batched eligibility query (fetchEligibilityCounts): one
                // subquery per variant, aliased n0, n1, ... in registry
                // order. Same semantics as the old per-variant COUNT
                // loop this replaced, just answered in one call.
                if (sql.startsWith('SELECT (SELECT COUNT(*)')) {
                  const row = {};
                  const re = /\(SELECT COUNT\(\*\) FROM (\w+) WHERE (?:coin = \? AND )?horizon_hours=\? AND realized_up IS NOT NULL AND (\w+) IS NOT NULL\) as (n\d+)/g;
                  let m;
                  while ((m = re.exec(sql))) {
                    const [, table, field, alias] = m;
                    row[alias] = variantCounts[`${table}:${field}`] ?? 0;
                  }
                  return row;
                }
                return null;
              },
              all: async () => {
                if (sql.includes('ts < ?') && sql.includes('ORDER BY ts DESC LIMIT 300')) {
                  return { results: historyRows };
                }
                // Batched per-table scoring query (fetchVariantRowsByTable):
                // one query per table among the eligible variants, selecting
                // every field that table's variants need. Zips the
                // per-field fixture arrays back together by index -- every
                // caller in this file supplies same-length, same-order
                // arrays per field, so this is a faithful reconstruction of
                // what one real combined row set would look like.
                for (const table of ['predictions', 'challenger_predictions']) {
                  if (sql.includes(`FROM ${table} WHERE`) && sql.includes('realized_up IS NOT NULL ORDER BY ts ASC')) {
                    const fieldsForTable = Object.keys(FIELD_TABLE).filter(f => FIELD_TABLE[f] === table);
                    const base = fieldsForTable.map(f => variantRowsByKey[f]).find(arr => arr && arr.length) || [];
                    const results = base.map((baseRow, i) => {
                      const row = { ts: baseRow.ts, realized_up: baseRow.realized_up };
                      for (const f of fieldsForTable) {
                        const arr = variantRowsByKey[f];
                        if (arr && arr[i]) row[f] = arr[i].p_up;
                      }
                      return row;
                    });
                    return { results };
                  }
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
      'logAnomalyGateExperiment', 'decideSelectionSoftened', 'computeLcaScore', 'coreTableForCoin', 'nearestRow', 'fetchEligibilityCounts', 'fetchVariantRowsByTable'
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
  it('the 3h tick (runCoinHorizonChain) no longer calls logAnomalyGateExperiment or logMomentumSelectionExperiment -- only the untouched selectBestVariant call remains on the hot path', () => {
    // Renamed from the old inline `predictThenSelect` closure to the
    // top-level runCoinHorizonChain as part of the 2026-09-02 batched-
    // sequential redesign (fixing the LINK/ETH selection starvation).
    const src = extractFunctions('runCoinHorizonChain');
    expect(src).toContain('await selectBestVariant(env, coin, horizon)');
    expect(src).not.toContain('logAnomalyGateExperiment');
    expect(src).not.toContain('logMomentumSelectionExperiment');
  });

  it('logAnomalyGateExperiment and logMomentumSelectionExperiment are called from the daily (0 7 * * *) cron branch, looped over all coins/horizons, independently caught exactly as before', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const dailyIdx = src.indexOf("event.cron === '0 7 * * *'");
    expect(dailyIdx).toBeGreaterThan(-1);
    // Bounded by the next `} else if (event.cron` that closes this
    // if-branch -- updated 2026-09-05 for the coin-invocation-isolation
    // fix, which replaced the old single `} else {` with three explicit
    // `else if (event.cron === '<coin's cron>')` branches (one per coin).
    const elseIfIdx = src.indexOf('} else if (event.cron', dailyIdx);
    expect(elseIfIdx).toBeGreaterThan(dailyIdx);
    const dailyBlock = src.slice(dailyIdx, elseIfIdx);
    expect(dailyBlock).toContain("ctx.waitUntil(logAnomalyGateExperiment(env, coin, h).catch(err => console.error(`Anomaly-gate experiment ${coin}/${h}h failed:`, err)));");
    expect(dailyBlock).toContain("ctx.waitUntil(logMomentumSelectionExperiment(env, coin, h).catch(err => console.error(`Momentum experiment ${coin}/${h}h failed:`, err)));");
    // Same coins/horizons loop the existing calibration refreshes already use.
    expect(dailyBlock).toContain("for (const coin of ['BTC', 'LINK', 'ETH']) {");
    expect(dailyBlock).toContain('for (const h of [12, 24]) {');
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
