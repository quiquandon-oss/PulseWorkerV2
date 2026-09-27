"""
Tests for research/stage7_sentiment_recalculation.py.

Run with: python3 -m pytest research/test_stage7_sentiment_recalculation.py -v
"""
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import stage7_sentiment_recalculation as sr  # noqa: E402

EVENT = {"event_id": 42, "event_ts": 1_000_000}


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, ts INTEGER, score INTEGER, "
                 "technical_score INTEGER, sources_json TEXT)")
    return conn


def test_fetch_v1_macro_context_reads_nearest_row_at_or_before_event_ts():
    conn = _fresh_db()
    conn.execute("INSERT INTO history VALUES (1, 900000, 55, 60, '{\"fng\": 40}')")
    conn.execute("INSERT INTO history VALUES (2, 1100000, 70, 65, '{\"fng\": 80}')")  # after event, must be ignored
    conn.commit()
    result = sr.fetch_v1_macro_context(conn, EVENT["event_ts"])
    assert result["available"] is True
    assert result["observation_ts"] == 900000
    assert result["composite_score"] == 55
    assert result["sources_snapshot"] == {"fng": 40}


def test_fetch_v1_macro_context_no_row_before_event_is_honestly_unavailable():
    conn = _fresh_db()
    conn.execute("INSERT INTO history VALUES (1, 2000000, 55, 60, '{}')")
    conn.commit()
    result = sr.fetch_v1_macro_context(conn, EVENT["event_ts"])
    assert result["available"] is False


def test_fetch_v1_macro_context_malformed_sources_json_degrades_to_empty_dict():
    conn = _fresh_db()
    conn.execute("INSERT INTO history VALUES (1, 900000, 55, 60, 'not-json')")
    conn.commit()
    result = sr.fetch_v1_macro_context(conn, EVENT["event_ts"])
    assert result["available"] is True
    assert result["sources_snapshot"] == {}


def test_dedupe_evidence_no_duplicates_passthrough():
    rows = [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "b"}]
    deduped, groups = sr.dedupe_evidence(rows)
    assert len(deduped) == 2
    assert groups == {}


def test_dedupe_evidence_keeps_first_seen_and_reports_group():
    rows = [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "a"}]
    deduped, groups = sr.dedupe_evidence(rows)
    assert len(deduped) == 1
    assert deduped[0]["evidence_id"] == 1
    assert groups["a"]["kept_evidence_id"] == 1
    assert groups["a"]["dropped_evidence_ids"] == [2]


def test_dedupe_evidence_missing_content_hash_never_discarded():
    rows = [{"evidence_id": 1, "content_hash": None}, {"evidence_id": 2, "content_hash": None}]
    deduped, groups = sr.dedupe_evidence(rows)
    assert len(deduped) == 2
    assert groups == {}


def test_compute_event_sentiment_no_response_leaves_sentiment_null_even_when_sufficient():
    v1_ctx = {"available": True, "observation_ts": 900000, "composite_score": 55}
    result = sr.compute_event_sentiment(
        EVENT, [{"evidence_id": 1, "content_hash": "a"}], {"cryptonews": "EVENT_SOURCE_ALIGNED"},
        "SUFFICIENT", v1_ctx, validated_response=None,
    )
    assert result["sentiment_label"] is None
    assert result["sentiment_score"] is None
    assert result["evidence_sufficiency"] == "SUFFICIENT"
    assert result["contributing_evidence_ids"] == [1]


def test_compute_event_sentiment_unvalidated_response_never_sets_a_score():
    v1_ctx = {"available": False}
    response = {"response_id": "r1", "validation_status": "PENDING",
                "findings": {"sentiment_assessment": "POSITIVE"}}
    result = sr.compute_event_sentiment(EVENT, [], {}, "INSUFFICIENT", v1_ctx, validated_response=response)
    assert result["sentiment_label"] is None
    assert result["sentiment_score"] is None
    assert result["ai_research_response_id"] is None


def test_compute_event_sentiment_validated_positive_response_sets_score_100():
    v1_ctx = {"available": False}
    response = {"response_id": "r1", "validation_status": "VALIDATED",
                "findings": {"sentiment_assessment": "POSITIVE"}}
    result = sr.compute_event_sentiment(EVENT, [], {}, "INSUFFICIENT_EVIDENCE", v1_ctx, validated_response=response)
    assert result["sentiment_label"] == "POSITIVE"
    assert result["sentiment_score"] == 100.0
    assert result["ai_research_response_id"] == "r1"


def test_compute_event_sentiment_validated_indeterminate_leaves_score_null():
    v1_ctx = {"available": False}
    response = {"response_id": "r1", "validation_status": "VALIDATED",
                "findings": {"sentiment_assessment": "INDETERMINATE"}}
    result = sr.compute_event_sentiment(EVENT, [], {}, "CONFLICTING", v1_ctx, validated_response=response)
    assert result["sentiment_label"] == "INDETERMINATE"
    assert result["sentiment_score"] is None


def test_compute_event_sentiment_full_source_set_includes_both_stage6_and_stage7_evidence():
    # "Never calculate using only newly discovered sources" -- both an
    # original Stage 6 row and a hypothetical Stage-7-added row must
    # both appear in contributing_evidence_ids.
    v1_ctx = {"available": False}
    evidence = [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "b"}]
    result = sr.compute_event_sentiment(EVENT, evidence, {}, "SUFFICIENT", v1_ctx)
    assert set(result["contributing_evidence_ids"]) == {1, 2}


def test_compute_event_sentiment_duplicate_evidence_excluded_with_reason():
    v1_ctx = {"available": False}
    evidence = [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "a"}]
    result = sr.compute_event_sentiment(EVENT, evidence, {}, "SUFFICIENT", v1_ctx)
    assert result["contributing_evidence_ids"] == [1]
    assert result["excluded_evidence"] == [{"evidence_id": 2, "reason": "duplicate content_hash of evidence_id 1"}]


def test_input_fingerprint_stable_for_same_inputs():
    a = sr.compute_input_fingerprint(1, [3, 2, 1], "SUFFICIENT", None)
    b = sr.compute_input_fingerprint(1, [1, 2, 3], "SUFFICIENT", None)  # order must not matter
    assert a == b


def test_input_fingerprint_changes_when_evidence_changes():
    a = sr.compute_input_fingerprint(1, [1, 2], "SUFFICIENT", None)
    b = sr.compute_input_fingerprint(1, [1, 2, 3], "SUFFICIENT", None)
    assert a != b


def test_is_idempotent_repeat_true_for_identical_fingerprint():
    v1_ctx = {"available": False}
    evidence = [{"evidence_id": 1, "content_hash": "a"}]
    first = sr.compute_event_sentiment(EVENT, evidence, {}, "SUFFICIENT", v1_ctx)
    second = sr.compute_event_sentiment(EVENT, evidence, {}, "SUFFICIENT", v1_ctx)
    assert sr.is_idempotent_repeat(second, first) is True


def test_is_idempotent_repeat_false_when_evidence_changed():
    v1_ctx = {"available": False}
    first = sr.compute_event_sentiment(EVENT, [{"evidence_id": 1, "content_hash": "a"}], {}, "SUFFICIENT", v1_ctx)
    second = sr.compute_event_sentiment(
        EVENT, [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "b"}],
        {}, "SUFFICIENT", v1_ctx,
    )
    assert sr.is_idempotent_repeat(second, first) is False


def test_is_idempotent_repeat_false_when_no_previous_result():
    v1_ctx = {"available": False}
    result = sr.compute_event_sentiment(EVENT, [], {}, "SUFFICIENT", v1_ctx)
    assert sr.is_idempotent_repeat(result, None) is False
