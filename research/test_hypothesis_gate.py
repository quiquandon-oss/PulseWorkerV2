"""
Tests for research/hypothesis_gate.py (PR5e).

All tests execute against a real, in-memory SQLite database shaped like
production (mirrors test_error_classification.py's/test_source_analysis.py's
own fixtures) or against hand-built candidate dicts. No network, no D1
connection, and no test here ever constructs or uses a production
connection.

Run with: python3 -m pytest research/test_hypothesis_gate.py -v
"""
import inspect
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import hypothesis_gate as hg  # noqa: E402
import error_classification as ec  # noqa: E402

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
        sources_json TEXT, technical_score INTEGER, gold_regime TEXT
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
    conn.execute("""CREATE TABLE research_event_evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
        feed_url TEXT, article_url TEXT, publisher TEXT, publication_ts INTEGER,
        collection_ts INTEGER, headline TEXT, keyword_score REAL,
        evidence_relation TEXT, content_hash TEXT
    )""")
    conn.execute("""CREATE TABLE research_hypotheses (
        hypothesis_id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts INTEGER NOT NULL,
        last_updated_ts INTEGER NOT NULL, subject TEXT NOT NULL, statement TEXT NOT NULL,
        source_analysis_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OBSERVATION',
        evidence_summary_json TEXT, out_of_sample_status TEXT
    )""")
    return conn


def insert_prediction(conn, ts, target_ts, p_up, realized_up, realized_return, horizon_hours=24,
                       model_version="test-model"):
    conn.execute(
        "INSERT INTO predictions (ts, target_ts, horizon_hours, p_up, realized_up, realized_return, model_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, target_ts, horizon_hours, p_up, realized_up, realized_return, model_version),
    )


def insert_history(conn, ts, score, sources_json=None, technical_score=None, gold_regime=None):
    conn.execute(
        "INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?, ?, ?, ?, ?)",
        (ts, score, sources_json, technical_score, gold_regime),
    )


def insert_price(conn, ts, price):
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))


# =====================================================================
# 1. Observation without sufficient evidence
# =====================================================================

def test_observation_without_sufficient_evidence():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO",
        "subject": "source:alpha:24h", "source_key": "alpha", "horizon_hours": 24,
        "level2": {"status": "INSUFFICIENT_DATA", "n": 2},
        "level3": None, "evidence_label": None, "repeatability_count": 0,
        "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    gate0 = hg.gate0_observation(candidate)
    assert gate0["passed"] is False
    result = hg.evaluate_candidate(candidate)
    assert result["lifecycle_status"] == "OBSERVATION"


# =====================================================================
# 2. Repeatable observation
# =====================================================================

def test_repeatable_observation_passes_gate1():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO",
        "subject": "source:alpha:24h", "source_key": "alpha", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 100, "effect_size_r": 0.3, "p_corrected": 0.2},
        "level3": None, "evidence_label": "STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE",
        "repeatability_count": 2, "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    gate1 = hg.gate1_repeatability(candidate)
    assert gate1["passed"] is True
    assert gate1["independent_instances"] == 2


def test_non_repeatable_observation_fails_gate1():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO",
        "subject": "source:alpha:24h", "source_key": "alpha", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 100}, "level3": None,
        "evidence_label": "STATISTICALLY_SIGNIFICANT", "repeatability_count": 1,
        "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    assert hg.gate1_repeatability(candidate)["passed"] is False


# =====================================================================
# 3. Association gate
# =====================================================================

def test_association_gate_passes_for_significant_source():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200, "effect_size_r": 0.4, "p_corrected": 0.01},
        "level3": None, "evidence_label": "STATISTICALLY_SIGNIFICANT", "repeatability_count": 1,
        "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    gate2 = hg.gate2_association(candidate)
    assert gate2["passed"] is True
    assert gate2["effect_size_r"] == 0.4


def test_association_gate_fails_for_inconclusive_source():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 50, "effect_size_r": 0.02, "p_corrected": 0.9},
        "level3": None, "evidence_label": "INCONCLUSIVE", "repeatability_count": 0,
        "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    assert hg.gate2_association(candidate)["passed"] is False


# =====================================================================
# 4. Incremental-value gate
# =====================================================================

def test_incremental_value_gate_passes_when_level3_improved():
    # Gate 3 for source candidates checks the DISCOVERY-half partial
    # correlation (a statistic independent of Gate 4's validation-half
    # OOS RMSE comparison) -- see gate3_incremental_value()'s docstring
    # for why duplicating Gate 4's own check here was a bug.
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200}, "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": {"status": "OK", "partial_correlation": 0.2,
                   "oos": {"status": "IMPROVED", "rmse_reduction_pct": 12.0}},
        "repeatability_count": 1, "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    gate3 = hg.gate3_incremental_value(candidate)
    assert gate3["passed"] is True
    assert gate3["beyond_composite"] == "MEASURABLE_PARTIAL_ASSOCIATION"
    # never claims Level-3-beyond-correlated-group
    assert gate3["beyond_correlated_group"] in ("UNKNOWN_STRONG_REDUNDANCY_PRESENT", "NOT_REDUNDANT_OBSERVED")


def test_incremental_value_gate_fails_when_level3_not_improved():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200}, "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": {"status": "OK", "partial_correlation": 0.01, "oos": {"status": "NOT_IMPROVED"}},
        "repeatability_count": 1, "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    assert hg.gate3_incremental_value(candidate)["passed"] is False


# =====================================================================
# 5. Insufficient sample
# =====================================================================

def test_insufficient_sample_returns_insufficient_evidence_not_forced_decision():
    candidate = {
        "candidate_type": "TAXONOMY_CONCENTRATION", "subject": "t", "horizon_hours": 24,
        "category": "STALE_SENTIMENT", "dimension": "regime", "bucket": "chop",
        "n_resolved": 3, "concentrated_share": 0.9, "baseline_share": 0.1,
        "n_evaluable_baseline": 3, "evidence_gate_status_from_pr5d": "OBSERVATION", "suppression": None,
    }
    gate2 = hg.gate2_association(candidate)
    assert gate2["passed"] is False
    result = hg.evaluate_candidate(candidate)
    assert result["evidence_status"] in ("INSUFFICIENT_EVIDENCE", "INCONCLUSIVE")
    assert result["lifecycle_status"] != "BUILD_REQUEST"


# =====================================================================
# 6. Chronological holdout
# =====================================================================

def test_chronological_holdout_uses_ordered_non_shuffled_split():
    src = inspect.getsource(hg.chronological_holdout_for_taxonomy_candidate)
    assert ".shuffle(" not in src
    assert "import random" not in src
    assert "split_idx" in src


def test_chronological_holdout_passes_when_pattern_holds_both_halves():
    conn = fresh_db()
    n = 200  # large enough that BOTH the 70% discovery and 30% validation
    # halves clear MIN_SAMPLE_FOR_HOLDOUT_HALF (30) for the BEARISH bucket.
    for i in range(n):
        # BEARISH bucket concentrated in TECHNICAL_SENTIMENT_CONFLICT throughout
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.7, 0, -1.0, horizon_hours=24)
        insert_history(conn, i * HOUR, 30, technical_score=60)  # composite/technical disagree -> conflict
    rows = ec.fetch_resolved_predictions_with_v1_context(conn, 24, 0, n * HOUR)
    events = ec.fetch_events_for_window(conn, 0, n * HOUR)
    classifications = [ec.classify_prediction(r, events, None, 5.0) for r in rows]
    candidate = {
        "candidate_type": "TAXONOMY_CONCENTRATION", "category": "TECHNICAL_SENTIMENT_CONFLICT",
        "dimension": "v1_composite_bucket", "bucket": "BEARISH", "baseline_share": 0.1,
    }
    result = hg.chronological_holdout_for_taxonomy_candidate(rows, classifications, candidate)
    assert result["status"] == "PASSED_HOLDOUT"
    conn.close()


# =====================================================================
# 7. Failed validation
# =====================================================================

def test_failed_validation_demotes_to_rejected():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200, "effect_size_r": 0.3, "p_corrected": 0.01},
        "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": {"status": "OK", "oos": {"status": "NOT_IMPROVED"}, "n_discovery": 100, "n_validation": 50},
        "repeatability_count": 2, "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    result = hg.evaluate_candidate(candidate)
    # gate3 (incremental) fails here (NOT_IMPROVED), not gate4 itself
    assert result["gate_results"]["gate3"]["passed"] is False
    assert result["lifecycle_status"] != "BUILD_REQUEST"


def test_explicit_failed_holdout_demotes_to_rejected():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200, "effect_size_r": 0.3, "p_corrected": 0.01},
        "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": {"status": "OK", "oos": {"status": "IMPROVED"}, "n_discovery": 100, "n_validation": 50},
        "repeatability_count": 2, "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    gate_results = {
        "gate0": hg.gate0_observation(candidate), "gate1": hg.gate1_repeatability(candidate),
        "gate2": hg.gate2_association(candidate), "gate3": hg.gate3_incremental_value(candidate),
        "gate4": {"validation_status": "FAILED_HOLDOUT", "passed": False},
    }
    assert hg.assign_lifecycle_status(gate_results) == "REJECTED"


# =====================================================================
# 8. Successful validation
# =====================================================================

def test_successful_validation_reaches_build_request():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200, "effect_size_r": 0.3, "p_corrected": 0.01},
        "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": {"status": "OK", "partial_correlation": 0.25,
                   "oos": {"status": "IMPROVED", "rmse_reduction_pct": 10.0},
                   "n_discovery": 140, "n_validation": 60},
        "repeatability_count": 2, "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    result = hg.evaluate_candidate(candidate)
    assert result["lifecycle_status"] == "BUILD_REQUEST"
    assert result["validation_status"] == "PASSED_HOLDOUT"


# =====================================================================
# 9. Correlated-source caveat
# =====================================================================

def test_correlated_source_never_claims_beyond_group():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200, "effect_size_r": 0.3, "p_corrected": 0.01},
        "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": {"status": "OK", "partial_correlation": 0.25,
                   "oos": {"status": "IMPROVED"}, "n_discovery": 140, "n_validation": 60},
        "repeatability_count": 2,
        "redundancy_pairwise": {("a", "b"): {"n": 100, "r": 0.9, "strong_redundancy": True}},
        "redundancy_vs_composite": None,
    }
    note = hg.source_redundancy_note(candidate)
    assert note == "UNKNOWN_STRONG_REDUNDANCY_PRESENT"
    result = hg.evaluate_candidate(candidate)
    assert result["source_redundancy_note"] == "UNKNOWN_STRONG_REDUNDANCY_PRESENT"
    # even a BUILD_REQUEST candidate must carry this caveat, visible, not hidden
    br = hg.build_build_request_candidate(result, candidate)
    assert "UNKNOWN_STRONG_REDUNDANCY_PRESENT" in br["known_confounders"]


def test_no_redundancy_reports_not_redundant_observed_not_a_clearance():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200}, "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": None, "repeatability_count": 1,
        "redundancy_pairwise": {}, "redundancy_vs_composite": {"n": 100, "r": 0.1, "strong_redundancy": False},
    }
    assert hg.source_redundancy_note(candidate) == "NOT_REDUNDANT_OBSERVED"


# =====================================================================
# 10. Post-event evidence cannot become predictive evidence
# =====================================================================

def test_misleading_sentiment_forced_explanatory():
    candidate = {"candidate_type": "TAXONOMY_CONCENTRATION", "category": "MISLEADING_SENTIMENT",
                 "dimension": "regime", "bucket": "chop", "horizon_hours": 24}
    assert hg.evidence_type_for_candidate(candidate) == "EXPLANATORY"


def test_explanatory_only_evidence_blocks_gate5_even_if_all_else_passes():
    gate_results = {
        "gate0": {"passed": True}, "gate1": {"passed": True}, "gate2": {"passed": True},
        "gate3": {"passed": True}, "gate4": {"passed": True, "validation_status": "PASSED_HOLDOUT"},
    }
    gate5 = hg.gate5_build_request_eligible(
        gate_results, evidence_type="EXPLANATORY", evidence_status="STATISTICALLY_SIGNIFICANT")
    assert gate5["passed"] is False
    assert gate5["blocked_by_explanatory_only_evidence"] is True


def test_predictive_evidence_allows_gate5_when_all_else_passes():
    gate_results = {
        "gate0": {"passed": True}, "gate1": {"passed": True}, "gate2": {"passed": True},
        "gate3": {"passed": True}, "gate4": {"passed": True, "validation_status": "PASSED_HOLDOUT"},
    }
    gate5 = hg.gate5_build_request_eligible(
        gate_results, evidence_type="PREDICTIVE", evidence_status="STATISTICALLY_SIGNIFICANT")
    assert gate5["passed"] is True


def test_non_significant_evidence_blocks_gate5_even_if_all_else_passes():
    """'Do not manufacture statistical significance': a candidate that is
    merely STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE may still be a valid
    RESEARCH_HYPOTHESIS / VALIDATION_READY candidate (Gate 2 accepts it),
    but must never reach BUILD_REQUEST on that basis alone."""
    gate_results = {
        "gate0": {"passed": True}, "gate1": {"passed": True}, "gate2": {"passed": True},
        "gate3": {"passed": True}, "gate4": {"passed": True, "validation_status": "PASSED_HOLDOUT"},
    }
    gate5 = hg.gate5_build_request_eligible(
        gate_results, evidence_type="PREDICTIVE",
        evidence_status="STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE")
    assert gate5["passed"] is False
    assert gate5["blocked_by_non_significant_evidence"] is True


# =====================================================================
# 11. Overlapping taxonomy categories preserved, not silently collapsed
# =====================================================================

def test_taxonomy_candidate_preserves_suppression_context():
    ec_report_obs = {"dimension": "v1_composite_bucket", "bucket": "BEARISH",
                      "dominant_error_type": "UNEXPECTED_SHOCK", "n_resolved": 40, "share": 0.6,
                      "evidence_gate_status": "OBSERVATION"}
    ec_report = {"per_horizon": {24: {"candidate_observations": [ec_report_obs]}}}
    overlap_report = {"per_horizon": {24: {
        "matched_category_distribution": {"n_evaluable": 100, "matched_counts": {"UNEXPECTED_SHOCK": 50, "REGIME_CHANGE": 20}},
        "suppression_analysis": {"UNEXPECTED_SHOCK": {"matched": 50, "winner": 40, "suppressed": 10}},
    }}}
    candidates = hg.derive_taxonomy_candidates(ec_report, overlap_report)
    assert len(candidates) == 1
    assert candidates[0]["suppression"]["suppressed"] == 10
    assert candidates[0]["baseline_share"] == 0.5


# =====================================================================
# 12. winner_label vs all_matched_categories preserved distinctly
# =====================================================================

def test_observation_detail_never_collapses_winner_vs_matched():
    candidate = {
        "candidate_type": "TAXONOMY_CONCENTRATION", "subject": "t", "horizon_hours": 24,
        "category": "UNEXPECTED_SHOCK", "dimension": "regime", "bucket": "chop",
        "n_resolved": 40, "concentrated_share": 0.6, "baseline_share": 0.3,
        "n_evaluable_baseline": 100, "evidence_gate_status_from_pr5d": "OBSERVATION",
        "suppression": {"matched": 50, "winner": 40, "suppressed": 10},
    }
    result = hg.evaluate_candidate(candidate)
    detail = result["observation"]["detail"]
    assert "suppression" in detail
    assert detail["suppression"]["matched"] != detail["suppression"]["winner"]  # distinct, not collapsed


# =====================================================================
# 13. Hypothesis lifecycle transitions
# =====================================================================

def test_lifecycle_transitions_in_order():
    base = {"gate0": {"passed": False}, "gate1": {"passed": False}, "gate2": {"passed": False},
            "gate3": {"passed": False}, "gate4": {"passed": False, "validation_status": "NOT_YET_TESTED"},
            "gate5": {"passed": False}}
    assert hg.assign_lifecycle_status(base) == "OBSERVATION"

    g = dict(base); g["gate0"] = {"passed": True}
    assert hg.assign_lifecycle_status(g) == "OBSERVATION"

    g["gate1"] = {"passed": True}
    assert hg.assign_lifecycle_status(g) == "MONITOR"

    g["gate2"] = {"passed": True}
    assert hg.assign_lifecycle_status(g) == "RESEARCH_HYPOTHESIS"

    g["gate3"] = {"passed": True}
    assert hg.assign_lifecycle_status(g) == "VALIDATION_READY"

    g["gate4"] = {"passed": True, "validation_status": "PASSED_HOLDOUT"}
    g["gate5"] = {"passed": True}
    assert hg.assign_lifecycle_status(g) == "BUILD_REQUEST"


def test_never_assigns_beyond_build_request():
    src = inspect.getsource(hg.assign_lifecycle_status)
    for forbidden in ("AWAITING_APPROVAL", "IMPLEMENTED", "VALIDATED", "ROLLED_BACK"):
        assert forbidden not in src


# =====================================================================
# 14. BUILD_REQUEST requires validation
# =====================================================================

def test_build_request_requires_gate4_passed():
    gate_results = {"gate0": {"passed": True}, "gate1": {"passed": True}, "gate2": {"passed": True},
                     "gate3": {"passed": True}, "gate4": {"passed": False, "validation_status": "NOT_YET_TESTED"}}
    gate5 = hg.gate5_build_request_eligible(
        gate_results, evidence_type="PREDICTIVE", evidence_status="STATISTICALLY_SIGNIFICANT")
    assert gate5["passed"] is False


# =====================================================================
# 15. No BUILD_REQUEST when validation fails
# =====================================================================

def test_no_build_request_when_validation_fails():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200, "effect_size_r": 0.3, "p_corrected": 0.01},
        "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": {"status": "OK", "oos": {"status": "NOT_IMPROVED"}, "n_discovery": 140, "n_validation": 60},
        "repeatability_count": 2, "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    result = hg.evaluate_candidate(candidate)
    assert result["lifecycle_status"] != "BUILD_REQUEST"
    with pytest.raises(ValueError):
        hg.build_build_request_candidate(result, candidate)


# =====================================================================
# 16. Evidence against a hypothesis prevents premature promotion
# =====================================================================

def test_contradicted_evidence_never_passes_association_gate():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200, "effect_size_r": 0.1, "p_corrected": 0.8},
        "evidence_label": "CONTRADICTED", "level3": None, "repeatability_count": 0,
        "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    gate2 = hg.gate2_association(candidate)
    assert gate2["passed"] is False
    result = hg.evaluate_candidate(candidate)
    assert result["lifecycle_status"] in ("OBSERVATION", "MONITOR")
    assert result["lifecycle_status"] != "BUILD_REQUEST"


# =====================================================================
# 17. Persistence / retrieval
# =====================================================================

def test_persist_and_retrieve_hypothesis():
    conn = fresh_db()
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200, "effect_size_r": 0.3, "p_corrected": 0.01},
        "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": {"status": "OK", "oos": {"status": "IMPROVED"}, "n_discovery": 140, "n_validation": 60},
        "repeatability_count": 2, "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    result = hg.evaluate_candidate(candidate)
    hypothesis_id = hg.persist_hypothesis(conn, 1000, result, source_analysis_ids=[])
    row = conn.execute(
        "SELECT subject, statement, status, out_of_sample_status, evidence_summary_json, source_analysis_ids "
        "FROM research_hypotheses WHERE hypothesis_id = ?", (hypothesis_id,)
    ).fetchone()
    assert row is not None
    subject, statement, status, oos_status, evidence_json, source_ids_json = row
    assert subject == result["subject"]
    assert status == result["lifecycle_status"]
    assert oos_status == result["validation_status"]
    payload = json.loads(evidence_json)
    assert payload["evidence_status"] == result["evidence_status"]
    assert json.loads(source_ids_json) == []
    conn.close()


def test_persist_never_exceeds_build_request_status():
    conn = fresh_db()
    for lifecycle_status in hg._ALLOWED_ASSIGNABLE_STATUSES:
        fake_result = {
            "subject": f"s:{lifecycle_status}", "hypothesis_statement": "x",
            "lifecycle_status": lifecycle_status, "validation_status": "NOT_YET_TESTED",
            "evidence_status": "INCONCLUSIVE", "evidence_type": "PREDICTIVE",
            "source_redundancy_note": "NOT_APPLICABLE",
            "observation": {}, "association": {}, "incremental_information": {},
            "gate_results": {}, "caveat": "x",
        }
        hg.persist_hypothesis(conn, 1000, fake_result)
    rows = conn.execute("SELECT status FROM research_hypotheses").fetchall()
    for (status,) in rows:
        assert status in hg._ALLOWED_ASSIGNABLE_STATUSES
        assert status not in ("AWAITING_APPROVAL", "IMPLEMENTED", "VALIDATED", "ROLLED_BACK")
    conn.close()


# =====================================================================
# 18. Deterministic / reproducible results
# =====================================================================

def test_deterministic_rerun_identical_report():
    conn = fresh_db()
    for i in range(60):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.5 + (i % 5) * 0.08,
                           1 if i % 3 == 0 else 0, ((-1) ** i) * (i % 4) * 0.5,
                           horizon_hours=24, model_version="m1")
        insert_history(conn, i * HOUR, 40 + (i % 30),
                        sources_json=json.dumps({"alpha": (i % 10) - 5, "beta": (i % 7) - 3}),
                        technical_score=40 + (i % 25))
        insert_price(conn, i * HOUR, 100.0 + i)
    report_1 = hg.build_hypothesis_report(conn, 0, 60 * HOUR, horizons=(24,), source_horizons=(24,))
    report_2 = hg.build_hypothesis_report(conn, 0, 60 * HOUR, horizons=(24,), source_horizons=(24,))
    assert report_1 == report_2
    conn.close()


# =====================================================================
# No production writes / no network / bounded windows
# =====================================================================

def test_only_persist_hypothesis_contains_insert_into():
    functions = [
        hg.derive_source_candidates, hg.derive_taxonomy_candidates, hg.evidence_type_for_candidate,
        hg.source_redundancy_note, hg.gate0_observation, hg.gate1_repeatability, hg.gate2_association,
        hg.gate3_incremental_value, hg.gate4_out_of_sample, hg.gate5_build_request_eligible,
        hg.assign_lifecycle_status, hg.assign_evidence_status, hg.evaluate_candidate,
        hg.build_build_request_candidate, hg.build_hypothesis_report,
        hg.chronological_holdout_for_taxonomy_candidate,
    ]
    for fn in functions:
        fn_src = inspect.getsource(fn)
        for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
            assert forbidden not in fn_src, f"{fn.__name__} must be read-only -- found {forbidden}"
    assert "INSERT INTO" in inspect.getsource(hg.persist_hypothesis)


def test_no_network_or_llm_calls_anywhere_in_module():
    src = inspect.getsource(hg)
    for forbidden in ["import requests", "requests.get(", "requests.post(", "urllib", "fetch(",
                       "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_empty_window_produces_empty_report_not_error():
    conn = fresh_db()
    report = hg.build_hypothesis_report(conn, 0, HOUR, horizons=(24,), source_horizons=(24,))
    assert report["n_source_candidates"] == 0
    assert report["n_taxonomy_candidates"] == 0
    conn.close()
