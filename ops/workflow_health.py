"""Checks that the scheduled collection and evaluation workflows are actually running.

For each workflow: the newest successful run must be recent enough, the newest completed runs must not all have
failed, and (where a minimum is set) enough runs must have succeeded in the last 24 hours. Read-only: it lists
workflow runs through the GitHub API. Used by .github/workflows/prediction-staleness-alert.yml.

  GITHUB_TOKEN=... python3 ops/workflow_health.py quiquandon-oss/PulseWorkerV2
"""
import datetime as dt
import json
import os
import sys
import urllib.request

# workflow file -> (max hours since the last success, minimum successful runs in the last 24 h or None)
EXPECTED = {
    "research-funding-intraday-schedule.yml": (3, 20),     # hourly, started by the Worker cron
    "event-detection-schedule.yml": (14, None),            # fallback trigger; GitHub starts about 5 a day
    "live-evidence-collection.yml": (16, None),            # every 6 h, often late
    "research-forward-schedule.yml": (40, None),           # daily
    "research-archive-feed.yml": (40, None),               # daily
    "exp23-forward-evaluation.yml": (40, None),            # daily
    "export-learning-data.yml": (40, None),                # daily
    "exp004-timesfm.yml": (40, None),                      # daily
    "exp008-chronos.yml": (40, None),                      # daily
    "pev1p-candle-archive.yml": (9 * 24, None),            # weekly
    "pev1p-counts.yml": (9 * 24, None),                    # weekly
}
REPEATED_FAILURES = 3


def _ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def assess(name, runs, now, max_age_h, min_24h):
    """runs: newest first, each with created_at, status, conclusion. Returns a list of problems."""
    out = []
    done = [r for r in runs if r.get("status") == "completed"]
    ok = [r for r in done if r.get("conclusion") == "success"]
    if not ok:
        return [f"{name}: no successful run among the latest {len(runs)}"]
    age = (now - _ts(ok[0]["created_at"])).total_seconds() / 3600
    if age > max_age_h:
        out.append(f"{name}: last success {age:.1f} h ago (limit {max_age_h} h)")
    latest = done[:REPEATED_FAILURES]
    if len(latest) == REPEATED_FAILURES and all(r.get("conclusion") != "success" for r in latest):
        out.append(f"{name}: the latest {REPEATED_FAILURES} completed runs all failed")
    if min_24h is not None:
        n = sum(1 for r in ok if (now - _ts(r["created_at"])).total_seconds() <= 24 * 3600)
        if n < min_24h:
            out.append(f"{name}: {n} successful runs in the last 24 h (minimum {min_24h})")
    return out


def fetch_runs(repo, workflow, token):
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs?per_page=40",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "pulseworker-v2-workflow-health",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res).get("workflow_runs", [])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    repo, token, now = argv[0], os.environ.get("GITHUB_TOKEN"), dt.datetime.now(dt.timezone.utc)
    found = []
    for wf, (max_age_h, min_24h) in EXPECTED.items():
        try:
            found += assess(wf, fetch_runs(repo, wf, token), now, max_age_h, min_24h)
        except Exception as e:  # an unreadable workflow is itself a problem to show, not to hide
            found.append(f"{wf}: could not read runs ({type(e).__name__})")
    for p in found:
        print(f"- {p}")
    print("scheduled workflows: " + ("PROBLEMS FOUND" if found else f"all {len(EXPECTED)} healthy"))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
