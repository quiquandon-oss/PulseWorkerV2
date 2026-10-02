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
import re
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import experiment5_agent as agent  # noqa: E402
import experiment5_pipeline as ep  # noqa: E402
import sentiment_archive as sa  # noqa: E402

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


def seed_archive(d1, observation_ts, score, sources, archived_ts, technical_score=None,
                  btc_price=None, gold_regime=None):
    """Writes directly into the FakeD1's real research_sentiment_archive
    table via sentiment_archive.archive_observation() -- simulating an
    observation a PRIOR pipeline run already archived, exactly as real
    production D1 holds after a backfill. Never goes through
    run_pipeline/the mirror -- this is what "already there before this
    run started" means for these tests."""
    sa.archive_observation(
        d1.conn, observation_ts, sources, score, technical_score=technical_score,
        btc_price=btc_price, gold_regime=gold_regime, source_weights_version="v1-unversioned",
        written_by="prior-run", archived_ts=archived_ts,
    )
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


class TestHistoricalObservationWindow:
    """Regression coverage for the steady-state observation-window
    defect found by the production audit: two real, unmasked runs 89
    minutes apart on 2026-09-26 showed the first archiving 422
    observations and the agent classifying normally, then the second
    archiving 0 new rows and returning INSUFFICIENT_ARCHIVE_DATA despite
    422 real rows still sitting in production D1. Root cause:
    _build_local_mirror only ever populated the mirror's
    research_sentiment_archive table with rows THIS run newly archived,
    never with rows a prior run already archived into real D1 -- so
    agent.run_agent_cycle's 14-day observe() window collapsed to "this
    cycle's fresh delta" in steady state. See experiment5_pipeline.py's
    own module docstring, "Steady-state observation window"."""

    def test_historical_archive_populates_mirror_when_nothing_newly_archived(self):
        # No history rows at all -- isolates the historical-replay path
        # from the newly-archived path entirely. Reproduces run #28's
        # exact real-world shape: newly_archived == 0, real archive
        # non-empty.
        d1 = FakeD1()
        seed_archive(d1, 0, 50, {"fng": 50}, archived_ts=0)
        seed_archive(d1, HOUR, 55, {"fng": 55}, archived_ts=0)
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=2 * HOUR)
        assert result["newly_archived"] == 0
        assert result["agent_cycle"]["status"] == "OK"  # not INSUFFICIENT_ARCHIVE_DATA
        assert result["agent_cycle"]["n_archive_rows"] == 2

    def test_only_rows_within_the_14_day_observe_window_are_observed(self):
        d1 = FakeD1()
        window = ep.agent.DEFAULT_OBSERVE_WINDOW_MS
        now_ts = 20 * DAY
        seed_archive(d1, now_ts - window - HOUR, 50, {"fng": 50}, archived_ts=0)  # just outside
        seed_archive(d1, now_ts - window + HOUR, 51, {"fng": 51}, archived_ts=0)  # just inside
        seed_archive(d1, now_ts - HOUR, 52, {"fng": 52}, archived_ts=0)           # well inside
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=now_ts)
        assert result["agent_cycle"]["n_archive_rows"] == 2  # the too-old row is excluded

    def test_defensive_backstop_holds_under_a_hypothetical_caller_mismatch(self):
        # NOT the mainline Case A/B scenario (see TestDuplicateHandlingContract
        # below for those, exercised via real run_pipeline paths). This is a
        # SEPARATE, defensive check: deliberately inconsistent inputs at the
        # _build_local_mirror level, where this ts is claimed already-archived
        # (present in historical_archive_rows) but NOT reflected in
        # archived_observation_ts (the idempotency set the newly-archived loop
        # actually checks). run_pipeline itself can never produce this exact
        # mismatch (both sets come from the same real D1 read, and the 14-day
        # historical window is always a subset of the 30-day
        # archived_observation_ts window), but archive_observation()'s own
        # existing-row check happens to still catch it here -- this test only
        # proves that incidental fact, not a designed contract for this
        # specific mismatch shape.
        ts = 5 * HOUR
        sources_json_text = '{"fng":60}'
        historical_row = {
            "observation_ts": ts, "sources_json": sources_json_text, "score": 60.0,
            "technical_score": None, "btc_price": None, "gold_regime": None,
            "source_weights_version": "v1-unversioned", "schema_version": sa.SCHEMA_VERSION,
            "written_by": "prior-run",
            "content_hash": sa.compute_content_hash(
                ts, sources_json_text, 60.0, None, None, None, "v1-unversioned", sa.SCHEMA_VERSION,
            ),
            "archived_ts": 0,
        }
        history_rows = [{"ts": ts, "score": 60.0, "sources_json": sources_json_text,
                          "technical_score": None, "gold_regime": None}]
        conn = ep._build_local_mirror(
            history_rows=history_rows, btc_rows=[], archived_observation_ts=set(),
            now_ts=ts, historical_archive_rows=[historical_row],
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM research_sentiment_archive WHERE observation_ts = ?", (ts,)
        ).fetchone()[0]
        assert count == 1  # never duplicated, even though archived_observation_ts disagreed

    def test_repeat_run_after_backfill_still_observes_historical_data_and_stays_idempotent(self):
        d1 = FakeD1()
        for i in range(4):
            insert_history(d1, i * HOUR, 50, {"fng": 50 + i * 5, "etfflows": 40 + i * 5})
        first = ep.run_pipeline(d1.query, d1.execute, now_ts=3 * HOUR)
        assert first["newly_archived"] == 4
        archive_count_after_first = d1.query("SELECT COUNT(*) as n FROM research_sentiment_archive")[0]["n"]

        # A second run 10 minutes later, no new history at all -- exactly
        # run #28's real production shape.
        sql_count_before_second = len(d1.executed_sql)
        second = ep.run_pipeline(d1.query, d1.execute, now_ts=3 * HOUR + 10 * 60000)
        assert second["newly_archived"] == 0
        # The agent sees the SAME historical rows, not an empty archive.
        assert second["agent_cycle"]["n_archive_rows"] == first["agent_cycle"]["n_archive_rows"]
        archive_count_after_second = d1.query("SELECT COUNT(*) as n FROM research_sentiment_archive")[0]["n"]
        assert archive_count_after_second == archive_count_after_first  # no duplicates written
        # Not one of the 4 already-historical rows was fed back through
        # the newly-archived write path on the second run.
        new_sql = d1.executed_sql[sql_count_before_second:]
        assert not any(sql.startswith("INSERT INTO research_sentiment_archive") for sql in new_sql)

    def test_insufficient_archive_data_still_reported_when_genuinely_insufficient(self):
        # Only 1 real archived row and no history at all -- the fix must
        # not fabricate a second observation to force an "OK" status.
        d1 = FakeD1()
        seed_archive(d1, 0, 50, {"fng": 50}, archived_ts=0)
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=HOUR)
        assert result["newly_archived"] == 0
        assert result["agent_cycle"]["status"] == "INSUFFICIENT_ARCHIVE_DATA"
        assert result["agent_cycle"]["n_archive_rows"] == 1


class TestDuplicateHandlingContract:
    """Verifies, separately and via the actual reachable code paths, the
    three duplicate-handling claims made for the historical-observation-
    window fix. Read _build_local_mirror's own history_rows loop first:
    a history row is only ever passed to sentiment_archive.archive_observation()
    when its ts is NOT in archived_observation_ts. Since archived_observation_ts
    and historical_archive_rows are both read from the SAME real D1 archive
    table (one ts-only over 30 days, the other full-row over 14 days, the
    narrower always a subset of the wider), a ts already covered by
    historical_archive_rows is ALWAYS also in archived_observation_ts --
    so it is skipped, never re-offered to archive_observation. This means
    the only way archive_observation's own conflict/no-op check is ever
    reached in a REAL run_pipeline call is a genuine duplicate ts WITHIN
    the `history` table itself (not yet archived at all), not any
    interaction between the historical and newly-archived paths -- Cases
    A and B below test exactly that, the actually-reachable shape.
    sentiment_archive.archive_observation()'s own no-op/conflict contract
    already has direct unit coverage in test_sentiment_archive.py; these
    tests instead verify how experiment5_pipeline.py's own run_pipeline
    integrates with that contract."""

    def test_case_a_identical_duplicate_within_history_is_idempotent_noop(self):
        # Two history rows, same ts, BYTE-IDENTICAL content -- the only
        # path through which a duplicate ts within a single run reaches
        # archive_observation() at all. Expected, explicit contract: an
        # idempotent no-op (one archive row, not two), never a raised
        # error -- this is NOT the same code path as the mirror's own
        # UNIQUE-index hard-fail (see
        # test_case_a_mirror_level_bare_insert_hard_fails_on_any_duplicate
        # below), so the two are deliberately tested separately rather
        # than assumed interchangeable.
        d1 = FakeD1()
        ts = 2 * HOUR
        insert_history(d1, ts, 60, {"fng": 60})
        insert_history(d1, ts, 60, {"fng": 60})  # exact duplicate row
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=ts + HOUR)
        assert result["newly_archived"] == 1  # counted once, never twice
        rows = d1.query("SELECT COUNT(*) as n FROM research_sentiment_archive")
        assert rows[0]["n"] == 1  # never silently duplicated

    def test_case_b_conflicting_duplicate_within_history_raises_and_writes_nothing(self):
        # Two history rows, same ts, DIFFERENT content -- must never be
        # silently resolved either direction. Expected, explicit
        # contract: sentiment_archive.ArchiveConflictError (a specific,
        # catchable type -- not a bare sqlite3.IntegrityError), raised
        # before run_pipeline ever calls d1_execute_fn, so real D1 is
        # never touched and nothing is overwritten.
        d1 = FakeD1()
        ts = 2 * HOUR
        insert_history(d1, ts, 60, {"fng": 60})  # content A
        insert_history(d1, ts, 99, {"fng": 99})  # same ts, conflicting content B
        with pytest.raises(sa.ArchiveConflictError):
            ep.run_pipeline(d1.query, d1.execute, now_ts=ts + HOUR)
        assert d1.executed_sql == []  # aborted before any real-D1 write was ever issued
        rows = d1.query("SELECT COUNT(*) as n FROM research_sentiment_archive")
        assert rows[0]["n"] == 0  # no partial/overwritten row left behind

    def test_case_a_mirror_level_bare_insert_hard_fails_on_any_duplicate(self):
        # Directly proves the OTHER claimed safeguard: historical_archive_rows
        # is replayed via a bare INSERT (no existing-row pre-check at all --
        # see _build_local_mirror), relying entirely on the mirror's own
        # UNIQUE index on observation_ts as the backstop. Real run_pipeline
        # can never actually produce two historical_archive_rows sharing a
        # ts (real D1's own UNIQUE index on research_sentiment_archive
        # prevents that SELECT from ever returning duplicates), so this is
        # a direct _build_local_mirror-level test of the backstop itself,
        # confirming it is a HARD FAILURE (sqlite3.IntegrityError) -- not
        # an idempotent no-op like Case A above -- for this specific
        # insertion path, whether the two rows are identical or
        # conflicting (a bare INSERT does not distinguish the two).
        ts = 3 * HOUR
        row_a = {
            "observation_ts": ts, "sources_json": '{"fng":10}', "score": 10.0,
            "technical_score": None, "btc_price": None, "gold_regime": None,
            "source_weights_version": "v1-unversioned", "schema_version": sa.SCHEMA_VERSION,
            "written_by": "prior-run", "content_hash": "hash-a", "archived_ts": 0,
        }
        row_b = dict(row_a, content_hash="hash-b")  # same ts; content_hash differs, but even
        # an identical copy of row_a here would fail identically -- the
        # bare INSERT has no content-aware branch at all.
        with pytest.raises(sqlite3.IntegrityError):
            ep._build_local_mirror(
                history_rows=[], btc_rows=[], archived_observation_ts=set(), now_ts=ts,
                historical_archive_rows=[row_a, row_b],
            )

    def test_case_c_normal_run_never_feeds_the_same_ts_through_both_paths(self):
        # The realistic disjointness guarantee: history rows whose ts is
        # already archived are excluded from the newly-archived set on a
        # normal run_pipeline call (not an artificial _build_local_mirror
        # input, unlike the defensive test above).
        d1 = FakeD1()
        ts_a, ts_b = 0, HOUR
        seed_archive(d1, ts_a, 50, {"fng": 50}, archived_ts=0)  # already archived by a prior run
        insert_history(d1, ts_a, 50, {"fng": 50})  # history still carries the old, already-archived row
        insert_history(d1, ts_b, 55, {"fng": 55})  # genuinely new, unarchived row
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=ts_b + HOUR)
        assert result["newly_archived"] == 1  # only ts_b -- ts_a was never re-offered
        insert_sqls = [sql for sql in d1.executed_sql if sql.startswith("INSERT INTO research_sentiment_archive")]
        assert len(insert_sqls) == 1
        # observation_ts is the first VALUES column (see ARCHIVE_COLUMNS) --
        # this pins the single INSERT to ts_b specifically, not merely to
        # "some" ts appearing anywhere in the string (0 is a misleading
        # substring of ts_b=3600000, so a bare `in` check on ts_a would be
        # meaningless here).
        assert insert_sqls[0].split("VALUES (", 1)[1].startswith(f"{ts_b},")
        rows = d1.query("SELECT COUNT(*) as n FROM research_sentiment_archive")
        assert rows[0]["n"] == 2  # ts_a (from the seed) + ts_b (newly written) -- never duplicated


class TestReplayCollisionReproduction:
    """Reproduces the ORIGINAL defect from first principles (not just asserting the fix)
    so a future refactor that reintroduces the old ordering is caught by a test that
    demonstrably fails for the right reason."""

    def _reversal_history(self, d1):
        for i in range(4):
            insert_history(d1, i * HOUR, 50, {"onchain": 50 + i * 10})
        insert_history(d1, 4 * HOUR, 50, {"onchain": 10})

    def test_the_old_ordering_raises_integrity_error_on_the_shared_hypothesis_id(self):
        d1 = FakeD1()
        self._reversal_history(d1)
        history_rows = d1.query("SELECT ts, score, sources_json, technical_score, gold_regime FROM history ORDER BY ts")
        conn = ep._build_local_mirror(history_rows, [], set(), now_ts=25 * HOUR)

        # OLD order: the agent cycle ran FIRST, so its new decision took local AUTOINCREMENT id 1 ...
        cycle = agent.run_agent_cycle(conn, as_of_ts=25 * HOUR, created_ts=25 * HOUR)
        assert cycle["decision_ids"] == [1]

        # ... and replaying a pending decision that carries REAL hypothesis_id 1 afterwards collided.
        with pytest.raises(sqlite3.IntegrityError, match="hypothesis_id"):
            conn.execute(
                "INSERT INTO research_hypotheses (hypothesis_id, created_ts, last_updated_ts, subject, statement, "
                "source_analysis_ids, status, evidence_summary_json, out_of_sample_status) "
                "VALUES (1, 0, 0, 'experiment5:replayed', 'x', '[]', 'OBSERVATION', '{}', NULL)"
            )

    def test_the_current_pipeline_does_not_raise_in_the_same_scenario(self):
        d1 = FakeD1()
        self._reversal_history(d1)
        payload = json.dumps({"decision": {"anchor_ts": 0, "cycle_ts": 0, "primary_source": "fng", "direction": 1,
                                           "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}}})
        d1.conn.execute(
            "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, source_analysis_ids, status, "
            "evidence_summary_json, out_of_sample_status) VALUES (0, 0, 'experiment5:reversal:fng', 'x', '[]', 'OBSERVATION', ?, NULL)",
            (payload,),
        )
        d1.conn.commit()
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=25 * HOUR)
        assert result["status"] == "OK" and result["decisions_created"] == 1


class TestSameRunDecisionIdAliasing:
    """A decision created by THIS run lives under a local, throwaway AUTOINCREMENT id; real D1
    assigns its own id on INSERT. If such a decision were also evaluated in the same run (a
    delayed/catch-up run whose newest observation is already older than the horizon), its outcome
    UPDATE used the LOCAL id and could overwrite a DIFFERENT real row. New decisions are therefore
    deferred to the next run, where they are replayed under their REAL id."""

    ORIGINAL_OUTCOME = {"marker": "ORIGINAL-RESOLVED-OUTCOME", "evaluated_ts": 1}

    def _seed(self):
        d1 = FakeD1()
        # real id 1: pending, not yet eligible (anchored 33h, horizon 24h, now = 34h)
        pending = json.dumps({"decision": {"anchor_ts": 33 * HOUR, "cycle_ts": 33 * HOUR, "primary_source": "fng", "direction": 1,
                                           "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}}})
        d1.conn.execute(
            "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, source_analysis_ids, status, "
            "evidence_summary_json, out_of_sample_status) VALUES (33, 33, 'experiment5:reversal:fng', 'x', '[]', 'OBSERVATION', ?, NULL)",
            (pending,),
        )
        # real id 2: ALREADY resolved earlier -- so it is NOT replayed, and the local mirror's counter
        # stops at 1 after replaying id 1: a new local decision would be numbered 2, aliasing this row.
        resolved = json.dumps({"decision": {"anchor_ts": 0, "cycle_ts": 0, "primary_source": "etfflows", "direction": -1,
                                            "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}},
                               "outcome": self.ORIGINAL_OUTCOME})
        d1.conn.execute(
            "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, source_analysis_ids, status, "
            "evidence_summary_json, out_of_sample_status) VALUES (1, 1, 'experiment5:reversal:etfflows', 'x', '[]', 'OBSERVATION', ?, 'FAILED_HOLDOUT')",
            (resolved,),
        )
        d1.conn.commit()
        # A stale newest observation (4h) so the reversal decision created this run is ANCHORED 4h and
        # already past its 24h horizon at now=34h; prices exist at the anchor and at anchor+24h.
        for i in range(4):
            insert_history(d1, i * HOUR, 50, {"onchain": 50 + i * 10})
        insert_history(d1, 4 * HOUR, 50, {"onchain": 10})
        insert_btc(d1, 4 * HOUR, 100.0)
        insert_btc(d1, 28 * HOUR, 110.0)
        return d1

    def _payload(self, d1, hypothesis_id):
        row = d1.query(f"SELECT evidence_summary_json, out_of_sample_status FROM research_hypotheses WHERE hypothesis_id = {hypothesis_id}")[0]
        return json.loads(row["evidence_summary_json"]), row["out_of_sample_status"]

    def test_a_decision_created_and_immediately_resolvable_never_updates_another_real_row(self):
        d1 = self._seed()
        before, before_status = self._payload(d1, 2)
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=34 * HOUR)

        assert result["decisions_created"] == 1
        after, after_status = self._payload(d1, 2)
        assert after == before and after["outcome"] == self.ORIGINAL_OUTCOME, "real row 2 was overwritten by the new decision's outcome"
        assert after_status == before_status == "FAILED_HOLDOUT"
        updates = [sql for sql in d1.executed_sql if sql.startswith("UPDATE research_hypotheses")]
        assert all("WHERE hypothesis_id = 2" not in sql for sql in updates)

    def test_the_new_decision_is_evaluated_on_the_next_run_under_its_REAL_id(self):
        d1 = self._seed()
        ep.run_pipeline(d1.query, d1.execute, now_ts=34 * HOUR)
        new_rows = d1.query("SELECT hypothesis_id FROM research_hypotheses WHERE subject LIKE 'experiment5:reversal:onchain'")
        assert len(new_rows) == 1
        real_new_id = new_rows[0]["hypothesis_id"]
        assert real_new_id == 3  # real D1 assigned its own id, distinct from local id 2

        second = ep.run_pipeline(d1.query, d1.execute, now_ts=35 * HOUR)
        assert second["decisions_evaluated"] == 1
        payload, status = self._payload(d1, real_new_id)
        assert "outcome" in payload and status in ("PASSED_HOLDOUT", "FAILED_HOLDOUT", "INSUFFICIENT_DATA_FOR_HOLDOUT")
        row2, _ = self._payload(d1, 2)
        assert row2["outcome"] == self.ORIGINAL_OUTCOME


class TestDeterminism:
    """Identical inputs + an identical supplied now_ts must reproduce identical writes, and no
    deterministic module may read the wall clock (the runner script supplies now_ts once)."""

    def _scenario(self):
        d1 = FakeD1()
        for i in range(4):
            insert_history(d1, i * HOUR, 50 + i, {"onchain": 50 + i * 10, "fng": 40 + i})
        insert_history(d1, 4 * HOUR, 50, {"onchain": 10, "fng": 45})
        insert_btc(d1, 4 * HOUR, 100.0)
        insert_btc(d1, 28 * HOUR, 110.0)
        payload = json.dumps({"decision": {"anchor_ts": 0, "cycle_ts": 0, "primary_source": "fng", "direction": 1,
                                           "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []}}})
        d1.conn.execute(
            "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, source_analysis_ids, status, "
            "evidence_summary_json, out_of_sample_status) VALUES (0, 0, 'experiment5:reversal:fng', 'x', '[]', 'OBSERVATION', ?, NULL)",
            (payload,),
        )
        d1.conn.commit()
        return d1

    def test_identical_inputs_and_now_ts_produce_identical_results_and_identical_sql(self):
        first, second = self._scenario(), self._scenario()
        r1 = ep.run_pipeline(first.query, first.execute, now_ts=34 * HOUR)
        r2 = ep.run_pipeline(second.query, second.execute, now_ts=34 * HOUR)
        assert r1 == r2
        assert first.executed_sql == second.executed_sql
        assert len(first.executed_sql) > 0

    def test_repeating_the_same_run_writes_nothing_new_to_the_archive(self):
        d1 = self._scenario()
        ep.run_pipeline(d1.query, d1.execute, now_ts=34 * HOUR)
        archived = d1.query("SELECT observation_ts, archived_ts, content_hash FROM research_sentiment_archive ORDER BY observation_ts")
        again = ep.run_pipeline(d1.query, d1.execute, now_ts=34 * HOUR)
        assert again["newly_archived"] == 0
        assert d1.query("SELECT observation_ts, archived_ts, content_hash FROM research_sentiment_archive ORDER BY observation_ts") == archived

    def test_every_archived_row_carries_exactly_the_supplied_now_ts(self):
        d1 = self._scenario()
        ep.run_pipeline(d1.query, d1.execute, now_ts=34 * HOUR)
        rows = d1.query("SELECT observation_ts, archived_ts FROM research_sentiment_archive")
        assert len(rows) == 5
        assert {r["archived_ts"] for r in rows} == {34 * HOUR}
        assert {r["observation_ts"] for r in rows} == {i * HOUR for i in range(5)}

    def test_content_hash_does_not_depend_on_when_the_run_happened(self):
        a, b = self._scenario(), self._scenario()
        ep.run_pipeline(a.query, a.execute, now_ts=34 * HOUR)
        ep.run_pipeline(b.query, b.execute, now_ts=90 * HOUR)
        hashes = lambda d: d.query("SELECT observation_ts, content_hash FROM research_sentiment_archive ORDER BY observation_ts")
        assert hashes(a) == hashes(b)

    def test_the_wall_clock_is_never_read_during_a_run(self, monkeypatch):
        import time

        def forbidden(*a, **k):
            raise AssertionError("the wall clock was read inside run_pipeline")

        d1 = self._scenario()
        monkeypatch.setattr(time, "time", forbidden)
        monkeypatch.setattr(time, "time_ns", forbidden)
        ep.run_pipeline(d1.query, d1.execute, now_ts=34 * HOUR)

    def test_no_deterministic_module_calls_the_wall_clock(self):
        # AST-based: inspects real call expressions, so a docstring that merely MENTIONS time.time()
        # (as sentiment_archive.py's does, to document that it never calls it) is not a false positive.
        import ast
        here = os.path.dirname(__file__)
        forbidden = {"time.time", "time.time_ns", "datetime.now", "datetime.utcnow", "datetime.today",
                     "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today", "date.today",
                     "datetime.date.today"}

        def dotted(node):
            parts = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
            return ".".join(reversed(parts))

        offenders = []
        for name in ("experiment5_agent.py", "experiment5_pipeline.py", "sentiment_archive.py",
                     "source_dynamics.py", "source_intelligence.py"):
            tree = ast.parse(open(os.path.join(here, name)).read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and dotted(node.func) in forbidden:
                    offenders.append(f"{name}:{node.lineno} {dotted(node.func)}()")
        assert offenders == [], offenders

    def test_the_scan_really_detects_a_wall_clock_call(self):
        # Guards the guard: the same detector must flag a genuine call.
        import ast
        tree = ast.parse("import time\nx = time.time()\n")
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        assert len(calls) == 1 and isinstance(calls[0].func, ast.Attribute) and calls[0].func.attr == "time"


MIGRATION_0019 = os.path.join(os.path.dirname(__file__), "..", ".ai", "migrations", "0019_experiment5_pipeline_runs.sql")


def apply_run_table_migration(d1):
    with open(MIGRATION_0019, encoding="utf-8") as fh:
        d1.conn.executescript(fh.read())


class TestSourcesClassification:
    @pytest.mark.parametrize("raw,expected", [
        (None, "EMPTY"), ("", "EMPTY"), ("   ", "EMPTY"), ("{}", "EMPTY"),
        ('{"fng": 50}', "OK"),
        ("{not json", "MALFORMED"),
        ("[1, 2]", "NOT_AN_OBJECT"), ('"text"', "NOT_AN_OBJECT"), ("42", "NOT_AN_OBJECT"),
    ])
    def test_classification(self, raw, expected):
        assert ep.classify_sources_json(raw) == expected


class TestMalformedObservationIsolation:
    def test_malformed_history_row_is_excluded_reported_and_does_not_abort_the_run(self):
        d1 = FakeD1()
        insert_history(d1, 0, 50, {"fng": 50})
        d1.conn.execute("INSERT INTO history (ts, score, sources_json) VALUES (?, 50, ?)", (HOUR, "{broken"))
        insert_history(d1, 2 * HOUR, 52, {"fng": 52})
        d1.conn.commit()
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=2 * HOUR)
        assert result["observations_rejected_malformed"] == 1
        assert result["rejected_observation_ts_sample"] == [HOUR]
        assert result["newly_archived"] == 2  # the two good rows; the broken one is not archived
        assert [r["observation_ts"] for r in d1.query("SELECT observation_ts FROM research_sentiment_archive ORDER BY 1")] == [0, 2 * HOUR]

    def test_malformed_row_already_archived_is_left_untouched_in_d1(self):
        d1 = FakeD1()
        seed_archive(d1, 0, 50, {"fng": 50}, archived_ts=1)
        d1.conn.execute("UPDATE research_sentiment_archive SET sources_json = '{broken' WHERE observation_ts = 0")
        d1.conn.commit()
        before = d1.query("SELECT * FROM research_sentiment_archive")
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=HOUR)
        assert result["observations_rejected_malformed"] == 1
        assert d1.query("SELECT * FROM research_sentiment_archive") == before  # append-only: no rewrite, no delete

    def test_observation_without_sources_is_counted_not_rejected(self):
        d1 = FakeD1()
        d1.conn.execute("INSERT INTO history (ts, score, sources_json) VALUES (0, 50, '{}')")
        d1.conn.commit()
        result = ep.run_pipeline(d1.query, d1.execute, now_ts=HOUR)
        assert result["observations_without_sources"] == 1
        assert result["observations_rejected_malformed"] == 0
        assert result["newly_archived"] == 1


class TestRunRecord:
    def test_insert_sql_is_valid_against_the_real_migration_schema(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        summary = ep.run_pipeline(d1.query, d1.execute, now_ts=HOUR)
        d1.execute(ep.build_insert_run_sql(HOUR, "OK", summary, None))
        row = d1.query("SELECT * FROM experiment5_pipeline_runs")[0]
        assert row["run_ts"] == HOUR and row["status"] == "OK" and row["error_text"] is None
        assert row["pipeline_version"] == ep.PIPELINE_VERSION
        assert json.loads(row["constants_json"])["target_horizon_hours"] == agent.EXPERIMENT5_TARGET_HORIZON_HOURS

    def test_record_run_reports_skipped_when_the_table_is_missing(self):
        d1 = FakeD1()
        before = len(d1.executed_sql)
        assert ep.record_run(d1.query, d1.execute, HOUR, "OK", {}, None) == "SKIPPED_TABLE_MISSING"
        assert len(d1.executed_sql) == before  # nothing written

    def test_record_run_reports_written_when_the_table_exists(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        assert ep.record_run(d1.query, d1.execute, HOUR, "OK", {}, None) == "WRITTEN"
        assert d1.query("SELECT COUNT(*) AS n FROM experiment5_pipeline_runs")[0]["n"] == 1

    def test_recorded_run_without_the_table_still_runs_and_says_it_did_not_record(self):
        d1 = FakeD1()
        insert_history(d1, 0, 50, {"fng": 50})
        result = ep.run_pipeline_recorded(d1.query, d1.execute, now_ts=HOUR)
        assert result["status"] == "OK" and result["run_record"] == "SKIPPED_TABLE_MISSING"
        assert result["newly_archived"] == 1

    def test_recorded_run_with_the_table_persists_counts_matching_the_summary(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        for i in range(3):
            insert_history(d1, i * HOUR, 50 + i, {"fng": 50 + i})
        result = ep.run_pipeline_recorded(d1.query, d1.execute, now_ts=2 * HOUR)
        assert result["run_record"] == "WRITTEN"
        row = d1.query("SELECT * FROM experiment5_pipeline_runs")[0]
        assert row["newly_archived"] == result["newly_archived"] == 3
        assert row["history_rows_read"] == 3
        assert row["decisions_created"] == result["decisions_created"]

    def test_failure_is_recorded_then_reraised(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        d1.conn.execute("DROP TABLE btc_data")  # makes run_pipeline fail on its own read
        with pytest.raises(sqlite3.OperationalError):
            ep.run_pipeline_recorded(d1.query, d1.execute, now_ts=HOUR)
        row = d1.query("SELECT * FROM experiment5_pipeline_runs")[0]
        assert row["status"] == "FAILED" and row["run_ts"] == HOUR
        assert row["error_text"].startswith("OperationalError:")
        assert row["newly_archived"] is None  # a failed run reports no counts it did not produce

    def test_failure_to_record_never_masks_the_original_error(self, capsys):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        d1.conn.execute("DROP TABLE btc_data")

        def failing_execute(sql):
            if "experiment5_pipeline_runs" in sql:
                raise RuntimeError("record write failed")
            d1.execute(sql)

        with pytest.raises(sqlite3.OperationalError):
            ep.run_pipeline_recorded(d1.query, failing_execute, now_ts=HOUR)
        assert "ALSO FAILED to record" in capsys.readouterr().err

    def test_error_text_is_truncated(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        ep.record_run(d1.query, d1.execute, HOUR, "FAILED", None, "x" * 5000)
        assert len(d1.query("SELECT error_text FROM experiment5_pipeline_runs")[0]["error_text"]) == ep.MAX_ERROR_CHARS

    def test_repeated_identical_runs_append_one_record_each_and_nothing_else_changes(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        insert_history(d1, 0, 50, {"fng": 50})
        ep.run_pipeline_recorded(d1.query, d1.execute, now_ts=HOUR)
        archive_after_first = d1.query("SELECT * FROM research_sentiment_archive")
        second = ep.run_pipeline_recorded(d1.query, d1.execute, now_ts=HOUR)
        assert second["newly_archived"] == 0
        assert d1.query("SELECT * FROM research_sentiment_archive") == archive_after_first
        assert d1.query("SELECT COUNT(*) AS n FROM experiment5_pipeline_runs")[0]["n"] == 2


def _decision_payload(anchor_ts, source="fng", direction=1):
    return json.dumps({"decision": {
        "anchor_ts": anchor_ts, "cycle_ts": anchor_ts, "primary_source": source, "direction": direction,
        "classifications": {}, "confirmation": {"classification": "NO_CONFIRMATION", "confirming_sources": []},
    }})


def insert_decision(d1, subject, anchor_ts, status=None, outcome=None, created_ts=None):
    payload = json.loads(_decision_payload(anchor_ts, source=subject.split(":")[-1]))
    if outcome is not None:
        payload["outcome"] = outcome
    d1.conn.execute(
        "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, source_analysis_ids, status, "
        "evidence_summary_json, out_of_sample_status) VALUES (?, ?, ?, 'x', '[]', 'OBSERVATION', ?, ?)",
        (anchor_ts if created_ts is None else created_ts, anchor_ts, subject, json.dumps(payload), status),
    )
    d1.conn.commit()
    return d1.conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestDecisionIdempotencyReproduction:
    """REPRODUCTION (found during PR #83 pre-merge review): the pipeline created a NEW decision row for the SAME
    (subject, anchor_ts) on every re-run. Decision ids are database AUTOINCREMENTs, so nothing made a repeat run
    recognise the decision it (or an earlier run) had already persisted. A manual re-dispatch, a retried workflow
    or a stalled V1 history write therefore inflated the decision count with duplicated, fully dependent samples."""

    def _reversal_scenario(self):
        d1 = FakeD1()
        for i in range(4):
            insert_history(d1, i * HOUR, 50, {"onchain": 50 + i * 10})
        insert_history(d1, 4 * HOUR, 50, {"onchain": 10})
        insert_btc(d1, 4 * HOUR, 100.0)
        insert_btc(d1, 28 * HOUR, 110.0)
        return d1

    def _count(self, d1):
        return d1.query("SELECT COUNT(*) AS n FROM research_hypotheses WHERE subject LIKE 'experiment5:%'")[0]["n"]

    def test_rerunning_with_identical_inputs_creates_no_second_decision_for_the_same_subject_and_anchor(self):
        d1 = self._reversal_scenario()
        first = ep.run_pipeline(d1.query, d1.execute, now_ts=10 * HOUR)
        assert first["decisions_created"] == 1 and self._count(d1) == 1
        again = ep.run_pipeline(d1.query, d1.execute, now_ts=10 * HOUR)
        assert again["decisions_created"] == 0
        assert again["decisions_skipped_duplicate"] == 1
        assert self._count(d1) == 1

    def test_a_decision_that_has_already_been_RESOLVED_is_still_recognised_as_existing(self):
        d1 = self._reversal_scenario()
        ep.run_pipeline(d1.query, d1.execute, now_ts=10 * HOUR)          # creates (anchor 4h)
        ep.run_pipeline(d1.query, d1.execute, now_ts=34 * HOUR)          # resolves it (24h horizon elapsed)
        resolved = d1.query("SELECT out_of_sample_status FROM research_hypotheses")[0]["out_of_sample_status"]
        assert resolved is not None
        third = ep.run_pipeline(d1.query, d1.execute, now_ts=34 * HOUR)  # same signal still in the observe window
        assert third["decisions_created"] == 0
        assert self._count(d1) == 1

    def test_a_genuinely_new_observation_still_creates_a_new_decision(self):
        d1 = self._reversal_scenario()
        ep.run_pipeline(d1.query, d1.execute, now_ts=10 * HOUR)
        # a later observation moves the anchor; the same dynamic at a NEW anchor is a legitimately new decision
        insert_history(d1, 5 * HOUR, 50, {"onchain": 90})
        insert_history(d1, 6 * HOUR, 50, {"onchain": 5})
        ep.run_pipeline(d1.query, d1.execute, now_ts=11 * HOUR)
        anchors = sorted(json.loads(r["evidence_summary_json"])["decision"]["anchor_ts"]
                         for r in d1.query("SELECT evidence_summary_json FROM research_hypotheses"))
        assert len(anchors) == len(set(anchors)), "no anchor may appear twice for the same subject"

    def test_the_duplicate_is_not_persisted_to_real_d1_at_all(self):
        d1 = self._reversal_scenario()
        ep.run_pipeline(d1.query, d1.execute, now_ts=10 * HOUR)
        before = list(d1.executed_sql)
        ep.run_pipeline(d1.query, d1.execute, now_ts=10 * HOUR)
        new_sql = d1.executed_sql[len(before):]
        assert not [s for s in new_sql if s.startswith("INSERT INTO research_hypotheses")]


class TestDecisionResolutionSafety:
    """No real row may be resolved/overwritten except the one the replayed decision actually is."""

    def test_the_outcome_update_only_applies_to_an_unresolved_experiment5_row(self):
        sql = ep.build_update_decision_outcome_sql(7, "{}", "PASSED_HOLDOUT", 5)
        assert "WHERE hypothesis_id = 7" in sql
        assert "out_of_sample_status IS NULL" in sql
        assert "subject LIKE 'experiment5:%'" in sql

    def test_a_row_resolved_by_someone_else_between_read_and_write_is_never_overwritten(self):
        d1 = FakeD1()
        hid = insert_decision(d1, "experiment5:reversal:fng", 0)
        insert_btc(d1, 0, 100.0)
        insert_btc(d1, 24 * HOUR, 110.0)
        insert_history(d1, 0, 60, {"fng": 50})
        for i in range(2):
            insert_history(d1, 30 * HOUR + i, 50, {"fng": 50})
        winner = {"marker": "FIRST-RESOLUTION-WINS"}
        original_execute = d1.execute

        def racing_execute(sql):
            if sql.startswith("UPDATE research_hypotheses"):
                # a concurrent run resolves the same decision just before this UPDATE lands
                d1.conn.execute(
                    "UPDATE research_hypotheses SET out_of_sample_status = 'FAILED_HOLDOUT', evidence_summary_json = ? WHERE hypothesis_id = ?",
                    (json.dumps({"decision": json.loads(_decision_payload(0))["decision"], "outcome": winner}), hid),
                )
                d1.conn.commit()
            original_execute(sql)

        result = ep.run_pipeline(d1.query, racing_execute, now_ts=30 * HOUR)
        assert result["decisions_evaluated"] == 1  # this run did evaluate it locally...
        row = d1.query(f"SELECT evidence_summary_json, out_of_sample_status FROM research_hypotheses WHERE hypothesis_id = {hid}")[0]
        assert json.loads(row["evidence_summary_json"])["outcome"] == winner  # ...but the first resolution is preserved
        assert row["out_of_sample_status"] == "FAILED_HOLDOUT"

    def test_multiple_pending_decisions_each_resolve_under_their_own_real_id_and_nothing_else_changes(self):
        d1 = FakeD1()
        ids = [insert_decision(d1, f"experiment5:reversal:{name}", anchor) for name, anchor in
               (("fng", 0), ("onchain", 2 * HOUR), ("etfflows", 3 * HOUR))]
        resolved_marker = {"marker": "UNTOUCHED", "evaluated_ts": 1}
        old_id = insert_decision(d1, "experiment5:reversal:legacy", 0, status="PASSED_HOLDOUT", outcome=resolved_marker)
        not_experiment5 = d1.conn.execute(
            "INSERT INTO research_hypotheses (created_ts, last_updated_ts, subject, statement, source_analysis_ids, status, "
            "evidence_summary_json, out_of_sample_status) VALUES (1, 1, 'unrelated:other', 'x', '[]', 'OBSERVATION', '{}', NULL)"
        ).lastrowid
        d1.conn.commit()
        for ts in (0, 2 * HOUR, 3 * HOUR):
            insert_history(d1, ts, 60, {"fng": 50})  # the outcome engine anchors each decision on the history row at its own ts
            insert_btc(d1, ts, 100.0)
            insert_btc(d1, ts + 24 * HOUR, 110.0)
        insert_history(d1, 49 * HOUR, 50, {"fng": 50})
        insert_history(d1, 50 * HOUR, 50, {"fng": 50})

        result = ep.run_pipeline(d1.query, d1.execute, now_ts=50 * HOUR)

        assert result["decisions_replayed"] == 3
        assert result["decisions_evaluated"] == 3
        for hid in ids:
            assert d1.query(f"SELECT out_of_sample_status AS s FROM research_hypotheses WHERE hypothesis_id = {hid}")[0]["s"] is not None
        legacy = d1.query(f"SELECT evidence_summary_json, out_of_sample_status FROM research_hypotheses WHERE hypothesis_id = {old_id}")[0]
        assert json.loads(legacy["evidence_summary_json"])["outcome"] == resolved_marker and legacy["out_of_sample_status"] == "PASSED_HOLDOUT"
        other = d1.query(f"SELECT evidence_summary_json, out_of_sample_status FROM research_hypotheses WHERE hypothesis_id = {not_experiment5}")[0]
        assert other["evidence_summary_json"] == "{}" and other["out_of_sample_status"] is None
        updated_ids = {int(re.search(r"hypothesis_id = (\d+)", s).group(1)) for s in d1.executed_sql if s.startswith("UPDATE research_hypotheses")}
        assert updated_ids == set(ids)

    def test_a_catch_up_run_long_after_the_horizons_resolves_each_pending_decision_once_and_a_second_run_changes_nothing(self):
        d1 = FakeD1()
        ids = [insert_decision(d1, f"experiment5:reversal:{name}", anchor) for name, anchor in (("fng", 0), ("onchain", HOUR))]
        for ts in (0, HOUR):
            insert_history(d1, ts, 60, {"fng": 50})
            insert_btc(d1, ts, 100.0)
            insert_btc(d1, ts + 24 * HOUR, 90.0)
        for t in (5 * 24 * HOUR, 5 * 24 * HOUR + HOUR):
            insert_history(d1, t, 50, {"fng": 50})
        late = 5 * 24 * HOUR + 2 * HOUR  # a run five days late
        first = ep.run_pipeline(d1.query, d1.execute, now_ts=late)
        assert first["decisions_evaluated"] == 2
        snapshot = d1.query("SELECT hypothesis_id, evidence_summary_json, out_of_sample_status, last_updated_ts FROM research_hypotheses ORDER BY hypothesis_id")
        again = ep.run_pipeline(d1.query, d1.execute, now_ts=late)
        assert again["decisions_evaluated"] == 0 and again["decisions_replayed"] == 0
        assert d1.query("SELECT hypothesis_id, evidence_summary_json, out_of_sample_status, last_updated_ts FROM research_hypotheses ORDER BY hypothesis_id") == snapshot


class TestObservationPopulationConsistency:
    """REPRODUCTION (PR #83 pre-merge review): the diagnostics mixed two populations. `archive_rows_observed` counted
    every row in the local mirror -- including rows up to 30 days old that the agent (which observes only its own
    14-day window) never sees -- and `observations_rejected_malformed` counted rejects across the same 30-day read
    window. So `observed + rejected` described no real set. Every counter the run reports about "what the agent saw"
    must now describe the agent's observe window; rejects outside it are reported separately."""

    DAY = 24 * HOUR

    def _scenario(self):
        d1 = FakeD1()
        now = 30 * self.DAY
        for i in range(3):                                           # inside the agent window, well-formed
            insert_history(d1, now - (3 - i) * HOUR, 50, {"fng": 50 + i})
        insert_history(d1, now - 90 * 60000, 50, {})                 # inside the window, no sources (own distinct ts)
        for i in range(2):                                           # 20 days old: archived, but NOT observed by the agent
            insert_history(d1, now - 20 * self.DAY + i * HOUR, 50, {"fng": 40 + i})
        d1.conn.execute("INSERT INTO history (ts, score, sources_json) VALUES (?, 50, '{bad')", (now - 20 * self.DAY + 5 * HOUR,))
        d1.conn.execute("INSERT INTO history (ts, score, sources_json) VALUES (?, 50, '{bad')", (now - 2 * HOUR,))
        d1.conn.commit()
        return d1, now

    def test_observed_without_sources_and_rejected_all_describe_the_agents_observe_window(self):
        d1, now = self._scenario()
        r = ep.run_pipeline(d1.query, d1.execute, now_ts=now)
        assert r["archive_rows_observed"] == 4                      # 3 well-formed + 1 empty, all inside 14 days
        assert r["observations_without_sources"] == 1
        assert r["observations_rejected_malformed"] == 1            # the 2h-old malformed row only
        assert r["observations_rejected_malformed_outside_observe_window"] == 1   # the 20-day-old one, reported separately
        # the agent's intended population is fully accounted for
        assert r["archive_rows_observed"] + r["observations_rejected_malformed"] == 5

    def test_all_rows_are_still_archived_so_nothing_is_lost_by_the_window_split(self):
        d1, now = self._scenario()
        r = ep.run_pipeline(d1.query, d1.execute, now_ts=now)
        assert r["newly_archived"] == 6                              # 4 recent + 2 old well-formed; the 2 malformed are rejected
        assert d1.query("SELECT COUNT(*) AS n FROM research_sentiment_archive")[0]["n"] == 6

    def test_a_malformed_row_seen_via_both_history_and_the_archive_is_counted_once(self):
        d1 = FakeD1()
        now = 10 * self.DAY
        seed_archive(d1, now - 2 * HOUR, 50, {"fng": 50}, archived_ts=1)
        d1.conn.execute("UPDATE research_sentiment_archive SET sources_json = '{bad' WHERE observation_ts = ?", (now - 2 * HOUR,))
        d1.conn.execute("INSERT INTO history (ts, score, sources_json) VALUES (?, 50, '{bad')", (now - 2 * HOUR,))
        d1.conn.commit()
        r = ep.run_pipeline(d1.query, d1.execute, now_ts=now)
        assert r["observations_rejected_malformed"] == 1

    def test_the_run_record_stores_the_window_consistent_counters(self):
        d1, now = self._scenario()
        apply_run_table_migration(d1)
        r = ep.run_pipeline_recorded(d1.query, d1.execute, now_ts=now)
        row = d1.query("SELECT * FROM experiment5_pipeline_runs")[0]
        assert row["archive_rows_observed"] == r["archive_rows_observed"] == 4
        assert row["observations_rejected_malformed"] == 1
        assert row["observations_rejected_outside_window"] == 1
        assert json.loads(row["rejected_observation_ts_json"]) == r["rejected_observation_ts_sample"]


class TestRunRecordSchemaAndSemantics:
    """Run record validated against the REAL migration 0019 in real sqlite."""

    def _table_info(self, d1):
        return {r["name"]: r for r in d1.query("PRAGMA table_info(experiment5_pipeline_runs)")}

    def test_every_column_the_insert_builder_writes_exists_and_every_required_column_is_written(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        sql = ep.build_insert_run_sql(5, "OK", {}, None)
        written = [c.strip() for c in re.search(r"\(([^)]*)\) VALUES", sql).group(1).split(",")]
        info = self._table_info(d1)
        assert set(written) <= set(info)
        required = {name for name, col in info.items() if col["notnull"] and col["dflt_value"] is None and not col["pk"]}
        assert required <= set(written), required - set(written)
        # nothing in the table is silently left unfilled by the builder (a column added to one side but not the other)
        assert set(info) - {"run_id"} == set(written)

    def test_status_is_constrained_to_OK_or_FAILED_by_the_schema(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        for bad in ("RUNNING", "ok", "", "SKIPPED_TABLE_MISSING"):
            with pytest.raises(sqlite3.IntegrityError):
                d1.conn.execute(
                    "INSERT INTO experiment5_pipeline_runs (run_ts, status, pipeline_version, constants_json) VALUES (1, ?, 'v', '{}')", (bad,))

    def test_the_status_lookup_indexes_exist(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        names = {r["name"] for r in d1.query("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'experiment5_pipeline_runs'")}
        assert {"idx_exp5_runs_run_ts", "idx_exp5_runs_status_run_ts"} <= names
        plan = " ".join(str(tuple(r)) for r in d1.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM experiment5_pipeline_runs WHERE status = 'OK' ORDER BY run_ts DESC, run_id DESC LIMIT 1"))
        assert "idx_exp5_runs_status_run_ts" in plan

    def test_hostile_text_in_an_error_is_stored_verbatim_and_cannot_break_out_of_the_statement(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        nasty = "boom'); DROP TABLE research_hypotheses; --"
        ep.record_run(d1.query, d1.execute, 7, "FAILED", None, nasty)
        assert d1.query("SELECT error_text FROM experiment5_pipeline_runs")[0]["error_text"] == nasty
        assert d1.query("SELECT COUNT(*) AS n FROM sqlite_master WHERE name = 'research_hypotheses'")[0]["n"] == 1

    def test_run_ts_is_exactly_the_supplied_clock_for_both_outcomes_and_no_wall_clock_is_read(self, monkeypatch):
        import time

        def forbidden(*a, **k):
            raise AssertionError("wall clock read")
        monkeypatch.setattr(time, "time", forbidden)
        monkeypatch.setattr(time, "time_ns", forbidden)
        ok = FakeD1(); apply_run_table_migration(ok)
        ep.run_pipeline_recorded(ok.query, ok.execute, now_ts=123 * HOUR)
        assert ok.query("SELECT run_ts, status FROM experiment5_pipeline_runs") == [{"run_ts": 123 * HOUR, "status": "OK"}]
        bad = FakeD1(); apply_run_table_migration(bad)
        bad.conn.execute("DROP TABLE btc_data")
        with pytest.raises(sqlite3.OperationalError):
            ep.run_pipeline_recorded(bad.query, bad.execute, now_ts=124 * HOUR)
        assert bad.query("SELECT run_ts, status FROM experiment5_pipeline_runs") == [{"run_ts": 124 * HOUR, "status": "FAILED"}]

    def test_counters_on_an_OK_record_equal_the_summary_and_a_FAILED_record_has_none(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        for i in range(4):
            insert_history(d1, i * HOUR, 50, {"onchain": 50 + i * 10})
        insert_history(d1, 4 * HOUR, 50, {"onchain": 10})
        d1.conn.execute("INSERT INTO history (ts, score, sources_json) VALUES (?, 50, '{bad')", (90 * 60000,))
        d1.conn.commit()
        insert_btc(d1, 4 * HOUR, 100.0)
        r = ep.run_pipeline_recorded(d1.query, d1.execute, now_ts=10 * HOUR)
        row = d1.query("SELECT * FROM experiment5_pipeline_runs")[0]
        for column, key in (("history_rows_read", "history_rows_read"), ("btc_rows_read", "btc_rows_read"), ("newly_archived", "newly_archived"),
                            ("archive_rows_observed", "archive_rows_observed"), ("observations_without_sources", "observations_without_sources"),
                            ("observations_rejected_malformed", "observations_rejected_malformed"),
                            ("decisions_replayed", "decisions_replayed"), ("decisions_created", "decisions_created"),
                            ("decisions_skipped_duplicate", "decisions_skipped_duplicate"), ("decisions_evaluated", "decisions_evaluated")):
            assert row[column] == r[key], column
        assert row["decisions_created"] == 1 and row["observations_rejected_malformed"] == 1
        assert row["status"] == "OK" and row["error_text"] is None
        assert row["decisions_evaluated"] == row["evaluated_passed"] + row["evaluated_failed"] + row["evaluated_inconclusive"]


class TestTelemetryFailureHandling:
    def test_missing_table_is_never_reported_as_recorded_and_the_experiment_still_ran(self):
        d1 = FakeD1()
        insert_history(d1, 0, 50, {"fng": 50})
        r = ep.run_pipeline_recorded(d1.query, d1.execute, now_ts=HOUR)
        assert r["run_record"] == "SKIPPED_TABLE_MISSING" and r["run_record"] != "WRITTEN"
        assert r["newly_archived"] == 1
        assert not [s for s in d1.executed_sql if "experiment5_pipeline_runs" in s]

    def test_a_success_whose_telemetry_write_fails_is_reported_WRITE_FAILED_not_WRITTEN_and_does_not_fail_the_run(self, capsys):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        insert_history(d1, 0, 50, {"fng": 50})

        def failing_execute(sql):
            if "INSERT INTO experiment5_pipeline_runs" in sql:
                raise RuntimeError("D1 write failed")
            d1.execute(sql)

        r = ep.run_pipeline_recorded(d1.query, failing_execute, now_ts=HOUR)   # must not raise
        assert r["run_record"] == "WRITE_FAILED"
        assert r["run_record_error"].startswith("RuntimeError: D1 write failed")
        assert r["newly_archived"] == 1 and d1.query("SELECT COUNT(*) AS n FROM research_sentiment_archive")[0]["n"] == 1
        assert d1.query("SELECT COUNT(*) AS n FROM experiment5_pipeline_runs")[0]["n"] == 0
        assert "TELEMETRY WRITE FAILED" in capsys.readouterr().err

    def test_a_transient_failure_of_the_table_existence_check_on_success_is_WRITE_FAILED_not_skipped(self):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        insert_history(d1, 0, 50, {"fng": 50})

        def flaky_query(sql):
            if "sqlite_master" in sql and "experiment5_pipeline_runs" in sql:
                raise RuntimeError("D1 timeout")
            return d1.query(sql)

        r = ep.run_pipeline_recorded(flaky_query, d1.execute, now_ts=HOUR)
        assert r["run_record"] == "WRITE_FAILED"   # unknown is not "table missing"

    def test_a_pipeline_failure_whose_record_also_fails_still_raises_the_ORIGINAL_error(self, capsys):
        d1 = FakeD1()
        apply_run_table_migration(d1)
        d1.conn.execute("DROP TABLE btc_data")

        def failing_execute(sql):
            if "experiment5_pipeline_runs" in sql:
                raise RuntimeError("telemetry down")
            d1.execute(sql)

        with pytest.raises(sqlite3.OperationalError, match="btc_data"):
            ep.run_pipeline_recorded(d1.query, failing_execute, now_ts=HOUR)
        assert "ALSO FAILED to record" in capsys.readouterr().err

    def test_a_pipeline_failure_when_the_table_check_itself_fails_still_raises_the_ORIGINAL_error(self):
        d1 = FakeD1()
        d1.conn.execute("DROP TABLE btc_data")

        def query(sql):
            if "experiment5_pipeline_runs" in sql:
                raise RuntimeError("D1 down")
            return d1.query(sql)

        with pytest.raises(sqlite3.OperationalError, match="btc_data"):
            ep.run_pipeline_recorded(query, d1.execute, now_ts=HOUR)

    def test_run_py_warns_loudly_when_a_run_was_not_recorded(self, monkeypatch, capsys):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "exp5_run_script", os.path.join(os.path.dirname(__file__), "..", "scripts", "experiment5-agent", "run.py"))
        run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run)
        summary = {
            "history_rows_read": 1, "btc_rows_read": 1, "newly_archived": 0, "decisions_created": 0, "decisions_skipped_duplicate": 0,
            "decisions_evaluated": 0, "observations_rejected_malformed": 0, "rejected_observation_ts_sample": [], "agent_cycle": {},
        }
        for record, needle in (("SKIPPED_TABLE_MISSING", "migration 0019"), ("WRITE_FAILED", "boom")):
            extra = {"run_record_error": "boom"} if record == "WRITE_FAILED" else {}
            monkeypatch.setattr(run.ep, "run_pipeline_recorded", lambda q, e, n, record=record, extra=extra: {**summary, "run_record": record, **extra})
            run.main()
            out = capsys.readouterr().out
            assert f"::warning title=Experiment 5 run not recorded::{record}" in out and needle in out
        monkeypatch.setattr(run.ep, "run_pipeline_recorded", lambda q, e, n: {**summary, "run_record": "WRITTEN"})
        run.main()
        assert "::warning" not in capsys.readouterr().out
