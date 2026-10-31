"""Production deployment safeguards (2026-10-31): static checks of every GitHub Actions workflow, plus a simulation
of GitHub's job-gating rules for the deploy jobs.

What this proves and what it does not:
- STATIC: the workflow files are structured so that every production deploy or production-secret job is gated on
  the full CI suite, restricted to refs/heads/main, and bound to the `production` environment, and that no other
  workflow can deploy with the production token or call the deploy workflows.
- SIMULATED: the `if:` expressions, evaluated under GitHub's documented rules (a job whose `if:` has no status
  function gets an implicit success(); a skipped, failed or cancelled `needs` job is not 'success'), allow a deploy
  only for a main-branch run whose test job succeeded.
- NOT PROVEN HERE: that GitHub enforces required reviewers on the `production` environment. That is a repository
  setting an administrator must configure; these tests cannot see it.
"""
import glob
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")
CI_TESTS = "./.github/workflows/ci-tests.yml"
PROD_TOKEN = "secrets.CLOUDFLARE_API_TOKEN"
# (workflow file, job id) of every job allowed to deploy a Worker or write a Worker secret with the production token.
PRODUCTION_JOBS = {
    ("deploy.yml", "deploy"),
    ("deploy-research-dispatcher.yml", "deploy"),
    ("test.yml", "production-learning-setup"),
}
DEPLOY_WORKFLOWS = ("deploy.yml", "deploy-research-dispatcher.yml")


def load(name):
    with open(os.path.join(WF_DIR, name)) as f:
        return yaml.safe_load(f)


def all_workflows():
    for path in sorted(glob.glob(os.path.join(WF_DIR, "*.yml"))):
        with open(path) as f:
            text = f.read()
        yield os.path.basename(path), yaml.safe_load(text), text


def job_text(job):
    return yaml.safe_dump(job, sort_keys=False)


def is_production_write(job):
    """A job that deploys a Worker or writes a Worker secret with the production Cloudflare token."""
    text = job_text(job)
    if PROD_TOKEN not in text:
        return False
    return bool(re.search(r"wrangler(@\d+)?\s+(deploy|secret\s+put)|cloudflare/wrangler-action", text))


# ------------------------------------------------------------------ inventory: no unlisted production path

def test_every_production_deploy_or_secret_job_is_listed_and_no_listed_job_is_missing():
    found = set()
    for name, wf, _ in all_workflows():
        for job_id, job in (wf.get("jobs") or {}).items():
            if is_production_write(job):
                found.add((name, job_id))
    assert found == PRODUCTION_JOBS


def test_every_production_job_needs_tests_main_and_the_production_environment():
    for name, job_id in PRODUCTION_JOBS:
        job = load(name)["jobs"][job_id]
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else needs
        assert needs == ["test", "approval-gate"], (name, job_id)
        cond = job.get("if", "")
        assert "github.ref == 'refs/heads/main'" in cond, (name, job_id)
        assert "needs.test.result == 'success'" in cond, (name, job_id)
        assert "needs.approval-gate.result == 'success'" in cond, (name, job_id)
        assert not re.search(r"\b(always|failure|cancelled)\(\)", cond), (name, job_id)   # never overrides the gate
        assert job.get("environment") == "production", (name, job_id)


def test_the_gating_test_job_is_the_full_ci_suite_in_every_production_workflow():
    for name in {n for n, _ in PRODUCTION_JOBS}:
        test_job = load(name)["jobs"]["test"]
        assert test_job.get("uses") == CI_TESTS, name
        assert "secrets" not in test_job, name                                  # no secrets passed to tests


def test_deploy_workflows_only_trigger_on_main_push_or_manual_dispatch_and_test_only_main():
    for name in DEPLOY_WORKFLOWS:
        wf = load(name)
        triggers = wf[True]
        assert set(triggers) == {"push", "workflow_dispatch"}, name           # no pull_request, workflow_call, workflow_run
        assert triggers["push"]["branches"] == ["main"], name
        assert wf["jobs"]["test"]["if"] == "github.ref == 'refs/heads/main'", name
        assert wf["permissions"] == {"contents": "read"}, name
        assert wf["concurrency"]["cancel-in-progress"] is False, name


def test_deploy_jobs_deploy_the_tested_commit():
    """The deploy job checks out github.sha (no `ref:` override) and ci-tests.yml tests the caller's github.sha."""
    for name, job_id in PRODUCTION_JOBS:
        for step in load(name)["jobs"][job_id]["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert "ref" not in (step.get("with") or {}), (name, job_id)
    for step in load("ci-tests.yml")["jobs"]["tests"]["steps"]:
        if str(step.get("uses", "")).startswith("actions/checkout"):
            assert "ref" not in (step.get("with") or {})


def test_no_workflow_calls_or_dispatches_a_deploy_workflow():
    for name, _, text in all_workflows():
        for target in DEPLOY_WORKFLOWS:
            assert not re.search(r"uses:\s*\./\.github/workflows/" + re.escape(target), text), (name, target)
            assert not re.search(r"workflow\s+run\s+" + re.escape(target), text), (name, target)
            assert not re.search(r"workflows/" + re.escape(target) + r"/dispatches", text), (name, target)


# ------------------------------------------------------------------ the reusable test workflow

def test_ci_tests_is_reusable_only_secret_free_and_read_only():
    wf = load("ci-tests.yml")
    assert set(wf[True]) == {"workflow_call"}
    assert wf["permissions"] == {"contents": "read"}
    with open(os.path.join(WF_DIR, "ci-tests.yml")) as f:
        text = f.read()
    assert "secrets." not in text
    assert "environment" not in wf["jobs"]["tests"]


def test_ci_tests_runs_the_full_javascript_and_python_suites():
    runs = [s.get("run", "") for s in load("ci-tests.yml")["jobs"]["tests"]["steps"]]
    assert "npm ci" in runs
    assert "npm test" in runs
    assert any(r.startswith("python3 -m pytest exp004-timesfm/ exp008-chronos/ research/") for r in runs)


def test_the_pr_check_runs_the_same_reusable_suite():
    assert load("test.yml")["jobs"]["test"] == {"uses": CI_TESTS}


# ------------------------------------------------------------------ simulation of GitHub's gating rules

def evaluate_if(cond, ref, test_result, gate_result="success"):
    """Evaluates the conjunctive `if:` forms used by the gated jobs. GitHub prepends success() to an `if:` with no
    status function, so the job also needs every `needs` job to have succeeded."""
    clauses = [c.strip() for c in cond.split("&&")]
    ok = True
    for c in clauses:
        m = re.fullmatch(r"github\.ref == '([^']+)'", c)
        if m:
            ok = ok and ref == m.group(1)
            continue
        m = re.fullmatch(r"needs\.test\.result == '([^']+)'", c)
        if m:
            ok = ok and test_result == m.group(1)
            continue
        m = re.fullmatch(r"needs\.approval-gate\.result == '([^']+)'", c)
        if m:
            ok = ok and gate_result == m.group(1)
            continue
        if re.fullmatch(r"\(inputs\.job == '[^']+'( \|\| inputs\.job == '[^']+')*\)", c):
            continue                                                            # trigger selector, assumed satisfied
        m = re.fullmatch(r"(github\.event_name|inputs\.job) == '([^']+)'", c)
        if m:
            continue                                                            # trigger selectors, assumed satisfied
        raise AssertionError(f"unrecognised clause in a gated if: {c!r}")
    implicit_success = test_result == "success" and (gate_result == "success" or "approval-gate" not in cond)
    return ok and implicit_success


def test_simulated_gate_allows_a_deploy_only_for_main_with_successful_tests():
    refs = ["refs/heads/main", "refs/heads/feature", "refs/heads/fix/stage7-registration-safety", "refs/pull/106/merge"]
    results = ["success", "failure", "cancelled", "skipped", None]             # None: the test job never reported
    for name, job_id in PRODUCTION_JOBS:
        cond = load(name)["jobs"][job_id]["if"]
        for ref in refs:
            for res in results:
                for gate in results:
                    allowed = evaluate_if(cond, ref, res, gate)
                    assert allowed == (ref == "refs/heads/main" and res == "success" and gate == "success"), (name, job_id, ref, res, gate)


def test_simulated_test_job_is_skipped_off_main_so_the_deploy_is_too():
    for name in DEPLOY_WORKFLOWS:
        cond = load(name)["jobs"]["test"]["if"]
        assert evaluate_if(cond, "refs/heads/feature", "success") is False
        # Skipped test job -> result 'skipped' -> the deploy's gate is false.
        assert evaluate_if(load(name)["jobs"]["deploy"]["if"], "refs/heads/feature", "skipped") is False


def test_simulator_rejects_unknown_clauses_so_a_new_condition_cannot_slip_past_this_test():
    try:
        evaluate_if("github.ref == 'refs/heads/main' || true", "refs/heads/main", "success")
    except AssertionError:
        return
    raise AssertionError("an unrecognised clause was accepted")


# ------------------------------------------------------------------ approval gate (fail-closed preflight)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("verify_production_environment", os.path.join(ROOT, ".github", "scripts", "verify_production_environment.py"))
vpe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vpe)
GATED_WORKFLOWS = ("deploy.yml", "deploy-research-dispatcher.yml", "test.yml")


def test_every_production_workflow_has_a_secret_free_approval_gate_that_runs_the_check():
    for name in GATED_WORKFLOWS:
        gate = load(name)["jobs"]["approval-gate"]
        assert gate["needs"] == "test", name
        assert "needs.test.result == 'success'" in gate["if"] and "github.ref == 'refs/heads/main'" in gate["if"], name
        assert "environment" not in gate, name
        text = job_text(gate)
        assert "secrets." not in text, name
        assert "verify_production_environment.py" in text and "--environment production --branch main" in text, name
        assert gate["permissions"] == {"contents": "read"}, name


def env_api(env=None, env_status=200, policies=None, pol_status=200):
    def get(url, token=None):
        if url.endswith("/deployment-branch-policies"):
            return pol_status, policies
        return env_status, env
    return get


GOOD_ENV = {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {"login": "owner"}}]}],
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
MAIN_ONLY = {"branch_policies": [{"name": "main", "type": "branch"}]}


def test_gate_passes_only_with_reviewers_and_a_main_only_branch_policy():
    assert vpe.check("o/r", "production", "main", get=env_api(GOOD_ENV, policies=MAIN_ONLY)) == []


def test_gate_fails_closed_for_every_missing_or_weaker_setting():
    cases = [
        env_api(None, env_status=404),                                                      # environment does not exist
        env_api(None, env_status=0),                                                        # API unreachable
        env_api({**GOOD_ENV, "protection_rules": []}, policies=MAIN_ONLY),                  # auto-created, no rules
        env_api({**GOOD_ENV, "protection_rules": [{"type": "required_reviewers", "reviewers": []}]}, policies=MAIN_ONLY),
        env_api({**GOOD_ENV, "protection_rules": [{"type": "wait_timer", "wait_timer": 5}]}, policies=MAIN_ONLY),
        env_api({**GOOD_ENV, "deployment_branch_policy": None}, policies=MAIN_ONLY),       # all branches allowed
        env_api({**GOOD_ENV, "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}}),
        env_api(GOOD_ENV, policies={"branch_policies": [{"name": "*", "type": "branch"}]}),
        env_api(GOOD_ENV, policies={"branch_policies": [{"name": "main", "type": "branch"}, {"name": "dev", "type": "branch"}]}),
        env_api(GOOD_ENV, policies={"branch_policies": [{"name": "main", "type": "tag"}]}),
        env_api(GOOD_ENV, policies=None, pol_status=403),
    ]
    for get in cases:
        assert vpe.check("o/r", "production", "main", get=get), get


def test_gate_cli_exits_non_zero_when_not_configured(monkeypatch):
    monkeypatch.setattr(vpe, "fetch", env_api(None, env_status=404))
    monkeypatch.setattr(vpe, "check", lambda repo, env, branch, get=vpe.fetch, token=None: ["missing"])
    assert vpe.main(["--repo", "o/r", "--environment", "production", "--branch", "main"]) == 1
