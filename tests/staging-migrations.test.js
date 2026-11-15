// scripts/staging-migrations.mjs: ordering, inclusion (0023 and its siblings), additive-only SQL, staging-only target,
// production rejection, fail-closed behaviour and idempotency. wrangler is replaced by an in-memory fake D1: nothing
// here touches Cloudflare.
import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { STAGING, PRODUCTION, PLAN, assertStagingConfig, assertAdditive, statements, resolvePlan, applyMigrations } from '../scripts/staging-migrations.mjs';

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
  const tables = new Set();
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
        for (const t of [...(files.get(file) || '').matchAll(/CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)/gi)]) if (createsNothing !== t[1]) tables.add(t[1]);
        return '[]';
      }
      const m = cmd.match(/name='(\w+)'/);
      return JSON.stringify([{ results: m && tables.has(m[1]) ? [{ name: m[1] }] : [] }]);
    }
    throw new Error(`unexpected wrangler command: ${sub}`);
  };
  const run = () => applyMigrations({ readText: (p) => { if (!files.has(p)) throw new Error(`ENOENT ${p}`); return files.get(p); }, exists: (p) => files.has(p), wrangler });
  return { run, calls, tables, files };
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
    expect(h.calls.length).toBeGreaterThan(8);
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
  it('runs the non-idempotent 0015 only when its table is absent, decided by a query rather than an error message', () => {
    const h = harness(); h.run();
    expect(executed(h.calls)).toContain('0015_experiment5_sentiment_archive.sql');
    const again = harness(); again.tables.add('research_sentiment_archive'); const log = again.run();
    expect(executed(again.calls)).not.toContain('0015_experiment5_sentiment_archive.sql');
    expect(log.some((l) => l.startsWith('kept 0015'))).toBe(true);
  });
  it('is safe to run twice: the second run re-applies only IF NOT EXISTS migrations', () => {
    const h = harness({ collector: true });
    h.run();
    h.calls.length = 0;
    const log = h.run();
    expect(executed(h.calls)).not.toContain('0015_experiment5_sentiment_archive.sql');
    expect(log.at(-1)).toMatch(/verified \d+ tables/);
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
