"""Frozen-plan evaluator (research/prospective_eval.py): population start, checkpoints, exclusions, Holm, EXP-003 tie and
duplicate handling. All rows are synthetic test data."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prospective_eval as pe  # noqa: E402

S = pe.POPULATION_START
H = 3600000


def tfm(h, i, correct, actual="UP", start=S):
    return {"horizon_hours": h, "input_end_ts": start + i * 24 * H, "correct": correct, "actual_direction": actual}


def test_exact_two_sided_and_holm():
    assert pe.exact_two_sided(0, 0) == 1.0
    assert abs(pe.exact_two_sided(0, 10) - 2 / 1024) < 1e-12
    assert pe.exact_two_sided(5, 10) == 1.0
    assert pe.holm({"a": 0.001, "b": 0.02, "c": 0.04}) == {"a": True, "b": True, "c": True}
    assert pe.holm({"a": 0.001, "b": 0.03, "c": 0.04}) == {"a": True, "b": False, "c": False}   # b > 0.05/2 stops the procedure
    assert pe.holm({"a": 0.2, "b": 0.001}) == {"b": True, "a": False}


def test_exp004_ignores_rows_before_population_start_and_reports_progress_only():
    rows = [tfm(12, i, 1, start=S - 30 * 24 * H) for i in range(20)] + [tfm(12, i, 1) for i in range(40)]
    rep = pe.exp004(rows, {}, {}, checkpoint=100)
    assert rep["h12"]["n_resolved"] == 40 and rep["h12"]["status"] == "COLLECTING (40/100)"
    assert "vs_always_up" not in rep["h12"]                     # no test below the checkpoint


def test_exp004_at_checkpoint_counts_exclusions_and_applies_holm():
    rows = [tfm(12, i, 1, actual="UP" if i % 2 else "DOWN") for i in range(10)] + [tfm(12, 10, None)]
    pers = {(12, S + i * 24 * H): "UP" for i in range(8)}          # two origins lack an EXP-008 persistence call
    knn = {12: [(S + i * 24 * H + 60000, 0.7) for i in range(10)]}
    rep = pe.exp004(rows, pers, knn, checkpoint=10)
    d = rep["h12"]
    assert d["status"] == "CHECKPOINT REACHED" and d["excluded"] == {"unresolved": 1, "no_persistence": 2}
    assert d["hits"] == {"timesfm": 10, "always_up": 5}
    assert d["vs_always_up"] == {"a_only": 5, "b_only": 0, "p": pe.exact_two_sided(5, 5)}
    assert d["vs_persistence"]["n"] == 8 and d["descriptive_vs_knn"]["n"] == 10
    assert d["first_window_meets_criterion"] is False          # p = 0.0625 for 5/5: not significant
    assert "replication" in d["note"]


def mrow(i, h, momentum, chosen, variant="original", pred=None, start=S):
    return {"ts": str(start + i * H), "horizon_hours": str(h), "prediction_ts": str(pred if pred is not None else start + i * H),
            "production_chosen_variant": variant,
            "scores_json": json.dumps([{"variant": "challenger_momentum", "lca": momentum}, {"variant": variant, "lca": chosen}])}


def test_exp003_uses_same_row_scores_drops_duplicates_ties_and_early_rows():
    rows = [mrow(0, 12, 0.7, 0.5), mrow(1, 12, 0.7, 0.5, pred=S),        # duplicate prediction_ts
            mrow(2, 12, 0.5, 0.5),                                      # tie
            mrow(3, 12, 0.4, 0.6),
            mrow(4, 12, 0.9, 0.1, start=S - 100 * H)]                   # before population start
    rep = pe.exp003(rows, checkpoint=60)
    assert rep["duplicates_dropped"] == 1
    assert rep["h12"] == {"non_tied": 2, "above": 1, "below": 1, "excluded": {"ties": 1, "chosen_variant_unscored": 0},
                          "checkpoint": 60, "status": "COLLECTING (2/60)"}


def test_exp003_chosen_variant_missing_from_scores_is_excluded():
    r = mrow(0, 24, 0.7, 0.5)
    r["production_chosen_variant"] = "challenger_flat"
    rep = pe.exp003([r], checkpoint=60)
    assert rep["h24"]["excluded"]["chosen_variant_unscored"] == 1 and rep["h24"]["non_tied"] == 0


def test_exp003_checkpoint_uses_the_first_window_only():
    rows = [mrow(i, 12, 0.9, 0.1) for i in range(10)] + [mrow(10 + i, 12, 0.1, 0.9) for i in range(10)]
    d = pe.exp003(rows, checkpoint=10)["h12"]
    assert d["status"] == "CHECKPOINT REACHED" and d["first_window_above"] == 10
    assert d["first_window_meets_criterion"] is True and d["p"] == pe.exact_two_sided(10, 10)
