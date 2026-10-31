"""Sample sizes for the frozen prospective plans (research/plans/2026-11-01-prospective-plans-h2.md).

Every required sample in those plans is computed here from a stated minimum effect, power, alpha and the number of
Holm-corrected comparisons; nothing is chosen by hand. Sizing uses the most stringent Holm step (alpha / m), so the
stated power holds for every comparison in the family.

  python3 research/power.py     # prints the table quoted in the plan
"""
from math import atanh, ceil, comb, sqrt
from statistics import NormalDist

_Z = NormalDist().inv_cdf


def n_correlation(r, alpha=0.05, power=0.80, m=1):
    """Two-sided test of a (partial) correlation = 0 via Fisher z: n = ((z_{1-a/2m} + z_power) / atanh(r))^2 + 3.
    For a partial correlation controlling for k covariates add k (returned separately by n_partial_correlation)."""
    a = alpha / m
    return ceil(((_Z(1 - a / 2) + _Z(power)) / atanh(abs(r))) ** 2 + 3)


def n_partial_correlation(r, k, alpha=0.05, power=0.80, m=1):
    return n_correlation(r, alpha, power, m) + k


def _binom_cdf(k, n, p):
    return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(0, k + 1))


def exact_sign_power(n, p1, alpha=0.05):
    """Power of the exact two-sided binomial test of H0 p = 0.5 at level alpha, when the true rate is p1."""
    # largest k_lo with P(X <= k_lo | 0.5) <= alpha/2; the test is symmetric under H0
    k_lo = -1
    while _binom_cdf(k_lo + 1, n, 0.5) <= alpha / 2:
        k_lo += 1
    k_hi = n - k_lo                       # reject if X <= k_lo or X >= k_hi
    if k_lo < 0:
        return 0.0
    upper = 1 - _binom_cdf(k_hi - 1, n, p1)
    lower = _binom_cdf(k_lo, n, p1)
    return upper + lower


def n_sign_test(p1, alpha=0.05, power=0.80, m=1, n_max=5000):
    """Smallest n such that the exact two-sided sign test at alpha/m reaches `power` at n and at every n up to n + 10
    (the exact test's power is saw-toothed; this avoids picking a lucky n)."""
    a = alpha / m
    for n in range(1, n_max):
        if all(exact_sign_power(k, p1, a) >= power for k in range(n, n + 11)):
            return n
    raise ValueError("n_max too small")


PLAN_TABLE = {
    # key: (kind, effect, m, k_covariates)
    "EXP-025 hloi": ("partial_r", 0.20, 1, 1),
    "EXP-027 rrs-v1": ("partial_r", 0.20, 1, 1),
    "EXP-028 DVOL": ("partial_r", 0.20, 2, 1),
    "EXP-028 USDT supply": ("partial_r", 0.20, 2, 1),
    "EXP-002 per horizon": ("sign", 0.65, 2, 0),
    "EXP-011": ("sign", 0.60, 1, 0),
}


def plan_table(alpha=0.05, power=0.80):
    out = {}
    for name, (kind, eff, m, k) in PLAN_TABLE.items():
        out[name] = n_partial_correlation(eff, k, alpha, power, m) if kind == "partial_r" else n_sign_test(eff, alpha, power, m)
    return out


if __name__ == "__main__":
    for name, n in plan_table().items():
        kind, eff, m, k = PLAN_TABLE[name]
        print(f"{name:24s} {kind:9s} effect {eff:.2f}  Holm m={m}  covariates={k}  ->  n = {n}")
