"""Guards for .github/workflows/staging-collector.yml: the recurring STAGING-ONLY collector."""
import os
import re

import pytest

import collector as c

yaml = pytest.importorskip("yaml")

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "..", ".github", "workflows", "staging-collector.yml")
SECRET = "STAGING_COLLECTOR_CLOUDFLARE_API_TOKEN"
REVIEWED = "ddf48cad109f306fe76f2c701ce8736369247f92"
COLLECTOR = "python3 reviewed/staging-collector/collector.py"
HARNESS = "python3 reviewed/staging-collector/history_harness.py"


def _load():
    with open(PATH) as f:
        text = f.read()
    wf = yaml.safe_load(text)
    return text, wf, wf["jobs"]["collect"]


def _code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_name_schedule_is_hourly_and_manual_dispatch_is_constrained():
    _, wf, job = _load()
    assert wf["name"] == "staging-collector" and set(wf["jobs"]) == {"collect"}
    triggers = wf[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert len(triggers["schedule"]) == 1
    minute, *rest = triggers["schedule"][0]["cron"].split()
    assert minute.isdigit() and 0 <= int(minute) <= 59 and rest == ["*", "*", "*", "*"], "exactly hourly"
    assert triggers["workflow_dispatch"]["inputs"]["mode"]["options"] == ["tick", "open-period", "close-period"]
    assert triggers["workflow_dispatch"]["inputs"]["mode"]["default"] == "tick"
    assert job["env"]["MODE"] == "${{ inputs.mode || 'tick' }}"


def test_kill_switch_environment_and_read_only_permissions():
    _, wf, job = _load()
    assert job["if"] == "vars.STAGING_COLLECTION_ENABLED == 'true'"
    assert job["environment"] == "staging-collection"
    assert wf["permissions"] == {"contents": "read"} and job["permissions"] == {"contents": "read"}
    assert wf["concurrency"]["group"] == "staging-collector" and wf["concurrency"]["cancel-in-progress"] is False


def test_target_is_staging_only_and_production_never_appears():
    text, _, job = _load()
    assert job["env"]["STAGING_COLLECTOR_ACCOUNT_ID"] == c.EXPECTED_STAGING_ACCOUNT_ID
    assert job["env"]["STAGING_COLLECTOR_DATABASE_NAME"] == c.EXPECTED_STAGING_DATABASE_NAME
    assert job["env"]["STAGING_COLLECTOR_DATABASE_ID"] == c.EXPECTED_STAGING_DATABASE_ID
    code = _code(text)
    for forbidden in (c.PRODUCTION_DATABASE_ID, c.PRODUCTION_DATABASE_NAME, "wrangler"):
        assert forbidden not in code, forbidden
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "api.cloudflare.com" not in runs, "no step talks to Cloudflare except through the collector's allowlist"


def test_secret_is_environment_scoped_and_only_reaches_collector_steps():
    text, _, job = _load()
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text)) == {SECRET}
    assert "secrets." not in yaml.safe_dump(job["env"])
    for i, step in enumerate(job["steps"]):
        if "secrets." in yaml.safe_dump(step):
            assert set(step["env"]) == {SECRET}, step["name"]
            assert i == 0 or COLLECTOR in step["run"], step["name"]
            assert "${{ secrets" not in step["run"]
        elif COLLECTOR in step.get("run", ""):
            pytest.fail(f"collector step without the scoped secret: {step['name']}")


def test_reviewed_collector_and_page_pins_are_mandatory():
    _, _, job = _load()
    refs = {s["with"]["ref"] for s in job["steps"] if s.get("uses", "").startswith("actions/checkout")}
    assert refs == {REVIEWED, c.PINNED_CRYPTOPULSE_COMMIT}
    assert job["env"]["REVIEWED_SHA"] == job["env"]["STAGING_COLLECTOR_GIT_SHA"] == REVIEWED
    assert job["env"]["CRYPTOPULSE_INDEX_SHA256"] == c.PINNED_CRYPTOPULSE_INDEX_SHA256
    runs = [s.get("run", "") for s in job["steps"]]
    verify = next(r for r in runs if "sha256sum -c" in r)
    for name in ("collector.py", "history_harness.py", "harness_policy.py", "relay_shims.py"):
        assert re.search(rf"^\s*[0-9a-f]{{64}}  {name}$", verify, re.M), name
    assert 'rev-parse HEAD)" = "$REVIEWED_SHA"' in verify and 'rev-parse HEAD)" = "$CRYPTOPULSE_COMMIT"' in verify
    assert all(s["with"].get("persist-credentials") is False for s in job["steps"]
               if s.get("uses", "").startswith("actions/checkout"))


def test_production_sinkholes_are_mandatory_and_precede_every_collector_call():
    _, _, job = _load()
    runs = [s.get("run", "") for s in job["steps"]]
    lock = next(i for i, r in enumerate(runs) if "/etc/hosts" in r)
    sinkholed = runs[lock].split("sudo tee -a /etc/hosts")[0]
    for host in ("sentiment-ff75.quiquandon.workers.dev", "pulseworker-v2.quiquandon.workers.dev",
                 "pulseworker-v2-staging.quiquandon.workers.dev", "script.google.com", "script.googleusercontent.com"):
        assert host in sinkholed, host
    first_write = next(i for i, r in enumerate(runs) if COLLECTOR in r or HARNESS in r)
    tests = next(i for i, r in enumerate(runs) if "pytest reviewed/staging-collector/" in r)
    assert tests < lock < first_write


def test_collector_is_the_only_write_path_and_nothing_else_is_invoked():
    _, _, job = _load()
    runs = [s.get("run", "") for s in job["steps"]]
    calls = [l.strip() for r in runs for l in r.splitlines() if l.strip().startswith("python3 ")]
    allowed = ("python3 -m pytest reviewed/staging-collector/", "python3 -m playwright install --with-deps chromium",
               HARNESS + " ", COLLECTOR + ' "$MODE" ', COLLECTOR + " btc-tick", COLLECTOR + " history-ingest ",
               COLLECTOR + " verify ", COLLECTOR + " status")
    for call in calls:
        assert call.startswith(allowed) or call == COLLECTOR + " btc-tick" or call == COLLECTOR + " status", call
    joined = "\n".join(runs).lower()
    for banned in ("stage7", "run_stage7", "experiment5", "exp005", "run_staging", "predict", "weight", "deploy",
                   "wrangler", "curl", "wget", "gh ", "python3 -c", "--dry-run", "openai", "anthropic", "gemini",
                   "migration", "sqlite3"):
        assert banned not in joined, banned
    period = next(s for s in job["steps"] if '"$MODE"' in s.get("run", ""))
    assert period["if"] == "env.MODE == 'open-period' || env.MODE == 'close-period'"
    assert "grep -Eq '^[A-Za-z0-9._-]{1,64}$'" in period["run"]
    for step in job["steps"]:
        run = step.get("run", "")
        if any(k in run for k in (" btc-tick", " history-ingest", " verify ", " status", HARNESS)):
            assert step["if"] == "env.MODE == 'tick'", step["name"]
