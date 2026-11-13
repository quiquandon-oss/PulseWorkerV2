import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exp002_eval as ev  # noqa: E402

S, H = ev.POPULATION_START, ev.H   # S is 00:00 UTC, a scheduled hour


def _a(p, p_up, h=24, ts=None):
    return {"coin": "BTC", "horizon_hours": str(h), "prediction_ts": str(p), "ts": str(ts if ts is not None else p + H), "chosen_p_up_anomaly": str(p_up)}


def _s(i, p, p_up, h=24, lag=2000):
    return {"id": str(i), "coin": "BTC", "horizon_hours": str(h), "prediction_ts": str(p), "ts": str(p + lag), "chosen_p_up": str(p_up)}


def _prov(i, trigger="cron"):
    return {"selection_table": "selection_decisions", "selection_row_id": str(i), "selection_trigger": trigger}


def test_production_row_prefers_the_provenance_cron_row():
    rows = [_s(1, S + 70000, 0.2), _s(2, S + 70000, 0.9, lag=5000)]
    assert ev.production_row(rows, {2})[0]["id"] == "2"
    assert ev.production_row(rows, set()) == (rows[0], "timing")


def test_timing_rule_needs_a_scheduled_hour_and_a_short_lag():
    assert ev.production_row([_s(1, S + H + 70000, 0.2)], set()) == (None, "no_identifiable_cron_row")       # hour 1: not a tick
    assert ev.production_row([_s(1, S + 400000, 0.2)], set()) == (None, "no_identifiable_cron_row")          # 400 s after the hour
    assert ev.production_row([_s(1, S + 70000, 0.2, lag=61000)], set()) == (None, "no_identifiable_cron_row")
    assert ev.production_row([], set()) == (None, "no_selection_row")


def test_counts_only_below_the_checkpoint_and_no_outcome_is_read():
    p = S + 70000
    anomaly = [_a(p, 0.7), _a(p, 0.7, ts=p + 2 * H), _a(p + 3 * H, 0.7), _a(p + 24 * H, 0.3), _a(p + 48 * H, 0.6), _a(p + 72 * H, 0.6)]
    selection = [_s(1, p, 0.2), _s(2, p, 0.9, lag=3 * H), _s(3, p + 3 * H, 0.2), _s(4, p + 24 * H, 0.3), _s(5, p + 72 * H, 0.1, lag=3 * H)]
    r = ev.evaluate(anomaly, selection, [], price_rows=None)        # price rows are never touched below the checkpoint
    c = r["horizons"]["24h"]
    assert r["status"] == "INSUFFICIENT_SAMPLE" and "p_value" not in c and "shadow_right" not in c
    assert c == {"anomaly_rows": 6, "cycles": 5, "repeated_anomaly_rows_excluded": 1, "excluded_no_selection_row": 1,
                 "excluded_no_identifiable_cron_row": 1, "excluded_missing_stored_value": 0, "paired_by_provenance": 0, "paired_by_timing": 3,
                 "same_call_pairs_excluded": 1, "informative_units": 2, "informative_units_dropped_for_overlap": 1,
                 "kept_informative_units": 1, "checkpoint_reached": False}
    assert "PROPOSED" in r["pairing_rule"] and "does not measure two predictions" in r["disclosure"]


def test_resolution_rule():
    ts, px = [0, 10 * H, 24 * H + 30 * 60000, 50 * H], [100.0, 101.0, 102.0, 90.0]
    assert ev.resolve(ts, px, 0, 24) == 1                          # first price at or after 24 h is 30 min late: accepted, up
    assert ev.resolve(ts, px, 10 * H, 24) is None                  # next price after 34 h is 16 h late: excluded
    assert ev.resolve(ts, [100.0, 101.0, 100.0, 90.0], 0, 24) == 0  # equal is DOWN (strictly above 0 is UP)


def test_confirmatory_analysis_runs_only_at_the_checkpoint():
    anomaly, selection, prices = [], [], []
    for h in (12, 24):
        for i in range(3):
            p = S + i * 24 * H + 70000
            anomaly.append(_a(p, 0.9, h=h)); selection.append(_s(len(selection) + 1, p, 0.1, h=h))
    for i in range(0, 5 * 24):
        prices.append({"ts": str(S + i * H), "btc_price": str(100 + i)})   # always rising: shadow (UP) is right
    r = ev.evaluate(anomaly, selection, [], prices, checkpoint=3)
    assert r["status"] == "EVALUATED"
    for k in ("12h", "24h"):
        c = r["horizons"][k]
        assert c["kept_informative_units"] == 3 and c["shadow_right"] == 3 and c["p_value"] == 0.25 and c["supported"] is False
