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
  it('keeps GLM disabled: empty model and a zero daily call ceiling', () => {
    expect(kv('SENTIMENT_V2_AI_MODEL')).toBe('');
    expect(kv('SENTIMENT_V2_AI_DAILY_CALLS')).toBe('0');
    expect(lines.join('\n')).not.toMatch(/glm/i);
  });
  it('sets no variable the Worker does not read, and no build, trigger or route keys', () => {
    expect(lines.join('\n')).not.toMatch(/SV2_GLM_DAILY_NEURONS|SV2_COLLECTOR_ENABLED/);
    expect(lines.join('\n')).not.toMatch(/^\[(build|triggers|env|\[routes\])|^(routes?|triggers|build)\s*=/m);
  });
  it('leaves the shared 50-subrequest budget in the event runner as is', () => {
    const core = readFileSync(join(root, 'events', 'event-core.js'), 'utf8');
    expect(core).toContain('Workers Free: 50 external subrequests and 50 D1 queries per invocation');
  });
});
