// GitHub execution evidence gathering. Uses the automatically-provided
// GITHUB_TOKEN (read scope on this repo is sufficient -- no extra secret
// needed). Every function here returns plain data or null on failure; it
// never throws for a "not found" case, only for a genuine transport error,
// so callers can distinguish "checked, doesn't exist" from "couldn't check".

const GITHUB_API = 'https://api.github.com';

function authHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'User-Agent': 'cryptopulsev2-canary-audit',
  };
}

async function ghGet(path, token) {
  const res = await fetch(GITHUB_API + path, { headers: authHeaders(token) });
  if (res.status === 404) return { status: 404, data: null };
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`GitHub API ${path} returned ${res.status}: ${body.slice(0, 300)}`);
  }
  return { status: res.status, data: await res.json() };
}

export async function checkRepoExists(owner, repo, token) {
  const { status } = await ghGet(`/repos/${owner}/${repo}`, token);
  return status === 200;
}

export async function checkBranchExists(owner, repo, branch, token) {
  const { status } = await ghGet(`/repos/${owner}/${repo}/branches/${encodeURIComponent(branch)}`, token);
  return status === 200;
}

export async function checkShaExists(owner, repo, sha, token) {
  const { status } = await ghGet(`/repos/${owner}/${repo}/commits/${sha}`, token);
  return status === 200;
}

// Finds the most relevant workflow run for the target SHA: prefers an
// exact head_sha match, on the Deploy workflow specifically (that's the
// run that actually put the canary code live). Falls back to any run
// matching the SHA if no deploy-workflow run is found. Returns null, not
// a guessed run, if nothing matches -- never picks "the most recent run"
// as a stand-in for "the run for this SHA".
export async function findWorkflowRunForSha(owner, repo, sha, token, { workflowFileName = 'deploy.yml' } = {}) {
  const { status, data } = await ghGet(`/repos/${owner}/${repo}/actions/runs?per_page=100`, token);
  if (status !== 200 || !data || !Array.isArray(data.workflow_runs)) return null;
  const matches = data.workflow_runs.filter((r) => String(r.head_sha).startsWith(sha) || String(sha).startsWith(r.head_sha));
  if (!matches.length) return null;
  const deployMatch = matches.find((r) => r.path && r.path.endsWith(workflowFileName));
  return deployMatch || matches[0];
}

export async function getWorkflowRunJobs(owner, repo, runId, token) {
  const { status, data } = await ghGet(`/repos/${owner}/${repo}/actions/runs/${runId}/jobs`, token);
  if (status !== 200 || !data) return [];
  return (data.jobs || []).map((j) => ({
    id: j.id,
    name: j.name,
    status: j.status,
    conclusion: j.conclusion,
    started_at: j.started_at,
    completed_at: j.completed_at,
    steps: (j.steps || []).map((s) => ({ name: s.name, status: s.status, conclusion: s.conclusion })),
  }));
}

export async function getWorkflowRunArtifacts(owner, repo, runId, token) {
  const { status, data } = await ghGet(`/repos/${owner}/${repo}/actions/runs/${runId}/artifacts`, token);
  if (status !== 200 || !data) return [];
  return (data.artifacts || []).map((a) => ({ id: a.id, name: a.name, size_in_bytes: a.size_in_bytes, created_at: a.created_at, expired: a.expired }));
}
