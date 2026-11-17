"""Resolution-rule audit: the frozen rule skips a tick written seconds before target_ts; the slot rule keeps it;
genuine gaps stay gaps; nothing is fabricated."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("rsa", Path(__file__).resolve().parent / "resolution_slot_audit.py")
rsa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsa)

H = 3600_000
T0 = 1794841267811                       # 2026-11-16 15:01:07.811 UTC: a real origin (tick price timestamp)


def ticks(start, n, prices):
    return [{"ts": start + k * 3 * H, "btc_price": prices[k]} for k in range(n)]


def test_tick_written_seconds_before_target_is_skipped_by_frozen_rule_but_kept_by_slot_rule():
    # the tick 12 h later is written 7.83 s before target (as on 2026-11-17 03:00:59.981 vs target 03:01:07.811)
    prices = [{"ts": T0, "btc_price": 64980.5}, {"ts": T0 + 12 * H - 7830, "btc_price": 64498.9},
              {"ts": T0 + 15 * H - 2000, "btc_price": 65500.0}]
    fc = [{"id": "a", "horizon_hours": 12, "input_end_ts": T0, "target_ts": T0 + 12 * H, "price_at_prediction": 64980.5,
           "direction": "DOWN"}]
    r = rsa.audit(fc, prices)
    row = r["rows"][0]
    assert row["frozen_price_ts"] == T0 + 15 * H - 2000 and row["frozen_late_ms"] > 2.9 * H
    assert row["slot_price_ts"] == T0 + 12 * H - 7830 and row["slot_early_ms"] == 7830
    assert (row["frozen_actual"], row["slot_actual"]) == ("UP", "DOWN")
    assert row["differs"] and row["correctness_changes"]
    assert r["summary"] == {"n": 1, "differs": 1, "correctness_changes": 1, "unresolvable_frozen": 0, "late_over_1h_frozen": 1}


def test_tick_at_or_after_target_is_the_same_under_both_rules():
    prices = ticks(T0, 6, [100, 101, 102, 103, 104, 105])
    fc = [{"id": "b", "horizon_hours": 12, "input_end_ts": T0, "target_ts": T0 + 12 * H, "price_at_prediction": 100,
           "direction": "UP"}]
    row = rsa.audit(fc, prices)["rows"][0]
    assert row["frozen_price_ts"] == row["slot_price_ts"] == T0 + 12 * H and not row["differs"]


def test_tolerance_never_reaches_the_previous_tick_and_gaps_are_not_filled():
    prices = [{"ts": T0 + 9 * H, "btc_price": 90}, {"ts": T0 + 21 * H, "btc_price": 110}]     # ticks 12 h and 15-18 h missing
    fc = [{"id": "c", "horizon_hours": 12, "input_end_ts": T0, "target_ts": T0 + 12 * H, "price_at_prediction": 100,
           "direction": "UP"}]
    row = rsa.audit(fc, prices)["rows"][0]
    assert row["slot_price_ts"] == row["frozen_price_ts"] == T0 + 21 * H        # 3 h earlier tick is never used
    assert rsa.SLOT_TOLERANCE_MS < rsa.TICK_MS / 10


def test_no_price_yet_is_unresolvable_not_fabricated():
    r = rsa.audit([{"id": "d", "horizon_hours": 12, "input_end_ts": T0, "target_ts": T0 + 12 * H,
                    "price_at_prediction": 100, "direction": "UP"}], [{"ts": T0, "btc_price": 100}])
    assert r["rows"] == [] and r["summary"]["unresolvable_frozen"] == 1
