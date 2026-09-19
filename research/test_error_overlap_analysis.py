"""
Tests for research/error_overlap_analysis.py (PR5d-followup).

All tests execute against a real, in-memory SQLite database shaped like
production (mirrors test_error_classification.py's own fresh_db()) or
against hand-built classify_prediction()-shaped fixtures. No network,
no D1 connection required.

Run with: python3 -m pytest research/test_error_overlap_analysis.py -v
"""
import inspect
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import error_classification as ec  # noqa: E402
import error_overlap_analysis as eoa  # noqa: E402

HOUR = 3600000
DAY = 24 * HOUR


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, target_ts INTEGER,
        horizon_hours INTEGER NOT NULL, p_up REAL, realized_up INTEGER, realized_return REAL,
        model_version TEXT, git_commit_sha TEXT
    )""")
    conn.execute("CREATE INDEX idx_predictions_horizon_ts ON predictions(horizon_hours, ts)")
    conn.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL,
        technical_score INTEGER, gold_regime TEXT
    )""")
    conn.execute("CREATE INDEX idx_ts ON history(ts)")
    conn.execute("""CREATE TABLE btc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
    )""")
    conn.execute("CREATE INDEX idx_btc_data_ts ON btc_data(ts)")
    conn.execute("""CREATE TABLE research_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL,
        event_ts INTEGER NOT NULL, detection_ts INTEGER NOT NULL, category TEXT NOT NULL,
        direction TEXT, intensity REAL, available_before_prediction INTEGER NOT NULL,
        is_post_event_analysis INTEGER NOT NULL DEFAULT 0,
        trigger_metric TEXT, trigger_threshold REAL, trigger_version TEXT
    )""")
    return conn


def insert_prediction(conn, ts, target_ts, p_up, realized_up, realized_return, horizon_hours=24,
                       model_version="test-model"):
    conn.execute(
        "INSERT INTO predictions (ts, target_ts, horizon_hours, p_up, realized_up, realized_return, model_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, target_ts, horizon_hours, p_up, realized_up, realized_return, model_version),
    )


def insert_history(conn, ts, score, technical_score=None, gold_regime=None):
    conn.execute("INSERT INTO history (ts, score, technical_score, gold_regime) VALUES (?, ?, ?, ?)",
                 (ts, score, technical_score, gold_regime))


# A synthetic classify_prediction()-shaped result, built directly (not
# via classify_prediction itself) so tests can pin exact
# contributing_signals combinations without depending on event windows.
def make_classification(error_type, status="RESOLVED", **signal_overrides):
    signals = {
        "large_move_events_in_window": 0,
        "regime_reversal_events_in_window": 0,
        "volatility_expansion_events_in_window": 0,
        "v1_btc_divergence_events_in_window": 0,
        "technical_sentiment_conflict": False,
        "v1_staleness_gap_exceeds_proposed_threshold": False,
    }
    signals.update(signal_overrides)
    return {"status": status, "error_type": error_type, "contributing_signals": signals,
            "classification_rule_version": ec.CLASSIFICATION_RULE_VERSION}


def make_unevaluable(status="RESOLVED"):
    return {"status": status, "error_type": ("INSUFFICIENT_INFORMATION" if status == "RESOLVED" else None),
            "contributing_signals": {"reason": "no_v1_context_at_prediction_time"} if status == "RESOLVED" else {},
            "classification_rule_version": ec.CLASSIFICATION_RULE_VERSION}


# =====================================================================
# A. Single-category match
# =====================================================================

def test_single_category_match():
    c = make_classification("REGIME_CHANGE", regime_reversal_events_in_window=1)
    matched, evaluable = eoa.matched_categories_for_classification(c)
    assert evaluable is True
    assert matched == ["REGIME_CHANGE"]


# =====================================================================
# B. Two-category overlap
# =====================================================================

def test_two_category_overlap():
    c = make_classification("UNEXPECTED_SHOCK", large_move_events_in_window=1, regime_reversal_events_in_window=1)
    matched, evaluable = eoa.matched_categories_for_classification(c)
    assert evaluable is True
    assert set(matched) == {"UNEXPECTED_SHOCK", "REGIME_CHANGE"}


# =====================================================================
# C. Three-category overlap (the task's worked example)
# =====================================================================

def test_three_category_overlap_worked_example():
    c = make_classification(
        "UNEXPECTED_SHOCK",
        large_move_events_in_window=1,
        regime_reversal_events_in_window=1,
        v1_btc_divergence_events_in_window=1,
    )
    matched, evaluable = eoa.matched_categories_for_classification(c)
    assert evaluable is True
    assert set(matched) == {"UNEXPECTED_SHOCK", "REGIME_CHANGE", "MISLEADING_SENTIMENT"}
    # winner_label must equal the classifier's own error_type, unaffected
    # by how many OTHER categories also matched.
    records = eoa.build_overlap_records([c])
    assert records[0]["winner_label"] == "UNEXPECTED_SHOCK"
    assert set(records[0]["all_matched_categories"]) == {"UNEXPECTED_SHOCK", "REGIME_CHANGE", "MISLEADING_SENTIMENT"}


# =====================================================================
# D. Winner differs from matched category (the "reverse" case: a
# category matched but never wins for this row)
# =====================================================================

def test_winner_differs_from_a_matched_category():
    c = make_classification("UNEXPECTED_SHOCK", large_move_events_in_window=1, regime_reversal_events_in_window=1)
    record = eoa.build_overlap_records([c])[0]
    assert record["winner_label"] == "UNEXPECTED_SHOCK"
    assert "REGIME_CHANGE" in record["all_matched_categories"]
    assert record["winner_label"] != "REGIME_CHANGE"


def test_category_present_in_matched_but_never_wins_across_many_rows():
    # REGIME_CHANGE matches every row (alongside UNEXPECTED_SHOCK, which
    # always outranks it) -- REGIME_CHANGE should show up in
    # matched_category_distribution but with winner=0 in suppression_analysis.
    classifications = [
        make_classification("UNEXPECTED_SHOCK", large_move_events_in_window=1, regime_reversal_events_in_window=1)
        for _ in range(35)
    ]
    records = eoa.build_overlap_records(classifications)
    dist = eoa.matched_category_distribution(records)
    assert dist["matched_counts"]["REGIME_CHANGE"] == 35
    suppression = eoa.suppression_analysis(records)
    assert suppression["REGIME_CHANGE"]["winner"] == 0
    assert suppression["REGIME_CHANGE"]["matched"] == 35
    assert suppression["REGIME_CHANGE"]["suppressed"] == 35


# =====================================================================
# E. No matched categories
# =====================================================================

def test_no_matched_categories():
    c = make_classification("WRONG_DIRECTION")  # all signals False/0
    matched, evaluable = eoa.matched_categories_for_classification(c)
    assert evaluable is True
    assert matched == []


def test_not_evaluable_rows_report_no_matches_and_are_excluded():
    c = make_unevaluable()
    matched, evaluable = eoa.matched_categories_for_classification(c)
    assert evaluable is False
    assert matched == []
    # excluded from n_evaluable everywhere
    records = eoa.build_overlap_records([c])
    matrix_result = eoa.co_occurrence_matrix(records)
    assert matrix_result["n_evaluable"] == 0


def test_unresolved_rows_are_not_evaluable():
    c = make_classification(None, status="UNRESOLVED")
    matched, evaluable = eoa.matched_categories_for_classification(c)
    assert evaluable is False
    assert matched == []


# =====================================================================
# F. Symmetric co-occurrence matrix
# =====================================================================

def test_co_occurrence_matrix_is_symmetric():
    classifications = [
        make_classification("UNEXPECTED_SHOCK", large_move_events_in_window=1, regime_reversal_events_in_window=1),
        make_classification("STALE_SENTIMENT", regime_reversal_events_in_window=1,
                             v1_staleness_gap_exceeds_proposed_threshold=True),
        make_classification("WRONG_DIRECTION"),  # matches nothing
    ]
    records = eoa.build_overlap_records(classifications)
    result = eoa.co_occurrence_matrix(records)
    for a in eoa.OVERLAP_CATEGORIES:
        for b in eoa.OVERLAP_CATEGORIES:
            assert result["matrix"][(a, b)]["count_both"] == result["matrix"][(b, a)]["count_both"]


def test_co_occurrence_diagonal_equals_single_count():
    classifications = [
        make_classification("UNEXPECTED_SHOCK", large_move_events_in_window=1) for _ in range(5)
    ]
    records = eoa.build_overlap_records(classifications)
    result = eoa.co_occurrence_matrix(records)
    assert result["matrix"][("UNEXPECTED_SHOCK", "UNEXPECTED_SHOCK")]["count_both"] == 5
    assert result["single_counts"]["UNEXPECTED_SHOCK"] == 5


# =====================================================================
# G. Suppression calculation (reconciliation)
# =====================================================================

def test_suppression_counts_reconcile_with_matched_counts():
    classifications = (
        [make_classification("UNEXPECTED_SHOCK", large_move_events_in_window=1, regime_reversal_events_in_window=1)
         for _ in range(10)]
        + [make_classification("REGIME_CHANGE", regime_reversal_events_in_window=1) for _ in range(4)]
    )
    records = eoa.build_overlap_records(classifications)
    suppression = eoa.suppression_analysis(records)
    for category in eoa.OVERLAP_CATEGORIES:
        s = suppression[category]
        assert s["matched"] == s["winner"] + s["suppressed"]
    assert suppression["REGIME_CHANGE"]["matched"] == 14
    assert suppression["REGIME_CHANGE"]["winner"] == 4
    assert suppression["REGIME_CHANGE"]["suppressed"] == 10


def test_suppression_rate_none_when_never_matched():
    records = eoa.build_overlap_records([make_classification("WRONG_DIRECTION")])
    suppression = eoa.suppression_analysis(records)
    assert suppression["STALE_SENTIMENT"]["matched"] == 0
    assert suppression["STALE_SENTIMENT"]["suppression_rate"] is None


# =====================================================================
# H. Multiple categories suppressed by the same winner
# =====================================================================

def test_multiple_categories_suppressed_by_same_winner():
    classifications = [
        make_classification("UNEXPECTED_SHOCK", large_move_events_in_window=1, regime_reversal_events_in_window=1,
                             v1_btc_divergence_events_in_window=1)
        for _ in range(20)
    ]
    records = eoa.build_overlap_records(classifications)
    suppression = eoa.suppression_analysis(records)
    assert suppression["REGIME_CHANGE"]["suppressed_by_winner"] == {"UNEXPECTED_SHOCK": 20}
    assert suppression["MISLEADING_SENTIMENT"]["suppressed_by_winner"] == {"UNEXPECTED_SHOCK": 20}


def test_suppressed_by_winner_breaks_down_across_multiple_winners():
    classifications = (
        [make_classification("UNEXPECTED_SHOCK", large_move_events_in_window=1, regime_reversal_events_in_window=1)
         for _ in range(6)]
        + [make_classification("MISSING_EVENT", volatility_expansion_events_in_window=1,
                                regime_reversal_events_in_window=1)
           for _ in range(3)]
    )
    records = eoa.build_overlap_records(classifications)
    suppression = eoa.suppression_analysis(records)
    assert suppression["REGIME_CHANGE"]["suppressed_by_winner"] == {"UNEXPECTED_SHOCK": 6, "MISSING_EVENT": 3}


# =====================================================================
# I. Category never appearing as winner but appearing in
# all_matched_categories (the analytical "reverse" case)
# =====================================================================

def test_category_never_wins_but_appears_in_matched_distribution():
    classifications = [
        make_classification("UNEXPECTED_SHOCK", large_move_events_in_window=1, regime_reversal_events_in_window=1)
        for _ in range(50)
    ]
    records = eoa.build_overlap_records(classifications)
    dist = eoa.matched_category_distribution(records)
    winner_counts = {}
    for c in classifications:
        winner_counts[c["error_type"]] = winner_counts.get(c["error_type"], 0) + 1
    assert dist["matched_counts"]["REGIME_CHANGE"] == 50
    assert winner_counts.get("REGIME_CHANGE", 0) == 0
    explanation = eoa.explain_absent_winners(eoa.suppression_analysis(records))
    assert explanation["REGIME_CHANGE"]["fully_suppressed"] is True
    assert explanation["REGIME_CHANGE"]["suppressed_by_winner"] == {"UNEXPECTED_SHOCK": 50}


# =====================================================================
# J. Regression: PR5d's own winner label is unchanged by this module
# =====================================================================

def test_winner_label_is_verbatim_copy_of_classify_prediction_error_type():
    conn = fresh_db()
    for i in range(40):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.7, 0 if i % 2 == 0 else 1,
                           -1.0 if i % 2 == 0 else 1.0, horizon_hours=24)
        insert_history(conn, i * HOUR, 55, 55)
    rows = ec.fetch_resolved_predictions_with_v1_context(conn, 24, 0, 40 * HOUR)
    events = ec.fetch_events_for_window(conn, 0, 40 * HOUR)
    original_classifications = [ec.classify_prediction(r, events, None, 5.0) for r in rows]
    records = eoa.build_overlap_records(original_classifications)
    for original, record in zip(original_classifications, records):
        assert record["winner_label"] == original["error_type"]
    conn.close()


def test_real_run_winner_distribution_matches_ec_breakdown_exactly():
    conn = fresh_db()
    for i in range(60):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.5 + (i % 5) * 0.08,
                           1 if i % 3 == 0 else 0, ((-1) ** i) * (i % 4) * 0.5, horizon_hours=24)
        insert_history(conn, i * HOUR, 40 + (i % 30), 40 + (i % 25))
    report = eoa.build_overlap_report(conn, 0, 60 * HOUR, horizons=(24,))
    ec_report = ec.build_error_classification_report(conn, 0, 60 * HOUR, horizons=(24,))
    assert report["per_horizon"][24]["winner_distribution"] == ec_report["per_horizon"][24]["overall"]
    conn.close()


# =====================================================================
# Precedence order fidelity (constructive, not just asserted)
# =====================================================================

def test_precedence_order_matches_classify_prediction_source():
    src = inspect.getsource(ec.classify_prediction)
    body = src[src.index("if not direction_correct:"):]
    order_markers = ["large_moves", "regime_reversals", "vol_expansions", "v1_divergences",
                      "technical_conflict", "is_stale"]
    positions = [body.index(m) for m in order_markers]
    assert positions == sorted(positions)
    assert eoa.PRECEDENCE_ORDER == eoa.OVERLAP_CATEGORIES


# =====================================================================
# Determinism / no production writes / no network / bounded windows
# =====================================================================

def test_deterministic_rerun_identical_report():
    conn = fresh_db()
    for i in range(50):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.5 + (i % 5) * 0.08,
                           1 if i % 3 == 0 else 0, ((-1) ** i) * (i % 4) * 0.5, horizon_hours=24)
        insert_history(conn, i * HOUR, 40 + (i % 30), 40 + (i % 25))
    report_1 = eoa.build_overlap_report(conn, 0, 50 * HOUR, horizons=(24,))
    report_2 = eoa.build_overlap_report(conn, 0, 50 * HOUR, horizons=(24,))
    assert report_1 == report_2
    conn.close()


def test_no_insert_update_delete_ddl_anywhere_in_module():
    src = inspect.getsource(eoa)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE", "ALTER TABLE", "DROP "]:
        assert forbidden not in src, f"error_overlap_analysis.py must be read-only -- found {forbidden}"


def test_no_network_or_llm_calls():
    src = inspect.getsource(eoa)
    for forbidden in ["requests.", "urllib", "fetch(", "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_empty_window_produces_empty_report_not_error():
    conn = fresh_db()
    report = eoa.build_overlap_report(conn, 0, HOUR, horizons=(24,))
    assert report["per_horizon"][24]["n_evaluable_for_overlap"] == 0
    conn.close()


def test_specific_overlap_findings_covers_required_pairs():
    conn = fresh_db()
    for i in range(40):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.7, 0, -6.0, horizon_hours=24)
        insert_history(conn, i * HOUR, 55, 55)
    report = eoa.build_overlap_report(conn, 0, 40 * HOUR, horizons=(24,))
    findings = report["per_horizon"][24]["specific_overlap_findings"]
    assert "UNEXPECTED_SHOCK+REGIME_CHANGE" in findings
    assert "UNEXPECTED_SHOCK+MISLEADING_SENTIMENT" in findings
    assert "REGIME_CHANGE+MISSING_EVENT" in findings
    assert "MISLEADING_SENTIMENT+TECHNICAL_SENTIMENT_CONFLICT" in findings
    assert "STALE_SENTIMENT+TECHNICAL_SENTIMENT_CONFLICT" in findings
    conn.close()


def test_underlying_event_category_mapping_documented():
    conn = fresh_db()
    report = eoa.build_overlap_report(conn, 0, HOUR, horizons=(24,))
    mapping = report["underlying_event_category_mapping"]
    assert mapping["UNEXPECTED_SHOCK"] == "LARGE_MOVE"
    assert mapping["REGIME_CHANGE"] == "REGIME_REVERSAL"
    assert mapping["MISSING_EVENT"] == "VOLATILITY_EXPANSION"
    assert mapping["MISLEADING_SENTIMENT"] == "V1_BTC_DIVERGENCE"
    conn.close()


def test_winner_vs_matched_note_present_and_distinct_keys():
    conn = fresh_db()
    report = eoa.build_overlap_report(conn, 0, HOUR, horizons=(24,))
    assert "winner_vs_matched_note" in report
    ph = report["per_horizon"][24]
    assert "winner_distribution" in ph and "matched_category_distribution" in ph
    assert ph["winner_distribution"] is not ph["matched_category_distribution"]
