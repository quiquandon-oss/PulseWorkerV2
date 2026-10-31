"""Structural check of EXP-009 / EXP-010 batches (owner decision H1). All rows are synthetic test data."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import structural_check_exp009_010 as sc  # noqa: E402
import event_source_evidence_join as join  # noqa: E402
import source_dialogue as sd  # noqa: E402

D = 24 * 3600000
T = 1793491200000                      # 2026-11-01


def r9(eid, sk, ts, lab="INSUFFICIENT_EVIDENCE", **extra):
    return {"event_id": eid, "event_ts": ts, "source_key": sk, "event_source_interpretation": lab,
            "is_internal_model_event": False, "post_event_return": 0.0123, "residual_pct": -0.5, **extra}


def batch9(a, results=None, start=None, **over):
    start = a - 90 * D if start is None else start
    results = [r9("e1", "fng", a - D), r9("e1", "news", a - D, "EVENT_SOURCE_ALIGNED")] if results is None else results
    rep = {"window": {"start_ts": start, "end_ts": a}, "events": [{"event_id": "e1"}], "results": results,
           "source_keys": ["fng", "news"], "evidence_coverage": {}, "by_source_interpretation": {}, "btc_outcome_coverage": {}}
    row = {"analysis_ts": a, "window_start_ts": start, "window_end_ts": a, "sample_size": len(results),
           "validation_status": "OK", "metric_json": json.dumps(rep)}
    row.update(over)
    return row


def batch10(a, n=3, **over):
    rep = {"window": {"start_ts": a - 90 * D, "end_ts": a}, "source_keys": ["a", "b"],
           "relationship_summary": {"n_source_pair_observations": n, "by_relationship": {"INSUFFICIENT_EVIDENCE": n, "DIFFERENT_TIMING": 0, "SUPPORTING": 0, "CONTRADICTING": 0}},
           "redundancy_summary": {"n_source_pairs": 1, "by_redundancy": {"REDUNDANCY_UNRESOLVED": 0, "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED": 1, "INSUFFICIENT_EVIDENCE": 0}}}
    row = {"analysis_ts": a, "window_start_ts": a - 90 * D, "window_end_ts": a, "sample_size": n,
           "validation_status": "OK", "metric_json": json.dumps(rep)}
    row.update(over)
    return row


def test_declared_label_sets_are_the_modules_own():
    assert sc.EXP009_INTERPRETATIONS == join.EVENT_SOURCE_INTERPRETATIONS
    assert sc.EXP010_RELATIONSHIP_LABELS == sd.RELATIONSHIP_LABELS
    assert sc.EXP010_REDUNDANCY_LABELS == sd.REDUNDANCY_LABELS


def test_four_weekly_valid_batches_meet_the_structural_milestone_and_daily_reruns_do_not_count():
    rows = [batch9(T + i * 7 * D) for i in range(4)] + [batch9(T + 7 * D + D)]
    rep = sc.check_subject("EXP-009", rows)
    assert rep["n_valid"] == 5 and rep["n_independent_valid"] == 4 and rep["structural_milestone"] == "MET"
    assert rep["predictive_conclusion"] is None
    assert sc.check_subject("EXP-009", rows[:3])["structural_milestone"] == "NOT_MET"


def test_exp009_invariants_each_fail_alone():
    a = T
    dup = batch9(a, [r9("e1", "fng", a - D), r9("e1", "fng", a - D)])
    assert sc.check_exp009_batch(dup) == ["no_duplicate_event_source_rows"]
    assert sc.check_exp009_batch(batch9(a, [r9("e1", "fng", a + D)])) == ["event_in_window"]
    assert sc.check_exp009_batch(batch9(a, [r9("e9", "fng", a - D)])) == ["event_listed"]
    assert sc.check_exp009_batch(batch9(a, [r9("e1", "zzz", a - D)])) == ["source_listed"]
    assert sc.check_exp009_batch(batch9(a, [r9("e1", "fng", a - D, "MADE_UP")])) == ["label_in_declared_set"]
    assert sc.check_exp009_batch(batch9(a, [r9("e1", "fng", a - D, is_internal_model_event=True)])) == ["no_internal_model_rows"]
    assert sc.check_exp009_batch(batch9(a, sample_size=7)) == ["sample_size_equals_len_results"]
    assert "window_end_equals_analysis_ts" in sc.check_exp009_batch(batch9(a, window_end_ts=a - 1))
    assert "window_at_most_90_days" in sc.check_exp009_batch(batch9(a, start=a - 120 * D))
    assert sc.check_exp009_batch(batch9(a, metric_json="{")) == ["metric_json_parses"]


def test_exp010_invariants():
    assert sc.check_exp010_batch(batch10(T)) == []
    assert sc.check_exp010_batch(batch10(T, sample_size=9)) == ["sample_size_equals_n_source_pair_observations"]
    bad = batch10(T)
    rep = json.loads(bad["metric_json"]); rep["relationship_summary"]["by_relationship"]["NEW"] = 0
    bad["metric_json"] = json.dumps(rep)
    assert sc.check_exp010_batch(bad) == ["relationship_labels_equal_declared_set"]
    rep = json.loads(batch10(T)["metric_json"]); rep["redundancy_summary"]["n_source_pairs"] = 5
    assert sc.check_exp010_batch(dict(batch10(T), metric_json=json.dumps(rep))) == ["redundancy_counts_sum_to_n"]


def test_output_carries_no_label_counts_and_no_outcome_values():
    rows = [batch9(T - 30 * D + i * 7 * D) for i in range(4)]          # windows overlap the seal
    out = json.dumps(sc.check_subject("EXP-009", rows))
    for label in sc.EXP009_INTERPRETATIONS:
        assert label not in out
    assert "0.0123" not in out and "-0.5" not in out and "post_event_return" not in out
    assert "NOT_CHECKED_ON_SEAL_OVERLAPPING_BATCHES" in out
    out10 = json.dumps(sc.check_subject("EXP-010", [batch10(T)]))
    for label in sc.EXP010_RELATIONSHIP_LABELS + sc.EXP010_REDUNDANCY_LABELS:
        assert label not in out10


def test_the_checker_never_reads_numeric_outcome_fields():
    src = Path(sc.__file__).read_text()
    for field in ("post_event_return", "pre_event_return", "residual_pct", "horizons", "control_classification", "by_source_interpretation\"]", "btc_outcome_coverage\"]"):
        assert field not in src.split('"""', 2)[2]                          # outside the module docstring


def test_seal_constants():
    from datetime import datetime, timezone
    assert sc.SEAL_START_TS == int(datetime(2026, 7, 22, tzinfo=timezone.utc).timestamp() * 1000)
    assert sc.SEAL_END_TS == int(datetime(2026, 10, 9, 21, tzinfo=timezone.utc).timestamp() * 1000)
