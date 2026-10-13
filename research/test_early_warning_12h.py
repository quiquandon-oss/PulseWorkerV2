"""Tests for research/early_warning_12h.py (EXP-23). Synthetic data only; no network, no repository data."""
import gzip
import json
import math
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import early_warning_12h as ew  # noqa: E402

H = ew.H


def candle(c):
    c = Decimal(str(c))
    return (c, c * Decimal("1.001"), c * Decimal("0.999"), c)


def flat_prices(start, end, base=100.0, wiggle=0.001):
    """Hourly closes on [start, end] (available_at grid), small deterministic oscillation, |R24| far below 2%."""
    out, t, i = {}, start, 0
    while t <= end:
        out[t] = candle(round(base * (1 + wiggle * math.sin(i / 3.0)), 6))
        t += H
        i += 1
    return out


def oi_series(start, end, avail_lag=5 * 60_000):
    return {ts: (ts + avail_lag, 90_000.0 + (ts // 300_000) % 50, 8e9) for ts in range(start, end + 1, 300_000)}


def funding_series(start, end):
    return {ts: (ts, 1e-4 * (1 + (ts // (8 * H)) % 3)) for ts in range(start, end + 1, 8 * H)}


# ------------------------------------------------------------------------------------------- sealed boundary
def test_label_builder_rejects_sealed_rows():
    with pytest.raises(ew.SealedDataError):
        ew.build_labels({ew.SEAL_END: Decimal("100")})
    with pytest.raises(ew.SealedDataError):
        ew.build_labels({ew.DEV_END: Decimal("100"), ew.SEAL_END + H: Decimal("100")})
    assert ew.build_labels({ew.SEAL_END + H: Decimal("100")})["episodes"] == []


def test_boundary_constants():
    assert ew.SEAL_END == 1791579600000          # 2026-10-09T21:00:00Z, sealed inclusive
    assert ew.T1 == 1791583200000                # 2026-10-09T22:00:00Z
    assert ew.LEAD_MIN == 12 * H and ew.LEAD_MAX == 36 * H


def test_sealed_move_never_becomes_a_label_and_first_labelable_hour():
    # A +10% move entirely inside the sealed period, then flat post-seal prices.
    prices = flat_prices(ew.SEAL_END - 900 * H, ew.SEAL_END + 120 * H)
    for k, t in enumerate(range(ew.SEAL_END - 30 * H, ew.SEAL_END + H, H)):
        prices[t] = candle(110.0)
    rep = ew.run(prices, {}, {})
    assert rep["labels"]["first_labelable"] == ew.SEAL_END + 26 * H
    assert rep["labels"]["episodes"] == 0 and rep["labels"]["burn_in_crossings"] == 0


def test_decision_hours_start_at_T1_and_use_sealed_lookback():
    prices = flat_prices(ew.SEAL_END - 800 * H, ew.SEAL_END + 5 * H)
    fb = ew.FeatureBuilder(prices)
    row = fb.build(ew.T1)
    assert row["P"] is not None and row["P"]["sig720"] is not None      # 720 h lookback reaches into the seal
    rep = ew.run(prices, {}, {})
    assert rep["decision_hours"]["eligible"] == 5                        # T1 .. SEAL_END + 5 h, never SEAL_END
    assert rep["decision_hours"]["coverage"]["F1"]["first_evaluable"] == ew.T1


# ----------------------------------------------------------------------------------------------- look-ahead
def test_features_do_not_change_when_future_data_changes():
    start, t = ew.SEAL_END - 800 * H, ew.T1 + 10 * H
    prices = flat_prices(start, t + 50 * H)
    oi, fund = oi_series(start, t + 50 * H), funding_series(start - 40 * 24 * H, t + 50 * H)
    base = ew.FeatureBuilder(prices, oi, fund).build(t)
    p2 = dict(prices)
    o2 = dict(oi)
    f2 = dict(fund)
    for u in [u for u in p2 if u > t]:
        p2[u] = candle(999.0)
    for u in [u for u in o2 if o2[u][0] > t]:
        o2[u] = (o2[u][0], 1.0, 1.0)
    for u in [u for u in f2 if f2[u][0] > t]:
        f2[u] = (f2[u][0], 9.9)
    assert ew.FeatureBuilder(p2, o2, f2).build(t) == base
    assert base["max_available_at"] <= t and base["O"] is not None and base["U"] is not None


def test_oi_snapshot_not_yet_available_is_ignored():
    start, t = ew.SEAL_END - 800 * H, ew.T1
    prices = flat_prices(start, t)
    oi = oi_series(start, t - 20 * 60_000)                       # last available snapshot 15 min before t
    oi[t - 5 * 60_000] = (t + 1, 1.0, 1.0)                        # timestamped before t but published after t
    row = ew.FeatureBuilder(prices, oi).build(t)
    assert row["O"] is not None and row["O"]["oi_btc"] != 1.0 and row["max_available_at"] <= t


# ------------------------------------------------------------------------------------------------ missing data
def test_price_gap_makes_features_not_evaluable_without_filling():
    start, t = ew.SEAL_END - 800 * H, ew.T1 + 30 * H
    prices = flat_prices(start, t)
    for u in (t - 5 * H, t - 4 * H, t - 3 * H):                 # 4 h gap inside the 24 h window
        del prices[u]
    assert ew.FeatureBuilder(prices).build(t)["P"] is None
    assert t - 4 * H not in prices                               # nothing synthesised


def test_stale_oi_and_funding_are_not_evaluable():
    start, t = ew.SEAL_END - 800 * H, ew.T1
    prices = flat_prices(start, t)
    oi = oi_series(start, t - 30 * 60_000)                        # newest snapshot 25 min old > 15 min
    fund = funding_series(start - 40 * 24 * H, t - 9 * H)         # newest settlement older than 8h15m
    row = ew.FeatureBuilder(prices, oi, fund).build(t)
    assert row["P"] is not None and row["O"] is None and row["U"] is None
    assert ew.vector(row, ("P",)) is not None and ew.vector(row, ("P", "O")) is None


def test_conflicting_price_sources_drop_the_hour():
    a = {ew.T1: candle(100.0), ew.T1 + H: candle(101.0)}
    b = {ew.T1: candle(100.5), ew.T1 + H: candle(101.0)}
    merged, conflicts = ew.merge_prices(a, b)
    assert conflicts == [ew.T1] and ew.T1 not in merged and ew.T1 + H in merged


# ------------------------------------------------------------------------------------------- onset & episodes
def ramp_prices(cross_up=True, total_move=0.05, ramp_hours=10, calm_hours=60):
    """Post-seal: calm for calm_hours (completes burn-in), then a ramp, then flat at the new level."""
    start = ew.SEAL_END + H
    prices = flat_prices(start, start + calm_hours * H, wiggle=0.0005)
    t = start + calm_hours * H
    level = 100.0
    for i in range(1, ramp_hours + 1):
        level = 100.0 * (1 + (total_move if cross_up else -total_move) * i / ramp_hours)
        prices[t + i * H] = candle(round(level, 6))
    end = t + ramp_hours * H
    for i in range(1, 80):
        prices[end + i * H] = candle(round(level, 6))
    return prices, t


def test_onset_first_crossing_and_burn_in():
    prices, ramp_start = ramp_prices()
    lab = ew.build_labels({t: v[3] for t, v in prices.items()})
    assert lab["burn_in_complete"] and lab["burn_in_end"] < ramp_start
    assert len(lab["episodes"]) == 1
    e = lab["episodes"][0]
    assert e["direction"] == "UP"
    assert e["T_x"] == ramp_start + 7 * H                         # +3.5% at ramp hour 7 is the first > 3% close
    window = [t for t in prices if e["T_x"] - 24 * H <= t <= e["T_x"]]
    low = min(prices[t][3] for t in window)
    assert e["T_on"] == max(t for t in window if prices[t][3] == low)   # lowest close, ties -> latest
    assert e["T_on"] <= ramp_start


def test_down_move_and_strict_threshold():
    prices, ramp_start = ramp_prices(cross_up=False)
    lab = ew.build_labels({t: v[3] for t, v in prices.items()})
    assert [e["direction"] for e in lab["episodes"]] == ["DOWN"]
    # exactly +3% over 24 h is NOT a crossing
    start = ew.SEAL_END + H
    p = {t: Decimal("100") for t in range(start, start + 60 * H, H)}
    t_end = start + 60 * H
    p[t_end] = Decimal("103")
    assert ew.r24_valid(p, sorted(p), t_end) == Decimal("0.03")
    assert ew.build_labels(p)["episodes"] == []


def test_crossing_during_burn_in_is_not_an_event():
    start = ew.SEAL_END + H
    p = {t: Decimal("100") for t in range(start, start + 27 * H, H)}
    for i, t in enumerate(range(start + 27 * H, start + 40 * H, H)):
        p[t] = Decimal("105")                                     # crossing at the first labelable hours
    lab = ew.build_labels(p)
    assert lab["episodes"] == [] and len(lab["burn_in_crossings"]) >= 1


# ---------------------------------------------------------------------------------------------- warning window
def _episode(T_on, d="UP"):
    return {"episode_id": f"e{T_on}{d}", "direction": d, "T_on": T_on, "T_x": T_on + 10 * H, "resolution": T_on + 40 * H}


def test_warning_window_boundaries():
    T_on = ew.T1 + 100 * H
    hours = list(range(ew.T1, ew.T1 + 300 * H, H))
    elig, evalb = set(hours), {t: True for t in hours}
    ep = [_episode(T_on)]
    end = hours[-1]
    for t0, kind in [(T_on - 12 * H, "hits"), (T_on - 36 * H, "hits"), (T_on - 11 * H, "inside_move"),
                     (T_on - 37 * H, "false_warnings")]:
        r = ew.score_runs([{"t0": t0, "end": t0}], ep, "UP", "T_on", end, elig, evalb)
        assert r[kind] == 1, (t0 - T_on) / H
    # direction mismatch is a false warning; WARN_ANY qualifies
    assert ew.score_runs([{"t0": T_on - 20 * H, "end": 0}], ep, "DOWN", "T_on", end, elig, evalb)["false_warnings"] == 1
    assert ew.score_runs([{"t0": T_on - 20 * H, "end": 0}], ep, "ANY", "T_on", end, elig, evalb)["hits"] == 1
    # second qualifying run is a duplicate; a run too close to the period end is censored
    r = ew.score_runs([{"t0": T_on - 30 * H, "end": 0}, {"t0": T_on - 20 * H, "end": 0}], ep, "UP", "T_on", end, elig, evalb)
    assert r["hits"] == 1 and r["duplicates"] == 1 and r["misses"] == 0
    assert ew.score_runs([{"t0": end - 10 * H, "end": 0}], ep, "UP", "T_on", end, elig, evalb)["censored_runs"] == 1


def test_episode_with_window_before_T1_is_not_evaluable():
    hours = list(range(ew.T1, ew.T1 + 100 * H, H))
    e = _episode(ew.T1 + 20 * H)                                  # window starts 16 h before T1
    assert not ew.episode_evaluable(e, "T_on", set(hours), {t: True for t in hours})


def test_runs_consolidate_consecutive_warnings_and_break_on_not_evaluable():
    hours = [ew.T1 + i * H for i in range(6)]
    warn = dict(zip(hours, [True, True, None, True, False, True]))
    runs = ew.runs_from(warn, hours)
    assert [r["t0"] for r in runs] == [hours[0], hours[3], hours[5]]


# --------------------------------------------------------------------------------- walk-forward causality, stats
def test_walk_forward_trains_only_on_final_labels(monkeypatch):
    hours = [ew.T1 + i * H for i in range(600)]
    feats = {t: {"P": {"a": float((t // H) % 7), "b": float((t // H) % 5)}, "O": None, "U": None} for t in hours}
    eps = [_episode(ew.T1 + k * 50 * H, "UP") for k in range(1, 12)]
    seen = []
    real = ew.fit_logistic

    def spy(X, y, **kw):
        seen.append(len(X))
        return real(X, y, **kw)
    monkeypatch.setattr(ew, "fit_logistic", spy)
    calls = []
    out = ew.walk_forward_warnings(hours, feats, eps, ("P",), "UP", (0.05,), lambda h: calls.append(h) or True)
    assert set(out[0.05]) == set(hours)
    assert seen, "model was never fitted"
    # every training hour h used at fit time t satisfied h + LABEL_FINAL_LAG <= t: the first fit needs 200 such hours
    first_fit_t = hours[0] + (ew.MIN_TRAIN_HOURS + ew.LABEL_FINAL_LAG // H) * H
    assert all(v is False for t, v in out[0.05].items() if t < first_fit_t)


def test_stats_helpers():
    assert ew.mcnemar_exact(0, 0) == 1.0
    assert ew.mcnemar_exact(10, 0) == pytest.approx(2 / 1024)
    lo, hi = ew.wilson(5, 10)
    assert lo < 0.5 < hi


def test_checkpoint_gating():
    eps = [{"direction": "UP"}] * 35 + [{"direction": "DOWN"}] * 24
    assert not ew.checkpoint(eps)["met"]
    assert ew.checkpoint(eps + [{"direction": "DOWN"}])["met"]


# --------------------------------------------------------------------------------------------- reproducibility
def _write(tmp_path):
    mm = tmp_path / "BTC.jsonl"
    with open(mm, "w") as f:
        for t, v in sorted(flat_prices(ew.SEAL_END - 800 * H, ew.SEAL_END).items()):
            f.write(json.dumps({"open_ts": t - H, "close_ts": t - 1, "available_at": t,
                                "o": str(v[0]), "h": str(v[1]), "l": str(v[2]), "c": str(v[3])}) + "\n")
    store = tmp_path / "store" / "hyperliquid_hip3" / "2026"
    store.mkdir(parents=True)
    with gzip.open(store / "2026-10-09.jsonl.gz", "wt") as f:
        series = flat_prices(ew.SEAL_END - 800 * H, ew.SEAL_END + 3 * H)
        for t, v in sorted((t, v) for t, v in series.items() if t >= ew.SEAL_END - 2 * H):
            for metric in ("close", "volume"):
                f.write(json.dumps({"instrument": "BTC", "metric": metric, "timestamp": t - H, "available_at": t,
                                    "value": float(v[3]), "raw": {"t": t - H, "T": t - 1, "o": str(v[0]),
                                    "h": str(v[1]), "l": str(v[2]), "c": str(v[3])}}) + "\n")
    oi = tmp_path / "oi.jsonl.gz"
    with gzip.open(oi, "wt") as f:
        for ts, (av, b, u) in sorted(oi_series(ew.SEAL_END - 800 * H, ew.SEAL_END + 3 * H).items()):
            for m, v in (("open_interest", b), ("open_interest_usd", u)):
                f.write(json.dumps({"source": "binance_oi_archive", "metric": m, "timestamp": ts,
                                    "available_at": av, "value": v}) + "\n")
        for ts, (av, v) in sorted(funding_series(ew.SEAL_END - 800 * H, ew.SEAL_END - 30 * H).items()):
            f.write(json.dumps({"source": "binance_funding_archive", "metric": "funding_rate", "timestamp": ts,
                                "available_at": av, "value": v}) + "\n")
    return mm, tmp_path / "store", oi


def test_cli_end_to_end_is_byte_reproducible(tmp_path):
    mm, store, oi = _write(tmp_path)
    outs = []
    for i in range(2):
        out = tmp_path / f"r{i}.json"
        ew.main(["--prices", str(mm), "--forward-store", str(store), "--oi-funding", str(oi), "--out", str(out)])
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]
    rep = json.loads(outs[0])
    assert rep["status"] == "INSUFFICIENT_SAMPLE" and rep["metrics"] is None
    cov = rep["decision_hours"]["coverage"]
    assert cov["F1"]["evaluable_hours"] == 3 and cov["F2"]["evaluable_hours"] == 3
    assert cov["F3"]["evaluable_hours"] == 0 and cov["F4"]["evaluable_hours"] == 0       # funding stale: blocked
    assert any("funding" in b for b in rep["blockers"])
    assert rep["data"]["conflicts_dropped"] == []                                          # overlapping hours agree


def test_end_to_end_synthetic_moves_counts_only_below_checkpoint():
    start = ew.SEAL_END - 800 * H
    prices = flat_prices(start, ew.SEAL_END + 60 * H, wiggle=0.0005)
    t, level, sign = ew.SEAL_END + 60 * H, 100.0, 1
    for k in range(6):                                            # six alternating 5% moves, each followed by calm
        for i in range(1, 11):
            prices[t + i * H] = candle(round(level * (1 + sign * 0.05 * i / 10), 6))
        level *= 1 + sign * 0.05
        t += 10 * H
        for i in range(1, 60):
            prices[t + i * H] = candle(round(level * (1 + 0.0005 * math.sin(i)), 6))
        t += 59 * H
        sign = -sign
    oi = oi_series(start, t)
    rep = ew.run(prices, oi, {})
    assert rep["labels"]["burn_in_complete"] and rep["labels"]["episodes"] == 6
    assert rep["labels"]["episodes_up"] == 3 and rep["labels"]["episodes_down"] == 3
    assert rep["status"] == "INSUFFICIENT_SAMPLE" and rep["metrics"] is None
    comps = rep["results_counts"]["comparisons"]
    assert comps["F2_vs_F1"]["scored_hours"] > 0 and comps["F3_vs_F1"]["scored_hours"] == 0
    assert comps["F2_vs_F1"]["incremental_vs_F1"]["status"] == "NOT_COMPUTED"
    for wtype in ew.WARN_TYPES:
        cell = comps["F2_vs_F1"]["F2"][wtype]["0.05"]
        assert cell["statistics"]["status"] == "NOT_COMPUTED"                      # no intervals below checkpoint
        assert "NOT a predictive model" in cell["F0_chance_reference"]["type"]
    assert "p_one_sided" not in json.dumps(rep) and "wilson95" not in json.dumps(rep)
    assert "hit_lead_hours" not in json.dumps(rep)                # no lead-time distributions below the checkpoint
    assert any(b.startswith("F3:") for b in rep["blockers"])
    assert ew.run(prices, oi, {}) == rep                           # deterministic


def test_stdlib_and_numpy_logistic_agree():
    X = [[float(i % 7), float((i * 3) % 5)] for i in range(300)]
    y = [1 if (i % 7) > 4 else 0 for i in range(300)]
    a = ew._fit_stdlib(X, y, 1.0, 25)
    if ew._np is None:
        pytest.skip("numpy not installed")
    b = ew._fit_numpy(X, y, 1.0, 25)
    assert all(abs(u - v) < 1e-6 for u, v in zip(a["w"], b["w"]))


# ---------------------------------------------------------------------------------- F0, intervals, units, gating
def test_f0_shift_preserves_warning_count_and_not_evaluable_hours():
    hours = [ew.T1 + i * H for i in range(10)]
    warn = dict(zip(hours, [True, True, False, None, False, True, False, False, None, False]))
    for s in ew.f0_shifts(8):
        sh = ew.shifted_warnings(warn, hours, s)
        assert [t for t in hours if sh[t] is None] == [hours[3], hours[8]]           # NOT_EVALUABLE stays put
        assert sum(1 for v in sh.values() if v) == 3                                   # same warning-hour count
    assert ew.f0_shifts(8) == ew.f0_shifts(8) and len(ew.f0_shifts(8)) == 7            # deterministic, distinct
    assert ew.f0_shifts(1) == []


def test_poisson_exact_interval_known_values():
    lo, hi = ew.poisson_ci(0)
    assert lo == 0.0 and hi == pytest.approx(3.6889, abs=1e-3)
    lo, hi = ew.poisson_ci(10)
    assert lo == pytest.approx(4.7954, abs=1e-3) and hi == pytest.approx(18.3904, abs=1e-3)


def test_day_block_bootstrap_uses_days_as_units_and_is_deterministic():
    day = 24 * H
    base = (ew.T1 // day + 1) * day
    scored = [base + i * H for i in range(10 * 24)]                                  # 10 UTC days
    a = [base + 2 * H, base + 3 * H, base + 5 * day]                                 # 2 in day 0, 1 in day 5
    b = [base + 7 * day]
    r1 = ew.day_block_bootstrap_diff(a, b, scored, seed=1)
    assert r1 == ew.day_block_bootstrap_diff(a, b, scored, seed=1)
    assert r1["unit"] == "UTC day" and r1["n_units"] == 10
    assert r1["point"] == pytest.approx((3 - 1) / 10 * 30)
    # moving warnings within the same day cannot change the result (day is the resampling unit)
    a2 = [base + 20 * H, base + 21 * H, base + 5 * day + 7 * H]
    assert ew.day_block_bootstrap_diff(a2, b, scored, seed=1) == r1
    assert r1["ci95"][0] <= r1["point"] <= r1["ci95"][1]
    assert ew.day_block_bootstrap_diff([], [], [], seed=1) is None


def test_holm_adjustment():
    assert ew.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def test_cell_statistics_gating_and_values():
    prim = {"hits": 30, "false_warnings": 10, "inside_move": 5, "evaluable_episodes": 60}
    scored = [ew.T1 + i * H for i in range(30 * 24)]
    assert ew.cell_statistics(prim, [1, 2, 3], scored, met=False)["status"] == "NOT_COMPUTED"
    small = dict(prim, evaluable_episodes=19)
    assert "19 < 20" in ew.cell_statistics(small, [1, 2], scored, met=True)["reason"]
    st = ew.cell_statistics(prim, [5] * 999 + [40], scored, met=True)
    assert st["status"] == "COMPUTED" and st["recall"]["value"] == 0.5
    assert st["precision"]["wilson95"] == ew.wilson(30, 45)
    days = len({t // (24 * H) for t in scored})
    assert st["false_warnings_per_30d"]["value"] == pytest.approx(10 / days * 30)
    assert st["vs_F0"]["p_one_sided"] == pytest.approx(2 / 1001)


def test_missing_funding_blocks_only_f3_f4(tmp_path):
    start = ew.SEAL_END - 800 * H
    prices = flat_prices(start, ew.SEAL_END + 40 * H)
    rep = ew.run(prices, oi_series(start, ew.SEAL_END + 40 * H), funding_series(start, ew.SEAL_END - 20 * H))
    c = rep["results_counts"]["comparisons"]
    assert c["F2_vs_F1"]["scored_hours"] == 40 and rep["decision_hours"]["coverage"]["F1"]["evaluable_hours"] == 40
    assert c["F3_vs_F1"]["scored_hours"] == 0 and c["F4_vs_F1"]["scored_hours"] == 0
    assert any(b.startswith("funding:") for b in rep["blockers"])
    assert rep["status"] == "INSUFFICIENT_SAMPLE"
