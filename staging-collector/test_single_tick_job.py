"""Guards for test.yml's `staging-single-tick` job: the one job allowed to write to staging (one real collector tick)."""
import os
import re

import pytest

yaml = pytest.importorskip("yaml")

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_YML = os.path.join(HERE, "..", ".github", "workflows", "test.yml")
REVIEWED_SHA = "ddf48cad109f306fe76f2c701ce8736369247f92"
CRYPTOPULSE = "0a1dfb8ce88883336ee2e712a84e49be857b1724"
SECRET = "STAGING_COLLECTOR_CLOUDFLARE_API_TOKEN"
STAGING = {"STAGING_COLLECTOR_ACCOUNT_ID": "f58e761fbc8e62dc404d8684290af264",
           "STAGING_COLLECTOR_DATABASE_NAME": "pulseworker-v2-staging",
           "STAGING_COLLECTOR_DATABASE_ID": "5458d504-2778-49ae-bd25-7751f1c49d50"}
COLLECTOR_CALL = "python3 reviewed/staging-collector/collector.py"
SEQUENCE = ["collector.py open-period", "collector.py btc-tick", "history_harness.py", "collector.py history-ingest",
            "collector.py verify", "collector.py status"]


def _load():
    with open(TEST_YML) as f:
        text = f.read()
    wf = yaml.safe_load(text)
    return text, wf, wf["jobs"]["staging-single-tick"]


def _runs(job):
    return [s.get("run", "") for s in job["steps"]]


def test_workflow_has_no_schedule_and_the_tick_runs_only_on_an_explicit_manual_choice():
    _, wf, job = _load()
    triggers = wf[True]
    assert "schedule" not in triggers and set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["job"]["default"] == "g1-live-validation"
    assert job["if"] == "github.event_name == 'workflow_dispatch' && inputs.job == 'staging-single-tick'"
    assert job["permissions"] == {"contents": "read"}
    assert job["environment"] == "staging-collection"


def test_only_this_job_and_only_its_collector_steps_consume_the_secret():
    text, wf, job = _load()
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text)) == {SECRET}
    for name, other in wf["jobs"].items():
        if name != "staging-single-tick":
            assert "secrets." not in yaml.safe_dump(other), name
    assert "secrets." not in yaml.safe_dump(job.get("env", {}))
    for step in job["steps"]:
        uses_secret = "secrets." in yaml.safe_dump(step)
        run = step.get("run", "")
        if uses_secret:
            assert set(step["env"]) == {SECRET}
            assert COLLECTOR_CALL in run or step is job["steps"][0], step["name"]
            assert "echo \"$" + SECRET not in run and "${{ secrets" not in run, "the secret is never echoed"
        elif COLLECTOR_CALL in run:
            pytest.fail(f"collector step without the secret: {step['name']}")
    first = job["steps"][0]["run"]
    assert '[ -z "$' + SECRET + '" ]' in first and "exit 1" in first


def test_target_is_exactly_the_staging_database():
    _, _, job = _load()
    for key, value in STAGING.items():
        assert job["env"][key] == value
    assert job["env"]["STAGING_COLLECTOR_GIT_SHA"] == REVIEWED_SHA


def test_reviewed_collector_and_page_pins_cannot_be_removed():
    text, _, job = _load()
    refs = {s["with"]["ref"] for s in job["steps"] if s.get("uses", "").startswith("actions/checkout")}
    assert refs == {REVIEWED_SHA, CRYPTOPULSE}
    assert all(s["with"].get("persist-credentials") is False for s in job["steps"]
               if s.get("uses", "").startswith("actions/checkout"))
    verify = next(r for r in _runs(job) if "sha256sum -c" in r)
    for name in ("collector.py", "history_harness.py", "harness_policy.py", "relay_shims.py"):
        assert re.search(rf"^\s*[0-9a-f]{{64}}  {name}$", verify, re.M), name
    assert 'rev-parse HEAD)" = "$REVIEWED_SHA"' in verify


def test_execution_path_is_exactly_the_reviewed_sequence():
    _, _, job = _load()
    calls = [line.strip() for r in _runs(job) for line in r.splitlines() if line.strip().startswith("python3 ")]
    calls = [c for c in calls if "pytest" not in c and "playwright" not in c]
    assert all(c.startswith("python3 reviewed/staging-collector/") for c in calls), calls
    assert len(calls) == len(SEQUENCE)
    for call, expected in zip(calls, SEQUENCE):
        assert expected in call, (call, expected)
    joined = "\n".join(_runs(job))
    for banned in ("--dry-run", "python3 -c", "curl", "wget", "wrangler", "close-period", "run_stage7",
                   "run_staging", "experiment5", "gh workflow", "migration"):
        assert banned not in joined, banned
    tests_idx = next(i for i, r in enumerate(_runs(job)) if "pytest reviewed/staging-collector/" in r)
    first_write = next(i for i, r in enumerate(_runs(job)) if "open-period" in r)
    assert tests_idx < first_write


def test_production_sinkholes_cannot_be_removed_and_precede_any_collector_call():
    _, _, job = _load()
    runs = _runs(job)
    lock = next(i for i, r in enumerate(runs) if "/etc/hosts" in r)
    sinkholed = runs[lock].split("sudo tee -a /etc/hosts")[0]
    for host in ("sentiment-ff75.quiquandon.workers.dev", "pulseworker-v2.quiquandon.workers.dev",
                 "pulseworker-v2-staging.quiquandon.workers.dev", "script.google.com"):
        assert host in sinkholed, host
    assert "api.cloudflare.com" not in sinkholed  # the collector's own allowlist limits it to the staging path
    assert lock < next(i for i, r in enumerate(runs) if COLLECTOR_CALL in r)
