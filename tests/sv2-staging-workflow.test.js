// Static guard for .github/workflows/deploy-sv2-staging.yml: staging-only, main-only, environment-scoped, and the SEC
// User-Agent (it contains a contact address) is a MASKED environment secret that never appears in a step's `env:`
// header as a plain variable, in a command line, in an echo, or in a Worker variable.
// Regression for run 43695682270, whose log printed the value because it came from `vars.` (variables are not masked).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const wf = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '.github', 'workflows', 'deploy-sv2-staging.yml'), 'utf8');
const code = wf.split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');

// Splits the job's steps and, for each, extracts name, env entries (KEY -> expression) and the run body.
function steps() {
  const body = code.slice(code.indexOf('    steps:\n') + '    steps:\n'.length);
  return body.split(/^ {6}- /m).filter((s) => s.trim()).map((chunk) => {
    const lines = chunk.split('\n');
    const name = (lines.find((l) => /^name:/.test(l)) || '').replace(/^name:\s*/, '') || (lines[0].match(/^uses:\s*(.*)/) || [])[1] || '';
    const env = {};
    const envAt = lines.findIndex((l) => /^ {8}env:\s*$/.test(l));
    if (envAt >= 0) {
      for (let i = envAt + 1; i < lines.length && /^ {10}\S/.test(lines[i]); i++) {
        const m = lines[i].match(/^ {10}([A-Z0-9_]+):\s*(.*)$/);
        if (m) env[m[1]] = m[2].trim();
      }
    }
    const runAt = lines.findIndex((l) => /^ {8}run:/.test(l));
    const run = runAt >= 0 ? lines.slice(runAt).join('\n') : '';
    return { name, env, run, raw: chunk };
  });
}
const all = steps();
const step = (n) => all.find((s) => s.name.startsWith(n));

describe('deploy-sv2-staging workflow', () => {
  it('parses into the expected steps', () => {
    expect(all.length).toBeGreaterThanOrEqual(10);
    expect(step('Deploy to the staging Worker')).toBeTruthy();
    expect(step('Set the staging admin token')).toBeTruthy();
    expect(step('Set the staging SEC User-Agent')).toBeTruthy();
  });

  it('is manual and runs in the main-only sv2-staging environment', () => {
    expect(wf).toMatch(/^on:\n {2}workflow_dispatch:/m);
    expect(wf).not.toMatch(/^ {2}(push|schedule|pull_request):/m);
    expect(wf).toMatch(/^ {4}environment: sv2-staging$/m);
    expect(wf).toContain('"$GITHUB_REF" != "refs/heads/main"');
  });

  it('reads credentials only from environment secrets, never the production token', () => {
    const used = new Set([...code.matchAll(/secrets\.([A-Z0-9_]+)/g)].map((m) => m[1]));
    expect([...used].sort()).toEqual(['EVENT_SEC_USER_AGENT', 'STAGE7_STAGING_ADMIN_TOKEN', 'STAGE7_STAGING_CLOUDFLARE_API_TOKEN']);
  });

  it('never takes the SEC User-Agent (or any token) from an unmasked variable', () => {
    expect(code).not.toMatch(/vars\./);
    for (const s of all) for (const [k, v] of Object.entries(s.env)) {
      if (/(AGENT|TOKEN|SECRET|PASSWORD|UA)$/.test(k)) expect(v, `${s.name}: ${k}`).toMatch(/^\$\{\{ secrets\.[A-Z0-9_]+ \}\}$/);
    }
  });

  it('keeps the SEC User-Agent out of every step except the check and the set-secret step', () => {
    const holders = all.filter((s) => Object.values(s.env).some((v) => v.includes('EVENT_SEC_USER_AGENT')));
    expect(holders.map((s) => s.name).sort()).toEqual(['Set the staging SEC User-Agent', 'Verify the staging-only secrets are configured']);
    expect(step('Deploy to the staging Worker').raw).not.toMatch(/SEC_UA|EVENT_SEC_USER_AGENT/);
  });

  it('sets the User-Agent as a Worker secret through stdin, never as a --var or argument', () => {
    const s = step('Set the staging SEC User-Agent');
    expect(s.run).toContain(`printf '%s' "$SEC_UA" | npx --yes wrangler@4 secret put EVENT_SEC_USER_AGENT -c wrangler.staging.toml`);
    expect(code).not.toMatch(/--var[ =]"?EVENT_SEC_USER_AGENT/);
    expect(code).not.toMatch(/--var[^\n]*SEC_UA/);
    // runs after the deploy that stops setting a variable of that name
    expect(all.indexOf(s)).toBeGreaterThan(all.indexOf(step('Deploy to the staging Worker')));
  });

  it('never echoes, traces or debug-logs a credential', () => {
    expect(code).not.toMatch(/\bset -[a-z]*x/);
    expect(code).not.toMatch(/ACTIONS_STEP_DEBUG|ACTIONS_RUNNER_DEBUG/);
    expect(code).not.toMatch(/echo[^\n]*\$\{?(SEC_UA|ADMIN|STAGING_ADMIN|STAGING_TOKEN|EVENT_SEC_USER_AGENT)\b/);
  });

  it('fails closed when the User-Agent secret is missing', () => {
    expect(step('Verify the staging-only secrets are configured').run).toContain('EVENT_SEC_USER_AGENT secret missing');
  });

  it('deploys only with the staging config and rejects production identifiers', () => {
    expect(code).toContain('wrangler@4 deploy -c wrangler.staging.toml');
    expect(code).not.toMatch(/wrangler(@4)? deploy(?! -c wrangler\.staging\.toml)/);
    expect(code).toContain('f91ca980-b886-423a-bd6f-f3baea46d181');
    expect(code).toContain('database_name = "sentiment-history"');
    expect(code).toContain('5458d504-2778-49ae-bd25-7751f1c49d50');
  });
});
