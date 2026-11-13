import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exp002_progress as ep  # noqa: E402

S, H = ep.POPULATION_START, ep.H


def _a(pts, h=24, coin="BTC", ts=None):
    return {"coin": coin, "horizon_hours": str(h), "prediction_ts": str(pts), "ts": str(ts if ts is not None else pts + H)}


def _s(pts, h=24, coin="BTC"):
    return {"coin": coin, "horizon_hours": str(h), "prediction_ts": str(pts)}


def test_counts_rows_cycles_duplicates_and_selection_coverage():
    anomaly = [_a(S), _a(S), _a(S + 24 * H), _a(S + 30 * H), _a(S + 48 * H, h=12)]
    selection = [_s(S), _s(S), _s(S + 24 * H), _s(S + 48 * H, h=12)]
    r = ep.progress(anomaly, selection)["horizons"]
    assert r["24h"] == {"anomaly_rows": 4, "cycles": 3, "cycles_logged_more_than_once": 1, "cycles_with_no_selection_row": 1,
                        "cycles_with_one_selection_row": 1, "cycles_with_several_selection_rows": 1,
                        "non_overlapping_cycles_upper_bound_on_kept_units": 2, "checkpoint_reached": False}
    assert r["12h"]["cycles"] == 1 and r["12h"]["cycles_with_one_selection_row"] == 1


def test_ignores_rows_before_the_population_start_and_other_coins():
    r = ep.progress([_a(S - 30 * H, ts=S - H), _a(S, coin="ETH")], [])
    assert r["horizons"] == {}


def test_reports_no_calls_or_outcomes():
    r = ep.progress([_a(S)], [_s(S)])
    text = str(r)
    assert "p_up" not in text and "right" not in text and "accuracy" not in text


def test_checkpoint_is_not_declared_from_counts_alone():
    anomaly = [_a(S + i * 24 * H) for i in range(ep.CHECKPOINT)]
    r = ep.progress(anomaly, [])["horizons"]["24h"]
    assert r["non_overlapping_cycles_upper_bound_on_kept_units"] == ep.CHECKPOINT and r["checkpoint_reached"] is None
