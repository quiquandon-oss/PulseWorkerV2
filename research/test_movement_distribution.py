"""
Tests for research/movement_distribution.py.

Pure-function tests -- no database. Run with:
  python3 -m pytest research/test_movement_distribution.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import event_detector  # noqa: E402
import movement_distribution as md  # noqa: E402


def resolved_row(forward_return_pct):
    return {
        "forward_return_pct": forward_return_pct,
        "absolute_forward_return_pct": abs(forward_return_pct),
        "outcome_status": "RESOLVED",
    }


def unresolved_row():
    return {
        "forward_return_pct": None,
        "absolute_forward_return_pct": None,
        "outcome_status": "UNRESOLVED_NO_FUTURE_PRICE_POINT",
    }


# ---- Constant integrity: the duplicated PR3 threshold must never drift ----

def test_pr3_threshold_constant_matches_event_detector():
    assert md.PR3_LARGE_MOVE_THRESHOLD_PCT == event_detector.LARGE_MOVE_THRESHOLD_PCT


# ---- Empty range / insufficient data ----

def test_empty_input_returns_insufficient_data_not_error():
    summary = md.summarize_absolute_returns([])
    assert summary["sample_size_status"] == "INSUFFICIENT_DATA"
    assert summary["n"] == 0
    assert summary["mean"] is None
    assert summary["proportion_below_pr3_threshold"] is None


def test_all_unresolved_input_is_insufficient_data_and_counted():
    rows = [unresolved_row(), unresolved_row(), unresolved_row()]
    summary = md.summarize_absolute_returns(rows)
    assert summary["n"] == 0
    assert summary["n_unresolved_excluded"] == 3
    assert summary["sample_size_status"] == "INSUFFICIENT_DATA"


def test_unresolved_rows_are_excluded_but_counted_not_dropped_silently():
    rows = [resolved_row(0.5), resolved_row(-0.3), unresolved_row(), unresolved_row()]
    summary = md.summarize_absolute_returns(rows)
    assert summary["n"] == 2
    assert summary["n_unresolved_excluded"] == 2


def test_small_sample_caution_flag():
    rows = [resolved_row(0.1 * i) for i in range(1, 10)]  # 9 rows, below MIN_SAMPLE_FOR_RELIABLE_PERCENTILES
    summary = md.summarize_absolute_returns(rows)
    assert summary["n"] == 9
    assert summary["sample_size_status"] == "SMALL_SAMPLE_CAUTION"


def test_sample_at_or_above_threshold_is_ok():
    rows = [resolved_row(0.1 * i) for i in range(1, md.MIN_SAMPLE_FOR_RELIABLE_PERCENTILES + 1)]
    summary = md.summarize_absolute_returns(rows)
    assert summary["sample_size_status"] == "OK"


# ---- Core statistics correctness ----

def test_mean_median_stddev_min_max_known_values():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    rows = [resolved_row(v) for v in values]
    summary = md.summarize_absolute_returns(rows)
    assert summary["n"] == 5
    assert summary["mean"] == 3.0
    assert summary["median"] == 3.0
    assert summary["min"] == 1.0
    assert summary["max"] == 5.0
    # population stddev of [1,2,3,4,5] = sqrt(2) ~= 1.4142
    assert abs(summary["stddev"] - 1.4142135623730951) < 1e-9


def test_absolute_percentiles_computed_on_magnitude_not_signed_value():
    # Symmetric signed values -- median of signed should be ~0, but
    # abs percentiles must reflect only magnitude.
    values = [-5.0, -1.0, 1.0, 5.0]
    rows = [resolved_row(v) for v in values]
    summary = md.summarize_absolute_returns(rows)
    assert summary["median"] == 0.0 or abs(summary["median"]) <= 1.0  # exact value depends on nearest-rank convention
    assert summary["abs_p50"] in (1.0, 5.0)  # nearest-rank on [1,1,5,5]
    assert summary["max"] == 5.0
    assert summary["min"] == -5.0


def test_direction_counts():
    rows = [resolved_row(1.0), resolved_row(-1.0), resolved_row(-2.0), resolved_row(0.0)]
    summary = md.summarize_absolute_returns(rows)
    assert summary["count_positive"] == 1
    assert summary["count_negative"] == 2
    assert summary["count_flat"] == 1


# ---- Bucket assignment ----

def test_bucket_assignment_boundaries():
    rows = [
        resolved_row(0.5),   # <1%
        resolved_row(-0.99),  # <1%
        resolved_row(1.0),   # 1-2% (boundary inclusive on lower edge)
        resolved_row(1.99),  # 1-2%
        resolved_row(2.5),   # 2-3%
        resolved_row(-3.5),  # 3-4%
        resolved_row(4.0),   # >=4% (boundary inclusive)
        resolved_row(10.0),  # >=4%
    ]
    summary = md.summarize_absolute_returns(rows)
    assert summary["bucket_counts"]["<1%"] == 2
    assert summary["bucket_counts"]["1-2%"] == 2
    assert summary["bucket_counts"]["2-3%"] == 1
    assert summary["bucket_counts"]["3-4%"] == 1
    assert summary["bucket_counts"][">=4%"] == 2
    assert sum(summary["bucket_counts"].values()) == summary["n"]


def test_bucket_pct_of_n_sums_to_100():
    rows = [resolved_row(0.1 * i) for i in range(1, 41)]
    summary = md.summarize_absolute_returns(rows)
    total_pct = sum(summary["bucket_pct_of_n"].values())
    assert abs(total_pct - 100.0) < 1e-9


def test_proportion_below_pr3_threshold():
    rows = [resolved_row(1.0), resolved_row(2.0), resolved_row(5.0), resolved_row(6.0)]
    summary = md.summarize_absolute_returns(rows)
    assert summary["proportion_below_pr3_threshold"] == 0.5


# ---- Determinism ----

def test_deterministic_repeated_computation():
    rows = [resolved_row(0.37 * i - 5) for i in range(1, 200)]
    first = md.summarize_absolute_returns(rows)
    second = md.summarize_absolute_returns(rows)
    assert first == second


# ---- propose_movement_buckets: always PROPOSED, never frozen ----

def test_propose_movement_buckets_status_is_always_proposed():
    rows = [resolved_row(0.1 * i) for i in range(1, 100)]
    summary = md.summarize_absolute_returns(rows)
    proposal = md.propose_movement_buckets({24: summary})
    assert proposal["status"] == "PROPOSED"


def test_propose_movement_buckets_flags_insufficient_horizon():
    empty_summary = md.summarize_absolute_returns([])
    proposal = md.propose_movement_buckets({1: empty_summary})
    assert proposal["per_horizon"][1]["status"] == "INSUFFICIENT_DATA"


def test_propose_movement_buckets_reports_adequacy_per_bucket():
    rows = [resolved_row(0.1) for _ in range(50)] + [resolved_row(4.5) for _ in range(2)]
    summary = md.summarize_absolute_returns(rows)
    proposal = md.propose_movement_buckets({24: summary})
    buckets = proposal["per_horizon"][24]["buckets"]
    assert buckets["<1%"]["adequately_populated"] is True   # 50 >= MIN_SAMPLE_FOR_RELIABLE_PERCENTILES
    assert buckets[">=4%"]["adequately_populated"] is False  # 2 < MIN_SAMPLE_FOR_RELIABLE_PERCENTILES


def test_propose_movement_buckets_never_mutates_input_summary():
    rows = [resolved_row(0.1 * i) for i in range(1, 60)]
    summary = md.summarize_absolute_returns(rows)
    summary_copy_repr = repr(summary)
    md.propose_movement_buckets({6: summary})
    assert repr(summary) == summary_copy_repr


# ---- Safety invariants ----

def test_module_makes_no_network_llm_or_database_calls():
    import inspect
    source = inspect.getsource(md)
    for forbidden in ["requests.", "urllib", "httpx", "openai", "anthropic", "sqlite3", "conn.execute"]:
        assert forbidden not in source
