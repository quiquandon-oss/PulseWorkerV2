"""Static deployment-isolation checks for the Risk Regime forward-collection workflows (no network)."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
VERIFY = WF / "research-forward-collection.yml"
STUB = ROOT / "research" / "scheduler" / "research-forward-schedule.yml"
DATA_BRANCH = "research-data/risk-regime-forward"
FORBIDDEN_TRIGGERS = {"workflow_dispatch", "workflow_run", "repository_dispatch", "pull_request", "pull_request_target",
                      "workflow_call", "release", "deployment", "deployment_status"}


def load(p):
    doc = yaml.safe_load(p.read_text())
    doc["on"] = doc.get("on", doc.get(True))           # PyYAML reads the bare key `on` as True
    return doc


def text_without_comments(p):
    return "\n".join(line.split("#", 1)[0] for line in p.read_text().splitlines())


def common_checks(p):
    wf = load(p)
    assert wf["permissions"] == {"contents": "write"}                    # least privilege, nothing else
    body = text_without_comments(p)
    assert "secrets." not in body and "wrangler" not in body and "CLOUDFLARE" not in body
    steps = wf["jobs"]["collect"]["steps"]
    code = [s for s in steps if s.get("with", {}).get("path") == "code"][0]
    data = [s for s in steps if s.get("with", {}).get("path") == "data"][0]
    assert code["with"]["persist-credentials"] is False                  # the code checkout cannot push
    assert data["with"]["ref"] == DATA_BRANCH
    assert all(re.fullmatch(r"actions/checkout@[0-9a-f]{40}", s["uses"]) for s in steps if "uses" in s)   # pinned actions
    run = "\n".join(s.get("run", "") for s in steps)
    pushes = re.findall(r"git push[^\n]*", run)
    assert pushes and all('"HEAD:refs/heads/${TARGET}"' in x for x in pushes)
    commit = [s for s in steps if s.get("env", {}).get("TARGET")][0]
    assert commit["env"]["TARGET"] == DATA_BRANCH and commit["working-directory"] == "data"
    assert "grep -v '^risk_regime_forward/'" in run                       # refuses anything outside the data dir
    assert "main" not in re.sub(r"#.*", "", run)
    assert "gh workflow" not in run and "gh api" not in run and "curl" not in run      # no chaining / dispatch
    return wf


def test_verification_workflow_is_push_only_on_the_research_branch():
    wf = common_checks(VERIFY)
    assert set(wf["on"]) == {"push"} and not set(wf["on"]) & FORBIDDEN_TRIGGERS
    assert wf["on"]["push"]["branches"] == ["claude/sweet-meitner-66ntx8"]
    assert set(wf["on"]["push"]["paths"]) == {".github/workflows/research-forward-collection.yml", "research/risk_regime_forward.py"}


def test_scheduler_stub_is_schedule_only_daily_1040_utc_and_not_installed():
    wf = common_checks(STUB)
    assert set(wf["on"]) == {"schedule"} and wf["on"]["schedule"] == [{"cron": "40 10 * * *"}]
    assert not (WF / "research-forward-schedule.yml").exists()          # inert until a human installs it on main
    assert "RESEARCH_CODE_SHA" in STUB.read_text()                      # must be replaced by a reviewed, pinned SHA


def test_deploy_cannot_be_reached_from_these_workflows():
    dep = load(WF / "deploy.yml")
    assert set(dep["on"]) == {"push", "workflow_dispatch"}
    assert dep["on"]["push"]["branches"] == ["main"]
    paths = dep["on"]["push"]["paths"]
    assert not any(p.startswith(".github/workflows/research-") or p.startswith("research/") for p in paths)
    assert ".github/workflows/research-forward-schedule.yml" not in paths
    for p in WF.glob("*.yml"):                                           # nothing listens for other workflows' completion
        assert "workflow_run" not in (load(p)["on"] or {}), p.name
