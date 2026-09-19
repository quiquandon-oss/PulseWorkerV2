"""
Tests for research/selection_resolver.py (PR5a).

Uses a real in-memory SQLite database with the same table/index shape as
production (predictions, selection_decisions), not mocks -- same
discipline as test_resolver.py and test_event_detector.py.
"""

import sqlite3
import unittest

from selection_resolver import (
    MAX_WINDOW_MS,
    resolve_selection_decision,
)


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE predictions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL,
          target_ts INTEGER NOT NULL,
          horizon_hours INTEGER NOT NULL,
          p_up REAL,
          realized_up INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_predictions_horizon_ts ON predictions(horizon_hours, ts)"
    )
    conn.execute(
        """
        CREATE TABLE selection_decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL,
          coin TEXT NOT NULL,
          horizon_hours INTEGER NOT NULL,
          chosen_variant TEXT,
          chosen_p_up REAL,
          cleared_gate INTEGER,
          lca_score REAL,
          comparison_count INTEGER,
          prediction_ts INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_selection_decisions_coin_time "
        "ON selection_decisions(coin, horizon_hours, ts)"
    )
    conn.commit()
    return conn


def insert_prediction(conn, ts, horizon_hours=24, p_up=0.5, realized_up=None, target_ts=None):
    conn.execute(
        "INSERT INTO predictions (ts, target_ts, horizon_hours, p_up, realized_up) "
        "VALUES (?,?,?,?,?)",
        (ts, target_ts if target_ts is not None else ts + horizon_hours * 3600000,
         horizon_hours, p_up, realized_up),
    )
    conn.commit()


def insert_selection(conn, ts, coin="BTC", horizon_hours=24, chosen_variant="original",
                      chosen_p_up=0.5, cleared_gate=0, prediction_ts=None):
    conn.execute(
        "INSERT INTO selection_decisions "
        "(ts, coin, horizon_hours, chosen_variant, chosen_p_up, cleared_gate, "
        " lca_score, comparison_count, prediction_ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, coin, horizon_hours, chosen_variant, chosen_p_up, cleared_gate,
         0.6, 6, prediction_ts),
    )
    conn.commit()


class TestBoundsValidation(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()

    def test_missing_start_ts_raises(self):
        with self.assertRaises(ValueError):
            resolve_selection_decision(self.conn, "BTC", 24, None, 1000)

    def test_missing_end_ts_raises(self):
        with self.assertRaises(ValueError):
            resolve_selection_decision(self.conn, "BTC", 24, 0, None)

    def test_reversed_window_raises(self):
        with self.assertRaises(ValueError):
            resolve_selection_decision(self.conn, "BTC", 24, 1000, 0)

    def test_zero_width_window_raises(self):
        with self.assertRaises(ValueError):
            resolve_selection_decision(self.conn, "BTC", 24, 1000, 1000)

    def test_oversized_window_raises(self):
        with self.assertRaises(ValueError):
            resolve_selection_decision(self.conn, "BTC", 24, 0, MAX_WINDOW_MS + 1)

    def test_max_window_exactly_at_limit_does_not_raise(self):
        # Should not raise -- exactly at the cap is allowed.
        resolve_selection_decision(self.conn, "BTC", 24, 0, MAX_WINDOW_MS)

    def test_unsupported_coin_raises(self):
        with self.assertRaises(ValueError):
            resolve_selection_decision(self.conn, "LINK", 24, 0, 1000)
        with self.assertRaises(ValueError):
            resolve_selection_decision(self.conn, "ETH", 24, 0, 1000)


class TestAsOfSemantics(unittest.TestCase):
    """The core behavior: as-of-wall-clock-time, not prediction_ts-keyed."""

    def setUp(self):
        self.conn = make_conn()

    def test_picks_most_recent_selection_at_or_before_prediction_ts(self):
        insert_selection(self.conn, ts=100, chosen_p_up=0.9)
        insert_selection(self.conn, ts=250, chosen_p_up=0.1)  # after prediction.ts
        insert_prediction(self.conn, ts=200, horizon_hours=24)

        rows = resolve_selection_decision(self.conn, "BTC", 24, 0, 1000)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["selection_ts"], 100)
        self.assertEqual(rows[0]["chosen_p_up"], 0.9)

    def test_boundary_inclusive_at_exactly_prediction_ts(self):
        insert_selection(self.conn, ts=200, chosen_p_up=0.42)
        insert_prediction(self.conn, ts=200, horizon_hours=24)

        rows = resolve_selection_decision(self.conn, "BTC", 24, 0, 1000)
        self.assertEqual(rows[0]["selection_ts"], 200)
        self.assertEqual(rows[0]["chosen_p_up"], 0.42)

    def test_no_lookahead_future_selection_never_used(self):
        """Mirrors resolver.py's own V1@100/200/300 no-lookahead case:
        a selection_decisions row that only exists AFTER the prediction's
        own ts must never be attached to that prediction."""
        insert_prediction(self.conn, ts=200, horizon_hours=24)
        insert_selection(self.conn, ts=300, chosen_p_up=0.77)  # only a future row exists

        rows = resolve_selection_decision(self.conn, "BTC", 24, 0, 1000)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["selection_ts"])
        self.assertIsNone(rows[0]["chosen_p_up"])

    def test_no_selection_exists_at_all_returns_none_not_a_default(self):
        insert_prediction(self.conn, ts=200, horizon_hours=24)
        rows = resolve_selection_decision(self.conn, "BTC", 24, 0, 1000)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["selection_ts"])
        self.assertIsNone(rows[0]["chosen_variant"])

    def test_different_horizon_hours_not_cross_matched(self):
        insert_prediction(self.conn, ts=200, horizon_hours=24)
        insert_selection(self.conn, ts=100, horizon_hours=12, chosen_p_up=0.9)  # wrong horizon

        rows = resolve_selection_decision(self.conn, "BTC", 24, 0, 1000)
        self.assertIsNone(rows[0]["selection_ts"])

    def test_different_coin_not_cross_matched(self):
        insert_prediction(self.conn, ts=200, horizon_hours=24)
        insert_selection(self.conn, ts=100, coin="LINK_INTERNAL_TEST", chosen_p_up=0.9)

        rows = resolve_selection_decision(self.conn, "BTC", 24, 0, 1000)
        self.assertIsNone(rows[0]["selection_ts"])


class TestRealDuplicateChainReproduction(unittest.TestCase):
    """Reproduces the real BTC/24h production chain that motivated this
    module: prediction_ts=1789614039967, with 7 selection_decisions rows
    all logged AFTER that prediction was made (the earliest ~1.4h later,
    the latest ~29h later), chosen_p_up drifting from 0.667 to 0.333 for
    the same prediction_ts. A prediction_ts-keyed join would have had to
    arbitrarily pick one of these seven, hindsight-contaminated rows. The
    as-of join must correctly attach NONE of them."""

    def setUp(self):
        self.conn = make_conn()
        self.prediction_ts = 1789614039967
        insert_prediction(
            self.conn, ts=self.prediction_ts, horizon_hours=24,
            p_up=0.3333333333333333, realized_up=1,
        )
        # Real observed ts values (ms) and chosen_p_up values for this chain.
        real_chain = [
            (1789619112544, 0.6666666666666666),
            (1789667200358, 0.6),
            (1789704381788, 0.3333333333333333),
            (1789704406392, 0.3333333333333333),
            (1789709104855, 0.3333333333333333),
            (1789718331625, 0.3333333333333333),
            (1789718360683, 0.3333333333333333),
        ]
        for ts, chosen_p_up in real_chain:
            insert_selection(
                self.conn, ts=ts, chosen_variant="original",
                chosen_p_up=chosen_p_up, prediction_ts=self.prediction_ts,
            )

    def test_none_of_the_seven_hindsight_rows_are_attached(self):
        rows = resolve_selection_decision(
            self.conn, "BTC", 24, self.prediction_ts, self.prediction_ts + 1
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["selection_ts"])
        self.assertIsNone(rows[0]["chosen_p_up"])

    def test_an_earlier_genuine_prior_decision_would_still_be_found(self):
        # Sanity check the as-of join isn't simply broken: a decision
        # genuinely logged BEFORE the prediction must still be found.
        insert_selection(
            self.conn, ts=self.prediction_ts - 1000, chosen_variant="original",
            chosen_p_up=0.55, prediction_ts=self.prediction_ts - 500000,
        )
        rows = resolve_selection_decision(
            self.conn, "BTC", 24, self.prediction_ts, self.prediction_ts + 1
        )
        self.assertEqual(rows[0]["selection_ts"], self.prediction_ts - 1000)
        self.assertEqual(rows[0]["chosen_p_up"], 0.55)


class TestDeterminism(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        insert_selection(self.conn, ts=100, chosen_p_up=0.9)
        insert_prediction(self.conn, ts=200, horizon_hours=24)
        insert_prediction(self.conn, ts=400, horizon_hours=24)

    def test_same_call_twice_yields_identical_results(self):
        first = resolve_selection_decision(self.conn, "BTC", 24, 0, 1000)
        second = resolve_selection_decision(self.conn, "BTC", 24, 0, 1000)
        self.assertEqual(first, second)


class TestQueryPlanUsesIndexes(unittest.TestCase):
    """Local-SQLite query-plan check, same convention resolver.py's own
    test suite uses -- the real production EXPLAIN QUERY PLAN (confirming
    idx_predictions_horizon_ts and idx_selection_decisions_coin_time are
    used with zero table scans) is recorded in the PR description, since
    CI does not have production D1 credentials."""

    def setUp(self):
        self.conn = make_conn()
        insert_prediction(self.conn, ts=200, horizon_hours=24)
        insert_selection(self.conn, ts=100, chosen_p_up=0.9)

    def test_no_full_table_scan_in_query_plan(self):
        from selection_resolver import SELECTION_RESOLVER_SQL

        plan_rows = self.conn.execute(
            "EXPLAIN QUERY PLAN " + SELECTION_RESOLVER_SQL,
            ("BTC", "BTC", 24, 0, 1000),
        ).fetchall()
        details = " | ".join(str(r[-1]) for r in plan_rows)
        self.assertNotIn("SCAN predictions", details)
        self.assertNotIn("SCAN sd", details)
        self.assertNotIn("SCAN sd2", details)
        self.assertIn("USING INDEX idx_predictions_horizon_ts", details)
        self.assertIn("USING INDEX idx_selection_decisions_coin_time", details)


if __name__ == "__main__":
    unittest.main()
