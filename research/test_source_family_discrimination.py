"""
Tests for research/source_family_discrimination.py (PR5f).

All tests execute against real (non-mocked) synthetic fixtures built
directly in the shape source_analysis.extract_source_matrix() /
outcome_engine.compute_forward_returns_from_history() already produce,
or against a real in-memory sqlite3 database mirroring production for
the full end-to-end orchestration test. No network, no D1 connection.

Run with: python3 -m pytest research/test_source_family_discrimination.py -v
"""
import inspect
import json
import sqlite3
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import source_family_discrimination as sfd  # noqa: E402
import hypothesis_gate as hg  # noqa: E402
import source_analysis as sa  # noqa: E402

HOUR = 3600000


def _noise(i):
    """Deterministic pseudo-noise in [-6, 6] -- no randomness."""
    return ((i * 37) % 13) - 6


def _noise2(i):
    return ((i * 53) % 11) - 5


def _build_matrix(n, source_fn, y_fn):
    matrix_rows, outcome_rows = [], []
    for i in range(n):
        ts = i * HOUR
        composite = 50 + (i % 7)
        sources = source_fn(i)
        y = y_fn(i, composite, sources)
        matrix_rows.append({"ts": ts, "v1_composite": float(composite), "gold_regime": None,
                             "sources": {k: float(v) for k, v in sources.items()}})
        outcome_rows.append({"anchor_ts": ts, "outcome_status": "RESOLVED", "forward_return_pct": y})
    return matrix_rows, outcome_rows


_FAKE_ORIGINAL = {"evidence_status": "STATISTICALLY_SIGNIFICANT",
                   "lifecycle_status": "BUILD_REQUEST", "validation_status": "PASSED_HOLDOUT"}


def _pw_lookup(pw, a, b):
    """pairwise_family_correlations() keys are (a, b) tuples in the same
    order sa.pairwise_source_redundancy() produces them (source_keys[i],
    source_keys[i+1:]) -- look up either order."""
    return pw.get((a, b), pw.get((b, a)))


# =====================================================================
# 1. Correlated-source case: a redundant echo is demoted, the real
# driver and an independent signal both survive -- the core new claim
# this PR makes over PR5e's own composite-only test
# =====================================================================

def test_redundant_echo_demoted_real_driver_and_independent_signal_survive():
    n = 400

    def source_fn(i):
        a = (i % 11) - 5
        b = a + 0.5 * _noise2(i)  # B is a NOISY ECHO of A -- realistic, not exact collinearity
        return {"A": a, "B": b, "C": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["A"] + 2.5 * s["C"] + 0.01 * c + 0.3 * _noise(i)

    matrix_rows, outcome_rows = _build_matrix(n, source_fn, y_fn)
    outcome_rows_by_horizon = {24: outcome_rows}

    pw = sfd.pairwise_family_correlations(matrix_rows, ["A", "B", "C"])
    assert abs(_pw_lookup(pw, "A", "B")["r"]) > 0.8  # genuinely redundant pair
    assert abs(_pw_lookup(pw, "A", "C")["r"]) < 0.1  # C genuinely independent

    result_a = sfd.evaluate_family(matrix_rows, outcome_rows_by_horizon, "A", ["B", "C"], 24, _FAKE_ORIGINAL)
    result_b = sfd.evaluate_family(matrix_rows, outcome_rows_by_horizon, "B", ["A", "C"], 24, _FAKE_ORIGINAL)
    result_c = sfd.evaluate_family(matrix_rows, outcome_rows_by_horizon, "C", ["A", "B"], 24, _FAKE_ORIGINAL)

    # A is the real driver of the outcome -- still adds information even
    # with its noisy echo B already in the baseline.
    assert result_a["discrimination_status"] == "DISCRIMINATED_INCREMENTAL"
    # B is JUST a noisy copy of A -- once A is already in the baseline,
    # B adds nothing: this is the exact case PR5e's composite-only test
    # could not catch (B could well have reached PR5e's own BUILD_REQUEST
    # on its correlation with the outcome alone).
    assert result_b["discrimination_status"] == "NOT_DISCRIMINATED"
    # C is independent of both A and B -- correctly confirmed regardless.
    assert result_c["discrimination_status"] == "DISCRIMINATED_INCREMENTAL"


# =====================================================================
# 2. Negative OOS case
# =====================================================================

def test_negative_oos_point_estimate_is_failed_holdout_not_discriminated():
    n = 300
    split_idx = int(n * sfd.OOS_SPLIT_FRACTION)

    def source_fn(i):
        return {"A": (i % 11) - 5, "B": (i % 13) - 6}

    def y_fn(i, c, s):
        # relationship with A holds in discovery, REVERSES in validation
        base = 0.01 * c + 0.5 * s["B"]
        return base + (3.0 * s["A"] if i < split_idx else -3.0 * s["A"])

    matrix_rows, outcome_rows = _build_matrix(n, source_fn, y_fn)
    result = sfd.evaluate_family(matrix_rows, {24: outcome_rows}, "A", ["B"], 24, _FAKE_ORIGINAL)
    assert result["oos"]["rmse_reduction_pct"] < 0
    assert result["validation_status"] == "FAILED_HOLDOUT"
    assert result["discrimination_status"] == "NOT_DISCRIMINATED"


# =====================================================================
# 3. Insufficient-data cases
# =====================================================================

def test_insufficient_total_sample_is_insufficient_data():
    n = 20  # below MIN_SAMPLE_FOR_LEVEL3 (40)

    def source_fn(i):
        return {"A": (i % 5) - 2, "B": (i % 3) - 1}

    def y_fn(i, c, s):
        return 3.0 * s["A"] + 0.01 * c

    matrix_rows, outcome_rows = _build_matrix(n, source_fn, y_fn)
    result = sfd.evaluate_family(matrix_rows, {24: outcome_rows}, "A", ["B"], 24, _FAKE_ORIGINAL)
    assert result["discrimination_status"] == "INSUFFICIENT_DATA"
    assert result["validation_status"] == "INSUFFICIENT_DATA_FOR_HOLDOUT"


def test_insufficient_validation_subsplit_sample_is_insufficient_data():
    n = 80  # enough for the overall model, not enough for 30-row sub-halves

    def source_fn(i):
        return {"A": (i % 11) - 5, "B": (i % 13) - 6}

    def y_fn(i, c, s):
        return 3.0 * s["A"] + 0.01 * c + 0.5 * s["B"]

    matrix_rows, outcome_rows = _build_matrix(n, source_fn, y_fn)
    result = sfd.evaluate_family(matrix_rows, {24: outcome_rows}, "A", ["B"], 24, _FAKE_ORIGINAL)
    assert result["oos"]["rmse_reduction_pct"] > 0  # positive point estimate...
    assert result["subsplit"]["status"] == "INSUFFICIENT_DATA_FOR_SUBSPLIT"  # ...cannot be confirmed
    assert result["validation_status"] == "INSUFFICIENT_DATA_FOR_HOLDOUT"
    assert result["discrimination_status"] == "INSUFFICIENT_DATA"


# =====================================================================
# 4. Higher-order partial correlation correctness (exact, not an
# approximation -- verified against the multiple-regression-residual
# method to machine precision)
# =====================================================================

def test_higher_order_partial_correlation_matches_regression_residual_method():
    import random
    from stats_utils import ols_nvar, pearson_correlation

    random.seed(42)
    n = 500
    z1 = [random.gauss(0, 1) for _ in range(n)]
    z2 = [random.gauss(0, 1) for _ in range(n)]
    x = [0.5 * z1[i] + 0.3 * z2[i] + random.gauss(0, 1) for i in range(n)]
    y = [0.4 * z1[i] - 0.2 * z2[i] + 0.6 * x[i] + random.gauss(0, 1) for i in range(n)]

    b0x, coefsx = ols_nvar([z1, z2], x)
    b0y, coefsy = ols_nvar([z1, z2], y)
    resid_x = [x[i] - (b0x + coefsx[0] * z1[i] + coefsx[1] * z2[i]) for i in range(n)]
    resid_y = [y[i] - (b0y + coefsy[0] * z1[i] + coefsy[1] * z2[i]) for i in range(n)]
    reference = pearson_correlation(resid_x, resid_y)

    result = sfd.higher_order_partial_correlation(
        {"target": x, "y": y, "z1": z1, "z2": z2}, "target", "y", ["z1", "z2"])
    assert abs(reference - result) < 1e-9


def test_higher_order_partial_correlation_reported_not_independent():
    doc = inspect.getdoc(sfd.evaluate_family) or ""
    src = inspect.getsource(sfd.evaluate_family)
    assert "note" in src  # the note field is attached to every non-trivial result
    assert "NOT independent" in inspect.getsource(sfd) or "not independent" in inspect.getsource(sfd).lower()


# =====================================================================
# 5. Horizon-overlap handling: primary horizon selection + context
# =====================================================================

def test_primary_horizon_is_the_best_evidenced_one_not_arbitrary():
    fake_report = {
        "signal_family_summary": {"build_request_horizons_by_family": {
            "fng": ["source:fng:6h", "source:fng:12h", "source:fng:24h"],
        }},
        "hypotheses": [
            {"subject": "source:fng:6h", "oos_validation": {"rmse_reduction_pct": 7.46}, "lifecycle_status": "BUILD_REQUEST"},
            {"subject": "source:fng:12h", "oos_validation": {"rmse_reduction_pct": 11.64}, "lifecycle_status": "BUILD_REQUEST"},
            {"subject": "source:fng:24h", "oos_validation": {"rmse_reduction_pct": 16.21}, "lifecycle_status": "BUILD_REQUEST"},
        ],
    }
    families = sfd.current_surviving_families(fake_report)
    primary = sfd.select_primary_horizon_per_family(fake_report, families)
    assert primary["fng"] == 24  # the highest rmse_reduction_pct, not the last/first/arbitrary one


def test_other_horizons_reported_as_context_not_independent_confirmation():
    fake_report = {
        "signal_family_summary": {"build_request_horizons_by_family": {
            "fng": ["source:fng:6h", "source:fng:12h", "source:fng:24h"],
        }},
        "hypotheses": [
            {"subject": "source:fng:6h", "oos_validation": {"rmse_reduction_pct": 7.46}, "lifecycle_status": "BUILD_REQUEST"},
            {"subject": "source:fng:12h", "oos_validation": {"rmse_reduction_pct": 11.64}, "lifecycle_status": "BUILD_REQUEST"},
            {"subject": "source:fng:24h", "oos_validation": {"rmse_reduction_pct": 16.21}, "lifecycle_status": "BUILD_REQUEST"},
        ],
    }
    families = sfd.current_surviving_families(fake_report)
    primary = sfd.select_primary_horizon_per_family(fake_report, families)
    context = sfd.other_horizon_context(fake_report, families, primary)
    assert len(context["fng"]["horizons"]) == 2  # 6h and 12h, not the primary 24h
    assert {h["horizon_hours"] for h in context["fng"]["horizons"]} == {6, 12}
    assert "NOT independent confirmations" in context["fng"]["caveat"]


def test_candidate_count_never_conflated_with_family_count_in_report():
    # signal_family_summary already distinguishes these in hypothesis_gate;
    # this PR must preserve that distinction, never re-collapse it.
    fake_report = {
        "signal_family_summary": {"build_request_horizons_by_family": {
            "fng": ["source:fng:6h", "source:fng:12h", "source:fng:24h"],
            "onchain": ["source:onchain:24h"],
        }},
        "hypotheses": [
            {"subject": "source:fng:6h", "oos_validation": {"rmse_reduction_pct": 7.46}, "lifecycle_status": "BUILD_REQUEST"},
            {"subject": "source:fng:12h", "oos_validation": {"rmse_reduction_pct": 11.64}, "lifecycle_status": "BUILD_REQUEST"},
            {"subject": "source:fng:24h", "oos_validation": {"rmse_reduction_pct": 16.21}, "lifecycle_status": "BUILD_REQUEST"},
            {"subject": "source:onchain:24h", "oos_validation": {"rmse_reduction_pct": 13.63}, "lifecycle_status": "BUILD_REQUEST"},
        ],
    }
    families = sfd.current_surviving_families(fake_report)
    assert len(families) == 2  # 2 FAMILIES
    assert sum(len(v) for v in families.values()) == 4  # 4 CANDIDATES -- explicitly distinct counts


# =====================================================================
# 6. Redundancy is reported, never gated
# =====================================================================

def test_redundancy_reported_not_gated():
    n = 400

    def source_fn(i):
        a = (i % 11) - 5
        return {"A": a, "B": a + 0.1 * _noise2(i), "C": (i % 9) - 4}  # A/B strongly correlated

    def y_fn(i, c, s):
        return 3.0 * s["A"] + 2.5 * s["C"] + 0.01 * c + 0.3 * _noise(i)

    matrix_rows, outcome_rows = _build_matrix(n, source_fn, y_fn)
    pw = sfd.pairwise_family_correlations(matrix_rows, ["A", "B", "C"])
    # the strong correlation is REPORTED (a real number)...
    assert abs(_pw_lookup(pw, "A", "B")["r"]) > 0.9
    # ...never used to block/exclude a family from evaluation:
    result_a = sfd.evaluate_family(matrix_rows, {24: outcome_rows}, "A", ["B", "C"], 24, _FAKE_ORIGINAL)
    assert result_a["discrimination_status"] in sfd.DISCRIMINATION_STATUSES  # evaluated, not skipped


# =====================================================================
# 7. Lookahead safety
# =====================================================================

def test_no_shuffle_anywhere_in_module():
    src = inspect.getsource(sfd)
    assert ".shuffle(" not in src
    assert "import random" not in src
    assert "random." not in src


def test_build_combined_rows_sorted_chronologically():
    n = 50

    def source_fn(i):
        return {"A": i % 5}

    def y_fn(i, c, s):
        return s["A"]

    matrix_rows, outcome_rows = _build_matrix(n, source_fn, y_fn)
    # shuffle the INPUT order (not the data) to prove the function sorts, not just passes through
    import copy
    shuffled_matrix = list(reversed(matrix_rows))
    combined = sfd.build_combined_rows(shuffled_matrix, outcome_rows, "A", ["v1_composite"])
    timestamps = [c[0] for c in combined]
    assert timestamps == sorted(timestamps)


def test_later_source_value_never_affects_earlier_row_pairing():
    # each row's outcome is paired only with that row's OWN ts via
    # anchor_ts -- changing a LATER row's target value must not change
    # an EARLIER row's (target, control, y) tuple.
    n = 60

    def source_fn(i):
        return {"A": i, "B": i % 3}

    def y_fn(i, c, s):
        return s["A"] * 0.1

    matrix_rows, outcome_rows = _build_matrix(n, source_fn, y_fn)
    combined_before = sfd.build_combined_rows(matrix_rows, outcome_rows, "A", ["v1_composite", "B"])

    matrix_rows[-1]["sources"]["A"] = 999999.0  # mutate only the LAST row's target value
    combined_after = sfd.build_combined_rows(matrix_rows, outcome_rows, "A", ["v1_composite", "B"])

    for before, after in zip(combined_before[:-1], combined_after[:-1]):
        assert before == after  # every earlier row unaffected


# =====================================================================
# 8. Deterministic reruns
# =====================================================================

def test_evaluate_family_deterministic_rerun():
    n = 300

    def source_fn(i):
        return {"A": (i % 11) - 5, "B": (i % 13) - 6}

    def y_fn(i, c, s):
        return 3.0 * s["A"] + 0.01 * c + 0.5 * s["B"] + 0.3 * _noise(i)

    matrix_rows, outcome_rows = _build_matrix(n, source_fn, y_fn)
    outcome_rows_by_horizon = {24: outcome_rows}
    result_1 = sfd.evaluate_family(matrix_rows, outcome_rows_by_horizon, "A", ["B"], 24, _FAKE_ORIGINAL)
    result_2 = sfd.evaluate_family(matrix_rows, outcome_rows_by_horizon, "A", ["B"], 24, _FAKE_ORIGINAL)
    assert result_1 == result_2


# =====================================================================
# 9. No production write anywhere in this module
# =====================================================================

def test_no_writes_anywhere_in_module():
    src = inspect.getsource(sfd)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in src
    assert "def persist" not in src  # this module has NO persistence function at all


def test_no_network_or_llm_calls_anywhere_in_module():
    src = inspect.getsource(sfd)
    for forbidden in ["import requests", "requests.get(", "requests.post(", "urllib", "fetch(",
                       "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_never_modifies_or_reimports_v1_v2_worker():
    src = inspect.getsource(sfd)
    for forbidden in ["worker.js", "import worker", "V1_WEIGHT", "prediction_generation", "cron"]:
        assert forbidden not in src


# =====================================================================
# 10. Full end-to-end orchestration against a real (small) in-memory DB
# =====================================================================

def _fresh_db():
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
    return conn


def test_build_family_discrimination_report_end_to_end_and_deterministic():
    conn = _fresh_db()
    n = 300
    for i in range(n):
        ts = i * HOUR
        target_ts = ts + 24 * HOUR
        a = (i % 11) - 5
        c = (i % 9) - 4
        composite = 40 + (i % 30)
        # realized_return driven by "alpha" and "gamma" sources beyond composite
        realized_return = 3.0 * a + 2.5 * c + 0.01 * composite + 0.3 * _noise(i)
        conn.execute(
            "INSERT INTO predictions (ts, target_ts, horizon_hours, p_up, realized_up, realized_return, model_version) "
            "VALUES (?, ?, 24, 0.5, ?, ?, 'test-model')",
            (ts, target_ts, 1 if realized_return > 0 else 0, realized_return),
        )
        conn.execute(
            "INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?, ?, ?, ?, ?)",
            (ts, composite, json.dumps({"alpha": a, "beta": a + 0.1 * _noise2(i), "gamma": c}), composite, None),
        )
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, 100.0 + 0.01 * realized_return))
    for i in range(n, n + 30):
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (i * HOUR, 103.0))
    conn.commit()

    start_ts, end_ts = 0, n * HOUR
    report_1 = sfd.build_family_discrimination_report(conn, start_ts, end_ts, source_horizons=(24,))
    report_2 = sfd.build_family_discrimination_report(conn, start_ts, end_ts, source_horizons=(24,))
    assert report_1 == report_2  # deterministic rerun
    conn.close()

    assert isinstance(report_1["surviving_families"], dict)
    assert isinstance(report_1["snapshot_diff_vs_pr5e"], dict)
    for family, result in report_1["family_results"].items():
        assert result["discrimination_status"] in sfd.DISCRIMINATION_STATUSES
        assert "original_pr5e_lifecycle_status" in result


def test_empty_window_produces_empty_report_not_error():
    conn = _fresh_db()
    report = sfd.build_family_discrimination_report(conn, 0, HOUR, source_horizons=(24,))
    assert report["surviving_families"] == {}
    assert report["family_results"] == {}
    conn.close()


# =====================================================================
# 11. Snapshot comparison explicitly reports differences
# =====================================================================

def test_snapshot_diff_reports_row_count_and_family_changes():
    current_counts = {"predictions": 1200, "history": 499, "btc_data": 2095}
    current_families = ["fng", "global"]  # onchain dropped out in this hypothetical rerun
    diff = sfd.compare_snapshot_to_pr5e(current_counts, current_families)
    assert "predictions" in diff
    assert diff["predictions"]["pr5e"] == 1070
    assert diff["predictions"]["current"] == 1200
    assert "surviving_families" in diff
    assert diff["surviving_families"]["current"] == ("fng", "global")


def test_snapshot_diff_empty_when_nothing_changed():
    current_counts = {"predictions": 1070, "history": 499, "btc_data": 2095}
    current_families = ["fng", "global", "onchain"]
    diff = sfd.compare_snapshot_to_pr5e(current_counts, current_families)
    assert diff == {}
