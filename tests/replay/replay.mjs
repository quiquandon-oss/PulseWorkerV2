// A8 local replay harness: runs the UNMODIFIED PulseWorkerV2 worker.js against a local SQLite
// D1 fake with a frozen clock, seeded randomness and stubbed network, and prints a golden record.
// Usage: node tests/replay/replay.mjs <scenario> [--root <repo>]   (one scenario per process)
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { makeWorld, summarise, H } from './world.mjs';

const T0 = Date.UTC(2026, 10, 1, 0, 0, 0);          // 2026-11-01T00:00Z, a 3h-tick hour
const WARMUP_TICKS = 72;                               // 9 days of 3h ticks: >50 resolved predictions per horizon so selection runs
const scenario = process.argv[2];
const root = resolve(process.argv.includes('--root') ? process.argv[process.argv.indexOf('--root') + 1] : '.');
const arg = (n) => (process.argv.includes(n) ? process.argv[process.argv.indexOf(n) + 1] : null);
const workerPath = arg('--worker');                       // run a different worker.js (e.g. a baseline copy) against the same world
const omitTables = arg('--omit-tables') ? arg('--omit-tables').split(',') : [];
const sha = (s) => createHash('sha256').update(s).digest('hex');

console.log = (...a) => console.error(...a);              // the worker's own logs go to stderr; stdout carries only the result
const { d1, cronTick, get } = await makeWorld({ root, t0: T0, warmupTicks: WARMUP_TICKS, workerPath, omitTables });
function tableState() {
  const out = {};
  for (const { name } of d1.db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").all()) {
    const rows = d1.db.prepare(`SELECT * FROM ${name} ORDER BY rowid`).all();
    if (rows.length) out[name] = { rows: rows.length, sha256: sha(JSON.stringify(rows)) };
  }
  return out;
}
const warmState = tableState();

const ROUTES = { predict: '/predict?horizon=24', eth_predict: '/eth-predict?horizon=24', link_predict: '/link-predict?horizon=24',
  chart_data: '/chart-data?horizon=24', calibration: '/calibration?horizon=24', calibration_history: '/calibration-history?horizon=24',
  prediction_freshness: '/prediction-freshness', challenger_calibration: '/challenger-calibration?coin=BTC&horizon=24',
  selection_history: '/selection-history?coin=BTC&horizon=24', select_variant: '/select-variant?coin=BTC&horizon=24' };

let result;
if (scenario === 'cron_tick') { await cronTick(T0); result = {}; }
else if (ROUTES[scenario]) result = await get(ROUTES[scenario], T0 + H);   // 1 h after the last tick: data is fresh
else { console.error('unknown scenario', scenario, 'choose: cron_tick', Object.keys(ROUTES).join(' ')); process.exit(2); }

const endState = tableState();
const changed = Object.fromEntries(Object.keys({ ...warmState, ...endState }).filter((t) => JSON.stringify(warmState[t]) !== JSON.stringify(endState[t])).map((t) => [t, { before: warmState[t]?.rows || 0, after: endState[t]?.rows || 0 }]));
process.stdout.write(JSON.stringify({ scenario, warmup_state_sha256: sha(JSON.stringify(warmState)),
  response: result.status !== undefined ? { status: result.status, sha256: sha(result.body), bytes: result.body.length, body: result.body } : null,
  tables_changed: changed, end_state: endState, end_state_sha256: sha(JSON.stringify(endState)), d1: summarise(d1.log),
  statements: d1.log.map((l) => ({ sql: l.sql.slice(0, 160), rows: l.rows, plan: l.plan })) }) + '\n');
