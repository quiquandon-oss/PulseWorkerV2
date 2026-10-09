// Read-only extractor for the T-A1 / T-A2 progress run (research/v1_prospective_extract.mjs). No network: fake fetch.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { extract, assertReadOnly, checkResponse, STATEMENTS, BOUNDARY_MS } from '../research/v1_prospective_extract.mjs';
import { evaluate, assertProgressOnly } from '../research/v1_prospective_eval.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TOKEN = 'test-token-value-not-real';
const ok = (results, meta = {}) => ({ success: true, errors: [], result: [{ success: true, results, meta: { changes: 0, rows_written: 0, changed_db: false, rows_read: results.length, ...meta } }] });

function fakeFetch(bodies, calls = []) {
  return async (url, init) => {
    calls.push({ url, init });
    const sql = JSON.parse(init.body).sql;
    const key = Object.keys(STATEMENTS).find((k) => STATEMENTS[k] === sql);
    return { ok: true, status: 200, json: async () => bodies[key] };
  };
}

describe('read-only extract', () => {
  it('fails closed without the dedicated read-only token, before any request', async () => {
    const calls = [];
    await expect(extract({ token: undefined, fetchImpl: fakeFetch({}, calls) })).rejects.toThrow(/RESEARCH_D1_READONLY_TOKEN/);
    expect(calls.length).toBe(0);
  });
  it('accepts only single plain SELECT statements; the fixed statements pass', () => {
    for (const s of Object.values(STATEMENTS)) expect(assertReadOnly(s)).toBe(true);
    for (const bad of ['DELETE FROM history', 'SELECT 1; DROP TABLE history', 'UPDATE history SET score = 1', 'WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x',
      'SELECT * FROM history WHERE 1 = 1 ON CONFLICT REPLACE', 'PRAGMA table_info(history)', ' select 1']) {
      expect(() => assertReadOnly(bad)).toThrow(/refused/);
    }
    expect(Object.keys(STATEMENTS)).toEqual(['archive', 'history', 'btc']);
    expect(STATEMENTS.archive).toContain(`> ${BOUNDARY_MS}`);
    expect(STATEMENTS.history).toContain(`> ${BOUNDARY_MS}`);
  });
  it('refuses any response that reports a change', () => {
    expect(() => checkResponse(ok([], { changes: 1 }))).toThrow(/change/);
    expect(() => checkResponse(ok([], { rows_written: 2 }))).toThrow(/change/);
    expect(() => checkResponse(ok([], { changed_db: true }))).toThrow(/change/);
    expect(() => checkResponse({ success: false, result: [] })).toThrow(/failed/);
  });
  it('an HTTP error writes nothing', async () => {
    const f = async () => ({ ok: false, status: 403, json: async () => ({}) });
    await expect(extract({ token: TOKEN, fetchImpl: f })).rejects.toThrow(/HTTP 403/);
  });
  it('produces extracts the progress run accepts; the token never appears in them', async () => {
    const t = BOUNDARY_MS + 3600000;
    const calls = [];
    const out = await extract({
      token: TOKEN, now: () => BOUNDARY_MS + 5 * 3600000,
      fetchImpl: fakeFetch({ archive: ok([]), history: ok([{ ts: t, score: 52, sources_json: '{"fng":40}' }]), btc: ok([{ ts: t - 1000, btc_price: 1 }]) }, calls),
    });
    expect(calls.length).toBe(3);
    expect(calls.every((c) => c.init.method === 'POST' && c.url.endsWith('/d1/database/f91ca980-b886-423a-bd6f-f3baea46d181/query'))).toBe(true);
    expect(JSON.stringify(out)).not.toContain(TOKEN);
    expect(out.v1.manifest).toMatchObject({ as_of_ms: BOUNDARY_MS + 5 * 3600000, rows: { archive: 0, history: 1 } });
    const prog = evaluate({ v1: out.v1, btc: out.btc });
    expect(assertProgressOnly(prog)).toBe(true);
  });
  it('the extractor has no write path and no other credential', () => {
    const src = readFileSync(join(ROOT, 'research/v1_prospective_extract.mjs'), 'utf8');
    expect(src).not.toMatch(/CLOUDFLARE_API_TOKEN|wrangler|STAGE7|\/raw|\/import|\/execute/);
    expect([...src.matchAll(/process\.env\.([A-Z0-9_]+)/g)].map((m) => m[1])).toEqual(['RESEARCH_D1_READONLY_TOKEN']);
  });
});
