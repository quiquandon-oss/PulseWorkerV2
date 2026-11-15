// Static guard for wrangler.staging.toml: staging-only identifiers, GLM (Workers AI) explicitly disabled, only the
// config keys the deploy workflow's allowlist accepts, and the reviewed 50-subrequest budget left untouched.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const text = readFileSync(join(root, 'wrangler.staging.toml'), 'utf8');
const lines = text.split('\n').filter((l) => !l.trim().startsWith('#'));
const kv = (key) => { const m = lines.map((l) => l.match(new RegExp(`^${key}\\s*=\\s*"(.*)"\\s*$`))).find(Boolean); return m ? m[1] : undefined; };

describe('wrangler.staging.toml', () => {
  it('targets only the staging Worker and staging D1, never a production identifier', () => {
    expect(kv('name')).toBe('pulseworker-v2-staging');
    expect(kv('database_id')).toBe('5458d504-2778-49ae-bd25-7751f1c49d50');
    expect(kv('database_name')).toBe('pulseworker-v2-staging');
    const body = lines.join('\n');
    expect(body).not.toContain('f91ca980-b886-423a-bd6f-f3baea46d181');
    expect(body).not.toContain('sentiment-history');
    expect(body).not.toMatch(/^name = "pulseworker-v2"$/m);
  });
  it('allows GLM on staging only as glm-4.7-flash with a hard 200-Neuron daily budget (2% of the free allocation)', () => {
    expect(kv('SENTIMENT_V2_AI_MODEL')).toBe('@cf/zai-org/glm-4.7-flash');
    expect(Number(kv('SV2_GLM_DAILY_NEURONS'))).toBeLessThanOrEqual(200);
    expect(text).toMatch(/GLM IS ENABLED ON STAGING/);
    expect(text).not.toMatch(/stays DISABLED/);
    const worker = readFileSync(join(root, 'worker.js'), 'utf8');
    expect(worker).toContain("if (!model || budget === 0) return { status: 'disabled'");
    expect(worker).toContain("'@cf/zai-org/glm-4.7-flash': { neurons_per_m_input: 5500, neurons_per_m_output: 36400 }");
  });
  it('runs the free independent evidence collector in staging and sets no paid-provider setting', () => {
    expect(kv('SV2_COLLECTOR_ENABLED')).toBe('1');
    expect(lines.join('\n')).not.toMatch(/API_KEY|TOKEN|SECRET|OPENAI|ANTHROPIC|SERPAPI|BRAVE|TAVILY|BING/i);
  });
  it('sets no variable the Worker does not read, and no build, trigger or route keys', () => {
    const worker = readFileSync(join(root, 'worker.js'), 'utf8') + readFileSync(join(root, 'sentiment-v2', 'collector.js'), 'utf8');
    const vars = [...lines.join('\n').matchAll(/^([A-Z][A-Z0-9_]+)\s*=\s*"/gm)].map((m) => m[1]).filter((k) => k !== 'GIT_COMMIT_SHA');
    for (const k of vars) expect(worker, `${k} is set in staging but read by no code`).toContain(`env.${k}`);
    expect(lines.join('\n')).not.toMatch(/^\[(build|triggers|env|\[routes\])|^(routes?|triggers|build)\s*=/m);
  });
  it('leaves the shared 50-subrequest budget in the event runner as is', () => {
    const core = readFileSync(join(root, 'events', 'event-core.js'), 'utf8');
    expect(core).toContain('Workers Free: 50 external subrequests and 50 D1 queries per invocation');
  });
});
