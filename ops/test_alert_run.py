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
    """OPERATIONS.md §3: three consecutive non-tick hours without a V2 decision, each slot classified."""

    def _chain(self, now, v2_by_slot):
        runs = []
        for s in alert_run.last_nontick_slots(now):
            st = v2_by_slot(s)
            if st == "missing":
                continue
            runs.append({"started_ts": (s + 54) * 1000, "chain": None if st == "nochain" else {"steps": {"v2": st}}})
        return {"runs": runs}

    def test_replay_of_the_2026_11_21_six_hour_gap(self):
        now = 1_795_273_200 + 25 * 60          # 2026-11-21 15:25 UTC: last non-tick slots 14:00, 13:00, 11:00
        payload = self._chain(now, lambda s: {"deferred": True, "reason": "deferred: D1 query budget"})
        problems, lines = alert_run.check_sentiment_v2(lambda u: payload if "chain" in u else {}, now)
        self.assertEqual(len(problems), 1)
        self.assertEqual([l[13:18] for l in lines], ["14:00", "13:00", "11:00"])
        self.assertTrue(all("v2_deferred" in l for l in lines))

    def test_each_state_is_distinguished_and_one_current_slot_passes(self):
        now = 1_795_273_200 + 25 * 60
        slots = alert_run.last_nontick_slots(now)
        states = {slots[0]: "missing", slots[1]: "nochain", slots[2]: {"written": 0, "reason": "no new V1 observation"}}
        problems, lines = alert_run.check_sentiment_v2(lambda u: self._chain(now, lambda s: states[s]), now)
        self.assertEqual(len(problems), 1)
        self.assertEqual([l.split(": ")[1].split(" ")[0] for l in lines], ["no_invocation", "invocation_failed", "v2_no_decision"])
        states[slots[1]] = {"written": 1}
        problems, _ = alert_run.check_sentiment_v2(lambda u: self._chain(now, lambda s: states[s]), now)
        self.assertEqual(problems, [])

    def test_falls_back_to_decision_age_when_the_chain_route_is_missing(self):
        now = 1_795_280_000
        def fetch(u):
            if "chain" in u:
                raise OSError("404")
            return {"points": [{"decision_ts": (now - 6 * 3600) * 1000}]}
        problems, _ = alert_run.check_sentiment_v2(fetch, now)
        self.assertEqual(len(problems), 1)

    def test_simulation(self):
        checks, _ = alert_run.simulate("v2_stale", 1_795_280_000)
        self.assertEqual(len(checks["sentiment_v2"]), 1)
