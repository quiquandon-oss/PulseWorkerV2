// /prediction-freshness: read-only persistence monitor for the 3h cron (added 2026-10-11), index-driven queries
// and a 60 s per-isolate cache (2026-11). Runs the real functions on SQLite with the production indexes.
import { describe, it, expect, beforeEach } from 'vitest';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';
import { makeD1 } from './replay/d1-sqlite.mjs';

// The implementation this replaced, kept verbatim as the reference for "same response as before".
const LEGACY = `async function getPredictionFreshnessLegacy(env, nowTs = Date.now()) {
  const since = nowTs - 48 * 60 * 60 * 1000;
  const out = { ok: true, generated_ts: nowTs, expected_cron_ticks_24h: 8, cron_grid_slack_minutes: CRON_GRID_SLACK_MS / 60000, coins: {} };
  const challengerRows = (await env.DB.prepare(
    'SELECT coin, horizon_hours, ts, git_commit_sha FROM challenger_predictions WHERE ts >= ?'
  ).bind(since).all()).results;
  const challengerLatest = (await env.DB.prepare(
    'SELECT coin, horizon_hours, MAX(ts) AS latest FROM challenger_predictions GROUP BY coin, horizon_hours'
  ).all()).results;
  for (const { coin, core, price } of FRESHNESS_COINS) {
    const coreRows = (await env.DB.prepare(
      \`SELECT horizon_hours, ts, git_commit_sha FROM \${core} WHERE ts >= ?\`
    ).bind(since).all()).results;
    const coreLatest = (await env.DB.prepare(
      \`SELECT horizon_hours, MAX(ts) AS latest FROM \${core} GROUP BY horizon_hours\`
    ).all()).results;
    const priceRow = await env.DB.prepare(\`SELECT MAX(ts) AS latest FROM \${price}\`).first();
    const coinOut = {
      price_data: { table: price, latest_ts: priceRow ? priceRow.latest : null,
        latest_age_hours: priceRow && priceRow.latest != null ? Number(((nowTs - priceRow.latest) / 3600000).toFixed(2)) : null },
      horizons: {},
    };
    for (const h of [12, 24]) {
      const cl = coreLatest.find(r => r.horizon_hours === h);
      const ch = challengerLatest.find(r => r.coin === coin && r.horizon_hours === h);
      const core24 = summarizeFreshness(coreRows.filter(r => r.horizon_hours === h), cl ? cl.latest : null, nowTs);
      const chal24 = summarizeFreshness(challengerRows.filter(r => r.coin === coin && r.horizon_hours === h), ch ? ch.latest : null, nowTs);
      coinOut.horizons[h] = { core: core24, challenger: chal24 };
    }
    out.coins[coin] = coinOut;
  }
  return out;
}`;

const scope = evalInScope(
  extractFunctions('getPredictionFreshness', 'summarizeFreshness', 'isCronGridTs', 'getPredictionFreshnessCached', 'freshnessCacheClear') + '\n' +
  extractConstants('FRESHNESS_COINS', 'CRON_TICK_MS', 'CRON_GRID_SLACK_MS', 'FRESHNESS_STALE_MS', 'FRESHNESS_HORIZONS', 'FRESHNESS_CACHE_TTL_MS') +
  '\nlet freshnessCacheEntry = null;\n' + LEGACY
);
const { getPredictionFreshness, isCronGridTs, getPredictionFreshnessCached, freshnessCacheClear, getPredictionFreshnessLegacy } = scope;

const H = 3600000;
const NOW = Date.UTC(2026, 9, 11, 13, 30);
const TICK = (h) => Date.UTC(2026, 9, 11, h, 0, 44);

// Columns the route reads, and the indexes that exist in production (sqlite_master, 2026-11-13).
const SCHEMA = [
  ...['predictions', 'eth_predictions', 'link_predictions'].flatMap((t) => [
    `CREATE TABLE ${t} (id INTEGER PRIMARY KEY, horizon_hours INTEGER, ts INTEGER, git_commit_sha TEXT)`,
    `CREATE INDEX idx_${t}_horizon_ts ON ${t}(horizon_hours, ts)`]),
  'CREATE TABLE challenger_predictions (id INTEGER PRIMARY KEY, coin TEXT, horizon_hours INTEGER, ts INTEGER, git_commit_sha TEXT)',
  'CREATE INDEX idx_challenger_predictions_coin_horizon_ts ON challenger_predictions(coin, horizon_hours, ts)',
  ...['btc_data', 'eth_data', 'link_data'].flatMap((t) => [`CREATE TABLE ${t} (id INTEGER PRIMARY KEY, ts INTEGER)`, `CREATE INDEX idx_${t}_ts ON ${t}(ts)`]),
];
function world(tables = {}) {
  const d1 = makeD1(SCHEMA);
  for (const [t, rows] of Object.entries(tables)) for (const r of rows) {
    const cols = Object.keys(r);
    d1.db.prepare(`INSERT INTO ${t} (${cols.join(',')}) VALUES (${cols.map(() => '?').join(',')})`).run(...cols.map((c) => r[c]));
  }
  return { d1, env: { DB: d1.binding } };
}

describe('getPredictionFreshness', () => {
  it('separates cron-grid rows from live rows, reports age, commit and status per coin/horizon/path', async () => {
    const { env } = world({
      predictions: [
        { horizon_hours: 24, ts: TICK(9), git_commit_sha: 'bbf6105' },
        { horizon_hours: 24, ts: TICK(12), git_commit_sha: 'bbf6105' },
        { horizon_hours: 12, ts: TICK(9) + 37 * 60000, git_commit_sha: 'bbf6105' },   // live, not cron grid
        { horizon_hours: 12, ts: NOW - 72 * H, git_commit_sha: 'old' },               // outside 48h window
      ],
      challenger_predictions: [{ coin: 'BTC', horizon_hours: 24, ts: TICK(12), git_commit_sha: 'bbf6105' }],
      btc_data: [{ ts: TICK(12) }],
    });
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
    const { env } = world({ predictions: [{ horizon_hours: 24, ts: NOW - 7 * H, git_commit_sha: 'x' }] });
    const out = await getPredictionFreshness(env, NOW);
    expect(out.coins.BTC.horizons[24].core.status).toBe('STALE');
    expect(out.coins.BTC.price_data.latest_ts).toBeNull();
  });

  it('a series whose newest row is older than 48 h still reports that row as latest (STALE, no commit)', async () => {
    const old = NOW - 9 * 24 * H;
    const { env } = world({ link_predictions: [{ horizon_hours: 12, ts: old, git_commit_sha: 'x' }, { horizon_hours: 12, ts: old - H, git_commit_sha: 'w' }],
      challenger_predictions: [{ coin: 'LINK', horizon_hours: 12, ts: old, git_commit_sha: 'x' }] });
    const s = (await getPredictionFreshness(env, NOW)).coins.LINK.horizons[12];
    expect(s.core).toMatchObject({ latest_ts: old, status: 'STALE', rows_24h: 0, latest_git_commit_sha: null });
    expect(s.challenger).toMatchObject({ latest_ts: old, status: 'STALE' });
  });

  it('cron grid = within 5 minutes after 00/03/.../21 UTC', () => {
    expect(isCronGridTs(Date.UTC(2026, 9, 11, 21, 0, 43))).toBe(true);
    expect(isCronGridTs(Date.UTC(2026, 9, 11, 21, 4, 59))).toBe(true);
    expect(isCronGridTs(Date.UTC(2026, 9, 11, 21, 6, 0))).toBe(false);
    expect(isCronGridTs(Date.UTC(2026, 9, 11, 22, 0, 0))).toBe(false);
  });
});

// A deterministic history: 40 days of 3-hourly rows for every series, gaps, live rows off the grid, one series
// stopped 3 days ago, one stopped 10 hours ago, other coins and horizons in the challenger table.
function history() {
  let seed = 7; const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
  const t = { predictions: [], eth_predictions: [], link_predictions: [], challenger_predictions: [], btc_data: [], eth_data: [], link_data: [] };
  const core = { BTC: 'predictions', ETH: 'eth_predictions', LINK: 'link_predictions' };
  const start = NOW - 40 * 24 * H;
  for (let ts = Math.ceil(start / (3 * H)) * 3 * H; ts <= NOW; ts += 3 * H) {
    for (const coin of ['BTC', 'ETH', 'LINK']) for (const h of [12, 24]) {
      if (coin === 'ETH' && h === 12 && ts > NOW - 3 * 24 * H) continue;      // stopped three days ago
      if (coin === 'LINK' && h === 24 && ts > NOW - 10 * H) continue;         // stale, inside the window
      if (rnd() < 0.05) continue;                                             // a missed run
      const at = ts + Math.floor(rnd() * 90000);
      const sha = ts > NOW - 20 * H ? 'new' : 'old';
      t[core[coin]].push({ horizon_hours: h, ts: at, git_commit_sha: sha });
      if (rnd() > 0.03) t.challenger_predictions.push({ coin, horizon_hours: h, ts: at, git_commit_sha: sha });
      if (rnd() < 0.08) t[core[coin]].push({ horizon_hours: h, ts: at + 47 * 60000, git_commit_sha: sha }); // live refresh
    }
    t.challenger_predictions.push({ coin: 'SOL', horizon_hours: 24, ts, git_commit_sha: 'z' }, { coin: 'BTC', horizon_hours: 48, ts, git_commit_sha: 'z' });
    t.predictions.push({ horizon_hours: 48, ts, git_commit_sha: 'z' });
    t.btc_data.push({ ts }, { ts: ts + 1000 }); t.eth_data.push({ ts }); t.link_data.push({ ts: ts - 2 * H });
  }
  return t;
}

describe('index-driven queries', () => {
  it('return exactly the response of the implementation they replace', async () => {
    const { env } = world(history());
    for (const now of [NOW, NOW + 5 * H, NOW + 30 * H, NOW + 60 * H, NOW - 24 * H]) {
      expect(await getPredictionFreshness(env, now)).toEqual(await getPredictionFreshnessLegacy(env, now));
    }
    const empty = world().env;
    expect(await getPredictionFreshness(empty, NOW)).toEqual(await getPredictionFreshnessLegacy(empty, NOW));
  });

  it('use an index for every statement and read at most a tenth of what the old statements returned', async () => {
    const data = history();
    const a = world(data), b = world(data);
    await getPredictionFreshness(a.env, NOW);
    await getPredictionFreshnessLegacy(b.env, NOW);
    for (const l of a.d1.log) expect(l.plan.join(' | ')).not.toMatch(/\bSCAN\b(?!.*USING (COVERING )?INDEX)/);
    for (const l of a.d1.log) expect(l.plan.join(' | ')).toMatch(/USING (COVERING )?INDEX/);
    const tableRows = Object.values(data).reduce((n, r) => n + r.length, 0);
    const returned = a.d1.log.reduce((n, l) => n + l.rows, 0);
    expect(b.d1.log.some((l) => /\bSCAN\b/.test(l.plan.join(' ')))).toBe(true);     // the old shape scanned
    expect(returned).toBeLessThan(tableRows / 10);
    expect(a.d1.log.length).toBe(9);     // 1 challenger + 3 core + 3 price + 2 lookups for the series that stopped three days ago
  });
});

describe('60 s cache', () => {
  beforeEach(() => freshnessCacheClear());

  it('serves the same report within the TTL without touching the database, then recomputes', async () => {
    const { env, d1 } = world(history());
    const first = await getPredictionFreshnessCached(env, NOW);
    expect(first.cache).toBe('miss');
    const n = d1.log.length;
    const second = await getPredictionFreshnessCached(env, NOW + 59000);
    expect(second).toMatchObject({ cache: 'hit', age_ms: 59000 });
    expect(second.result).toBe(first.result);
    expect(second.result.generated_ts).toBe(NOW);            // the report says when it was computed
    expect(d1.log.length).toBe(n);
    const third = await getPredictionFreshnessCached(env, NOW + 60000);
    expect(third.cache).toBe('miss');
    expect(third.result.generated_ts).toBe(NOW + 60000);
    expect(d1.log.length).toBeGreaterThan(n);
  });

  it('never serves an expired report when the database fails: the error reaches the caller', async () => {
    const { env } = world(history());
    await getPredictionFreshnessCached(env, NOW);
    const broken = { DB: { prepare() { throw new Error('D1_ERROR: too many reads'); } } };
    await expect(getPredictionFreshnessCached(broken, NOW + 61000)).rejects.toThrow('too many reads');
    expect((await getPredictionFreshnessCached(env, NOW + 62000)).cache).toBe('miss');   // nothing was kept from the failure
  });

  it('a report within its TTL is still served if the database fails meanwhile (it is not expired)', async () => {
    const { env } = world(history());
    const first = await getPredictionFreshnessCached(env, NOW);
    const broken = { DB: { prepare() { throw new Error('down'); } } };
    expect((await getPredictionFreshnessCached(broken, NOW + 30000)).result).toBe(first.result);
  });

  it('is cleared by a write or a scheduled run, ignores clock steps backwards, and can be disabled', async () => {
    const { env } = world(history());
    await getPredictionFreshnessCached(env, NOW);
    freshnessCacheClear();
    expect((await getPredictionFreshnessCached(env, NOW + 1000)).cache).toBe('miss');
    expect((await getPredictionFreshnessCached(env, NOW)).cache).toBe('miss');           // entry is "in the future" for this caller
    const off = { ...env, FRESHNESS_CACHE: '0' };
    expect((await getPredictionFreshnessCached(off, NOW + 2000)).cache).toBe('off');
    expect((await getPredictionFreshnessCached(off, NOW + 2001)).cache).toBe('off');
  });
});
