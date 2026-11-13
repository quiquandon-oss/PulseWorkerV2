"""Reports the alert checks' findings once, through a single GitHub issue, instead of once per run.

State is one open issue carrying the label below. Its body holds a fingerprint of the current set of problems.

  problems, no open issue          -> open the issue                      (notification), exit 1
  same problems as the open issue  -> update "last seen" in the body only (no notification), exit 0
  different problems               -> comment on the issue, update body   (notification), exit 1
  no problems, issue open          -> comment "recovered" and close it    (notification), exit 0
  no problems, no issue            -> nothing, exit 0
  GitHub cannot be reached         -> exit 1, so the run itself is red

A red run therefore marks a new or changed alert, and an open issue marks an ongoing one. The fingerprint ignores
figures in the problem texts, so "62% used" followed by "64% used" is the same alert. See ops/ALERTING.md.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request

LABEL = "ops-alert"
MARKER = re.compile(r"<!-- alert-fingerprint: ([0-9a-f]{16}) -->")
FIRST_SEEN = re.compile(r"First seen: ([^\n]+)")


def problem_lines(checks):
    """checks: {check name: [problem text, ...]} -> sorted 'name: text' lines."""
    return sorted(f"{name}: {text}" for name, problems in checks.items() for text in problems)


def fingerprint(lines):
    stable = sorted(re.sub(r"\d[\d.,]*", "#", line) for line in lines)
    return hashlib.sha256("\n".join(stable).encode()).hexdigest()[:16]


def _utc(now):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))


def issue_body(lines, fp, first_seen, now, run_url):
    return "\n".join([
        "The scheduled alert checks found a problem. This issue stays open while it lasts and is closed automatically",
        "when the checks pass again. It is updated in place; a comment is added only when the set of problems changes.",
        "", "**Current problems**", *[f"- {line}" for line in lines], "",
        f"First seen: {first_seen}", f"Last seen: {_utc(now)}", f"Latest run: {run_url or 'not reported'}",
        "", "What the checks mean and what to do: `ops/ALERTING.md`.", "", f"<!-- alert-fingerprint: {fp} -->"])


def github_api(repo, token):
    def call(method, path, body=None):
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "pulseworker-v2-alert-state",
                                              "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
            return json.loads(raw) if raw else None
    return call


def report(checks, api, now, run_url=None, label=LABEL):
    """Apply the table in the module docstring. Returns (exit code, lines to print)."""
    lines = problem_lines(checks)
    try:
        listed = api("GET", f"/issues?labels={label}&state=open&per_page=10") or []
        issues = sorted((i for i in listed if "pull_request" not in i), key=lambda i: i["number"])
        issue = issues[0] if issues else None
        if not lines:
            if issue is None:
                return 0, ["alert state: healthy, no open alert"]
            api("POST", f"/issues/{issue['number']}/comments", {"body": f"Recovered: all checks passed at {_utc(now)}.\n\nRun: {run_url or 'not reported'}"})
            api("PATCH", f"/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
            return 0, [f"alert state: recovered, closed issue #{issue['number']}"]
        fp = fingerprint(lines)
        title = f"Operations alert: {len(lines)} problem{'s' if len(lines) != 1 else ''} found by the scheduled checks"
        if issue is None:
            created = api("POST", "/issues", {"title": title, "labels": [label], "body": issue_body(lines, fp, _utc(now), now, run_url)})
            return 1, [f"alert state: NEW alert, opened issue #{created['number']}"]
        body = issue.get("body") or ""
        known = MARKER.search(body)
        first = FIRST_SEEN.search(body)
        first_seen = first.group(1).strip() if first else _utc(now)
        api("PATCH", f"/issues/{issue['number']}", {"title": title, "body": issue_body(lines, fp, first_seen, now, run_url)})
        if known and known.group(1) == fp:
            return 0, [f"alert state: ongoing alert already reported in issue #{issue['number']} (no new notification)"]
        api("POST", f"/issues/{issue['number']}/comments",
            {"body": "The set of problems changed.\n\n" + "\n".join(f"- {line}" for line in lines) + f"\n\nRun: {run_url or 'not reported'}"})
        return 1, [f"alert state: CHANGED alert, commented on issue #{issue['number']}"]
    except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError) as e:
        return 1, [f"alert state: could not reach the GitHub issue API ({type(e).__name__}); "
                   + (f"{len(lines)} problem(s) found and not recorded" if lines else "no problems found")]
