// Cloudflare Worker deployment metadata -- READ ONLY. Never calls any
// endpoint that creates, updates, or deletes anything (no PUT to the
// script content endpoint, no deploy trigger). If CLOUDFLARE_API_TOKEN or
// CLOUDFLARE_ACCOUNT_ID is missing, every function here returns null
// immediately -- callers treat that as "credentials unavailable" and mark
// the corresponding checks UNAVAILABLE, per the task's explicit
// requirement not to fail the whole audit over this alone.

const CF_API = 'https://api.cloudflare.com/client/v4';

function authHeaders(token) {
  return { Authorization: `Bearer ${token}` };
}

async function cfGet(path, token) {
  const res = await fetch(CF_API + path, { headers: authHeaders(token) });
  const body = await res.json().catch(() => null);
  if (!res.ok || !body || body.success === false) {
    return { ok: false, status: res.status, errors: body ? body.errors : null };
  }
  return { ok: true, status: res.status, result: body.result };
}

// GET-only: confirms the Worker script exists and returns its metadata
// (name, modified_on) -- does NOT fetch or expose the source (not needed
// for this audit and avoids any chance of leaking secrets baked into
// build output).
export async function getWorkerMetadata(accountId, scriptName, token) {
  if (!accountId || !scriptName || !token) return null;
  const { ok, result } = await cfGet(`/accounts/${accountId}/workers/scripts/${scriptName}`, token);
  if (!ok || !result) return null;
  return { id: result.id, etag: result.etag, modified_on: result.modified_on };
}

// GET-only: deployment history for the script (Cloudflare's gradual-
// deployments API). Returns the most recent deployment's metadata, or
// null if the account/token doesn't have this feature enabled or lacks
// permission -- treated as UNAVAILABLE, not a failure.
export async function getLatestDeployment(accountId, scriptName, token) {
  if (!accountId || !scriptName || !token) return null;
  const { ok, result } = await cfGet(`/accounts/${accountId}/workers/scripts/${scriptName}/deployments`, token);
  if (!ok || !result) return null;
  const deployments = result.deployments || result.items || (Array.isArray(result) ? result : []);
  if (!deployments.length) return null;
  const latest = deployments[0];
  return { id: latest.id, created_on: latest.created_on, source: latest.source, strategy: latest.strategy };
}
