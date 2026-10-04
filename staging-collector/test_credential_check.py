"""The read-only credential check: never sends SQL, and its test.yml job stays manual, pinned and sinkholed."""
import ast
import io
import json
import os
import urllib.parse
from contextlib import redirect_stdout

import pytest

import collector as c
import credential_check as cc

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = {c.ENV_ACCOUNT_ID: c.EXPECTED_STAGING_ACCOUNT_ID, c.ENV_DATABASE_NAME: c.EXPECTED_STAGING_DATABASE_NAME,
       c.ENV_DATABASE_ID: c.EXPECTED_STAGING_DATABASE_ID, c.ENV_API_TOKEN: "SECRET-VALUE-9f3a"}


class Resp(io.BytesIO):
    def __init__(self, status, obj):
        super().__init__(json.dumps(obj).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(monkeypatch, opener):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    original = c.StagingD1.__init__
    monkeypatch.setattr(c.StagingD1, "__init__", lambda self, target, opener_=None: original(self, target, opener))
    monkeypatch.setattr(c.StagingD1, "run", lambda *a, **k: pytest.fail("credential check must never send SQL"))
    out = io.StringIO()
    with redirect_stdout(out):
        code = cc.main([HERE])
    return code, out.getvalue()


def test_success_is_a_single_metadata_get(monkeypatch):
    calls = []

    def opener(req, timeout=None):
        calls.append((req.get_method(), urllib.parse.urlsplit(req.full_url).path))
        return Resp(200, {"success": True, "result": {"uuid": c.EXPECTED_STAGING_DATABASE_ID,
                                                      "name": c.EXPECTED_STAGING_DATABASE_NAME}})
    code, out = run(monkeypatch, opener)
    assert code == 0 and "RESULT: AUTHENTICATED" in out and "SECRET-VALUE-9f3a" not in out
    assert calls == [("GET", c.STAGING_DB_PATH)]


def test_401_is_reported_without_the_token(monkeypatch):
    import urllib.error

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b'{"errors":[{"code":10000}]} SECRET-VALUE-9f3a'))
    code, out = run(monkeypatch, opener)
    assert code == 3 and "HTTP 401" in out and "10000" in out and "SECRET-VALUE-9f3a" not in out


def test_script_contains_no_sql_call():
    with open(os.path.join(HERE, "credential_check.py")) as f:
        tree = ast.parse(f.read())
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "run" not in attrs and "SQL" not in attrs


def test_job_is_manual_pinned_sinkholed_and_runs_only_the_check():
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(HERE, "..", ".github", "workflows", "test.yml")) as f:
        job = yaml.safe_load(f)["jobs"]["staging-credential-check"]
    assert job["if"] == "github.event_name == 'workflow_dispatch' && inputs.job == 'staging-credential-check'"
    assert job["environment"] == "staging-collection" and job["permissions"] == {"contents": "read"}
    assert job["env"]["STAGING_COLLECTOR_DATABASE_ID"] == c.EXPECTED_STAGING_DATABASE_ID
    runs = [s.get("run", "") for s in job["steps"]]
    calls = [l.strip() for r in runs for l in r.splitlines() if l.strip().startswith("python3 ")]
    assert calls == ["python3 staging-collector/credential_check.py reviewed/staging-collector"]
    refs = {s["with"].get("ref") for s in job["steps"] if s.get("uses", "").startswith("actions/checkout")}
    assert "ddf48cad109f306fe76f2c701ce8736369247f92" in refs
    sinkholed = next(r for r in runs if "/etc/hosts" in r)
    for host in ("sentiment-ff75.quiquandon.workers.dev", "pulseworker-v2.quiquandon.workers.dev", "script.google.com"):
        assert host in sinkholed
    assert all(set(s.get("env", {})) <= {c.ENV_API_TOKEN} for s in job["steps"] if "secrets." in yaml.safe_dump(s))
    joined = "\n".join(runs)
    for banned in ("collector.py", "open-period", "btc-tick", "history", "curl", "wrangler", "python3 -c"):
        assert banned not in joined.replace("reviewed/staging-collector/collector.py", ""), banned
