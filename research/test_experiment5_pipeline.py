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


class TestPendingDecisionReplayOrdering:
    """Regression coverage for the fix to a real crash: previously,
    run_pipeline() replayed pre-existing pending decisions (carrying
    their REAL hypothesis_id) into the fresh local mirror AFTER
    run_agent_cycle() had already created new decisions there, whose
    local AUTOINCREMENT ids start at 1 on every run. A real pending
    hypothesis_id colliding with a same-cycle newly-assigned local id
    (very plausible early in the table's life, since both sequences
    start near 1) raised sqlite3.IntegrityError, silently halting the
    pipeline under the workflow's continue-on-error. The fix replays
    pending decisions BEFORE run_agent_cycle so SQLite's own
    AUTOINCREMENT bookkeeping guarantees no later local id can ever
    coincide with an already-used real one."""

    def test_new_decision_and_pending_decision_same_cycle_do_not_collide(self):
        d1 = FakeD1()
        anchor_ts = 0
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
        pending_id = d1.conn.execute("SELECT hypothesis_id FROM research_hypotheses").fetchone()[0]
        assert pending_id == 1  # sanity: this is the id that used to collide with the fresh local mirror's own autoincrement

        # A price at the pending decision's own 24h target (anchor 0 +
        # target_horizon_hours) -- close enough to resolve it under
        # EXPERIMENT5_HORIZON_TOLERANCE_MS. The brand-new same-cycle
        # decision (anchor 4h) is never resolved in this same run
        # regardless of this price's placement: its own eligible_ts
        # (4h + 24h = 28h) is still ahead of now_ts (25h) below, so the
        # eligibility gate alone keeps this test isolated to exactly the
        # collision scenario.
        insert_btc(d1, 0, 100.0)
        insert_btc(d1, 24 * HOUR, 110.0)

        # History that makes THIS SAME cycle's run_agent_cycle create a
        # brand-new decision (a reversal on a DIFFERENT source than the
        # pending one above).
        for i in range(4):
            insert_history(d1, i * HOUR, 50, {"onchain": 50 + i * 10})
        insert_history(d1, 4 * HOUR, 50, {"onchain": 10})

        result = ep.run_pipeline(d1.query, d1.execute, now_ts=25 * HOUR)  # must not raise

        assert result["decisions_created"] >= 1
        assert result["decisions_evaluated"] == 1

        rows = {r["hypothesis_id"]: r for r in d1.query(
            "SELECT hypothesis_id, subject, out_of_sample_status FROM research_hypotheses"
        )}
        assert len(rows) == 2  # the original pending decision + exactly one new one
        assert rows[pending_id]["out_of_sample_status"] == "PASSED_HOLDOUT"

        new_ids = [hid for hid in rows if hid != pending_id]
        assert len(new_ids) == 1
        new_id = new_ids[0]
        assert new_id != pending_id  # real D1 assigned its own, distinct id -- never colliding
        assert "reversal:onchain" in rows[new_id]["subject"]
        assert rows[new_id]["out_of_sample_status"] is None  # not (yet) evaluated -- unaffected by the fix

        # the outcome UPDATE issued against real D1 targeted only the
        # correct, pre-existing real hypothesis_id.
        update_sqls = [sql for sql in d1.executed_sql if sql.startswith("UPDATE research_hypotheses")]
        assert len(update_sqls) == 1
        assert f"WHERE hypothesis_id = {pending_id}" in update_sqls[0]

        # the new decision was written via its own plain INSERT (no
        # explicit hypothesis_id) -- never overwriting the pending row.
        insert_decision_sqls = [sql for sql in d1.executed_sql if sql.startswith("INSERT INTO research_hypotheses")]
        assert len(insert_decision_sqls) == 1
        assert "hypothesis_id" not in insert_decision_sqls[0].split("VALUES")[0]

    def test_replayed_decision_cannot_overwrite_or_leak_outcome_into_another_pending_decision(self):
        d1 = FakeD1()
        anchor_a = 0
        anchor_b = 20 * HOUR  # in the past relative to now_ts below, but its own 24h horizon has not resolved yet

        payload_a = json.dumps({"decision": {
            "anchor_ts": anchor_a, "cycle_ts": anchor_a, "primary_source": "fng", "direction": 1,
            "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []},
        }})
        payload_b = json.dumps({"decision": {
            "anchor_ts": anchor_b, "cycle_ts": anchor_b, "primary_source": "etfflows", "direction": -1,
            "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []},
        }})
        d1.conn.execute(
            "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, "
            "source_analysis_ids, status, evidence_summary_json, out_of_sample_status) "
            "VALUES (?, ?, 'experiment5:reversal:fng', 'x', '[]', 'OBSERVATION', ?, NULL)",
            (anchor_a, anchor_a, payload_a),
        )
        d1.conn.execute(
            "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, "
            "source_analysis_ids, status, evidence_summary_json, out_of_sample_status) "
            "VALUES (?, ?, 'experiment5:reversal:etfflows', 'x', '[]', 'OBSERVATION', ?, NULL)",
            (anchor_b, anchor_b, payload_b),
        )
        d1.conn.commit()
        id_a, id_b = [r[0] for r in d1.conn.execute(
            "SELECT hypothesis_id FROM research_hypotheses ORDER BY hypothesis_id"
        ).fetchall()]

        # A minimal history row at A's own anchor so outcome_engine's
        # history-anchored query has a row to anchor on at all; none at
        # B's anchor -- either way (missing anchor row, or an anchor row
        # with no qualifying future price) resolves to "leave pending,
        # never fabricate," so B is exercised via the "no history row"
        # path here, which evaluate_pending_decisions treats identically.
        insert_history(d1, anchor_a, 60, {"fng": 60})
        insert_btc(d1, anchor_a, 100.0)
        # A price at A's own 24h target -- resolves A under
        # EXPERIMENT5_HORIZON_TOLERANCE_MS. B (anchor_b=20h) is never
        # resolved in this same run regardless: its own eligible_ts
        # (20h + 24h = 44h) is still ahead of now_ts (25h) below.
        insert_btc(d1, anchor_a + 24 * HOUR, 110.0)

        result = ep.run_pipeline(d1.query, d1.execute, now_ts=anchor_a + 25 * HOUR)

        assert result["decisions_evaluated"] == 1
        rows = {r["hypothesis_id"]: r for r in d1.query(
            "SELECT hypothesis_id, out_of_sample_status, evidence_summary_json FROM research_hypotheses"
        )}

        assert rows[id_a]["out_of_sample_status"] == "PASSED_HOLDOUT"
        assert "outcome" in json.loads(rows[id_a]["evidence_summary_json"])

        # B is untouched: still pending, no outcome fabricated or leaked
        # from A's own resolution, and its original decision payload is
        # exactly what was seeded.
        assert rows[id_b]["out_of_sample_status"] is None
        payload_b_after = json.loads(rows[id_b]["evidence_summary_json"])
        assert "outcome" not in payload_b_after
        assert payload_b_after["decision"]["primary_source"] == "etfflows"

        update_sqls = [sql for sql in d1.executed_sql if sql.startswith("UPDATE research_hypotheses")]
        assert len(update_sqls) == 1
        assert f"WHERE hypothesis_id = {id_a}" in update_sqls[0]
        assert f"WHERE hypothesis_id = {id_b}" not in update_sqls[0]


class TestArchivedTsProvenance:
    def test_archived_ts_reflects_actual_pipeline_run_time_not_observation_time(self):
        d1 = FakeD1()
        observation_ts = 0
        now_ts = 100 * HOUR  # this pipeline run happens long after the observation itself (a catch-up run)
        insert_history(d1, observation_ts, 55, {"fng": 55})

        result = ep.run_pipeline(d1.query, d1.execute, now_ts=now_ts)

        assert result["newly_archived"] == 1
        row = d1.query("SELECT observation_ts, archived_ts FROM research_sentiment_archive")[0]
        assert row["observation_ts"] == observation_ts
        assert row["archived_ts"] == now_ts
        assert row["archived_ts"] != row["observation_ts"]


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
