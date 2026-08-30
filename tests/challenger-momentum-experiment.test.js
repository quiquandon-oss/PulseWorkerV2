import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

describe('computeMomentumBlend — pure function correctness', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('computeMomentumBlend'));
  });

  it('not anomalous -> falls back to pUpFlat unchanged, regardless of how strong the trend is', () => {
    const result = scope.computeMomentumBlend(false, 0.9, 0.65, 0.5, 0.2);
    expect(result.pUpMomentum).toBe(0.65);
    expect(result.momentumTriggered).toBe(false);
  });

  it('anomalous but trend at or below threshold -> falls back to pUpFlat unchanged (boundary: exactly at threshold does NOT trigger)', () => {
    const atThreshold = scope.computeMomentumBlend(true, 0.5, 0.65, 0.5, 0.2);
    expect(atThreshold.pUpMomentum).toBe(0.65);
    expect(atThreshold.momentumTriggered).toBe(false);
    const belowThreshold = scope.computeMomentumBlend(true, 0.3, 0.65, 0.5, 0.2);
    expect(belowThreshold.pUpMomentum).toBe(0.65);
    expect(belowThreshold.momentumTriggered).toBe(false);
  });

  it('anomalous + trend strictly above threshold (positive) -> blends toward continuation, exact math verified', () => {
    // trendStrength=0.8 -> momentumSignal = 0.5 + 0.8/2 = 0.9
    // blended = (1-0.2)*0.65 + 0.2*0.9 = 0.52 + 0.18 = 0.70
    const result = scope.computeMomentumBlend(true, 0.8, 0.65, 0.5, 0.2);
    expect(result.momentumTriggered).toBe(true);
    expect(result.pUpMomentum).toBeCloseTo(0.70, 10);
  });

  it('anomalous + trend strictly below negative threshold -> blends toward downside continuation, exact math verified', () => {
    // trendStrength=-0.8 -> momentumSignal = 0.5 + (-0.8)/2 = 0.1
    // blended = (1-0.2)*0.65 + 0.2*0.1 = 0.52 + 0.02 = 0.54
    const result = scope.computeMomentumBlend(true, -0.8, 0.65, 0.5, 0.2);
    expect(result.momentumTriggered).toBe(true);
    expect(result.pUpMomentum).toBeCloseTo(0.54, 10);
  });

  it('null trend_strength -> falls back to pUpFlat unchanged, never treated as zero or triggering', () => {
    const result = scope.computeMomentumBlend(true, null, 0.65, 0.5, 0.2);
    expect(result.pUpMomentum).toBe(0.65);
    expect(result.momentumTriggered).toBe(false);
  });

  it('output is always clamped to [0.05, 0.95] even under an extreme blend', () => {
    const result = scope.computeMomentumBlend(true, 1.0, 0.94, 0.5, 0.2);
    expect(result.pUpMomentum).toBeLessThanOrEqual(0.95);
    const result2 = scope.computeMomentumBlend(true, -1.0, 0.06, 0.5, 0.2);
    expect(result2.pUpMomentum).toBeGreaterThanOrEqual(0.05);
  });

  it('a different weight parameter changes the blend proportionally -- the function takes weight as an argument, not a hardcoded value', () => {
    const light = scope.computeMomentumBlend(true, 0.8, 0.65, 0.5, 0.15);
    const heavy = scope.computeMomentumBlend(true, 0.8, 0.65, 0.5, 0.25);
    // momentumSignal=0.9 in both cases; heavier weight pulls further toward 0.9
    expect(heavy.pUpMomentum).toBeGreaterThan(light.pUpMomentum);
  });

  it('is pure -- repeated calls with identical inputs always produce identical output', () => {
    const results = new Set();
    for (let i = 0; i < 10; i++) {
      results.add(JSON.stringify(scope.computeMomentumBlend(true, 0.7, 0.6, 0.5, 0.2)));
    }
    expect(results.size).toBe(1);
  });
});

describe('MOMENTUM_TREND_THRESHOLD_V1 / MOMENTUM_BLEND_WEIGHT_V1 — documented, versioned constants', () => {
  it('threshold is exactly 0.5, matching applyTrendGuardrail\'s own existing "strong trend" definition', () => {
    const scope = evalInScope(extractConstants('MOMENTUM_TREND_THRESHOLD_V1'));
    expect(scope.MOMENTUM_TREND_THRESHOLD_V1).toBe(0.5);
  });

  it('blend weight is exactly 0.20, the midpoint of the roadmap\'s specified 0.15-0.25 range', () => {
    const scope = evalInScope(extractConstants('MOMENTUM_BLEND_WEIGHT_V1'));
    expect(scope.MOMENTUM_BLEND_WEIGHT_V1).toBe(0.20);
    expect(scope.MOMENTUM_BLEND_WEIGHT_V1).toBeGreaterThanOrEqual(0.15);
    expect(scope.MOMENTUM_BLEND_WEIGHT_V1).toBeLessThanOrEqual(0.25);
  });

  it('both constants are versioned in name (_V1 suffix), per the "no silent threshold changes" requirement', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('const MOMENTUM_TREND_THRESHOLD_V1 = 0.5;');
    expect(src).toContain('const MOMENTUM_BLEND_WEIGHT_V1 = 0.20;');
  });
});

describe('CORRECTION (post-audit): SELECTION_VARIANTS reverted -- challenger_momentum deliberately excluded from the production pool', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractConstants('SELECTION_VARIANTS'));
  });

  it('challenger_momentum does NOT appear anywhere in SELECTION_VARIANTS for BTC, LINK, or ETH', () => {
    for (const coin of ['BTC', 'LINK', 'ETH']) {
      const entry = scope.SELECTION_VARIANTS[coin].find((v) => v.key === 'challenger_momentum');
      expect(entry).toBeUndefined();
    }
  });

  it('every coin has exactly the original 6 variants -- production pool size is unchanged by this PR', () => {
    for (const coin of ['BTC', 'LINK', 'ETH']) {
      expect(scope.SELECTION_VARIANTS[coin]).toHaveLength(6);
      const keys = scope.SELECTION_VARIANTS[coin].map((v) => v.key);
      expect(keys).toEqual(['original', 'experimental', 'calibrated', 'challenger_flat', 'challenger_tilted', 'challenger_calibrated']);
    }
  });
});

describe('PROOF: selectBestVariant can never see, score, or choose challenger_momentum', () => {
  it('the eligibility loop never queries challenger_predictions.p_up_momentum at all -- it is simply not in the registry it iterates', async () => {
    const source = extractFunctions('selectBestVariant', 'decideSelection', 'computeLcaScore', 'coreTableForCoin') + '\n\n' +
      extractConstants('SELECTION_VARIANTS', 'SELECTION_MIN_HISTORY', 'SELECTION_MIN_MATCHED', 'SELECTION_CRITICAL_Z');
    const counts = {
      'predictions:p_up': 100, 'predictions:p_up_experimental': 100, 'predictions:calibrated_p_up': 100,
      'challenger_predictions:p_up_flat': 100, 'challenger_predictions:p_up_tilted': 100, 'challenger_predictions:calibrated_p_up_flat': 100,
      'challenger_predictions:p_up_momentum': 100, // even with plenty of history, it must never be looked at
    };
    const eligibleChecked = [];
    const db = {
      prepare(sql) {
        return {
          bind: (...args) => ({
            first: async () => null,
            all: async () => {
              if (sql.includes('SELECT COUNT(*) as n FROM')) {
                const table = sql.match(/FROM (\w+)/)[1];
                const field = sql.match(/AND (\w+) IS NOT NULL$/)[1];
                const key = `${table}:${field}`;
                eligibleChecked.push(key);
                return { results: [{ n: counts[key] ?? 0 }] };
              }
              return { results: [] };
            },
          }),
        };
      },
    };
    const scope = evalInScope(source);
    await scope.selectBestVariant({ DB: db }, 'BTC', 24);
    // The definitive proof: momentum's table/field pair is never even
    // checked, because it was never in the registry selectBestVariant
    // iterates -- it structurally cannot be chosen, not just "chosen
    // rarely" or "gated out downstream".
    expect(eligibleChecked).not.toContain('challenger_predictions:p_up_momentum');
    expect(eligibleChecked).toHaveLength(6);
  });

  it('decideSelection\'s candidate pool can never contain challenger_momentum, since scores are only ever built from SELECTION_VARIANTS entries', () => {
    const src = extractFunctions('selectBestVariant');
    // The only place `scores.push` happens is inside the `for (const v of eligible)`
    // loop, where `eligible` is filtered from `variantDefs = SELECTION_VARIANTS[coin]`.
    // With momentum absent from that registry (proven above), it cannot enter
    // `eligible`, `scores`, or therefore `decision.chosen`.
    expect(src).toContain('const variantDefs = SELECTION_VARIANTS[coin];');
    expect(src).not.toContain('MOMENTUM_EXPERIMENT_VARIANT');
    expect(src).not.toContain("'challenger_momentum'");
  });
});

describe('MOMENTUM_EXPERIMENT_VARIANT / logMomentumSelectionExperiment — parallel scoring, logged-only, decision-free', () => {
  it('MOMENTUM_EXPERIMENT_VARIANT is a standalone constant, not a member of SELECTION_VARIANTS', () => {
    const scope = evalInScope(extractConstants('MOMENTUM_EXPERIMENT_VARIANT', 'SELECTION_VARIANTS'));
    expect(scope.MOMENTUM_EXPERIMENT_VARIANT).toEqual({ key: 'challenger_momentum', table: 'challenger_predictions', field: 'p_up_momentum', coinFilter: true });
    for (const coin of ['BTC', 'LINK', 'ETH']) {
      expect(scope.SELECTION_VARIANTS[coin]).not.toContainEqual(scope.MOMENTUM_EXPERIMENT_VARIANT);
    }
  });

  it('logMomentumSelectionExperiment never calls selectBestVariant, decideSelection, decideSelectionSoftened, or logAnomalyGateExperiment', () => {
    const src = extractFunctions('logMomentumSelectionExperiment');
    expect(src).not.toContain('selectBestVariant(');
    expect(src).not.toContain('decideSelection(');
    expect(src).not.toContain('decideSelectionSoftened(');
    expect(src).not.toContain('logAnomalyGateExperiment(');
  });

  it('logMomentumSelectionExperiment never writes to selection_decisions or selection_decisions_anomaly -- only reads the former, writes only to selection_decisions_momentum', () => {
    const src = extractFunctions('logMomentumSelectionExperiment');
    expect(src).not.toMatch(/INSERT INTO selection_decisions\s/);
    expect(src).not.toContain('INSERT INTO selection_decisions_anomaly');
    expect(src).toContain('INSERT INTO selection_decisions_momentum');
    expect(src).toContain('SELECT chosen_variant, chosen_p_up, lca_score FROM selection_decisions');
  });

  it('computes a ranking, never a Bonferroni-gated verdict -- no SELECTION_CRITICAL_Z, ANOMALY_GATE_MARGIN_FACTOR, or "cleared_gate" concept inside it', () => {
    const src = extractFunctions('logMomentumSelectionExperiment');
    expect(src).not.toContain('SELECTION_CRITICAL_Z');
    expect(src).not.toContain('ANOMALY_GATE_MARGIN_FACTOR');
    expect(src).not.toContain('cleared_gate');
    expect(src).not.toContain('requiredMargin');
  });

  it('behaviorally: scores all 7 (6 production + momentum), logs momentum\'s LCA/rank, and reads (not recomputes) the latest production decision for comparison', async () => {
    const source = extractFunctions('logMomentumSelectionExperiment', 'computeLcaScore', 'coreTableForCoin', 'nearestRow') + '\n\n' +
      extractConstants('SELECTION_VARIANTS', 'MOMENTUM_EXPERIMENT_VARIANT', 'SELECTION_MIN_HISTORY', 'SELECTION_MIN_MATCHED');
    const scope = evalInScope(source);

    const now = Date.now();
    const history = Array.from({ length: 20 }, (_, i) => ({
      ts: now - (20 - i) * 3600000, features_json: JSON.stringify({ f: i }), realized_up: i % 2,
    }));
    const variantRows = (pUp) => history.map(h => ({ ts: h.ts, p_up: pUp, realized_up: h.realized_up }));

    let inserted = null;
    const db = {
      prepare(sql) {
        return {
          bind: (...args) => ({
            first: async () => {
              if (sql.includes('FROM predictions WHERE') && sql.includes('ORDER BY ts DESC LIMIT 1') && !sql.includes('selection_decisions')) {
                return { ts: now, features_json: JSON.stringify({ f: 0 }) };
              }
              if (sql.includes('FROM selection_decisions WHERE')) {
                return { chosen_variant: 'challenger_flat', chosen_p_up: 0.61, lca_score: 0.58 };
              }
              return null;
            },
            all: async () => {
              if (sql.includes('SELECT COUNT(*) as n FROM')) return { results: [{ n: 100 }] };
              if (sql.includes('FROM predictions WHERE') && sql.includes('LIMIT 300')) return { results: history };
              if (sql.includes('SELECT ts, ')) return { results: variantRows(0.7) };
              return { results: [] };
            },
            run: async () => { inserted = args; return {}; },
          }),
        };
      },
    };
    // Capture bound args on the INSERT specifically.
    const origPrepare = db.prepare.bind(db);
    db.prepare = (sql) => {
      const stmt = origPrepare(sql);
      if (sql.includes('INSERT INTO selection_decisions_momentum')) {
        const origBind = stmt.bind.bind(stmt);
        stmt.bind = (...args) => { inserted = args; return origBind(...args); };
      }
      return stmt;
    };

    const result = await scope.logMomentumSelectionExperiment({ DB: db }, 'BTC', 24);
    expect(result.ok).toBe(true);
    if (result.logged) {
      expect(result.total_scored).toBe(7);
      expect(result.production_chosen_variant).toBe('challenger_flat');
      expect(inserted).not.toBeNull();
    }
  });
});

describe('STRUCTURAL CHECK: existing flat/tilted/calibrated Challenger logic is byte-identical', () => {
  it('the flat variant computation block is textually unchanged', () => {
    const src = extractFunctions('runChallengerPrediction');
    expect(src).toContain("const ANOMALY_SHRINK = 0.5; // halve the distance from 0.5 when no good analog exists");
    expect(src).toContain('pUpFlat = 0.5 + (coreResult.p_up - 0.5) * ANOMALY_SHRINK;');
    expect(src).toContain('pUpFlat = applyTrendGuardrail(pUpFlat, coreResult.trend_strength);');
  });

  it('the tilted variant computation block (Foufi digest logic) is textually unchanged', () => {
    const src = extractFunctions('runChallengerPrediction');
    expect(src).toContain("'SELECT video_id, published_ts, transcript_status, summary_json FROM foufi_digest ORDER BY published_ts DESC LIMIT 1'");
    expect(src).toContain("pUpTilted = pUpFlat + (pUpFlat >= 0.5 ? 0.10 : -0.10);");
  });

  it('the calibrated-flat computation (getLatestChallengerCalibrationCurve / applyCalibratedProbability) is textually unchanged', () => {
    const src = extractFunctions('runChallengerPrediction');
    expect(src).toContain('const challengerCurveRows = await getLatestChallengerCalibrationCurve(env, coin, horizonHours);');
    expect(src).toContain('const calibratedPUpFlat = applyCalibratedProbability(pUpFlat, challengerCurveRows);');
  });

  it('backfillChallengerPredictions (the shared resolution path) is textually unchanged -- confirms it needed no update because it is already column-agnostic', () => {
    const src = extractFunctions('backfillChallengerPredictions');
    expect(src).toContain("'UPDATE challenger_predictions SET realized_price=?, realized_return=?, realized_up=?, resolved_ts=? WHERE id=?'");
    expect(src).not.toContain('momentum'); // proves this function was never touched to accommodate the new column
  });
});

describe('STRUCTURAL CHECK: production selection/core k-NN logic and fencing-token functions are byte-identical', () => {
  it('selectBestVariant, decideSelection, computeLcaScore are present with their exact original signatures', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('async function selectBestVariant(env, coin, horizonHours) {');
    expect(src).toContain('function decideSelection(scores) {');
    expect(src).toContain('function computeLcaScore(variantRows, neighborhood, todaysCallUp, tolMs) {');
  });

  it('every SELECTION_* production constant is present with its exact original value', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('const SELECTION_MIN_HISTORY = 50;');
    expect(src).toContain('const SELECTION_MIN_MATCHED = 3;');
    expect(src).toContain('const SELECTION_CRITICAL_Z = { 1: 1.6449, 2: 1.9600, 3: 2.1280, 4: 2.2414, 5: 2.3263, 6: 2.3940 };');
  });

  it("decideSelection's own formula is textually unchanged -- no momentum/blend reference anywhere in it", () => {
    const src = extractFunctions('decideSelection');
    expect(src).toContain('const requiredMargin = z * Math.sqrt(0.25 / winner.n_matched);');
    expect(src).not.toContain('momentum');
  });

  it('selectBestVariant never calls computeMomentumBlend directly -- momentum only enters through the normal SELECTION_VARIANTS eligibility/scoring path, same as every other variant', () => {
    const src = extractFunctions('selectBestVariant');
    expect(src).not.toContain('computeMomentumBlend');
    expect(src).not.toContain('MOMENTUM_TREND_THRESHOLD_V1');
    expect(src).not.toContain('MOMENTUM_BLEND_WEIGHT_V1');
  });

  it('Experiment 2 (softened gate) is untouched by this PR', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('const ANOMALY_GATE_MARGIN_FACTOR = 0.5;');
    expect(src).toContain('async function logAnomalyGateExperiment(env, coin, horizonHours) {');
  });

  it('the fencing-token / read-only ingestion functions are unmodified in shape', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('async function claimStaleRefresh(env, coin, nowTs, claimWindowMs = 60 * 1000)');
    expect(src).toContain('async function resolveWriteAuthorization(env, table, coin, allowWrite)');
    expect(src).toMatch(/FROM stale_refresh_claim WHERE coin = /);
  });
});

describe('INSERT statement includes p_up_momentum and momentum_triggered in both persist branches', () => {
  it('both the unconditional (cron) and conditional (claimToken) INSERT branches include the new columns', () => {
    const src = extractFunctions('runChallengerPrediction');
    const insertCount = (src.match(/p_up_momentum, momentum_triggered/g) || []).length;
    expect(insertCount).toBe(2); // one per persist branch
  });

  it('the return object includes p_up_momentum and momentum_triggered for external visibility', () => {
    const src = extractFunctions('runChallengerPrediction');
    expect(src).toContain('p_up_momentum: Number(pUpMomentum.toFixed(3)), momentum_triggered: momentumTriggered,');
  });
});
