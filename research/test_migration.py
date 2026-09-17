"""
Tests for .ai/migrations/0005_research_schema.sql.

These ACTUALLY EXECUTE the migration against a real, in-memory SQLite
database (Python's built-in sqlite3 -- same dialect D1 uses, zero new
dependencies, zero network access) rather than only checking the SQL
text. Where a claim can be behaviorally proven (constraint enforcement,
query plan), it is -- not just asserted from reading the file.

Run with: python3 -m pytest research/ -v
"""
import os
import re
import sqlite3

import pytest

MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", ".ai", "migrations", "0005_research_schema.sql")

with open(MIGRATION_PATH) as f:
    MIGRATION_SQL = f.read()


def fresh_db_with_history():
    """A minimal in-memory DB with just enough of the real `history`
    table shape (and its existing idx_ts index, confirmed present in
    production before writing this migration) for the view and its
    query-plan test to be meaningful, plus the migration applied on top.
    Not a full production schema copy -- only what PR1's own objects
    actually touch."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL,
          score INTEGER NOT NULL,
          btc_price REAL,
          sources_json TEXT, technical_score INTEGER, gold_regime TEXT,
          regime_mag REAL, bottom_score INTEGER, global_mcap REAL
        )
    """)
    conn.execute("CREATE INDEX idx_ts ON history(ts)")
    conn.executescript(MIGRATION_SQL)
    return conn


# ---- Migration succeeds ----

def test_migration_executes_without_error():
    conn = fresh_db_with_history()
    conn.close()


def test_all_expected_objects_created():
    conn = fresh_db_with_history()
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view','index')"
    )}
    for expected in ["research_observations_v1", "research_events", "research_analyses",
                      "idx_research_events_fingerprint", "idx_research_events_event_ts",
                      "idx_research_analyses_ts"]:
        assert expected in names, f"{expected} was not created by the migration"
    conn.close()


# ---- Correction 1: no correlated subquery, view is single-table and index-safe ----

def test_view_definition_contains_no_subquery_at_all():
    match = re.search(r"CREATE VIEW research_observations_v1 AS(.*?);", MIGRATION_SQL, re.DOTALL)
    assert match is not None
    view_sql = match.group(1)
    assert "(SELECT" not in view_sql, "view must not contain any subquery, correlated or otherwise"


def test_view_only_references_history_not_v2_tables():
    match = re.search(r"CREATE VIEW research_observations_v1 AS(.*?);", MIGRATION_SQL, re.DOTALL)
    view_sql = match.group(1)
    assert "FROM history" in view_sql
    for forbidden in ["selection_decisions", "predictions", "link_predictions", "eth_predictions",
                       "challenger_predictions"]:
        assert forbidden not in view_sql, (
            f"PR1's view must not reference {forbidden} -- V2 linkage is explicitly deferred to PR2"
        )


def test_view_query_uses_the_existing_index_when_filtered_by_ts():
    """Behavioral proof, not just a text check: querying the view with a
    ts filter must use idx_ts, not a full table scan -- confirming the
    view is genuinely index-safe, not just free of subqueries in isolation."""
    conn = fresh_db_with_history()
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM research_observations_v1 WHERE ts > 1000"
    ).fetchall()
    plan_text = " ".join(str(row) for row in plan).upper()
    assert "IDX_TS" in plan_text or "USING INDEX" in plan_text, (
        f"expected the ts filter to use idx_ts, got plan: {plan}"
    )
    assert "SCAN" not in plan_text or "USING INDEX" in plan_text, (
        f"expected an index-assisted access, not a full scan: {plan}"
    )
    conn.close()


def test_view_returns_real_rows_correctly():
    conn = fresh_db_with_history()
    conn.execute(
        "INSERT INTO history (ts, score, btc_price, technical_score, bottom_score, regime_mag, gold_regime, sources_json) "
        "VALUES (1000, 55, 90000.0, 60, 10, 0.02, 'neutral', '{}')"
    )
    conn.commit()
    row = conn.execute("SELECT ts, v1_composite, btc_price FROM research_observations_v1 WHERE ts=1000").fetchone()
    assert row == (1000, 55, 90000.0)
    conn.close()


# ---- Correction 2: idempotency via a required unique fingerprint, not AUTOINCREMENT alone ----

def test_research_events_fingerprint_is_required():
    conn = fresh_db_with_history()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO research_events (event_ts, detection_ts, category, available_before_prediction) "
            "VALUES (1000, 1001, 'directional_move', 1)"
        )
    conn.close()


def test_duplicate_fingerprint_is_rejected_not_duplicated():
    """The exact behavior required: same input -> same fingerprint ->
    second insert attempt fails rather than creating a duplicate row."""
    conn = fresh_db_with_history()
    conn.execute(
        "INSERT INTO research_events (fingerprint, event_ts, detection_ts, category, available_before_prediction) "
        "VALUES ('directional_move_1000', 1000, 1001, 'directional_move', 1)"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO research_events (fingerprint, event_ts, detection_ts, category, available_before_prediction) "
            "VALUES ('directional_move_1000', 1000, 1002, 'directional_move', 1)"
        )
    n = conn.execute("SELECT COUNT(*) FROM research_events").fetchone()[0]
    assert n == 1, "a rejected duplicate insert must not leave a partial/duplicate row behind"
    conn.close()


def test_different_fingerprint_is_accepted_normally():
    conn = fresh_db_with_history()
    conn.execute(
        "INSERT INTO research_events (fingerprint, event_ts, detection_ts, category, available_before_prediction) "
        "VALUES ('a', 1000, 1001, 'directional_move', 1)"
    )
    conn.execute(
        "INSERT INTO research_events (fingerprint, event_ts, detection_ts, category, available_before_prediction) "
        "VALUES ('b', 2000, 2001, 'volatility', 1)"
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM research_events").fetchone()[0]
    assert n == 2
    conn.close()


# ---- Correction 3: events hold no evidence-specific columns ----

def test_research_events_has_no_evidence_columns():
    conn = fresh_db_with_history()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(research_events)")}
    for forbidden in ["evidence_url", "evidence_hash", "publication_ts", "source_url"]:
        assert forbidden not in cols, (
            f"research_events must not carry evidence-specific column {forbidden} -- "
            "evidence is a separate, one-to-many layer deferred to a later PR"
        )
    conn.close()


# ---- research_analyses: required fields enforced ----

def test_research_analyses_required_fields_enforced():
    conn = fresh_db_with_history()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO research_analyses (analysis_ts, window_start_ts) VALUES (1000, 900)")
    conn.close()


def test_research_analyses_accepts_a_valid_row():
    conn = fresh_db_with_history()
    conn.execute(
        "INSERT INTO research_analyses (analysis_ts, window_start_ts, window_end_ts, sample_size, subject, metric_json) "
        "VALUES (1000, 0, 1000, 33, 'composite', '{\"r\":0.02}')"
    )
    conn.commit()
    row = conn.execute("SELECT validation_status FROM research_analyses").fetchone()
    assert row[0] == "observation", "validation_status should default to 'observation'"
    conn.close()


# ---- Existing production tables/behavior untouched ----

def test_migration_contains_no_alter_or_drop_of_any_existing_table():
    existing_tables = ["history", "predictions", "link_predictions", "eth_predictions",
                       "challenger_predictions", "selection_decisions", "selection_decisions_anomaly",
                       "selection_decisions_momentum", "experiment_4_timesfm", "btc_data", "link_data", "eth_data"]
    upper_sql = MIGRATION_SQL.upper()
    for t in existing_tables:
        assert f"ALTER TABLE {t.upper()}" not in upper_sql
        assert f"DROP TABLE {t.upper()}" not in upper_sql
    assert "DELETE FROM" not in upper_sql
    assert "UPDATE " not in upper_sql


def test_migration_is_purely_additive_only_create_statements():
    statements = [s.strip() for s in MIGRATION_SQL.split(";") if s.strip() and not s.strip().startswith("--")]
    for s in statements:
        assert s.upper().lstrip().startswith("CREATE"), f"non-CREATE statement found in migration: {s[:60]}"
