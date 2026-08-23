// GitHub evidence gathering. Uses the automatically-provided GITHUB_TOKEN
// (read scope on both repos is sufficient). Every function returns plain
// data or null on a "not found" case; only genuine transport errors throw.

const GITHUB_API = 'https://api.github.com';

function authHeaders(token) {
  return { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'pulseworkerv2-production-chain-audit' };
}

async function ghGet(path, token) {
  if (!token) return { status: null, data: null, unavailable: true };
  const res = await fetch(GITHUB_API + path, { headers: authHeaders(token) });
  if (res.status === 404) return { status: 404, data: null };
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`GitHub API ${path} returned ${res.status}: ${body.slice(0, 300)}`);
  }
  return { status: res.status, data: await res.json() };
}

export async function getMainHeadSha(owner, repo, token) {
  const { status, data } = await ghGet(`/repos/${owner}/${repo}/commits/main`, token);
  if (status !== 200 || !data) return null;
  return { sha: data.sha, committed_at: data.commit?.committer?.date ?? null };
}

// Contents API, pinned to an exact ref -- deliberately NOT
// raw.githubusercontent.com, which this project has previously confirmed
// serves stale content for minutes after a push regardless of
// cache-busting query strings (see .ai docs / prior session notes). Base64
// content up to GitHub's 1MB Contents API limit, sufficient for both
// worker.js and index.html here.
export async function getFileContentAtRef(owner, repo, path, ref, token) {
  const { status, data } = await ghGet(`/repos/${owner}/${repo}/contents/${encodeURIComponent(path)}?ref=${encodeURIComponent(ref)}`, token);
  if (status !== 200 || !data || !data.content) return null;
  return Buffer.from(data.content, data.encoding || 'base64').toString('utf8');
}

// Most recent successful Deploy Worker run on main -- the run that
// actually shipped whatever is currently live, as distinct from "the
// latest commit on main" (which could be un-deployed if the deploy step
// itself failed).
export async function getLatestSuccessfulDeploy(owner, repo, token, { workflowFileName = 'deploy.yml' } = {}) {
  const { status, data } = await ghGet(`/repos/${owner}/${repo}/actions/runs?branch=main&status=success&per_page=20`, token);
  if (status !== 200 || !data || !Array.isArray(data.workflow_runs)) return null;
  const match = data.workflow_runs.find((r) => r.path && r.path.endsWith(workflowFileName));
  if (!match) return null;
  return { run_id: match.id, head_sha: match.head_sha, created_at: match.created_at, updated_at: match.updated_at };
}
