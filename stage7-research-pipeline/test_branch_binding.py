"""
One traceable relationship between reviewed Stage 7 code and the code that runs/deploys against staging.

Background (found while remediating PR #82): the pipeline workflow, the staging deploy and the daily dispatcher were
bound to different branches/SHAs. The dispatcher (on main) fired the pipeline daily on the OLD
claude/stage7-research-pipeline branch, the pipeline workflow's guard accepted only that old branch, and the
request publisher refused to publish from anywhere but that old branch -- so on the reviewed branch the
publish step could never succeed (FAILED_RETRYABLE -> FAILED_PERMANENT after 5 attempts) while unreviewed code
ran on schedule.

These tests pin: ONE reviewed branch literal across publisher + workflows, a human-supplied SHA that is verified,
no schedule, no Cloudflare secret in the dispatcher, and that the staging deploy bakes the reviewed SHA in.

Run with: python3 -m pytest stage7-research-pipeline/ -v
"""
import os
import re
import sys

import yaml

HERE = os.path.dirname(__file__)
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "research"))
import stage7_github_publisher as pub  # noqa: E402

REVIEWED_BRANCH = "claude/stage7-human-controlled-workflow"
REVIEWED_REF = f"refs/heads/{REVIEWED_BRANCH}"
OLD_BRANCH = "claude/stage7-research-pipeline"

WORKFLOWS = os.path.join(REPO, ".github", "workflows")


def _wf(name):
    with open(os.path.join(WORKFLOWS, name)) as f:
        return yaml.safe_load(f)


def _executable_text(name):
    """Everything that actually executes or configures: run scripts, `with`/`env` values and `uses` -- never comments."""
    wf = _wf(name)
    chunks = []
    for job in wf["jobs"].values():
        for step in job["steps"]:
            chunks.append(step.get("run", ""))
            chunks.append(yaml.dump(step.get("with", {})))
            chunks.append(yaml.dump(step.get("env", {})))
    return "\n".join(chunks)


# ---------------------------------------------------------------- publisher


class _R:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def _request():
    return {
        "request_id": "stage7-req-1-1", "event_id": 1, "created_ts": 1000, "historical_cutoff_ts": 999,
        "sufficiency_status": "INSUFFICIENT_EVIDENCE", "reasons": ["no evidence rows exist"],
        "questions": ["what happened?"], "missing_categories": ["primary_reporting"],
        "event": {"event_id": 1, "coin": "BTC", "category": "LARGE_MOVE", "event_ts": 950},
        "evidence_snapshot": [], "prompt_text": "stored prompt",
    }


def test_publisher_publishes_from_the_reviewed_branch(tmp_path):
    """Reproduction: with the old constant this returned published=False ('not the allowed publish branch')."""
    calls = []

    def run(args, cwd):
        calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return _R(0, REVIEWED_BRANCH + "\n")
        return _R(0)

    result = pub.publish_request_file(_request(), str(tmp_path), run=run)
    assert result["error"] is None, result
    assert result["published"] is True
    assert ["git", "push"] in calls


def test_publisher_still_refuses_the_old_branch_main_and_detached_head(tmp_path):
    for branch in (OLD_BRANCH, "main", "master", "HEAD"):
        def run(args, cwd, branch=branch):
            return _R(0, branch + "\n") if args[:2] == ["git", "rev-parse"] else _R(0)
        result = pub.publish_request_file(_request(), str(tmp_path), run=run)
        assert result["published"] is False, branch
        assert "refusing to publish" in result["error"]


def test_the_publisher_constant_is_the_reviewed_branch():
    assert pub.ALLOWED_PUBLISH_BRANCH == REVIEWED_BRANCH
    assert "main" in pub.FORBIDDEN_PUBLISH_BRANCHES


# ---------------------------------------------------------------- workflows


def test_pipeline_workflow_guard_accepts_only_the_reviewed_ref_and_a_well_formed_sha():
    steps = _wf("stage7-research-pipeline.yml")["jobs"]["research"]["steps"]
    guard = steps[0]
    assert guard["name"] == "Enforce staging-only branch"
    assert f'!= "{REVIEWED_REF}"' in guard["run"]
    assert OLD_BRANCH not in guard["run"]
    assert "[0-9a-f]{40}" in guard["run"]
    assert guard["env"]["DISPATCH_REF"] == "${{ github.ref }}"  # via env, never interpolated into the script
    assert "${{" not in guard["run"]


def test_pipeline_workflow_verifies_the_checked_out_head_against_expected_sha_before_any_cloudflare_access():
    steps = _wf("stage7-research-pipeline.yml")["jobs"]["research"]["steps"]
    names = [s.get("name", "") for s in steps]
    checkout = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/checkout"))
    verify = next(i for i, n in enumerate(names) if n.startswith("Verify the head is the reviewed commit"))
    run_stage7 = next(i for i, n in enumerate(names) if n.startswith("Run Stage 7"))
    assert steps[checkout]["with"]["fetch-depth"] == 0
    assert checkout < verify < run_stage7
    script = steps[verify]["run"]
    assert "git merge-base --is-ancestor" in script
    assert "grep -v '^research/stage7_requests/'" in script  # only publisher data may follow the reviewed commit
    assert steps[verify]["env"]["EXPECTED_SHA"] == "${{ inputs.expected_sha }}"
    # The step that touches Cloudflare comes strictly after the verification.
    cloud_steps = [i for i, s in enumerate(steps) if "CLOUDFLARE_API_TOKEN" in yaml.dump(s.get("env", {}))
                   and "-z" not in s.get("run", "")]
    assert cloud_steps and min(cloud_steps) > verify


def test_pipeline_workflow_requires_expected_sha_input_and_has_no_schedule():
    triggers = _wf("stage7-research-pipeline.yml")[True]
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["expected_sha"]["required"] is True


def test_staging_deploy_enforces_branch_sha_and_bakes_the_reviewed_sha_into_the_worker():
    wf = _wf("deploy-stage7-staging.yml")
    steps = wf["jobs"]["deploy-staging"]["steps"]
    triggers = wf[True]
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["expected_sha"]["required"] is True
    assert f'!= "{REVIEWED_REF}"' in steps[0]["run"]
    names = [s.get("name", "") for s in steps]
    checkout = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/checkout"))
    verify = next(i for i, n in enumerate(names) if n.startswith("Verify the head is the reviewed commit"))
    deploy = names.index("Deploy to staging")
    assert 0 < checkout < verify < deploy
    assert steps[checkout]["with"] == {"ref": "${{ github.sha }}", "fetch-depth": 0}  # a SHA, never a moving ref
    assert "git merge-base --is-ancestor" in steps[verify]["run"]
    assert "--var GIT_COMMIT_SHA:$REVIEWED_SHA" in steps[deploy]["run"]
    assert steps[deploy]["env"]["REVIEWED_SHA"] == "${{ inputs.expected_sha }}"
    assert "wrangler deploy -c wrangler.staging.toml" in steps[deploy]["run"]
    # No stale hard-coded checkout SHA remains.
    assert not re.search(r"ref:\s*[0-9a-f]{40}", open(os.path.join(WORKFLOWS, "deploy-stage7-staging.yml")).read())


def test_staging_deploy_refuses_a_production_stage7_flag():
    script = "\n".join(s.get("run", "") for s in _wf("deploy-stage7-staging.yml")["jobs"]["deploy-staging"]["steps"])
    assert "grep -qx 'STAGE7_ENABLED = \"true\"' wrangler.staging.toml" in script
    assert "grep -q 'STAGE7_ENABLED' wrangler.toml" in script


def test_dispatcher_is_manual_only_targets_the_reviewed_branch_and_forwards_the_sha():
    wf = _wf("stage7-staging-dispatcher.yml")
    triggers = wf[True]
    assert set(triggers) == {"workflow_dispatch"}, "the daily cron fired unreviewed code; it must stay removed"
    assert triggers["workflow_dispatch"]["inputs"]["expected_sha"]["required"] is True
    text = _executable_text("stage7-staging-dispatcher.yml")
    assert f"--ref {REVIEWED_BRANCH}" in text
    assert OLD_BRANCH not in text
    assert 'expected_sha="$EXPECTED_SHA"' in text


def test_no_executable_workflow_content_or_code_still_binds_to_the_old_branch():
    for name in ("stage7-research-pipeline.yml", "deploy-stage7-staging.yml", "stage7-staging-dispatcher.yml"):
        assert OLD_BRANCH not in _executable_text(name), name
    with open(os.path.join(REPO, "research", "stage7_github_publisher.py")) as f:
        code = [line for line in f if not line.lstrip().startswith("#")]
    assert OLD_BRANCH not in "".join(code)


def test_reviewed_branch_literal_is_identical_everywhere_it_is_used():
    seen = set()
    for name in ("stage7-research-pipeline.yml", "deploy-stage7-staging.yml", "stage7-staging-dispatcher.yml"):
        seen.update(re.findall(r"claude/stage7-[a-z-]+", _executable_text(name)))
    seen.add(pub.ALLOWED_PUBLISH_BRANCH)
    assert seen == {REVIEWED_BRANCH}
