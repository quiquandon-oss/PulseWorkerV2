"""Fail-closed preflight for production jobs: verifies, through the GitHub REST API, that the deployment environment
actually enforces human approval before any job that holds production credentials may start.

Passes only when ALL hold for the environment:
  - it exists (HTTP 200);
  - it has a `required_reviewers` protection rule with at least one reviewer;
  - its deployment branch policy is custom and allows exactly the branch `main` (no other branch, no tag).
Anything else -- 404, an API error, a missing or empty rule, a broader branch policy -- exits non-zero, so the
production job that depends on this check never starts. Uses no secrets; GITHUB_TOKEN is optional (public repo).

  python3 .github/scripts/verify_production_environment.py --repo owner/name --environment production --branch main
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def fetch(url, token=None):
    """GET a GitHub API URL; returns (status, parsed JSON or None). With a token, a 401/403/404 is retried once without
    auth: GitHub answers 404 (not 403) when a token lacks permission to see a resource, and this repository is public.
    Every attempt's status is printed so a failure shows which case it was."""
    status = 0
    for auth in ([token, None] if token else [None]):
        label = "token" if auth else "anonymous"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        if auth:
            req.add_header("Authorization", f"Bearer {auth}")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"GET {url} ({label}): HTTP {r.status}")
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            status = e.code
            print(f"GET {url} ({label}): HTTP {e.code}")
            if e.code in (401, 403, 404) and auth:
                continue
            return e.code, None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"GET {url} ({label}): {type(e).__name__}")
            return 0, None
    return status, None


def check(repo, environment, branch, get=fetch, token=None):
    """Returns a list of failures (empty means the environment enforces approval as required)."""
    status, env = get(f"{API}/repos/{repo}/environments/{environment}", token)
    if status != 200 or not isinstance(env, dict):
        return [f"environment '{environment}' could not be read (HTTP {status}); it may not exist"]
    failures = []
    rules = env.get("protection_rules") or []
    reviewers = [r for rule in rules if rule.get("type") == "required_reviewers" for r in (rule.get("reviewers") or [])]
    if not reviewers:
        failures.append("no required reviewers: a deployment would not wait for approval")
    policy = env.get("deployment_branch_policy")
    if not policy or not policy.get("custom_branch_policies") or policy.get("protected_branches"):
        failures.append("deployment branch policy must be 'Selected branches' allowing only main")
    else:
        status, pol = get(f"{API}/repos/{repo}/environments/{environment}/deployment-branch-policies", token)
        if status != 200 or not isinstance(pol, dict):
            failures.append(f"deployment branch policies could not be read (HTTP {status})")
        else:
            entries = [(p.get("type", "branch"), p.get("name")) for p in pol.get("branch_policies") or []]
            if entries != [("branch", branch)]:
                failures.append(f"deployment branch policy allows {entries}, expected only [('branch', '{branch}')]")
    return failures


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--environment", required=True)
    ap.add_argument("--branch", required=True)
    a = ap.parse_args(argv)
    failures = check(a.repo, a.environment, a.branch, token=os.environ.get("GITHUB_TOKEN") or None)
    if failures:
        for f in failures:
            print(f"::error::production approval gate not configured: {f}")
        print("Refusing to start the production job. Configure Settings -> Environments -> "
              f"'{a.environment}': required reviewers, and deployment branches 'Selected branches' = {a.branch}.")
        return 1
    print(f"environment '{a.environment}' enforces required reviewers and allows only {a.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
