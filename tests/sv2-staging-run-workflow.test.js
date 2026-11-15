// Static guard for .github/workflows/sv2-staging-run.yml: it may only call the STAGING Worker, only from main, only
// inside the protected sv2-staging Environment, with only the staging admin token, and it can never deploy or migrate.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const wf = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '.github', 'workflows', 'sv2-staging-run.yml'), 'utf8');
const code = wf.split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');

describe('sv2-staging-run.yml', () => {
  it('is manual only, main only and inside the protected sv2-staging Environment', () => {
    expect(code).toMatch(/^on:\n  workflow_dispatch:/m);
    expect(code).not.toMatch(/^\s+(push|pull_request|schedule|workflow_run):/m);
    expect(code).toMatch(/^\s+environment: sv2-staging$/m);
    expect(code).toContain('[ "$GITHUB_REF" = "refs/heads/main" ]');
    expect(code).toMatch(/^permissions: \{\}$/m);
  });
  it('talks to the staging Worker only, never production', () => {
    const hosts = [...code.matchAll(/https:\/\/([a-z0-9.-]+)/g)].map((m) => m[1]);
    expect(new Set(hosts)).toEqual(new Set(['pulseworker-v2-staging.quiquandon.workers.dev']));
    expect(code).not.toMatch(/pulseworker-v2\.quiquandon|f91ca980|sentiment-history/);
  });
  it('uses only the staging admin token; no Cloudflare token, no deploy, no migration, no secret echo', () => {
    const secrets = [...code.matchAll(/secrets\.([A-Z0-9_]+)/g)].map((m) => m[1]);
    expect(secrets).toEqual(['STAGE7_STAGING_ADMIN_TOKEN']);
    expect(code).not.toMatch(/wrangler|CLOUDFLARE_API_TOKEN|d1 execute|migrations/);
    expect(code).not.toMatch(/(echo|printf)[^\n]*\$\{?STAGING_ADMIN\b/);
    expect(code).not.toMatch(/set -x/);
  });
  it('fails the job when a requested step failed or was skipped, not only on a non-200 (Codex P2)', () => {
    expect(code).toContain("elif res.get('ok') is not True: bad.append");
    expect(code).toContain("elif res.get('skipped'): bad.append");
    expect(code).toMatch(/if bad:[\s\S]*sys\.exit\(1\)/);
  });
  it('accepts evidence only if generated after this run, otherwise labels it not fresh (Codex P2 x2)', () => {
    expect(code).toContain('/api/sentiment-v2/evidence?hours=24&limit=${LIMIT}');
    expect(code).toMatch(/get\('generated_at'\) or 0\) >= \$RAN_AT/);
    expect(code).toContain("NOT FRESH: every read was an edge-cached response from before this run");
    expect(code).not.toContain('GITHUB_RUN_NUMBER');
  });
  it('offers only the steps the Worker accepts; the GLM probe runs alone and passes only on a validated ok answer', () => {
    const opts = [...code.matchAll(/^\s+- ((?:sync|v2|collect|glm_probe)(?:,(?:sync|v2|collect))*)$/gm)].map((m) => m[1]);
    expect(opts).toEqual(['collect', 'sync,v2', 'sync,v2,collect', 'glm_probe']);
    expect(code).toContain("if res.get('status') != 'ok': bad.append('glm_probe: status=%s reason=%s'");
  });
});
