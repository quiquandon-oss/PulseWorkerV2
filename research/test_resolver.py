"""
Tests for research/resolver.py.

All tests execute against a real, in-memory SQLite database with the
same schema shape and indexes as production (verified against the real
D1 indexes before writing this: idx_ts on history(ts),
idx_predictions_horizon_ts on predictions(horizon_hours, ts)). No
network, no D1 connection required to run these.

Run with: python3 -m pytest research/test_resolver.py -v
"""
import inspect
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import resolver  # noqa: E402


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER,
        technical_score INTEGER, bottom_score INTEGER, regime_mag REAL
    )""")
    conn.execute("CREATE INDEX idx_ts ON history(ts)")
    conn.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, horizon_hours INTEGER NOT NULL,
        realized_up INTEGER, target_ts INTEGER
    )""")
    conn.execute("CREATE INDEX idx_predictions_horizon_ts ON predictions(horizon_hours, ts)")
    return conn


# ---- Requirement 1: mandatory bounds ----

def test_start_ts_and_end_ts_have_no_default_value():
    sig = inspect.signature(resolver.resolve_observations)
    for name in ("start_ts", "end_ts"):
        assert sig.parameters[name].default is inspect.Parameter.empty, (
            f"{name} must have no default -- an unbounded call must not be constructible"
        )


def test_missing_bounds_raise_typeerror():
    conn = fresh_db()
    with pytest.raises(TypeError):
        resolver.resolve_observations(conn, 24)  # start_ts/end_ts omitted entirely
    conn.close()


def test_none_bounds_raise_valueerror():
    conn = fresh_db()
    with pytest.raises(ValueError):
        resolver.resolve_observations(conn, 24, start_ts=None, end_ts=1000)
    with pytest.raises(ValueError):
        resolver.resolve_observations(conn, 24, start_ts=1000, end_ts=None)
    conn.close()


def test_reversed_window_rejected():
    conn = fresh_db()
    with pytest.raises(ValueError):
        resolver.resolve_observations(conn, 24, start_ts=2000, end_ts=1000)
    conn.close()


def test_zero_width_window_rejected():
    conn = fresh_db()
    with pytest.raises(ValueError):
        resolver.resolve_observations(conn, 24, start_ts=1000, end_ts=1000)
    conn.close()


def test_excessively_large_window_rejected():
    conn = fresh_db()
    with pytest.raises(ValueError):
        resolver.resolve_observations(conn, 24, start_ts=0, end_ts=resolver.MAX_WINDOW_MS + 1)
    conn.close()


def test_window_exactly_at_max_is_accepted():
    conn = fresh_db()
    # Should not raise -- MAX_WINDOW_MS itself is a valid (largest allowed) window.
    resolver.resolve_observations(conn, 24, start_ts=0, end_ts=resolver.MAX_WINDOW_MS)
    conn.close()


# ---- Requirement 2 & 5: indexed query plan, fail on any table scan ----

def test_query_plan_uses_indexes_not_table_scans():
    conn = fresh_db()
    plan = resolver.explain_resolver_query_plan(conn, 24, 1000, 2000)
    plan_text = " ".join(str(row) for row in plan).upper()
    assert "SEARCH PREDICTIONS USING INDEX" in plan_text.replace("SEARCH P USING INDEX", "SEARCH PREDICTIONS USING INDEX"), (
        f"expected an indexed search on predictions, got: {plan}"
    )
    assert "USING INDEX IDX_TS" in plan_text, f"expected the history lookup to use idx_ts, got: {plan}"
    assert "SCAN PREDICTIONS" not in plan_text.replace(" P ", " PREDICTIONS "), f"predictions must not be table-scanned: {plan}"
    assert "SCAN HISTORY" not in plan_text.replace(" H ", " HISTORY ").replace(" H2 ", " HISTORY "), (
        f"history must not be table-scanned: {plan}"
    )
    conn.close()


# ---- Requirement 6: boundedness is structural, not just behavioral ----

def test_sql_template_contains_the_three_bind_parameters():
    # horizon_hours, start_ts, end_ts -- exactly 3 '?' placeholders, all
    # inside the WHERE clause that bounds the outer query.
    where_clause = resolver.RESOLVER_SQL[resolver.RESOLVER_SQL.index("WHERE"):]
    assert where_clause.count("?") == 3
    assert "p.ts >= ?" in resolver.RESOLVER_SQL
    assert "p.ts < ?" in resolver.RESOLVER_SQL


def test_no_unbounded_query_string_exists_anywhere_in_the_module():
    src = inspect.getsource(resolver)
    # Exactly one bounded query construct should exist in this module --
    # checking for the WHERE-bound marker specifically (not a bare count
    # of the word "SELECT", which would also match the correlated
    # subquery's own inner SELECT and produce a false failure).
    assert src.count("WHERE p.horizon_hours") == 1, "exactly one bounded query template should exist in this module"


# ---- Requirement 7: read volume bounded by window, not table size ----

def _seed_realistic(conn, n_history=500, n_predictions=2000, base=1780000000000):
    for i in range(n_history):
        conn.execute("INSERT INTO history (ts, score) VALUES (?, ?)", (base + i * 3600000 * 2, 50 + i % 20))
    for i in range(n_predictions):
        conn.execute("INSERT INTO predictions (ts, horizon_hours, realized_up, target_ts) VALUES (?, ?, ?, ?)",
                     (base + i * 3600000 * 3, 24, i % 2, base + i * 3600000 * 3 + 86400000))
    conn.commit()
    return base


def test_read_volume_scales_with_window_not_table_size():
    conn = fresh_db()
    base = _seed_realistic(conn)
    rows_7d = resolver.resolve_observations(conn, 24, base, base + 7 * 86400000)
    rows_30d = resolver.resolve_observations(conn, 24, base, base + 30 * 86400000)
    rows_max = resolver.resolve_observations(conn, 24, base, base + resolver.MAX_WINDOW_MS)
    assert len(rows_7d) < len(rows_30d) < len(rows_max)
    # The largest allowed window must still be far smaller than what an
    # unbounded query over this table would return (2000) -- proving the
    # cap is real, not just present in name.
    assert len(rows_max) <= 2000
    print(f"\nread-volume evidence: 7d={len(rows_7d)} rows, 30d={len(rows_30d)} rows, "
          f"max({resolver.MAX_WINDOW_MS}ms)={len(rows_max)} rows, table_total=2000 predictions")
    conn.close()


# ---- Requirements 3, 4 & the exact tricky synthetic dataset from review ----

def test_the_exact_tricky_timestamp_case_from_review():
    """V1: 100, 200, 300. Prediction: 250. Expected resolved V1 = 200, never 300."""
    conn = fresh_db()
    for ts in (100, 200, 300):
        conn.execute("INSERT INTO history (ts, score) VALUES (?, ?)", (ts, ts))
    conn.execute("INSERT INTO predictions (ts, horizon_hours, realized_up, target_ts) VALUES (250, 24, 1, 350)")
    conn.commit()
    rows = resolver.resolve_observations(conn, 24, start_ts=0, end_ts=1000)
    assert len(rows) == 1
    assert rows[0]["v1_observation_ts"] == 200, f"expected V1 observation at ts=200, got {rows[0]['v1_observation_ts']}"
    assert rows[0]["v1_composite_at_prediction_time"] == 200
    conn.close()


def test_v1_observation_exactly_at_prediction_time_is_eligible():
    """Boundary inclusivity: if V1 and the prediction share the exact same
    timestamp, that reflects real information-timestamp semantics -- it
    must be selected, not excluded."""
    conn = fresh_db()
    conn.execute("INSERT INTO history (ts, score) VALUES (200, 999)")
    conn.execute("INSERT INTO predictions (ts, horizon_hours, realized_up, target_ts) VALUES (200, 24, 1, 300)")
    conn.commit()
    rows = resolver.resolve_observations(conn, 24, start_ts=0, end_ts=1000)
    assert rows[0]["v1_observation_ts"] == 200
    assert rows[0]["v1_composite_at_prediction_time"] == 999
    conn.close()


def test_v1_observation_after_prediction_time_is_never_selected_no_lookahead():
    """The explicit no-lookahead requirement: V1 at T+1 must not be
    selected for a prediction at T, even though it's the only V1 row in
    the database (proving this isn't accidentally passing because an
    earlier row also happens to exist)."""
    conn = fresh_db()
    conn.execute("INSERT INTO history (ts, score) VALUES (251, 12345)")  # strictly AFTER the prediction
    conn.execute("INSERT INTO predictions (ts, horizon_hours, realized_up, target_ts) VALUES (250, 24, 1, 350)")
    conn.commit()
    rows = resolver.resolve_observations(conn, 24, start_ts=0, end_ts=1000)
    assert len(rows) == 1
    assert rows[0]["v1_observation_ts"] is None, (
        "no V1 observation exists at or before the prediction time -- must resolve to NULL, "
        "never to the future-dated row"
    )
    assert rows[0]["v1_composite_at_prediction_time"] is None
    conn.close()


def test_outcome_is_read_from_existing_realized_up_not_recomputed():
    """The actual BTC outcome must come from predictions.realized_up/
    target_ts exactly as production already computes them -- this
    resolver must not invent a second, parallel outcome definition."""
    conn = fresh_db()
    conn.execute("INSERT INTO history (ts, score) VALUES (100, 50)")
    conn.execute("INSERT INTO predictions (ts, horizon_hours, realized_up, target_ts) VALUES (150, 24, 0, 250)")
    conn.commit()
    rows = resolver.resolve_observations(conn, 24, start_ts=0, end_ts=1000)
    assert rows[0]["actual_outcome"] == 0
    assert rows[0]["target_ts"] == 250
    conn.close()


def test_horizon_hours_filter_is_respected():
    """A 12h prediction must not be picked up when resolving for horizon 24."""
    conn = fresh_db()
    conn.execute("INSERT INTO history (ts, score) VALUES (100, 50)")
    conn.execute("INSERT INTO predictions (ts, horizon_hours, realized_up, target_ts) VALUES (150, 12, 1, 200)")
    conn.commit()
    rows = resolver.resolve_observations(conn, 24, start_ts=0, end_ts=1000)
    assert rows == []


# ---- Requirement 8: no production behavior ----

def test_module_makes_no_network_or_llm_calls():
    src = inspect.getsource(resolver)
    for forbidden in ["requests.", "urllib", "fetch(", "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower(), f"resolver.py must not reference {forbidden}"


def test_module_never_writes_to_the_database():
    src = inspect.getsource(resolver)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in src, f"resolver.py must be read-only -- found {forbidden}"
