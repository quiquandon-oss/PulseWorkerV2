"""
Tests for research/evidence_temporal.py (PR5c Section 4 / Section 16).

Pure-function tests -- no database, no network.
Run with: python3 -m pytest research/test_evidence_temporal.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import evidence_temporal as et  # noqa: E402


def test_is_predictive_eligible_strictly_before():
    assert et.is_predictive_eligible(publication_ts=100, prediction_ts=200) is True


def test_is_predictive_eligible_false_when_after():
    assert et.is_predictive_eligible(publication_ts=300, prediction_ts=200) is False


def test_is_predictive_eligible_false_when_equal_not_strictly_before():
    assert et.is_predictive_eligible(publication_ts=200, prediction_ts=200) is False


def test_is_predictive_eligible_requires_both_timestamps():
    with pytest.raises(ValueError):
        et.is_predictive_eligible(None, 200)
    with pytest.raises(ValueError):
        et.is_predictive_eligible(100, None)


# ---- Section 4: post-prediction information must never enter predictive analysis ----

def test_filter_predictive_evidence_excludes_post_prediction_rows():
    prediction_ts = 1000
    rows = [
        {"headline": "before", "publication_ts": 500},
        {"headline": "after", "publication_ts": 1500},
        {"headline": "exactly_at", "publication_ts": 1000},
    ]
    result = et.filter_predictive_evidence(rows, prediction_ts)
    eligible_headlines = {r["headline"] for r in result["eligible"]}
    excluded_headlines = {r["headline"] for r in result["excluded_post_prediction"]}
    assert eligible_headlines == {"before"}
    assert excluded_headlines == {"after", "exactly_at"}


def test_filter_predictive_evidence_never_drops_rows_silently():
    prediction_ts = 1000
    rows = [{"headline": f"row{i}", "publication_ts": i * 100} for i in range(20)]
    result = et.filter_predictive_evidence(rows, prediction_ts)
    assert len(result["eligible"]) + len(result["excluded_post_prediction"]) == len(rows)


def test_filter_predictive_evidence_empty_input():
    result = et.filter_predictive_evidence([], 1000)
    assert result["eligible"] == []
    assert result["excluded_post_prediction"] == []


# ---- Section 4: same-window ambiguity stays a SEPARATE, non-substituting check ----

def test_annotate_same_window_separation_labels_all_three_relations():
    event_ts = 10_000
    tolerance = 1000
    rows = [
        {"publication_ts": event_ts - 5000},   # PRE_EVENT
        {"publication_ts": event_ts - 500},    # SAME_WINDOW
        {"publication_ts": event_ts + 500},    # SAME_WINDOW
        {"publication_ts": event_ts + 5000},   # POST_EVENT
    ]
    annotated = et.annotate_same_window_separation(rows, event_ts, tolerance)
    relations = [r["event_relative_relation"] for r in annotated]
    assert relations == ["PRE_EVENT", "SAME_WINDOW", "SAME_WINDOW", "POST_EVENT"]


def test_same_window_classification_is_independent_of_prediction_time_eligibility():
    # A SAME_WINDOW article relative to its event can still be
    # prediction-ineligible relative to an earlier prediction, and a
    # PRE_EVENT article can still be prediction-ineligible too -- the
    # two checks must not be conflated (Section 4 explicit requirement).
    event_ts = 10_000
    prediction_ts = 9_000  # prediction made BEFORE the event itself
    row = {"publication_ts": 10_100}  # SAME_WINDOW relative to event, but after prediction_ts
    annotated = et.annotate_same_window_separation([row], event_ts, same_window_tolerance_ms=1000)
    assert annotated[0]["event_relative_relation"] == "SAME_WINDOW"
    # independently, prediction-time eligibility says this is NOT usable
    assert et.is_predictive_eligible(row["publication_ts"], prediction_ts) is False


def test_annotate_same_window_preserves_original_fields():
    rows = [{"publication_ts": 100, "headline": "x"}]
    annotated = et.annotate_same_window_separation(rows, event_ts=100, same_window_tolerance_ms=50)
    assert annotated[0]["headline"] == "x"
