import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare  # noqa: E402


def test_metrics_counts_calls_by_direction():
    m = compare.metrics([(0.8, 1), (0.6, 0), (0.4, 0), (0.2, 1), (0.5, 1)])
    assert m["n"] == 5 and m["up_calls"] == 3 and m["down_calls"] == 2
    assert m["accuracy"] == 0.6 and m["up_precision"] == 0.6667 and m["up_recall"] == 0.6667
    assert m["down_precision"] == 0.5 and m["down_recall"] == 0.5
    assert compare.metrics([]) == {"n": 0}


def test_compare_variant_reports_changed_calls_and_who_was_right():
    a = {1: 0.7, 2: 0.4, 3: 0.6, 4: None}
    b = {1: 0.3, 2: 0.4, 3: 0.9, 4: 0.5}
    r = compare.compare_variant(a, b, {1: 1, 2: 0, 3: None})
    assert r["compared"] == 3 and r["changed_calls"] == 1 and r["resolved"] == 2
    assert r["changed_calls_where_stored_series_was_right"] == 1 and r["changed_calls_where_one_row_per_run_was_right"] == 0
    assert r["stored_series"]["accuracy"] == 1.0 and r["one_row_per_run"]["accuracy"] == 0.5


def test_non_overlapping_windows():
    h = 3600 * 1000
    assert compare.non_overlapping([0, 3 * h, 12 * h, 13 * h, 24 * h], 12) == 3
