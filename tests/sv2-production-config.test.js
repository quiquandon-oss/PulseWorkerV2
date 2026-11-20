// Production safety for the Sentiment V2 evidence work: the PRODUCTION configuration must keep GLM and the collector OFF
// (GLM twice over: an empty model id and a zero Neuron budget), V1 schedules and targets must be unchanged, and a
// production deploy must stay behind the `production` environment approval gate. Static checks on the files that ship.
// Staging expectations are asserted here as well so the two configurations cannot drift into each other.
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const text = (p) => readFileSync(join(ROOT, p), 'utf8');
const noComments = (s) => s.split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');
const kv = (src, key) => { const m = noComments(src).match(new RegExp(`^${key}\\s*=\\s*"(.*)"\\s*(#.*)?$`, 'm')); return m ? m[1] : undefined; };

// A small TOML reader for the subset wrangler configs use: [table], [a.b], [[array of tables]], and key = "string" | number |
// true | false | ["string", ...] on one line, with # comments. Anything else throws, so a new construct cannot slip past.
function parseToml(src) {
  const root = {};
  let cur = root;
  const stripComment = (l) => { let q = false; for (let i = 0; i < l.length; i += 1) { if (l[i] === '"') q = !q; else if (l[i] === '#' && !q) return l.slice(0, i); } return l; };
  const val = (v) => {
    if (/^"(?:[^"\\]|\\.)*"$/.test(v)) return JSON.parse(v);
    if (/^-?\d+(\.\d+)?$/.test(v)) return Number(v);
    if (v === 'true' || v === 'false') return v === 'true';
    if (/^\[\s*("(?:[^"\\]|\\.)*"\s*(,\s*)?)*\]$/.test(v)) return JSON.parse(v.replace(/,\s*\]$/, ']'));
    throw new Error(`unsupported TOML value: ${v}`);
  };
  const walk = (path, create) => path.split('.').reduce((o, k) => { if (!(k in o)) o[k] = create === 'array' ? [] : {}; return o[k]; }, root);
  for (const raw of src.split('\n')) {
    const line = stripComment(raw).trim();
    if (!line) continue;
    let m;
    if ((m = line.match(/^\[\[([\w.]+)\]\]$/))) { const parts = m[1].split('.'); const parent = parts.length > 1 ? walk(parts.slice(0, -1).join('.')) : root; const k = parts.at(-1); (parent[k] = parent[k] || []).push(cur = {}); }
    else if ((m = line.match(/^\[([\w.]+)\]$/))) cur = walk(m[1]);
    else if ((m = line.match(/^([\w-]+)\s*=\s*(.+)$/))) cur[m[1]] = val(m[2].trim());
    else throw new Error(`unsupported TOML line: ${line}`);
  }
  return root;
}

const PROD = text('wrangler.toml');
const STAGING = text('wrangler.staging.toml');
const DEPLOY = text('.github/workflows/deploy.yml');
const deployCode = noComments(DEPLOY);

describe('production wrangler.toml: GLM is OFF; the collector is ON behind its safety stop', () => {
  it('the GLM model id is EMPTY (not merely a zero budget)', () => {
    expect(kv(PROD, 'SENTIMENT_V2_AI_MODEL')).toBe('');
  });
  it('the Neuron budget is "0"; the collector is "1" with Bitcoin Magazine disabled and no stop override', () => {
    expect(kv(PROD, 'SV2_GLM_DAILY_NEURONS')).toBe('0');
    expect(kv(PROD, 'SV2_COLLECTOR_ENABLED')).toBe('1');
    expect(kv(PROD, 'SV2_SOURCES_DISABLED')).toBe('bitcoinmagazine');
    // The daily D1 stop runs at its defaults in production: no threshold override, no freshness opt-out, recording on.
    const code = noComments(PROD);
    expect(code).not.toMatch(/SV2_COLLECTOR_D1_(READ|WRITE)_STOP|SV2_USAGE_GUARD_REQUIRE_FRESH|D1_USAGE_TABLE/);
  });
  it('no GLM model name or other provider appears in any non-comment line, and no key or token is configured', () => {
    const code = noComments(PROD);
    expect(code).not.toMatch(/zai-org|@cf\/|glm-\d/i);   // no model identifier (the variable NAME SV2_GLM_DAILY_NEURONS is fine)
    expect(code).not.toMatch(/API_KEY|OPENAI|ANTHROPIC|SERPAPI|BRAVE|TAVILY|BING/i);
  });
  it('the only AI route is the existing Workers AI binding and worker.js needs BOTH a model and a budget to call it', () => {
    expect(noComments(PROD)).toMatch(/\[ai\]\s*\nbinding = "AI"/);
    expect(text('worker.js')).toContain("if (!model || budget === 0) return { status: 'disabled'");
  });
  it('V1 schedules and the production targets are unchanged', () => {
    expect(noComments(PROD)).toMatch(/^crons = \["0 \* \* \* \*", "0 7 \* \* \*"\]/m);
    expect(kv(PROD, 'V1_ARCHIVE_SYNC_ENABLED')).toBe('1');
    expect(kv(PROD, 'SENTIMENT_V2_ENABLED')).toBe('1');
  });
  it('the ENTIRE parsed production configuration equals the reviewed snapshot: every top-level key, every key of every table, every variable', () => {
    // Any added, removed or changed setting (workers_dev, routes, vars, observability, a new binding...) fails here and must be reviewed.
    expect(parseToml(PROD)).toEqual({
      name: 'pulseworker-v2',
      main: 'worker.js',
      compatibility_date: '2024-11-01',
      account_id: 'f58e761fbc8e62dc404d8684290af264',
      ai: { binding: 'AI' },
      vars: {
        GIT_COMMIT_SHA: 'unknown',
        SENTIMENT_V2_ENABLED: '1',
        SENTIMENT_V2_AI_MODEL: '',          // GLM off, part 1: no model id
        SV2_GLM_DAILY_NEURONS: '0',         // GLM off, part 2: no Neuron budget
        GEMINI_DAILY_CRON_ENABLED: '0',         // 07:00 Gemini narrative stopped (2026-11-17)
        EXP002_CRON_ENABLED: '0',               // EXP-002 scheduled logging stopped
        EXP003_CRON_ENABLED: '0',               // EXP-003 scheduled logging stopped
        SV2_COLLECTOR_ENABLED: '1',         // collector on (2026-11-15), behind the daily D1 stop
        SV2_SOURCES_DISABLED: 'bitcoinmagazine', // feed redirects to plain http
        RESEARCH_FUNDING_DISPATCH_ENABLED: '0', // EXP-023 intraday funding dispatch paused (2026-11-17)
        V1_ARCHIVE_SYNC_ENABLED: '1',
        CHALLENGER_ENABLED: '1',                // challenger pause switch, ON until the owner activates the pause
        SV2_V1_SERVER_INPUT: '1',               // V1 hourly server readings as a second V2 input (2026-11-16)
        GLM_SIGNAL_ENABLED: '1',                // GLM daily signal record (07:00), inference off -> status model_disabled
        GLM_INFERENCE_ENABLED: '1',             // owner mandate 2026-11-20: one GLM call per day
        GLM_TICK_HOOK_ENABLED: '1',             // per BTC 24 h tick: baseline / applied / final ledger + snapshot glm object
        GLM_INFLUENCE_CAP: '0.03',              // PROVISIONAL bounded influence on ELIGIBLE signals (hard max 0.10)
        GLM_ALLOW_FLIP: '0',                    // GLM never reverses the baseline side of 0.5
        GLM_DAILY_NEURONS: '300',               // GLM's own Neuron cap (one call), shared atomic ledger
      },
      d1_databases: [{ binding: 'DB', database_name: 'sentiment-history', database_id: 'f91ca980-b886-423a-bd6f-f3baea46d181' }],
      triggers: { crons: ['0 * * * *', '0 7 * * *'] },
      observability: { logs: { enabled: true, head_sampling_rate: 0.1, invocation_logs: false, persist: true }, traces: { enabled: false, head_sampling_rate: 1, persist: true } },
    });
  });
  it('the parser itself rejects constructs it does not understand (so nothing can hide behind unsupported syntax)', () => {
    expect(() => parseToml('a = { b = 1 }')).toThrow(/unsupported TOML value/);
    expect(() => parseToml('weird line')).toThrow(/unsupported TOML line/);
    expect(parseToml('[t]\nk = "a # not a comment"  # comment\n')).toEqual({ t: { k: 'a # not a comment' } });
  });
  it('the complete production deployment target is unchanged: Worker, entry point, account, and the single D1 binding', () => {
    const code = noComments(PROD);
    const top = code.slice(0, code.search(/^\[/m));                       // keys before the first table
    const topKv = (k) => (top.match(new RegExp(`^${k}\\s*=\\s*"(.*)"`, 'm')) || [])[1];
    expect(topKv('name')).toBe('pulseworker-v2');
    expect(topKv('main')).toBe('worker.js');
    expect(topKv('compatibility_date')).toBe('2024-11-01');
    expect(topKv('account_id')).toBe('f58e761fbc8e62dc404d8684290af264');
    // exactly one D1 binding and it is the production database, under the binding name the Worker code uses
    expect((code.match(/^\[\[d1_databases\]\]/gm) || []).length).toBe(1);
    const d1 = code.slice(code.indexOf('[[d1_databases]]')).split(/^\[(?!\[d1_databases)/m)[0];
    const d1kv = (k) => (d1.match(new RegExp(`^${k}\\s*=\\s*"(.*)"`, 'm')) || [])[1];
    expect({ binding: d1kv('binding'), database_name: d1kv('database_name'), database_id: d1kv('database_id') })
      .toEqual({ binding: 'DB', database_name: 'sentiment-history', database_id: 'f91ca980-b886-423a-bd6f-f3baea46d181' });
    // the same set of tables as before: no new binding, trigger table, service, route or build step
    expect([...code.matchAll(/^\[{1,2}([\w.]+)\]{1,2}/gm)].map((m) => m[1]).sort())
      .toEqual(['ai', 'd1_databases', 'observability.logs', 'observability.traces', 'triggers', 'vars']);
    expect(code).not.toMatch(/^(routes?|services|kv_namespaces|r2_buckets|queues|durable_objects|build|env)\b/m);
  });
});

describe('staging wrangler.staging.toml: collector ON, GLM bounded to a 200-Neuron daily probe budget', () => {
  it('collector enabled; GLM only glm-4.7-flash with at most 200 Neurons per day (production stays off)', () => {
    expect(kv(STAGING, 'SV2_COLLECTOR_ENABLED')).toBe('1');
    expect(kv(STAGING, 'SENTIMENT_V2_AI_MODEL')).toBe('@cf/zai-org/glm-4.7-flash');
    expect(Number(kv(STAGING, 'SV2_GLM_DAILY_NEURONS'))).toBeLessThanOrEqual(200);
    expect(kv(PROD, 'SENTIMENT_V2_AI_MODEL')).toBe('');
    expect(kv(PROD, 'SV2_GLM_DAILY_NEURONS')).toBe('0');
  });
  it('is the staging target, never a production identifier', () => {
    expect(kv(STAGING, 'name')).toBe('pulseworker-v2-staging');
    expect(noComments(STAGING)).toContain('5458d504-2778-49ae-bd25-7751f1c49d50');
    expect(noComments(STAGING)).not.toContain('f91ca980-b886-423a-bd6f-f3baea46d181');
  });
});

describe('production deploy stays behind the approval gate (deploy.yml)', () => {
  it('runs only on a push to main, or a manual dispatch: no pull_request, schedule or workflow_run trigger', () => {
    expect(deployCode).toMatch(/^on:\n {2}push:\n {4}branches: \[main\]/m);
    expect(deployCode).toMatch(/^ {2}workflow_dispatch: \{\}/m);
    expect(deployCode).not.toMatch(/^ {2}(pull_request|pull_request_target|schedule|workflow_run|repository_dispatch):/m);
  });
  it('the deploy job needs the tests and the approval-gate job, runs only on main, and is in the `production` environment', () => {
    const deploy = deployCode.slice(deployCode.indexOf('\n  deploy:'));
    expect(deploy).toMatch(/needs: \[test, approval-gate\]/);
    expect(deploy).toMatch(/if: github\.ref == 'refs\/heads\/main' && needs\.test\.result == 'success' && needs\.approval-gate\.result == 'success'/);
    expect(deploy).toMatch(/^ {4}environment: production$/m);
  });
  it('the approval-gate job fails the run unless `production` has required reviewers and allows only main', () => {
    const gate = deployCode.slice(deployCode.indexOf('\n  approval-gate:'), deployCode.indexOf('\n  deploy:'));
    expect(gate).toContain('verify_production_environment.py');
    expect(gate).toContain('--environment production --branch main');
    expect(gate).not.toMatch(/continue-on-error|\|\| true/);
    const script = text('.github/scripts/verify_production_environment.py');
    expect(script).toContain('required_reviewers');
    expect(script).toContain("custom_branch_policies");
  });
  it('the production deploy command changes no configuration: it sets only GIT_COMMIT_SHA and never a GLM, collector or secret override', () => {
    expect(deployCode).toContain('command: deploy --var GIT_COMMIT_SHA:${{ github.sha }}');
    expect(deployCode).not.toMatch(/SV2_|SENTIMENT_V2|--var (?!GIT_COMMIT_SHA)/);
    expect([...new Set([...deployCode.matchAll(/secrets\.([A-Z0-9_]+)/g)].map((m) => m[1]))].sort()).toEqual(['CLOUDFLARE_API_TOKEN', 'GEMINI_API_KEY']);   // V1's existing secrets only
  });
  it('the new evidence modules trigger the same gated deploy, not a separate or ungated one', () => {
    expect(deployCode).toContain("- 'sentiment-v2/**'");
    expect(deployCode).toContain("- 'observability/**'");
    expect(deployCode).toContain("- 'events/**'");
  });
});

describe('only the known, gated or staging-only workflows can deploy a Worker', () => {
  const DEPLOY_CMD = /(^|[^\w-])wrangler(@\d+)?\s+(versions\s+)?deploy|wrangler-action|command:\s*deploy\b/;
  const files = readdirSync(join(ROOT, '.github/workflows')).filter((f) => f.endsWith('.yml'));
  const deployers = files.filter((f) => DEPLOY_CMD.test(noComments(text(`.github/workflows/${f}`)))).sort();
  it('the set of deploy-capable workflows is exactly the reviewed set (a new one needs a deliberate change here)', () => {
    expect(deployers).toEqual(['deploy-research-dispatcher.yml', 'deploy-stage7-staging.yml', 'deploy-sv2-staging.yml', 'deploy.yml', 'test.yml']);
  });
  it('each production-token deployer is in the `production` environment; the rest use only the staging token', () => {
    for (const f of deployers) {
      const code = noComments(text(`.github/workflows/${f}`));
      const usesProd = /secrets\.CLOUDFLARE_API_TOKEN\b/.test(code);
      if (usesProd) expect(code, f).toMatch(/^\s+environment: production\b/m);
      if (f === 'deploy-stage7-staging.yml' || f === 'deploy-sv2-staging.yml') expect(code, `${f} must not use the production token`).not.toMatch(/secrets\.CLOUDFLARE_API_TOKEN\b/);
    }
  });
  it('the staging deployers target only the staging config', () => {
    for (const f of ['deploy-stage7-staging.yml', 'deploy-sv2-staging.yml']) {
      const code = noComments(text(`.github/workflows/${f}`));
      expect(code, f).not.toMatch(/wrangler(@\d+)?\s+deploy(?! -c wrangler\.staging\.toml)/);
    }
  });
});
