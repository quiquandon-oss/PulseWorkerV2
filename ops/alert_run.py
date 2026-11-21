"""Runs the three alert checks and reports their findings once (see alert_state.py and ops/ALERTING.md).

  predictions  /prediction-freshness interpreted by prediction_staleness.py
  workflows    GitHub run history interpreted by workflow_health.py
  usage        /d1-usage interpreted by d1_usage_alert.py
  sentiment_v2 newest V2 decision from /api/sentiment-v2/forecast-research (silent stop, 2026-11-21)

A check that cannot read its source is itself a finding, never a pass. Read-only apart from the alert issue.

  GITHUB_TOKEN=... python3 ops/alert_run.py quiquandon-oss/PulseWorkerV2 [--simulate NAME]

--simulate replaces the live sources with a fixed scenario and uses the label "ops-alert-test", so the whole
path (checks, de-duplication, issue, run result) can be exercised without touching the real alert. Scenarios:
healthy, usage_warning, usage_critical, usage_stale, predictions_stale, v2_stale, fetch_failure.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request

import alert_state
import d1_usage_alert
import prediction_staleness
import workflow_health

WORKER = "https://pulseworker-v2.quiquandon.workers.dev"
TEST_LABEL = "ops-alert-test"


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pulseworker-v2-alert-run", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.load(res)


def check_predictions(fetch):
    try:
        return prediction_staleness.problems(fetch(WORKER + "/prediction-freshness")), []
    except Exception as e:
        return [f"could not read /prediction-freshness ({type(e).__name__})"], []


# V2 freshness (docs/OPERATIONS.md §3, owner mandate OD-18): a finding when the last THREE non-tick hourly opportunities
# (hours not divisible by 3, UTC) produced no V2 decision. Each slot is classified from the chain summary that the
# Worker keeps on the hour's event_runs row (GET /api/sentiment-v2/chain):
#   no_invocation      no hourly run record for that slot (not scheduled, or nothing recorded)
#   invocation_failed  a run record exists but the chain never finished or a step errored before V2
#   v2_deferred        the run happened but V2 deferred itself (D1 query budget)
#   v2_no_decision     V2 ran but wrote no decision (reason given)
#   current            V2 wrote a decision
# Diagnostic only: nothing is changed or replayed. Falls back to the newest decision's age when the route is missing.
V2_MAX_GAP_H = 4
V2_SLOTS = 3
SLOT_SETTLE_S = 20 * 60


def v2_slot_state(run):
    if run is None:
        return "no_invocation", "no hourly run record"
    c = run.get("chain")
    if not c:
        return "invocation_failed", "run record without a finished chain summary"
    v2 = (c.get("steps") or {}).get("v2")
    if not v2 or v2.get("error"):
        return "invocation_failed", (v2 or {}).get("error") or "V2 step did not run"
    if v2.get("deferred"):
        return "v2_deferred", v2.get("reason") or "deferred (D1 query budget)"
    if not v2.get("written"):
        return "v2_no_decision", v2.get("reason") or "no decision written"
    return "current", f"{v2.get('written')} decision(s)"


def last_nontick_slots(now, n=V2_SLOTS):
    out, h = [], int((now - SLOT_SETTLE_S) // 3600) * 3600
    while len(out) < n:
        if (h // 3600) % 24 % 3 != 0:
            out.append(h)
        h -= 3600
    return out


def classify_v2(chain_payload, now):
    runs = (chain_payload or {}).get("runs") or []
    rows = []
    for slot in last_nontick_slots(now):
        run = next((r for r in runs if slot * 1000 <= float(r.get("started_ts") or 0) < (slot + 1800) * 1000), None)
        state, detail = v2_slot_state(run)
        rows.append((slot, state, detail))
    return rows


def check_sentiment_v2(fetch, now):
    try:
        rows = classify_v2(fetch(WORKER + "/api/sentiment-v2/chain?limit=12"), now)
    except Exception:
        rows = None
    if rows is not None:
        lines = [f"Sentiment V2 {time.strftime('%H:%M', time.gmtime(s))} UTC: {st} ({d})" for s, st, d in rows]
        if all(st != "current" for _, st, _ in rows):
            return [f"Sentiment V2: no decision in the last {len(rows)} non-tick hours: " + "; ".join(lines)], lines
        return [], lines
    try:
        d = fetch(WORKER + "/api/sentiment-v2/forecast-research?limit=1")
        pts = (d or {}).get("points") or []
        if not pts:
            return ["Sentiment V2: no decision returned by /api/sentiment-v2/forecast-research"], []
        age_h = (now * 1000 - float(pts[0]["decision_ts"])) / 3600000
        if age_h > V2_MAX_GAP_H:
            return [f"Sentiment V2: no new decision for {age_h:.1f} h (limit {V2_MAX_GAP_H} h; chain route unavailable)"], []
        return [], [f"Sentiment V2: newest decision {age_h:.1f} h old (chain route unavailable)"]
    except Exception as e:
        return [f"could not read Sentiment V2 freshness ({type(e).__name__})"], []


def check_usage(fetch, now):
    try:
        result = d1_usage_alert.assess(fetch(WORKER + "/d1-usage?days=2"), now)
        return result["problems"], result["lines"] + ["D1 consumption: " + result["level"].upper()]
    except Exception as e:
        return [f"could not read /d1-usage ({type(e).__name__})"], []


def check_workflows(repo, token, now, fetch_runs=workflow_health.fetch_runs):
    found = []
    when = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    for wf, (max_age_h, min_24h) in workflow_health.EXPECTED.items():
        try:
            found += workflow_health.assess(wf, fetch_runs(repo, wf, token), when, max_age_h, min_24h)
        except Exception as e:
            found.append(f"{wf}: could not read runs ({type(e).__name__})")
    return found, []


def _usage_payload(now, read, minutes_ago=5):
    hours = int((now % 86400) // 3600) + 1
    return {"ok": True, "recording": True, "daily_read_limit": 5000000, "days": [{
        "day": time.strftime("%Y-%m-%d", time.gmtime(now)), "rows_read": read, "rows_written": 0,
        "entries": [{"kind": "scheduled", "key": "0 * * * *", "invocations": hours, "rows_read": read, "last_ts": (now - minutes_ago * 60) * 1000}]}]}


def _freshness_payload(status):
    series = {"status": status, "cron_rows_24h": 8}
    return {"ok": True, "coins": {"BTC": {"price_data": {"latest_age_hours": 1.0}, "horizons": {"24": {"core": dict(series), "challenger": {"status": "FRESH", "cron_rows_24h": 8}}}}}}


def simulate(name, now):
    """The same check functions, fed a fixed scenario instead of the live sources."""
    def failing(_url):
        raise OSError("simulated outage")
    usage = {"healthy": 500000, "usage_warning": 3200000, "usage_critical": 5200000}
    checks, notes = {"predictions": [], "workflows": [], "usage": [], "sentiment_v2": []}, [f"SIMULATION: {name} (label {TEST_LABEL}; live sources not read)"]
    if name in usage:
        checks["usage"], lines = check_usage(lambda _u: _usage_payload(now, usage[name]), now)
        notes += lines
    elif name == "usage_stale":
        checks["usage"], lines = check_usage(lambda _u: _usage_payload(now, 500000, minutes_ago=300), now)
        notes += lines
    elif name == "predictions_stale":
        checks["predictions"], _ = check_predictions(lambda _u: _freshness_payload("STALE"))
    elif name == "v2_stale":
        checks["sentiment_v2"], _ = check_sentiment_v2(lambda _u: {"runs": [{"started_ts": (s + 54) * 1000, "chain": {"steps": {"v2": {"deferred": True, "reason": "deferred: D1 query budget"}}}} for s in last_nontick_slots(now)]}, now)
    elif name == "fetch_failure":
        checks["predictions"], _ = check_predictions(failing)
        checks["usage"], _ = check_usage(failing, now)
    else:
        raise SystemExit(f"unknown simulation: {name}")
    return checks, notes


def main(argv=None, now=None):
    argv = sys.argv[1:] if argv is None else argv
    now = time.time() if now is None else now
    repo, token = argv[0], os.environ.get("GITHUB_TOKEN")
    scenario = argv[argv.index("--simulate") + 1].strip() if "--simulate" in argv else ""
    if scenario:
        checks, notes = simulate(scenario, now)
        label = TEST_LABEL
    else:
        p, _ = check_predictions(fetch_json)
        w, _ = check_workflows(repo, token, now)
        u, notes = check_usage(fetch_json, now)
        v, vnotes = check_sentiment_v2(fetch_json, now)
        notes += vnotes
        checks, label = {"predictions": p, "workflows": w, "usage": u, "sentiment_v2": v}, alert_state.LABEL
    for line in notes:
        print(line)
    print()
    for name in ("predictions", "workflows", "usage", "sentiment_v2"):
        print(f"{name}: " + ("no problem" if not checks[name] else f"{len(checks[name])} problem(s)"))
        for text in checks[name]:
            print(f"- {text}")
    code, lines = alert_state.report(checks, alert_state.github_api(repo, token), now, run_url=os.environ.get("ALERT_RUN_URL"), label=label)
    print()
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
