import calendar
import unittest

from d1_usage_alert import assess, coverage, projection

NOON = calendar.timegm((2026, 11, 13, 12, 0, 0))
DAY = "2026-11-13"


def payload(read, hourly_runs=12, last_minutes_ago=10, now=NOON, day=DAY, **kw):
    entries = [{"kind": "scheduled", "key": "0 * * * *", "rows_read": read // 2, "invocations": hourly_runs, "last_ts": (now - last_minutes_ago * 60) * 1000},
               {"kind": "scheduled", "key": "0 7 * * *", "rows_read": 0, "invocations": 1, "last_ts": (now - 5 * 3600) * 1000},
               {"kind": "fetch", "key": "GET /chart-data", "rows_read": read - read // 2, "invocations": 40, "last_ts": (now - max(30, last_minutes_ago) * 60) * 1000}]
    return {"ok": True, "recording": True, "daily_read_limit": 5000000,
            "days": [{"day": day, "rows_read": read, "rows_written": 10, "entries": entries}], **kw}


class CoverageTest(unittest.TestCase):
    def test_full_day_is_reliable_after_three_hours(self):
        elapsed, covered, complete = coverage(payload(1)["days"][0], NOON)
        self.assertEqual((elapsed, covered, complete), (12.0, 12.0, True))
        self.assertEqual(projection(1000000, 12.0, 12.0, True), (2000000.0, "reliable"))
        self.assertEqual(projection(600000, 1.0, 1.0, True), (None, "none"))

    def test_partial_day_uses_the_measured_rate_and_is_provisional(self):
        elapsed, covered, complete = coverage(payload(1, hourly_runs=5)["days"][0], NOON)
        self.assertEqual((elapsed, covered, complete), (12.0, 5.0, False))
        value, quality = projection(1000000, 12.0, 5.0, False)
        self.assertEqual((round(value), quality), (4800000, "provisional"))   # 200,000 rows/h * 24, not 1,000,000 * 24 / 12
        self.assertEqual(projection(1000000, 12.0, 0.0, False), (None, "none"))

    def test_only_the_hourly_cron_counts_as_coverage(self):
        p = payload(1, hourly_runs=0)
        self.assertEqual(coverage(p["days"][0], NOON)[1:], (0.0, False))


class AssessTest(unittest.TestCase):
    def test_healthy(self):
        r = assess(payload(400000), NOON)
        self.assertEqual((r["level"], r["problems"], r["remaining"], r["projection_quality"]), ("healthy", [], 4600000, "reliable"))
        self.assertIn("8.0%", r["lines"][0])

    def test_warning_by_share_used(self):
        r = assess(payload(3200000), NOON)
        self.assertEqual(r["level"], "warning")
        self.assertEqual(len(r["problems"]), 2)  # 64% used and 128% projected (reliable)

    def test_warning_by_reliable_projection_only(self):
        r = assess(payload(2300000), NOON)  # 46% used, 92% projected
        self.assertEqual((r["level"], len(r["problems"])), ("warning", 1))
        self.assertIn("projected", r["problems"][0])
        self.assertEqual(assess(payload(2100000), NOON)["level"], "healthy")  # 84% projected: below the threshold

    def test_provisional_projection_is_reported_but_does_not_alert(self):
        r = assess(payload(1500000, hourly_runs=5), NOON)  # 30% used; 7.2M/day at the measured rate
        self.assertEqual((r["level"], r["problems"], r["projection_quality"], r["coverage_complete"]), ("healthy", [], "provisional", False))
        self.assertEqual(round(r["projected"]), 7200000)
        self.assertTrue(any("provisional" in line and "does not raise" in line for line in r["lines"]))
        self.assertTrue(any("partial-day" in line for line in r["lines"]))

    def test_partial_day_still_alerts_on_measured_share(self):
        self.assertEqual(assess(payload(3100000, hourly_runs=5), NOON)["level"], "warning")

    def test_critical_at_the_limit(self):
        r = assess(payload(5000000), NOON)
        self.assertEqual((r["level"], r["remaining"]), ("critical", 0))
        self.assertEqual(assess(payload(4999999), NOON)["level"], "warning")

    def test_no_projection_in_first_hours(self):
        early = calendar.timegm((2026, 11, 13, 1, 0, 0))
        r = assess(payload(600000, hourly_runs=2, now=early), early)  # would project to 288% but too early to say
        self.assertEqual((r["level"], r["projected"]), ("healthy", None))

    def test_stale_measurement_and_missing_day(self):
        self.assertEqual(assess(payload(400000, last_minutes_ago=200), NOON)["level"], "stale")
        r = assess(payload(1, day="2026-11-12"), NOON)
        self.assertEqual((r["level"], r["problems"]), ("stale", ["no usage recorded today"]))
        early = calendar.timegm((2026, 11, 13, 0, 30, 0))
        self.assertEqual(assess(payload(1, day="2026-11-12"), early)["problems"], [])

    def test_unknown_payloads_are_problems(self):
        for bad in ({"ok": False}, {"ok": True, "recording": False, "days": []}, {"ok": True, "recording": True, "days": []}):
            r = assess(bad, NOON)
            self.assertEqual(r["level"], "unknown")
            self.assertTrue(r["problems"])

    def test_problem_texts_are_stable_between_runs(self):
        a, b = assess(payload(3200000), NOON)["problems"], assess(payload(3350000, hourly_runs=15, now=NOON + 3 * 3600), NOON + 3 * 3600)["problems"]
        self.assertEqual(a, b)   # no figures in the problem lines, so a repeated alert has the same text


if __name__ == "__main__":
    unittest.main()
