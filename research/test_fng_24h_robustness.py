"""
Tests for research/fng_24h_robustness.py (PR5g).

All tests execute against real (non-mocked) synthetic fixtures or a
real in-memory sqlite3 database mirroring production. No network, no
D1 connection.

Run with: python3 -m pytest research/test_fng_24h_robustness.py -v
"""
import inspect
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import fng_24h_robustness as fr  # noqa: E402
import source_family_discrimination as sfd  # noqa: E402

HOUR = 3600000


def _noise(i):
    return ((i * 37) % 13) - 6


def _synthetic_matrix(n, source_fn, y_fn, start_ts=0):
    matrix_rows, outcome_rows = [], []
    for i in range(n):
        ts = start_ts + i * HOUR
        composite = 50 + (i % 7)
        sources = source_fn(i)
        y = y_fn(i, composite, sources)
        matrix_rows.append({"ts": ts, "v1_composite": float(composite), "gold_regime": None,
                             "sources": {k: float(v) for k, v in sources.items()}})
        outcome_rows.append({"anchor_ts": ts, "outcome_status": "RESOLVED", "forward_return_pct": y})
    return matrix_rows, outcome_rows


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, target_ts INTEGER,
        horizon_hours INTEGER NOT NULL, p_up REAL, realized_up INTEGER, realized_return REAL,
        model_version TEXT, git_commit_sha TEXT
    )""")
    conn.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL,
        sources_json TEXT, technical_score INTEGER, gold_regime TEXT
    )""")
    conn.execute("""CREATE TABLE btc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
    )""")
    return conn


# =====================================================================
# 1. Predefined 24h horizon / no horizon re-selection
# =====================================================================

def test_horizon_is_fixed_at_24h():
    assert fr.HORIZON_HOURS == 24


def test_module_never_recomputes_6h_or_12h():
    # SIX_TWELVE_HOUR_CONTEXT must be a hardcoded, fixed dict of PR5f's
    # own already-published audit numbers -- never derived from a fresh
    # computation inside this module (no function anywhere in the module
    # takes a horizon_hours parameter other than HORIZON_HOURS=24, except
    # check_new_chronological_data's own generic horizon_hours arg, which
    # is only ever called with HORIZON_HOURS at the report level).
    assert fr.SIX_TWELVE_HOUR_CONTEXT[6]["rmse_reduction_pct"] == 1.5463980874769077
    assert fr.SIX_TWELVE_HOUR_CONTEXT[12]["rmse_reduction_pct"] == 4.397635340269696
    report_src = inspect.getsource(fr.build_fng_24h_robustness_report)
    assert "SIX_TWELVE_HOUR_CONTEXT" in report_src
    # the report function never calls anything with a literal 6 or 12 as horizon
    assert "outcome_engine.compute_forward_returns_from_history(conn, start_ts, end_ts, 6)" not in report_src
    assert "outcome_engine.compute_forward_returns_from_history(conn, start_ts, end_ts, 12)" not in report_src


def test_report_never_computes_a_horizon_other_than_24h_from_live_data():
    conn = _fresh_db()
    n = 300

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c + 0.5 * s["global"] + 0.3 * s["onchain"] + 0.3 * _noise(i)

    for i in range(n):
        ts = i * HOUR
        s = source_fn(i)
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
                     (ts, 50 + (i % 7), __import__("json").dumps(s), 50, None))
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, 100.0 + 0.01 * i))
    for i in range(n, n + 30):
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (i * HOUR, 103.0))
    conn.commit()
    report = fr.build_fng_24h_robustness_report(conn, pr5f_start_ts=0, pr5f_end_ts=(n - 1) * HOUR)
    assert report["horizon_hours"] == 24
    conn.close()


# =====================================================================
# 2. Data availability check -- insufficient post-period data
# =====================================================================

def test_check_new_data_reports_zero_when_nothing_exists_after_window():
    conn = _fresh_db()
    for i in range(50):
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
                     (i * HOUR, 50, "{}", 50, None))
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (i * HOUR, 100.0))
    conn.commit()
    result = fr.check_new_chronological_data(conn, pr5f_window_end_ts=49 * HOUR, horizon_hours=24)
    assert result["n_history_rows_after_window"] == 0
    assert result["sufficient_for_new_period_test"] is False
    conn.close()


def test_check_new_data_insufficient_when_future_price_data_too_short():
    conn = _fresh_db()
    for i in range(50):
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
                     (i * HOUR, 50, "{}", 50, None))
    # a few NEW history rows exist after the window...
    for i in range(50, 55):
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
                     (i * HOUR, 50, "{}", 50, None))
    # ...but btc_data only extends 6 hours past the window end -- far
    # short of the 24h needed to resolve even the earliest of them.
    for i in range(56):
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (i * HOUR, 100.0))
    conn.commit()
    result = fr.check_new_chronological_data(conn, pr5f_window_end_ts=49 * HOUR, horizon_hours=24)
    assert result["n_history_rows_after_window"] == 5
    assert result["n_resolvable_new_rows"] == 0
    assert result["sufficient_for_new_period_test"] is False
    conn.close()


def test_check_new_data_sufficient_when_genuinely_enough_new_resolvable_rows():
    conn = _fresh_db()
    n_total = 150
    for i in range(n_total):
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
                     (i * HOUR, 50, "{}", 50, None))
    # btc_data extends far enough past the window for the new rows to resolve at 24h
    for i in range(n_total + 24):
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (i * HOUR, 100.0))
    conn.commit()
    result = fr.check_new_chronological_data(conn, pr5f_window_end_ts=99 * HOUR, horizon_hours=24)
    assert result["n_history_rows_after_window"] == 50  # rows 100..149
    assert result["sufficient_for_new_period_test"] is True
    conn.close()


# =====================================================================
# 3. Chronological ordering / temporal segment construction / no leakage
# =====================================================================

def test_quartiles_are_chronologically_ordered_and_non_overlapping():
    n = 200
    combined = [(i * HOUR, float(i), [1.0, 2.0], float(i)) for i in range(n)]
    quartiles = fr.split_into_predefined_quartiles(combined)
    assert len(quartiles) == 4
    assert sum(len(q) for q in quartiles) == n
    all_ts = [c[0] for q in quartiles for c in q]
    assert all_ts == sorted(all_ts)
    # non-overlapping: each quartile's max ts < next quartile's min ts
    for i in range(3):
        assert quartiles[i][-1][0] < quartiles[i + 1][0][0]


def test_walk_forward_never_trains_on_future_data():
    src = inspect.getsource(fr.quartile_walk_forward_on_existing_sample)
    assert ".shuffle(" not in src
    assert "import random" not in src
    # structural: fold i's test set is quartiles[i+1], never part of train_rows before that point
    n = 400

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c + 0.5 * s["global"] + 0.3 * s["onchain"]

    matrix_rows, outcome_rows = _synthetic_matrix(n, source_fn, y_fn)
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, "fng", ["v1_composite", "global", "onchain"])
    quartiles = fr.split_into_predefined_quartiles(combined)
    folds = fr.quartile_walk_forward_on_existing_sample(combined)
    # fold 1's test period (Q2) must start strictly after fold 1's train period (Q1) ends
    assert folds[0]["test_period"][0] > folds[0]["train_period"][1]
    assert folds[1]["test_period"][0] > folds[1]["train_period"][1]
    assert folds[2]["test_period"][0] > folds[2]["train_period"][1]


def test_mutating_a_later_row_never_changes_an_earlier_folds_result():
    n = 400

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c + 0.5 * s["global"] + 0.3 * s["onchain"]

    matrix_rows, outcome_rows = _synthetic_matrix(n, source_fn, y_fn)
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, "fng", ["v1_composite", "global", "onchain"])
    folds_before = fr.quartile_walk_forward_on_existing_sample(combined)

    # mutate only the LAST row's target value
    last_ts, last_target, last_controls, last_y = combined[-1]
    combined[-1] = (last_ts, 999999.0, last_controls, last_y)
    folds_after = fr.quartile_walk_forward_on_existing_sample(combined)

    # fold 1 (train Q1, test Q2) must be completely unaffected by a
    # mutation in Q4 (the last row)
    assert folds_before[0] == folds_after[0]


# =====================================================================
# 4. Minimum sample handling / insufficient data
# =====================================================================

def test_walk_forward_reports_insufficient_data_for_small_fold():
    n = 80  # quartiles of 20 rows each -- below MIN_SAMPLE_FOR_HOLDOUT_HALF (30)

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c

    matrix_rows, outcome_rows = _synthetic_matrix(n, source_fn, y_fn)
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, "fng", ["v1_composite", "global", "onchain"])
    folds = fr.quartile_walk_forward_on_existing_sample(combined)
    assert folds[0]["status"] == "INSUFFICIENT_DATA"
    assert fr.classify_walk_forward(folds) == "INSUFFICIENT_DATA"


# =====================================================================
# 5. Positive / negative / mixed replication classification
# =====================================================================

def test_classify_walk_forward_robust_when_all_folds_strongly_positive():
    folds = [
        {"status": "OK", "rmse_reduction_pct": 8.0},
        {"status": "OK", "rmse_reduction_pct": 9.0},
        {"status": "OK", "rmse_reduction_pct": 12.0},
    ]
    assert fr.classify_walk_forward(folds) == "ROBUST"


def test_classify_walk_forward_not_replicated_when_all_folds_negative():
    folds = [
        {"status": "OK", "rmse_reduction_pct": -3.0},
        {"status": "OK", "rmse_reduction_pct": -1.0},
        {"status": "OK", "rmse_reduction_pct": -5.0},
    ]
    assert fr.classify_walk_forward(folds) == "NOT_REPLICATED"


def test_classify_walk_forward_mixed_when_inconsistent():
    folds = [
        {"status": "OK", "rmse_reduction_pct": 6.0},
        {"status": "OK", "rmse_reduction_pct": -2.0},
        {"status": "OK", "rmse_reduction_pct": 1.0},
    ]
    result = fr.classify_walk_forward(folds)
    assert result in ("MIXED", "NOT_REPLICATED")  # majority positive but not all -> not ROBUST
    assert result != "ROBUST"


def test_end_to_end_positive_replication_with_real_synthetic_signal():
    n = 800  # large enough for 4 quartiles of 200, each comfortably above MIN_SAMPLE_FOR_HOLDOUT_HALF

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c + 0.5 * s["global"] + 0.3 * s["onchain"] + 0.3 * _noise(i)

    matrix_rows, outcome_rows = _synthetic_matrix(n, source_fn, y_fn)
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, "fng", ["v1_composite", "global", "onchain"])
    folds = fr.quartile_walk_forward_on_existing_sample(combined)
    assert all(f["status"] == "OK" for f in folds)
    assert all(f["rmse_reduction_pct"] > 0 for f in folds)
    assert fr.classify_walk_forward(folds) == "ROBUST"


def test_end_to_end_negative_replication_when_relationship_reverses():
    n = 800
    q_boundary = n // 2

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        base = 0.01 * c + 0.5 * s["global"] + 0.3 * s["onchain"] + 0.3 * _noise(i)
        # relationship with fng reverses halfway through -> later folds should fail
        return base + (3.0 * s["fng"] if i < q_boundary else -3.0 * s["fng"])

    matrix_rows, outcome_rows = _synthetic_matrix(n, source_fn, y_fn)
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, "fng", ["v1_composite", "global", "onchain"])
    folds = fr.quartile_walk_forward_on_existing_sample(combined)
    assert folds[-1]["status"] == "OK"
    assert folds[-1]["rmse_reduction_pct"] < 0
    classification = fr.classify_walk_forward(folds)
    assert classification in ("MIXED", "NOT_REPLICATED")


# =====================================================================
# 6. Branch A (frozen-model forward application) -- synthetic, since no
# real new data exists today
# =====================================================================

def test_frozen_model_applied_to_synthetic_new_period_positive_case():
    n = 300

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c + 0.5 * s["global"] + 0.3 * s["onchain"] + 0.3 * _noise(i)

    matrix_rows, outcome_rows = _synthetic_matrix(n, source_fn, y_fn)
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, "fng", ["v1_composite", "global", "onchain"])
    frozen = fr.frozen_model_from_pr5f_discovery(combined)
    assert frozen["baseline"][0] is not None

    # new period: same relationship continues to hold
    new_matrix, new_outcomes = _synthetic_matrix(100, source_fn, y_fn, start_ts=n * HOUR)
    new_combined = sfd.build_combined_rows(new_matrix, new_outcomes, "fng", ["v1_composite", "global", "onchain"])
    result = fr.evaluate_period_against_frozen_model(new_combined, frozen)
    assert result["status"] == "OK"
    assert result["rmse_reduction_pct"] > 0


def test_frozen_model_insufficient_data_for_tiny_new_period():
    n = 300

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c

    matrix_rows, outcome_rows = _synthetic_matrix(n, source_fn, y_fn)
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, "fng", ["v1_composite", "global", "onchain"])
    frozen = fr.frozen_model_from_pr5f_discovery(combined)

    new_matrix, new_outcomes = _synthetic_matrix(5, source_fn, y_fn, start_ts=n * HOUR)
    new_combined = sfd.build_combined_rows(new_matrix, new_outcomes, "fng", ["v1_composite", "global", "onchain"])
    result = fr.evaluate_period_against_frozen_model(new_combined, frozen)
    assert result["status"] == "INSUFFICIENT_DATA"


# =====================================================================
# 7. Deterministic rerun
# =====================================================================

def test_quartile_walk_forward_deterministic_rerun():
    n = 400

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c + 0.5 * s["global"] + 0.3 * s["onchain"] + 0.3 * _noise(i)

    matrix_rows, outcome_rows = _synthetic_matrix(n, source_fn, y_fn)
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, "fng", ["v1_composite", "global", "onchain"])
    folds_1 = fr.quartile_walk_forward_on_existing_sample(combined)
    folds_2 = fr.quartile_walk_forward_on_existing_sample(combined)
    assert folds_1 == folds_2


def test_full_report_deterministic_rerun():
    conn = _fresh_db()
    n = 400

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c + 0.5 * s["global"] + 0.3 * s["onchain"] + 0.3 * _noise(i)

    import json
    for i in range(n):
        ts = i * HOUR
        s = source_fn(i)
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
                     (ts, 50 + (i % 7), json.dumps(s), 50, None))
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, 100.0))
    for i in range(n, n + 30):
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (i * HOUR, 100.0))
    conn.commit()
    report_1 = fr.build_fng_24h_robustness_report(conn, pr5f_start_ts=0, pr5f_end_ts=(n - 1) * HOUR)
    report_2 = fr.build_fng_24h_robustness_report(conn, pr5f_start_ts=0, pr5f_end_ts=(n - 1) * HOUR)
    assert report_1 == report_2
    conn.close()


# =====================================================================
# 8. Missing source data / complete-case handling
# =====================================================================

def test_missing_source_rows_excluded_not_imputed():
    n = 300

    def source_fn(i):
        # onchain missing every 3rd row
        d = {"fng": (i % 11) - 5, "global": (i % 13) - 6}
        if i % 3 != 0:
            d["onchain"] = (i % 9) - 4
        return d

    matrix_rows, outcome_rows = [], []
    for i in range(n):
        ts = i * HOUR
        s = source_fn(i)
        matrix_rows.append({"ts": ts, "v1_composite": 50.0, "gold_regime": None,
                             "sources": {k: float(v) for k, v in s.items()}})
        outcome_rows.append({"anchor_ts": ts, "outcome_status": "RESOLVED", "forward_return_pct": float(i % 5)})
    combined = sfd.build_combined_rows(matrix_rows, outcome_rows, "fng", ["v1_composite", "global", "onchain"])
    # roughly 2/3 of rows have onchain present
    assert len(combined) < n
    assert len(combined) > 0
    for row in combined:
        assert row[1] is not None
        assert all(v is not None for v in row[2])


# =====================================================================
# 9. No production writes / no migration / no V1/V2/Worker changes
# =====================================================================

def test_no_writes_anywhere_in_module():
    src = inspect.getsource(fr)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in src
    assert "def persist" not in src


def test_no_network_or_llm_calls_anywhere_in_module():
    src = inspect.getsource(fr)
    for forbidden in ["import requests", "requests.get(", "requests.post(", "urllib", "fetch(",
                       "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_never_modifies_or_reimports_v1_v2_worker():
    src = inspect.getsource(fr)
    for forbidden in ["worker.js", "import worker", "V1_WEIGHT", "prediction_generation", "cron", "0008"]:
        assert forbidden not in src


# =====================================================================
# 10. Report-level correctness -- no automatic "validated" claim
# =====================================================================

def test_report_never_claims_validated():
    conn = _fresh_db()
    n = 400
    import json

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c + 0.5 * s["global"] + 0.3 * s["onchain"] + 0.3 * _noise(i)

    for i in range(n):
        ts = i * HOUR
        s = source_fn(i)
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
                     (ts, 50 + (i % 7), json.dumps(s), 50, None))
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, 100.0))
    conn.commit()
    report = fr.build_fng_24h_robustness_report(conn, pr5f_start_ts=0, pr5f_end_ts=(n - 1) * HOUR)
    assert "not_validated_statement" in report
    assert "NOT" in report["not_validated_statement"]
    assert report["primary_classification"] in fr.CLASSIFICATIONS
    conn.close()


def test_disclosure_states_robustness_not_confirmation_when_branch_b():
    conn = _fresh_db()
    n = 400
    import json

    def source_fn(i):
        return {"fng": (i % 11) - 5, "global": (i % 13) - 6, "onchain": (i % 9) - 4}

    def y_fn(i, c, s):
        return 3.0 * s["fng"] + 0.01 * c

    for i in range(n):
        ts = i * HOUR
        s = source_fn(i)
        conn.execute("INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
                     (ts, 50 + (i % 7), json.dumps(s), 50, None))
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, 100.0))
    conn.commit()
    report = fr.build_fng_24h_robustness_report(conn, pr5f_start_ts=0, pr5f_end_ts=(n - 1) * HOUR)
    assert report["branch"] == "B_existing_sample_walk_forward"
    assert report["is_independent_confirmation"] is False
    assert "robustness evidence, not" in report["disclosure"].lower()
    assert report["primary_classification"] == "INSUFFICIENT_DATA"
    conn.close()
