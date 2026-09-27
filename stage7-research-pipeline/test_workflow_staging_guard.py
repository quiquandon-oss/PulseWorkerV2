"""
Regression coverage for the Copilot-audit remediation, requirement #7:
"unauthorized branch/ref is rejected before Cloudflare access or
publishing." GitHub Actions itself cannot be unit-tested (no local runner
here, and this task's own scope explicitly forbids running the workflow),
so this asserts directly against the parsed YAML: the branch guard is the
very first step in the job (before checkout, tests, or the Stage 7 run
step), it checks github.ref for exact equality against
refs/heads/claude/stage7-research-pipeline, the job uses the dedicated
STAGE7_STAGING_CLOUDFLARE_API_TOKEN secret (never the general
CLOUDFLARE_API_TOKEN production deploy.yml uses), and a missing/empty
token is checked and fails closed.

Run with: python3 -m pytest stage7-research-pipeline/ -v
"""
import os

import yaml

WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "stage7-research-pipeline.yml"
)


def _load_workflow():
    with open(WORKFLOW_PATH) as f:
        return yaml.safe_load(f)


def _steps():
    return _load_workflow()["jobs"]["research"]["steps"]


def test_branch_guard_is_the_first_step_in_the_job():
    steps = _steps()
    assert len(steps) > 0
    first_step_run = steps[0].get("run", "")
    assert "github.ref" in first_step_run
    assert "refs/heads/claude/stage7-research-pipeline" in first_step_run
    assert "exit 1" in first_step_run


def test_branch_guard_checks_exact_equality_not_a_prefix_match():
    steps = _steps()
    first_step_run = steps[0].get("run", "")
    # Exact-equality shell comparison ("!=" against the literal ref) --
    # never a substring/prefix test that a differently-named branch
    # (e.g. claude/stage7-research-pipeline-old) could slip through.
    assert '!= "refs/heads/claude/stage7-research-pipeline"' in first_step_run


def test_branch_guard_precedes_checkout_and_every_other_step():
    steps = _steps()
    step_names = [s.get("name", "") for s in steps]
    guard_index = next(i for i, s in enumerate(steps) if "refs/heads/claude/stage7-research-pipeline" in s.get("run", ""))
    checkout_index = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/checkout"))
    run_stage7_index = next(i for i, name in enumerate(step_names) if "Run Stage 7" in name)
    assert guard_index < checkout_index < run_stage7_index


def test_job_uses_dedicated_staging_secret_not_general_production_token():
    workflow_text = open(WORKFLOW_PATH).read()
    assert "STAGE7_STAGING_CLOUDFLARE_API_TOKEN" in workflow_text
    steps = _steps()
    run_stage7_step = next(s for s in steps if "Run Stage 7" in s.get("name", ""))
    env = run_stage7_step.get("env", {})
    assert env.get("CLOUDFLARE_API_TOKEN") == "${{ secrets.STAGE7_STAGING_CLOUDFLARE_API_TOKEN }}"
    # Never the general-purpose production secret deploy.yml uses.
    assert "${{ secrets.CLOUDFLARE_API_TOKEN }}" not in yaml.dump(run_stage7_step)


def test_job_passes_explicit_staging_target_to_the_runner():
    steps = _steps()
    run_stage7_step = next(s for s in steps if "Run Stage 7" in s.get("name", ""))
    env = run_stage7_step.get("env", {})
    assert env.get("STAGE7_TARGET_ACCOUNT_ID") == "f58e761fbc8e62dc404d8684290af264"
    assert env.get("STAGE7_TARGET_DATABASE_NAME") == "pulseworker-v2-staging"
    assert env.get("STAGE7_TARGET_DATABASE_ID") == "5458d504-2778-49ae-bd25-7751f1c49d50"


def test_missing_staging_token_check_runs_before_checkout_and_fails_closed():
    steps = _steps()
    checkout_index = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/checkout"))
    token_check_index = next(
        i for i, s in enumerate(steps)
        if "STAGE7_STAGING_CLOUDFLARE_API_TOKEN" in s.get("env", {}) and "-z" in s.get("run", "")
    )
    assert token_check_index < checkout_index
    token_check_step = steps[token_check_index]
    assert "exit 1" in token_check_step["run"]


def test_workflow_is_not_triggered_by_pushes():
    # This workflow's on: block must never include a `push` trigger at
    # all (unlike deploy.yml, which deploys on push to main) -- only
    # schedule/workflow_dispatch. The branch guard is defense in depth;
    # not having a push trigger in the first place is the primary
    # control. PyYAML parses the YAML 1.1 boolean key `on:` as `True`.
    triggers = _load_workflow()[True]
    assert "push" not in triggers
    assert set(triggers.keys()) == {"schedule", "workflow_dispatch"}
