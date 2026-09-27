"""
Static regression checks for the Stage 7 staging-scheduler split between
main and claude/stage7-research-pipeline (see .github/workflows/stage7-
staging-dispatcher.yml's own module comment for the full rationale).

These tests only run against files present on main -- there is no
checkout of the feature branch available here. The complementary checks
for the Stage 7 workflow itself (branch guard, validate_staging_target,
dedicated staging secret, no schedule: trigger there) live in
stage7-research-pipeline/test_workflow_staging_guard.py on
claude/stage7-research-pipeline, and are NOT duplicated here.

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
FEATURE_BRANCH = "claude/stage7-research-pipeline"
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
# Requirement #2: the daily schedule is defined ONLY in the dispatcher
# on main (the Stage 7 workflow's own schedule: removal is verified on
# the feature branch itself, in test_workflow_staging_guard.py).
# =====================================================================

def test_dispatcher_has_the_daily_11_00_utc_schedule():
    triggers = _load_dispatcher()[True]  # PyYAML parses `on:` as key True
    assert triggers["schedule"] == [{"cron": "0 11 * * *"}]


def test_dispatcher_also_allows_manual_dispatch_for_testing():
    triggers = _load_dispatcher()[True]
    assert "workflow_dispatch" in triggers


def test_no_other_workflow_on_main_defines_the_stage7_daily_schedule():
    # Belt-and-braces: confirm this specific cron string doesn't also
    # appear in some other main-branch workflow (which would mean the
    # daily trigger is defined twice, not "only in the dispatcher").
    workflows_dir = os.path.join(REPO_ROOT, ".github", "workflows")
    hits = []
    for name in os.listdir(workflows_dir):
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(workflows_dir, name)) as f:
            if "0 11 * * *" in f.read():
                hits.append(name)
    assert hits == ["stage7-staging-dispatcher.yml"]


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


def test_stage7_workflow_file_itself_is_not_present_on_main():
    # The Stage 7 workflow's actual logic (branch guard, staging-target
    # validation, dedicated secret) is owned entirely by
    # claude/stage7-research-pipeline. Main only ever gets this thin
    # dispatcher -- never a copy of the Stage 7 workflow's own logic.
    stage7_path = os.path.join(REPO_ROOT, ".github", "workflows", STAGE7_WORKFLOW_FILENAME)
    assert not os.path.exists(stage7_path)
