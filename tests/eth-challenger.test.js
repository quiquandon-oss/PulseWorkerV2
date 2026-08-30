import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

describe('SELECTION_VARIANTS.ETH now includes challenger entries, mirroring BTC/LINK', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractConstants('SELECTION_VARIANTS'));
  });

  it('ETH has all 7 variants now (was 6 pre-Experiment-3), not just the original 3', () => {
    // Updated for Learning Roadmap §3 Experiment 3: challenger_momentum
    // added to SELECTION_VARIANTS for all three coins, same shape as the
    // other challenger_* entries. This test previously asserted 6; the
    // 7th (challenger_momentum) is intentional, not a regression.
    const keys = scope.SELECTION_VARIANTS.ETH.map((v) => v.key);
    expect(keys).toEqual(['original', 'experimental', 'calibrated', 'challenger_flat', 'challenger_tilted', 'challenger_calibrated', 'challenger_momentum']);
  });

  it('ETH\'s challenger entries point at challenger_predictions with coinFilter true, same as BTC/LINK', () => {
    const ethChallengerEntries = scope.SELECTION_VARIANTS.ETH.filter((v) => v.key.startsWith('challenger'));
    for (const entry of ethChallengerEntries) {
      expect(entry.table).toBe('challenger_predictions');
      expect(entry.coinFilter).toBe(true);
    }
    expect(ethChallengerEntries.map((v) => v.field)).toEqual(['p_up_flat', 'p_up_tilted', 'calibrated_p_up_flat', 'p_up_momentum']);
  });
});

describe('regression: backfillChallengerPredictions no longer silently resolves ETH against btc_data', () => {
  // The bug: `p.coin === 'LINK' ? 'link_data' : 'btc_data'` treated any
  // non-LINK coin (including the newly-added ETH) as BTC -- ETH's
  // challenger predictions would have resolved against the wrong price
  // table entirely, producing meaningless realized_return/realized_up.
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('backfillChallengerPredictions'));
  });

  function makeFakeDb({ pendingRows, priceRowsByTable }) {
    const runCalls = [];
    return {
      db: {
        prepare(sql) {
          return {
            bind: (...args) => ({
              all: async () => {
                if (sql.includes('SELECT * FROM challenger_predictions')) return { results: pendingRows };
                for (const [table, rows] of Object.entries(priceRowsByTable)) {
                  if (sql.includes(`FROM ${table}`)) return { results: rows };
                }
                return { results: [] };
              },
              run: async () => { runCalls.push({ sql, args }); return {}; },
            }),
          };
        },
      },
      runCalls,
    };
  }

  it('resolves an ETH challenger prediction against eth_data/eth_price, not btc_data', async () => {
    const pendingRows = [{ id: 1, coin: 'ETH', horizon_hours: 24, target_ts: 1000, price_at_prediction: 100, p_up_flat: 0.6, p_up_tilted: 0.6, calibrated_p_up_flat: 0.6 }];
    const { db, runCalls } = makeFakeDb({
      pendingRows,
      priceRowsByTable: {
        eth_data: [{ ts: 1000, price: 110 }], // correct table -- ETH really moved up 10%
        btc_data: [{ ts: 1000, price: 50 }],  // wrong table -- if this got used instead, the math would be nonsensical
      },
    });
    await scope.backfillChallengerPredictions({ DB: db });
    const updateCall = runCalls.find((c) => c.sql.includes('UPDATE') || c.sql.includes('SET'));
    // realized_price should reflect eth_data's 110, proving eth_data was the table actually queried
    expect(updateCall.args).toContain(110);
  });

  it('still resolves LINK against link_data and BTC against btc_data (no regression to the two coins that already worked)', async () => {
    const pendingRows = [
      { id: 2, coin: 'LINK', horizon_hours: 24, target_ts: 1000, price_at_prediction: 10, p_up_flat: 0.6, p_up_tilted: 0.6, calibrated_p_up_flat: 0.6 },
      { id: 3, coin: 'BTC', horizon_hours: 24, target_ts: 1000, price_at_prediction: 100, p_up_flat: 0.6, p_up_tilted: 0.6, calibrated_p_up_flat: 0.6 },
    ];
    const { db, runCalls } = makeFakeDb({
      pendingRows,
      priceRowsByTable: {
        link_data: [{ ts: 1000, price: 11 }],
        btc_data: [{ ts: 1000, price: 105 }],
        eth_data: [{ ts: 1000, price: 999 }], // decoy -- must never be used for LINK or BTC rows
      },
    });
    await scope.backfillChallengerPredictions({ DB: db });
    const updateCalls = runCalls.filter((c) => c.sql.includes('UPDATE') || c.sql.includes('SET'));
    const realizedPrices = updateCalls.map((c) => c.args.find((a) => a === 11 || a === 105));
    expect(realizedPrices).toContain(11);
    expect(realizedPrices).toContain(105);
  });
});

describe('regression: getChallengerCalibration no longer silently substitutes btc_data for ETH', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(extractFunctions('getChallengerCalibration'));
  });

  it('queries eth_data/eth_price for coin=ETH, not btc_data', async () => {
    let queriedTable = null;
    const db = {
      prepare(sql) {
        const handle = {
          bind: (...args) => handle,
          all: async () => {
            if (sql.includes('FROM challenger_predictions')) {
              // 5 resolved rows -- just enough to pass the n<5 early-return
              return { results: Array.from({ length: 5 }, (_, i) => ({ ts: i, target_ts: i, p_up_flat: 0.6, realized_up: 1, price_at_prediction: 100 })) };
            }
            const tableMatch = sql.match(/FROM (\w+)/);
            if (tableMatch) queriedTable = tableMatch[1];
            return { results: [{ ts: 0, price: 100 }] };
          },
        };
        return handle;
      },
    };
    await scope.getChallengerCalibration({ DB: db }, 'ETH', 24);
    expect(queriedTable).toBe('eth_data');
  });
});

describe('regression: the daily cron no longer excludes ETH from Challenger calibration refresh', () => {
  it('scheduled() loops over BTC, LINK, and ETH together for both refreshCalibrationCurve and refreshChallengerCalibrationCurve', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    // This exact loop-header text appears twice in the file (this cron loop,
    // and the daily report's model_comparison loop) -- anchor on the second
    // occurrence specifically by searching past the first one's position.
    const firstIdx = src.indexOf("for (const coin of ['BTC', 'LINK', 'ETH']) {");
    expect(firstIdx).toBeGreaterThan(-1);
    const idx = src.indexOf("for (const coin of ['BTC', 'LINK', 'ETH']) {", firstIdx + 1);
    expect(idx).toBeGreaterThan(-1);
    const nearby = src.slice(idx, idx + 600);
    expect(nearby).toContain('refreshCalibrationCurve');
    expect(nearby).toContain('refreshChallengerCalibrationCurve');
    expect(src).not.toMatch(/core-model calibration only, no Challenger loop/);
  });
});

describe('regression: ETH is included in the daily report\'s Challenger vs Production comparison', () => {
  it('the model_comparison loop now covers all three coins, not just BTC/LINK', () => {
    const src = readFileSync(new URL('../worker.js', import.meta.url), 'utf8');
    const idx = src.indexOf('Challenger vs Production comparison');
    expect(idx).toBeGreaterThan(-1);
    const nearby = src.slice(idx, idx + 600);
    expect(nearby).toContain("for (const coin of ['BTC', 'LINK', 'ETH'])");
    expect(nearby).toContain('model_comparison');
  });
});

describe('regression: ethPredictAndLog now calls runChallengerPrediction, mirroring linkPredictAndLog', () => {
  it('the function source shows the same try/catch challenger wiring LINK already had', () => {
    const src = extractFunctions('ethPredictAndLog');
    expect(src).toMatch(/runChallengerPrediction\(env, \{/);
    expect(src).toMatch(/coin: 'ETH'/);
    expect(src).toMatch(/priceTable: 'eth_data'/);
    expect(src).toMatch(/priceCol: 'eth_price'/);
  });

  it('never throws out to the caller if the challenger call itself fails -- same resilience contract as LINK', () => {
    const src = extractFunctions('ethPredictAndLog');
    expect(src).toMatch(/catch \(e\) \{\s*result\.challenger = \{ ok: false, error: String\(e\) \};/);
  });
});
