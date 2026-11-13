import unittest
import urllib.error

from alert_state import LABEL, MARKER, fingerprint, problem_lines, report

NOW = 1794590000
WARN = {"usage": ["rows read today are above 60% of the daily limit"], "predictions": [], "workflows": []}
CRIT = {"usage": ["rows read today have reached the daily limit"], "predictions": [], "workflows": []}
HEALTHY = {"usage": [], "predictions": [], "workflows": []}


class FakeGitHub:
    """An in-memory issue tracker speaking the four calls report() makes."""

    def __init__(self, fail=False):
        self.issues, self.calls, self.fail = [], [], fail

    def __call__(self, method, path, body=None):
        self.calls.append((method, path.split("?")[0]))
        if self.fail:
            raise urllib.error.URLError("down")
        if method == "GET":
            wanted = path.split("labels=")[1].split("&")[0]
            return [i for i in self.issues if i["state"] == "open" and wanted in i["labels"]]
        if method == "POST" and path == "/issues":
            issue = {"number": len(self.issues) + 1, "state": "open", "comments": [], **body}
            self.issues.append(issue)
            return issue
        number = int(path.split("/")[2])
        issue = next(i for i in self.issues if i["number"] == number)
        if method == "POST":
            issue["comments"].append(body["body"])
        else:
            issue.update(body)
        return issue

    def notifications(self):
        """Things GitHub notifies about: issue opened, comment added, issue closed."""
        return sum(1 for m, p in self.calls if m == "POST") + sum(1 for i in self.issues if i["state"] == "closed")


class ReportTest(unittest.TestCase):
    def test_healthy_with_no_alert_does_nothing(self):
        gh = FakeGitHub()
        self.assertEqual(report(HEALTHY, gh, NOW)[0], 0)
        self.assertEqual(gh.calls, [("GET", "/issues")])

    def test_new_problem_opens_one_issue_and_fails_the_run(self):
        gh = FakeGitHub()
        code, lines = report(WARN, gh, NOW, run_url="https://example/run/1")
        self.assertEqual(code, 1)
        self.assertIn("NEW alert", lines[0])
        self.assertEqual(len(gh.issues), 1)
        issue = gh.issues[0]
        self.assertEqual(issue["labels"], [LABEL])
        self.assertIn("usage: rows read today are above 60% of the daily limit", issue["body"])
        self.assertEqual(MARKER.search(issue["body"]).group(1), fingerprint(problem_lines(WARN)))

    def test_repeated_problem_is_not_reported_again(self):
        gh = FakeGitHub()
        report(WARN, gh, NOW)
        before = gh.notifications()
        for k in range(1, 6):   # five more 3-hourly runs with the same finding
            code, lines = report(WARN, gh, NOW + k * 3 * 3600)
            self.assertEqual(code, 0)
            self.assertIn("already reported", lines[0])
        self.assertEqual((len(gh.issues), gh.issues[0]["comments"], gh.notifications()), (1, [], before))
        self.assertIn("First seen: 2026-11-13 17:13 UTC", gh.issues[0]["body"])     # kept
        self.assertIn("Last seen: 2026-11-14 08:13 UTC", gh.issues[0]["body"])      # advanced

    def test_figures_do_not_make_a_new_alert_but_a_different_problem_does(self):
        a = {"predictions": ["BTC 24h core: 5 scheduled rows in 24 h (expected 8, minimum 6)"]}
        b = {"predictions": ["BTC 24h core: 4 scheduled rows in 24 h (expected 8, minimum 6)"]}
        self.assertEqual(fingerprint(problem_lines(a)), fingerprint(problem_lines(b)))
        self.assertNotEqual(fingerprint(problem_lines(WARN)), fingerprint(problem_lines(CRIT)))
        self.assertNotEqual(fingerprint(problem_lines({"predictions": ["BTC 24h core: status STALE"]})),
                            fingerprint(problem_lines({"predictions": ["ETH 24h core: status STALE"]})))

    def test_escalation_comments_once_and_fails_the_run(self):
        gh = FakeGitHub()
        report(WARN, gh, NOW)
        code, lines = report(CRIT, gh, NOW + 3 * 3600)
        self.assertEqual(code, 1)
        self.assertIn("CHANGED alert", lines[0])
        self.assertEqual(len(gh.issues[0]["comments"]), 1)
        self.assertIn("reached the daily limit", gh.issues[0]["comments"][0])
        self.assertEqual(report(CRIT, gh, NOW + 6 * 3600)[0], 0)                      # and then it is deduplicated again
        self.assertEqual(len(gh.issues[0]["comments"]), 1)

    def test_recovery_comments_and_closes_then_a_new_problem_opens_a_new_issue(self):
        gh = FakeGitHub()
        report(WARN, gh, NOW)
        code, lines = report(HEALTHY, gh, NOW + 3 * 3600)
        self.assertEqual(code, 0)
        self.assertIn("closed issue #1", lines[0])
        self.assertEqual(gh.issues[0]["state"], "closed")
        self.assertIn("Recovered", gh.issues[0]["comments"][-1])
        self.assertEqual(report(HEALTHY, gh, NOW + 6 * 3600)[0], 0)
        self.assertEqual(report(WARN, gh, NOW + 9 * 3600)[0], 1)
        self.assertEqual([i["state"] for i in gh.issues], ["closed", "open"])

    def test_github_failure_fails_the_run_whatever_was_found(self):
        for checks in (WARN, HEALTHY):
            code, lines = report(checks, FakeGitHub(fail=True), NOW)
            self.assertEqual(code, 1)
            self.assertIn("could not reach", lines[0])

    def test_a_separate_label_keeps_test_alerts_apart(self):
        gh = FakeGitHub()
        report(WARN, gh, NOW, label="ops-alert-test")
        self.assertEqual(gh.issues[0]["labels"], ["ops-alert-test"])
        self.assertEqual(report(HEALTHY, gh, NOW)[0], 0)          # the real label sees no open alert
        self.assertEqual(gh.issues[0]["state"], "open")


if __name__ == "__main__":
    unittest.main()
