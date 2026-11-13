// sec-v2-ops: owner-only operational write routes. Real worker.fetch; a DB/fetch that records any use.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import worker from '../worker.js';

const OPS = [['GET', '/btc-backfill'], ['GET', '/btc-backfill-hourly'], ['GET', '/link-backfill'], ['GET', '/link-backfill-hourly'],
  ['GET', '/eth-backfill-history'], ['GET', '/recalibrate-refresh'], ['GET', '/recalibrate-refresh-challenger'],
  ['GET', '/run-link-analysis'], ['GET', '/run-analysis'], ['GET', '/backtest-technical'], ['GET', '/backtest-feature'],
  ['GET', '/backtest-combined'], ['GET', '/backtest-regime-split'], ['POST', '/analyst-relay/submit']];
const T = 'ops-token-0123456789abcdef';
function trapDb() {
  const used = [];
  const stmt = () => ({ bind: () => stmt(), run: async () => { used.push('run'); return { meta: {} }; }, all: async () => { used.push('all'); return { results: [] }; }, first: async () => { used.push('first'); return null; } });
  return { used, prepare: (q) => { used.push(q); return stmt(); }, batch: async () => { used.push('batch'); return []; } };
}
const call = (m, p, h = {}, env = {}) => worker.fetch(new Request('https://w' + p, { method: m, headers: h, body: m === 'POST' ? '{}' : undefined }), env);
let rf;
beforeEach(() => { rf = globalThis.fetch; globalThis.fetch = vi.fn(async () => new Response('[]', { status: 200 })); vi.spyOn(console, 'log').mockImplementation(() => {}); vi.spyOn(console, 'error').mockImplementation(() => {}); });
afterEach(() => { globalThis.fetch = rf; vi.restoreAllMocks(); });

describe('operational write routes', () => {
  for (const [m, p] of OPS) {
    it(`${m} ${p}: 503 without the secret, 401 on a wrong/missing bearer, no DB or network use; passes with the token`, async () => {
      for (const [h, env, code] of [[{}, {}, 503], [{ Authorization: 'Bearer ' + T }, {}, 503], [{}, { OPS_ADMIN_TOKEN: T }, 401],
        [{ Authorization: 'Bearer wrong' }, { OPS_ADMIN_TOKEN: T }, 401], [{ Authorization: T }, { OPS_ADMIN_TOKEN: T }, 401]]) {
        const db = trapDb();
        const r = await call(m, p, h, { DB: db, ...env });
        expect(r.status).toBe(code);
        expect(db.used).toEqual([]);
        expect(globalThis.fetch).not.toHaveBeenCalled();
      }
      const r = await call(m, p, { Authorization: 'Bearer ' + T }, { DB: trapDb(), OPS_ADMIN_TOKEN: T });
      expect([401, 503]).not.toContain(r.status);
    });
  }
  it('the Research Lab admin secret works when OPS_ADMIN_TOKEN is not set, and OPS_ADMIN_TOKEN takes precedence when it is', async () => {
    const S7 = 'stage7-admin-abcdef'; const p = '/recalibrate-refresh';
    expect((await call('GET', p, { Authorization: 'Bearer wrong' }, { DB: trapDb(), STAGE7_ADMIN_TOKEN: S7 })).status).toBe(401);
    expect([401, 503]).not.toContain((await call('GET', p, { Authorization: 'Bearer ' + S7 }, { DB: trapDb(), STAGE7_ADMIN_TOKEN: S7 })).status);
    expect((await call('GET', p, { Authorization: 'Bearer ' + S7 }, { DB: trapDb(), STAGE7_ADMIN_TOKEN: S7, OPS_ADMIN_TOKEN: T })).status).toBe(401);
    expect([401, 503]).not.toContain((await call('GET', p, { Authorization: 'Bearer ' + T }, { DB: trapDb(), STAGE7_ADMIN_TOKEN: S7, OPS_ADMIN_TOKEN: T })).status);
  });
  it('POST /analyst-relay/submit: an authorised request records, an oversized one is refused, and the preflight allows the Authorization header', async () => {
    const env = { DB: trapDb(), OPS_ADMIN_TOKEN: T };
    const post = (body, h = { Authorization: 'Bearer ' + T }) => worker.fetch(new Request('https://w/analyst-relay/submit', { method: 'POST', headers: { 'Content-Type': 'application/json', ...h }, body: JSON.stringify(body) }), env);
    expect((await post({ rawResponseText: 'x'.repeat(200 * 1024 + 1) })).status).toBe(413);
    expect((await post({ rawResponseText: '' })).status).toBe(400);
    expect(env.DB.used).toEqual([]);
    const ok = await post({ rawResponseText: 'an analyst answer', candidateId: 'c1', assets: ['BTC'] });
    expect([401, 403, 413, 503]).not.toContain(ok.status);
    expect(env.DB.used.length).toBeGreaterThan(0);
    const pre = await worker.fetch(new Request('https://w/analyst-relay/submit', { method: 'OPTIONS' }), {});
    expect(pre.headers.get('Access-Control-Allow-Headers')).toContain('Authorization');
  });
  it('read routes and /predict are not affected', async () => {
    for (const p of ['/chart-data', '/prediction-freshness', '/calibration-history']) {
      const r = await call('GET', p, {}, { DB: trapDb() });
      expect([401, 503]).not.toContain(r.status);
    }
  });
});
