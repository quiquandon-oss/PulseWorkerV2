"""The non-writing G1-A/G1-B validation: report logic, and the safety properties of test.yml's g1-live-validation job."""
import json
import os
import re

import pytest

import g1_report as g

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_YML = os.path.join(HERE, "..", ".github", "workflows", "test.yml")
REVIEWED_SHA = "ddf48cad109f306fe76f2c701ce8736369247f92"

BTC_OK = ('DRY RUN: local in-memory replica\nObservation: {"price": 64000.0, "cross_check_price": 64064.0, '
          '"observed_ts": 1}\nRESULT: WRITTEN {"table": "btc_data", "id": 9}\n')
BTC_REJECTED = BTC_OK.replace("RESULT: WRITTEN", "RESULT: REJECTED")
HIST_OK = 'DRY RUN\nRESULT: WRITTEN {"table": "history", "id": 1}\n'


def capture(**over):
    base = {"fixture_mode": False, "excluded_sources": ["foufi", "ninemag"],
            "payload": {"score": 55, "technicalScore": 60, "btcPrice": 64010.0, "sources": {"fng": 54, "funding": 50}},
            "request_log": [
                {"method": "POST", "url": "https://sentiment-ff75.quiquandon.workers.dev/history", "action": "CAPTURE"},
                {"method": "POST", "url": "https://script.google.com/macros/s/x/exec", "action": "BLOCK"},
                {"method": "GET", "url": "https://sentiment-ff75.quiquandon.workers.dev/foufi-latest", "action": "BLOCK"},
                {"method": "GET", "url": "https://api.alternative.me/fng/", "action": "PUBLIC"}]}
    base.update(over)
    return base


def test_pass_report_contains_every_requested_field():
    r = g.build_report(BTC_OK, capture(), HIST_OK)
    assert r["RESULT"] == "PASS"
    assert (r["hyperliquid_price"], r["coinbase_price"], r["pct_difference"]) == (64000.0, 64064.0, 0.0999)
    assert r["btc_passed"] and r["sources_resolved"] == 2 and r["btcPrice_populated"]
    assert (r["score"], r["technical_score"]) == (55, 60)
    assert r["unresolved_sources"] == ["foufi", "ninemag"] and r["blocked_request_count"] == 2
    assert "POST https://script.google.com/macros/s/x/exec" in r["blocked_requests"]
    md = g.to_markdown(r)
    assert "| RESULT | PASS |" in md and "BTC Coinbase price" in md


def test_any_production_request_not_blocked_fails_the_run():
    log = capture()["request_log"] + [{"method": "POST", "url": "https://sentiment-ff75.quiquandon.workers.dev/txs-backup",
                                       "action": "PUBLIC"}]
    assert g.build_report(BTC_OK, capture(request_log=log), HIST_OK)["RESULT"] == "FAIL"


def test_fixture_capture_fails_and_missing_pieces_are_incomplete():
    assert g.build_report(BTC_OK, capture(fixture_mode=True), HIST_OK)["RESULT"] == "FAIL"
    assert g.build_report(BTC_REJECTED, capture(), HIST_OK)["RESULT"] == "INCOMPLETE"
    assert g.build_report(BTC_OK, capture(payload=None), "RESULT: NO_OBSERVATION")["RESULT"] == "INCOMPLETE"
    r = g.build_report("", {}, "")
    assert r["RESULT"] == "FAIL" and r["composite_captured"] is False  # no capture file at all: fixture_mode unknown


def _job():
    yaml = pytest.importorskip("yaml")
    with open(TEST_YML) as f:
        text = f.read()
    return text, yaml.safe_load(text)["jobs"]["g1-live-validation"]


def test_job_runs_only_on_manual_dispatch_with_read_only_permissions_and_no_secret():
    text, job = _job()
    assert job["if"] == "github.event_name == 'workflow_dispatch'"
    assert job["permissions"] == {"contents": "read"}
    assert "secrets." not in text, "test.yml must not reference any secret"


def test_job_uses_the_reviewed_commit_and_the_pinned_page():
    _, job = _job()
    refs = {s["with"].get("ref") for s in job["steps"] if s.get("uses", "").startswith("actions/checkout")
            and s.get("with", {}).get("ref")}
    assert refs == {REVIEWED_SHA, "0a1dfb8ce88883336ee2e712a84e49be857b1724"}
    assert job["env"]["REVIEWED_SHA"] == REVIEWED_SHA
    hashes = re.findall(r"^\s*([0-9a-f]{64})  (\w+\.py)$", _job()[0], re.M)
    assert {name for _, name in hashes} == {"collector.py", "history_harness.py", "harness_policy.py", "relay_shims.py"}


def test_job_never_writes_and_never_reaches_cloudflare_or_production():
    _, job = _job()
    runs = [s.get("run", "") for s in job["steps"]]
    collector_calls = [line for r in runs for line in r.splitlines() if "python3" in line and "collector.py" in line]
    assert collector_calls and all("--dry-run" in line for line in collector_calls)
    for cmd in ("open-period", "close-period", " verify", " status", "wrangler", "curl", "migration"):
        assert not any(cmd in r for r in runs), cmd
    lock = next(i for i, r in enumerate(runs) if "/etc/hosts" in r)
    first_live = next(i for i, r in enumerate(runs) if "btc-tick" in r)
    assert lock < first_live, "hosts must be sinkholed before any live step"
    sinkholed = runs[lock].split("sudo tee -a /etc/hosts")[0]  # only the hosts actually written to /etc/hosts
    for host in ("api.cloudflare.com", "sentiment-ff75.quiquandon.workers.dev", "pulseworker-v2.quiquandon.workers.dev",
                 "script.google.com"):
        assert host in sinkholed, host
    assert all(s.get("with", {}).get("persist-credentials") is False for s in job["steps"]
               if s.get("uses", "").startswith("actions/checkout"))
