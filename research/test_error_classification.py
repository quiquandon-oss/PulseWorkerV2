"""
Tests for research/error_classification.py (PR5d).

All tests execute against a real, in-memory SQLite database shaped like
production (predictions, history, btc_data, research_events,
research_event_evidence). No network, no D1 connection required to run
these, and no test here ever constructs or uses a production connection.

Run with: python3 -m pytest research/test_error_classification.py -v
"""
import inspect
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import error_classification as ec  # noqa: E402
import event_detector  # noqa: E402

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
    conn.execute("CREATE UNIQUE INDEX idx_research_events_fingerprint ON research_events(fingerprint)")
    conn.execute("CREATE INDEX idx_research_events_event_ts ON research_events(event_ts)")
    conn.execute("""CREATE TABLE research_event_evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
        feed_url TEXT, article_url TEXT, publisher TEXT, publication_ts INTEGER,
        collection_ts INTEGER, headline TEXT, keyword_score REAL,
        evidence_relation TEXT, content_hash TEXT
    )""")
    conn.execute("""CREATE TABLE research_analyses (
        analysis_id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_ts INTEGER NOT NULL,
        window_start_ts INTEGER NOT NULL, window_end_ts INTEGER NOT NULL,
        sample_size INTEGER NOT NULL, subject TEXT NOT NULL, metric_json TEXT NOT NULL,
        multiple_testing_correction TEXT, validation_status TEXT NOT NULL DEFAULT 'observation'
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


def insert_price(conn, ts, price):
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))


def insert_flat_daily_prices(conn, start_ts, n_days, price=100.0):
    for i in range(n_days):
        insert_price(conn, start_ts + i * DAY, price)


# =====================================================================
# Bounded query windows
# =====================================================================

def test_fetch_requires_bounds_no_default():
    sig = inspect.signature(ec.fetch_resolved_predictions_with_v1_context)
    for name in ("start_ts", "end_ts"):
        assert sig.parameters[name].default is inspect.Parameter.empty


def test_fetch_missing_bounds_raises():
    conn = fresh_db()
    with pytest.raises(TypeError):
        ec.fetch_resolved_predictions_with_v1_context(conn, horizon_hours=24)
    with pytest.raises(ValueError):
        ec.fetch_resolved_predictions_with_v1_context(conn, horizon_hours=24, start_ts=None, end_ts=1000)
    conn.close()


def test_fetch_window_too_wide_rejected():
    conn = fresh_db()
    with pytest.raises(ValueError):
        ec.fetch_resolved_predictions_with_v1_context(conn, 24, 0, ec.MAX_WINDOW_MS + 1)
    conn.close()


def test_fetch_unsupported_horizon_rejected():
    conn = fresh_db()
    with pytest.raises(ValueError):
        ec.fetch_resolved_predictions_with_v1_context(conn, 6, 0, 1000)
    conn.close()


def test_explain_query_plan_uses_indexes_not_scans():
    conn = fresh_db()
    for i in range(10):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.6, 1, 2.0)
        insert_history(conn, i * HOUR, 50)
    plan_rows = ec.explain_base_query_plan(conn, 24, 0, 10 * HOUR)
    plan_text = " ".join(str(r) for r in plan_rows).upper()
    assert "USING INDEX" in plan_text
    assert "SCAN PREDICTIONS" not in plan_text.replace("SEARCH", "")
    conn.close()


# =====================================================================
# Missing V1 / history
# =====================================================================

def test_missing_v1_history_yields_insufficient_information_when_wrong():
    conn = fresh_db()
    insert_prediction(conn, 1000, 2000, 0.7, 0, -1.0)  # wrong direction, no history at all
    rows = ec.fetch_resolved_predictions_with_v1_context(conn, 24, 0, 5000)
    result = ec.classify_prediction(rows[0], {}, None, None)
    assert result["error_type"] == "INSUFFICIENT_INFORMATION"
    conn.close()


def test_missing_v1_history_no_error_when_direction_correct():
    conn = fresh_db()
    insert_prediction(conn, 1000, 2000, 0.7, 1, 1.0)  # correct direction, no history
    rows = ec.fetch_resolved_predictions_with_v1_context(conn, 24, 0, 5000)
    result = ec.classify_prediction(rows[0], {}, None, None)
    assert result["error_type"] is None
    conn.close()


# =====================================================================
# Missing event evidence (PR4, optional)
# =====================================================================

def test_find_evidence_for_event_no_match_returns_no_evidence_found():
    conn = fresh_db()
    result = ec.find_evidence_for_event(conn, "LARGE_MOVE", 1000)
    assert result["status"] == "NO_EVIDENCE_FOUND"
    conn.close()


def test_find_evidence_for_event_matched_event_no_evidence_rows():
    conn = fresh_db()
    conn.execute(
        "INSERT INTO research_events (fingerprint, event_ts, detection_ts, category, "
        "available_before_prediction) VALUES ('fp1', 1000, 2000, 'LARGE_MOVE', 1)"
    )
    result = ec.find_evidence_for_event(conn, "LARGE_MOVE", 1000)
    assert result["status"] == "NO_EVIDENCE_FOUND"
    assert result["reason"] == "event_persisted_but_no_evidence_rows"
    conn.close()


def test_find_evidence_for_event_with_evidence_returns_it():
    conn = fresh_db()
    conn.execute(
        "INSERT INTO research_events (fingerprint, event_ts, detection_ts, category, "
        "available_before_prediction) VALUES ('fp1', 1000, 2000, 'LARGE_MOVE', 1)"
    )
    event_id = conn.execute("SELECT event_id FROM research_events").fetchone()[0]
    conn.execute(
        "INSERT INTO research_event_evidence (event_id, article_url, publisher, publication_ts, "
        "headline, evidence_relation) VALUES (?, 'http://x', 'pub', 900, 'headline', 'PRE_EVENT')",
        (event_id,),
    )
    result = ec.find_evidence_for_event(conn, "LARGE_MOVE", 1000)
    assert result["status"] == "OK"
    assert len(result["evidence"]) == 1
    conn.close()


# =====================================================================
# Regime boundaries (reuses PR3's own frozen extremes)
# =====================================================================

def test_v1_composite_bucket_uses_frozen_extremes():
    assert ec.v1_composite_bucket(ec.FROZEN_V1_BEARISH_EXTREME - 1) == "BEARISH"
    assert ec.v1_composite_bucket(ec.FROZEN_V1_BEARISH_EXTREME) == "NEUTRAL"
    assert ec.v1_composite_bucket(ec.FROZEN_V1_BULLISH_EXTREME) == "NEUTRAL"
    assert ec.v1_composite_bucket(ec.FROZEN_V1_BULLISH_EXTREME + 1) == "BULLISH"
    assert ec.v1_composite_bucket(50) == "NEUTRAL"


def test_v1_composite_bucket_none_when_missing():
    assert ec.v1_composite_bucket(None) is None


def test_v1_bucket_extremes_match_event_detector_verbatim():
    # NOT duplicated as a second literal -- imported directly.
    assert ec.FROZEN_V1_BEARISH_EXTREME == event_detector.FROZEN_V1_BEARISH_EXTREME
    assert ec.FROZEN_V1_BULLISH_EXTREME == event_detector.FROZEN_V1_BULLISH_EXTREME


# =====================================================================
# Wrong-direction classification
# =====================================================================

def test_wrong_direction_default_when_no_more_specific_cause():
    row = {"prediction_ts": 1000, "target_ts": 2000, "horizon_hours": 24, "model_version": "m",
           "p_up": 0.7, "realized_up": 0, "realized_return": -1.0, "v1_observation_ts": 500,
           "v1_composite": 55, "technical_score": 55, "gold_regime": None}
    result = ec.classify_prediction(row, {}, staleness_threshold_ms=10_000_000, magnitude_threshold_pct=5.0)
    assert result["error_type"] == "WRONG_DIRECTION"
    assert result["observed_fact"]["direction_correct"] is False


def test_wrong_direction_p_up_exactly_half_treated_as_down_prediction():
    row = {"prediction_ts": 1000, "target_ts": 2000, "horizon_hours": 24, "model_version": "m",
           "p_up": 0.5, "realized_up": 1, "realized_return": 1.0, "v1_observation_ts": 500,
           "v1_composite": 50, "technical_score": 50, "gold_regime": None}
    result = ec.classify_prediction(row, {}, staleness_threshold_ms=10_000_000, magnitude_threshold_pct=5.0)
    assert result["observed_fact"]["direction_correct"] is False
    assert result["error_type"] == "WRONG_DIRECTION"


# =====================================================================
# Correct-direction/wrong-magnitude classification
# =====================================================================

def test_correct_direction_wrong_magnitude_at_or_above_threshold():
    row = {"prediction_ts": 1000, "target_ts": 2000, "horizon_hours": 24, "model_version": "m",
           "p_up": 0.7, "realized_up": 1, "realized_return": 6.0, "v1_observation_ts": 500,
           "v1_composite": 55, "technical_score": 55, "gold_regime": None}
    result = ec.classify_prediction(row, {}, staleness_threshold_ms=10_000_000, magnitude_threshold_pct=5.0)
    assert result["error_type"] == "CORRECT_DIRECTION_WRONG_MAGNITUDE"


def test_correct_direction_close_below_threshold_is_no_error():
    row = {"prediction_ts": 1000, "target_ts": 2000, "horizon_hours": 24, "model_version": "m",
           "p_up": 0.7, "realized_up": 1, "realized_return": 1.0, "v1_observation_ts": 500,
           "v1_composite": 55, "technical_score": 55, "gold_regime": None}
    result = ec.classify_prediction(row, {}, staleness_threshold_ms=10_000_000, magnitude_threshold_pct=5.0)
    assert result["error_type"] is None


def test_magnitude_threshold_never_hardcoded_in_source():
    src = inspect.getsource(ec)
    # the classification function itself must not contain a bare
    # percent-looking magic number used as a magnitude cut -- it must
    # receive magnitude_threshold_pct as a parameter, always computed by
    # empirical_magnitude_threshold().
    fn_src = inspect.getsource(ec.classify_prediction)
    assert "magnitude_threshold_pct" in fn_src
    assert "4.0" not in fn_src and "5.0" not in fn_src


def test_empirical_magnitude_threshold_is_proposed_not_frozen():
    rows = [
        {"p_up": 0.9, "realized_up": 1, "realized_return": r}
        for r in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    ]
    result = ec.empirical_magnitude_threshold(rows)
    assert result["status"] == "PROPOSED"
    assert result["n"] == 8
    assert result["proposed_threshold_pct"] == result["p50_pct"]


def test_empirical_magnitude_threshold_insufficient_data():
    result = ec.empirical_magnitude_threshold([])
    assert result["status"] == "INSUFFICIENT_DATA"


# =====================================================================
# Insufficient-information fallback
# =====================================================================

def test_insufficient_information_never_used_when_cause_is_findable():
    row = {"prediction_ts": 1000, "target_ts": 2000, "horizon_hours": 24, "model_version": "m",
           "p_up": 0.7, "realized_up": 0, "realized_return": -6.0, "v1_observation_ts": 500,
           "v1_composite": 55, "technical_score": 55, "gold_regime": None}
    events = {"LARGE_MOVE": [{"event_ts": 1500, "direction": "DOWN", "intensity": 6.0}]}
    result = ec.classify_prediction(row, events, staleness_threshold_ms=10_000_000, magnitude_threshold_pct=5.0)
    assert result["error_type"] == "UNEXPECTED_SHOCK"
    assert result["error_type"] != "INSUFFICIENT_INFORMATION"


# =====================================================================
# No-lookahead / prediction-event ordering (constructive proof)
# =====================================================================

def test_event_at_or_before_prediction_ts_excluded_from_window():
    events = [{"event_ts": 999, "direction": "UP"}, {"event_ts": 1000, "direction": "UP"},
              {"event_ts": 1001, "direction": "UP"}]
    overlapping = ec._events_overlapping_window(events, window_start_ts=1000, window_end_ts=2000)
    assert len(overlapping) == 1
    assert overlapping[0]["event_ts"] == 1001


def test_event_at_exactly_target_ts_included():
    events = [{"event_ts": 2000, "direction": "UP"}]
    overlapping = ec._events_overlapping_window(events, window_start_ts=1000, window_end_ts=2000)
    assert len(overlapping) == 1


def test_no_lookahead_event_before_prediction_never_used():
    # A LARGE_MOVE event strictly BEFORE prediction_ts must never
    # trigger UNEXPECTED_SHOCK for that prediction, even though it's in
    # the same events_by_category pool (constructive proof, not just a
    # comment).
    row = {"prediction_ts": 1000, "target_ts": 2000, "horizon_hours": 24, "model_version": "m",
           "p_up": 0.7, "realized_up": 0, "realized_return": -6.0, "v1_observation_ts": 500,
           "v1_composite": 55, "technical_score": 55, "gold_regime": None}
    events = {"LARGE_MOVE": [{"event_ts": 999, "direction": "DOWN", "intensity": 6.0}]}  # BEFORE prediction_ts
    result = ec.classify_prediction(row, events, staleness_threshold_ms=10_000_000, magnitude_threshold_pct=5.0)
    assert result["error_type"] != "UNEXPECTED_SHOCK"
    assert result["error_type"] == "WRONG_DIRECTION"


def test_v1_btc_divergence_always_used_as_post_outcome_only():
    row = {"prediction_ts": 1000, "target_ts": 2000, "horizon_hours": 24, "model_version": "m",
           "p_up": 0.7, "realized_up": 0, "realized_return": -1.0, "v1_observation_ts": 500,
           "v1_composite": 55, "technical_score": 55, "gold_regime": None}
    events = {"V1_BTC_DIVERGENCE": [{"event_ts": 1500, "direction": "v1_bullish_btc_down",
                                      "intensity": 5.0, "is_post_event_analysis": 1}]}
    result = ec.classify_prediction(row, events, staleness_threshold_ms=10_000_000, magnitude_threshold_pct=5.0)
    assert result["error_type"] == "MISLEADING_SENTIMENT"
    assert "post-outcome" in result["contributing_signals"]["note"].lower()


# =====================================================================
# Deterministic rerun
# =====================================================================

def test_deterministic_rerun_identical_report():
    conn = fresh_db()
    for i in range(60):
        insert_prediction(conn, i * DAY, i * DAY + 24 * HOUR, 0.5 + (i % 5) * 0.08,
                           1 if i % 3 == 0 else 0, ((-1) ** i) * (i % 4) * 0.5,
                           horizon_hours=24, model_version="m1" if i % 2 == 0 else "m2")
        insert_history(conn, i * DAY, 40 + (i % 30), 40 + (i % 25),
                        "regimeA" if i % 2 == 0 else "regimeB")
        insert_price(conn, i * DAY, 100.0 + i)
    report_1 = ec.build_error_classification_report(conn, 0, 60 * DAY, horizons=(24,))
    report_2 = ec.build_error_classification_report(conn, 0, 60 * DAY, horizons=(24,))
    assert report_1 == report_2
    conn.close()


# =====================================================================
# Empty / small samples
# =====================================================================

def test_empty_window_produces_empty_report_not_error():
    conn = fresh_db()
    report = ec.build_error_classification_report(conn, 0, DAY, horizons=(24,))
    assert report["per_horizon"][24]["n_predictions"] == 0
    assert report["per_horizon"][24]["overall"]["n_resolved"] == 0
    conn.close()


def test_small_sample_flagged_not_hidden():
    conn = fresh_db()
    insert_prediction(conn, 1000, 2000, 0.7, 0, -1.0, horizon_hours=24)
    insert_history(conn, 500, 40)
    report = ec.build_error_classification_report(conn, 0, 5000, horizons=(24,))
    assert report["per_horizon"][24]["overall"]["sample_size_status"] == "SMALL_SAMPLE_CAUTION"
    conn.close()


def test_transitions_insufficient_data_below_min_sample():
    classifications = [{"status": "RESOLVED", "error_type": "WRONG_DIRECTION"} for _ in range(5)]
    result = ec.error_transition_report(classifications)
    assert result["status"] == "INSUFFICIENT_DATA"


# =====================================================================
# Multi-horizon isolation
# =====================================================================

def test_multi_horizon_isolation():
    conn = fresh_db()
    # horizon 12: all wrong direction
    for i in range(40):
        insert_prediction(conn, i * HOUR, i * HOUR + 12 * HOUR, 0.7, 0, -1.0, horizon_hours=12)
        insert_history(conn, i * HOUR, 55)
    # horizon 24: all correct, close
    for i in range(40):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.7, 1, 1.0, horizon_hours=24)
        insert_history(conn, i * HOUR + 1, 55)
    report = ec.build_error_classification_report(conn, 0, 40 * HOUR, horizons=(12, 24))
    h12 = report["per_horizon"][12]["overall"]
    h24 = report["per_horizon"][24]["overall"]
    # horizon 12 is entirely direction-wrong predictions -- must show up
    # there and NOWHERE in horizon 24's own breakdown (the actual point
    # of this test: no cross-horizon contamination).
    assert h12["error_counts"]["WRONG_DIRECTION"] == 40
    assert h12["n_resolved"] == 40
    assert h24["n_resolved"] == 40
    assert h24["error_counts"]["WRONG_DIRECTION"] == 0
    conn.close()


def test_fetch_never_mixes_horizons():
    conn = fresh_db()
    insert_prediction(conn, 1000, 2000, 0.7, 1, 1.0, horizon_hours=12)
    insert_prediction(conn, 1000, 2000, 0.3, 1, 1.0, horizon_hours=24)
    rows_12 = ec.fetch_resolved_predictions_with_v1_context(conn, 12, 0, 5000)
    rows_24 = ec.fetch_resolved_predictions_with_v1_context(conn, 24, 0, 5000)
    assert len(rows_12) == 1 and rows_12[0]["p_up"] == 0.7
    assert len(rows_24) == 1 and rows_24[0]["p_up"] == 0.3
    conn.close()


# =====================================================================
# Model-version isolation
# =====================================================================

def test_model_version_breakdown_isolates_versions():
    conn = fresh_db()
    for i in range(35):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.7, 0, -1.0,
                           horizon_hours=24, model_version="legacy")
        insert_history(conn, i * HOUR, 55)
    for i in range(35, 70):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.7, 1, 1.0,
                           horizon_hours=24, model_version="knn-core-v1")
        insert_history(conn, i * HOUR, 55)
    rows = ec.fetch_resolved_predictions_with_v1_context(conn, 24, 0, 70 * HOUR)
    events = ec.fetch_events_for_window(conn, 0, 70 * HOUR)
    classifications = [ec.classify_prediction(r, events, None, 5.0) for r in rows]
    breakdown = ec.breakdown_by_model_version(rows, classifications)
    assert breakdown["legacy"]["error_counts"]["WRONG_DIRECTION"] == 35
    assert breakdown["legacy"]["n_resolved"] == 35
    assert breakdown["knn-core-v1"]["n_resolved"] == 35
    # the point of this test: knn-core-v1's rows are all direction-
    # correct, so none of them may be attributed WRONG_DIRECTION just
    # because legacy (a different model_version) had 35 of them.
    assert breakdown["knn-core-v1"]["error_counts"]["WRONG_DIRECTION"] == 0
    conn.close()


# =====================================================================
# No production writes
# =====================================================================

def test_only_persist_findings_contains_insert_into():
    functions = [
        ec.fetch_resolved_predictions_with_v1_context, ec.fetch_events_for_window,
        ec.v2_failure_cluster_context, ec.classify_prediction, ec.classify_all,
        ec.breakdown_by_v1_bucket, ec.breakdown_by_regime, ec.breakdown_by_model_version,
        ec.breakdown_by_confidence_band, ec.breakdown_by_chronological_period,
        ec.error_transition_report, ec.derive_candidate_observations,
        ec.build_error_classification_report,
    ]
    for fn in functions:
        fn_src = inspect.getsource(fn)
        for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
            assert forbidden not in fn_src, f"{fn.__name__} must be read-only -- found {forbidden}"
    # find_evidence_for_event legitimately runs SELECTs only
    assert "INSERT INTO" not in inspect.getsource(ec.find_evidence_for_event)
    assert "INSERT INTO" in inspect.getsource(ec.persist_findings)


def test_no_network_or_llm_calls_anywhere_in_module():
    src = inspect.getsource(ec)
    for forbidden in ["requests.", "urllib", "fetch(", "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_persist_findings_writes_only_to_in_memory_test_db():
    conn = fresh_db()
    analysis_id = ec.persist_findings(
        conn, analysis_ts=1000, window_start_ts=0, window_end_ts=1000, sample_size=10,
        subject="error_classification:BTC:24h", metric_json_obj={"n_resolved": 10},
    )
    row = conn.execute("SELECT * FROM research_analyses WHERE analysis_id = ?", (analysis_id,)).fetchone()
    assert row is not None
    conn.close()


def test_persist_findings_never_exceeds_research_hypothesis_gate_by_default():
    conn = fresh_db()
    ec.persist_findings(conn, 1000, 0, 1000, 10, "subj", {}, validation_status="OBSERVATION")
    row = conn.execute("SELECT validation_status FROM research_analyses").fetchone()
    assert row[0] in ("OBSERVATION", "RESEARCH_HYPOTHESIS")
    conn.close()


def test_derive_candidate_observations_never_assigns_beyond_research_hypothesis():
    conn = fresh_db()
    for i in range(40):
        insert_prediction(conn, i * HOUR, i * HOUR + 24 * HOUR, 0.7, 0, -1.0, horizon_hours=24)
        insert_history(conn, i * HOUR, 55)
    rows = ec.fetch_resolved_predictions_with_v1_context(conn, 24, 0, 40 * HOUR)
    events = ec.fetch_events_for_window(conn, 0, 40 * HOUR)
    classifications = [ec.classify_prediction(r, events, None, 5.0) for r in rows]
    cuts = ec.confidence_band_cuts(rows)
    candidates = ec.derive_candidate_observations(rows, classifications, 0, cuts)
    for c in candidates:
        assert c["evidence_gate_status"] in ("OBSERVATION", "RESEARCH_HYPOTHESIS")
    conn.close()


# =====================================================================
# Deterministic classification-rule version stamped on every result
# =====================================================================

def test_every_classification_carries_rule_version():
    row = {"prediction_ts": 1000, "target_ts": 2000, "horizon_hours": 24, "model_version": "m",
           "p_up": 0.7, "realized_up": 0, "realized_return": -1.0, "v1_observation_ts": 500,
           "v1_composite": 55, "technical_score": 55, "gold_regime": None}
    result = ec.classify_prediction(row, {}, 10_000_000, 5.0)
    assert result["classification_rule_version"] == ec.CLASSIFICATION_RULE_VERSION


def test_unresolved_prediction_excluded_from_error_stats():
    row = {"prediction_ts": 1000, "target_ts": 2000, "horizon_hours": 24, "model_version": "m",
           "p_up": 0.7, "realized_up": None, "realized_return": None, "v1_observation_ts": 500,
           "v1_composite": 55, "technical_score": 55, "gold_regime": None}
    result = ec.classify_prediction(row, {}, 10_000_000, 5.0)
    assert result["status"] == "UNRESOLVED"
    assert result["error_type"] is None
