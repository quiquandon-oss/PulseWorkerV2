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
import source_analysis as sa  # noqa: E402

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


def _synthetic_matrix_source(n, y_fn, source_key="a"):
    """Builds (matrix_rows, outcome_rows) in the exact shape
    source_analysis.extract_source_matrix()/outcome_engine.compute_
    forward_returns_from_history() return, WITHOUT touching a DB --
    y_fn(i, composite, source) controls the outcome deterministically
    (no randomness -- every value below is exactly reproducible) so
    Gate 4's magnitude/stability behavior can be verified against a
    real, non-mocked call to hg.source_subsplit_stability() and
    sa.level3_incremental_for_source().
    """
    matrix_rows, outcome_rows = [], []
    for i in range(n):
        ts = i * HOUR
        composite = 50 + (i % 7)
        source_value = (i % 11) - 5
        y = y_fn(i, composite, source_value)
        matrix_rows.append({"ts": ts, "v1_composite": float(composite), "gold_regime": None,
                             "sources": {source_key: float(source_value)}})
        outcome_rows.append({"anchor_ts": ts, "outcome_status": "RESOLVED", "forward_return_pct": y})
    return matrix_rows, outcome_rows


def _deterministic_noise(i):
    """A fixed, non-random pseudo-noise sequence in [-6, 6] -- deterministic
    (same input always gives same output), used only to make synthetic
    OOS fixtures realistic (a perfectly noiseless relationship makes
    EVERY nonzero coefficient look like a ~100% RMSE reduction, which
    is not representative of the real production snapshot)."""
    return ((i * 37) % 13) - 6


def _source_candidate_from_level3(level3, source_key="a", horizon_hours=24,
                                   evidence_label="STATISTICALLY_SIGNIFICANT", repeatability_count=2,
                                   redundancy_pairwise=None, redundancy_vs_composite=None):
    return {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": f"source:{source_key}:{horizon_hours}h",
        "source_key": source_key, "horizon_hours": horizon_hours,
        "level2": {"status": "OK", "n": level3.get("n"), "effect_size_r": 0.9, "p_corrected": 0.0001},
        "evidence_label": evidence_label, "level3": level3, "repeatability_count": repeatability_count,
        "redundancy_pairwise": redundancy_pairwise or {}, "redundancy_vs_composite": redundancy_vs_composite,
    }


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
    assert gate3["beyond_correlated_group"] in ("REDUNDANCY_UNRESOLVED", "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED")
    # v2: Gate 3 must explicitly flag it is NOT independent confirmation of Gate 2
    assert gate3["independent_of_gate2"] is False
    assert "not independent" in gate3["note"].lower() or "never" in gate3["note"].lower()


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
# 7. Failed validation (v2: a clearly NEGATIVE full-validation OOS point
# estimate, verified against a real regime-reversal fixture)
# =====================================================================

def test_failed_validation_demotes_to_rejected():
    n = 300
    split_idx = int(n * hg.OOS_SPLIT_FRACTION)

    def y_fn(i, c, s):
        # relationship holds in discovery, REVERSES sign in validation --
        # the discovery-fit full model should do measurably WORSE OOS.
        return (3.0 * s + 0.01 * c) if i < split_idx else (-3.0 * s + 0.01 * c)

    matrix_rows, outcome_rows = _synthetic_matrix_source(n, y_fn)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    assert (level3["oos"]["rmse_reduction_pct"]) < 0  # sanity-check the fixture itself
    candidate = _source_candidate_from_level3(level3)
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
    assert result["oos_validation"]["validation_status"] == "FAILED_HOLDOUT"
    assert result["lifecycle_status"] == "REJECTED"


def test_explicit_failed_holdout_demotes_to_rejected():
    gate_results = {
        "gate0": {"passed": True}, "gate1": {"passed": True}, "gate2": {"passed": True},
        "gate3": {"passed": True},
        "gate4": {"validation_status": "FAILED_HOLDOUT", "passed": False},
    }
    assert hg.assign_lifecycle_status(gate_results) == "REJECTED"


# =====================================================================
# 8. Successful validation -- meaningful positive OOS improvement CAN
# promote when stability criteria are satisfied
# =====================================================================

def test_successful_validation_reaches_build_request():
    matrix_rows, outcome_rows = _synthetic_matrix_source(300, lambda i, c, s: 3.0 * s + 0.01 * c)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    candidate = _source_candidate_from_level3(level3)
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
    assert result["oos_validation"]["meaningful_effect"] is True
    assert result["oos_validation"]["sign_stable_across_subsplit"] is True
    assert result["lifecycle_status"] == "BUILD_REQUEST"
    assert result["validation_status"] == "PASSED_HOLDOUT"


# =====================================================================
# NEW (v2): positive but negligible OOS improvement does NOT promote
# =====================================================================

def test_negligible_positive_oos_improvement_does_not_promote():
    """Full-validation point estimate is positive AND stable across both
    sub-windows, but below MEANINGFUL_OOS_IMPROVEMENT_PCT (5%) -- must
    be NOT_REPLICATED, never PASSED_HOLDOUT/BUILD_REQUEST."""
    matrix_rows, outcome_rows = _synthetic_matrix_source(
        300, lambda i, c, s: 0.3 * s + 0.01 * c + 1.5 * _deterministic_noise(i))
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    full_red = level3["oos"]["rmse_reduction_pct"]
    assert 0 < full_red < hg.MEANINGFUL_OOS_IMPROVEMENT_PCT  # sanity-check the fixture
    candidate = _source_candidate_from_level3(level3)
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
    assert result["oos_validation"]["meaningful_effect"] is False
    assert result["validation_status"] == "NOT_REPLICATED"
    assert result["lifecycle_status"] != "BUILD_REQUEST"
    assert result["lifecycle_status"] != "REJECTED"  # positive estimate is not evidence AGAINST it


# =====================================================================
# NEW (v2): unstable OOS improvement does NOT promote, even if the
# full-validation magnitude alone would have cleared the 5% floor
# =====================================================================

def test_unstable_oos_improvement_does_not_promote():
    n = 300
    split_idx = int(n * hg.OOS_SPLIT_FRACTION)
    validation_len = n - split_idx
    sub2_start = split_idx + validation_len // 2

    def y_fn(i, c, s):
        base = 0.5 * s + 0.01 * c + 1.0 * _deterministic_noise(i)
        if i >= sub2_start:
            base -= 0.35 * s  # weakens/reverses the relationship in JUST the second validation sub-window
        return base

    matrix_rows, outcome_rows = _synthetic_matrix_source(n, y_fn)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    full_red = level3["oos"]["rmse_reduction_pct"]
    assert full_red >= hg.MEANINGFUL_OOS_IMPROVEMENT_PCT  # magnitude alone WOULD clear the floor
    subsplit = hg.source_subsplit_stability(matrix_rows, outcome_rows, "a", 24)
    assert subsplit["sign_stable_across_subsplit"] is False  # but it does not replicate
    candidate = _source_candidate_from_level3(level3)
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
    assert result["oos_validation"]["meaningful_effect"] is True
    assert result["oos_validation"]["sign_stable_across_subsplit"] is False
    assert result["validation_status"] == "NOT_REPLICATED"
    assert result["lifecycle_status"] != "BUILD_REQUEST"


# =====================================================================
# NEW (v2): insufficient validation sample does NOT promote (honest
# INSUFFICIENT_DATA_FOR_HOLDOUT, never a forced PASSED/FAILED)
# =====================================================================

def test_insufficient_validation_sample_does_not_promote():
    n = 80  # validation half ~24 rows; each sub-half ~12, below MIN_SAMPLE_FOR_HOLDOUT_HALF (30)
    matrix_rows, outcome_rows = _synthetic_matrix_source(n, lambda i, c, s: 3.0 * s + 0.01 * c)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    assert level3["oos"]["rmse_reduction_pct"] > 0  # a positive point estimate alone...
    subsplit = hg.source_subsplit_stability(matrix_rows, outcome_rows, "a", 24)
    assert subsplit["status"] == "INSUFFICIENT_DATA_FOR_SUBSPLIT"  # ...cannot be confirmed at this sample size
    candidate = _source_candidate_from_level3(level3)
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
    assert result["validation_status"] == "INSUFFICIENT_DATA_FOR_HOLDOUT"
    assert result["lifecycle_status"] != "BUILD_REQUEST"


def test_subsplit_stability_cannot_pass_below_the_sample_floor_even_with_a_perfect_signal():
    """Boundary proof (per independent review): the two-window replication
    check must refuse to declare stability below MIN_SAMPLE_FOR_HOLDOUT_HALF
    (30) per sub-window, EVEN when the underlying relationship is a
    perfect, noiseless, textbook-strong signal that WOULD trivially pass
    at a slightly larger sample. This rules out the check silently
    "passing by construction" on a trivial/small sample -- it is a hard
    sample-size gate, not a statistic that happens to look good with too
    little data.
    """
    y_fn = lambda i, c, s: 3.0 * s + 0.01 * c  # noqa: E731 -- as strong/clean a signal as possible

    # n=192 -> validation sub-halves of exactly 29 rows each (one row
    # short of MIN_SAMPLE_FOR_HOLDOUT_HALF) -- must be refused.
    matrix_rows_29, outcome_rows_29 = _synthetic_matrix_source(192, y_fn)
    result_29 = hg.source_subsplit_stability(matrix_rows_29, outcome_rows_29, "a", 24)
    assert result_29["status"] == "INSUFFICIENT_DATA_FOR_SUBSPLIT"
    assert result_29["n_sub1"] == 29 and result_29["n_sub2"] == 29

    # n=198 -> validation sub-halves of exactly 30 rows each -- now
    # evaluable, and (with this strong a signal) stable.
    matrix_rows_30, outcome_rows_30 = _synthetic_matrix_source(198, y_fn)
    result_30 = hg.source_subsplit_stability(matrix_rows_30, outcome_rows_30, "a", 24)
    assert result_30["status"] == "OK"
    assert result_30["n_sub1"] == 30 and result_30["n_sub2"] == 30
    assert result_30["sign_stable_across_subsplit"] is True

    # And the full gate4/evaluate_candidate path reflects the same refusal:
    # a "trivially small sample with a perfect signal" is INSUFFICIENT_DATA_
    # FOR_HOLDOUT, never a manufactured PASSED_HOLDOUT/BUILD_REQUEST.
    level3_29 = sa.level3_incremental_for_source(matrix_rows_29, outcome_rows_29, "a", 24)
    candidate_29 = _source_candidate_from_level3(level3_29)
    result = hg.evaluate_candidate(candidate_29, source_matrix_rows=matrix_rows_29, source_outcome_rows=outcome_rows_29)
    assert result["validation_status"] == "INSUFFICIENT_DATA_FOR_HOLDOUT"
    assert result["lifecycle_status"] != "BUILD_REQUEST"


# =====================================================================
# 9. Correlated-source caveat
# =====================================================================

def test_correlated_source_never_claims_beyond_group():
    """v2: REDUNDANCY_UNRESOLVED now also BLOCKS BUILD_REQUEST outright
    (Gate 6) -- a candidate can have strong association, incremental
    value, and a validated OOS effect and still be capped at
    VALIDATION_READY if redundancy is unresolved."""
    matrix_rows, outcome_rows = _synthetic_matrix_source(300, lambda i, c, s: 3.0 * s + 0.01 * c)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    candidate = _source_candidate_from_level3(
        level3, redundancy_pairwise={("a", "b"): {"n": 100, "r": 0.9, "strong_redundancy": True}})
    note = hg.source_redundancy_note(candidate)
    assert note == "REDUNDANCY_UNRESOLVED"
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
    assert result["source_redundancy_note"] == "REDUNDANCY_UNRESOLVED"
    # Gates 0-5 all pass (the underlying evidence is genuinely strong) but
    # Gate 6 blocks BUILD_REQUEST specifically because of the redundancy.
    assert result["gate_results"]["gate5"]["passed"] is True
    assert result["gate_results"]["gate6"]["passed"] is False
    assert result["gate_results"]["gate6"]["blocked_by_unresolved_redundancy"] is True
    assert result["lifecycle_status"] == "VALIDATION_READY"
    assert result["lifecycle_status"] != "BUILD_REQUEST"
    with pytest.raises(ValueError):
        hg.build_build_request_candidate(result, candidate)


def test_no_redundancy_reports_not_redundant_observed_not_a_clearance():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "s", "source_key": "a", "horizon_hours": 24,
        "level2": {"status": "OK", "n": 200}, "evidence_label": "STATISTICALLY_SIGNIFICANT",
        "level3": None, "repeatability_count": 1,
        "redundancy_pairwise": {}, "redundancy_vs_composite": {"n": 100, "r": 0.1, "strong_redundancy": False},
    }
    note = hg.source_redundancy_note(candidate)
    assert note == "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED"
    # v2: this label must NEVER be readable as "independence established"
    assert "INDEPENDENT" not in note
    doc = inspect.getdoc(hg.source_redundancy_note) or ""
    assert "never" in doc.lower() or "not establish" in doc.lower() or "does not" in doc.lower()


def test_build_request_still_reachable_when_no_redundancy_signal():
    """Sanity check that Gate 6's new redundancy check is not overly
    strict: NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED (the normal case, no
    known strong partner) must still allow BUILD_REQUEST when every
    other gate passes."""
    matrix_rows, outcome_rows = _synthetic_matrix_source(300, lambda i, c, s: 3.0 * s + 0.01 * c)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    candidate = _source_candidate_from_level3(level3)
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
    assert result["source_redundancy_note"] == "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED"
    assert result["lifecycle_status"] == "BUILD_REQUEST"


# =====================================================================
# 10. Post-event evidence cannot become predictive evidence
# =====================================================================

def test_misleading_sentiment_forced_explanatory():
    candidate = {"candidate_type": "TAXONOMY_CONCENTRATION", "category": "MISLEADING_SENTIMENT",
                 "dimension": "regime", "bucket": "chop", "horizon_hours": 24}
    assert hg.evidence_type_for_candidate(candidate) == "EXPLANATORY"


def test_significance_eligibility_gate_is_a_stricter_reread_of_gate2():
    """v2: Gate 5 is documented and behaves as a STRICTER FORM of Gate 2's
    own metric, not an independent evidence layer -- it passes only for
    STATISTICALLY_SIGNIFICANT specifically, not the weaker STABLE label
    Gate 2 itself already accepts."""
    assert hg.gate5_significance_eligibility("STATISTICALLY_SIGNIFICANT")["passed"] is True
    assert hg.gate5_significance_eligibility("STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE")["passed"] is False
    assert hg.gate5_significance_eligibility("INCONCLUSIVE")["passed"] is False
    doc = inspect.getdoc(hg.gate5_significance_eligibility) or ""
    assert "not an independent evidence layer" in doc.lower() or "not independent" in doc.lower()


def test_explanatory_only_evidence_blocks_gate6_even_if_all_else_passes():
    gate_results = {
        "gate0": {"passed": True}, "gate1": {"passed": True}, "gate2": {"passed": True},
        "gate3": {"passed": True}, "gate4": {"passed": True, "validation_status": "PASSED_HOLDOUT"},
        "gate5": {"passed": True},
    }
    gate6 = hg.gate6_build_request_eligible(
        gate_results, evidence_type="EXPLANATORY", redundancy_note="NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED")
    assert gate6["passed"] is False
    assert gate6["blocked_by_explanatory_only_evidence"] is True


def test_predictive_evidence_allows_gate6_when_all_else_passes():
    gate_results = {
        "gate0": {"passed": True}, "gate1": {"passed": True}, "gate2": {"passed": True},
        "gate3": {"passed": True}, "gate4": {"passed": True, "validation_status": "PASSED_HOLDOUT"},
        "gate5": {"passed": True},
    }
    gate6 = hg.gate6_build_request_eligible(
        gate_results, evidence_type="PREDICTIVE", redundancy_note="NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED")
    assert gate6["passed"] is True


def test_non_significant_evidence_blocks_gate6_even_if_all_else_passes():
    """'Do not manufacture statistical significance': a candidate that is
    merely STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE may still be a valid
    RESEARCH_HYPOTHESIS / VALIDATION_READY candidate (Gate 2 accepts it),
    but must never reach BUILD_REQUEST on that basis alone -- Gate 5
    fails, so Gate 6's all_prior_passed is False regardless of evidence
    type or redundancy."""
    gate_results = {
        "gate0": {"passed": True}, "gate1": {"passed": True}, "gate2": {"passed": True},
        "gate3": {"passed": True}, "gate4": {"passed": True, "validation_status": "PASSED_HOLDOUT"},
        "gate5": {"passed": False},
    }
    gate6 = hg.gate6_build_request_eligible(
        gate_results, evidence_type="PREDICTIVE", redundancy_note="NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED")
    assert gate6["passed"] is False
    assert gate6["all_prior_gates_passed"] is False


def test_unresolved_redundancy_blocks_gate6_even_if_all_else_passes():
    gate_results = {
        "gate0": {"passed": True}, "gate1": {"passed": True}, "gate2": {"passed": True},
        "gate3": {"passed": True}, "gate4": {"passed": True, "validation_status": "PASSED_HOLDOUT"},
        "gate5": {"passed": True},
    }
    gate6 = hg.gate6_build_request_eligible(
        gate_results, evidence_type="PREDICTIVE", redundancy_note="REDUNDANCY_UNRESOLVED")
    assert gate6["passed"] is False
    assert gate6["blocked_by_unresolved_redundancy"] is True


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
            "gate5": {"passed": False}, "gate6": {"passed": False}}
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
    assert hg.assign_lifecycle_status(g) == "VALIDATION_READY"  # gate5/gate6 not yet passed

    g["gate5"] = {"passed": True}
    g["gate6"] = {"passed": True}
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
                     "gate3": {"passed": True}, "gate4": {"passed": False, "validation_status": "NOT_YET_TESTED"},
                     "gate5": {"passed": True}}
    gate6 = hg.gate6_build_request_eligible(
        gate_results, evidence_type="PREDICTIVE", redundancy_note="NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED")
    assert gate6["passed"] is False


# =====================================================================
# 15. No BUILD_REQUEST when validation fails
# =====================================================================

def test_no_build_request_when_validation_fails():
    n = 300
    split_idx = int(n * hg.OOS_SPLIT_FRACTION)

    def y_fn(i, c, s):
        return (3.0 * s + 0.01 * c) if i < split_idx else (-3.0 * s + 0.01 * c)

    matrix_rows, outcome_rows = _synthetic_matrix_source(n, y_fn)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    candidate = _source_candidate_from_level3(level3)
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
    assert result["lifecycle_status"] != "BUILD_REQUEST"
    with pytest.raises(ValueError):
        hg.build_build_request_candidate(result, candidate)


# =====================================================================
# NEW (v2): a significant Gate 2 result does not automatically imply
# BUILD_REQUEST -- Gate 4 (strengthened OOS check) must independently
# pass too
# =====================================================================

def test_significant_gate2_does_not_automatically_imply_build_request():
    candidate = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "source:a:24h", "source_key": "a",
        "horizon_hours": 24,
        "level2": {"status": "OK", "n": 500, "effect_size_r": 0.3, "p_corrected": 0.0001},
        "evidence_label": "STATISTICALLY_SIGNIFICANT",  # Gate 2 AND Gate 5 both pass
        "level3": None,  # but no Level 3 / OOS evidence exists at all
        "repeatability_count": 2, "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    assert hg.gate2_association(candidate)["passed"] is True
    result = hg.evaluate_candidate(candidate)
    assert result["gate_results"]["gate5"]["passed"] is True  # significance alone: True
    assert result["lifecycle_status"] != "BUILD_REQUEST"  # but Gate 4 has no evidence -> can't promote
    assert result["validation_status"] == "INSUFFICIENT_DATA_FOR_HOLDOUT"


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
    matrix_rows, outcome_rows = _synthetic_matrix_source(300, lambda i, c, s: 3.0 * s + 0.01 * c)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    candidate = _source_candidate_from_level3(level3)
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
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
        hg.gate3_incremental_value, hg.gate4_out_of_sample, hg.gate5_significance_eligibility,
        hg.gate6_build_request_eligible, hg.source_subsplit_stability,
        hg.assign_lifecycle_status, hg.assign_evidence_status, hg.evaluate_candidate,
        hg.build_build_request_candidate, hg.build_hypothesis_report,
        hg.chronological_holdout_for_taxonomy_candidate,
    ]
    for fn in functions:
        fn_src = inspect.getsource(fn)
        for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
            assert forbidden not in fn_src, f"{fn.__name__} must be read-only -- found {forbidden}"
    assert "INSERT INTO" in inspect.getsource(hg.persist_hypothesis)


# =====================================================================
# NEW (v2): Gate 3 partial correlation does not count as independent
# confirmation -- a candidate that clears Gate 3 but never gets a real
# Gate 4 evaluation cannot reach BUILD_REQUEST on Gate 3 alone
# =====================================================================

def test_gate3_alone_never_reaches_build_request():
    matrix_rows, outcome_rows = _synthetic_matrix_source(300, lambda i, c, s: 3.0 * s + 0.01 * c)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    candidate = _source_candidate_from_level3(level3)
    # Gate 3 passes (partial correlation is real and measurable)...
    assert hg.gate3_incremental_value(candidate)["passed"] is True
    # ...but evaluate WITHOUT giving Gate 4 the raw rows it needs:
    result = hg.evaluate_candidate(candidate)  # no source_matrix_rows/source_outcome_rows
    assert result["gate_results"]["gate3"]["passed"] is True
    assert result["gate_results"]["gate4"]["passed"] is False
    assert result["lifecycle_status"] != "BUILD_REQUEST"


# =====================================================================
# NEW (v2): overlapping horizons do not count as independent
# confirmations -- signal_family_summary groups by source, not by
# (source, horizon) candidate
# =====================================================================

def test_overlapping_horizons_grouped_into_one_signal_family():
    matrix_rows, outcome_rows = _synthetic_matrix_source(300, lambda i, c, s: 3.0 * s + 0.01 * c)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    hypotheses = []
    for horizon_hours in (6, 12, 24):  # same source, three "adjacent" horizons, all BUILD_REQUEST
        candidate = _source_candidate_from_level3(level3, horizon_hours=horizon_hours)
        hypotheses.append(
            hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
        )
    assert all(h["lifecycle_status"] == "BUILD_REQUEST" for h in hypotheses)
    summary = hg._signal_family_summary(hypotheses)
    assert summary["n_source_candidates"] == 3
    assert summary["n_distinct_source_families"] == 1  # NOT 3 independent confirmations
    assert summary["n_build_request_candidates"] == 3
    assert summary["n_distinct_source_families_with_a_build_request"] == 1
    assert "not independent" in summary["note"].lower()


# =====================================================================
# NEW (v2): gate funnel counts reconcile (monotonically non-increasing,
# and BUILD_REQUEST count matches lifecycle_status_counts)
# =====================================================================

def test_gate_funnel_counts_reconcile():
    matrix_rows, outcome_rows = _synthetic_matrix_source(300, lambda i, c, s: 3.0 * s + 0.01 * c)
    level3_pass = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    candidate_pass = _source_candidate_from_level3(level3_pass, source_key="a")
    hyp_pass = hg.evaluate_candidate(candidate_pass, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)

    candidate_fail = {
        "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "source:b:24h", "source_key": "b",
        "horizon_hours": 24, "level2": {"status": "INSUFFICIENT_DATA", "n": 2},
        "level3": None, "evidence_label": None, "repeatability_count": 0,
        "redundancy_pairwise": {}, "redundancy_vs_composite": None,
    }
    hyp_fail = hg.evaluate_candidate(candidate_fail)

    hypotheses = [hyp_pass, hyp_fail]
    funnel = hg._funnel_counts(hypotheses)
    assert funnel["total_candidates"] == 2
    counts = [funnel[g] for g in hg._GATE_ORDER] + [funnel["BUILD_REQUEST"]]
    assert all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1))  # monotonically non-increasing
    # hyp_fail has level2.status == INSUFFICIENT_DATA -- fails gate0 itself,
    # so only hyp_pass should count as having PASSED gate0 (a true funnel,
    # not "evaluated up to this point" which would trivially be 2/2).
    assert funnel["gate0"] == 1
    assert funnel["BUILD_REQUEST"] == 1
    assert funnel["BUILD_REQUEST"] == sum(1 for h in hypotheses if h["lifecycle_status"] == "BUILD_REQUEST")


# =====================================================================
# NEW (v2): every rejected/non-promoted candidate has an auditable
# first-failed-gate reason -- no candidate's fate is hidden
# =====================================================================

def test_every_non_build_request_candidate_has_auditable_reason():
    candidates = [
        {  # OBSERVATION only
            "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "source:x:1h", "source_key": "x",
            "horizon_hours": 1, "level2": {"status": "INSUFFICIENT_DATA", "n": 2},
            "level3": None, "evidence_label": None, "repeatability_count": 0,
            "redundancy_pairwise": {}, "redundancy_vs_composite": None,
        },
        {  # CONTRADICTED -- fails gate2
            "candidate_type": "SOURCE_INCREMENTAL_INFO", "subject": "source:y:1h", "source_key": "y",
            "horizon_hours": 1, "level2": {"status": "OK", "n": 200, "effect_size_r": 0.1, "p_corrected": 0.8},
            "evidence_label": "CONTRADICTED", "level3": None, "repeatability_count": 0,
            "redundancy_pairwise": {}, "redundancy_vs_composite": None,
        },
    ]
    for candidate in candidates:
        result = hg.evaluate_candidate(candidate)
        assert result["lifecycle_status"] != "BUILD_REQUEST"
        failed_gate, reason = hg.first_failed_gate(result["gate_results"])
        assert failed_gate is not None
        assert failed_gate == result["first_failed_gate"]
        assert reason == result["first_failed_reason"]
        assert isinstance(reason, dict) and reason.get("passed") is False


# =====================================================================
# NEW (v2): BUILD_REQUEST still requires human approval, explicitly
# =====================================================================

def test_build_request_candidate_always_requires_human_approval():
    matrix_rows, outcome_rows = _synthetic_matrix_source(300, lambda i, c, s: 3.0 * s + 0.01 * c)
    level3 = sa.level3_incremental_for_source(matrix_rows, outcome_rows, "a", 24)
    candidate = _source_candidate_from_level3(level3)
    result = hg.evaluate_candidate(candidate, source_matrix_rows=matrix_rows, source_outcome_rows=outcome_rows)
    assert result["lifecycle_status"] == "BUILD_REQUEST"
    br = hg.build_build_request_candidate(result, candidate)
    assert br["human_approval_required"] is True
    assert br["proposed_change_description"].startswith("NONE")
    assert "why_it_passed_the_promotion_gate" in br
    assert "known_limitations" in br and len(br["known_limitations"]) > 0


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
