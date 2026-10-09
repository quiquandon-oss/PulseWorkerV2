"""Static checks for the T-A1 / T-A2 progress-run workflow stub (no network)."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
STUB = ROOT / "research" / "scheduler" / "v1-prospective-progress.yml"
DATA_BRANCH = "research-data/v1-prospective"
FORBIDDEN_TRIGGERS = {"workflow_dispatch", "workflow_run", "repository_dispatch", "pull_request", "pull_request_target",
                      "workflow_call", "push", "release", "deployment", "deployment_status"}


def load(p):
    doc = yaml.safe_load(p.read_text())
    doc["on"] = doc.get("on", doc.get(True))
    return doc


def steps():
    return load(STUB)["jobs"]["progress"]["steps"]


def no_comments(text):
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_stub_is_inert_schedule_only_daily_after_forward_collection():
    wf = load(STUB)
    assert set(wf["on"]) == {"schedule"} and not set(wf["on"]) & FORBIDDEN_TRIGGERS
    assert wf["on"]["schedule"] == [{"cron": "40 11 * * *"}]
    assert not (WF / "v1-prospective-progress.yml").exists()                      # not installed
    assert "V1_PROSPECTIVE_CODE_SHA" in STUB.read_text()                         # needs a reviewed, pinned SHA
    fwd = WF / "research-forward-schedule.yml"
    if fwd.exists():                                                              # runs one hour after it
        assert load(fwd)["on"]["schedule"] == [{"cron": "40 10 * * *"}]


def test_least_privilege_and_only_the_read_only_secret():
    wf = load(STUB)
    assert wf["permissions"] == {"contents": "write"}
    body = no_comments(STUB.read_text())
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", body)) == {"RESEARCH_D1_READONLY_TOKEN"}
    assert "CLOUDFLARE_API_TOKEN" not in body and "wrangler" not in body and "STAGE7" not in body
    st = steps()
    with_secret = [s for s in st if "secrets." in str(s.get("env", {}))]
    assert len(with_secret) == 1 and "v1_prospective_extract.mjs" in with_secret[0]["run"]
    assert "no fallback" in with_secret[0]["run"]
    with_token = [s for s in st if "github.token" in str(s.get("env", {}))]
    assert len(with_token) == 1 and with_token[0]["env"]["PUSH_TOKEN"] == "${{ github.token }}"
    assert "github.token" not in "".join(str(s) for s in st if s is not with_token[0])
    assert with_secret[0] is not with_token[0]


def test_checkouts_keep_no_credentials_and_actions_are_pinned():
    st = steps()
    code = [s for s in st if s.get("with", {}).get("path") == "code"][0]
    data = [s for s in st if s.get("with", {}).get("path") == "data"][0]
    assert code["with"]["persist-credentials"] is False and data["with"]["persist-credentials"] is False
    assert data["with"]["ref"] == DATA_BRANCH
    assert all(re.fullmatch(r"actions/checkout@[0-9a-f]{40}", s["uses"]) for s in st if "uses" in s)


def test_progress_mode_only_and_never_a_look():
    run = "\n".join(s.get("run", "") for s in steps())
    assert "--mode progress" in run
    assert "--mode look" not in run and "--candidate" not in run
    evals = [s for s in steps() if "v1_prospective_eval.mjs" in s.get("run", "")]   # one parse per test
    assert len(evals) == 1 and "env" not in evals[0]                              # no credential in the evaluator step
    assert "--previous" in evals[0]["run"]                                        # every earlier record is compared


def test_single_append_only_push_to_the_data_branch():
    st = steps()
    run = "\n".join(s.get("run", "") for s in st)
    pushes = re.findall(r"git push[^\n]*", run)
    assert len(pushes) == 1 and '"HEAD:refs/heads/${TARGET}"' in pushes[0]
    commit = [s for s in st if s.get("env", {}).get("TARGET")][0]
    assert commit["env"]["TARGET"] == DATA_BRANCH and commit["working-directory"] == "data"
    assert commit.get("if") == "success()"                                        # a failed check writes nothing
    assert "grep -v '^v1_prospective/runs/'" in commit["run"]
    assert "grep -v '^A'" in commit["run"]                                        # added files only
    assert "already exists; records are append-only" in run
    assert "main" not in re.sub(r"#.*", "", run)
    assert "gh workflow" not in run and "gh api" not in run and "curl" not in run


def test_deploy_cannot_be_reached():
    dep = load(WF / "deploy.yml")
    paths = dep["on"]["push"]["paths"]
    assert not any(p.startswith("research/") for p in paths)
    assert ".github/workflows/v1-prospective-progress.yml" not in paths
