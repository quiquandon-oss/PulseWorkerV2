"""
Static regression checks for the Stage 7 staging-scheduler split between
main and claude/stage7-research-pipeline (see .github/workflows/stage7-
staging-dispatcher.yml's own module comment for the full rationale).

These tests only run against files present on main -- there is no
checkout of the feature branch available here. A copy of stage7-
research-pipeline.yml DOES live on main (registration-only, see that
file's own top-of-file comment); this file re-checks the same guard/
secret/target properties against THAT copy. The authoritative copy's own
full test suite (including its pure-Python config validation) lives in
stage7-research-pipeline/test_workflow_staging_guard.py and
stage7-research-pipeline/test_run_stage7.py on
claude/stage7-research-pipeline, and is NOT duplicated here.

Run with: python3 -m pytest research/ -v
"""
import os

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
DISPATCHER_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "stage7-staging-dispatcher.yml")
DEPLOY_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "deploy.yml")
WRANGLER_TOML_PATH = os.path.join(REPO_ROOT, "wrangler.toml")

PRODUCTION_WORKER_NAME = "pulseworker-v2"
PRODUCTION_DATABASE_NAME = "sentiment-history"
PRODUCTION_DATABASE_ID = "f91ca980-b886-423a-bd6f-f3baea46d181"
FEATURE_BRANCH = "claude/stage7-human-controlled-workflow"  # the reviewed Stage 7 branch (see stage7-research-pipeline/test_branch_binding.py)
STAGE7_WORKFLOW_FILENAME = "stage7-research-pipeline.yml"


def _load_dispatcher():
    with open(DISPATCHER_PATH) as f:
        return yaml.safe_load(f)


def _dispatcher_text():
    with open(DISPATCHER_PATH) as f:
        return f.read()


# =====================================================================
# Requirement #1: the dispatcher targets the exact feature branch and
# the exact Stage 7 workflow file.
# =====================================================================

def test_dispatcher_run_step_targets_the_exact_stage7_workflow_and_branch():
    text = _dispatcher_text()
    assert STAGE7_WORKFLOW_FILENAME in text
    assert f"--ref {FEATURE_BRANCH}" in text


def test_dispatcher_does_not_target_main_or_any_other_ref():
    text = _dispatcher_text()
    # Only one --ref flag, and it must be the feature branch -- never
    # main, never a variable/expression that could resolve to main.
    assert text.count("--ref") == 1
    assert "--ref main" not in text
    assert "github.ref" not in text  # never dispatches to "wherever this runs"


# =====================================================================
# Requirement #2 (revised in the PR #82 remediation): Stage 7 is
# human-controlled, so NOTHING schedules it. The former daily 11:00 UTC cron
# fired the pipeline on a different, older branch (unreviewed code ran on
# schedule), and a scheduled run cannot supply the reviewed SHA the pipeline
# now requires. The dispatcher is manual-only and forwards a human-supplied
# expected_sha.
# =====================================================================

def test_dispatcher_has_no_schedule_and_is_manual_dispatch_only():
    triggers = _load_dispatcher()[True]  # PyYAML parses `on:` as key True
    assert "schedule" not in triggers
    assert set(triggers) == {"workflow_dispatch"}


def test_dispatcher_requires_and_forwards_the_reviewed_sha():
    triggers = _load_dispatcher()[True]
    assert triggers["workflow_dispatch"]["inputs"]["expected_sha"]["required"] is True
    assert 'expected_sha="$EXPECTED_SHA"' in _dispatcher_text()


def test_no_workflow_defines_a_stage7_schedule():
    # The former cron string must not exist in ANY workflow, so no scheduled trigger can fire Stage 7.
    workflows_dir = os.path.join(REPO_ROOT, ".github", "workflows")
    hits = []
    for name in os.listdir(workflows_dir):
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(workflows_dir, name)) as f:
            workflow = yaml.safe_load(f)
        triggers = workflow.get(True, {}) or {}
        for entry in (triggers.get("schedule") or []) if isinstance(triggers, dict) else []:
            if entry.get("cron") == "0 11 * * *" and "stage7" in name:
                hits.append(name)
    assert hits == []


# =====================================================================
# Requirement #4: the dispatcher contains no Cloudflare credentials and
# performs no Cloudflare API / D1 operation.
# =====================================================================

def test_dispatcher_references_no_cloudflare_secret():
    # Checked against the PARSED structure (comments stripped), not the
    # raw file text -- the file's own explanatory comments name these
    # secrets deliberately (to say they must never appear), which would
    # otherwise false-positive a plain substring search.
    structure_text = yaml.dump(_load_dispatcher())
    for forbidden in ("CLOUDFLARE_API_TOKEN", "STAGE7_STAGING_CLOUDFLARE_API_TOKEN", "GEMINI_API_KEY"):
        assert forbidden not in structure_text


def test_dispatcher_performs_no_cloudflare_or_d1_operation():
    structure_text = yaml.dump(_load_dispatcher()).lower()
    for forbidden in ("wrangler", "cloudflare.com", "d1 execute", "d1/database"):
        assert forbidden not in structure_text


def test_dispatcher_uses_only_the_narrow_actions_write_permission():
    workflow = _load_dispatcher()
    assert workflow["permissions"] == {"actions": "write"}


def test_dispatcher_secrets_block_only_ever_references_github_token():
    workflow = _load_dispatcher()
    steps = workflow["jobs"]["dispatch-stage7"]["steps"]
    for step in steps:
        env = step.get("env", {})
        for value in env.values():
            assert "secrets." in value or "secrets" not in value.lower()
            if "secrets." in value:
                assert value == "${{ secrets.GITHUB_TOKEN }}"


# =====================================================================
# Requirement #6: production wrangler.toml, the production deploy
# workflow, and production D1 configuration are unchanged by this task.
# =====================================================================

def test_wrangler_toml_still_targets_the_real_production_worker_and_d1():
    with open(WRANGLER_TOML_PATH) as f:
        text = f.read()
    assert f'name = "{PRODUCTION_WORKER_NAME}"' in text
    assert f'database_name = "{PRODUCTION_DATABASE_NAME}"' in text
    assert f'database_id = "{PRODUCTION_DATABASE_ID}"' in text
    # No staging identifiers leaked into the production config.
    assert "pulseworker-v2-staging" not in text
    assert "5458d504-2778-49ae-bd25-7751f1c49d50" not in text


def test_deploy_workflow_still_only_deploys_on_push_to_main_with_the_production_token():
    with open(DEPLOY_PATH) as f:
        deploy = yaml.safe_load(f)
    triggers = deploy[True]
    assert triggers["push"]["branches"] == ["main"]
    deploy_step = deploy["jobs"]["deploy"]["steps"][1]
    assert deploy_step["with"]["apiToken"] == "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    # The dedicated Stage 7 staging secret must never appear in the
    # production deploy workflow.
    with open(DEPLOY_PATH) as f:
        assert "STAGE7_STAGING_CLOUDFLARE_API_TOKEN" not in f.read()


# =====================================================================
# A copy of stage7-research-pipeline.yml DOES exist on main -- REQUIRED
# for GitHub to recognize it as a dispatchable workflow at all (GitHub
# only lists/dispatches-by-filename a workflow that exists on the
# default branch or has already run at least once; confirmed via a
# direct 404 on GET /actions/workflows/stage7-research-pipeline.yml
# before this copy was added). It must never diverge from the feature
# branch's own copy in the properties that keep it safe to have on main
# at all: reject everything but the exact feature-branch ref, carry no
# schedule: of its own, and never use the general-purpose production
# token. The feature branch's own test_workflow_staging_guard.py checks
# the SAME properties against its own (authoritative) copy -- this is
# not a substitute for that, it's the same invariant re-checked against
# whatever main happens to carry, since main's copy is edited far less
# often and is easy to forget to keep in sync.
# =====================================================================

def _stage7_workflow_path_on_main():
    return os.path.join(REPO_ROOT, ".github", "workflows", STAGE7_WORKFLOW_FILENAME)


def _load_stage7_workflow_on_main():
    with open(_stage7_workflow_path_on_main()) as f:
        return yaml.safe_load(f)


def test_stage7_workflow_copy_exists_on_main_for_registration():
    assert os.path.exists(_stage7_workflow_path_on_main())


def test_stage7_workflow_copy_on_main_still_has_no_schedule_trigger():
    triggers = _load_stage7_workflow_on_main()[True]
    assert "schedule" not in triggers
    assert set(triggers.keys()) == {"workflow_dispatch"}


def test_stage7_workflow_copy_on_main_still_rejects_every_ref_but_the_feature_branch():
    workflow = _load_stage7_workflow_on_main()
    first_step = workflow["jobs"]["research"]["steps"][0]
    run_text = first_step.get("run", "")
    assert f'!= "refs/heads/{FEATURE_BRANCH}"' in run_text
    assert "exit 1" in run_text


def test_stage7_workflow_copy_on_main_branch_guard_precedes_checkout():
    workflow = _load_stage7_workflow_on_main()
    steps = workflow["jobs"]["research"]["steps"]
    guard_index = next(i for i, s in enumerate(steps) if f"refs/heads/{FEATURE_BRANCH}" in s.get("run", ""))
    checkout_index = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/checkout"))
    assert guard_index < checkout_index


def test_stage7_workflow_copy_on_main_still_uses_the_dedicated_staging_secret():
    workflow = _load_stage7_workflow_on_main()
    steps = workflow["jobs"]["research"]["steps"]
    run_step = next(s for s in steps if "Run Stage 7" in s.get("name", ""))
    env = run_step.get("env", {})
    assert env.get("CLOUDFLARE_API_TOKEN") == "${{ secrets.STAGE7_STAGING_CLOUDFLARE_API_TOKEN }}"
    assert "${{ secrets.CLOUDFLARE_API_TOKEN }}" not in yaml.dump(run_step)


def test_stage7_workflow_copy_on_main_still_targets_the_expected_staging_database():
    workflow = _load_stage7_workflow_on_main()
    steps = workflow["jobs"]["research"]["steps"]
    run_step = next(s for s in steps if "Run Stage 7" in s.get("name", ""))
    env = run_step.get("env", {})
    assert env.get("STAGE7_TARGET_DATABASE_NAME") == "pulseworker-v2-staging"
    assert env.get("STAGE7_TARGET_DATABASE_ID") == "5458d504-2778-49ae-bd25-7751f1c49d50"
    assert env.get("STAGE7_TARGET_DATABASE_NAME") != PRODUCTION_DATABASE_NAME
    assert env.get("STAGE7_TARGET_DATABASE_ID") != PRODUCTION_DATABASE_ID
