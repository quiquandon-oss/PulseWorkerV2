"""
Tests for research/fng_24h_readiness.py.

All tests execute against a real (non-mocked) in-memory sqlite3
database mirroring production. No network, no D1 connection.

Run with: python3 -m pytest research/test_fng_24h_readiness.py -v
"""
import inspect
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import fng_24h_readiness as frd  # noqa: E402

HOUR = 3600000
WINDOW_END = 59 * HOUR  # a fixed, injectable "reference window end" for tests --
# never PR5F_REFERENCE's real production value, so these tests can never be
# confused with (or accidentally validate against) the real frozen reference.


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


def _insert_history(conn, ts, sources=None, score=50):
    sources = sources if sources is not None else {"fng": 50, "global": 10, "onchain": 5}
    conn.execute(
        "INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
        (ts, score, json.dumps(sources), 50, None),
    )


def _insert_btc(conn, ts, price=100.0):
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))


def _seed_pre_window_sample(conn, n=60):
    """Simulates the EXISTING PR5f/PR5g sample: complete, resolvable rows
    entirely AT OR BEFORE WINDOW_END. Used by every test to prove new
    counts never include this population."""
    for i in range(n):
        _insert_history(conn, i * HOUR)
    for i in range(n + 24):  # btc_data far enough to resolve every pre-window row
        _insert_btc(conn, i * HOUR)


# =====================================================================
# 1. Zero new rows
# =====================================================================

def test_zero_new_rows_reports_waiting():
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=60)  # all at/before WINDOW_END=59h
    conn.commit()
    record = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert record["new_candidate_rows"] == 0
    assert record["new_resolvable_rows"] == 0
    assert record["sufficient_for_branch_a"] is False
    assert record["status"] == "WAITING_FOR_NEW_24H_DATA"
    conn.close()


# =====================================================================
# The existing 257-row (here: 60-row) sample must never be counted new
# =====================================================================

def test_existing_sample_never_counted_as_new_even_when_btc_data_advances():
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=60)
    # btc_data advances well past the window end (as it does in
    # production), but NO new history row exists past it.
    for i in range(60, 131):
        _insert_btc(conn, i * HOUR)
    conn.commit()
    record = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert record["current_btc_data_max_ts"] == 130 * HOUR
    assert record["new_candidate_rows"] == 0
    assert record["new_resolvable_rows"] == 0
    assert record["status"] == "WAITING_FOR_NEW_24H_DATA"
    conn.close()


# =====================================================================
# 2 / 8. Partial future data / future timestamps without a resolved
# outcome yet -- including a MIXED case (some resolvable, some not)
# =====================================================================

def test_partial_future_data_no_resolved_24h_outcomes_yet():
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=60)
    for i in range(60, 65):  # 5 new candidate rows
        _insert_history(conn, i * HOUR)
    # btc_data extends only ~6h past the window -- far short of 24h
    for i in range(66):
        _insert_btc(conn, i * HOUR)
    conn.commit()
    record = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert record["new_candidate_rows"] == 5
    assert record["new_resolvable_rows"] == 0
    assert record["sufficient_for_branch_a"] is False
    assert record["status"] == "WAITING_FOR_NEW_24H_DATA"
    assert record["data_gap_ms"] > 0
    conn.close()


def test_future_timestamps_partially_resolved_mixed_case():
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=60)
    for i in range(60, 80):  # 20 new candidate rows (i=60..79)
        _insert_history(conn, i * HOUR)
    # btc_data extends to 94h -> only rows with ts + 24h <= 94h resolve,
    # i.e. i <= 70 -> i=60..70 inclusive = 11 rows. i=71..79 (9 rows) are
    # genuinely future timestamps without a resolved outcome yet.
    for i in range(95):
        _insert_btc(conn, i * HOUR)
    conn.commit()
    record = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert record["new_candidate_rows"] == 20
    assert record["new_resolvable_rows"] == 11
    assert record["n_incomplete_or_unresolved_candidate_rows"] == 9
    assert record["status"] == "WAITING_FOR_NEW_24H_DATA"
    conn.close()


# =====================================================================
# 3 / 4 / 5. Threshold boundary behavior
# =====================================================================

def test_exactly_threshold_rows_is_sufficient():
    assert frd.REQUIRED_MINIMUM_ROWS == 40
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=60)
    for i in range(60, 100):  # exactly 40 new candidate rows (i=60..99)
        _insert_history(conn, i * HOUR)
    for i in range(124):  # covers ts=99h + 24h = 123h
        _insert_btc(conn, i * HOUR)
    conn.commit()
    record = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert record["new_candidate_rows"] == 40
    assert record["new_resolvable_rows"] == 40
    assert record["sufficient_for_branch_a"] is True
    assert record["status"] == "READY_FOR_PR5G_BRANCH_A"
    conn.close()


def test_one_below_threshold_is_insufficient():
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=60)
    for i in range(60, 99):  # 39 new candidate rows (i=60..98)
        _insert_history(conn, i * HOUR)
    for i in range(123):  # covers ts=98h + 24h = 122h
        _insert_btc(conn, i * HOUR)
    conn.commit()
    record = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert record["new_candidate_rows"] == 39
    assert record["new_resolvable_rows"] == 39
    assert record["sufficient_for_branch_a"] is False
    assert record["status"] == "WAITING_FOR_NEW_24H_DATA"
    conn.close()


def test_comfortably_sufficient_new_rows():
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=60)
    for i in range(60, 110):  # 50 new candidate rows (i=60..109), all > WINDOW_END=59h
        _insert_history(conn, i * HOUR)
    for i in range(134):  # covers ts=109h + 24h = 133h
        _insert_btc(conn, i * HOUR)
    conn.commit()
    record = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert record["new_candidate_rows"] == 50
    assert record["new_resolvable_rows"] == 50
    assert record["sufficient_for_branch_a"] is True
    assert record["status"] == "READY_FOR_PR5G_BRANCH_A"
    assert record["earliest_new_resolvable_ts"] == 60 * HOUR
    assert record["latest_new_resolvable_ts"] == 109 * HOUR
    conn.close()


# =====================================================================
# 6 / 7. Source completeness -- missing target or a control excludes a
# row from readiness even though it is chronologically new and
# resolvable by timing alone.
# =====================================================================

def test_missing_fng_excludes_rows_from_readiness():
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=60)
    for i in range(60, 105):  # 45 new candidate rows, all missing "fng"
        _insert_history(conn, i * HOUR, sources={"global": 10, "onchain": 5})
    for i in range(130):
        _insert_btc(conn, i * HOUR)
    conn.commit()
    record = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert record["new_candidate_rows"] == 45
    assert record["new_resolvable_rows_by_timestamp_only"] == 45  # timing alone says "resolvable"
    assert record["new_resolvable_rows"] == 0  # but none carry the target family
    assert record["sufficient_for_branch_a"] is False
    assert record["status"] == "WAITING_FOR_NEW_24H_DATA"
    conn.close()


def test_missing_control_family_excludes_rows_from_readiness():
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=60)
    for i in range(60, 105):  # 45 new candidate rows, all missing "onchain"
        _insert_history(conn, i * HOUR, sources={"fng": 50, "global": 10})
    for i in range(130):
        _insert_btc(conn, i * HOUR)
    conn.commit()
    record = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert record["new_candidate_rows"] == 45
    assert record["new_resolvable_rows_by_timestamp_only"] == 45
    assert record["new_resolvable_rows"] == 0
    assert record["status"] == "WAITING_FOR_NEW_24H_DATA"
    conn.close()


# =====================================================================
# 9. Deterministic repeated execution
# =====================================================================

def test_deterministic_rerun_produces_identical_record():
    conn = _fresh_db()
    _seed_pre_window_sample(conn, n=50)
    for i in range(50, 100):
        _insert_history(conn, i * HOUR)
    for i in range(124):
        _insert_btc(conn, i * HOUR)
    conn.commit()
    r1 = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    r2 = frd.build_readiness_record(conn, reference_window_end_ts=WINDOW_END, extraction_timestamp=1000)
    assert r1 == r2
    conn.close()


# =====================================================================
# 10. No production writes / no network / never touches V1/V2/Worker
# =====================================================================

def test_no_writes_anywhere_in_module():
    src = inspect.getsource(frd)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in src
    assert "def persist" not in src


def test_no_network_or_llm_calls_anywhere_in_module():
    src = inspect.getsource(frd)
    for forbidden in ["import requests", "requests.get(", "requests.post(", "urllib", "fetch(",
                       "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_never_modifies_or_reimports_v1_v2_worker():
    src = inspect.getsource(frd)
    for forbidden in ["worker.js", "import worker", "V1_WEIGHT", "prediction_generation", "cron", "0008"]:
        assert forbidden not in src


def test_never_computes_an_fng_performance_number():
    # The readiness module must never call ols_nvar/rmse or any fitting
    # routine -- it only ever counts rows.
    src = inspect.getsource(frd)
    for forbidden in ["ols_nvar(", "rmse(", "frozen_model_from_pr5f_discovery(",
                       "evaluate_period_against_frozen_model(", "quartile_walk_forward"]:
        assert forbidden not in src


def test_reference_window_end_matches_pr5g_own_frozen_reference():
    import fng_24h_robustness as fr
    assert frd.REFERENCE_WINDOW_END_TS == fr.PR5F_REFERENCE["window_end_ts"]


def test_required_minimum_reused_from_pr5c_not_reinvented():
    import source_analysis as sa
    assert frd.REQUIRED_MINIMUM_ROWS == sa.MIN_SAMPLE_FOR_LEVEL3
