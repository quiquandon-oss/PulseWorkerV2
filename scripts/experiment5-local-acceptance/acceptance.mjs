// LOCAL browser acceptance for the Experiment 5 status + decisions UI.
//
// What this is: real Chromium (Playwright) driving the real worker.js on localhost against a real SQLite file built
// from the real migrations (0008, 0015, 0019), where the REAL pipeline (research/experiment5_pipeline.py via
// pipeline_driver.py) creates REAL decisions from synthetic history and later RESOLVES them against synthetic prices.
// What this is NOT: it is not the deployed Worker, not production or staging D1, and it says nothing about whether
// the challenger predicts well (the data is synthetic by construction). Staging acceptance remains a manual step.
//
// Usage: node --no-warnings acceptance.mjs [--out <dir>] [--worker <path>]
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { startServer } from './server.mjs';

const require = createRequire(import.meta.url);
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const here = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(here, '..', '..');
const args = process.argv.slice(2);
const argVal = (n, d) => { const i = args.indexOf(n); return i >= 0 ? args[i + 1] : d; };
const OUT = resolve(argVal('--out', join(ROOT, 'acceptance-output')));
const WORKER = resolve(argVal('--worker', join(ROOT, 'worker.js')));
mkdirSync(OUT, { recursive: true });
const H = 3600000;

const results = [];
function check(area, name, pass, detail = '') {
  results.push({ area, name, pass: !!pass, detail: String(detail) });
  console.log(`${pass ? 'PASS' : 'FAIL'}  [${area}] ${name}${detail && !pass ? `  -- ${detail}` : ''}`);
}

const MIGRATIONS = join(ROOT, '.ai', 'migrations');
function buildDb(path, { runTable = true, runTableSql = null } = {}) {
  rmSync(path, { force: true });
  const db = new DatabaseSync(path);
  db.exec(`CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL, btc_price REAL,
    sources_json TEXT, technical_score INTEGER, gold_regime TEXT);
    CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL);
    CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, horizon_hours INTEGER, p_up REAL, realized_up INTEGER);`);
  const migration = (prefix) => readFileSync(join(MIGRATIONS, readdirSync(MIGRATIONS).find((f) => f.startsWith(prefix + '_'))), 'utf8');
  db.exec(migration('0008'));
  db.exec(migration('0015'));
  if (runTableSql) db.exec(runTableSql);
  else if (runTable) db.exec(migration('0019'));
  return db;
}

function seedSignal(db, base) {
  // Rising on-chain source for 4h, then a sharp reversal at hour 4 -> the agent proposes a REVERSAL decision anchored at base+4h.
  const ins = db.prepare('INSERT INTO history (ts, score, sources_json) VALUES (?, ?, ?)');
  for (let i = 0; i < 4; i++) ins.run(base + i * H, 60, JSON.stringify({ onchain: 50 + i * 10, fng: 40 + i }));
  ins.run(base + 4 * H, 40, JSON.stringify({ onchain: 10, fng: 45 }));   // V1 composite 40 => baseline direction DOWN
  const btc = db.prepare('INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)');
  for (let h = 0; h <= 40; h++) btc.run(base + h * H, 100 + (h >= 4 ? (h - 4) * 0.5 : 0)); // price rises after the anchor => realized UP
  ins.run(base + 6 * H, 45, '{broken json');   // one malformed observation
}

function driver(dbPath, nowTs, mode = 'ok') {
  const r = spawnSync('python3', [join(here, 'pipeline_driver.py'), dbPath, String(nowTs), mode], { encoding: 'utf8' });
  try { return JSON.parse(r.stdout.trim().split('\n').pop()); } catch (_e) { return { driverError: r.stdout + r.stderr }; }
}

const label2 = (area) => area;
async function scenario(browser, label, viewport, mobile) {
  const area = mobile ? 'mobile' : 'desktop';
  const base = Date.now() - 80 * H;
  const dbPath = join(OUT, `${label}.db`);
  const db = buildDb(dbPath);
  seedSignal(db, base);
  db.close();
  const q = new DatabaseSync(dbPath);
  const one = (sql, ...a) => q.prepare(sql).get(...a);
  const count = (t, w = '1=1') => Number(one(`SELECT COUNT(*) n FROM ${t} WHERE ${w}`).n);

  // ---- the REAL pipeline creates a REAL decision ----
  const run1 = driver(dbPath, base + 10 * H);
  check(area, 'run 1 (real pipeline): archives the good rows, isolates the malformed one, creates one decision, records the run',
    run1.status === 'OK' && run1.newly_archived === 5 && run1.observations_rejected_malformed === 1 && run1.decisions_created === 1 && run1.run_record === 'WRITTEN', JSON.stringify(run1));
  const decision = one("SELECT * FROM research_hypotheses WHERE subject LIKE 'experiment5:%'");
  check(area, 'a real decision row exists, unresolved (pending), capped at OBSERVATION', !!decision && decision.out_of_sample_status === null && decision.status === 'OBSERVATION', JSON.stringify(decision));
  const payload = JSON.parse(decision.evidence_summary_json);
  check(area, 'the decision records its own anchor, direction and eligibility time (anchor + 24h)', payload.decision.anchor_ts === base + 4 * H && payload.decision.eligible_ts === base + 28 * H, JSON.stringify(payload.decision).slice(0, 300));

  // idempotency: the same run again must not create a second decision
  const run1b = driver(dbPath, base + 10 * H);
  check(area, 're-running with identical inputs creates NO duplicate decision (skipped as duplicate) and archives nothing new',
    run1b.decisions_created === 0 && run1b.decisions_skipped_duplicate === 1 && run1b.newly_archived === 0 && count('research_hypotheses', "subject LIKE 'experiment5:%'") === 1, JSON.stringify(run1b));

  // a run before the horizon leaves the decision pending; a run after resolves it exactly once
  const early = driver(dbPath, base + 20 * H);
  check(area, 'a run before the 24h horizon does not resolve the decision (never forced)', early.decisions_evaluated === 0 && one('SELECT out_of_sample_status s FROM research_hypotheses WHERE hypothesis_id = ?', decision.hypothesis_id).s === null, JSON.stringify(early));
  const late = driver(dbPath, base + 40 * H);
  check(area, 'a run after the horizon resolves it exactly once, under its own real id', late.decisions_evaluated === 1 && late.decisions_replayed === 1, JSON.stringify(late));
  const resolved = one('SELECT * FROM research_hypotheses WHERE hypothesis_id = ?', decision.hypothesis_id);
  const outcome = JSON.parse(resolved.evidence_summary_json).outcome;
  check(area, 'resolved: outcome appended (decision payload untouched), realized UP, V1 baseline DOWN, status set',
    !!outcome && outcome.realized_direction === 'UP' && outcome.v1_baseline_direction === 'DOWN' && ['PASSED_HOLDOUT', 'FAILED_HOLDOUT'].includes(resolved.out_of_sample_status) &&
    JSON.stringify(JSON.parse(resolved.evidence_summary_json).decision) === JSON.stringify(payload.decision), JSON.stringify(outcome));
  const snapshot = JSON.stringify(q.prepare('SELECT * FROM research_hypotheses').all());
  const again = driver(dbPath, base + 40 * H);
  check(area, 'repeating the late run changes nothing (resolution is idempotent and append-only)', again.decisions_evaluated === 0 && JSON.stringify(q.prepare('SELECT * FROM research_hypotheses').all()) === snapshot, JSON.stringify(again));

  // a genuinely failing run, then a good one
  const failing = driver(dbPath, base + 41 * H, 'fail');
  check(area, 'a failing run is recorded as FAILED and raised (the job would go red)', /OperationalError/.test(failing.raised || '') && one("SELECT status s FROM experiment5_pipeline_runs ORDER BY run_id DESC LIMIT 1").s === 'FAILED', JSON.stringify(failing));
  const ok2 = driver(dbPath, Date.now() - 1 * H);
  check(area, 'the next good run is recorded OK (consecutive failures reset)', ok2.run_record === 'WRITTEN', JSON.stringify(ok2));

  // ---- UI ----
  const srv = await startServer({ workerPath: WORKER, dbPath, port: mobile ? 8813 : 8812 });
  const context = await browser.newContext({ viewport, isMobile: mobile, hasTouch: mobile, baseURL: `http://127.0.0.1:${mobile ? 8813 : 8812}` });
  const page = await context.newPage();
  const errors = []; page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto('/research-lab');
  await page.click('button[data-page="Experiment 5"]');
  await page.waitForSelector('[data-testid="exp5-operational"]');
  const op = await page.locator('[data-testid="exp5-operational"]').innerText();
  const pr = await page.locator('[data-testid="exp5-predictive"]').innerText();
  check(area, 'execution card: last run OK, last failure shown with its error, last run counters present', /LAST RUN/i.test(op) && /OK/.test(op) && /LAST FAILURE/i.test(op) && /OperationalError/.test(op) && /skipped as duplicates/i.test(op), op.slice(0, 600));
  check(area, 'execution card states what it measures and its agent-window counters', /Pipeline execution \(is it running\?\)/i.test(op) && /agent window/i.test(op));
  check(area, 'predictive card: 1 resolved decision, below the minimum sample, NO CONCLUSION, no criterion defined (a healthy pipeline is not evidence of skill)',
    /RESOLVED DECISIONS\s*\n?\s*1/i.test(pr) && /not reached/i.test(pr) && /NO CONCLUSION/.test(pr) && /none defined/i.test(pr), pr.slice(0, 600));
  const decisionsText = await page.locator('#app').innerText();
  check(area, 'the Decisions list shows the real, resolved decision with its outcome', /experiment5:reversal:onchain/.test(decisionsText) && /(PASSED|FAILED|matched|did not)/i.test(decisionsText), decisionsText.slice(0, 200));
  check(area, 'no JavaScript errors on the Experiment 5 tab', errors.length === 0, errors.join(' | '));
  const o = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }));
  check(area, `no horizontal overflow at ${viewport.width}px`, o.sw <= o.iw + 1, JSON.stringify(o));
  await page.screenshot({ path: join(OUT, `${label}-01-healthy.png`), fullPage: true });
  await context.close(); await srv.close();

  // ---- states: telemetry missing, schema behind, transient database error ----
  const states = [
    ['telemetry table missing (migration 0019 not applied)', { runTable: false }, null, /TELEMETRY NOT RECORDED/, /UNKNOWN/],
    ['run-log schema behind (a column missing)', { runTableSql: 'CREATE TABLE experiment5_pipeline_runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_ts INTEGER NOT NULL, status TEXT NOT NULL, pipeline_version TEXT NOT NULL, constants_json TEXT NOT NULL); INSERT INTO experiment5_pipeline_runs (run_ts, status, pipeline_version, constants_json) VALUES (1, \'OK\', \'v\', \'{}\');' }, null, /RUN LOG SCHEMA BEHIND/, /missing column/i],
    ['transient database error on the run log', {}, (d) => ({ prepare(sql) { if (sql.includes('experiment5_pipeline_runs')) throw new Error('D1_ERROR: Network connection lost'); return d.prepare(sql); } }), /RUN LOG TEMPORARILY UNREADABLE/, /NOT evidence that migration 0019 is missing/],
  ];
  let port = mobile ? 8823 : 8822;
  let stateIndex = 0;
  for (const [name, opts, wrap, label, extra] of states) {
    stateIndex++;
    const p = join(OUT, `${label2(area)}-state${stateIndex}.db`);
    const sdb = buildDb(p, opts); seedSignal(sdb, base); sdb.close();
    const s = await startServer({ workerPath: WORKER, dbPath: p, port, wrapDb: wrap || ((d) => d) });
    const ctx = await browser.newContext({ viewport, isMobile: mobile, hasTouch: mobile, baseURL: `http://127.0.0.1:${port}` });
    const pg = await ctx.newPage();
    const errs = []; pg.on('pageerror', (e) => errs.push(String(e)));
    await pg.goto('/research-lab');
    await pg.click('button[data-page="Experiment 5"]');
    await pg.waitForSelector('[data-testid="exp5-operational"]');
    const t = await pg.locator('[data-testid="exp5-operational"]').innerText();
    check(area, `${name}: shown as its own state, execution health UNKNOWN, never as OK/FAILED`, label.test(t) && extra.test(t) && /UNKNOWN/.test(t) && !/\nOK\b/.test(t.replace(/Execution health[\s\S]*/, '')) , t.slice(0, 500));
    const ov = await pg.evaluate(() => fetch('/api/research-lab/experiment5-status').then((r) => r.json()));
    check(area, `${name}: the status API says so explicitly and the predictive block is still served`, ov.ok === true && ov.operational.run_log_state !== 'AVAILABLE' && ov.predictive && ov.predictive.state === 'AVAILABLE', JSON.stringify(ov.operational).slice(0, 200));
    check(area, `${name}: no JS errors, no horizontal overflow`, errs.length === 0 && (await pg.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)));
    await pg.screenshot({ path: join(OUT, `${label2(area)}-state${stateIndex}.png`), fullPage: true });
    await ctx.close(); await s.close(); port++;
  }
  q.close();
}

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome', args: ['--no-sandbox'] });
try {
  await scenario(browser, 'desktop', { width: 1280, height: 900 }, false).catch((e) => check('desktop', 'scenario aborted', false, String(e && e.message || e).split('\n')[0]));
  await scenario(browser, 'mobile', { width: 390, height: 844 }, true).catch((e) => check('mobile', 'scenario aborted', false, String(e && e.message || e).split('\n')[0]));
} finally { await browser.close(); }
const failed = results.filter((r) => !r.pass);
writeFileSync(join(OUT, 'results.json'), JSON.stringify(results, null, 2));
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) { console.log('FAILED:'); failed.forEach((f) => console.log(` - [${f.area}] ${f.name}: ${f.detail}`)); }
process.exit(failed.length ? 1 : 0);
