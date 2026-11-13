// Replays the Worker's own prediction, calibration, challenger and selection code over the exported price series,
// in a local SQLite sandbox. Nothing here reads or writes production.
//
//   node research/price_dup_replay/replay-arm.mjs <stored|one-row-per-run> <exports dir> <history.json> <out.json>
//
// Arm "stored": every exported price row, with its stored technical score.
// Arm "one-row-per-run": the first row of each run (rows within 10 minutes); from the coin's first multi-row run
// onward the technical score is recomputed with the Worker's formula from the 30 prices before it, as the logger does.
// A prediction is made at every (coin, horizon, ts) where production stored one, with the clock set to that ts,
// so the two arms and the production record share the same cycles. Every other input is identical in both arms.
import { readFileSync, writeFileSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { makeWorld } from '../../tests/replay/world.mjs';

const [arm, dir, historyPath, outPath] = process.argv.slice(2);
if (!['stored', 'one-row-per-run'].includes(arm)) throw new Error('arm must be stored or one-row-per-run');
const RUN_WINDOW_MS = 10 * 60 * 1000;
const COINS = {
  BTC: { data: 'btc_data', price: 'btc_price', preds: 'predictions', predCsv: 'btc_predictions.csv', run: 'runPrediction', backfill: 'backfillPredictions' },
  ETH: { data: 'eth_data', price: 'eth_price', preds: 'eth_predictions', predCsv: 'eth_predictions.csv', run: 'runEthPrediction', backfill: 'backfillEthPredictions' },
  LINK: { data: 'link_data', price: 'link_price', preds: 'link_predictions', predCsv: 'link_predictions.csv', run: 'runLinkPrediction', backfill: 'backfillLinkPredictions' },
};
function csv(path) {   // RFC 4180: quoted fields may contain commas, quotes and newlines
  const text = readFileSync(path, 'utf8'); const rows = []; let row = [], f = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) { if (ch === '"') { if (text[i + 1] === '"') { f += '"'; i++; } else q = false; } else f += ch; }
    else if (ch === '"') q = true;
    else if (ch === ',') { row.push(f); f = ''; }
    else if (ch === '\n') { row.push(f); rows.push(row); row = []; f = ''; }
    else if (ch !== '\r') f += ch;
  }
  if (f !== '' || row.length) { row.push(f); rows.push(row); }
  const cols = rows.shift();
  return rows.filter((r) => r.length === cols.length).map((v) => Object.fromEntries(cols.map((c, i) => [c, v[i]])));
}

const root = resolve('.');
// The unmodified worker.js plus one export line, so its internal functions can be called directly.
const patched = join(root, `.price-dup-replay-worker-${process.pid}.mjs`);   // beside worker.js so its relative imports resolve; removed at exit
process.on('exit', () => { try { rmSync(patched); } catch {} });
const names = ['computeSimpleTechnicalScore', 'backfillChallengerPredictions', 'runChallengerPrediction', 'selectBestVariant',
  ...Object.values(COINS).flatMap((c) => [c.run, c.backfill])];
writeFileSync(patched, readFileSync(join(root, 'worker.js'), 'utf8') + `\nexport { ${names.join(', ')} };\n`);

const realLog = console.log, realErr = console.error;
console.log = () => {}; console.error = () => {};
const world = await makeWorld({ root, t0: Date.UTC(2026, 0, 1), warmupTicks: 0, workerPath: patched });
const W = await import(pathToFileURL(patched).href + '?direct=1');
const db = world.d1.db;
for (const { name } of db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").all()) db.exec(`DELETE FROM ${name}`);
globalThis.fetch = async () => new Response('replay: external call blocked', { status: 503 });

// Price rows per coin for this arm.
const priceRows = [];
for (const [coin, c] of Object.entries(COINS)) {
  const rows = csv(join(dir, `${c.data}.csv`)).map((r) => ({ coin, ts: Number(r.ts), price: Number(r[c.price]),
    tech: r.technical_score === '' ? null : Number(r.technical_score), funding: r.funding_adj === undefined || r.funding_adj === '' ? null : Number(r.funding_adj) }))
    .filter((r) => Number.isFinite(r.ts) && Number.isFinite(r.price)).sort((a, b) => a.ts - b.ts);
  if (arm === 'stored') { priceRows.push(...rows); continue; }
  let runStart = -Infinity, dupSeen = false; const kept = [];
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].ts - runStart <= RUN_WINDOW_MS) continue;
    runStart = rows[i].ts;
    if (!dupSeen && rows[i + 1] && rows[i + 1].ts - runStart <= RUN_WINDOW_MS) dupSeen = true;
    const row = { ...rows[i] };
    if (dupSeen) row.tech = W.computeSimpleTechnicalScore(kept.slice(-30).map((k) => k.price));
    kept.push(row);
  }
  priceRows.push(...kept);
}
priceRows.sort((a, b) => a.ts - b.ts);
const history = JSON.parse(readFileSync(historyPath, 'utf8')).sort((a, b) => a.ts - b.ts);
const cycles = [];
for (const [coin, c] of Object.entries(COINS)) for (const p of csv(join(dir, c.predCsv))) cycles.push({ coin, ts: Number(p.ts), horizon: Number(p.horizon_hours || 24) });
cycles.sort((a, b) => a.ts - b.ts);

const ins = {
  BTC: db.prepare('INSERT INTO btc_data (ts, btc_price, technical_score) VALUES (?,?,?)'),
  ETH: db.prepare('INSERT INTO eth_data (ts, eth_price, technical_score) VALUES (?,?,?)'),
  LINK: db.prepare('INSERT INTO link_data (ts, link_price, technical_score, funding_adj) VALUES (?,?,?,?)'),
};
const insHist = db.prepare('INSERT INTO history (ts, score, regime_mag, bottom_score) VALUES (?,?,?,?)');
let pi = 0, hi = 0, done = 0; const failures = [];
const t0 = performance.now();
for (const cy of cycles) {
  for (; pi < priceRows.length && priceRows[pi].ts <= cy.ts; pi++) {
    const r = priceRows[pi];
    if (r.coin === 'LINK') ins.LINK.run(r.ts, r.price, r.tech, r.funding); else ins[r.coin].run(r.ts, r.price, r.tech);
  }
  for (; hi < history.length && history[hi].ts <= cy.ts; hi++) insHist.run(history[hi].ts, history[hi].score, history[hi].regime_mag, history[hi].bottom_score);
  world.clock.now = cy.ts;
  const c = COINS[cy.coin];
  try {
    await W[c.backfill](world.env);
    await W.backfillChallengerPredictions(world.env);
    const result = await W[c.run](world.env, cy.horizon, { persist: true });
    try {
      await W.runChallengerPrediction(world.env, { coin: cy.coin, horizonHours: cy.horizon, priceTable: c.data, priceCol: c.price,
        priceNow: result[`${cy.coin.toLowerCase()}_price_now`], coreResult: result }, { persist: true });
    } catch (e) { failures.push({ step: 'challenger', ...cy, error: String(e) }); }
    try { await W.selectBestVariant(world.env, cy.coin, cy.horizon); } catch (e) { failures.push({ step: 'selection', ...cy, error: String(e) }); }
  } catch (e) { failures.push({ step: 'prediction', ...cy, error: String(e) }); }
  if (++done % 500 === 0) realErr(`${arm}: ${done}/${cycles.length} cycles, ${((performance.now() - t0) / 1000).toFixed(0)} s`);
}
const all = (t) => db.prepare(`SELECT * FROM ${t} ORDER BY rowid`).all();
const out = { arm, cycles: cycles.length, price_rows: priceRows.length, failures: failures.length, failure_sample: failures.slice(0, 10),
  predictions: Object.fromEntries(Object.entries(COINS).map(([coin, c]) => [coin, all(c.preds)])),
  challenger_predictions: all('challenger_predictions'), selection_decisions: all('selection_decisions') };
writeFileSync(outPath, JSON.stringify(out));
world.close();
realLog(JSON.stringify({ arm, cycles: out.cycles, price_rows: out.price_rows, failures: out.failures, failure_sample: out.failure_sample.slice(0, 3),
  rows: Object.fromEntries(Object.entries(out.predictions).map(([k, v]) => [k, v.length])), challenger: out.challenger_predictions.length, selection: out.selection_decisions.length }));
