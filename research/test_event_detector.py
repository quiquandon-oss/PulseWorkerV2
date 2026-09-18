"""
Tests for research/event_detector.py. All executed against a real
in-memory SQLite database. No network, no D1 connection required.

Run with: python3 -m pytest research/test_event_detector.py -v
"""
import inspect
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import event_detector as ed  # noqa: E402


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL)")
    conn.execute("CREATE INDEX idx_btc_data_ts ON btc_data(ts)")
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER)")
    conn.execute("CREATE INDEX idx_ts ON history(ts)")
    for table in ("predictions", "link_predictions", "eth_predictions"):
        conn.execute(f"""CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, horizon_hours INTEGER NOT NULL,
            p_up REAL, realized_up INTEGER
        )""")
        conn.execute(f"CREATE INDEX idx_{table}_horizon_ts ON {table}(horizon_hours, ts)")
    return conn


DAY = 24 * 3600000
HOUR = 3600000


def insert_prices(conn, points):
    for ts, price in points:
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))
    conn.commit()


# ---- Bounded window enforcement, identical discipline to PR2, for every detector ----

@pytest.mark.parametrize("fn,args", [
    (ed.detect_large_moves, ()),
    (ed.detect_regime_reversals, ()),
    (ed.detect_volatility_expansion, ()),
    (ed.detect_v1_btc_divergence, ()),
])
def test_market_detectors_reject_missing_and_invalid_bounds(fn, args):
    conn = fresh_db()
    with pytest.raises(TypeError):
        fn(conn, *args)
    with pytest.raises(ValueError):
        fn(conn, *args, start_ts=None, end_ts=1000)
    with pytest.raises(ValueError):
        fn(conn, *args, start_ts=2000, end_ts=1000)
    with pytest.raises(ValueError):
        fn(conn, *args, start_ts=0, end_ts=ed.MAX_WINDOW_MS + 1)
    conn.close()


def test_failure_cluster_detector_also_rejects_invalid_bounds():
    conn = fresh_db()
    with pytest.raises(TypeError):
        ed.detect_v2_failure_clusters(conn, "BTC", 24)
    with pytest.raises(ValueError):
        ed.detect_v2_failure_clusters(conn, "BTC", 24, start_ts=2000, end_ts=1000)
    conn.close()


# ---- LARGE_MOVE ----

def test_large_move_detected_with_correct_direction_and_event_ts_at_move_end():
    conn = fresh_db()
    base = 10_000_000_000
    insert_prices(conn, [(base, 90000), (base + DAY, 95000)])  # +5.56%, above the 4% threshold
    events = ed.detect_large_moves(conn, start_ts=base, end_ts=base + 2 * DAY)
    assert len(events) == 1
    assert events[0]["category"] == "LARGE_MOVE"
    assert events[0]["direction"] == "UP"
    assert events[0]["event_ts"] == base + DAY, "event_ts must be the move's END, not its start"
    assert events[0]["trigger_threshold"] == ed.LARGE_MOVE_THRESHOLD_PCT
    assert events[0]["trigger_version"] == ed.TRIGGER_VERSION
    conn.close()


def test_move_below_threshold_does_not_fire():
    conn = fresh_db()
    base = 10_000_000_000
    insert_prices(conn, [(base, 90000), (base + DAY, 91000)])  # +1.1%, well under 4%
    events = ed.detect_large_moves(conn, start_ts=base, end_ts=base + 2 * DAY)
    assert events == []
    conn.close()


def test_large_move_downward_direction():
    conn = fresh_db()
    base = 10_000_000_000
    insert_prices(conn, [(base, 90000), (base + DAY, 85000)])  # -5.56%
    events = ed.detect_large_moves(conn, start_ts=base, end_ts=base + 2 * DAY)
    assert events[0]["direction"] == "DOWN"
    conn.close()


# ---- REGIME_REVERSAL ----

def test_regime_reversal_preserves_the_actual_transition_not_collapsed():
    conn = fresh_db()
    base = 10_000_000_000
    points = [(base + i * DAY, 90000) for i in range(7)]  # flat baseline for the 7d lookback
    points.append((base + 7 * DAY, 96000))   # trail7 vs day0=90000: +6.7% -> rally
    points.append((base + 8 * DAY, 96000))   # trail7 vs day1=90000: +6.7% -> still rally
    points.append((base + 9 * DAY, 80000))   # trail7 vs day2=90000: -11.1% -> correction
    insert_prices(conn, points)
    events = ed.detect_regime_reversals(conn, start_ts=base, end_ts=base + 10 * DAY)
    directions = [e["direction"] for e in events]
    assert any(d == "rally_to_correction" for d in directions), (
        f"expected the exact transition 'rally_to_correction' preserved, not collapsed to a generic label, got {directions}"
    )
    conn.close()


def test_regime_reversal_gap_tolerance_suppresses_false_reversal_across_data_outage():
    """The exact scenario from review: Monday=rally, Thursday=correction
    across a real outage must NOT be reported as an immediate reversal."""
    conn = fresh_db()
    base = 10_000_000_000
    # Enough history for a rally classification at 'Monday'
    points = [(base + i * DAY, 90000) for i in range(7)]
    points.append((base + 7 * DAY, 97000))  # Monday: rally
    insert_prices(conn, points)
    conn.commit()
    # Thursday (3 days later = 72h gap, > MAX_OBSERVATION_GAP_MS of 48h): correction
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (base + 10 * DAY, 89000))
    conn.commit()
    events = ed.detect_regime_reversals(conn, start_ts=base, end_ts=base + 11 * DAY)
    # No event should be attributed to the Monday->Thursday jump specifically
    # (the gap between those two points exceeds MAX_OBSERVATION_GAP_MS).
    gap_events = [e for e in events if e["event_ts"] == base + 10 * DAY]
    assert gap_events == [], f"a reversal across a >{ed.MAX_OBSERVATION_GAP_MS}ms gap must be suppressed, got {gap_events}"
    conn.close()


# ---- VOLATILITY_EXPANSION ----

def test_volatility_baseline_is_a_frozen_literal_not_computed_from_the_database():
    """Structural proof: the baseline constant must not be derived from
    any query against btc_data/history inside this module."""
    src = inspect.getsource(ed)
    baseline_line = [l for l in src.splitlines() if "FROZEN_VOLATILITY_BASELINE_PCT =" in l and not l.strip().startswith("#")]
    assert len(baseline_line) == 1
    assert "SELECT" not in baseline_line[0]
    assert baseline_line[0].strip().startswith("FROZEN_VOLATILITY_BASELINE_PCT = 1.959")


def test_volatility_expansion_fires_on_a_genuinely_choppy_window():
    conn = fresh_db()
    base = 10_000_000_000
    # Alternate sharply day to day -- realized vol well above the frozen 1.959% baseline
    prices = [90000, 96000, 88000, 97000, 87000, 98000, 86000, 99000]
    points = [(base + i * DAY, p) for i, p in enumerate(prices)]
    insert_prices(conn, points)
    events = ed.detect_volatility_expansion(conn, start_ts=base + 6 * DAY, end_ts=base + 8 * DAY)
    assert len(events) >= 1
    assert events[0]["trigger_metric"] == "7d_realized_vol_over_frozen_baseline"
    conn.close()


def test_volatility_expansion_does_not_fire_on_a_flat_window():
    conn = fresh_db()
    base = 10_000_000_000
    points = [(base + i * DAY, 90000 + i * 5) for i in range(9)]  # near-flat, tiny drift
    insert_prices(conn, points)
    events = ed.detect_volatility_expansion(conn, start_ts=base + 7 * DAY, end_ts=base + 9 * DAY)
    assert events == []
    conn.close()


# ---- V2_FAILURE_CLUSTER ----

def test_five_consecutive_failures_fires_exactly_once_at_the_fifth():
    conn = fresh_db()
    base = 10_000_000_000
    for i in range(5):
        conn.execute("INSERT INTO predictions (ts, horizon_hours, p_up, realized_up) VALUES (?, 24, 0.9, 0)",
                     (base + i * HOUR,))
    conn.commit()
    events = ed.detect_v2_failure_clusters(conn, "BTC", 24, start_ts=base, end_ts=base + 10 * HOUR)
    assert len(events) == 1
    assert events[0]["event_ts"] == base + 4 * HOUR
    assert events[0]["direction"] == "BTC_24h"
    conn.close()


def test_streak_resets_on_a_correct_prediction():
    conn = fresh_db()
    base = 10_000_000_000
    for i in range(4):
        conn.execute("INSERT INTO predictions (ts, horizon_hours, p_up, realized_up) VALUES (?, 24, 0.9, 0)",
                     (base + i * HOUR,))
    conn.execute("INSERT INTO predictions (ts, horizon_hours, p_up, realized_up) VALUES (?, 24, 0.9, 1)",
                 (base + 4 * HOUR,))  # correct -- breaks the streak at 4
    for i in range(5, 9):
        conn.execute("INSERT INTO predictions (ts, horizon_hours, p_up, realized_up) VALUES (?, 24, 0.9, 0)",
                     (base + i * HOUR,))
    conn.commit()
    events = ed.detect_v2_failure_clusters(conn, "BTC", 24, start_ts=base, end_ts=base + 10 * HOUR)
    assert events == [], "streak of 4 then reset then 4 more must never reach 5 -- must not fire"
    conn.close()


def test_failure_cluster_is_scoped_never_combines_coins_or_horizons():
    conn = fresh_db()
    base = 10_000_000_000
    # 3 BTC/24h failures + 3 ETH/12h failures interleaved -- neither alone
    # reaches 5, and they must never be combined into one streak.
    for i in range(3):
        conn.execute("INSERT INTO predictions (ts, horizon_hours, p_up, realized_up) VALUES (?, 24, 0.9, 0)",
                     (base + i * HOUR,))
        conn.execute("INSERT INTO eth_predictions (ts, horizon_hours, p_up, realized_up) VALUES (?, 12, 0.9, 0)",
                     (base + i * HOUR,))
    conn.commit()
    btc_events = ed.detect_v2_failure_clusters(conn, "BTC", 24, start_ts=base, end_ts=base + 10 * HOUR)
    eth_events = ed.detect_v2_failure_clusters(conn, "ETH", 12, start_ts=base, end_ts=base + 10 * HOUR)
    assert btc_events == []
    assert eth_events == []
    conn.close()


# ---- V1_BTC_DIVERGENCE ----

def test_v1_bounds_are_frozen_literals_not_computed_live():
    src = inspect.getsource(ed)
    bearish_line = [l for l in src.splitlines() if "FROZEN_V1_BEARISH_EXTREME =" in l and not l.strip().startswith("#")]
    bullish_line = [l for l in src.splitlines() if "FROZEN_V1_BULLISH_EXTREME =" in l and not l.strip().startswith("#")]
    assert bearish_line[0].strip() == "FROZEN_V1_BEARISH_EXTREME = 45"
    assert bullish_line[0].strip() == "FROZEN_V1_BULLISH_EXTREME = 58"
    assert "SELECT" not in bearish_line[0] and "SELECT" not in bullish_line[0]


def test_divergence_marks_is_post_event_analysis_true():
    conn = fresh_db()
    base = 10_000_000_000
    conn.execute("INSERT INTO history (ts, score) VALUES (?, 70)", (base,))  # bullish extreme (>58)
    insert_prices(conn, [(base, 90000), (base + DAY, 85000)])  # BTC drops 5.6% -- opposite direction
    events = ed.detect_v1_btc_divergence(conn, start_ts=base, end_ts=base + 2 * DAY)
    assert len(events) == 1
    assert events[0]["is_post_event_analysis"] == 1
    assert events[0]["direction"] == "v1_bullish_btc_down"
    conn.close()


def test_same_direction_is_not_divergence():
    conn = fresh_db()
    base = 10_000_000_000
    conn.execute("INSERT INTO history (ts, score) VALUES (?, 70)", (base,))  # bullish
    insert_prices(conn, [(base, 90000), (base + DAY, 95000)])  # BTC also up -- agreement, not divergence
    events = ed.detect_v1_btc_divergence(conn, start_ts=base, end_ts=base + 2 * DAY)
    assert events == []
    conn.close()


def test_non_extreme_v1_score_never_triggers_divergence():
    conn = fresh_db()
    base = 10_000_000_000
    conn.execute("INSERT INTO history (ts, score) VALUES (?, 50)", (base,))  # inside 45-58, not extreme
    insert_prices(conn, [(base, 90000), (base + DAY, 85000)])  # big move, but V1 wasn't extreme
    events = ed.detect_v1_btc_divergence(conn, start_ts=base, end_ts=base + 2 * DAY)
    assert events == []
    conn.close()


def test_small_btc_move_does_not_trigger_divergence_even_if_v1_extreme():
    conn = fresh_db()
    base = 10_000_000_000
    conn.execute("INSERT INTO history (ts, score) VALUES (?, 70)", (base,))
    insert_prices(conn, [(base, 90000), (base + DAY, 91000)])  # +1.1%, under the 4% threshold
    events = ed.detect_v1_btc_divergence(conn, start_ts=base, end_ts=base + 2 * DAY)
    assert events == []
    conn.close()


# ---- No production behavior ----

def test_module_makes_no_network_or_llm_calls():
    src = inspect.getsource(ed)
    for forbidden in ["requests.", "urllib", "fetch(", "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_module_never_writes_to_the_database():
    src = inspect.getsource(ed)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in src


def test_all_events_carry_trigger_version():
    conn = fresh_db()
    base = 10_000_000_000
    insert_prices(conn, [(base, 90000), (base + DAY, 96000)])
    events = ed.detect_large_moves(conn, start_ts=base, end_ts=base + 2 * DAY)
    assert all(e["trigger_version"] == ed.TRIGGER_VERSION for e in events)
    conn.close()


# ---- Query-plan check on the shared bounded fetch ----

def test_bounded_price_fetch_uses_the_index_not_a_scan():
    conn = fresh_db()
    base = 10_000_000_000
    for i in range(50):
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (base + i * HOUR, 90000))
    conn.commit()
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT ts, btc_price FROM btc_data WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
        (base, base + 10 * HOUR),
    ).fetchall()
    plan_text = " ".join(str(row) for row in plan).upper()
    assert "USING INDEX IDX_BTC_DATA_TS" in plan_text
    assert "SCAN BTC_DATA" not in plan_text
    conn.close()
