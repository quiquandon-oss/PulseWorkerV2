"""
Tests for research/stats_utils.py. Pure-function tests -- no database.

Run with: python3 -m pytest research/test_stats_utils.py -v
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import stats_utils as su  # noqa: E402


# ---- pearson_correlation ----

def test_perfect_positive_correlation():
    xs = [1, 2, 3, 4, 5]
    ys = [2, 4, 6, 8, 10]
    assert su.pearson_correlation(xs, ys) == 1.0


def test_perfect_negative_correlation():
    xs = [1, 2, 3, 4, 5]
    ys = [10, 8, 6, 4, 2]
    r = su.pearson_correlation(xs, ys)
    assert math.isclose(r, -1.0, abs_tol=1e-9)


def test_no_variation_returns_none_not_zero():
    xs = [5, 5, 5, 5]
    ys = [1, 2, 3, 4]
    assert su.pearson_correlation(xs, ys) is None


def test_fewer_than_two_points_returns_none():
    assert su.pearson_correlation([1], [2]) is None
    assert su.pearson_correlation([], []) is None


def test_mismatched_lengths_raise():
    try:
        su.pearson_correlation([1, 2], [1])
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---- fisher_z_ci_and_p ----

def test_fisher_z_returns_none_below_min_n():
    result = su.fisher_z_ci_and_p(0.5, su.MIN_N_FOR_FISHER_Z - 1)
    assert result["ci_low"] is None
    assert result["ci_high"] is None
    assert result["p_two_tailed"] is None


def test_fisher_z_none_r_returns_none():
    result = su.fisher_z_ci_and_p(None, 100)
    assert result["p_two_tailed"] is None


def test_fisher_z_zero_correlation_p_near_one():
    # r=0 -> z=0 -> z_stat=0 -> p = 2*(1-0.5) = 1.0
    result = su.fisher_z_ci_and_p(0.0, 100)
    assert math.isclose(result["p_two_tailed"], 1.0, abs_tol=1e-9)


def test_fisher_z_strong_correlation_large_n_significant():
    result = su.fisher_z_ci_and_p(0.5, 200)
    assert result["p_two_tailed"] < 0.05
    assert result["ci_low"] < 0.5 < result["ci_high"]


def test_fisher_z_ci_widens_with_smaller_n():
    wide = su.fisher_z_ci_and_p(0.3, 10)
    narrow = su.fisher_z_ci_and_p(0.3, 1000)
    assert (wide["ci_high"] - wide["ci_low"]) > (narrow["ci_high"] - narrow["ci_low"])


# ---- benjamini_hochberg ----

def test_benjamini_hochberg_none_passthrough():
    results = su.benjamini_hochberg([0.01, None, 0.5])
    assert results[1]["p_corrected"] is None
    assert results[1]["significant"] is False


def test_benjamini_hochberg_all_significant_when_all_tiny():
    results = su.benjamini_hochberg([0.001, 0.002, 0.003], alpha=0.05)
    assert all(r["significant"] for r in results)


def test_benjamini_hochberg_reduces_significance_vs_raw():
    # Textbook property: BH correction is never more permissive than
    # marking everything significant at the raw threshold when only one
    # p-value is small among many.
    pvalues = [0.049] + [0.9] * 19
    results = su.benjamini_hochberg(pvalues, alpha=0.05)
    assert results[0]["p_corrected"] > 0.049
    assert results[0]["significant"] is False


def test_benjamini_hochberg_monotonic_in_rank():
    pvalues = [0.001, 0.01, 0.02, 0.04, 0.5]
    results = su.benjamini_hochberg(pvalues)
    corrected = [r["p_corrected"] for r in results]
    # sorted-by-rank corrected values must be non-decreasing (BH step-up
    # guarantee), even though inputs here happen to already be sorted.
    assert all(corrected[i] <= corrected[i + 1] + 1e-12 for i in range(len(corrected) - 1))


def test_benjamini_hochberg_empty_list():
    assert su.benjamini_hochberg([]) == []


# ---- partial_correlation ----

def test_partial_correlation_none_inputs():
    assert su.partial_correlation(None, 0.5, 0.5) is None


def test_partial_correlation_known_value():
    # r_xy=0.6, r_xz=0.3, r_zy=0.3 -> (0.6 - 0.09) / sqrt(0.91*0.91) = 0.51/0.91
    r = su.partial_correlation(0.6, 0.3, 0.3)
    assert math.isclose(r, 0.51 / 0.91, rel_tol=1e-9)


def test_partial_correlation_perfect_collinearity_undefined():
    assert su.partial_correlation(0.5, 1.0, 1.0) is None


# ---- ols_1var / ols_2var ----

def test_ols_1var_recovers_exact_line():
    xs = [1, 2, 3, 4, 5]
    ys = [3 + 2 * x for x in xs]
    slope, intercept = su.ols_1var(xs, ys)
    assert math.isclose(slope, 2.0, abs_tol=1e-9)
    assert math.isclose(intercept, 3.0, abs_tol=1e-9)


def test_ols_1var_zero_variance_x_returns_none():
    assert su.ols_1var([5, 5, 5], [1, 2, 3]) == (None, None)


def test_ols_2var_recovers_exact_plane():
    x1s = [1, 2, 3, 4, 5, 6]
    x2s = [2, 1, 4, 3, 6, 5]
    ys = [1 + 2 * a + 3 * b for a, b in zip(x1s, x2s)]
    b0, b1, b2 = su.ols_2var(x1s, x2s, ys)
    assert math.isclose(b0, 1.0, abs_tol=1e-6)
    assert math.isclose(b1, 2.0, abs_tol=1e-6)
    assert math.isclose(b2, 3.0, abs_tol=1e-6)


def test_ols_2var_singular_returns_none_triple():
    # x2 = 2*x1 exactly -> collinear -> singular normal equations
    x1s = [1, 2, 3, 4]
    x2s = [2, 4, 6, 8]
    ys = [1, 2, 3, 4]
    assert su.ols_2var(x1s, x2s, ys) == (None, None, None)


# ---- ols_nvar (PR5f) ----

def test_ols_nvar_matches_ols_2var_for_two_predictors():
    x1s = [1, 2, 3, 4, 5, 6]
    x2s = [2, 1, 4, 3, 6, 5]
    ys = [1 + 2 * a + 3 * b for a, b in zip(x1s, x2s)]
    b0_2var, b1_2var, b2_2var = su.ols_2var(x1s, x2s, ys)
    intercept, coefs = su.ols_nvar([x1s, x2s], ys)
    assert math.isclose(intercept, b0_2var, abs_tol=1e-6)
    assert math.isclose(coefs[0], b1_2var, abs_tol=1e-6)
    assert math.isclose(coefs[1], b2_2var, abs_tol=1e-6)


def test_ols_nvar_recovers_exact_hyperplane_four_predictors():
    n = 30
    x1s = [i % 5 for i in range(n)]
    x2s = [(i * 3) % 7 - 3 for i in range(n)]
    x3s = [(i * 5) % 11 - 5 for i in range(n)]
    x4s = [(i * 2) % 9 - 4 for i in range(n)]
    ys = [1.5 + 2 * a + 0.5 * b - 3 * c + 4 * d
          for a, b, c, d in zip(x1s, x2s, x3s, x4s)]
    intercept, coefs = su.ols_nvar([x1s, x2s, x3s, x4s], ys)
    assert math.isclose(intercept, 1.5, abs_tol=1e-6)
    assert math.isclose(coefs[0], 2.0, abs_tol=1e-6)
    assert math.isclose(coefs[1], 0.5, abs_tol=1e-6)
    assert math.isclose(coefs[2], -3.0, abs_tol=1e-6)
    assert math.isclose(coefs[3], 4.0, abs_tol=1e-6)


def test_ols_nvar_singular_returns_none_pair():
    x1s = [1, 2, 3, 4, 5, 6]
    x2s = [2, 4, 6, 8, 10, 12]  # x2 = 2*x1 exactly -> collinear
    ys = [1, 2, 3, 4, 5, 6]
    assert su.ols_nvar([x1s, x2s], ys) == (None, None)


def test_ols_nvar_insufficient_sample_returns_none_pair():
    x1s = [1, 2, 3]
    x2s = [2, 1, 4]
    x3s = [3, 5, 1]
    ys = [1, 2, 3]  # n=3, k=3 predictors -- needs n >= k+2 = 5
    assert su.ols_nvar([x1s, x2s, x3s], ys) == (None, None)


def test_ols_nvar_mismatched_column_lengths_returns_none_pair():
    assert su.ols_nvar([[1, 2, 3], [1, 2]], [1, 2, 3]) == (None, None)


# ---- rmse ----

def test_rmse_zero_for_perfect_prediction():
    assert su.rmse([1, 2, 3], [1, 2, 3]) == 0.0


def test_rmse_known_value():
    assert math.isclose(su.rmse([0, 0], [3, 4]), math.sqrt((9 + 16) / 2), rel_tol=1e-9)


def test_rmse_empty_returns_none():
    assert su.rmse([], []) is None
