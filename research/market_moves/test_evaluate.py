"""Tests for research/market_moves/evaluate.py (definition v1.0.0, plan v1.0.0).

Synthetic candle sequences exist only inside these tests, to exercise each frozen rule at its boundary.
Run with: python3 -m pytest research/market_moves/test_evaluate.py -v
"""
import json
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import evaluate as ev  # noqa: E402

H, D = ev.H, ev.D
T0 = 1_780_000_000_000 // H * H  # hour-aligned start


def series(closes, start=T0, skip=()):
    rows = []
    for k, c in enumerate(closes):
        if k in skip:
            continue
        t = start + (k + 1) * H
        rows.append({"available_at": t, "open_ts": t - H, "close_ts": t - 1, "c": str(c)})
    return ev.Series(rows)


B1 = ev.make_return_classifier(D, Decimal("0.03"), Decimal("0.045"), Decimal("0.02"))


def run_b1(closes, **kw):
    s = series(closes, **kw)
    return s, ev.run_episodes(s, B1, s.t[-1] + H)["episodes"]


def test_frozen_files_match_pinned_hashes():
    definition, plan, def_sha, plan_sha = ev.load_frozen()
    assert def_sha == plan["definition_sha256"]
    assert definition["definition_version"] == "1.0.0" and plan["plan_version"] == "1.0.0"


def test_nearest_reference_ties_go_to_the_earlier_observation():
    s = series([100] * 5)
    target = (s.t[1] + s.t[2]) // 2  # exactly between two observations
    assert s.nearest(target, tol=H) == 1


def test_exactly_three_percent_is_not_a_crossing():
    _, eps = run_b1([100] * 25 + [103] * 5)
    assert eps == []
    _, eps = run_b1([100] * 25 + [Decimal("103.0001")] * 5)
    assert len(eps) == 1 and eps[0]["direction"] == "UP"


def test_up_episode_onset_confirmation_and_calm_resolution():
    closes = [100] * 25 + [101, 102, 103.5, 104, 104, 104, 104] + [104] * 30
    s, eps = run_b1(closes)
    assert len(eps) == 1
    e = eps[0]
    assert e["direction"] == "UP"
    assert e["first_crossing_ts"] == s.t[27]                     # 103.5 / 100 > 1.03
    assert e["onset_ts"] == s.t[24]                              # lowest close in [t0-24h, t0], ties to latest
    assert e["confirmation_rule"] == "persistence" and e["confirmation_ts"] == s.t[27] + 3 * H
    # 24 h after the move the return falls back below 2%; the 6th consecutive calm VALID hour resolves it
    assert e["resolution_reason"] == "CALM" and e["resolution_ts"] is not None
    assert e["magnitude"] == pytest.approx(0.04)


def test_magnitude_confirmation_beats_persistence():
    s, eps = run_b1([100] * 25 + [105] + [105] * 5)
    assert eps[0]["confirmation_rule"] == "magnitude" and eps[0]["confirmation_ts"] == s.t[25]


def test_opposite_crossing_ends_the_episode_and_starts_a_new_alerted_one():
    closes = [100] * 25 + [104] * 3 + [97] * 2 + [96] * 30
    _, eps = run_b1(closes)
    assert [e["direction"] for e in eps][:2] == ["UP", "DOWN"]
    assert eps[0]["resolution_reason"] == "OPPOSITE_CROSSING"


def test_same_direction_crossing_within_24h_of_resolution_reopens_without_alert():
    # +4% crossing, six calm hours (+1.5%) resolve it, then +4% again within 24 h of the last crossing
    _, eps = run_b1([100] * 25 + [104] + [Decimal("101.5")] * 6 + [104] * 3)
    assert len(eps) == 1 and len(eps[0]["merged_reopenings"]) == 1


def test_same_direction_crossing_more_than_24h_later_is_a_new_episode():
    _, eps = run_b1([100] * 25 + [104] + [Decimal("101.5")] * 26 + [Decimal("106")] * 3)
    assert [e["direction"] for e in eps] == ["UP", "UP"] and not eps[0]["merged_reopenings"]


def test_gap_over_two_hours_is_low_coverage_and_never_classified():
    s = series([100] * 25 + [104] * 5, skip=(10, 11, 12))  # a 4 h gap inside every 24 h window that follows
    assert B1(s, s.idx[T0 + 26 * H])["status"] == ev.LOW
    assert ev.run_episodes(s, B1, s.t[-1] + H)["episodes"] == []


def test_development_isolation_drops_observations_at_or_after_the_cutoff(tmp_path):
    for a in ev.ASSETS:
        d = tmp_path / "hourly" / a
        d.mkdir(parents=True)
        lines = [json.dumps({"available_at": T0 + k * H, "open_ts": T0 + (k - 1) * H, "close_ts": T0 + k * H - 1, "c": "1"}) for k in range(1, 11)]
        (d / "observations.jsonl").write_text("\n".join(lines) + "\n")
    out = ev.load_store(tmp_path, T0 + 5 * H, {})
    assert max(out["BTC"].t) < T0 + 5 * H and len(out["BTC"].t) == 4


def test_evaluation_period_is_sealed_without_the_plan_sha(tmp_path):
    with pytest.raises(SystemExit, match="sealed"):
        ev.evaluate(tmp_path, "evaluation", authorize=None)
    with pytest.raises(SystemExit, match="sealed"):
        ev.evaluate(tmp_path, "evaluation", authorize="0" * 64)


def test_matching_hit_duplicate_false_alarm_and_late():
    ep = {"episode_id": "e1", "direction": "UP", "onset_ts": 10 * H, "first_crossing_ts": 12 * H, "first_detection_ts": 12 * H,
          "window_end_ts": 20 * H, "resolution_ts": 20 * H, "magnitude": 0.05}
    alerts = [{"alert_ts": 13 * H, "direction": "UP"}, {"alert_ts": 14 * H, "direction": "UP"},
              {"alert_ts": 15 * H, "direction": "DOWN"}, {"alert_ts": 30 * H, "direction": "UP"}]
    m = ev.match(alerts, [ep], (0, 100 * H))
    kinds = [r["kind"] for r in m["rows"]]
    assert kinds == ["HIT", "DUPLICATE", "FALSE_ALARM", "FALSE_ALARM"]
    assert m["rows"][3]["late"] is True and m["rows"][2]["late"] is False
    assert m["direction_inside"] == (2, 3)


def test_small_sample_cells_report_counts_only():
    ep = {"episode_id": "e1", "direction": "UP", "onset_ts": 0, "first_crossing_ts": H, "first_detection_ts": H,
          "window_end_ts": 5 * H, "resolution_ts": 5 * H, "magnitude": 0.04}
    m = ev.match([{"alert_ts": 2 * H, "direction": "UP"}], [ep], (0, 10 * H))
    out = ev.summarise(m["ref"], m["rows"], m["hit_by"], 1.0)
    assert out["status"] == "INSUFFICIENT_SAMPLE" and "recall" not in out and out["hits"] == 1


def test_b0h_uses_the_unchanged_production_function_on_three_hourly_readings():
    # flat for 5 days, then a +5% step: production LARGE_MOVE (4%, first reading of each UTC day) must fire
    start = (T0 // D) * D
    closes = [100] * (5 * 24) + [105] * (3 * 24)
    s = series(closes, start=start - H)
    alerts = ev.b0h_alerts(s, (start, start + 8 * D))
    assert alerts and alerts[0]["direction"] == "UP"
    assert alerts[0]["alert_ts"] % (6 * H) == 0 and alerts[0]["alert_ts"] > alerts[0]["event_ts"]
    assert len({a["event_ts"] for a in alerts}) == len(alerts)  # one alert per fingerprint
