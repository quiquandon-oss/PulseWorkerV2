import { describe, it, expect, beforeAll, vi } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { extractConstants, extractFunctions, evalInScope } from './helpers/extract.js';

// The Worker's idea of "what Stage 7 needs" (STAGE7_REQUIRED_SCHEMA) is checked against the REAL migration files,
// so the two cannot drift apart silently, and the classifier's behaviour for missing tables/columns is pinned.
const MIGRATIONS_DIR = new URL('../.ai/migrations/', import.meta.url);

// Columns each migration file declares, per table: CREATE TABLE bodies and ALTER TABLE ... ADD COLUMN.
function declaredColumns(fileName) {
  const sql = readFileSync(new URL(fileName, MIGRATIONS_DIR), 'utf8')
    .split('\n').map((line) => line.replace(/--.*$/, '')).join('\n');
  const out = {};
  for (const m of sql.matchAll(/CREATE TABLE\s+(\w+)\s*\(([\s\S]*?)\n\);/g)) {
    out[m[1]] = out[m[1]] || new Set();
    for (const line of m[2].split('\n')) {
      const col = line.match(/^\s{2}(\w+)\s+(TEXT|INTEGER|REAL)\b/i);
      if (col) out[m[1]].add(col[1]);
    }
  }
  for (const m of sql.matchAll(/ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(\w+)/g)) {
    out[m[1]] = out[m[1]] || new Set();
    out[m[1]].add(m[2]);
  }
  return out;
}
const migrationFile = (num) => readdirSync(MIGRATIONS_DIR).find((f) => f.startsWith(`${num}_`));

describe('STAGE7_REQUIRED_SCHEMA matches the real migrations', () => {
  let scope;
  beforeAll(() => {
    scope = evalInScope(
      extractConstants('STAGE7_REQUIRED_SCHEMA', 'STAGE7_SCHEMA_PROBE_SQL') + '\n' +
      extractFunctions('classifyStage7Schema', 'getStage7SchemaStatus')
    );
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('every declared column is introduced by the migration it names', () => {
    for (const group of scope.STAGE7_REQUIRED_SCHEMA) {
      const declared = declaredColumns(migrationFile(group.migration))[group.table];
      expect(declared, `${group.table} in ${group.migration}`).toBeTruthy();
      for (const col of group.columns) {
        expect(declared.has(col), `${group.table}.${col} should be declared by migration ${group.migration}`).toBe(true);
      }
    }
  });

  it('every column that migrations 0016-0018 add to a Stage 7 table is listed (nothing the code could rely on is unchecked)', () => {
    const listed = {};
    for (const g of scope.STAGE7_REQUIRED_SCHEMA) {
      for (const c of g.columns) (listed[`${g.migration}:${g.table}`] = listed[`${g.migration}:${g.table}`] || new Set()).add(c);
    }
    for (const num of ['0016', '0017', '0018']) {
      const cols = declaredColumns(migrationFile(num));
      for (const [table, set] of Object.entries(cols)) {
        if (!table.startsWith('stage7_')) continue;
        for (const col of set) {
          expect(listed[`${num}:${table}`]?.has(col), `${num} adds ${table}.${col}; it must be in STAGE7_REQUIRED_SCHEMA`).toBe(true);
        }
      }
    }
  });

  it('0017 and 0018 only depend on tables created by an earlier migration (dependency order)', () => {
    const created = (num) => new Set(Object.keys(declaredColumns(migrationFile(num))));
    const alterTargets = (num) => {
      const sql = readFileSync(new URL(migrationFile(num), MIGRATIONS_DIR), 'utf8');
      return new Set([...sql.matchAll(/ALTER TABLE\s+(\w+)\s+ADD COLUMN/g)].map((m) => m[1]));
    };
    for (const t of alterTargets('0017')) expect(created('0016').has(t), `0017 alters ${t}, created by 0016`).toBe(true);
    for (const t of alterTargets('0018')) {
      expect(created('0016').has(t) || created('0017').has(t), `0018 alters ${t}`).toBe(true);
    }
  });

  it('the schema probe is one read-only statement over exactly the tables Stage 7 depends on', () => {
    const tables = [...new Set(scope.STAGE7_REQUIRED_SCHEMA.map((g) => g.table))].sort();
    const probed = [...scope.STAGE7_SCHEMA_PROBE_SQL.matchAll(/'(\w+)'/g)].map((m) => m[1]).filter((t) => t !== 'table').sort();
    expect(probed).toEqual(tables);
    expect(scope.STAGE7_SCHEMA_PROBE_SQL.trim()).toMatch(/^SELECT /);
  });

  const rowsFor = (upTo, drop = () => false) => scope.STAGE7_REQUIRED_SCHEMA
    .filter((g) => g.migration <= upTo)
    .flatMap((g) => g.columns.map((col) => ({ tbl: g.table, col })))
    .filter((r) => !drop(r));

  it('complete schema: ready', () => {
    expect(scope.classifyStage7Schema(rowsFor('0018'))).toMatchObject({ ready: true, state: 'READY', missing_tables: [], missing_columns: [] });
  });

  it('nothing applied: NOT_APPLIED, every migration listed in order', () => {
    const r = scope.classifyStage7Schema([]);
    expect(r).toMatchObject({ ready: false, state: 'NOT_APPLIED' });
    expect(r.migrations_required).toEqual(['0005', '0016', '0017', '0018']);
    expect(r.message).toMatch(/not applied/);
    expect(r.message).not.toMatch(/production/i);
  });

  it('only 0016: PARTIAL, 0017 and 0018 named, candidates table reported missing', () => {
    const r = scope.classifyStage7Schema(rowsFor('0016'));
    expect(r.state).toBe('PARTIAL');
    expect(r.missing_tables).toEqual(['stage7_research_candidates']);
    expect(r.migrations_required).toEqual(['0017', '0018']);
  });

  it('0016+0017: PARTIAL, only 0018 and only its columns', () => {
    const r = scope.classifyStage7Schema(rowsFor('0017'));
    expect(r.migrations_required).toEqual(['0018']);
    expect(r.missing_columns).toEqual(expect.arrayContaining([
      { table: 'stage7_research_requests', column: 'recalculation_status', migration: '0018' },
      { table: 'stage7_research_responses', column: 'human_confirmed_ts', migration: '0018' },
    ]));
    expect(r.missing_columns.every((c) => c.migration === '0018')).toBe(true);
  });

  it('one missing column anywhere is enough to be not ready (a half-applied ALTER)', () => {
    const r = scope.classifyStage7Schema(rowsFor('0018', (row) => row.col === 'prompt_text'));
    expect(r.ready).toBe(false);
    expect(r.state).toBe('PARTIAL');
    expect(r.missing_columns).toEqual([{ table: 'stage7_research_requests', column: 'prompt_text', migration: '0017' }]);
  });

  it('a missing research_events dependency is reported against migration 0005', () => {
    const r = scope.classifyStage7Schema(rowsFor('0018', (row) => row.tbl === 'research_events'));
    expect(r.missing_tables).toEqual(['research_events']);
    expect(r.migrations_required).toEqual(['0005']);
  });

  it('a failed probe is CHECK_FAILED and says it is not evidence of a missing migration', async () => {
    const db = { prepare() { return { all: async () => { throw new Error('D1_ERROR: timeout'); } }; } };
    const r = await scope.getStage7SchemaStatus({ DB: db });
    expect(r).toMatchObject({ ready: false, state: 'CHECK_FAILED', migrations_required: [] });
    expect(r.message).toMatch(/NOT evidence that a migration is missing/);
    expect(JSON.stringify(r)).not.toMatch(/timeout/);
  });
});
