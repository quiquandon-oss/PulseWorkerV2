"""Tests for research/prediction_eval.py (protocol PEV-1). Synthetic data only."""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prediction_eval as pe  # noqa: E402

H = pe.H
T0 = 1774310400000  # 2026-03-24T00:00Z, pre-seal, multiple of 24 h


def walk(n, start=100.0, seed=3, t0=T0, step=pe.STEP_MS):
    s, p, out = seed, start, {}
    for i in range(n):
        s = (s * 16807) % 2147483647
        p *= 1 + (s / 2147483647 - 0.5) * 0.03
        out[t0 + i * step] = round(p, 4)
    return out


# ---------------------------------------------------------------- feature parity and seal
def test_technical_score_matches_worker_js():
    # Expected values produced by running worker.js's computeSimpleTechnicalScore under node on the same inputs.
    cases = [([1, 2, 3, 4, 5, 6], 100), ([6, 5, 4, 3, 2, 1], 0), ([10, 11, 10.5, 10.7, 10.2, 10.9, 11.3, 11.1], 66),
             ([5] * 7, 50), ([100, 101, 99, 102, 98, 103, 97, 104, 96, 105], 56), ([1, 2, 3], 50),
             ([3, 1, 2, 1, 2, 1, 2, 1, 2.5, 1], 41)]
    for prices, want in cases:
        assert pe.simple_technical_score(prices) == want


def test_seal_rules_are_boundary_exact():
    assert pe.price_usable(pe.SEAL_START_TS)            # available exactly at seal start: pre-seal close
    assert not pe.price_usable(pe.SEAL_START_TS + 1)
    assert not pe.price_usable(pe.SEAL_END_TS)          # the candle closing at seal end is sealed
    assert pe.price_usable(pe.SEAL_END_TS + 1)
    assert pe.reading_usable(pe.SEAL_START_TS - 1) and not pe.reading_usable(pe.SEAL_START_TS)
    assert not pe.reading_usable(pe.SEAL_END_TS) and pe.reading_usable(pe.SEAL_END_TS + 1)


def test_load_candles_drops_sealed_closes(tmp_path):
    d = tmp_path / "BTC"
    d.mkdir()
    rows = [{"close_ts": pe.SEAL_START_TS - 1, "c": "1"}, {"close_ts": pe.SEAL_START_TS + H - 1, "c": "2"},
            {"close_ts": pe.SEAL_END_TS - 1, "c": "3"}, {"close_ts": pe.SEAL_END_TS + H - 1, "c": "4"}]
    (d / "observations.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    got = pe.load_candles([tmp_path], "BTC")
    assert got == {pe.SEAL_START_TS: 1.0, pe.SEAL_END_TS + H: 4.0}


def test_load_sentiment_drops_sealed_and_missing(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"series": [{"observation_ts": pe.SEAL_START_TS - 5, "score": 40},
                                        {"observation_ts": pe.SEAL_START_TS + 5, "score": 41},
                                        {"observation_ts": pe.SEAL_END_TS + 5, "score": None},
                                        {"observation_ts": pe.SEAL_END_TS + 9, "score": 60}]}))
    assert pe.load_sentiment(f) == [(pe.SEAL_START_TS - 5, 40.0), (pe.SEAL_END_TS + 9, 60.0)]


def test_build_rows_uses_only_past_closes_and_gaps_break_windows():
    closes = walk(80)
    rows = pe.build_rows(closes)
    r = rows[40]
    hist = [closes[T0 + i * pe.STEP_MS] for i in range(41)]
    assert r["technical_score"] == pe.simple_technical_score(hist[-30:])
    assert math.isclose(r["regime_mag"], pe.regime_mag(hist))
    assert math.isclose(r["labels"][24], closes[r["ts"] + 24 * H] / r["price"] - 1)
    gap = dict(closes)
    del gap[T0 + 50 * pe.STEP_MS]
    g = {x["ts"]: x for x in pe.build_rows(gap)}
    assert g[T0 + 51 * pe.STEP_MS]["technical_score"] is None          # window restarted after the gap
    assert g[T0 + 46 * pe.STEP_MS]["labels"][12] is None                # label lands on the gap: unresolved


def test_features_unchanged_when_future_closes_change():
    closes = walk(90)
    future = {t: (p * 3 if t > T0 + 60 * pe.STEP_MS else p) for t, p in closes.items()}
    a, b = pe.build_rows(closes), pe.build_rows(future)
    for x, y in zip(a[:61], b[:61]):
        assert (x["technical_score"], x["regime_mag"], x["prev_ret"]) == (y["technical_score"], y["regime_mag"], y["prev_ret"])


# ---------------------------------------------------------------- joins
def test_asof_never_returns_a_later_reading_and_respects_max_age():
    rd = [(10, 1.0), (20, 2.0), (30, 3.0)]
    ts = [x[0] for x in rd]
    assert pe.asof_reading(ts, rd, 25, max_age=100) == 2.0
    assert pe.asof_reading(ts, rd, 20, max_age=100) == 2.0
    assert pe.asof_reading(ts, rd, 9, max_age=100) is None
    assert pe.asof_reading(ts, rd, 40, max_age=10) == 3.0 and pe.asof_reading(ts, rd, 41, max_age=10) is None


def test_legacy_nearest_can_look_ahead_but_only_among_readings_existing_at_decision_time():
    rd = [(10, 1.0), (24, 2.0)]
    ts = [x[0] for x in rd]
    assert pe.nearest_reading(ts, rd, 20, avail_until=100, tol=10) == 2.0      # the later reading wins (leak)
    assert pe.nearest_reading(ts, rd, 20, avail_until=23, tol=10) == 1.0       # did not exist yet at decision time


# ---------------------------------------------------------------- models
def _setup(n=400):
    closes = walk(n)
    rows = pe.build_rows(closes)
    sent = [(r["ts"] + 30 * 60000, 30.0 + (i * 7) % 40) for i, r in enumerate(rows)]
    return closes, rows, sent


def test_no_model_changes_when_everything_after_the_unit_changes():
    closes, rows, sent = _setup()
    u_idx = 300
    u = rows[u_idx]
    altered_closes = {t: (p * 1.7 if t > u["ts"] else p) for t, p in closes.items()}
    rows2 = pe.build_rows(altered_closes)
    sent2 = [(t, (99.0 if t > u["ts"] else v)) for t, v in sent]
    st, st2 = [s[0] for s in sent], [s[0] for s in sent2]
    upto, upto2 = rows[:u_idx + 1], rows2[:u_idx + 1]
    for fn in (lambda up, s, sts: pe.model_a(up, up[-1], 24, sts, s),
               lambda up, s, sts: pe.model_corrected(up, up[-1], 24),
               lambda up, s, sts: pe.model_corrected(up, up[-1], 24, sts, s),
               lambda up, s, sts: pe.model_lr(up, up[-1], 24, 1.0),
               lambda up, s, sts: pe.climatology(up, up[-1], 24)):
        p1, p2 = fn(upto, sent, st), fn(upto2, sent2, st2)
        assert p1 is not None and p1 == p2


def _capture(monkeypatch):
    seen = {}
    real = pe.knn_p_up
    def spy(query, cands, keys, stats, k_override=None):
        seen.update(query=query, cands=cands, stats=stats)
        return real(query, cands, keys, stats, k_override)
    monkeypatch.setattr(pe, "knn_p_up", spy)
    return seen


def test_legacy_analogs_carry_later_readings_corrected_analogs_never_do(monkeypatch):
    _, rows, _ = _setup(300)
    # one reading 30 min AFTER each row, sentiment value = 1000 + row index, so each value names its source time
    rd = [(r["ts"] + 30 * 60000, 1000.0 + i) for i, r in enumerate(rows)]
    ts = [x[0] for x in rd]
    seen = _capture(monkeypatch)
    pe.model_a(rows, rows[-1], 24, ts, rd)
    legacy = [c["sentiment"] for c, _ in seen["cands"]]
    later = sum(1 for c, _ in seen["cands"] if c["sentiment"] >= 1000.0)
    assert later == len(legacy) > 0
    # legacy analog i carries reading i, taken 30 min after the analog (inside its outcome window)
    feat = [r for r in rows if r["technical_score"] is not None and r["regime_mag"] is not None]
    idx = {r["ts"]: i for i, r in enumerate(rows)}
    assert all(c["sentiment"] == 1000.0 + idx[r["ts"]] for (c, _), r in zip(seen["cands"], pe._candidates(feat, rows[-1]["ts"], 24)))
    pe.model_corrected(rows, rows[-1], 24, ts, rd)
    for (c, _), r in zip(seen["cands"], pe._candidates(feat, rows[-1]["ts"], 24)):
        assert c["sentiment"] == 1000.0 + idx[r["ts"]] - 1        # the previous row's reading: at or before the analog


def test_corrected_scaling_and_imputation_exclude_the_query_row(monkeypatch):
    _, rows, sent = _setup(200)
    ts = [x[0] for x in sent]
    seen = _capture(monkeypatch)
    pe.model_corrected(rows, rows[-1], 24, ts, sent)
    s1 = dict(seen["stats"])
    extreme = rows[:-1] + [dict(rows[-1], technical_score=1e6, regime_mag=-1e6)]
    pe.model_corrected(extreme, extreme[-1], 24, ts, sent)
    assert seen["stats"] == s1
    pe.model_a(extreme, extreme[-1], 24, ts, sent)
    assert seen["stats"]["technical_score"] != s1["technical_score"]   # legacy: the query row moves the scaling


def test_c_abstains_without_real_reading_and_knn_respects_min_resolved():
    _, rows, _ = _setup(60)
    p, n = pe.knn_p_up({"a": 0}, [({"a": 1}, True)] * 3, ("a",), {"a": (0, 1)})
    assert p is None and n == 3
    p, n = pe.knn_p_up({"a": 0}, [({"a": i}, i % 2 == 0) for i in range(20)], ("a",), {"a": (0, 1)})
    assert n == 6 and math.isclose(p, 3 / 6)          # K = min(15, max(5, 20 // 3)) = 6


def test_logistic_recovers_direction():
    xs = [[x / 10] for x in range(-50, 50)]
    ys = [1 if x[0] > 0 else 0 for x in xs]
    w = pe.fit_logistic(xs, ys, lam=1.0)
    assert w[1] > 1


# ---------------------------------------------------------------- metrics and decision rule
def test_metrics_known_values():
    ps, ys = [0.9, 0.2, 0.6, 0.4], [True, False, False, True]
    ba, ru, rd = pe.balanced_accuracy(ps, ys)
    assert (ba, ru, rd) == (0.5, 0.5, 0.5)
    assert math.isclose(pe.brier(ps, ys), (0.01 + 0.04 + 0.36 + 0.36) / 4)
    assert pe.auc([1, 2, 3, 4], [False, False, True, True]) == 1.0
    assert pe.auc([1, 1], [True, False]) == 0.5
    assert pe.auc([1, 2], [True, True]) is None
    cal = pe.calibration([0.1, 0.1, 0.9, 1.0], [False, True, True, True])
    assert cal["table"][0]["n"] == 2 and cal["table"][-1]["n"] == 2


def test_bootstrap_is_deterministic():
    data = [0.0, 1.0] * 20
    f = lambda ix: sum(data[i] for i in ix) / len(ix)
    assert pe.bootstrap_ci(40, f) == pe.bootstrap_ci(40, f)
    lo, hi = pe.bootstrap_ci(40, f)
    assert lo < 0.5 < hi


def test_decision_label_rules():
    small = {"n_units": 5, "models": {}, "paired": {}}
    assert pe.decision_label(small, "C").startswith("NOT EVALUABLE")
    block = {"n_units": 30, "models": {"always_up": {"balanced_accuracy": 0.5}, "persistence": {"balanced_accuracy": 0.55}},
             "paired": {"C-persistence": {"d_ba_ci95": [0.01, 0.2]}, "C-climatology": {"d_brier_ci95": [-0.05, -0.001]}}}
    assert pe.decision_label(block, "C") == "DEMONSTRATED"
    block["paired"]["C-climatology"]["d_brier_ci95"] = [-0.05, 0.01]
    assert pe.decision_label(block, "C") == "NOT DEMONSTRATED"


def test_score_block_uses_common_eligible_set():
    units = [{"ts": i, "labels": {24: (0.01 if i % 2 else -0.01)}} for i in range(6)]
    units[5]["labels"][24] = None
    preds = {"X": [0.6, 0.4, None, 0.7, 0.3, 0.5], "Y": [0.5] * 6}
    block, keep = pe.score_block(["X", "Y"], preds, units, 24)
    assert keep == [0, 1, 3, 4]
    assert block["excluded"] == {"unresolved": 1, "model_abstained": 1}


def test_unit_times_are_non_overlapping():
    rows = [{"ts": T0 + i * pe.STEP_MS} for i in range(16)]
    assert [r["ts"] for r in pe.unit_times(rows, 24, 0, 10 ** 15)] == [T0, T0 + 24 * H]
    assert len(pe.unit_times(rows, 12, 0, 10 ** 15)) == 4


def test_models_abstain_when_the_query_has_no_features():
    _, rows, sent = _setup(120)
    q = dict(rows[-1], technical_score=None)
    ts = [x[0] for x in sent]
    assert pe.model_a(rows[:-1] + [q], q, 24, ts, sent) is None
    assert pe.model_corrected(rows[:-1] + [q], q, 24, ts, sent) is None
    assert pe.model_lr(rows[:-1] + [q], q, 24, 1.0) is None
    assert pe.model_shrink(rows[:-1] + [q], q, 24, 10) is None
