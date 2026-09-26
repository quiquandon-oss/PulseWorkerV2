"""
Tests for research/experiment5_pipeline.py.

All D1 access is injected via `d1_query_fn`/`d1_execute_fn`, backed here
by a REAL in-memory sqlite3 connection (same SQL dialect D1 itself
uses) -- this exercises the actual SQL strings the module builds, not a
hand-rolled approximation. Mirrors test_live_evidence_pipeline.py's own
FakeD1 convention exactly. Zero network access anywhere in this file.

Run with: python3 -m pytest research/test_experiment5_pipeline.py -v
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
import experiment5_pipeline as ep  # noqa: E402

HOUR = 3600000
DAY = 24 * HOUR


class FakeD1:
    """Wraps a real in-memory sqlite3 connection with the same
    (query, execute) shape run_pipeline() expects -- the module's real
    SQL strings are actually parsed and run against real sqlite, not
    approximated."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""CREATE TABLE history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL,
            sources_json TEXT, technical_score INTEGER, gold_regime TEXT
        )""")
        self.conn.execute("""CREATE TABLE btc_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
        )""")
        self.conn.execute("""CREATE TABLE research_sentiment_archive (
            archive_id INTEGER PRIMARY KEY AUTOINCREMENT, observation_ts INTEGER NOT NULL,
            sources_json TEXT, score REAL, technical_score REAL, btc_price REAL, gold_regime TEXT,
            source_weights_version TEXT NOT NULL, schema_version TEXT NOT NULL, written_by TEXT NOT NULL,
            content_hash TEXT NOT NULL, archived_ts INTEGER NOT NULL
        )""")
        self.conn.execute("CREATE UNIQUE INDEX idx_archive_ts ON research_sentiment_archive(observation_ts)")
        self.conn.execute("""CREATE TABLE research_hypotheses (
            hypothesis_id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts INTEGER NOT NULL,
            last_updated_ts INTEGER NOT NULL, subject TEXT NOT NULL, statement TEXT NOT NULL,
            source_analysis_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OBSERVATION',
            evidence_summary_json TEXT, out_of_sample_status TEXT
        )""")
        self.executed_sql = []

    def query(self, sql):
        cursor = self.conn.execute(sql)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def execute(self, sql):
        self.executed_sql.append(sql)
        self.conn.execute(sql)
        self.conn.commit()


def insert_history(d1, ts, score, sources, technical_score=None, gold_regime=None):
    d1.conn.execute(
        "INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?, ?, ?, ?, ?)",
        (ts, score, json.dumps(sources), technical_score, gold_regime),
    )
    d1.conn.commit()


def insert_btc(d1, ts, price):
    d1.conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))
    d1.conn.commit()


class TestArchiving:
    def test_history_rows_get_archived_to_real_d1_via_execute(self):
        d1 = FakeD1()
        for i in range(3):
            insert_history(d1, i * HOUR, 50 + i, {"fng": 50 + i})
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=2 * HOUR)
        assert result["newly_archived"] == 3
        archived = d1.query("SELECT observation_ts FROM research_sentiment_archive ORDER BY observation_ts")
        assert [r["observation_ts"] for r in archived] == [0, HOUR, 2 * HOUR]

    def test_second_run_does_not_re_archive_already_known_rows(self):
        d1 = FakeD1()
        for i in range(3):
            insert_history(d1, i * HOUR, 50 + i, {"fng": 50 + i})
        ep.run_pipeline(d1.query, d1.execute, now_ts=2 * HOUR)
        # a later run sees the same rows plus one new one
        insert_history(d1, 3 * HOUR, 53, {"fng": 53})
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=3 * HOUR)
        assert result["newly_archived"] == 1  # only the genuinely new row
        n_total = d1.query("SELECT COUNT(*) as n FROM research_sentiment_archive")[0]["n"]
        assert n_total == 4  # never duplicated

    def test_archived_sources_json_matches_original_exactly(self):
        d1 = FakeD1()
        insert_history(d1, 0, 60, {"fng": 71, "onchain": 12})
        insert_history(d1, HOUR, 61, {"fng": 72, "onchain": 13})
        ep.run_pipeline(d1.query, d1.execute, now_ts=HOUR)
        row = d1.query("SELECT sources_json FROM research_sentiment_archive WHERE observation_ts = 0")[0]
        assert json.loads(row["sources_json"]) == {"fng": 71, "onchain": 12}


class TestAgentCycleIntegration:
    def test_cross_source_confirmation_produces_a_real_hypothesis_row(self):
        d1 = FakeD1()
        for i in range(4):
            insert_history(d1, i * HOUR, 50, {"fng": 50 + i * 5, "etfflows": 40 + i * 5})
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=3 * HOUR)
        assert result["decisions_created"] >= 1
        rows = d1.query("SELECT subject, status FROM research_hypotheses")
        assert any("cross_source_confirmation" in r["subject"] for r in rows)

    def test_decision_rows_written_via_execute_not_bypassing_it(self):
        d1 = FakeD1()
        for i in range(4):
            insert_history(d1, i * HOUR, 50, {"fng": 50 + i * 5, "etfflows": 40 + i * 5})
        ep.run_pipeline(d1.query, d1.execute, now_ts=3 * HOUR)
        assert any("INSERT INTO research_hypotheses" in sql for sql in d1.executed_sql)


class TestOutcomeEvaluationIntegration:
    def test_pending_decision_gets_evaluated_and_written_back(self):
        d1 = FakeD1()
        anchor_ts = 0
        insert_history(d1, anchor_ts, 60, {"fng": 60})
        insert_btc(d1, anchor_ts, 100.0)
        insert_btc(d1, anchor_ts + 24 * HOUR, 110.0)

        # Seed a real pending Experiment 5 decision directly into D1, as
        # if a PRIOR run had created it.
        decision_payload = json.dumps({"decision": {
            "anchor_ts": anchor_ts, "cycle_ts": anchor_ts, "primary_source": "fng",
            "direction": 1, "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []},
        }})
        d1.conn.execute(
            "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, "
            "source_analysis_ids, status, evidence_summary_json, out_of_sample_status) "
            "VALUES (?, ?, 'experiment5:reversal:fng', 'x', '[]', 'OBSERVATION', ?, NULL)",
            (anchor_ts, anchor_ts, decision_payload),
        )
        d1.conn.commit()

        result = ep.run_pipeline(d1.query, d1.execute, now_ts=anchor_ts + 25 * HOUR)
        assert result["decisions_evaluated"] == 1

        row = d1.query("SELECT out_of_sample_status, evidence_summary_json FROM research_hypotheses")[0]
        assert row["out_of_sample_status"] == "PASSED_HOLDOUT"
        payload = json.loads(row["evidence_summary_json"])
        assert payload["decision"]["primary_source"] == "fng"  # original decision untouched
        assert payload["outcome"]["realized_direction"] == "UP"

    def test_update_written_via_execute_uses_update_statement(self):
        d1 = FakeD1()
        anchor_ts = 0
        insert_history(d1, anchor_ts, 60, {"fng": 60})
        insert_btc(d1, anchor_ts, 100.0)
        insert_btc(d1, anchor_ts + 24 * HOUR, 110.0)
        decision_payload = json.dumps({"decision": {
            "anchor_ts": anchor_ts, "cycle_ts": anchor_ts, "primary_source": "fng", "direction": 1,
            "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []},
        }})
        d1.conn.execute(
            "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, "
            "source_analysis_ids, status, evidence_summary_json, out_of_sample_status) "
            "VALUES (?, ?, 'experiment5:reversal:fng', 'x', '[]', 'OBSERVATION', ?, NULL)",
            (anchor_ts, anchor_ts, decision_payload),
        )
        d1.conn.commit()
        ep.run_pipeline(d1.query, d1.execute, now_ts=anchor_ts + 25 * HOUR)
        assert any(sql.startswith("UPDATE research_hypotheses") for sql in d1.executed_sql)


class TestSqlBuilders:
    def test_build_insert_archive_sql_round_trips(self):
        row = {
            "observation_ts": 1000, "sources_json": '{"fng":50}', "score": 55, "technical_score": None,
            "btc_price": 100.0, "gold_regime": None, "source_weights_version": "v1", "schema_version": "exp5-v1",
            "written_by": "test", "content_hash": "abc123", "archived_ts": 1000,
        }
        sql = ep.build_insert_archive_sql(row)
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE research_sentiment_archive (
            archive_id INTEGER PRIMARY KEY AUTOINCREMENT, observation_ts INTEGER NOT NULL,
            sources_json TEXT, score REAL, technical_score REAL, btc_price REAL, gold_regime TEXT,
            source_weights_version TEXT NOT NULL, schema_version TEXT NOT NULL, written_by TEXT NOT NULL,
            content_hash TEXT NOT NULL, archived_ts INTEGER NOT NULL
        )""")
        conn.execute(sql)
        result = conn.execute("SELECT observation_ts, score FROM research_sentiment_archive").fetchone()
        assert result == (1000, 55.0)

    def test_sql_literal_escapes_single_quotes(self):
        assert ep._sql_literal("O'Brien") == "'O''Brien'"

    def test_sql_literal_null_for_none(self):
        assert ep._sql_literal(None) == "NULL"
