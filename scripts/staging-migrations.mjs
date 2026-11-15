// Staging-only D1 migration runner for .github/workflows/deploy-sv2-staging.yml.
//
// The workflow copies THIS file from origin/main (never from the ref being deployed) and runs it with the staging
// token. The SQL files come from the deployed ref and are treated as data: each one must be additive.
//
// Guarantees (all fail closed; any problem throws and the workflow stops before the next migration or the deploy):
//   - the wrangler config must name exactly the staging Worker and the staging D1 database, and must contain no
//     production identifier on any non-comment line;
//   - before any SQL is executed, `wrangler d1 info` must report the staging database id, and no production id;
//   - migrations run in ascending numeric order from a fixed plan; a listed file that is missing is an ERROR (it is
//     never skipped), except 0029, which is required exactly when the ref contains the evidence collector that needs it;
//   - SQL must be additive: CREATE TABLE/INDEX or ALTER TABLE ... ADD COLUMN, nothing that deletes or rewrites data;
//   - 0015 has no IF NOT EXISTS, so it is run only when its table is absent (checked, not guessed from an error text);
//   - after the last migration every table the SQL creates must exist.
// Only the commands below are ever run, always with `-c <staging config>` and the staging database name.
import { readFileSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';

export const STAGING = Object.freeze({
  worker: 'pulseworker-v2-staging',
  database_name: 'pulseworker-v2-staging',
  database_id: '5458d504-2778-49ae-bd25-7751f1c49d50',
});
export const PRODUCTION = Object.freeze({
  worker: 'pulseworker-v2',
  database_name: 'sentiment-history',
  database_id: 'f91ca980-b886-423a-bd6f-f3baea46d181',
});

// Order matters and is ascending. `guardTable`: not idempotent, so run only when that table is absent.
// `requiresPath`: required only when the deployed ref contains that file (the code that needs these tables).
export const PLAN = Object.freeze([
  { file: '0015_experiment5_sentiment_archive.sql', guardTable: 'research_sentiment_archive' },
  { file: '0023_event_detection.sql' },
  { file: '0026_d1_usage_daily.sql' },
  { file: '0027_sentiment_v2_results.sql' },
  { file: '0029_sv2_evidence_and_ai_budget.sql', requiresPath: 'sentiment-v2/collector.js' },
]);

const nonComment = (text) => text.split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');

// Throws unless the config is exactly the staging target.
export function assertStagingConfig(tomlText) {
  const body = nonComment(tomlText);
  const has = (re) => re.test(body);
  if (!has(new RegExp(`^name = "${STAGING.worker}"\\s*$`, 'm'))) throw new Error('config does not declare the staging Worker name');
  if (!has(new RegExp(`^database_name = "${STAGING.database_name}"\\s*$`, 'm'))) throw new Error('config does not bind the staging D1 database name');
  if (!has(new RegExp(`^database_id = "${STAGING.database_id}"\\s*$`, 'm'))) throw new Error('config does not bind the staging D1 database id');
  if ((body.match(/^database_id\s*=/gm) || []).length !== 1) throw new Error('config must bind exactly one D1 database');
  if (has(new RegExp(`^name = "${PRODUCTION.worker}"\\s*$`, 'm')) || body.includes(PRODUCTION.database_id) || has(new RegExp(`database_name\\s*=\\s*"${PRODUCTION.database_name}"`))) {
    throw new Error('config contains a production Worker or D1 identifier');
  }
}

// Strips SQL comments; returns the statements.
export function statements(sql) {
  const noComments = sql.replace(/--[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '');
  return noComments.split(';').map((s) => s.trim()).filter(Boolean);
}

const ADDITIVE = [
  /^CREATE\s+(UNIQUE\s+)?INDEX\s+(IF\s+NOT\s+EXISTS\s+)?[\w"`]+\s+ON\s+/i,
  /^CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?[\w"`]+\s*\(/i,
  /^ALTER\s+TABLE\s+[\w"`]+\s+ADD\s+COLUMN\s+/i,
];
// Throws unless every statement is additive. Returns the tables the SQL creates.
export function assertAdditive(file, sql) {
  const tables = [];
  for (const st of statements(sql)) {
    if (!ADDITIVE.some((re) => re.test(st))) throw new Error(`${file}: not an additive statement: ${st.slice(0, 60).replace(/\s+/g, ' ')}`);
    const m = st.match(/^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)/i);
    if (m) tables.push(m[1]);
  }
  return tables;
}

// The migrations to run for this checkout, in order. `exists(relPath)` checks the deployed ref.
export function resolvePlan(dir, exists) {
  const out = [];
  let last = '';
  for (const m of PLAN) {
    if (m.file <= last) throw new Error(`migration plan is not strictly ascending at ${m.file}`);
    last = m.file;
    const present = exists(join(dir, m.file));
    if (m.requiresPath) {
      const needed = exists(m.requiresPath);
      if (!needed) continue;     // this ref has no code that needs the tables: not part of its plan
      if (!present) throw new Error(`${m.file} is required by this ref (${m.requiresPath}) but the file is missing`);
    } else if (!present) {
      throw new Error(`${m.file} is part of the staging plan but is missing from this ref`);
    }
    out.push(m);
  }
  return out;
}

function defaultWrangler(args, env = process.env) {
  return execFileSync('npx', ['--yes', 'wrangler@4', ...args], { encoding: 'utf8', env, stdio: ['ignore', 'pipe', 'pipe'], timeout: 180_000 });
}

function parseJson(text, what) {
  const start = text.search(/[[{]/);
  const end = Math.max(text.lastIndexOf(']'), text.lastIndexOf('}'));
  if (start < 0 || end < start) throw new Error(`no JSON in wrangler output for ${what}`);
  try { return JSON.parse(text.slice(start, end + 1)); } catch { throw new Error(`could not parse wrangler output for ${what}`); }
}
const allStrings = (v, acc = []) => { if (typeof v === 'string') acc.push(v); else if (v && typeof v === 'object') Object.values(v).forEach((x) => allStrings(x, acc)); return acc; };

// Applies the plan. `wrangler(args)` is injectable for tests. Returns a log of what happened.
export function applyMigrations({ dir = '.ai/migrations', config = 'wrangler.staging.toml', readText = (p) => readFileSync(p, 'utf8'), exists = existsSync, wrangler = defaultWrangler } = {}) {
  const log = [];
  assertStagingConfig(readText(config));
  const plan = resolvePlan(dir, exists);
  const sqlByFile = new Map();
  const expectedTables = [];
  for (const m of plan) {            // validate every file BEFORE touching the database
    const sql = readText(join(dir, m.file));
    expectedTables.push(...assertAdditive(m.file, sql));
    sqlByFile.set(m.file, sql);
  }
  const base = ['-c', config];
  const run = (args) => wrangler(args);

  // 1. The database wrangler resolves must be the staging database, before any SQL runs.
  const info = parseJson(run(['d1', 'info', STAGING.database_name, ...base, '--json']), 'd1 info');
  const strings = allStrings(info);
  if (strings.includes(PRODUCTION.database_id)) throw new Error('d1 info reported the production database id');
  if (!strings.includes(STAGING.database_id)) throw new Error('d1 info did not report the staging database id');
  log.push(`target verified: ${STAGING.database_name} ${STAGING.database_id}`);

  const query = (sql) => parseJson(run(['d1', 'execute', STAGING.database_name, '--remote', ...base, '--json', '--command', sql]), 'd1 query');
  const tableExists = (name) => allStrings(query(`SELECT name FROM sqlite_master WHERE type='table' AND name='${name}'`)).includes(name);

  for (const m of plan) {
    if (m.guardTable && tableExists(m.guardTable)) { log.push(`kept ${m.file} (table ${m.guardTable} exists)`); continue; }
    run(['d1', 'execute', STAGING.database_name, '--remote', ...base, '--file', join(dir, m.file)]);
    log.push(`applied ${m.file}`);
  }

  for (const t of expectedTables) if (!tableExists(t)) throw new Error(`expected table ${t} does not exist after the migrations`);
  log.push(`verified ${expectedTables.length} tables`);
  return log;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const cmd = process.argv[2];
  try {
    if (cmd === 'plan') {
      assertStagingConfig(readFileSync('wrangler.staging.toml', 'utf8'));
      for (const m of resolvePlan('.ai/migrations', existsSync)) console.log(m.file);
    } else if (cmd === 'apply') {
      for (const line of applyMigrations()) console.log(line);
    } else {
      throw new Error('usage: staging-migrations.mjs plan|apply');
    }
  } catch (err) {
    console.error(`::error::${err.message}`);
    process.exit(1);
  }
}
