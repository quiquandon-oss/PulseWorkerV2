"""
Tests for research/experiment5_agent.py.

All tests execute against a real, in-memory SQLite database shaped
like production + the proposed migration 0015 archive table (mirrors
test_hypothesis_gate.py's own fixture convention). No network, no D1
connection, no test here ever constructs or uses a production
connection.

Run with: python3 -m pytest research/test_experiment5_agent.py -v
"""
import inspect
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import experiment5_agent as agent  # noqa: E402
import sentiment_archive as sa  # noqa: E402

HOUR = 3600000
DAY = 24 * HOUR


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE research_sentiment_archive (
        archive_id INTEGER PRIMARY KEY AUTOINCREMENT, observation_ts INTEGER NOT NULL,
        sources_json TEXT, score REAL, technical_score REAL, btc_price REAL, gold_regime TEXT,
        source_weights_version TEXT NOT NULL, schema_version TEXT NOT NULL, written_by TEXT NOT NULL,
        content_hash TEXT NOT NULL, archived_ts INTEGER NOT NULL
    )""")
    conn.execute("CREATE UNIQUE INDEX idx_archive_ts ON research_sentiment_archive(observation_ts)")
    conn.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL,
        sources_json TEXT, technical_score INTEGER, gold_regime TEXT
    )""")
    conn.execute("CREATE INDEX idx_ts ON history(ts)")
    conn.execute("""CREATE TABLE btc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
    )""")
    conn.execute("CREATE INDEX idx_btc_data_ts ON btc_data(ts)")
    conn.execute("""CREATE TABLE research_hypotheses (
        hypothesis_id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts INTEGER NOT NULL,
        last_updated_ts INTEGER NOT NULL, subject TEXT NOT NULL, statement TEXT NOT NULL,
        source_analysis_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OBSERVATION',
        evidence_summary_json TEXT, out_of_sample_status TEXT
    )""")
    return conn


def seed(conn, ts, sources, score, btc_price=None):
    """Writes BOTH the archive row and the matching `history`/`btc_data`
    rows at the same ts -- mirrors how a real production write would
    populate the archive alongside (not instead of) the existing
    write path once wired in."""
    sa.archive_observation(conn, ts, sources, score, source_weights_version="v1", written_by="test", archived_ts=ts)
    conn.execute("INSERT INTO history (ts, score, sources_json) VALUES (?, ?, ?)", (ts, score, json.dumps(sources)))
    if btc_price is not None:
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, btc_price))


class TestObserveNoLookahead:
    def test_observe_never_returns_a_row_after_as_of_ts(self):
        conn = fresh_db()
        seed(conn, 1000, {"fng": 50}, 50)
        seed(conn, 2000, {"fng": 55}, 55)
        seed(conn, 3000, {"fng": 60}, 60)  # "the future" relative to as_of_ts=2000
        rows = agent.observe(conn, as_of_ts=2000, window_ms=10000)
        observed_ts = [r["observation_ts"] for r in rows]
        assert 3000 not in observed_ts
        assert observed_ts == [1000, 2000]

    def test_observe_respects_window_lower_bound(self):
        conn = fresh_db()
        seed(conn, 1000, {"fng": 50}, 50)
        seed(conn, 50000, {"fng": 55}, 55)
        rows = agent.observe(conn, as_of_ts=50000, window_ms=1000)  # window excludes ts=1000
        assert [r["observation_ts"] for r in rows] == [50000]


class TestRunAgentCycle:
    def test_insufficient_data_with_fewer_than_two_rows(self):
        conn = fresh_db()
        seed(conn, 1000, {"fng": 50}, 50)
        result = agent.run_agent_cycle(conn, as_of_ts=1000, created_ts=1000)
        assert result["status"] == "INSUFFICIENT_ARCHIVE_DATA"
        assert result["decisions_created"] == 0

    def test_cross_source_confirmation_creates_a_monitor_decision(self):
        conn = fresh_db()
        # Two independent sources both trending bullish, same window.
        for i in range(4):
            seed(conn, i * HOUR, {"fng": 50 + i * 5, "etfflows": 40 + i * 5}, 50)
        result = agent.run_agent_cycle(conn, as_of_ts=3 * HOUR, created_ts=3 * HOUR, window_ms=10 * HOUR)
        assert result["status"] == "OK"
        assert result["confirmation"]["classification"] == "CROSS_SOURCE_CONFIRMATION"
        assert result["decisions_created"] >= 1

        rows = conn.execute("SELECT subject, status, evidence_summary_json FROM research_hypotheses").fetchall()
        confirmation_rows = [r for r in rows if "cross_source_confirmation" in r[0]]
        assert len(confirmation_rows) == 1
        assert confirmation_rows[0][1] == "MONITOR"
        payload = json.loads(confirmation_rows[0][2])
        assert "decision" in payload
        assert "outcome" not in payload  # not yet evaluated

    def test_reversal_creates_an_observation_decision(self):
        conn = fresh_db()
        # Established uptrend then a sharp reversal, single source.
        values = [50, 55, 60, 65, 55]
        for i, v in enumerate(values):
            seed(conn, i * HOUR, {"fng": v}, 50)
        result = agent.run_agent_cycle(conn, as_of_ts=4 * HOUR, created_ts=4 * HOUR, window_ms=10 * HOUR)
        rows = conn.execute("SELECT subject, status FROM research_hypotheses WHERE subject LIKE '%reversal%'").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "OBSERVATION"

    def test_candidate_new_sources_reported_never_persisted_as_a_decision(self):
        conn = fresh_db()
        seed(conn, 0, {"fng": 50, "totally_new_source": 1}, 50)
        seed(conn, HOUR, {"fng": 52, "totally_new_source": 2}, 50)
        result = agent.run_agent_cycle(conn, as_of_ts=HOUR, created_ts=HOUR, window_ms=10 * HOUR)
        assert "totally_new_source" in result["candidate_new_sources"]
        # a candidate-source observation is not, on its own, a persisted decision
        n_decisions = conn.execute("SELECT COUNT(*) FROM research_hypotheses").fetchone()[0]
        assert n_decisions == result["decisions_created"]

    def test_decision_count_capped_per_cycle(self):
        conn = fresh_db()
        # Build a scenario that could plausibly want many reversal decisions:
        # several sources each independently reversing.
        keys = [f"src{i}" for i in range(agent.MAX_DECISIONS_PER_CYCLE + 5)]
        values = [50, 55, 60, 65, 55]
        for i, v in enumerate(values):
            seed(conn, i * HOUR, {k: v for k in keys}, 50)
        result = agent.run_agent_cycle(conn, as_of_ts=4 * HOUR, created_ts=4 * HOUR, window_ms=10 * HOUR)
        assert result["decisions_created"] <= agent.MAX_DECISIONS_PER_CYCLE


class TestPersistDecisionStatusGuard:
    def test_disallowed_status_raises_and_never_inserts(self):
        conn = fresh_db()
        record = {
            "subject": "experiment5:test", "statement": "x",
            "lifecycle_status": "BUILD_REQUEST",  # not allowed from this MVP
            "decision": {"anchor_ts": 1000, "cycle_ts": 1000, "primary_source": "fng", "direction": 1,
                         "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}},
        }
        with pytest.raises(ValueError):
            agent.persist_experiment5_decision(conn, 1000, record)
        assert conn.execute("SELECT COUNT(*) FROM research_hypotheses").fetchone()[0] == 0

    def test_allowed_statuses_accepted(self):
        conn = fresh_db()
        for status in agent.EXPERIMENT5_ALLOWED_STATUSES:
            record = {
                "subject": f"experiment5:test:{status}", "statement": "x", "lifecycle_status": status,
                "decision": {"anchor_ts": 1000, "cycle_ts": 1000, "primary_source": "fng", "direction": 1,
                             "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}},
            }
            agent.persist_experiment5_decision(conn, 1000, record)
        assert conn.execute("SELECT COUNT(*) FROM research_hypotheses").fetchone()[0] == len(agent.EXPERIMENT5_ALLOWED_STATUSES)


class TestOutcomeEvaluation:
    def test_agent_correct_prediction_scored_passed_holdout(self):
        conn = fresh_db()
        anchor_ts = 1000
        seed(conn, anchor_ts, {"fng": 70}, score=60, btc_price=100.0)
        seed(conn, anchor_ts + 24 * HOUR, {"fng": 70}, score=60, btc_price=110.0)  # price went UP

        record = {
            "subject": "experiment5:reversal:fng", "statement": "x", "lifecycle_status": "OBSERVATION",
            "decision": {"anchor_ts": anchor_ts, "cycle_ts": anchor_ts, "primary_source": "fng",
                         "direction": 1,  # agent predicted UP
                         "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}},
        }
        agent.persist_experiment5_decision(conn, anchor_ts, record)

        result = agent.evaluate_pending_decisions(conn, as_of_ts=anchor_ts + 25 * HOUR, horizon_hours=24)
        assert result["n_evaluated"] == 1
        outcome = result["results"][0]
        assert outcome["realized_direction"] == "UP"
        assert outcome["agent_correct"] is True

        row = conn.execute("SELECT out_of_sample_status, evidence_summary_json FROM research_hypotheses").fetchone()
        assert row[0] == "PASSED_HOLDOUT"
        payload = json.loads(row[1])
        assert "decision" in payload and "outcome" in payload  # decision key never removed

    def test_agent_wrong_prediction_scored_failed_holdout(self):
        conn = fresh_db()
        anchor_ts = 1000
        seed(conn, anchor_ts, {"fng": 70}, score=60, btc_price=100.0)
        seed(conn, anchor_ts + 24 * HOUR, {"fng": 70}, score=60, btc_price=90.0)  # price went DOWN

        record = {
            "subject": "experiment5:reversal:fng", "statement": "x", "lifecycle_status": "OBSERVATION",
            "decision": {"anchor_ts": anchor_ts, "cycle_ts": anchor_ts, "primary_source": "fng", "direction": 1,
                         "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}},
        }
        agent.persist_experiment5_decision(conn, anchor_ts, record)
        result = agent.evaluate_pending_decisions(conn, as_of_ts=anchor_ts + 25 * HOUR, horizon_hours=24)
        assert result["results"][0]["agent_correct"] is False

        row = conn.execute("SELECT out_of_sample_status FROM research_hypotheses").fetchone()
        assert row[0] == "FAILED_HOLDOUT"

    def test_v1_baseline_scored_alongside_agent(self):
        conn = fresh_db()
        anchor_ts = 1000
        seed(conn, anchor_ts, {"fng": 70}, score=30, btc_price=100.0)  # V1 composite < 50 -> bearish baseline
        seed(conn, anchor_ts + 24 * HOUR, {"fng": 70}, score=30, btc_price=90.0)  # price went DOWN

        record = {
            "subject": "experiment5:reversal:fng", "statement": "x", "lifecycle_status": "OBSERVATION",
            "decision": {"anchor_ts": anchor_ts, "cycle_ts": anchor_ts, "primary_source": "fng", "direction": -1,
                         "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}},
        }
        agent.persist_experiment5_decision(conn, anchor_ts, record)
        result = agent.evaluate_pending_decisions(conn, as_of_ts=anchor_ts + 25 * HOUR, horizon_hours=24)
        outcome = result["results"][0]
        assert outcome["v1_baseline_direction"] == "DOWN"
        assert outcome["v1_baseline_correct"] is True
        assert outcome["agent_correct"] is True  # both correct here, not a claim one beats the other

    def test_unresolved_horizon_is_left_pending_never_forced(self):
        conn = fresh_db()
        anchor_ts = 1000
        seed(conn, anchor_ts, {"fng": 70}, score=60, btc_price=100.0)
        # no future btc_data point at all -- horizon cannot resolve yet
        record = {
            "subject": "experiment5:reversal:fng", "statement": "x", "lifecycle_status": "OBSERVATION",
            "decision": {"anchor_ts": anchor_ts, "cycle_ts": anchor_ts, "primary_source": "fng", "direction": 1,
                         "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}},
        }
        agent.persist_experiment5_decision(conn, anchor_ts, record)
        result = agent.evaluate_pending_decisions(conn, as_of_ts=anchor_ts + HOUR, horizon_hours=24)
        assert result["n_evaluated"] == 0
        row = conn.execute("SELECT out_of_sample_status FROM research_hypotheses").fetchone()
        assert row[0] is None  # still pending, not fabricated

    def test_decision_key_never_rewritten_by_evaluation(self):
        conn = fresh_db()
        anchor_ts = 1000
        seed(conn, anchor_ts, {"fng": 70}, score=60, btc_price=100.0)
        seed(conn, anchor_ts + 24 * HOUR, {"fng": 70}, score=60, btc_price=110.0)
        record = {
            "subject": "experiment5:reversal:fng", "statement": "ORIGINAL STATEMENT", "lifecycle_status": "OBSERVATION",
            "decision": {"anchor_ts": anchor_ts, "cycle_ts": anchor_ts, "primary_source": "fng", "direction": 1,
                         "classifications": {"fng": {"marker": "original"}},
                         "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}},
        }
        agent.persist_experiment5_decision(conn, anchor_ts, record)
        agent.evaluate_pending_decisions(conn, as_of_ts=anchor_ts + 25 * HOUR, horizon_hours=24)
        row = conn.execute("SELECT evidence_summary_json FROM research_hypotheses").fetchone()
        payload = json.loads(row[0])
        assert payload["decision"]["classifications"]["fng"]["marker"] == "original"  # untouched


class TestCompareV1VsChallenger:
    def test_aggregate_counts_and_rates(self):
        results = [
            {"agent_correct": True, "v1_baseline_correct": True},
            {"agent_correct": True, "v1_baseline_correct": False},
            {"agent_correct": False, "v1_baseline_correct": False},
        ]
        summary = agent.compare_v1_vs_challenger(results)
        assert summary["n_decisions_evaluated"] == 3
        assert summary["agent_accuracy"] == round(2 / 3, 3)
        assert summary["v1_baseline_accuracy"] == round(1 / 3, 3)
        assert "note" in summary  # never a bare verdict without the caveat

    def test_empty_results_never_divides_by_zero(self):
        summary = agent.compare_v1_vs_challenger([])
        assert summary["agent_accuracy"] is None
        assert summary["v1_baseline_accuracy"] is None


class TestNoProductionTouch:
    def test_module_never_references_production_selection_symbols(self):
        source = inspect.getsource(agent)
        for forbidden in ["SELECTION_VARIANTS", "COMPOSITE_SOURCES_DEFAULTS", "selectBestVariant", "decideSelection"]:
            assert forbidden not in source

    def test_module_never_writes_to_history_or_btc_data(self):
        source = inspect.getsource(agent)
        assert "INSERT INTO history" not in source
        assert "INSERT INTO btc_data" not in source
        assert "DELETE FROM history" not in source
