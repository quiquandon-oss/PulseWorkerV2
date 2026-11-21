import calendar
import unittest

import alert_run

NOW = calendar.timegm((2026, 11, 13, 12, 30, 0))


class SimulationTest(unittest.TestCase):
    def test_each_scenario_goes_through_the_real_checks(self):
        expect = {"healthy": {}, "usage_warning": {"usage": 2}, "usage_critical": {"usage": 1}, "usage_stale": {"usage": 1},
                  "predictions_stale": {"predictions": 1}, "fetch_failure": {"predictions": 1, "usage": 1}}
        for name, want in expect.items():
            checks, notes = alert_run.simulate(name, NOW)
            self.assertEqual({k: len(v) for k, v in checks.items() if v}, want, name)
            self.assertIn("SIMULATION", notes[0])
        self.assertIn("reached the daily limit", alert_run.simulate("usage_critical", NOW)[0]["usage"][0])
        self.assertIn("could not read", alert_run.simulate("fetch_failure", NOW)[0]["usage"][0])
        with self.assertRaises(SystemExit):
            alert_run.simulate("nonsense", NOW)


class LiveCheckTest(unittest.TestCase):
    def test_unreadable_sources_are_findings(self):
        def down(_url):
            raise OSError("down")
        self.assertEqual(len(alert_run.check_predictions(down)[0]), 1)
        self.assertEqual(len(alert_run.check_usage(down, NOW)[0]), 1)
        found, _ = alert_run.check_workflows("o/r", None, NOW, fetch_runs=lambda *a: (_ for _ in ()).throw(OSError("down")))
        self.assertEqual(len(found), len(alert_run.workflow_health.EXPECTED))

    def test_healthy_sources_pass(self):
        self.assertEqual(alert_run.check_usage(lambda _u: alert_run._usage_payload(NOW, 400000), NOW)[0], [])
        self.assertEqual(alert_run.check_predictions(lambda _u: alert_run._freshness_payload("FRESH"))[0], [])


if __name__ == "__main__":
    unittest.main()


class SentimentV2StalenessTest(unittest.TestCase):
    """2026-11-21: V2 stopped writing for 6 h without an alert (D1 budget deferral)."""

    def test_recent_decision_passes_and_a_6h_gap_is_a_finding(self):
        now = 1_795_280_000
        ok, notes = alert_run.check_sentiment_v2(lambda _u: {"points": [{"decision_ts": (now - 3600) * 1000}]}, now)
        self.assertEqual(ok, [])
        self.assertTrue(notes)
        bad, _ = alert_run.check_sentiment_v2(lambda _u: {"points": [{"decision_ts": (now - 6 * 3600) * 1000}]}, now)
        self.assertEqual(len(bad), 1)
        self.assertIn("6.0 h", bad[0])

    def test_unreadable_or_empty_is_a_finding(self):
        def down(_u):
            raise OSError("x")
        self.assertEqual(len(alert_run.check_sentiment_v2(down, 0)[0]), 1)
        self.assertEqual(len(alert_run.check_sentiment_v2(lambda _u: {"points": []}, 0)[0]), 1)

    def test_simulation(self):
        checks, _ = alert_run.simulate("v2_stale", 1_795_280_000)
        self.assertEqual(len(checks["sentiment_v2"]), 1)

