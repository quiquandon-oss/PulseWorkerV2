// Evaluation dimensions (migration 0022 + registry fields): standalone outcome and pool contribution stay apart.
// Every row below is TEST DATA for software behaviour only -- never scientific evidence.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const require = createRequire(import.meta.url);
const { DatabaseSync } = require('node:sqlite');
const MIGRATION = readFileSync(new URL('../.ai/migrations/0022_experiment_evaluation_dimensions.sql', import.meta.url), 'utf8');
const TEST_SHA = 'a'.repeat(64);

function scope() {
  return evalInScope(
    extractFunctions('loadEvaluationRows', 'attachEvaluationDimensions', 'parseJsonColumn') + '\n' +
    extractConstants('EVAL_RECORDED_TS', 'EVAL_RECORDED_BY', 'CODE_EVALUATION_RECORDS'), {});
}

// A local SQLite database standing in for D1 (same SQL dialect), wrapped in D1's prepare().all() shape.
function localDb({ migrate = true } = {}) {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE research_experiment_registry (experiment_id TEXT PRIMARY KEY);');
  db.exec("INSERT INTO research_experiment_registry VALUES ('EXP-004'), ('EXP-005'), ('EXP-009');");
  if (migrate) db.exec(MIGRATION);
  return { db, env: { DB: { prepare(sql) { return { async all() { return { results: db.prepare(sql).all() }; } }; } } } };
}

const insertPool = (db, o = {}) => db.prepare(`INSERT INTO research_pool_evaluations
  (experiment_id, component_id, component_provenance, pool_id, pool_version, pool_state, reason, protocol_id, protocol_sha256,
   evaluator_version, window_start_ts, window_end_ts, out_of_sample, baseline_n, candidate_n, overlap_n,
   baseline_metrics_json, candidate_metrics_json, uncertainty_json, evidence_refs_json, evidence_sha256, recorded_ts, recorded_by, supersedes_id)
  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING`).run(...[
  'EXP-004', 'TEST component', 'TEST provenance', 'TEST-pool', 'v1', 'VALIDATED_INCREMENTAL_VALUE', 'TEST DATA', 'TEST-protocol', TEST_SHA,
  'TEST-eval-1', 1, 2, 1, 50, 50, 50, '{"acc":0.5}', '{"acc":0.6}', '{"ci":{"lo":0.05,"hi":0.15}}', '["test://evidence"]', TEST_SHA, 10, 'TEST', null,
].map((v, i) => (Object.prototype.hasOwnProperty.call(o, i) ? o[i] : v)));

describe('migration 0022 (local SQLite only -- never applied to production here)', () => {
  it('creates two new tables and touches nothing else', () => {
    const { db } = localDb();
    const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all().map(r => r.name);
    expect(tables).toEqual(['research_experiment_registry', 'research_pool_evaluations', 'research_standalone_evaluations', 'sqlite_sequence']);
    const statements = MIGRATION.replace(/--.*$/gm, '');
    expect(statements).not.toMatch(/\b(ALTER|DROP|UPDATE|DELETE|INSERT|REPLACE INTO)\b/i);
    expect(statements.match(/CREATE TABLE/g)).toHaveLength(2);
  });

  it('rejects unknown states and conclusive pool rows without protocol, evidence or out-of-sample window', () => {
    const { db } = localDb();
    expect(() => insertPool(db, { 5: 'USEFUL' })).toThrow(/CHECK/);
    expect(() => insertPool(db, { 8: null })).toThrow(/CHECK/);     // no protocol sha
    expect(() => insertPool(db, { 20: null })).toThrow(/CHECK/);    // no evidence sha
    expect(() => insertPool(db, { 12: 0 })).toThrow(/CHECK/);       // not out-of-sample
    expect(() => insertPool(db, { 19: '[]' })).toThrow(/CHECK/);    // no evidence refs
    expect(() => insertPool(db, { 10: 5, 11: 4 })).toThrow(/CHECK/); // inverted window
    // A candidate needs no protocol: it is a designation, not a result.
    insertPool(db, { 5: 'CANDIDATE', 3: null, 4: null, 7: null, 8: null, 9: null, 12: null, 20: null, 19: '[]' });
    expect(db.prepare('SELECT COUNT(*) n FROM research_pool_evaluations').get().n).toBe(1);
  });

  it('standalone: EVALUATED needs an outcome and evidence; an outcome needs EVALUATED (NOT_APPLICABLE aside)', () => {
    const { db } = localDb();
    const ins = (status, outcome, refs = '["test://e"]') => db.prepare(`INSERT INTO research_standalone_evaluations
      (experiment_id, evaluation_version, spec_ref, evaluation_status, scientific_outcome, reason, evidence_refs_json, recorded_ts, recorded_by)
      VALUES ('EXP-005','TEST-v1','TEST spec',?,?,'TEST DATA',?,1,'TEST')`).run(status, outcome, refs);
    expect(() => ins('EVALUATED', null)).toThrow(/CHECK/);
    expect(() => ins('EVALUATED', 'SUPPORTED', '[]')).toThrow(/CHECK/);
    expect(() => ins('BLOCKED', 'NOT_SUPPORTED')).toThrow(/CHECK/);
    expect(() => ins('DONE', null)).toThrow(/CHECK/);
    ins('BLOCKED', 'NOT_APPLICABLE');
    ins('EVALUATED', 'NOT_SUPPORTED');
  });

  it('re-ingesting the same evidence is idempotent; a correction (supersedes_id) is kept', () => {
    const { db } = localDb();
    insertPool(db); insertPool(db);
    expect(db.prepare('SELECT COUNT(*) n FROM research_pool_evaluations').get().n).toBe(1);
    insertPool(db, { 23: 1 });
    expect(db.prepare('SELECT COUNT(*) n FROM research_pool_evaluations').get().n).toBe(2);
  });
});

describe('registry evaluation dimensions', () => {
  const entries = () => ['EXP-003', 'EXP-004', 'EXP-005', 'EXP-009'].map(experiment_id => ({ experiment_id, conclusion: null }));

  it('without migration 0022 (today): tables NOT_PRESENT, code records attached, nothing invented elsewhere', async () => {
    const { loadEvaluationRows, attachEvaluationDimensions } = scope();
    const rows = await loadEvaluationRows(localDb({ migrate: false }).env);
    expect(rows).toMatchObject({ tables: 'NOT_PRESENT', error: null });
    const out = attachEvaluationDimensions(entries(), rows);
    const by = Object.fromEntries(out.map(e => [e.experiment_id, e]));
    for (const id of ['EXP-004', 'EXP-005', 'EXP-009']) {
      expect(by[id].standalone_evaluations).toHaveLength(1);
      expect(by[id].standalone_evaluations[0]).toMatchObject({ evaluation_status: 'BLOCKED', record_source: 'code' });
    }
    expect(by['EXP-004'].standalone_evaluations[0].scientific_outcome).toBeNull();
    expect(by['EXP-009'].standalone_evaluations[0].scientific_outcome).toBe('NOT_APPLICABLE');
    expect(by['EXP-004'].pool_evaluations.map(r => r.pool_state)).toEqual(['CANDIDATE']);
    expect(by['EXP-005'].pool_evaluations.map(r => r.pool_state)).toEqual(['CANDIDATE']);
    expect(by['EXP-009'].pool_evaluations.map(r => r.pool_state)).toEqual(['NOT_TESTED']);
    // An experiment with no record: no standalone status is invented; pool has no evaluation (empty list).
    expect(by['EXP-003'].standalone_evaluations).toBeUndefined();
    expect(by['EXP-003'].pool_evaluations).toEqual([]);
    expect(by['EXP-003'].conclusion).toBeNull();
  });

  it('code records are never conclusive and carry provenance', () => {
    const { CODE_EVALUATION_RECORDS } = scope();
    for (const [id, r] of Object.entries(CODE_EVALUATION_RECORDS)) {
      for (const s of r.standalone) {
        expect(s.evaluation_status, id).not.toBe('EVALUATED');
        expect(s.recorded_by && s.recorded_ts && s.reason && s.spec_ref, id).toBeTruthy();
        expect(s.spec_sha256, id).toMatch(/^[0-9a-f]{64}$/);
      }
      for (const p of r.pool) {
        expect(['CANDIDATE', 'NOT_TESTED', 'BLOCKED'], id).toContain(p.pool_state);
        expect(p.recorded_by && p.recorded_ts && p.reason, id).toBeTruthy();
      }
    }
  });

  it('frozen spec hashes in the code records match the migration files', async () => {
    const { createHash } = await import('node:crypto');
    const { CODE_EVALUATION_RECORDS } = scope();
    for (const r of Object.values(CODE_EVALUATION_RECORDS)) {
      const s = r.standalone[0];
      const file = s.spec_ref.split(' ')[0];
      expect(createHash('sha256').update(readFileSync(new URL(`../${file}`, import.meta.url))).digest('hex'), file).toBe(s.spec_sha256);
    }
  });

  it('with migration 0022: D1 rows join the history after the code record; a pool row never touches standalone', async () => {
    const { loadEvaluationRows, attachEvaluationDimensions } = scope();
    const { db, env } = localDb();
    insertPool(db, { 5: 'INCONCLUSIVE', 6: 'TEST DATA: wide interval', 10: 1, 11: 2 });
    const rows = await loadEvaluationRows(env);
    expect(rows.tables).toBe('PRESENT');
    const e = attachEvaluationDimensions(entries(), rows).find(x => x.experiment_id === 'EXP-004');
    expect(e.pool_evaluations.map(r => [r.pool_state, r.record_source])).toEqual([['CANDIDATE', 'code'], ['INCONCLUSIVE', 'd1']]);
    expect(e.pool_evaluations[1]).toMatchObject({ pool: { pool_id: 'TEST-pool', version: 'v1' }, out_of_sample: true,
      eligible: { baseline_n: 50, candidate_n: 50, overlap_n: 50 }, baseline_metrics: { acc: 0.5 }, evidence_refs: ['test://evidence'] });
    expect(e.pool_component).toMatchObject({ component_id: 'TEST component' });
    expect(e.standalone_evaluations).toHaveLength(1);
    expect(e.standalone_evaluations[0].evaluation_status).toBe('BLOCKED');
  });

  it('any other D1 error is reported, not turned into an outcome', async () => {
    const { loadEvaluationRows } = scope();
    const rows = await loadEvaluationRows({ DB: { prepare() { return { async all() { throw new Error('D1_ERROR: timeout'); } }; } } });
    expect(rows).toMatchObject({ tables: 'UNAVAILABLE', standalone: {}, pool: {} });
    expect(rows.error).toMatch(/timeout/);
  });
});
