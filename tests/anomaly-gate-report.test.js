import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const REPORT_FNS = [
  'computeAnomalyGateReport', 'computeFullAnomalyGateAudit',
  'groupAnomalyGateEpisodes', 'computeAnomalyGateAggregate', 'coreTableForCoin',
];
const REPORT_CONSTS = ['ANOMALY_AUDIT_MIN_SAMPLE_N', 'ANOMALY_AUDIT_MIN_EPISODES', 'ANOMALY_AUDIT_MAX_GAP_MS'];

function reportSource() {
  return extractFunctions(...REPORT_FNS) + '\n\n' + extractConstants(...REPORT_CONSTS);
}

// Fake DB: routes purely by table name in the SQL text, returns pre-seeded
// rows. Deliberately has NO .run() implementation at all -- any write
// attempt (INSERT/UPDATE/DELETE via .run()) throws immediately, which is
// itself the read-only regression test.
function makeFakeDb({ anomalyRows = [], prodRows = [], coreRows = [] } = {}) {
  return {
    prepare(sql) {
      return {
        bind: (...args) => ({
          all: async () => {
            if (sql.includes('FROM selection_decisions_anomaly')) return { results: anomalyRows };
            if (sql.includes('FROM selection_decisions WHERE')) return { results: prodRows };
            return { results: coreRows };
          },
        }),
      };
    },
  };
}

function scoresJson(entries) {
  return JSON.stringify(entries);
}

describe('computeAnomalyGateReport — empty / missing data', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(reportSource()); });

  it('no selection_decisions_anomaly rows -> ok:true, empty rows/episodes, aggregate:null, no fabricated numbers', async () => {
    const db = makeFakeDb({ anomalyRows: [] });
    const report = await scope.computeAnomalyGateReport({ DB: db }, 'BTC', 24);
    expect(report.ok).toBe(true);
    expect(report.raw_observation_count).toBe(0);
    expect(report.rows).toEqual([]);
    expect(report.episodes).toEqual([]);
    expect(report.aggregate).toBeNull();
  });
});

describe('computeAnomalyGateReport — row construction, production join, resolved outcome', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(reportSource()); });

  it('joins production selection_decisions by prediction_ts and exposes both decisions side by side', async () => {
    const anomalyRows = [{
      ts: 5000, prediction_ts: 1000, is_regime_anomaly: 1, margin_factor: 0.5,
      chosen_variant_anomaly: 'challenger_flat', chosen_p_up_anomaly: 0.65,
      lca_score_anomaly: 0.7, comparison_count_anomaly: 3, base_required_margin: 0.2,
      required_margin_anomaly: 0.1, cleared_gate_anomaly: 1, k_sel_anomaly: 10,
      reason_anomaly: 'softened gate cleared', scores_json_anomaly: scoresJson([{ variant: 'challenger_flat', p_up: 0.65, lca: 0.7, n_matched: 12 }]),
    }];
    const prodRows = [{ ts: 5001, prediction_ts: 1000, chosen_variant: 'original', chosen_p_up: 0.55, cleared_gate: 0, comparison_count: 3 }];
    const coreRows = [{ ts: 1000, realized_up: 1, resolved_ts: 9999 }];
    const db = makeFakeDb({ anomalyRows, prodRows, coreRows });
    const report = await scope.computeAnomalyGateReport({ DB: db }, 'BTC', 24);
    expect(report.rows).toHaveLength(1);
    const row = report.rows[0];
    expect(row.production_chosen_variant).toBe('original');
    expect(row.anomaly_gate_chosen_variant).toBe('challenger_flat');
    expect(row.decisions_agree).toBe(false);
    expect(row.anomaly_gate_n_matched).toBe(12);
    expect(row.resolved).toBe(true);
    expect(row.realized_up).toBe(1);
  });

  it('no matching production row for that prediction_ts -> production fields are null, never fabricated, decisions_agree is null not false', async () => {
    const anomalyRows = [{
      ts: 5000, prediction_ts: 2000, is_regime_anomaly: 1, margin_factor: 0.5,
      chosen_variant_anomaly: 'calibrated', chosen_p_up_anomaly: 0.6, lca_score_anomaly: 0.6,
      comparison_count_anomaly: 1, base_required_margin: 0.15, required_margin_anomaly: 0.075,
      cleared_gate_anomaly: 0, k_sel_anomaly: 8, reason_anomaly: 'no variant cleared',
      scores_json_anomaly: scoresJson([]),
    }];
    const db = makeFakeDb({ anomalyRows, prodRows: [], coreRows: [] });
    const report = await scope.computeAnomalyGateReport({ DB: db }, 'ETH', 12);
    const row = report.rows[0];
    expect(row.production_chosen_variant).toBeNull();
    expect(row.production_decision_available).toBe(false);
    expect(row.decisions_agree).toBeNull();
    expect(row.resolved).toBe(false);
    expect(row.realized_up).toBeNull();
  });

  it('core row exists but not yet resolved (resolved_ts / realized_up null) -> resolved:false, not silently treated as a miss', async () => {
    const anomalyRows = [{
      ts: 5000, prediction_ts: 3000, is_regime_anomaly: 1, margin_factor: 0.5,
      chosen_variant_anomaly: 'original', chosen_p_up_anomaly: 0.5, lca_score_anomaly: 0.5,
      comparison_count_anomaly: 1, base_required_margin: 0.1, required_margin_anomaly: 0.05,
      cleared_gate_anomaly: 0, k_sel_anomaly: 8, reason_anomaly: 'r', scores_json_anomaly: scoresJson([]),
    }];
    const coreRows = [{ ts: 3000, realized_up: null, resolved_ts: null }];
    const db = makeFakeDb({ anomalyRows, prodRows: [], coreRows });
    const report = await scope.computeAnomalyGateReport({ DB: db }, 'LINK', 24);
    expect(report.rows[0].resolved).toBe(false);
    expect(report.rows[0].realized_up).toBeNull();
  });

  it('malformed scores_json_anomaly does not throw -- degrades to empty scores array', async () => {
    const anomalyRows = [{
      ts: 5000, prediction_ts: 4000, is_regime_anomaly: 1, margin_factor: 0.5,
      chosen_variant_anomaly: 'original', chosen_p_up_anomaly: 0.5, lca_score_anomaly: 0.5,
      comparison_count_anomaly: 1, base_required_margin: 0.1, required_margin_anomaly: 0.05,
      cleared_gate_anomaly: 0, k_sel_anomaly: 8, reason_anomaly: 'r', scores_json_anomaly: 'not json',
    }];
    const db = makeFakeDb({ anomalyRows });
    const report = await scope.computeAnomalyGateReport({ DB: db }, 'BTC', 24);
    expect(report.rows[0].scores_anomaly).toEqual([]);
    expect(report.rows[0].anomaly_gate_n_matched).toBeNull();
  });
});

describe('computeAnomalyGateReport — coin/horizon filtering never guesses a default', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(reportSource()); });

  it('coin and horizon are passed straight through to the query params and echoed back, not defaulted internally', async () => {
    const calls = [];
    const db = {
      prepare(sql) {
        return {
          bind: (...args) => {
            calls.push({ sql, args });
            return { all: async () => ({ results: [] }) };
          },
        };
      },
    };
    const report = await scope.computeAnomalyGateReport({ DB: db }, 'LINK', 12);
    expect(report.coin).toBe('LINK');
    expect(report.horizon_hours).toBe(12);
    expect(calls[0].args).toEqual(['LINK', 12]);
  });
});

describe('computeAnomalyGateAggregate / episode-level discipline', () => {
  let scope;
  beforeAll(() => { scope = evalInScope(reportSource()); });

  function row({ ts, gateUp = 1, prodUp = 1, actualUp = 1, resolved = true, prodAvailable = true }) {
    return {
      prediction_ts: ts, resolved,
      anomaly_gate_chosen_p_up: gateUp ? 0.7 : 0.3,
      production_chosen_p_up: prodAvailable ? (prodUp ? 0.7 : 0.3) : null,
      decisions_agree: prodAvailable ? (gateUp === prodUp) : null,
      realized_up: resolved ? (actualUp ? 1 : 0) : null,
    };
  }

  it('zero resolved rows -> aggregate.available = false, explicit reason, no fabricated accuracy', () => {
    const rows = [row({ ts: 1000, resolved: false })];
    const episodes = scope.groupAnomalyGateEpisodes(rows);
    const agg = scope.computeAnomalyGateAggregate(rows, episodes);
    expect(agg.available).toBe(false);
    expect(agg.reason).toBe('no_resolved_observations');
  });

  it('few resolved rows / few episodes -> insufficient_sample:true, numbers still reported but flagged', () => {
    const rows = [
      row({ ts: 1000, gateUp: 1, prodUp: 0, actualUp: 1 }),
      row({ ts: 4000, gateUp: 1, prodUp: 0, actualUp: 1 }), // far enough to be a 2nd episode
    ];
    const episodes = scope.groupAnomalyGateEpisodes(rows.map(r => ({ ...r, resolved: true })));
    const agg = scope.computeAnomalyGateAggregate(rows, episodes);
    expect(agg.available).toBe(true);
    expect(agg.n_resolved).toBe(2);
    expect(agg.insufficient_sample).toBe(true); // below min_sample_n=5 and min_episodes=3
  });

  it('consecutive close-in-time cycles collapse into ONE episode, not one per row -- the actual point of this discipline', () => {
    const base = 1_000_000;
    const rows = Array.from({ length: 10 }, (_, i) => row({ ts: base + i * 3 * 3600000, gateUp: 1, prodUp: 1, actualUp: 1 }));
    const episodes = scope.groupAnomalyGateEpisodes(rows);
    expect(episodes).toHaveLength(1);
    expect(episodes[0].n_cycles).toBe(10);
    const agg = scope.computeAnomalyGateAggregate(rows, episodes);
    expect(agg.n_resolved).toBe(10); // raw rows still counted
    expect(agg.episode_count_total).toBe(1); // but only ONE independent episode
    expect(agg.insufficient_sample).toBe(true); // 1 episode < min_episodes=3, regardless of n=10
  });

  it('a gap larger than the tolerance starts a genuinely new episode', () => {
    const base = 1_000_000;
    const rows = [
      row({ ts: base, gateUp: 1, prodUp: 1, actualUp: 1 }),
      row({ ts: base + 100 * 3600000, gateUp: 1, prodUp: 1, actualUp: 1 }), // 100h gap >> 6h tolerance
    ];
    const episodes = scope.groupAnomalyGateEpisodes(rows);
    expect(episodes).toHaveLength(2);
  });

  it('accuracy and agreement are computed only over comparable (non-null) rows, never dividing by a fabricated denominator', () => {
    const rows = [
      row({ ts: 1000, gateUp: 1, prodUp: 1, actualUp: 1 }),
      row({ ts: 4000, gateUp: 0, prodUp: 1, actualUp: 1, prodAvailable: false }),
      row({ ts: 7000, gateUp: 1, prodUp: 0, actualUp: 0 }),
    ];
    const episodes = scope.groupAnomalyGateEpisodes(rows);
    const agg = scope.computeAnomalyGateAggregate(rows, episodes);
    expect(agg.anomaly_gate_n).toBe(3);
    expect(agg.production_n).toBe(2); // one row had no production decision available
    expect(agg.agreement_n).toBe(2);
  });
});

describe('computeFullAnomalyGateAudit — orchestration', () => {
  // Only computeFullAnomalyGateAudit itself is extracted here (not
  // computeAnomalyGateReport too) so the injected mock below actually
  // takes effect instead of being shadowed by the real function's own
  // local declaration in the same eval scope.
  function orchestrationSource() {
    return extractFunctions('computeFullAnomalyGateAudit') + '\n\n' + extractConstants(...REPORT_CONSTS);
  }

  it('calls computeAnomalyGateReport for exactly the 3x2 coin/horizon grid', async () => {
    const calls = [];
    const scope = evalInScope(orchestrationSource(), {
      computeAnomalyGateReport: async (env, coin, horizon) => {
        calls.push(`${coin}-${horizon}`);
        return { ok: true, coin, horizon_hours: horizon };
      },
    });
    const result = await scope.computeFullAnomalyGateAudit({});
    expect(calls.sort()).toEqual(['BTC-12', 'BTC-24', 'ETH-12', 'ETH-24', 'LINK-12', 'LINK-24'].sort());
    expect(result.results.BTC[12].coin).toBe('BTC');
    expect(result.results.ETH[24].horizon_hours).toBe(24);
  });

  it('a failure in one coin/horizon combination does not block or corrupt the others', async () => {
    const scope = evalInScope(orchestrationSource(), {
      computeAnomalyGateReport: async (env, coin, horizon) => {
        if (coin === 'ETH' && horizon === 12) throw new Error('simulated failure for ETH/12h only');
        return { ok: true, coin, horizon_hours: horizon };
      },
    });
    const result = await scope.computeFullAnomalyGateAudit({});
    expect(result.results.ETH[12].ok).toBe(false);
    expect(result.results.ETH[12].error).toContain('simulated failure for ETH/12h only');
    expect(result.results.BTC[24].ok).toBe(true);
    expect(result.results.LINK[24].ok).toBe(true);
  });
});

describe('DETERMINISM / READ-ONLY PROOF', () => {
  it('the same DB state produces byte-identical output across repeated calls, and the fake DB (no .run() implemented) never throws -- proving no write is attempted', async () => {
    const scope = evalInScope(reportSource());
    const anomalyRows = [{
      ts: 5000, prediction_ts: 1000, is_regime_anomaly: 1, margin_factor: 0.5,
      chosen_variant_anomaly: 'challenger_flat', chosen_p_up_anomaly: 0.65, lca_score_anomaly: 0.7,
      comparison_count_anomaly: 3, base_required_margin: 0.2, required_margin_anomaly: 0.1,
      cleared_gate_anomaly: 1, k_sel_anomaly: 10, reason_anomaly: 'r',
      scores_json_anomaly: scoresJson([{ variant: 'challenger_flat', p_up: 0.65, lca: 0.7, n_matched: 12 }]),
    }];
    const prodRows = [{ ts: 5001, prediction_ts: 1000, chosen_variant: 'original', chosen_p_up: 0.55, cleared_gate: 0, comparison_count: 3 }];
    const coreRows = [{ ts: 1000, realized_up: 1, resolved_ts: 9999 }];
    const run1 = await scope.computeAnomalyGateReport({ DB: makeFakeDb({ anomalyRows, prodRows, coreRows }) }, 'BTC', 24);
    const run2 = await scope.computeAnomalyGateReport({ DB: makeFakeDb({ anomalyRows, prodRows, coreRows }) }, 'BTC', 24);
    const strip = (r) => { const { generated_at, ...rest } = r; return rest; };
    expect(strip(run1)).toEqual(strip(run2));
  });

  it('computeAnomalyGateReport source never calls .run() and never references any write SQL verb', () => {
    const src = extractFunctions('computeAnomalyGateReport');
    expect(src).not.toContain('.run()');
    expect(src).not.toMatch(/INSERT INTO|UPDATE \w+ SET|DELETE FROM/);
  });

  it('computeAnomalyGateReport never calls selectBestVariant, decideSelection, computeLcaScore, or logAnomalyGateExperiment', () => {
    const src = extractFunctions('computeAnomalyGateReport');
    expect(src).not.toMatch(/selectBestVariant\(|decideSelection\(|computeLcaScore\(|logAnomalyGateExperiment\(/);
  });

  it('the route block itself only calls .all()-backed report functions, never .run(), and requires GET', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf("'/research/anomaly-gate'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = src.slice(idx, idx + 1400);
    expect(nearby).toContain("request.method === 'GET'");
    expect(nearby).toContain('hasValidCoin && hasValidHorizon');
    expect(nearby).toContain('computeAnomalyGateReport');
    expect(nearby).toContain('computeFullAnomalyGateAudit');
    expect(nearby).not.toContain('.run(');
    expect(nearby).not.toMatch(/INSERT INTO|UPDATE \w+ SET|DELETE FROM/);
  });

  it('the route never defaults coin or horizon -- both must be explicitly valid to scope to a single report, same discipline as /research/anomaly-conditioned-audit', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf("'/research/anomaly-gate'");
    const nearby = src.slice(idx, idx + 1400);
    expect(nearby).not.toMatch(/:\s*'BTC'/);
    expect(nearby).not.toMatch(/:\s*24\b/);
  });
});

describe('STRUCTURAL CHECK: production selection logic remains completely untouched by this endpoint', () => {
  it('selectBestVariant, decideSelection, computeLcaScore keep their exact original signatures', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('async function selectBestVariant(env, coin, horizonHours) {');
    expect(src).toContain('function decideSelection(scores) {');
    expect(src).toContain('function computeLcaScore(variantRows, neighborhood, todaysCallUp, tolMs) {');
  });

  it('every production selection constant is present with its exact original value', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('const SELECTION_MIN_HISTORY = 50;');
    expect(src).toContain("const SELECTION_MIN_MATCHED = 3;");
    expect(src).toContain('const SELECTION_CRITICAL_Z = { 1: 1.6449, 2: 1.9600, 3: 2.1280, 4: 2.2414, 5: 2.3263, 6: 2.3940 };');
    expect(src).toContain("const FEATURE_KEYS = ['score', 'technical_score', 'regime_mag', 'bottom_score'];");
    expect(src).toContain('const CONDITIONAL_CALIB_WEIGHTS = { score: 1.0, technical_score: 1.0, regime_mag: 1.5, bottom_score: 0.3 };');
  });

  it('the fencing-token / read-only ingestion functions are unmodified in shape', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    expect(src).toContain('async function claimStaleRefresh(env, coin, nowTs, claimWindowMs = 60 * 1000)');
    expect(src).toContain('async function resolveWriteAuthorization(env, table, coin, allowWrite)');
    expect(src).toMatch(/FROM stale_refresh_claim WHERE coin = /);
  });

  it("decideSelection's own formula is textually unchanged -- no margin factor, no anomaly-gate reference", () => {
    const src = extractFunctions('decideSelection');
    expect(src).toContain('const requiredMargin = z * Math.sqrt(0.25 / winner.n_matched);');
    expect(src).not.toContain('marginFactor');
    expect(src).not.toContain('ANOMALY_GATE_MARGIN_FACTOR');
  });

  it('selectBestVariant never calls any of the read-endpoint\'s new functions', () => {
    const src = extractFunctions('selectBestVariant');
    expect(src).not.toMatch(/computeAnomalyGateReport|computeFullAnomalyGateAudit|groupAnomalyGateEpisodes|computeAnomalyGateAggregate/);
  });

  it('the production selection pool (SELECTION_VARIANTS) still has exactly the original 6 variants per coin -- challenger_momentum is not in it, and this endpoint does not add to it', () => {
    const src = extractConstants('SELECTION_VARIANTS');
    const scope = evalInScope(src);
    for (const coin of ['BTC', 'LINK', 'ETH']) {
      expect(scope.SELECTION_VARIANTS[coin]).toHaveLength(6);
      expect(scope.SELECTION_VARIANTS[coin].map(v => v.key)).not.toContain('challenger_momentum');
    }
  });

  it('this PR\'s new functions do not appear anywhere inside selectBestVariant, decideSelection, or computeLcaScore', () => {
    for (const name of ['selectBestVariant', 'decideSelection', 'computeLcaScore']) {
      const src = extractFunctions(name);
      expect(src).not.toMatch(/AnomalyGateReport|AnomalyGateAudit|AnomalyGateEpisodes|AnomalyGateAggregate/);
    }
  });
});
