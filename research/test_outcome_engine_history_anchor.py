"""
Tests for research/outcome_engine.py's PR5c addition:
compute_forward_returns_from_history() / explain_history_outcome_query_plan(),
and the shared _resolve_outcome() no-lookahead helper both anchor functions
now use. test_outcome_engine.py (PR5b, unmodified in behavior) already
covers compute_forward_returns() itself -- this file covers only the new
history-anchored surface plus a direct proof that the refactor did not
change the shared resolution rule.

Run with: python3 -m pytest research/test_outcome_engine_history_anchor.py -v
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
    conn.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX idx_ts ON history(ts)")
    conn.execute("""CREATE TABLE btc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
    )""")
    conn.execute("CREATE INDEX idx_btc_data_ts ON btc_data(ts)")
    return conn


def insert_history(conn, ts, score=50):
    conn.execute("INSERT INTO history (ts, score) VALUES (?, ?)", (ts, score))


def insert_price(conn, ts, price):
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))


def test_bounds_required_no_default():
    sig = inspect.signature(outcome_engine.compute_forward_returns_from_history)
    for name in ("start_ts", "end_ts"):
        assert sig.parameters[name].default is inspect.Parameter.empty


def test_missing_bounds_raise():
    conn = fresh_db()
    with pytest.raises(TypeError):
        outcome_engine.compute_forward_returns_from_history(conn, horizon_hours=1)
    with pytest.raises(ValueError):
        outcome_engine.compute_forward_returns_from_history(conn, start_ts=None, end_ts=1000, horizon_hours=1)
    conn.close()


def test_unsupported_horizon_rejected():
    conn = fresh_db()
    with pytest.raises(ValueError):
        outcome_engine.compute_forward_returns_from_history(conn, start_ts=0, end_ts=1000, horizon_hours=2)
    conn.close()


def test_resolved_forward_return_computed_correctly():
    conn = fresh_db()
    insert_history(conn, 1000, score=42)
    insert_price(conn, 1000, 100.0)
    insert_price(conn, 1000 + HOUR, 110.0)
    results = outcome_engine.compute_forward_returns_from_history(conn, 0, 2000 + HOUR, horizon_hours=1)
    assert len(results) == 1
    row = results[0]
    assert row["outcome_status"] == "RESOLVED"
    assert row["v1_composite"] == 42
    assert row["forward_return_pct"] == pytest.approx(10.0)
    assert row["realized_direction"] == "UP"
    conn.close()


def test_unresolved_when_no_future_price_point():
    conn = fresh_db()
    insert_history(conn, 1000, score=10)
    insert_price(conn, 1000, 100.0)
    # no future price within the horizon
    results = outcome_engine.compute_forward_returns_from_history(conn, 0, 2000, horizon_hours=1)
    assert len(results) == 1
    assert results[0]["outcome_status"] == "UNRESOLVED_NO_FUTURE_PRICE_POINT"
    assert results[0]["forward_return_pct"] is None
    conn.close()


def test_every_history_row_represented_exactly_once():
    conn = fresh_db()
    for i in range(5):
        insert_history(conn, i * HOUR, score=i)
        insert_price(conn, i * HOUR, 100.0 + i)
    results = outcome_engine.compute_forward_returns_from_history(conn, 0, 5 * HOUR, horizon_hours=1)
    assert len(results) == 5
    conn.close()


def test_v1_composite_carried_through():
    conn = fresh_db()
    insert_history(conn, 1000, score=77)
    insert_price(conn, 1000, 50.0)
    results = outcome_engine.compute_forward_returns_from_history(conn, 0, 2000, horizon_hours=3)
    assert results[0]["v1_composite"] == 77
    conn.close()


def test_all_supported_horizons_accepted():
    conn = fresh_db()
    insert_history(conn, 0, score=1)
    insert_price(conn, 0, 100.0)
    for h in (1, 3, 6, 12, 24):
        insert_price(conn, h * HOUR, 105.0)
        results = outcome_engine.compute_forward_returns_from_history(conn, -1, h * HOUR + 1, horizon_hours=h)
        assert any(r["outcome_status"] == "RESOLVED" for r in results)
    conn.close()


def test_explain_query_plan_uses_index_not_scan():
    conn = fresh_db()
    for i in range(20):
        insert_history(conn, i * HOUR, score=i)
        insert_price(conn, i * HOUR, 100.0)
    plan_rows = outcome_engine.explain_history_outcome_query_plan(conn, 0, 20 * HOUR, horizon_hours=1)
    plan_text = " ".join(str(r) for r in plan_rows).upper()
    assert "SCAN HISTORY" not in plan_text.replace("SEARCH HISTORY", "")
    conn.close()


def test_shared_resolve_outcome_helper_no_future_ts_after_anchor_is_unresolved():
    # Direct proof the refactor's shared helper preserves the strict
    # future_ts > anchor_ts no-lookahead rule.
    forward_return_pct, absolute_forward_return_pct, realized_direction, outcome_status = (
        outcome_engine._resolve_outcome(anchor_ts=1000, price_now=100.0, future_ts=1000, price_future=110.0)
    )
    assert outcome_status == "UNRESOLVED_NO_FUTURE_PRICE_POINT"
    assert forward_return_pct is None


def test_module_never_writes_to_the_database():
    src = inspect.getsource(outcome_engine)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in src, f"outcome_engine.py must be read-only -- found {forbidden}"


def test_no_network_or_llm_calls():
    src = inspect.getsource(outcome_engine)
    for forbidden in ["requests.", "urllib", "fetch(", "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()
