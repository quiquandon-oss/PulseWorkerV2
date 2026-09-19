"""
Tests for research/outcome_engine.py.

All tests execute against a real, in-memory SQLite database with the
same schema shape and indexes as production (idx_predictions_horizon_ts
on predictions(horizon_hours, ts), idx_btc_data_ts on btc_data(ts) --
both confirmed present in production before writing this module; see
PR description for the real EXPLAIN QUERY PLAN output). No network, no
D1 connection required to run these.

Run with: python3 -m pytest research/test_outcome_engine.py -v
"""
import inspect
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import outcome_engine  # noqa: E402

HOUR = 3600000
DAY = 24 * HOUR


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, horizon_hours INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX idx_predictions_horizon_ts ON predictions(horizon_hours, ts)")
    # Added by this PR's own migration (0009_outcome_engine_predictions_ts_index.sql) --
    # see outcome_engine.py's module docstring for why it's needed.
    conn.execute("CREATE INDEX idx_predictions_ts ON predictions(ts)")
    conn.execute("""CREATE TABLE btc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
    )""")
    conn.execute("CREATE INDEX idx_btc_data_ts ON btc_data(ts)")
    return conn


def insert_prediction(conn, ts, horizon_hours=24):
    conn.execute("INSERT INTO predictions (ts, horizon_hours) VALUES (?, ?)", (ts, horizon_hours))


def insert_price(conn, ts, price):
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))


# ---- Requirement: mandatory bounds (mirrors resolver.py's own contract) ----

def test_start_ts_and_end_ts_have_no_default_value():
    sig = inspect.signature(outcome_engine.compute_forward_returns)
    for name in ("start_ts", "end_ts"):
        assert sig.parameters[name].default is inspect.Parameter.empty


def test_missing_bounds_raise_typeerror():
    conn = fresh_db()
    with pytest.raises(TypeError):
        outcome_engine.compute_forward_returns(conn, horizon_hours=1)
    conn.close()


def test_none_bounds_raise_valueerror():
    conn = fresh_db()
    with pytest.raises(ValueError):
        outcome_engine.compute_forward_returns(conn, start_ts=None, end_ts=1000, horizon_hours=1)
    with pytest.raises(ValueError):
        outcome_engine.compute_forward_returns(conn, start_ts=1000, end_ts=None, horizon_hours=1)
    conn.close()


def test_reversed_window_rejected():
    conn = fresh_db()
    with pytest.raises(ValueError):
        outcome_engine.compute_forward_returns(conn, start_ts=2000, end_ts=1000, horizon_hours=1)
    conn.close()


def test_empty_range_returns_empty_list_not_error():
    conn = fresh_db()
    result = outcome_engine.compute_forward_returns(conn, start_ts=1000, end_ts=2000, horizon_hours=1)
    assert result == []
    conn.close()


def test_excessively_large_window_rejected():
    conn = fresh_db()
    with pytest.raises(ValueError):
        outcome_engine.compute_forward_returns(
            conn, start_ts=0, end_ts=outcome_engine.MAX_WINDOW_MS + 1, horizon_hours=1
        )
    conn.close()


def test_window_exactly_at_max_is_accepted():
    conn = fresh_db()
    outcome_engine.compute_forward_returns(
        conn, start_ts=0, end_ts=outcome_engine.MAX_WINDOW_MS, horizon_hours=1
    )
    conn.close()


def test_unsupported_horizon_rejected():
    conn = fresh_db()
    with pytest.raises(ValueError):
        outcome_engine.compute_forward_returns(conn, start_ts=0, end_ts=1000, horizon_hours=2)
    conn.close()


def test_all_documented_horizons_accepted():
    conn = fresh_db()
    for h in (1, 3, 6, 12, 24):
        outcome_engine.compute_forward_returns(conn, start_ts=0, end_ts=DAY, horizon_hours=h)
    conn.close()


# ---- Horizon filtering / OUTCOME_HORIZON_MS correctness ----

def test_horizon_ms_mapping_matches_documented_hours():
    assert outcome_engine.OUTCOME_HORIZON_MS[1] == 3600000
    assert outcome_engine.OUTCOME_HORIZON_MS[3] == 10800000
    assert outcome_engine.OUTCOME_HORIZON_MS[6] == 21600000
    assert outcome_engine.OUTCOME_HORIZON_MS[12] == 43200000
    assert outcome_engine.OUTCOME_HORIZON_MS[24] == 86400000


# ---- Core outcome computation ----

def test_simple_resolved_case_computes_correct_return():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base, 100000.0)
    insert_price(conn, base + HOUR, 101000.0)
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert len(result) == 1
    row = result[0]
    assert row["outcome_status"] == "RESOLVED"
    assert row["btc_price_at_prediction"] == 100000.0
    assert row["realized_btc_price"] == 101000.0
    assert abs(row["forward_return_pct"] - 1.0) < 1e-9
    assert abs(row["absolute_forward_return_pct"] - 1.0) < 1e-9
    assert row["realized_direction"] == "UP"
    conn.close()


def test_negative_return_direction_is_down():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base, 100000.0)
    insert_price(conn, base + HOUR, 99000.0)
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert result[0]["realized_direction"] == "DOWN"
    assert result[0]["forward_return_pct"] < 0
    conn.close()


def test_exact_zero_return_direction_is_flat():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base, 100000.0)
    insert_price(conn, base + HOUR, 100000.0)
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert result[0]["realized_direction"] == "FLAT"
    assert result[0]["forward_return_pct"] == 0
    conn.close()


def test_absolute_forward_return_calculation():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base, 100000.0)
    insert_price(conn, base + HOUR, 95000.0)  # -5% signed, 5% absolute
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert abs(result[0]["forward_return_pct"] - (-5.0)) < 1e-9
    assert abs(result[0]["absolute_forward_return_pct"] - 5.0) < 1e-9
    conn.close()


# ---- Missing outcome / no silent interpolation ----

def test_missing_future_price_point_is_unresolved_not_dropped():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base, 100000.0)
    # No btc_data point anywhere near base + 1h.
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert len(result) == 1  # row is present, not dropped
    assert result[0]["outcome_status"] == "UNRESOLVED_NO_FUTURE_PRICE_POINT"
    assert result[0]["forward_return_pct"] is None
    assert result[0]["absolute_forward_return_pct"] is None
    assert result[0]["realized_direction"] is None
    conn.close()


def test_missing_price_at_prediction_time_is_unresolved():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    # No btc_data at or before base at all.
    insert_price(conn, base + HOUR, 101000.0)
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert result[0]["outcome_status"] == "UNRESOLVED_NO_FUTURE_PRICE_POINT"
    conn.close()


def test_stale_cadence_gap_wider_than_horizon_is_unresolved_no_lookahead_substitute():
    """If the only 'future' price point available is not actually after
    the prediction ts (e.g. the correlated subquery would otherwise
    reuse the SAME price_now row because no newer observation exists
    yet), this must be reported as unresolved, never as a fabricated
    zero return."""
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base, 100000.0)
    # Next real observation is 2 hours later -- outside the 1h horizon
    # window relative to base, so "as of base+1h" there is no new point.
    insert_price(conn, base + 2 * HOUR, 105000.0)
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert result[0]["outcome_status"] == "UNRESOLVED_NO_FUTURE_PRICE_POINT"
    conn.close()


def test_future_ts_strictly_after_prediction_ts_required():
    """A future_ts exactly equal to prediction_ts (degenerate case: the
    only btc_data row is the prediction-time row itself) must not be
    treated as a resolved zero-return outcome."""
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base, 100000.0)  # only price point that exists
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert result[0]["outcome_status"] == "UNRESOLVED_NO_FUTURE_PRICE_POINT"
    conn.close()


# ---- No look-ahead ----

def test_no_lookahead_price_now_never_uses_future_price():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base - HOUR, 90000.0)   # prior price -- correct price_now
    insert_price(conn, base + 30 * 60 * 1000, 999999.0)  # future price, must NOT be price_now
    insert_price(conn, base + HOUR, 91000.0)
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert result[0]["btc_price_at_prediction"] == 90000.0
    conn.close()


def test_price_exactly_at_prediction_ts_is_eligible_for_price_now():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base, 100000.0)  # exactly at prediction ts
    insert_price(conn, base + HOUR, 102000.0)
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert result[0]["btc_price_at_prediction"] == 100000.0
    conn.close()


# ---- Horizon selects a genuinely different future point per call ----

def test_different_horizons_select_different_future_points():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base)
    insert_price(conn, base, 100000.0)
    insert_price(conn, base + HOUR, 101000.0)
    insert_price(conn, base + 6 * HOUR, 106000.0)
    insert_price(conn, base + DAY, 110000.0)

    r1 = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + 2 * DAY, horizon_hours=1)
    r6 = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + 2 * DAY, horizon_hours=6)
    r24 = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + 2 * DAY, horizon_hours=24)

    assert r1[0]["realized_btc_price"] == 101000.0
    assert r6[0]["realized_btc_price"] == 106000.0
    assert r24[0]["realized_btc_price"] == 110000.0
    conn.close()


# ---- prediction_horizon_hours passthrough (distinct axis) ----

def test_prediction_horizon_hours_is_passed_through_unmodified():
    conn = fresh_db()
    base = 1780000000000
    insert_prediction(conn, base, horizon_hours=12)
    insert_price(conn, base, 100000.0)
    insert_price(conn, base + HOUR, 100500.0)
    result = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=1)
    assert result[0]["prediction_horizon_hours"] == 12  # the PREDICTION's own horizon
    assert result[0]["horizon_hours"] == 1               # the OUTCOME horizon requested


# ---- Determinism ----

def test_deterministic_repeated_execution():
    conn = fresh_db()
    base = 1780000000000
    for i in range(20):
        insert_prediction(conn, base + i * HOUR)
    for i in range(30):
        insert_price(conn, base + i * HOUR, 90000.0 + i * 137.5)
    first = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=6)
    second = outcome_engine.compute_forward_returns(conn, start_ts=base, end_ts=base + DAY, horizon_hours=6)
    assert first == second
    conn.close()


# ---- Bounded query behavior: read volume scales with window, not table size ----

def _seed_realistic(conn, n_predictions=2000, n_prices=5000, base=1780000000000):
    for i in range(n_predictions):
        insert_prediction(conn, base + i * HOUR, horizon_hours=24 if i % 2 else 12)
    for i in range(n_prices):
        insert_price(conn, base + i * (HOUR // 2), 90000.0 + (i % 50) * 10)


def test_read_volume_scales_with_window_not_table_size():
    conn = fresh_db()
    base = 1780000000000
    _seed_realistic(conn, base=base)
    small_window_rows = outcome_engine.compute_forward_returns(
        conn, start_ts=base, end_ts=base + 10 * HOUR, horizon_hours=1
    )
    assert len(small_window_rows) <= 11  # bounded by the window, not the 2000-row table


def test_query_plan_uses_indexes_not_table_scans():
    conn = fresh_db()
    base = 1780000000000
    _seed_realistic(conn, base=base)
    plan = outcome_engine.explain_outcome_query_plan(conn, base, base + DAY, 1)
    plan_text = " ".join(str(row) for row in plan).upper()
    # With idx_predictions_ts present (this PR's migration), the outer
    # query must be a genuine bounded SEARCH, not a full SCAN -- see
    # outcome_engine.py's module docstring for why a plain SCAN would
    # otherwise occur (the composite horizon_hours index can't serve a
    # ts-only range filter).
    assert "SEARCH P USING" in plan_text and "IDX_PREDICTIONS_TS" in plan_text, (
        f"expected a bounded search on predictions.ts, got: {plan}"
    )
    assert "USING INDEX IDX_BTC_DATA_TS" in plan_text, f"expected btc_data lookups to use idx_btc_data_ts, got: {plan}"
    assert "SCAN P" not in plan_text.replace("SEARCH P", ""), f"predictions must not be table/full-index-scanned: {plan}"


# ---- Safety invariants (mirror resolver.py's own tests) ----

def test_sql_template_contains_the_four_bind_parameters():
    assert outcome_engine.OUTCOME_SQL.count("?") == 4


def test_no_unbounded_query_string_exists_anywhere_in_the_module():
    source = inspect.getsource(outcome_engine)
    assert "SELECT *" not in source


def test_module_makes_no_network_or_llm_calls():
    source = inspect.getsource(outcome_engine)
    for forbidden in ["requests.", "urllib", "httpx", "openai", "anthropic"]:
        assert forbidden not in source


def test_module_never_writes_to_the_database():
    source = inspect.getsource(outcome_engine)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in source
