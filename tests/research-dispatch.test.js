// Reliable trigger for the hourly research funding collector (worker.js).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { extractFunctions, extractConstants, evalInScope } from './helpers/extract.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = readFileSync(join(ROOT, 'worker.js'), 'utf8');
const scope = evalInScope(extractConstants('RESEARCH_DISPATCH') + '\n' + extractFunctions('dispatchResearchCollection'), {});
const quiet = () => { const l = console.log, e = console.error; console.log = () => {}; console.error = () => {}; return () => { console.log = l; console.error = e; }; };

describe('dispatchResearchCollection', () => {
  it('does nothing without a token', async () => {
    let called = false;
    const r = await scope.dispatchResearchCollection({}, async () => { called = true; });
    expect(r).toEqual({ dispatched: false, reason: 'no_token' });
    expect(called).toBe(false);
  });

  it('dispatches exactly the funding workflow on main, with the token and a User-Agent', async () => {
    const restore = quiet(); let seen;
    try {
      const r = await scope.dispatchResearchCollection({ RESEARCH_DISPATCH_TOKEN: 't0k' }, async (url, init) => { seen = { url, init }; return { status: 204 }; });
      expect(r).toEqual({ dispatched: true, status: 204 });
    } finally { restore(); }
    expect(seen.url).toBe('https://api.github.com/repos/quiquandon-oss/PulseWorkerV2/actions/workflows/research-funding-intraday-schedule.yml/dispatches');
    expect(seen.init.method).toBe('POST');
    expect(seen.init.headers.Authorization).toBe('Bearer t0k');
    expect(seen.init.headers['User-Agent']).toBeTruthy();
    expect(JSON.parse(seen.init.body)).toEqual({ ref: 'main' });
  });

  it('reports a refused dispatch and never throws on network errors', async () => {
    const restore = quiet();
    try {
      expect(await scope.dispatchResearchCollection({ RESEARCH_DISPATCH_TOKEN: 'x' }, async () => ({ status: 403 }))).toEqual({ dispatched: false, status: 403 });
      expect(await scope.dispatchResearchCollection({ RESEARCH_DISPATCH_TOKEN: 'x' }, async () => { throw new Error('down'); })).toEqual({ dispatched: false, reason: 'error' });
    } finally { restore(); }
  });

  it('is wired to the hourly cron only, outside the tick handlers', () => {
    const start = SRC.indexOf('async scheduled(event, env, ctx) {', SRC.indexOf('export default {'));
    const body = SRC.slice(start, SRC.indexOf('\n  },\n', start));
    expect(body).toMatch(/if \(event && event\.cron === '0 \* \* \* \*'\) \{\s*ctx\.waitUntil\(dispatchResearchCollection\(env\)\);/);
  });
});

describe('research-funding-intraday-schedule.yml', () => {
  const y = readFileSync(join(ROOT, '.github/workflows/research-funding-intraday-schedule.yml'), 'utf8');
  const on = y.slice(y.indexOf('\non:'), y.indexOf('\npermissions:'));
  it('keeps its schedule and adds only workflow_dispatch', () => {
    expect(on).toContain("- cron: '17 * * * *'");
    expect(on).toMatch(/\n  workflow_dispatch: \{\}\n/);
    expect(on).not.toMatch(/\n  (push|pull_request|workflow_run|repository_dispatch):/);
  });
  it('still runs the same pinned collector into the same single target', () => {
    expect(y).toContain('ref: a199b795932f1bbfd36306495a16cf2921b06d33');
    expect(y).toContain('--sources hyperliquid_btc_funding okx_btc_funding --max-days 60 --open-day');
    expect(y).toContain('TARGET: research-data/risk-regime-forward');
    expect(y).toContain('group: research-forward-collection');
  });
});
