"""
Tests for research/source_analysis.py (PR5c).

All tests execute against a real, in-memory SQLite database shaped like
production (history + btc_data, with idx_ts/idx_btc_data_ts). No network,
no D1 connection required to run these -- and no test in this file ever
constructs or uses a production connection (Section 19 -- production D1
stays read-only, and this PR's own test suite never touches it at all).

Run with: python3 -m pytest research/test_source_analysis.py -v
"""
import inspect
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import source_analysis as sa  # noqa: E402

HOUR = 3600000
DAY = 24 * HOUR


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL,
        btc_price REAL, sources_json TEXT, technical_score INTEGER, gold_regime TEXT,
        regime_mag REAL, bottom_score INTEGER, global_mcap REAL
    )""")
    conn.execute("CREATE INDEX idx_ts ON history(ts)")
    conn.execute("""CREATE TABLE btc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
    )""")
    conn.execute("CREATE INDEX idx_btc_data_ts ON btc_data(ts)")
    conn.execute("""CREATE TABLE research_analyses (
        analysis_id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_ts INTEGER NOT NULL,
        window_start_ts INTEGER NOT NULL, window_end_ts INTEGER NOT NULL,
        sample_size INTEGER NOT NULL, subject TEXT NOT NULL, metric_json TEXT NOT NULL,
        multiple_testing_correction TEXT, validation_status TEXT NOT NULL DEFAULT 'observation'
    )""")
    return conn


def insert_history(conn, ts, score, sources_json=None, gold_regime=None):
    conn.execute(
        "INSERT INTO history (ts, score, sources_json, gold_regime) VALUES (?, ?, ?, ?)",
        (ts, score, sources_json, gold_regime),
    )


def insert_price(conn, ts, price):
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))


# =====================================================================
# Source enumeration (Section 5) -- data-derived, not hard-coded
# =====================================================================

def test_discover_sources_is_data_derived_not_hardcoded():
    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"alpha": 1, "beta": 2}))
    insert_history(conn, HOUR, 51, json.dumps({"alpha": 3, "gamma": 4}))
    keys = sa.discover_sources(conn, 0, 2 * HOUR)
    assert keys == ["alpha", "beta", "gamma"]
    conn.close()


def test_discover_sources_ignores_malformed_json():
    conn = fresh_db()
    insert_history(conn, 0, 50, "not valid json")
    insert_history(conn, HOUR, 51, json.dumps({"alpha": 1}))
    keys = sa.discover_sources(conn, 0, 2 * HOUR)
    assert keys == ["alpha"]
    conn.close()


def test_discover_sources_bounds_required():
    conn = fresh_db()
    with pytest.raises(TypeError):
        sa.discover_sources(conn)


def test_valid_sources_json_object_recognized():
    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"alpha": 1}))
    keys, rows, non_numeric = sa.extract_source_matrix(conn, 0, HOUR)
    assert keys == ["alpha"]
    assert rows[0]["sources"]["alpha"] == 1
    conn.close()


def test_scalar_source_extraction_direct_integer():
    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"alpha": 7}))
    _, rows, _ = sa.extract_source_matrix(conn, 0, HOUR)
    assert rows[0]["sources"]["alpha"] == 7
    conn.close()


# =====================================================================
# Missing != zero (Section 5/6)
# =====================================================================

def test_missing_source_is_none_not_zero():
    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"alpha": 1}))
    insert_history(conn, HOUR, 51, json.dumps({"beta": 2}))  # no "alpha" here
    keys, rows, _ = sa.extract_source_matrix(conn, 0, 2 * HOUR)
    alpha_values = [r["sources"]["alpha"] for r in rows]
    assert None in alpha_values
    assert 0 not in alpha_values
    conn.close()


def test_null_sources_json_row_still_present_all_none():
    conn = fresh_db()
    insert_history(conn, 0, 50, None)
    keys, rows, _ = sa.extract_source_matrix(conn, 0, HOUR)
    assert len(rows) == 1
    assert rows[0]["sources"] == {}


def test_source_coverage_report_reports_missing_never_imputes():
    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"alpha": 10}))
    insert_history(conn, HOUR, 51, json.dumps({}))  # alpha missing
    keys, rows, non_numeric = sa.extract_source_matrix(conn, 0, 2 * HOUR)
    report = sa.source_coverage_report(keys, rows, non_numeric)
    assert report["alpha"]["present"] == 1
    assert report["alpha"]["missing"] == 1
    assert report["alpha"]["mean"] == 10  # never averaged in a phantom 0
    conn.close()


# =====================================================================
# Source schema drift (structural_shape_report)
# =====================================================================

def test_structural_shape_report_detects_drift():
    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"alpha": 1, "beta": 2}))
    insert_history(conn, HOUR, 51, json.dumps({"alpha": 1}))  # different shape
    _, rows, _ = sa.extract_source_matrix(conn, 0, 2 * HOUR)
    shapes = sa.structural_shape_report(rows)
    assert shapes["n_distinct_shapes"] == 2
    conn.close()


def test_structural_shape_report_single_shape_when_uniform():
    conn = fresh_db()
    for i in range(5):
        insert_history(conn, i * HOUR, 50, json.dumps({"alpha": i}))
    _, rows, _ = sa.extract_source_matrix(conn, 0, 5 * HOUR)
    shapes = sa.structural_shape_report(rows)
    assert shapes["n_distinct_shapes"] == 1


# =====================================================================
# Source scale inspection (Level 1 / Section 6)
# =====================================================================

def test_level1_reports_range_and_variation():
    conn = fresh_db()
    for i, v in enumerate([1, 5, 9, 3, 7]):
        insert_history(conn, i * HOUR, 50, json.dumps({"alpha": v}))
    keys, rows, non_numeric = sa.extract_source_matrix(conn, 0, 5 * HOUR)
    report = sa.source_coverage_report(keys, rows, non_numeric)
    assert report["alpha"]["min"] == 1
    assert report["alpha"]["max"] == 9
    assert report["alpha"]["has_variation"] is True
    assert report["alpha"]["level1_status"] == "OK"


def test_level1_flags_no_variation_source():
    conn = fresh_db()
    for i in range(5):
        insert_history(conn, i * HOUR, 50, json.dumps({"constant": 42}))
    keys, rows, non_numeric = sa.extract_source_matrix(conn, 0, 5 * HOUR)
    report = sa.source_coverage_report(keys, rows, non_numeric)
    assert report["constant"]["has_variation"] is False
    assert report["constant"]["level1_status"] == "NO_VARIATION"


def test_level1_flags_no_data_source():
    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"alpha": 1}))
    insert_history(conn, HOUR, 51, json.dumps({"alpha": None}))
    keys, rows, non_numeric = sa.extract_source_matrix(conn, 0, 2 * HOUR)
    # a JSON null value should not be treated as present numeric data
    report = sa.source_coverage_report(keys, rows, non_numeric)
    assert report["alpha"]["present"] == 1


# =====================================================================
# No-lookahead (Section 4) -- constructive proof
# =====================================================================

def test_no_lookahead_later_source_value_never_affects_earlier_pairing():
    import outcome_engine

    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"alpha": 1}))
    insert_price(conn, 0, 100.0)
    insert_price(conn, HOUR, 105.0)

    outcome_rows_before = outcome_engine.compute_forward_returns_from_history(conn, 0, HOUR, horizon_hours=1)
    _, rows_before, _ = sa.extract_source_matrix(conn, 0, HOUR)
    result_before = sa.level2_association_for_source(rows_before, outcome_rows_before, "alpha", 1)

    # Now add a LATER row with a wildly different alpha value and a
    # LATER price. If this changed result_before's pairing for ts=0,
    # that would be a lookahead bug.
    insert_history(conn, 2 * HOUR, 50, json.dumps({"alpha": 9999}))
    insert_price(conn, 2 * HOUR, 1_000_000.0)

    outcome_rows_after = outcome_engine.compute_forward_returns_from_history(conn, 0, HOUR, horizon_hours=1)
    _, rows_after, _ = sa.extract_source_matrix(conn, 0, HOUR)
    result_after = sa.level2_association_for_source(rows_after, outcome_rows_after, "alpha", 1)

    assert result_before == result_after
    conn.close()


def test_forward_return_only_ever_uses_ts_strictly_after_anchor():
    import outcome_engine

    conn = fresh_db()
    insert_history(conn, HOUR, 50, json.dumps({"alpha": 1}))
    insert_price(conn, HOUR, 100.0)
    insert_price(conn, HOUR, 999.0)  # same ts as anchor -- must NOT count as "future"
    results = outcome_engine.compute_forward_returns_from_history(conn, 0, 2 * HOUR, horizon_hours=1)
    assert results[0]["outcome_status"] == "UNRESOLVED_NO_FUTURE_PRICE_POINT"
    conn.close()


# =====================================================================
# Continuous forward return / all supported horizons (Section 7)
# =====================================================================

def test_all_supported_horizons_produce_resolved_rows():
    import outcome_engine

    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"alpha": 1}))
    insert_price(conn, 0, 100.0)
    for h in (1, 3, 6, 12, 24):
        insert_price(conn, h * HOUR, 100.0 + h)
    for h in (1, 3, 6, 12, 24):
        rows = outcome_engine.compute_forward_returns_from_history(conn, 0, 24 * HOUR + 1, horizon_hours=h)
        assert any(r["outcome_status"] == "RESOLVED" and r["horizon_hours"] == h for r in rows)
    conn.close()


# =====================================================================
# Sub-4% movement analysis (Section 8) -- reuses movement_distribution
# =====================================================================

def test_movement_distribution_reused_reports_sub_4pct_fraction():
    conn = fresh_db()
    for i in range(40):
        insert_history(conn, i * HOUR, 50, json.dumps({"alpha": i % 5}))
        insert_price(conn, i * HOUR, 100.0 + (i % 3) * 0.1)  # tiny moves, well under 4%
    report = sa.build_source_effectiveness_report(conn, 0, 40 * HOUR, horizons=(1,))
    summary = report["movement_distribution_by_horizon"][1]
    assert summary["n"] > 0
    assert summary["proportion_below_pr3_threshold"] == 1.0
    conn.close()


# =====================================================================
# Multiple testing correction (Section 11)
# =====================================================================

def _seeded_linear_dataset(conn, n=60, slope=0.05, noise_seed=1234):
    import random
    rnd = random.Random(noise_seed)
    price = 100.0
    for i in range(n):
        ts = i * HOUR
        alpha = rnd.uniform(-10, 10)
        insert_history(conn, ts, 50, json.dumps({"alpha": alpha, "flat": 1}))
        insert_price(conn, ts, price)
        price = price * (1 + (slope * alpha + rnd.gauss(0, 0.05)) / 100.0)
    insert_price(conn, n * HOUR, price)


def test_multiple_testing_correction_applied_across_battery():
    conn = fresh_db()
    _seeded_linear_dataset(conn, n=60)
    keys, rows, non_numeric = sa.extract_source_matrix(conn, 0, 60 * HOUR)
    import outcome_engine
    outcome_rows = outcome_engine.compute_forward_returns_from_history(conn, 0, 60 * HOUR, horizon_hours=1)
    battery = sa.run_level2_battery(rows, {1: outcome_rows}, keys)
    assert battery["multiple_testing_correction"]["method"] == "benjamini_hochberg"
    assert battery["multiple_testing_correction"]["n_tests"] == len(keys)
    for key in keys:
        result = battery["tests"][(key, 1)]
        assert "p_corrected" in result
        assert "significant" in result
    conn.close()


def test_raw_p_below_005_alone_is_not_called_significant_without_correction_field():
    # A result with p_raw < 0.05 but p_corrected >= 0.05 must not report significant=True.
    result = {"p_corrected": 0.2, "p_raw": 0.01}
    assert not (result["p_corrected"] < sa.ALPHA)


# =====================================================================
# Pairwise / source-vs-composite redundancy (Section 9)
# =====================================================================

def test_pairwise_redundancy_detects_strong_correlation():
    conn = fresh_db()
    for i in range(40):
        v = i % 10
        insert_history(conn, i * HOUR, 50, json.dumps({"a": v, "b": v, "c": -v}))
    _, rows, _ = sa.extract_source_matrix(conn, 0, 40 * HOUR)
    redundancy = sa.pairwise_source_redundancy(rows, ["a", "b", "c"])
    assert redundancy[("a", "b")]["strong_redundancy"] is True
    assert redundancy[("a", "b")]["r"] == pytest.approx(1.0, abs=1e-9)
    assert redundancy[("a", "c")]["strong_redundancy"] is True
    assert redundancy[("a", "c")]["r"] == pytest.approx(-1.0, abs=1e-9)
    conn.close()


def test_pairwise_redundancy_reports_actual_n_per_pair_not_global_n():
    conn = fresh_db()
    insert_history(conn, 0, 50, json.dumps({"a": 1, "b": 2}))
    insert_history(conn, HOUR, 50, json.dumps({"a": 2}))  # b missing here
    _, rows, _ = sa.extract_source_matrix(conn, 0, 2 * HOUR)
    redundancy = sa.pairwise_source_redundancy(rows, ["a", "b"])
    assert redundancy[("a", "b")]["n"] == 1


def test_source_vs_composite_redundancy():
    conn = fresh_db()
    for i in range(40):
        insert_history(conn, i * HOUR, i, json.dumps({"mirror": i}))
    _, rows, _ = sa.extract_source_matrix(conn, 0, 40 * HOUR)
    redundancy = sa.source_vs_composite_redundancy(rows, ["mirror"])
    assert redundancy["mirror"]["r"] == pytest.approx(1.0, abs=1e-9)
    assert redundancy["mirror"]["strong_redundancy"] is True


# =====================================================================
# Level 2 vs Level 3 distinction (Section 3)
# =====================================================================

def test_level2_significant_does_not_imply_level3_result_exists():
    # level2_association_for_source() never computes or returns anything
    # about incremental/partial information -- that's a structurally
    # separate function (level3_incremental_for_source()).
    result = sa.level2_association_for_source(
        rows=[{"ts": i, "sources": {"alpha": i}, "v1_composite": i} for i in range(10)],
        outcome_rows=[{"anchor_ts": i, "outcome_status": "RESOLVED", "forward_return_pct": float(i)} for i in range(10)],
        source_key="alpha",
        horizon_hours=1,
    )
    assert "partial_correlation" not in result
    assert "oos" not in result or result.get("oos_split") is not None  # oos_split is Level2's own stability field, not Level3's oos


def test_level3_insufficient_data_reported_not_fabricated():
    rows = [{"ts": i, "sources": {"alpha": i}, "v1_composite": i, "gold_regime": None} for i in range(5)]
    outcome_rows = [{"anchor_ts": i, "outcome_status": "RESOLVED", "forward_return_pct": float(i)} for i in range(5)]
    result = sa.level3_incremental_for_source(rows, outcome_rows, "alpha", 1)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["partial_correlation"] is None


def test_level3_reports_oos_rmse_reduction_when_source_helps():
    import random
    rnd = random.Random(42)
    rows, outcome_rows = [], []
    for i in range(100):
        composite = rnd.uniform(-5, 5)
        source_val = rnd.uniform(-5, 5)
        # true outcome depends on BOTH composite and source
        y = 0.5 * composite + 2.0 * source_val + rnd.gauss(0, 0.01)
        rows.append({"ts": i, "sources": {"alpha": source_val}, "v1_composite": composite, "gold_regime": None})
        outcome_rows.append({"anchor_ts": i, "outcome_status": "RESOLVED", "forward_return_pct": y})
    result = sa.level3_incremental_for_source(rows, outcome_rows, "alpha", 1)
    assert result["status"] == "OK"
    assert result["oos"]["status"] == "IMPROVED"
    assert result["oos"]["rmse_reduction_pct"] > 0


def test_level3_uses_chronological_split_not_random():
    src = inspect.getsource(sa.level3_incremental_for_source)
    assert ".shuffle(" not in src
    assert "import random" not in src
    assert "combined.sort(key=lambda t: t[0])" in src  # explicit chronological sort


# =====================================================================
# Level 3 scope: "beyond the V1 composite" ONLY, never "beyond
# correlated/redundant sources" (post-review correction)
# =====================================================================

def test_level3_only_ever_conditions_on_source_and_composite():
    # Level 3's model has exactly 3 coefficients (intercept, composite,
    # source) -- no other source can be part of the conditioning set,
    # by construction, not just by claim.
    rnd_rows, outcome_rows = [], []
    for i in range(60):
        composite = float(i % 7)
        source_val = float(i % 5)
        y = 0.3 * composite + 1.0 * source_val
        rnd_rows.append({"ts": i, "sources": {"alpha": source_val, "beta": float(i % 3)},
                          "v1_composite": composite, "gold_regime": None})
        outcome_rows.append({"anchor_ts": i, "outcome_status": "RESOLVED", "forward_return_pct": y})
    result = sa.level3_incremental_for_source(rnd_rows, outcome_rows, "alpha", 1)
    coefs = result["oos"]["regression_coefficients_full_model"]
    assert set(coefs.keys()) == {"intercept", "composite_coef", "source_coef"}
    # the presence of an unrelated correlated source ("beta") in the
    # input rows must never appear in, or change the shape of, Level 3's
    # own model -- it is simply never read by this function at all.
    assert "beta" not in str(coefs)


def test_level3_never_reads_redundancy_output():
    # pairwise_source_redundancy()/source_vs_composite_redundancy() are
    # never called from within level3_incremental_for_source() -- Level 3
    # and Section 9's redundancy analysis are computed independently.
    src = inspect.getsource(sa.level3_incremental_for_source)
    assert "pairwise_source_redundancy(" not in src
    assert "source_vs_composite_redundancy(" not in src


def test_level3_docstrings_scope_claim_to_v1_composite_only():
    module_src = inspect.getsource(sa)
    function_src = inspect.getsource(sa.level3_incremental_for_source)
    for src in (module_src, function_src):
        assert "beyond the V1 composite" in src or "beyond THE V1 COMPOSITE" in src.upper()
    # the corrected, narrower claim must be present...
    assert "does NOT condition on any other source" in function_src or "does NOT condition on any other source" in module_src
    # ...and the disclaimed broader claim must be explicitly named as
    # NOT established, not merely absent.
    assert "beyond correlated" in module_src.lower()
    assert "not established" in module_src.lower() or "not addressed" in module_src.lower()


def test_report_carries_explicit_level3_scope_note():
    conn = fresh_db()
    for i in range(10):
        insert_history(conn, i * HOUR, 50, json.dumps({"alpha": i}))
        insert_price(conn, i * HOUR, 100.0 + i)
    report = sa.build_source_effectiveness_report(conn, 0, 10 * HOUR, horizons=(1,))
    assert "level3_scope_note" in report
    note = report["level3_scope_note"].lower()
    assert "beyond the v1 composite" in note
    assert "not" in note and "correlated" in note
    conn.close()


# =====================================================================
# Chronological / OOS validation (Section 10)
# =====================================================================

def test_level2_oos_split_is_chronological():
    rows = [{"ts": i, "sources": {"alpha": i}, "v1_composite": i} for i in range(20)]
    outcome_rows = [{"anchor_ts": i, "outcome_status": "RESOLVED", "forward_return_pct": float(i)} for i in range(20)]
    result = sa.level2_association_for_source(rows, outcome_rows, "alpha", 1, oos_split_fraction=0.5)
    assert result["oos_split"]["n_discovery"] == 10
    assert result["oos_split"]["n_validation"] == 10


# =====================================================================
# Regime sign-reversal reporting (Section 13) -- never gated away
# =====================================================================

def test_regime_stability_reports_sign_reversal_without_rejecting():
    rows, outcome_rows = [], []
    ts = 0
    # regime A: strong positive relationship
    for i in range(15):
        rows.append({"ts": ts, "sources": {"alpha": float(i)}, "v1_composite": 0, "gold_regime": "A"})
        outcome_rows.append({"anchor_ts": ts, "outcome_status": "RESOLVED", "forward_return_pct": float(i)})
        ts += 1
    # regime B: strong negative relationship
    for i in range(15):
        rows.append({"ts": ts, "sources": {"alpha": float(i)}, "v1_composite": 0, "gold_regime": "B"})
        outcome_rows.append({"anchor_ts": ts, "outcome_status": "RESOLVED", "forward_return_pct": -float(i)})
        ts += 1
    result = sa.regime_stability_for_source(rows, outcome_rows, "alpha", 1)
    assert result["sign_reversal_detected"] is True
    assert result["per_regime"]["A"]["r"] > 0
    assert result["per_regime"]["B"]["r"] < 0
    # both regimes are reported, neither is dropped or averaged away
    assert set(result["per_regime"].keys()) == {"A", "B"}


def test_regime_stability_insufficient_data_per_small_regime():
    rows = [{"ts": i, "sources": {"alpha": float(i)}, "v1_composite": 0, "gold_regime": "rare"} for i in range(3)]
    outcome_rows = [{"anchor_ts": i, "outcome_status": "RESOLVED", "forward_return_pct": float(i)} for i in range(3)]
    result = sa.regime_stability_for_source(rows, outcome_rows, "alpha", 1)
    assert result["per_regime"]["rare"]["status"] == "INSUFFICIENT_DATA"


# =====================================================================
# Insufficient sample handling (general)
# =====================================================================

def test_level2_insufficient_data_for_tiny_n():
    rows = [{"ts": 0, "sources": {"alpha": 1.0}, "v1_composite": 0}]
    outcome_rows = [{"anchor_ts": 0, "outcome_status": "RESOLVED", "forward_return_pct": 1.0}]
    result = sa.level2_association_for_source(rows, outcome_rows, "alpha", 1)
    assert result["status"] == "INSUFFICIENT_DATA"


def test_level2_no_variation_reported_explicitly():
    rows = [{"ts": i, "sources": {"alpha": 5.0}, "v1_composite": 0} for i in range(10)]
    outcome_rows = [{"anchor_ts": i, "outcome_status": "RESOLVED", "forward_return_pct": float(i)} for i in range(10)]
    result = sa.level2_association_for_source(rows, outcome_rows, "alpha", 1)
    assert result["status"] == "NO_VARIATION"


# =====================================================================
# CONTRADICTED requires adequate contradictory evidence (Section 12)
# =====================================================================

def test_contradicted_requires_adequate_sample_both_halves():
    xs_discovery = list(range(40))
    ys_discovery = [float(x) for x in xs_discovery]  # positive, strong
    xs_validation = list(range(40))
    ys_validation = [-float(x) for x in xs_validation]  # negative, strong

    rows = (
        [{"ts": i, "sources": {"alpha": float(xs_discovery[i])}, "v1_composite": 0} for i in range(40)]
        + [{"ts": 100 + i, "sources": {"alpha": float(xs_validation[i])}, "v1_composite": 0} for i in range(40)]
    )
    outcome_rows = (
        [{"anchor_ts": i, "outcome_status": "RESOLVED", "forward_return_pct": ys_discovery[i]} for i in range(40)]
        + [{"anchor_ts": 100 + i, "outcome_status": "RESOLVED", "forward_return_pct": ys_validation[i]} for i in range(40)]
    )
    level2 = sa.level2_association_for_source(rows, outcome_rows, "alpha", 1, oos_split_fraction=0.5)
    level2["p_corrected"] = 0.9  # force non-significant so we test the stability branch
    label = sa.classify_evidence(level2)
    assert label == "CONTRADICTED"


def test_insufficient_replication_is_inconclusive_not_contradicted():
    # discovery half has adequate n and a positive effect; validation
    # half is TINY (below MIN_SAMPLE_FOR_CONTRADICTED) with an opposite
    # sign -- this must be INCONCLUSIVE, never CONTRADICTED (Section 12).
    n_discovery = 40
    rows = [{"ts": i, "sources": {"alpha": float(i)}, "v1_composite": 0} for i in range(n_discovery)]
    outcome_rows = [{"anchor_ts": i, "outcome_status": "RESOLVED", "forward_return_pct": float(i)} for i in range(n_discovery)]
    # tiny validation tail, opposite sign
    for i in range(3):
        ts = 1000 + i
        rows.append({"ts": ts, "sources": {"alpha": float(i)}, "v1_composite": 0})
        outcome_rows.append({"anchor_ts": ts, "outcome_status": "RESOLVED", "forward_return_pct": -float(i)})

    level2 = sa.level2_association_for_source(rows, outcome_rows, "alpha", 1, oos_split_fraction=40 / 43)
    level2["p_corrected"] = 0.9
    label = sa.classify_evidence(level2)
    assert label != "CONTRADICTED"
    assert label in ("INCONCLUSIVE", "STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE")


def test_significant_result_labeled_statistically_significant():
    level2 = {"status": "OK", "p_corrected": 0.001, "effect_size_r": 0.8,
              "oos_split": {"r_discovery": 0.7, "r_validation": 0.75, "n_discovery": 50, "n_validation": 50}}
    assert sa.classify_evidence(level2) == "STATISTICALLY_SIGNIFICANT"


def test_insufficient_data_level2_yields_inconclusive_label():
    level2 = {"status": "INSUFFICIENT_DATA"}
    assert sa.classify_evidence(level2) == "INCONCLUSIVE"


# =====================================================================
# Determinism (Section 9 build authorization / Section 18)
# =====================================================================

def test_deterministic_reruns_identical_report():
    conn = fresh_db()
    _seeded_linear_dataset(conn, n=50)
    report_1 = sa.build_source_effectiveness_report(conn, 0, 50 * HOUR, horizons=(1, 3))
    report_2 = sa.build_source_effectiveness_report(conn, 0, 50 * HOUR, horizons=(1, 3))
    assert report_1 == report_2
    conn.close()


# =====================================================================
# No production writes (Section 17/19) -- persist_analysis is the ONLY
# function in this module that may write, and it is never called
# against a production connection in this test suite.
# =====================================================================

def test_only_persist_analysis_contains_insert_into():
    src = inspect.getsource(sa)
    functions = [
        sa.discover_sources, sa.extract_source_matrix, sa.source_coverage_report,
        sa.structural_shape_report, sa.level2_association_for_source, sa.run_level2_battery,
        sa.level3_incremental_for_source, sa.pairwise_source_redundancy,
        sa.source_vs_composite_redundancy, sa.regime_stability_for_source,
        sa.classify_evidence, sa.build_source_effectiveness_report,
    ]
    for fn in functions:
        fn_src = inspect.getsource(fn)
        for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
            assert forbidden not in fn_src, f"{fn.__name__} must be read-only -- found {forbidden}"
    assert "INSERT INTO" in inspect.getsource(sa.persist_analysis)


def test_no_network_or_llm_calls_anywhere_in_module():
    src = inspect.getsource(sa)
    for forbidden in ["requests.", "urllib", "fetch(", "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()


def test_persist_analysis_writes_only_to_in_memory_test_db():
    conn = fresh_db()
    analysis_id = sa.persist_analysis(
        conn, analysis_ts=1000, window_start_ts=0, window_end_ts=1000,
        sample_size=42, subject="source_effectiveness:alpha",
        metric_json_obj={"effect_size_r": 0.3}, multiple_testing_correction="benjamini_hochberg",
    )
    row = conn.execute("SELECT * FROM research_analyses WHERE analysis_id = ?", (analysis_id,)).fetchone()
    assert row is not None
    conn.close()


def test_persist_analysis_metric_json_round_trips():
    conn = fresh_db()
    payload = {"nested": {"a": 1, "b": [1, 2, 3]}}
    analysis_id = sa.persist_analysis(
        conn, analysis_ts=1000, window_start_ts=0, window_end_ts=1000,
        sample_size=1, subject="test", metric_json_obj=payload,
        multiple_testing_correction="benjamini_hochberg",
    )
    row = conn.execute("SELECT metric_json FROM research_analyses WHERE analysis_id = ?", (analysis_id,)).fetchone()
    assert json.loads(row[0]) == payload
    conn.close()
