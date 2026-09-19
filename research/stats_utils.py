"""
PR5c: shared, pure-function statistics primitives.

No database access, no network, no LLM, no third-party dependency (no
scipy/numpy/pandas) -- stdlib `math` only, matching movement_distribution.py's
own precedent of keeping the purely analytical layer dependency-free and
independently testable.

Method disclosure (PR5c Section 6/11/14 -- statistical choices must be
documented, not merely applied):

- Association (Level 2) uses the Pearson correlation coefficient, tested
  for significance via the Fisher z-transformation (Fisher, 1921), not a
  t-distribution/incomplete-beta computation -- this avoids a hand-rolled
  incomplete-beta-function implementation while remaining a standard,
  textbook technique for Pearson-r significance and confidence intervals.
  It uses only `math.erf`/`math.atanh`/`math.tanh` (all stdlib). The
  normal approximation underlying Fisher's z is accurate for the sample
  sizes this project's tables actually produce (n in the hundreds, never
  the single digits) and is documented here so results are reproducible,
  not asserted as the only valid choice.
- Pearson's r is invariant to any positive linear rescaling of either
  input series. That is precisely why PR5c's Level 1/Level 2/redundancy
  analysis does not normalize the 21 raw integer source scores before
  correlating them, even though Section 6 empirical inspection (see
  research/source_analysis.py docstring) shows they are NOT all on the
  same underlying scale -- normalization would not change any r, p, or
  CI computed here. It WOULD change the magnitude of an OLS regression
  coefficient, which is why regression slopes (used only for Level 3's
  incremental-error-reduction check, never for correlation) are always
  reported alongside each source's own raw min/max/mean/stddev (from
  the Level 1 report) rather than presented as a normalized, cross-
  source-comparable number.
- Multiple-testing correction uses Benjamini-Hochberg (1995) step-up FDR
  control, chosen (over the stricter Bonferroni) because PR5c runs many
  correlated tests (the same BTC forward return is reused as the outcome
  across every source at a given horizon, so tests are not independent)
  and Bonferroni's independence assumption would be a worse fit, not a
  more conservative one.
"""

import math

# Below this correlation sample size, the Fisher z normal approximation is
# not reported -- se = 1/sqrt(n-3) is undefined for n<=3, and results would
# be numerically unstable, not merely "wide", for n in {4,5}. Chosen as a
# reporting floor, not a claim that n=6 is definitively "enough" (see
# sample_size_status alongside every reported r elsewhere in this PR).
MIN_N_FOR_FISHER_Z = 6

# Standard normal two-tailed 95% critical value (z such that
# P(|Z|>z)=0.05). Hardcoded rather than computed because this module only
# ever reports 95% CIs (alpha=0.05 throughout PR5c) -- documented here
# rather than silently magic elsewhere.
Z_CRITICAL_95 = 1.959963984540054


def _clamp(value, low, high):
    return max(low, min(high, value))


def pearson_correlation(xs, ys):
    """Pearson product-moment correlation coefficient.

    Returns None (never raises, never fabricates a value) when there are
    fewer than 2 paired observations, or when either series has zero
    variance (correlation is mathematically undefined for a constant
    series -- reported as None, never as 0.0, since 0.0 would misleadingly
    imply "no relationship" rather than "undefined").
    """
    n = len(xs)
    if n != len(ys):
        raise ValueError(f"xs and ys must be the same length, got {n} and {len(ys)}")
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    r = cov / math.sqrt(var_x * var_y)
    return _clamp(r, -1.0, 1.0)


def normal_cdf(x):
    """Standard normal CDF via math.erf (stdlib, exact to float precision)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fisher_z_ci_and_p(r, n, z_critical=Z_CRITICAL_95):
    """95% CI and two-tailed p-value (null hypothesis r=0) for a Pearson r,
    via the Fisher z-transformation. See module docstring for method
    disclosure.

    Returns a dict with ci_low, ci_high, p_two_tailed all None (not
    fabricated, not zero) when r is None or n < MIN_N_FOR_FISHER_Z.
    """
    if r is None or n is None or n < MIN_N_FOR_FISHER_Z:
        return {"ci_low": None, "ci_high": None, "p_two_tailed": None, "z_stat": None}
    r_clamped = _clamp(r, -0.999999, 0.999999)
    z = math.atanh(r_clamped)
    se = 1.0 / math.sqrt(n - 3)
    z_lo = z - z_critical * se
    z_hi = z + z_critical * se
    z_stat = z / se
    p_two_tailed = 2.0 * (1.0 - normal_cdf(abs(z_stat)))
    return {
        "ci_low": math.tanh(z_lo),
        "ci_high": math.tanh(z_hi),
        "p_two_tailed": p_two_tailed,
        "z_stat": z_stat,
    }


def benjamini_hochberg(pvalues, alpha=0.05):
    """Benjamini-Hochberg step-up FDR correction.

    `pvalues` is a list in any order; entries may be None (reported back
    as None/not-significant, excluded from the correction's rank
    denominator -- a missing test is never silently treated as p=0 or
    p=1). Returns a list of dicts, same length and order as the input:
    {"p_raw": ..., "p_corrected": ..., "significant": bool}.

    p_corrected (the BH-adjusted q-value) is computed as the standard
    step-up adjustment: for the i-th smallest of m testable p-values,
    q_i = min(1, min_{j>=i} p_(j) * m / j) -- monotonic non-decreasing
    when read in ascending p-value order, which is what makes it a valid
    FDR bound rather than a per-test rescaling.
    """
    m = sum(1 for p in pvalues if p is not None)
    indexed = [(i, p) for i, p in enumerate(pvalues) if p is not None]
    indexed.sort(key=lambda t: t[1])

    corrected = [None] * len(pvalues)
    if m > 0:
        raw_q = [None] * m
        for rank, (orig_i, p) in enumerate(indexed, start=1):
            raw_q[rank - 1] = min(1.0, p * m / rank)
        # enforce monotonicity from the largest rank down (step-up)
        running_min = 1.0
        for k in range(m - 1, -1, -1):
            running_min = min(running_min, raw_q[k])
            raw_q[k] = running_min
        for k, (orig_i, p) in enumerate(indexed):
            corrected[orig_i] = raw_q[k]

    results = []
    for p, q in zip(pvalues, corrected):
        results.append({
            "p_raw": p,
            "p_corrected": q,
            "significant": (q is not None and q < alpha),
        })
    return results


def partial_correlation(r_xy, r_xz, r_zy):
    """First-order partial correlation of x and y controlling for z, from
    three pairwise Pearson correlations (all computed over the SAME
    sample -- callers must ensure that, this function does not verify
    it). Returns None when any input r is None or the denominator is
    zero (perfect collinearity with the control variable -- partial
    correlation undefined, not zero)."""
    if r_xy is None or r_xz is None or r_zy is None:
        return None
    denom = math.sqrt((1 - r_xz ** 2) * (1 - r_zy ** 2))
    if denom == 0:
        return None
    return _clamp((r_xy - r_xz * r_zy) / denom, -1.0, 1.0)


def ols_1var(xs, ys):
    """Closed-form simple linear regression y = intercept + slope * x.
    Returns (slope, intercept), or (None, None) if x has zero variance
    or fewer than 2 points."""
    n = len(xs)
    if n < 2:
        return None, None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        return None, None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov / var_x
    intercept = mean_y - slope * mean_x
    return slope, intercept


def ols_2var(x1s, x2s, ys):
    """Closed-form multiple linear regression y = b0 + b1*x1 + b2*x2 via
    the normal equations, solved directly (3x3 Cramer's rule) -- no
    numpy dependency. Returns (b0, b1, b2), or (None, None, None) if the
    system is singular (e.g. x1 and x2 perfectly collinear) or n < 3."""
    n = len(x1s)
    if n < 3:
        return None, None, None
    s1 = sum(x1s)
    s2 = sum(x2s)
    sy = sum(ys)
    s11 = sum(x * x for x in x1s)
    s22 = sum(x * x for x in x2s)
    s12 = sum(a * b for a, b in zip(x1s, x2s))
    s1y = sum(a * b for a, b in zip(x1s, ys))
    s2y = sum(a * b for a, b in zip(x2s, ys))

    # [ n  s1  s2 ] [b0]   [ sy ]
    # [ s1 s11 s12] [b1] = [ s1y]
    # [ s2 s12 s22] [b2]   [ s2y]
    a = [[n, s1, s2], [s1, s11, s12], [s2, s12, s22]]
    b = [sy, s1y, s2y]

    det = _det3(a)
    if abs(det) < 1e-12:
        return None, None, None

    result = []
    for col in range(3):
        a_col = [row[:] for row in a]
        for row_i in range(3):
            a_col[row_i][col] = b[row_i]
        result.append(_det3(a_col) / det)
    return tuple(result)


def _det3(m):
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def rmse(actual, predicted):
    """Root-mean-square error. Returns None for empty/mismatched input."""
    n = len(actual)
    if n == 0 or n != len(predicted):
        return None
    return math.sqrt(sum((a - p) ** 2 for a, p in zip(actual, predicted)) / n)
