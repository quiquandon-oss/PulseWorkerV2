// Static guard for .github/workflows/deploy-sv2-staging.yml: staging-only, main-only, environment-scoped, and the
// SEC EDGAR User-Agent reaches the staging Worker runtime from the sv2-staging environment variable.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const wf = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '.github', 'workflows', 'deploy-sv2-staging.yml'), 'utf8');
const executable = wf.split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');

describe('deploy-sv2-staging workflow', () => {
  it('is manual and runs in the main-only sv2-staging environment', () => {
    expect(wf).toMatch(/^on:\n {2}workflow_dispatch:/m);
    expect(wf).not.toMatch(/^ {2}(push|schedule|pull_request):/m);
    expect(wf).toMatch(/^ {4}environment: sv2-staging$/m);
    expect(wf).toContain('"$GITHUB_REF" != "refs/heads/main"');
  });
  it('reads the staging secrets and the SEC User-Agent variable only from the environment context', () => {
    expect(executable).toContain('secrets.STAGE7_STAGING_CLOUDFLARE_API_TOKEN');
    expect(executable).toContain('secrets.STAGE7_STAGING_ADMIN_TOKEN');
    expect(executable).toContain('vars.EVENT_SEC_USER_AGENT');
    expect(executable).not.toMatch(/secrets\.CLOUDFLARE_API_TOKEN/);
  });
  it('fails closed without the User-Agent and passes it to the Worker as a var, never echoing it', () => {
    expect(executable).toContain('EVENT_SEC_USER_AGENT variable missing');
    expect(executable).toContain('--var "EVENT_SEC_USER_AGENT:${EVENT_SEC_USER_AGENT}"');
    expect(executable).not.toMatch(/echo[^\n]*(SEC_UA|EVENT_SEC_USER_AGENT)\b(?!\s+variable missing)/);
  });
  it('deploys only with the staging config and rejects production identifiers', () => {
    expect(executable).toContain('wrangler@4 deploy -c wrangler.staging.toml');
    expect(executable).not.toMatch(/wrangler(@4)? deploy(?! -c wrangler\.staging\.toml)/);
    expect(executable).toContain('f91ca980-b886-423a-bd6f-f3baea46d181');
    expect(executable).toContain('database_name = "sentiment-history"');
    expect(executable).toContain('5458d504-2778-49ae-bd25-7751f1c49d50');
  });
});
