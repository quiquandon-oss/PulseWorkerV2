import calendar
import unittest

from d1_usage_alert import assess

NOON = calendar.timegm((2026, 11, 13, 12, 0, 0))


def payload(read, day="2026-11-13", **kw):
    return {"ok": True, "recording": True, "daily_read_limit": 5000000,
            "days": [{"day": day, "rows_read": read, "rows_written": 10,
                      "entries": [{"kind": "scheduled", "key": "0 * * * *", "rows_read": read, "invocations": 12}]}], **kw}


class AssessTest(unittest.TestCase):
    def test_low_usage_is_fine(self):
        problems, lines = assess(payload(400000), NOON)
        self.assertEqual(problems, [])
        self.assertIn("8.0%", lines[0])

    def test_over_share_so_far(self):
        problems, _ = assess(payload(3200000), NOON)
        self.assertEqual(len(problems), 2)  # 64% so far and 128% projected

    def test_projection_only(self):
        problems, _ = assess(payload(2300000), NOON)  # 46% so far, 92% projected
        self.assertEqual(len(problems), 1)
        self.assertIn("projected", problems[0])

    def test_no_projection_in_first_hours(self):
        early = calendar.timegm((2026, 11, 13, 1, 0, 0))
        self.assertEqual(assess(payload(600000), early)[0], [])  # would project to 288% but too early to say

    def test_missing_day(self):
        self.assertEqual(assess(payload(1, day="2026-11-12"), NOON)[0], ["no usage recorded today"])
        early = calendar.timegm((2026, 11, 13, 0, 30, 0))
        self.assertEqual(assess(payload(1, day="2026-11-12"), early)[0], [])

    def test_bad_payloads(self):
        self.assertTrue(assess({"ok": False}, NOON)[0])
        self.assertTrue(assess({"ok": True, "recording": False, "days": []}, NOON)[0])


if __name__ == "__main__":
    unittest.main()
