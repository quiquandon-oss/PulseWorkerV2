// scripts/staging-migrations.mjs: ordering, inclusion (0023 and its siblings), additive-only SQL, staging-only target,
// production rejection, fail-closed behaviour and idempotency. wrangler is replaced by an in-memory fake D1: nothing
// here touches Cloudflare.
import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { STAGING, PRODUCTION, PLAN, assertStagingConfig, assertAdditive, statements, normalizeSql, tokenize, resolvePlan, applyMigrations } from '../scripts/staging-migrations.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const real = (p) => readFileSync(join(root, p), 'utf8');
const MIG = '.ai/migrations';
const STAGING_TOML = real('wrangler.staging.toml');
const hasCollector = existsSync(join(root, 'sentiment-v2/collector.js'));

// A fake file system and fake wrangler. `files` maps relative paths to text.
function harness({ collector = false, overrides = {}, failOn = null, infoJson = null, createsNothing = null } = {}) {
  const files = new Map();
  for (const m of PLAN) {
    const p = `${MIG}/${m.file}`;
    if (existsSync(join(root, p))) files.set(p, real(p));
  }
  // 0029 does not exist on every branch: provide a minimal additive stand-in so the plan can be exercised either way
  if (!files.has(`${MIG}/0029_sv2_evidence_and_ai_budget.sql`)) {
    files.set(`${MIG}/0029_sv2_evidence_and_ai_budget.sql`, 'CREATE TABLE IF NOT EXISTS sv2_evidence (id TEXT PRIMARY KEY);\nCREATE INDEX IF NOT EXISTS i_ev ON sv2_evidence (id);');
  }
  files.set('wrangler.staging.toml', STAGING_TOML);
  if (collector) files.set('sentiment-v2/collector.js', '// collector');
  for (const [k, v] of Object.entries(overrides)) { if (v === null) files.delete(k); else files.set(k, v); }

  const calls = [];
  // A fake sqlite_master: "type:name" -> the stored CREATE text. SQLite keeps the statement text as written (inline
  // comments included) but drops IF NOT EXISTS; the fake does the same, so the comparison is realistic.
  const tables = new Map();
  const stored = (chunk) => chunk.slice(chunk.search(/CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\b/i)).trim().replace(/^(CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX))\s+IF\s+NOT\s+EXISTS/i, '$1');
  // Splits on ';' outside `-- comments`, keeping the comments in each statement's text, as SQLite stores them.
  const splitKeepingComments = (sql) => {
    const out = []; let cur = ''; let inComment = false;
    for (const ch of sql) {
      if (inComment) { cur += ch; if (ch === '\n') inComment = false; continue; }
      if (ch === '-' && cur.endsWith('-')) inComment = true;
      if (ch === ';') { out.push(cur); cur = ''; continue; }
      cur += ch;
    }
    out.push(cur);
    return out;
  };
  const applyFile = (file) => {
    const sql = files.get(file) || '';
    for (const chunk of splitKeepingComments(sql)) {
      const t = chunk.replace(/--[^\n]*/g, '').match(/CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)/i);
      if (!t || createsNothing === t[3]) continue;
      const key = `${t[2].toLowerCase()}:${t[3]}`;
      if (!tables.has(key)) tables.set(key, stored(chunk));          // CREATE ... IF NOT EXISTS leaves an existing object as it is
    }
  };
  const wrangler = (args) => {
    calls.push(args);
    const sub = args.slice(0, 2).join(' ');
    if (sub === 'd1 info') return `⛅️ wrangler\n${JSON.stringify(infoJson || { name: STAGING.database_name, uuid: STAGING.database_id })}`;
    if (sub === 'd1 execute') {
      const fi = args.indexOf('--file');
      const ci = args.indexOf('--command');
      const file = fi >= 0 ? args[fi + 1] : null;
      const cmd = ci >= 0 ? args[ci + 1] : '';
      if (file) {
        if (failOn && file.endsWith(failOn)) throw new Error('simulated D1 failure');
        applyFile(file);
        return '[]';
      }
      const names = new Set([...cmd.matchAll(/'(\w+)'/g)].map((m) => m[1]));
      const results = [...tables].map(([k, sql]) => ({ type: k.split(':')[0], name: k.split(':')[1], sql })).filter((r) => names.has(r.name));
      return JSON.stringify([{ results, success: true }]);
    }
    throw new Error(`unexpected wrangler command: ${sub}`);
  };
  const run = () => applyMigrations({ readText: (p) => { if (!files.has(p)) throw new Error(`ENOENT ${p}`); return files.get(p); }, exists: (p) => files.has(p), wrangler });
  const apply = (name) => applyFile(`${MIG}/${name}`);
  return { run, calls, tables, files, apply };
}
const executed = (calls) => calls.filter((a) => a[1] === 'execute' && a.includes('--file')).map((a) => a[a.indexOf('--file') + 1].split('/').pop());

describe('plan: order and inclusion', () => {
  it('lists the event-detection migration 0023 and the D1 usage ledger 0026 with the V2 migrations', () => {
    const names = PLAN.map((m) => m.file);
    expect(names).toContain('0023_event_detection.sql');
    expect(names).toContain('0026_d1_usage_daily.sql');
    expect(names).toContain('0027_sentiment_v2_results.sql');
    expect(names).toEqual([...names].sort());
    expect(new Set(names).size).toBe(names.length);
  });
  it('runs 0015, 0023, 0026, 0027 in that order, and 0029 last only when the ref has the evidence collector', () => {
    const a = harness({ collector: false }); a.run();
    expect(executed(a.calls)).toEqual(['0015_experiment5_sentiment_archive.sql', '0023_event_detection.sql', '0026_d1_usage_daily.sql', '0027_sentiment_v2_results.sql']);
    const b = harness({ collector: true }); b.run();
    expect(executed(b.calls)).toEqual(['0015_experiment5_sentiment_archive.sql', '0023_event_detection.sql', '0026_d1_usage_daily.sql', '0027_sentiment_v2_results.sql', '0029_sv2_evidence_and_ai_budget.sql']);
  });
  it('fails (never skips) when a planned migration file is missing from the ref', () => {
    const h = harness({ overrides: { [`${MIG}/0023_event_detection.sql`]: null } });
    expect(() => h.run()).toThrow(/0023_event_detection.sql is part of the staging plan but is missing/);
    expect(h.calls).toHaveLength(0);
  });
  it('fails when the ref has the collector but not migration 0029', () => {
    const h = harness({ collector: true, overrides: { [`${MIG}/0029_sv2_evidence_and_ai_budget.sql`]: null } });
    expect(() => h.run()).toThrow(/0029.*required.*missing/);
    expect(h.calls).toHaveLength(0);
  });
  it('resolves the fixed ascending plan when every file exists', () => {
    const names = resolvePlan(MIG, () => true).map((m) => m.file);
    expect(names).toEqual(PLAN.map((m) => m.file));
  });
});

describe('target: staging only, production rejected', () => {
  it('accepts the real staging config', () => { expect(() => assertStagingConfig(STAGING_TOML)).not.toThrow(); });
  const bad = (name, text, re) => it(`rejects ${name}`, () => expect(() => assertStagingConfig(text)).toThrow(re));
  bad('the production Worker name', STAGING_TOML.replace(`name = "${STAGING.worker}"`, `name = "${PRODUCTION.worker}"`), /staging Worker name/);
  bad('the production D1 id', STAGING_TOML.replace(STAGING.database_id, PRODUCTION.database_id), /staging D1 database id/);
  bad('the production D1 name', STAGING_TOML.replace(`database_name = "${STAGING.database_name}"`, `database_name = "${PRODUCTION.database_name}"`), /staging D1 database name/);
  bad('a second D1 binding to production', `${STAGING_TOML}\n[[d1_databases]]\nbinding = "DB2"\ndatabase_name = "x"\ndatabase_id = "${PRODUCTION.database_id}"\n`, /exactly one D1/);
  it('ignores production identifiers that only appear in comments', () => {
    expect(() => assertStagingConfig(`# production is "${PRODUCTION.worker}" ${PRODUCTION.database_id}\n${STAGING_TOML}`)).not.toThrow();
  });
  it('refuses to run any SQL when the config is not staging', () => {
    const h = harness({ overrides: { 'wrangler.staging.toml': STAGING_TOML.replace(STAGING.database_id, PRODUCTION.database_id) } });
    expect(() => h.run()).toThrow();
    expect(h.calls).toHaveLength(0);
  });
  it('aborts before any SQL when wrangler resolves the production database', () => {
    const h = harness({ infoJson: { name: 'x', uuid: PRODUCTION.database_id } });
    expect(() => h.run()).toThrow(/production database id/);
    expect(executed(h.calls)).toEqual([]);
  });
  it('aborts before any SQL when wrangler does not report the staging id', () => {
    const h = harness({ infoJson: { name: 'x', uuid: 'deadbeef' } });
    expect(() => h.run()).toThrow(/did not report the staging database id/);
    expect(executed(h.calls)).toEqual([]);
  });
  it('every command targets the staging database with the staging config, never the production id', () => {
    const h = harness({ collector: true }); h.run();
    expect(h.calls.length).toBeGreaterThanOrEqual(8);   // d1 info + two schema snapshots + five migration files
    for (const a of h.calls) {
      expect(a.slice(0, 2)).toSatisfy((s) => s[0] === 'd1');
      expect(a[2]).toBe(STAGING.database_name);
      expect(a[a.indexOf('-c') + 1]).toBe('wrangler.staging.toml');
      expect(JSON.stringify(a)).not.toContain(PRODUCTION.database_id);
      expect(JSON.stringify(a)).not.toContain(PRODUCTION.database_name);
    }
    expect(h.calls.every((a) => a[1] === 'info' || a.includes('--remote'))).toBe(true);
  });
});

describe('SQL must be additive', () => {
  it('accepts every real planned migration in the repository', () => {
    for (const m of PLAN) {
      const p = join(root, MIG, m.file);
      if (existsSync(p)) expect(() => assertAdditive(m.file, readFileSync(p, 'utf8'))).not.toThrow();
    }
  });
  for (const sql of ['DROP TABLE history', 'DELETE FROM history', 'UPDATE history SET x = 1', 'INSERT INTO history VALUES (1)', 'ALTER TABLE history DROP COLUMN x',
    'ALTER TABLE history RENAME TO h2', 'PRAGMA writable_schema = 1', 'ATTACH DATABASE "x" AS y', 'CREATE TABLE a (x); DROP TABLE a', 'CREATE TRIGGER t AFTER INSERT ON a BEGIN DELETE FROM a; END']) {
    it(`rejects: ${sql}`, () => expect(() => assertAdditive('x.sql', sql)).toThrow(/not an additive statement/));
  }
  describe('quote-aware SQL handling (Codex): nothing can hide inside a string, identifier or comment', () => {
    const rejects = (sql, re = /not an additive statement|unterminated/) => expect(() => assertAdditive('x.sql', sql)).toThrow(re);
    it('a `--` inside a string literal does not swallow the DROP that follows', () => {
      rejects(`CREATE TABLE safe (value TEXT DEFAULT '--'); DROP TABLE history;`);
      rejects(`CREATE TABLE safe (value TEXT DEFAULT '--');\nDROP TABLE history;`);
    });
    it('a `/*` inside a string literal does not open a comment over a later DROP', () => {
      rejects(`CREATE TABLE safe (value TEXT DEFAULT '/*'); DROP TABLE history; -- */`);
    });
    it('a `--` or `;` inside a quoted identifier does not hide or split anything', () => {
      rejects(`CREATE TABLE "a--b;c" (x TEXT); DROP TABLE history;`);
      rejects('CREATE TABLE `a--b` (x TEXT); DELETE FROM history;');
      rejects('CREATE TABLE [a--b] (x TEXT); UPDATE history SET x = 1;');
    });
    it('a `;` inside a string literal does not split the statement (and a harmless one is accepted)', () => {
      expect(statements(`CREATE TABLE a (v TEXT DEFAULT ';x');`)).toEqual([`CREATE TABLE a (v TEXT DEFAULT ';x')`]);
      expect(() => assertAdditive('x.sql', `CREATE TABLE a (v TEXT DEFAULT ';--/*');`)).not.toThrow();
    });
    it('doubled quotes are escapes: a statement cannot end early inside \'it\'\'s;\'', () => {
      expect(statements(`CREATE TABLE a (v TEXT DEFAULT 'it''s;'); DROP TABLE h`)).toHaveLength(2);
      rejects(`CREATE TABLE a (v TEXT DEFAULT 'it''s;'); DROP TABLE h`);
    });
    it('an unterminated string, identifier or block comment is an error, never partly read', () => {
      for (const sql of [`CREATE TABLE a (v TEXT DEFAULT 'oops); DROP TABLE h;`, 'CREATE TABLE "a (x); DROP TABLE h;', 'CREATE TABLE a (x); /* DROP TABLE h;']) {
        expect(() => tokenize(sql), sql).toThrow(/unterminated/);
        rejects(sql);
      }
    });
    it('a trailing line comment and a block comment between statements are fine', () => {
      expect(() => assertAdditive('x.sql', 'CREATE TABLE a (x INTEGER); -- done\n/* more */ CREATE INDEX i ON a (x);')).not.toThrow();
    });
    it('the executed text and the validated text are the same statements: every real migration tokenizes with no leftovers', () => {
      for (const m of PLAN) {
        const p = join(root, MIG, m.file);
        if (!existsSync(p)) continue;
        const sql = readFileSync(p, 'utf8');
        expect(tokenize(sql).map((t) => t.v).join(''), m.file).toBe(sql);             // lossless
        expect(statements(sql).length, m.file).toBeGreaterThan(0);
      }
    });
  });

  describe('canonical form compares what defines the object', () => {
    it('is case-sensitive for string literals and quoted identifiers', () => {
      expect(normalizeSql(`CREATE TABLE a (v TEXT DEFAULT 'A')`)).not.toBe(normalizeSql(`CREATE TABLE a (v TEXT DEFAULT 'a')`));
      expect(normalizeSql('CREATE TABLE a ("V" TEXT)')).not.toBe(normalizeSql('CREATE TABLE a ("v" TEXT)'));
      expect(normalizeSql(`CREATE TABLE a (v TEXT CHECK (v IN ('OK','FAILED')))`)).not.toBe(normalizeSql(`CREATE TABLE a (v TEXT CHECK (v IN ('ok','failed')))`));
    });
    it('ignores keyword case, whitespace, comments and IF NOT EXISTS', () => {
      const a = normalizeSql(`CREATE TABLE IF NOT EXISTS Foo (\n  id INTEGER PRIMARY KEY, -- note\n  Name TEXT NOT NULL DEFAULT 'x y'\n)`);
      const b = normalizeSql(`create table foo(id integer primary key,name text not null default 'x y')`);
      expect(a).toBe(b);
    });
    it('still distinguishes different columns, types, constraints and index columns', () => {
      const base = normalizeSql('CREATE TABLE a (id INTEGER PRIMARY KEY, n TEXT NOT NULL)');
      for (const other of ['CREATE TABLE a (id INTEGER PRIMARY KEY, m TEXT NOT NULL)', 'CREATE TABLE a (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)', 'CREATE TABLE a (id INTEGER PRIMARY KEY, n TEXT)', 'CREATE TABLE a (id INTEGER, n TEXT NOT NULL)']) expect(normalizeSql(other)).not.toBe(base);
      expect(normalizeSql('CREATE INDEX i ON a (x)')).not.toBe(normalizeSql('CREATE UNIQUE INDEX i ON a (x)'));
      expect(normalizeSql('CREATE INDEX i ON a (x)')).not.toBe(normalizeSql('CREATE INDEX i ON a (y)'));
    });
    it('a table whose only difference is a case-sensitive default literal is reported as drift', () => {
      const h = harness({ overrides: { [`${MIG}/0026_d1_usage_daily.sql`]: "CREATE TABLE IF NOT EXISTS d1_usage_daily (day TEXT NOT NULL DEFAULT 'A');" } });
      h.tables.set('table:d1_usage_daily', "CREATE TABLE d1_usage_daily (day TEXT NOT NULL DEFAULT 'a')");
      expect(() => h.run()).toThrow(/table d1_usage_daily.*schema drift/);
      expect(executed(h.calls)).toEqual([]);
    });
  });

  it('ignores comments (a commented DROP is not a statement)', () => {
    expect(statements('-- DROP TABLE x;\nCREATE TABLE a (x); /* DELETE FROM a */')).toEqual(['CREATE TABLE a (x)']);
  });
  it('rejects a non-additive migration before touching the database at all', () => {
    const h = harness({ overrides: { [`${MIG}/0026_d1_usage_daily.sql`]: 'DROP TABLE history;' } });
    expect(() => h.run()).toThrow(/not an additive statement/);
    expect(h.calls).toHaveLength(0);
  });
});

describe('fail closed and idempotent', () => {
  it('does not run later migrations after a failure', () => {
    const h = harness({ failOn: '0023_event_detection.sql' });
    try { h.run(); } catch { /* expected */ }
    const ran = h.calls.filter((a) => a.includes('--file')).map((a) => a[a.indexOf('--file') + 1].split('/').pop());
    expect(ran).toEqual(['0015_experiment5_sentiment_archive.sql', '0023_event_detection.sql']);
    expect(ran).not.toContain('0026_d1_usage_daily.sql');
    expect(ran).not.toContain('0027_sentiment_v2_results.sql');
  });
  it('fails when a table the SQL creates does not exist afterwards', () => {
    const h = harness({ createsNothing: 'event_ledger' });
    expect(() => h.run()).toThrow(/expected table event_ledger does not exist/);
  });
  it('fails when an index the SQL creates does not exist afterwards', () => {
    const h = harness({ createsNothing: 'idx_event_ledger_detected' });
    expect(() => h.run()).toThrow(/expected index idx_event_ledger_detected does not exist/);
  });
  const ALL_0015 = ['table:research_sentiment_archive', 'index:idx_research_sentiment_archive_observation_ts', 'index:idx_research_sentiment_archive_archived_ts'];
  it('derives the objects 0015 creates from its SQL: the table and both indexes', () => {
    const objs = assertAdditive('0015', real(`${MIG}/0015_experiment5_sentiment_archive.sql`)).map((o) => `${o.type}:${o.name}`);
    expect(objs.sort()).toEqual([...ALL_0015].sort());
  });
  it('runs the non-idempotent 0015 only when NONE of its objects exist, decided by queries rather than an error message', () => {
    const h = harness(); h.run();
    expect(executed(h.calls)).toContain('0015_experiment5_sentiment_archive.sql');
    const again = harness(); again.apply('0015_experiment5_sentiment_archive.sql'); const log = again.run();
    expect(executed(again.calls)).not.toContain('0015_experiment5_sentiment_archive.sql');
    expect(log.some((l) => l.startsWith('kept 0015'))).toBe(true);
  });
  for (const [name, drop] of [['the table without its indexes', ALL_0015.slice(1)], ['the table with one index', ALL_0015.slice(2)], ['an index without the table', [ALL_0015[0], ALL_0015[2]]]]) {
    it(`treats a partially applied 0015 (${name}) as a failure: nothing is executed and nothing deploys`, () => {
      const h = harness(); h.apply('0015_experiment5_sentiment_archive.sql'); drop.forEach((k) => h.tables.delete(k));
      expect(() => h.run()).toThrow(/0015_experiment5_sentiment_archive\.sql is partially applied on staging \(missing: .+\)/);
      expect(executed(h.calls)).toEqual([]);
    });
  }
  describe('schema drift: an existing object with a different definition is never accepted', () => {
    const drifted = (key, from, to, applyFirst) => {
      const h = harness(); applyFirst.forEach((f) => h.apply(f));
      h.tables.set(key, h.tables.get(key).replace(from, to));
      return h;
    };
    it('0015 table missing a column is rejected before anything is executed', () => {
      const h = drifted('table:research_sentiment_archive', 'observation_ts', 'obs_ts', ['0015_experiment5_sentiment_archive.sql']);
      expect(() => h.run()).toThrow(/differs from the reviewed migration \(schema drift\)/);
      expect(executed(h.calls)).toEqual([]);
    });
    it('an index on different columns is rejected', () => {
      const real0015 = real(`${MIG}/0015_experiment5_sentiment_archive.sql`);
      expect(real0015).toMatch(/idx_research_sentiment_archive_archived_ts/);
      const h = harness(); h.apply('0015_experiment5_sentiment_archive.sql');
      h.tables.set('index:idx_research_sentiment_archive_archived_ts', 'CREATE INDEX idx_research_sentiment_archive_archived_ts ON research_sentiment_archive(score)');
      expect(() => h.run()).toThrow(/idx_research_sentiment_archive_archived_ts.*schema drift/);
      expect(executed(h.calls)).toEqual([]);
    });
    it('an IF NOT EXISTS table that already exists with different columns is caught (the SQL would silently leave it alone)', () => {
      const h = harness(); h.apply('0023_event_detection.sql');
      const k = 'table:event_ledger';
      h.tables.set(k, h.tables.get(k).replace(/event_id/i, 'evt_id'));
      expect(() => h.run()).toThrow(/table event_ledger.*schema drift/);
      expect(executed(h.calls)).toEqual([]);
    });
    it('is still accepted when only whitespace, comments, case or IF NOT EXISTS differ', () => {
      const h = harness(); h.apply('0023_event_detection.sql');
      for (const [k, v] of [...h.tables]) h.tables.set(k, `-- stored with a comment\n${v.replace(/\s+/g, '  ').replace(/CREATE TABLE/i, 'create table')}`);
      expect(() => h.run()).not.toThrow();
    });
    it('a second run on a correctly migrated database passes and applies nothing non-idempotent', () => {
      const h = harness({ collector: true }); h.run(); h.calls.length = 0;
      expect(() => h.run()).not.toThrow();
      expect(executed(h.calls)).not.toContain('0015_experiment5_sentiment_archive.sql');
    });
  });
  it('is safe to run twice: the second run re-applies only IF NOT EXISTS migrations', () => {
    const h = harness({ collector: true });
    h.run();
    h.calls.length = 0;
    const log = h.run();
    expect(executed(h.calls)).not.toContain('0015_experiment5_sentiment_archive.sql');
    expect(log.at(-1)).toMatch(/verified \d+ tables and indexes/);
    for (const m of PLAN.filter((x) => !x.guardTable)) {
      const text = h.files.get(`${MIG}/${m.file}`);
      expect(text, m.file).toMatch(/IF NOT EXISTS/i);
      expect(text.match(/CREATE (UNIQUE )?(TABLE|INDEX)(?! IF NOT EXISTS)/gi) || [], m.file).toEqual([]);
    }
  });
});

describe('workflow wiring', () => {
  const wf = readFileSync(join(root, '.github/workflows/deploy-sv2-staging.yml'), 'utf8');
  const code = wf.split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');
  it('runs the runner copied from origin/main, not the one in the deployed ref', () => {
    expect(code).toContain('git show "FETCH_HEAD:scripts/staging-migrations.mjs"');
    expect(code).toMatch(/node "\$RUNNER_TEMP\/staging-migrations\.mjs" apply/);
  });
  it('no longer skips missing files or tolerates errors by message matching', () => {
    expect(code).not.toMatch(/not in this ref/);
    expect(code).not.toMatch(/already exists/);
    expect(code).not.toMatch(/continue-on-error/);
    expect(code).not.toMatch(/d1 execute/);   // all D1 access goes through the validated runner
  });
  it('runs the migrations before the deploy, after the config validation', () => {
    const i = (s) => code.indexOf(s);
    expect(i('Validate wrangler.staging.toml targets only')).toBeLessThan(i('migrations to the STAGING database'));
    expect(i('migrations to the STAGING database')).toBeLessThan(i('Deploy to the staging Worker'));
  });
  it('gives the migration step only the staging Cloudflare token', () => {
    const step = code.slice(code.indexOf('migrations to the STAGING database'), code.indexOf('Deploy to the staging Worker'));
    expect([...step.matchAll(/secrets\.([A-Z0-9_]+)/g)].map((m) => m[1])).toEqual(['STAGE7_STAGING_CLOUDFLARE_API_TOKEN']);
  });
});
