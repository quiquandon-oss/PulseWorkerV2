// /prediction-freshness: read-only persistence monitor for the 3h cron
// (added 2026-10-11 while verifying the cron CPU fix bbf6105).
import { describe, it, expect } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const { getPredictionFreshness, isCronGridTs } = evalInScope(
  extractFunctions('getPredictionFreshness', 'summarizeFreshness', 'isCronGridTs') + '\n' +
  extractConstants('FRESHNESS_COINS', 'CRON_TICK_MS', 'CRON_GRID_SLACK_MS', 'FRESHNESS_STALE_MS')
);

const H = 3600000;
const NOW = Date.UTC(2026, 9, 11, 13, 30);
const TICK = (h) => Date.UTC(2026, 9, 11, h, 0, 44);

function fakeDb(tables) {
  return {
    prepare(sql) {
      let args = [];
      const table = sql.match(/FROM (\w+)/)[1];
      const rows = () => (tables[table] || []).filter(r => !/WHERE ts >= \?/.test(sql) || r.ts >= args[0]);
      const q = {
        bind(...a) { args = a; return q; },
        async all() {
          if (/MAX\(ts\) AS latest/.test(sql) && /GROUP BY coin, horizon_hours/.test(sql)) {
            const g = {};
            for (const r of rows()) { const k = r.coin + '|' + r.horizon_hours; g[k] = Math.max(g[k] ?? -Infinity, r.ts); }
            return { results: Object.entries(g).map(([k, latest]) => ({ coin: k.split('|')[0], horizon_hours: +k.split('|')[1], latest })) };
          }
          if (/GROUP BY horizon_hours/.test(sql)) {
            const g = {};
            for (const r of rows()) g[r.horizon_hours] = Math.max(g[r.horizon_hours] ?? -Infinity, r.ts);
            return { results: Object.entries(g).map(([h, latest]) => ({ horizon_hours: +h, latest })) };
          }
          return { results: rows() };
        },
        async first() { const r = rows(); return { latest: r.length ? Math.max(...r.map(x => x.ts)) : null }; },
      };
      return q;
    },
  };
}

describe('getPredictionFreshness', () => {
  it('separates cron-grid rows from live rows, reports age, commit and status per coin/horizon/path', async () => {
    const env = { DB: fakeDb({
      predictions: [
        { horizon_hours: 24, ts: TICK(9), git_commit_sha: 'bbf6105' },
        { horizon_hours: 24, ts: TICK(12), git_commit_sha: 'bbf6105' },
        { horizon_hours: 12, ts: TICK(9) + 37 * 60000, git_commit_sha: 'bbf6105' },   // live, not cron grid
        { horizon_hours: 12, ts: NOW - 72 * H, git_commit_sha: 'old' },               // outside 48h window
      ],
      challenger_predictions: [{ coin: 'BTC', horizon_hours: 24, ts: TICK(12), git_commit_sha: 'bbf6105' }],
      btc_data: [{ ts: TICK(12) }],
      eth_predictions: [], link_predictions: [], eth_data: [], link_data: [],
    }) };
    const out = await getPredictionFreshness(env, NOW);
    const b24 = out.coins.BTC.horizons[24];
    expect(b24.core).toMatchObject({ latest_ts: TICK(12), rows_24h: 2, cron_rows_24h: 2, latest_cron_ts: TICK(12), latest_git_commit_sha: 'bbf6105', status: 'FRESH' });
    expect(b24.challenger).toMatchObject({ rows_24h: 1, cron_rows_24h: 1, status: 'FRESH' });
    const b12 = out.coins.BTC.horizons[12];
    expect(b12.core).toMatchObject({ latest_ts: TICK(9) + 37 * 60000, rows_24h: 1, cron_rows_24h: 0, latest_cron_ts: null, status: 'FRESH' });
    expect(b12.challenger).toMatchObject({ latest_ts: null, status: 'NONE', rows_24h: 0 });
    expect(out.coins.ETH.horizons[24].core.status).toBe('NONE');
    expect(out.coins.BTC.price_data.latest_age_hours).toBeCloseTo(1.49, 1);
    expect(out.expected_cron_ticks_24h).toBe(8);
  });

  it('marks a coin STALE after 6h without a row, never FRESH', async () => {
    const env = { DB: fakeDb({ predictions: [{ horizon_hours: 24, ts: NOW - 7 * H, git_commit_sha: 'x' }], challenger_predictions: [],
      eth_predictions: [], link_predictions: [], btc_data: [], eth_data: [], link_data: [] }) };
    const out = await getPredictionFreshness(env, NOW);
    expect(out.coins.BTC.horizons[24].core.status).toBe('STALE');
    expect(out.coins.BTC.price_data.latest_ts).toBeNull();
  });

  it('cron grid = within 5 minutes after 00/03/.../21 UTC', () => {
    expect(isCronGridTs(Date.UTC(2026, 9, 11, 21, 0, 43))).toBe(true);
    expect(isCronGridTs(Date.UTC(2026, 9, 11, 21, 4, 59))).toBe(true);
    expect(isCronGridTs(Date.UTC(2026, 9, 11, 21, 6, 0))).toBe(false);
    expect(isCronGridTs(Date.UTC(2026, 9, 11, 22, 0, 0))).toBe(false);
  });
});
