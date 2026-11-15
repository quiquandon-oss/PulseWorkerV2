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

// A quote-aware SQL tokenizer (SQLite lexical rules): 'strings' and "identifiers" with doubled-quote escapes, `identifiers`,
// [identifiers], -- line comments and /* block comments */. Returns tokens { t: 'word' | 'string' | 'quoted' | 'punct' |
// 'space' | 'comment', v }. An unterminated string, identifier or comment THROWS: the file is never partly understood.
export function tokenize(sql) {
  const toks = [];
  let i = 0;
  const n = sql.length;
  const quoted = (open, close, type) => {
    let j = i + 1;
    for (;;) {
      if (j >= n) throw new Error(`unterminated ${type} starting at offset ${i}`);
      if (sql[j] === close) { if (close !== ']' && sql[j + 1] === close) { j += 2; continue; } break; }
      j += 1;
    }
    toks.push({ t: type === 'string' ? 'string' : 'quoted', v: sql.slice(i, j + 1) });
    i = j + 1;
  };
  while (i < n) {
    const c = sql[i];
    if (/\s/.test(c)) { let j = i; while (j < n && /\s/.test(sql[j])) j += 1; toks.push({ t: 'space', v: sql.slice(i, j) }); i = j; }
    else if (c === '-' && sql[i + 1] === '-') { let j = sql.indexOf('\n', i); if (j < 0) j = n; toks.push({ t: 'comment', v: sql.slice(i, j) }); i = j; }
    else if (c === '/' && sql[i + 1] === '*') { const j = sql.indexOf('*/', i + 2); if (j < 0) throw new Error(`unterminated comment starting at offset ${i}`); toks.push({ t: 'comment', v: sql.slice(i, j + 2) }); i = j + 2; }
    else if (c === "'") quoted("'", "'", 'string');
    else if (c === '"') quoted('"', '"', 'identifier');
    else if (c === '`') quoted('`', '`', 'identifier');
    else if (c === '[') quoted('[', ']', 'identifier');
    else if (/[A-Za-z0-9_$\u0080-￿]/.test(c)) { let j = i; while (j < n && /[A-Za-z0-9_$.\u0080-￿]/.test(sql[j])) j += 1; toks.push({ t: 'word', v: sql.slice(i, j) }); i = j; }
    else { toks.push({ t: 'punct', v: c }); i += 1; }
  }
  return toks;
}

// The statements of a file, split on ';' OUTSIDE strings, identifiers and comments; comments become one space and
// string contents are kept exactly. Throws on an unterminated quote or comment.
export function statements(sql) {
  const out = [];
  let cur = '';
  for (const tok of tokenize(sql)) {
    if (tok.t === 'punct' && tok.v === ';') { out.push(cur); cur = ''; continue; }
    cur += tok.t === 'comment' ? ' ' : tok.v;
  }
  out.push(cur);
  return out.map((s) => s.trim()).filter(Boolean);
}

const ADDITIVE = [
  /^CREATE\s+(UNIQUE\s+)?INDEX\s+(IF\s+NOT\s+EXISTS\s+)?[\w"`]+\s+ON\s+/i,
  /^CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?[\w"`]+\s*\(/i,
  /^ALTER\s+TABLE\s+[\w"`]+\s+ADD\s+COLUMN\s+/i,
];
// Canonical form of a CREATE statement for comparison. Ignored: comments, whitespace, IF NOT EXISTS (SQLite does not store
// it) and the case of keywords and unquoted names. Compared exactly: string literals (case-sensitive), quoted identifiers,
// and everything that defines the object (columns, types, constraints, defaults, index columns, uniqueness).
export function normalizeSql(sql) {
  const toks = tokenize(sql).filter((x) => x.t !== 'space' && x.t !== 'comment');
  const end = toks.findIndex((x) => x.t === 'punct' && x.v === ';');
  const one = end < 0 ? toks : toks.slice(0, end);
  const words = one.map((x) => (x.t === 'word' ? { ...x, v: x.v.toLowerCase() } : x));
  for (let k = 0; k + 2 < words.length; k += 1) {
    if (words[k].t === 'word' && words[k].v === 'if' && words[k + 1].v === 'not' && words[k + 2].v === 'exists') { words.splice(k, 3); break; }
  }
  let out = '';
  words.forEach((x, k) => {
    const tight = (v) => v === '(' || v === ')' || v === ',';
    if (k > 0 && !(x.t === 'punct' && tight(x.v)) && !(words[k - 1].t === 'punct' && tight(words[k - 1].v))) out += ' ';
    out += x.v;
  });
  return out;
}

// Throws unless every statement is additive. Returns the objects the SQL creates and their canonical definitions:
// [{ type: 'table' | 'index', name, sql }].
export function assertAdditive(file, sql) {
  const objects = [];
  for (const st of statements(sql)) {
    if (!ADDITIVE.some((re) => re.test(st))) throw new Error(`${file}: not an additive statement: ${st.slice(0, 60).replace(/\s+/g, ' ')}`);
    const t = st.match(/^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)/i);
    if (t) objects.push({ type: 'table', name: t[1], sql: normalizeSql(st) });
    const i = st.match(/^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)/i);
    if (i) objects.push({ type: 'index', name: i[1], sql: normalizeSql(st) });
  }
  return objects;
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
  const expected = [];
  const objectsOf = new Map();
  for (const m of plan) {            // validate every file BEFORE touching the database
    const sql = readText(join(dir, m.file));
    const objects = assertAdditive(m.file, sql);
    objectsOf.set(m.file, objects);
    expected.push(...objects);
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
  // One query returns the stored definition of every object the plan creates (names are \w+ only, from the SQL files).
  const rowsOf = (json) => (Array.isArray(json) ? json : [json]).flatMap((r) => (r && Array.isArray(r.results) ? r.results : []));
  const snapshot = () => {
    const names = [...new Set(expected.map((o) => `'${o.name}'`))].join(',');
    const have = new Map();
    for (const r of rowsOf(query(`SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','index') AND name IN (${names})`))) {
      have.set(`${r.type}:${r.name}`, normalizeSql(String(r.sql || '')));
    }
    return have;
  };
  // 'absent', 'match' or 'drift' (exists with a different definition than the reviewed migration).
  const state = (have, o) => (!have.has(`${o.type}:${o.name}`) ? 'absent' : have.get(`${o.type}:${o.name}`) === o.sql ? 'match' : 'drift');
  const assertNoDrift = (have, when) => {
    const drift = expected.filter((o) => state(have, o) === 'drift').map((o) => `${o.type} ${o.name}`);
    if (drift.length) throw new Error(`${when}: ${drift.join(', ')} exist on staging with a definition that differs from the reviewed migration (schema drift). Refusing to continue or deploy; repair the staging database by hand, then rerun.`);
  };

  // Before anything runs: an existing object whose definition differs would be silently left alone by CREATE ... IF NOT EXISTS.
  const before = snapshot();
  assertNoDrift(before, 'before the migrations');

  for (const m of plan) {
    if (m.guardTable) {
      // Not idempotent (no IF NOT EXISTS): decide from the database state, for EVERY object the file creates. A partial
      // state (for example the table without an index after an interrupted run) is never treated as done.
      const objects = objectsOf.get(m.file);
      const present = objects.filter((o) => state(before, o) === 'match');
      if (present.length === objects.length) { log.push(`kept ${m.file} (all ${objects.length} objects exist with the reviewed definition)`); continue; }
      if (present.length > 0) {
        const missing = objects.filter((o) => !present.includes(o)).map((o) => `${o.type} ${o.name}`).join(', ');
        throw new Error(`${m.file} is partially applied on staging (missing: ${missing}). Refusing to continue or deploy; repair the staging database by hand, then rerun.`);
      }
    }
    run(['d1', 'execute', STAGING.database_name, '--remote', ...base, '--file', join(dir, m.file)]);
    log.push(`applied ${m.file}`);
  }

  const after = snapshot();
  assertNoDrift(after, 'after the migrations');
  for (const o of expected) if (state(after, o) !== 'match') throw new Error(`expected ${o.type} ${o.name} does not exist after the migrations`);
  log.push(`verified ${expected.length} tables and indexes against the reviewed definitions`);
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
